import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Isolated child messenger contracts. No Telegram token, request or real send."""
import importlib
import gc
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import weakref
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import kitten_messenger as kitten
from family_messages import MessageStore
from family_context import ChatLog


class FakeChatLog:
    def __init__(self):self.records=[]
    def append_many(self, records):
        self.records.extend(dict(row) for row in records)
        return [{'id':str(len(self.records)), 'inserted':True} for row in records]
    def append(self, role, text, **values):
        return self.append_many([dict(role=role,text=text,**values)])[0]


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='kitten-log-',dir=base)
        self.log=ChatLog(self.temporary.name)
        self.model=kitten.LocalConversation(kitten.CatDialogue(),self.log,'fixture-session')
    def tearDown(self):self.temporary.cleanup()
    def rows(self):
        import json
        return [json.loads(line) for line in self.log.export_jsonl().read_text(encoding='utf-8').splitlines()]
    def test_local_turn_and_family_source_ids_are_separate_and_deduplicated(self):
        self.model.submit('Привет','turn-1')
        controller=mock.Mock()
        controller.send.return_value={'status':'queued'}
        self.model.send(controller,'Папа, привет','request-1')
        controller.snapshot.return_value={'channel':{},'worker_ready':False,'messages':[{'id':'canonical-out','client_id':'request-1','direction':'outgoing','text':'Папа, привет'},{'id':'parent-1','direction':'incoming','text':'Привет, сынок'}]}
        self.model.snapshot(controller);self.model.snapshot(controller)
        rows=self.rows()
        self.assertEqual(len(rows),4)
        self.assertEqual(sum(r['role']=='parent' for r in rows),1)
        self.assertEqual(sum(r['text']=='Папа, привет' for r in rows),1)
        self.assertTrue(all(r['person_verified']==0 for r in rows))
    def test_disk_retry_keeps_the_same_generated_reply(self):
        original=self.log.append_many
        self.log.append_many=mock.Mock(side_effect=OSError('fixture'))
        with self.assertRaises(OSError):self.model.submit('Привет','turn-1')
        answer=self.model.pending[2]['text']
        self.log.append_many=original
        result=self.model.submit('Привет','turn-1')
        self.assertEqual(result['text'],answer)
        self.assertEqual(len(self.rows()),2)
    def test_danger_help_and_parent_contact_survive_separate_log_failure(self):
        self.log.append_many=mock.Mock(side_effect=OSError('fixture disk denied'))
        result=self.model.submit('Я хочу навредить себе','critical-1')
        self.assertEqual(result['intent'],'self_harm')
        self.assertIs(result['history_saved'],False)
        self.assertRegex(result['text'],'взросл|помощ')
        controller=mock.Mock();controller.send.return_value={'status':'queued'}
        result=self.model.send(controller,'Папа, мне нужна помощь','family-critical')
        controller.send.assert_called_once_with('Папа, мне нужна помощь','family-critical')
        self.assertIs(result['history_saved'],False)
    def test_study_notice_is_logged_once_and_never_changes_a_timer(self):
        event={'id':'session-1:study_started','kind':'study_started','author':'local_study_timer','text':'Сейчас время уроков.'}
        study=SimpleNamespace(poll=lambda:{'status':{'state':'active','active':True,'remaining_seconds':1800},'events':[event]})
        self.model.study=study
        controller=mock.Mock();controller.snapshot.return_value={'messages':[]}
        first=self.model.snapshot(controller);second=self.model.snapshot(controller)
        self.assertEqual(first['study_events'],[event]);self.assertEqual(second['study_events'],[])
        self.assertEqual(self.rows()[0]['role'],'system')
        controller.send.assert_not_called()


class FakeBridge:
    def __init__(self, state='ready'):
        self.state=state;self.calls=[];self.fail=None

    def channel_status(self):
        return {'state':self.state,'channel_ready':self.state=='ready'}

    def send_message(self,text,request_id):
        self.calls.append((text,request_id,threading.get_ident()))
        if self.fail:raise self.fail
        return {'id':'receipt-1','status':'queued','worker_state':'ready'}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='kitten-',dir=base)
        self.store=MessageStore(self.temporary.name)
        self.bridge=FakeBridge()
        self.worker=mock.Mock()
        self.controller=kitten.MessengerController(self.store,self.bridge,self.worker)

    def tearDown(self):self.temporary.cleanup()

    def test_import_is_inert(self):
        with mock.patch('family_messages.MessageStore') as make_store,mock.patch('family_agent.ensure_running') as start:
            importlib.reload(kitten)
            make_store.assert_not_called();start.assert_not_called()

    def test_snapshot_does_not_send_and_includes_incoming_shown(self):
        self.store.enqueue_reply('Сын пишет',client_id='fixture-1')
        # Local incoming fixture using the ordinary store API.
        row=self.store.add_incoming('Папа отвечает',telegram_update_id=1,telegram_message_id=2)
        self.store.mark_incoming_shown(row['id'])
        snapshot=self.controller.snapshot()
        self.assertEqual(len(snapshot['messages']),2)
        self.assertEqual(snapshot['messages'][-1]['text'],'Папа отвечает')
        self.assertEqual(snapshot['messages'][-1]['status'],'shown')
        self.assertEqual(self.bridge.calls,[])
        self.worker.assert_called_once_with(self.store)

    def test_unready_snapshot_does_not_start_worker(self):
        for state in ('not_configured','not_paired','paused'):
            self.bridge.state=state
            snapshot=self.controller.snapshot()
            self.assertFalse(snapshot['worker_ready'])
        self.worker.assert_not_called()

    def test_send_goes_only_through_fixed_bridge_and_keeps_id(self):
        receipt=self.controller.send('Привет, папа!','request-123')
        self.assertEqual(receipt['status'],'queued')
        self.assertEqual(self.bridge.calls[0][:2],('Привет, папа!','request-123'))
        self.assertEqual(self.store.list_messages(),[])

    def test_invalid_draft_never_reaches_bridge(self):
        for text in ('','  ','x'*4001,'bad\x00'):
            with self.subTest(text=text[:10]),self.assertRaises(ValueError):
                self.controller.send(text,'request-123')
        self.assertEqual(self.bridge.calls,[])

    def test_local_kitten_never_invokes_transport(self):
        for text in ('привет','мне грустно','я делаю уроки','котик','как написать папе','что ты умеешь','неизвестная фраза'):
            self.assertTrue(kitten.kitten_reply(text))
        self.assertEqual(self.bridge.calls,[])
        self.assertEqual(kitten.CatDialogue().reply('ничего')['intent'],'fallback')

    def test_arbitrary_error_never_leaks_remote_payload(self):
        secret='https://api.telegram.org/bot123:secret_token/very_private_text'
        self.assertNotIn(secret,kitten.friendly_error(RuntimeError(secret)))
        self.assertNotIn('secret_token',kitten.friendly_error(RuntimeError(secret)))
        self.assertIn('не отправляй повторно',kitten.RECEIPTS['uncertain'])

    @unittest.skipUnless(os.name=='nt','Native Windows instance signal')
    def test_one_instance_and_repeat_launch_signal_without_storage(self):
        one=kitten._InstanceSignal(Path(self.temporary.name)/'inert-signal-root')
        two=kitten._InstanceSignal(Path(self.temporary.name)/'inert-signal-root')
        three=kitten._InstanceSignal(Path(self.temporary.name)/'inert-signal-root')
        try:
            self.assertTrue(one.acquire())
            self.assertFalse(two.acquire())
            self.assertTrue(one.requested())
            self.assertFalse(one.requested())
            one.close()
            self.assertTrue(three.acquire())
            self.assertFalse((Path(self.temporary.name)/'inert-signal-root').exists())
        finally:
            one.close();two.close();three.close()


class UiTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.bridge=FakeBridge()
        self.controller=mock.Mock()
        self.controller.send.side_effect=self.bridge.send_message
        self.controller.snapshot.return_value={'channel':{'state':'ready','channel_ready':True},'messages':[],'worker_ready':True}
        self.log=FakeChatLog()
        self.family=SimpleNamespace(family_reply=lambda text:None)
        self.study=SimpleNamespace(poll=lambda:{'status':{'state':'idle','active':False},'events':[]})
        self.speech=SimpleNamespace(status=lambda:{'available':False},stop=lambda:None)
        ai_gateway=mock.Mock()
        ai_gateway.status.return_value={'enabled':False,'ready':False,'state':'disabled'}
        ai_gateway.respond.side_effect=AssertionError('Legacy chat fixture must remain offline')
        self.window=kitten.KittenMessenger(controller=self.controller,start_background=False,chat_log=self.log,family_context=self.family,study_monitor=self.study,transcriber=self.speech,ai_settings=mock.Mock(),ai_gateway=ai_gateway)
        self.window.withdraw();self.window.update()

    def tearDown(self):
        window=self.window
        self.window=None
        if window is not None and not window._closed:
            window._busy_send=False;window.close()
        del window
        # Destroyed Tk widgets contain Python cycles. Collect them on their
        # creating thread before another fixture starts allocating in a worker.
        gc.collect()

    def pump(self,predicate,seconds=3):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:
            self.window.update();time.sleep(.015)
        self.assertTrue(predicate())

    def test_quick_phrase_only_edits_and_local_chat_sends_nothing(self):
        self.window.fill_phrase('Папа, привет!')
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Папа, привет!')
        self.controller.send.assert_not_called()
        self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        self.controller.send.assert_not_called()
        self.assertIn('Папе ничего не отправлено',self.window.notice.get())
        self.assertEqual([row['role'] for row in self.log.records],['user','kitten'])

    def test_dialogue_keeps_context_and_does_not_clear_draft_on_log_failure(self):
        self.window.fill_phrase('I am angry');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        self.assertEqual(self.window.dialogue.pending,'calm')
        self.window.fill_phrase('Не сейчас');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        self.assertTrue(self.window.dialogue.coaching_declined)
        self.assertEqual(self.window.suggestions_frame.winfo_children(),[])
        self.log.append_many=mock.Mock(side_effect=OSError('fixture disk full'))
        self.window.fill_phrase('Поговорим о котах');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Поговорим о котах')
        self.assertIn('оставлен',self.window.notice.get())

    def test_family_snapshot_and_send_attempts_are_logged_without_auto_send(self):
        self.controller.snapshot.return_value={'channel':{'state':'ready','channel_ready':True},'messages':[{'id':'incoming-1','direction':'incoming','text':'Привет от папы','status':'shown'}],'worker_ready':True}
        self.window.refresh();self.pump(lambda:not self.window._busy_refresh)
        self.assertEqual(self.log.records[0]['role'],'parent')
        self.controller.send.assert_not_called()
        self.window.fill_phrase('Ответ папе');self.window.send_draft()
        self.pump(lambda:not self.window._busy_send)
        outgoing=[r for r in self.log.records if r.get('role')=='user' and r.get('kind')=='family']
        self.assertEqual(outgoing[0]['text'],'Ответ папе')

    def test_critical_response_still_visible_when_history_disk_fails(self):
        self.log.append_many=mock.Mock(side_effect=OSError('fixture'))
        self.window.fill_phrase('Я хочу убить себя');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        self.assertIn('взросл',self.window.transcript.get('1.0','end'))
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Я хочу убить себя')
        self.assertIn('сохранить не получилось',self.window.notice.get())

    def test_deliberate_send_runs_off_tk_and_clears_only_after_ack(self):
        self.window.fill_phrase('Я тебя люблю!')
        main_thread=threading.get_ident()
        self.window.send_draft()
        self.pump(lambda:not self.window._busy_send)
        self.assertEqual(len(self.bridge.calls),1)
        self.assertNotEqual(self.bridge.calls[0][2],main_thread)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'')
        self.assertIn('В очереди',self.window.notice.get())

    def test_error_preserves_draft_and_retry_reuses_request_id(self):
        self.bridge.fail=RuntimeError('unsafe secret remote payload')
        self.window.fill_phrase('Папа, позвони')
        self.window.send_draft();self.pump(lambda:not self.window._busy_send)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Папа, позвони')
        self.assertNotIn('unsafe',self.window.notice.get())
        request=self.bridge.calls[0][1]
        self.window.send_draft();self.pump(lambda:not self.window._busy_send)
        self.assertEqual(self.bridge.calls[1][1],request)

    def test_uncertain_same_text_not_resent_and_close_waits_send(self):
        self.controller.send.return_value={'id':'r','status':'uncertain'}
        self.controller.send.side_effect=None
        self.window.fill_phrase('Один раз')
        self.window.send_draft();self.window.close()
        self.assertFalse(self.window._closed)
        self.pump(lambda:not self.window._busy_send)
        self.window.fill_phrase('Один раз');self.window.send_draft()
        self.assertEqual(self.controller.send.call_count,1)
        self.assertIn('неизвестен',self.window.notice.get())

    def test_mini_chat_roundtrip_keeps_small_minimum_size(self):
        self.window.show_mini();self.window.update()
        self.assertEqual(tuple(map(int,self.window.minsize())),(176,205))
        self.window.open_chat();self.window.update()
        self.assertEqual(tuple(map(int,self.window.minsize())),(440,570))

    def test_voice_is_explicit_and_recording_is_not_claimed_as_transcription(self):
        voice=mock.Mock()
        voice.speak.return_value={'status':'spoken'}
        voice.record.return_value={'status':'recorded','path':'fixture-voice.wav','asr_available':False}
        self.window._voice=voice
        self.window.fill_phrase('Привет');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue)
        voice.speak.assert_not_called();voice.record.assert_not_called()
        self.window.speak_answer();self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(voice.speak.call_args.args,(self.window._last_cat_text,))
        self.assertIsInstance(voice.speak.call_args.kwargs['cancel_event'],threading.Event)
        self.window.record_voice();self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(voice.record.call_args.kwargs['seconds'],8)
        self.assertIn('Распознавание',self.window.notice.get())
        self.assertTrue(any('fixture-voice.wav' in r['text'] for r in self.log.records))
        self.controller.send.assert_not_called()

    def test_transcription_only_fills_unchanged_empty_draft_without_dialogue_or_send(self):
        voice=mock.Mock();voice.record.return_value={'status':'recorded','path':'fixture.wav'}
        speech=mock.Mock();speech.status.return_value={'available':True}
        speech.transcribe.return_value={'status':'transcribed','text':'Папа, привет','language':'ru','local':True}
        self.window._voice=voice;self.window._transcriber=speech
        self.window.record_voice();self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Папа, привет')
        self.assertEqual(self.window._local,[])
        self.controller.send.assert_not_called()
        self.assertIn('Ничего не отправлено',self.window.notice.get())
        self.assertTrue(speech.transcribe.call_args.kwargs.get('cancel_event') is not None)

    def test_new_typing_is_preserved_and_recognized_preview_is_explicitly_appended(self):
        entered=threading.Event();release=threading.Event()
        voice=mock.Mock();voice.record.return_value={'status':'recorded','path':'fixture.wav'}
        speech=mock.Mock();speech.status.return_value={'available':True}
        def recognize(_path,**_kwargs):
            entered.set();release.wait(3)
            return {'status':'transcribed','text':'Новая запись','language':'ru','local':True}
        speech.transcribe.side_effect=recognize
        self.window._voice=voice;self.window._transcriber=speech
        self.window.record_voice()
        try:
            self.assertTrue(entered.wait(2))
            self.pump(lambda:'Распознаю' in self.window.notice.get())
            self.window.draft.insert('1.0','Мой текст')
            self.window.send_draft();self.window.talk_to_cat()
            self.controller.send.assert_not_called();self.assertFalse(self.window._busy_dialogue)
        finally:release.set()
        self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Мой текст')
        self.assertEqual(self.window._recognized_preview,'Новая запись')
        self.window.use_recognized()
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Мой текст\nНовая запись')
        self.assertIsNone(self.window._recognized_preview)
        self.controller.send.assert_not_called()

    def test_edit_then_delete_still_requires_review_of_transcription(self):
        self.window._voice=mock.Mock()
        self.window._voice.record.return_value={'status':'recorded','path':'fixture.wav'}
        speech=mock.Mock();speech.status.return_value={'available':True}
        speech.transcribe.return_value={'status':'transcribed','text':'Записанная фраза','language':'ru','local':True}
        self.window._transcriber=speech
        pending=[]
        self.window._start=lambda kind,work:pending.append((kind,work))
        self.window.record_voice()
        self.window.draft.insert('1.0','x');self.window.update()
        self.window.draft.delete('1.0','end');self.window.update()
        kind,work=pending.pop();self.window._results.put((kind,work(),None))
        self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'')
        self.assertEqual(self.window._recognized_preview,'Записанная фраза')

    def test_stop_before_queued_worker_never_opens_mic_and_stale_result_is_ignored(self):
        voice=mock.Mock();self.window._voice=voice
        pending=[];self.window._start=lambda kind,work:pending.append((kind,work))
        self.window.record_voice();request=self.window._voice_request
        self.window.stop_voice()
        kind,work=pending.pop();self.window._results.put((kind,work(),None))
        self.pump(lambda:not self.window._busy_voice)
        voice.record.assert_not_called()
        self.window._results.put(('voice_record:'+request,{'status':'recorded','transcription':'transcribed','text':'Устаревший текст'},None))
        self.window._pump()
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'')

    def test_mini_cancels_capture_and_keeps_stop_visible_until_finished(self):
        voice=mock.Mock();self.window._voice=voice
        pending=[];self.window._start=lambda kind,work:pending.append((kind,work))
        self.window.record_voice();self.window.show_mini()
        self.assertTrue(self.window._voice_cancel.is_set())
        self.assertFalse(self.window.overrideredirect())
        kind,work=pending.pop();self.window._results.put((kind,work(),None))
        self.pump(lambda:not self.window._busy_voice)
        self.window.show_mini();self.assertTrue(self.window.overrideredirect())

    def test_stop_during_recognition_discards_late_text_and_preserves_typing(self):
        entered=threading.Event();release=threading.Event()
        voice=mock.Mock();voice.record.return_value={'status':'recorded','path':'fixture.wav'}
        speech=mock.Mock();speech.status.return_value={'available':True}
        def recognize(_path,**_kwargs):
            entered.set();release.wait(3)
            return {'status':'transcribed','text':'Поздний результат','language':'ru','local':True}
        speech.transcribe.side_effect=recognize
        self.window._voice=voice;self.window._transcriber=speech
        self.window.record_voice()
        try:
            self.assertTrue(entered.wait(2))
            self.window.draft.insert('1.0','Свежий текст');self.window.stop_voice()
            speech.stop.assert_called_once();voice.stop.assert_called_once()
        finally:release.set()
        self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Свежий текст')
        self.assertIsNone(self.window._recognized_preview)
        self.controller.send.assert_not_called()

    def test_close_before_queued_capture_cancels_without_starting_microphone(self):
        voice=mock.Mock();self.window._voice=voice
        pending=[];self.window._start=lambda kind,work:pending.append((kind,work))
        self.window.record_voice();self.window.close()
        self.assertFalse(self.window._closed)
        kind,work=pending.pop();self.window._results.put((kind,work(),None))
        self.pump(lambda:self.window._closed)
        voice.record.assert_not_called()

    def test_session_auto_voice_only_speaks_kitten_reply_and_defaults_off(self):
        voice=mock.Mock();voice.speak.return_value={'status':'spoken'};self.window._voice=voice
        self.assertFalse(self.window.auto_speak.get())
        self.window.auto_speak.set(True)
        self.window.fill_phrase('Привет');self.window.talk_to_cat()
        self.pump(lambda:not self.window._busy_dialogue and not self.window._busy_voice)
        voice.speak.assert_called_once()
        self.assertEqual(voice.speak.call_args.args[0],self.window._last_cat_text)
        self.controller.snapshot.return_value={'channel':{},'messages':[{'direction':'incoming','id':'private-parent','text':'Личное сообщение папы'}]}
        self.window.refresh();self.pump(lambda:not self.window._busy_refresh)
        self.assertEqual(voice.speak.call_count,1)

    def test_family_send_preempts_speech_and_owns_buttons_until_its_receipt(self):
        speech_started = threading.Event()
        speech_release = threading.Event()
        send_started = threading.Event()
        send_release = threading.Event()
        voice = mock.Mock()

        def speak(_text, *, cancel_event):
            speech_started.set()
            speech_release.wait(4)
            return {'status': 'spoken'}

        def send(_text, _request_id):
            send_started.set()
            send_release.wait(4)
            return {'id': 'fixture-queued', 'status': 'queued'}

        voice.speak.side_effect = speak
        self.controller.send.side_effect = send
        self.window._voice = voice
        self.window._last_cat_text = 'Первый ответ котика'
        self.window.speak_answer()
        try:
            self.assertTrue(speech_started.wait(2))
            self.assertEqual(self.window.send_button.cget('state'), 'normal')
            self.assertEqual(self.window.cat_button.cget('state'), 'normal')
            self.window.fill_phrase('Папа, мне нужна помощь')
            self.window.send_draft()
            self.assertTrue(send_started.wait(2))
            self.assertTrue(voice.speak.call_args.kwargs['cancel_event'].is_set())
            voice.stop.assert_called_once()
            speech_release.set()
            self.pump(lambda: not self.window._busy_voice)
            self.assertEqual(self.window.send_button.cget('state'), 'disabled')
            self.assertEqual(self.window.cat_button.cget('state'), 'disabled')
            self.assertIn('Передаю', self.window.notice.get())
        finally:
            speech_release.set()
            send_release.set()
        self.pump(lambda: not self.window._busy_send)
        self.assertIn('В очереди', self.window.notice.get())
        self.controller.send.assert_called_once()

    def test_next_cat_reply_waits_to_speak_until_superseded_helper_exits(self):
        speech_started = threading.Event()
        speech_release = threading.Event()
        voice = mock.Mock()

        def speak(_text, *, cancel_event):
            if not speech_started.is_set():
                speech_started.set()
                speech_release.wait(4)
            return {'status': 'spoken'}

        voice.speak.side_effect = speak
        self.window._voice = voice
        self.window.auto_speak.set(True)
        self.window.fill_phrase('Привет')
        self.window.talk_to_cat()
        try:
            self.pump(speech_started.is_set)
            first_text = self.window._last_cat_text
            self.window.fill_phrase('Мне грустно')
            self.window.talk_to_cat()
            self.pump(lambda: not self.window._busy_dialogue)
            second_text = self.window._last_cat_text
            self.assertNotEqual(second_text, first_text)
            voice.speak.assert_called_once()
            self.assertTrue(voice.speak.call_args.kwargs['cancel_event'].is_set())
            self.assertIn('Беседа сохранена', self.window.notice.get())
        finally:
            speech_release.set()
        self.pump(lambda: voice.speak.call_count == 2 and not self.window._busy_voice)
        self.assertEqual([call.args[0] for call in voice.speak.call_args_list], [first_text, second_text])
        self.assertFalse(voice.speak.call_args.kwargs['cancel_event'].is_set())
        self.controller.send.assert_not_called()

    def test_missing_microphone_is_friendly_and_preserves_draft(self):
        class MicrophoneError(RuntimeError):code='microphone_unavailable'
        voice=mock.Mock();voice.record.side_effect=MicrophoneError('private native error')
        self.window._voice=voice;self.window.fill_phrase('Несохранённый черновик')
        self.window.record_voice();self.pump(lambda:not self.window._busy_voice)
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Несохранённый черновик')
        self.assertIn('Микрофон недоступен',self.window.notice.get())
        self.assertNotIn('native',self.window.notice.get())

    def test_controls_are_accessible_on_720p_sized_window_with_scrollable_body(self):
        self.window.deiconify();self.window.geometry('470x610+0+0');self.window.update()
        self.window._recognized_preview='Проверка текста'
        self.window.recognized_frame.pack(fill='x',before=self.window.draft)
        self.window.notice.set('Запись сохранена на этом ПК. Распознавание пока недоступно: локальная модель не настроена. Можно написать текстом.')
        self.window.update()
        for widget in (self.window.draft,self.window.send_button,self.window.cat_button,self.window.microphone_button,self.window.stop_voice_button,self.window.refresh_button):
            self.assertTrue(widget.winfo_ismapped())
            top=widget.winfo_rooty()-self.window.winfo_rooty()
            self.assertGreaterEqual(top,0)
            self.assertLessEqual(top+widget.winfo_height(),self.window.winfo_height())
        self.assertGreater(self.window.body.winfo_reqheight(),self.window.body_canvas.winfo_height())
        self.assertGreaterEqual(self.window.body_canvas.winfo_height(),48)
        self.assertEqual(self.window.chat.grid_rowconfigure(2)['weight'],1)

    def test_initial_chat_keeps_controls_inside_a_560_pixel_work_area(self):
        with mock.patch.object(kitten,'desktop_work_area',return_value=(0,0,800,560)):
            self.window.open_chat();self.window.update()
        self.window._recognized_preview='Распознанный текст'
        self.window.recognized_frame.pack(fill='x',before=self.window.draft)
        self.window.notice.set('Запись сохранена на этом ПК. Распознавание пока недоступно: локальная модель не настроена. Можно написать текстом.')
        self.window.update()
        self.assertFalse(self.window.header.winfo_ismapped())
        self.assertLessEqual(self.window.winfo_rooty()+self.window.winfo_height(),560)
        for widget in (self.window.draft,self.window.send_button,self.window.cat_button,self.window.microphone_button,self.window.stop_voice_button,self.window.refresh_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_rooty(),0)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),560)

    def test_tricks_editor_launch_is_explicit_and_does_not_send_or_change_draft(self):
        launcher=mock.Mock();self.window._tricks_launcher=launcher
        self.window.fill_phrase('Папа, привет!');launcher.assert_not_called()
        self.window.open_tricks();launcher.assert_called_once_with()
        self.assertEqual(self.window.draft.get('1.0','end-1c'),'Папа, привет!')
        self.controller.send.assert_not_called()
        self.assertIn('режиме уроков',self.window.notice.get())
        launcher.side_effect=RuntimeError('private installation path')
        self.window.open_tricks()
        self.assertIn('недоступен',self.window.notice.get());self.assertNotIn('private',self.window.notice.get())

    def test_background_refresh_cannot_own_last_gui_reference(self):
        entered=threading.Event();release=threading.Event();done=threading.Event()
        snapshot={'channel':{'state':'ready','channel_ready':True},'messages':[],'worker_ready':True}
        def blocked():
            entered.set()
            try:
                release.wait(3)
                return snapshot
            finally:done.set()
        self.controller.snapshot.side_effect=blocked
        window=self.window
        self.window=None
        reference=weakref.ref(window)
        try:
            window.refresh()
            self.assertTrue(entered.wait(2))
            window.close()
            del window
            gc.collect()
            self.assertIsNone(reference(),'Background work must not retain any Tk root or widget')
        finally:
            release.set()
            self.assertTrue(done.wait(2))


if __name__=='__main__':unittest.main()
