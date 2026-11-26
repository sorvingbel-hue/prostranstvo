"""Synthetic parent-channel envelopes, fake callbacks/outbox and temporary files."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'LocalProduct/src'))
sys.path.insert(0,str(Path(__file__).parent))
import board_telegram as bt

BINDING='a'*64
OTHER='b'*64
DAY=date(2026,11,26)
STUDY='c'*32


def plan():
    return {'date':DAY.isoformat(),'revision':123,'status':'submitted','game_minutes':60,
            'feedback':'','study_session_id':STUDY,
            'tasks':[{'text':'Synthetic task one','status':'done','points':20},
                     {'text':'Synthetic task two','status':'done','points':10}],
            'quality_criteria':'Agreed fixture criteria'}


class BoardTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'ledger'
        self.now=10000.0
        self.day=DAY
        self.plan=plan()
        self.get_plan=mock.Mock(side_effect=lambda:deepcopy(self.plan))
        self.counter=0
        def nonce():
            self.counter+=1
            return str(self.counter).zfill(32)
        self.board=bt.BoardTelegram(self.root,self.get_plan,clock=lambda:self.now,
                                   today=lambda:self.day,nonce=nonce)
        self.callback=mock.Mock(return_value={'state':'approved'})
        self.authorized=mock.Mock(return_value=True)
        self.update=1

    def send(self,text,binding=BINDING,update=None,message_id=10):
        if update is None:
            update=self.update;self.update+=1
        return self.board.accept_message(text,update,message_id,binding)

    def read(self):
        return json.loads(self.board.path.read_text(encoding='utf-8'))

    def reply_text(self):
        return self.read()['replies'][-1]['text']

    def issue(self):
        self.send('/plan')
        text=self.reply_text()
        return text.split('/approve ',1)[1].split('\n',1)[0]

    def queue(self,quality=None,comment=None):
        code=self.issue()
        if comment is None:
            comment='Finish the agreed fixture detail.' if quality is not None and quality<100 else ''
        self.send('/approve '+code+(' '+str(quality) if quality is not None else '')+(' '+comment if comment else ''))
        return code

    def dispatch(self,**kwargs):
        return self.board.process_pending(self.callback,kwargs.get('is_authorized',self.authorized))

    def test_constructor_status_are_inert_and_redacted(self):
        self.assertFalse(self.root.exists())
        status=self.board.status()
        self.assertEqual(status['pending_approvals'],0)
        self.assertFalse(self.root.exists())
        self.get_plan.assert_not_called()
        self.assertNotIn('token_hash',json.dumps(status))

    def test_non_commands_do_not_touch_storage_or_callbacks(self):
        for text in ('Hello family','/planet','/approveExtra','ordinary /approve text',None):
            self.assertEqual(self.board.accept_message(text,1,10,BINDING),{'handled':False})
        self.assertFalse(self.root.exists())
        self.get_plan.assert_not_called()

    def test_plan_issues_bounded_random_code_tied_to_exact_snapshot(self):
        code=self.issue();token=self.read()['tokens'][0]
        self.assertEqual(len(code),32)
        self.assertEqual(token['token_hash'],hashlib.sha256(code.encode()).hexdigest())
        self.assertEqual(token['date'],DAY.isoformat())
        self.assertEqual(token['revision'],123)
        self.assertEqual(token['study_id'],STUDY)
        self.assertEqual(token['binding'],BINDING)
        self.assertEqual(token['expires_utc'],10900)
        self.assertEqual(token['state'],'issued')
        self.assertIsNone(token['quality_percent'])
        self.assertIn('Synthetic task one',self.reply_text())
        self.callback.assert_not_called()

    def test_open_and_approved_plans_visible_without_token(self):
        for state in ('open','approved'):
            with self.subTest(state=state):
                self.plan['status']=state
                self.send('/plan')
                self.assertIn('Synthetic task one',self.reply_text())
                self.assertNotIn('/approve ',self.reply_text())
        self.assertEqual(self.read()['tokens'],[])

    def test_absent_past_or_malformed_plan_gives_static_response(self):
        for invalid in (None,{},dict(plan(),date='2026-11-25'),dict(plan(),revision=True),
                        dict(plan(),game_minutes=1000),dict(plan(),study_session_id='bad'),
                        dict(plan(),tasks=[{'text':'bad\x00text','status':'done'}])):
            with self.subTest(invalid=invalid):
                self.plan=invalid
                self.send('/plan')
                self.assertIn('Не удалось',self.reply_text())
                self.assertNotIn('/approve ',self.reply_text())
        self.assertFalse(self.read()['tokens'])

    def test_get_plan_failure_is_not_echoed(self):
        self.get_plan.side_effect=RuntimeError('PRIVATE KEY ABC')
        self.send('/plan')
        self.assertNotIn('PRIVATE',self.reply_text())
        self.assertFalse(self.read()['tokens'])

    def test_reserved_invalid_syntax_is_handled_not_child_message(self):
        for text in ('/approve','/approve BAD','/plan extra','/plan@ForeignBot',
                     '/approve@ForeignBot 123','/approve '+'z'*32+' 101',
                     '/approve '+'z'*32+' -1','/approve '+'z'*32+' 01'):
            with self.subTest(text=text):
                self.assertTrue(self.send(text)['handled'])
                self.assertIn('Команда не выполнена',self.reply_text())
        self.callback.assert_not_called()

    def test_forwarded_commands_are_durable_rejected_not_executed(self):
        self.board.accept_message('/plan',1,10,BINDING,forwarded=True)
        self.assertFalse(self.read()['tokens'])
        self.assertIn('Пересланные команды',self.reply_text())
        self.get_plan.assert_not_called()
        with self.assertRaisesRegex(bt.BoardTelegramError,'update_identity_changed'):
            self.board.accept_message('/plan',1,10,BINDING,forwarded=False)

    def test_invalid_envelope_has_no_storage_effect(self):
        for update,message,binding in ((True,1,BINDING),(-1,1,BINDING),(1,0,BINDING),
                                       (1,1,'invalid'),(2**63,1,BINDING)):
            with self.assertRaises(bt.BoardTelegramError):
                self.board.accept_message('/plan',update,message,binding)
        self.assertFalse(self.root.exists())

    def test_duplicate_update_returns_exact_receipt_no_new_nonce_or_reply(self):
        first=self.send('/plan',update=10)
        before=self.board.path.read_bytes()
        second=self.send('/plan',update=10)
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['reply_id'],second['reply_id'])
        self.assertEqual(before,self.board.path.read_bytes())
        self.assertEqual(self.get_plan.call_count,1)

    def test_changed_update_identity_is_rejected_without_overwrite(self):
        self.send('/plan',update=20)
        before=self.board.path.read_bytes()
        for text,message in (('/plan extra',10),('/plan',11)):
            with self.assertRaisesRegex(bt.BoardTelegramError,'update_identity_changed'):
                self.send(text,update=20,message_id=message)
            self.assertEqual(before,self.board.path.read_bytes())

    def test_accept_only_durably_queues_no_callback(self):
        self.queue()
        self.assertEqual(self.board.status()['pending_approvals'],1)
        self.assertEqual(self.read()['tokens'][0]['quality_percent'],100)
        self.callback.assert_not_called()
        self.assertIn('ещё не подтверждены',self.reply_text())

    def test_optional_quality_is_bound_to_command_fingerprint(self):
        self.queue(85)
        token=self.read()['tokens'][0]
        expected=hashlib.sha256(json.dumps({'plan':token['fingerprint'],'quality_percent':85,
            'comment':'Finish the agreed fixture detail.'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(token['quality_percent'],85)
        self.assertEqual(token['command_fingerprint'],expected)
        result=self.dispatch()
        self.assertEqual(result['state'],'approved')
        self.assertEqual(self.callback.call_args.kwargs['quality_percent'],85)
        self.assertEqual(self.callback.call_args.kwargs['comment'],'Finish the agreed fixture detail.')

    def test_command_names_are_case_insensitive_without_lowercasing_nonce(self):
        self.send('/PLAN')
        code=self.reply_text().split('/approve ',1)[1].split('\n',1)[0]
        self.send('/APPROVE '+code+' 85 Finish the fixture detail.')
        self.assertEqual(self.dispatch()['state'],'approved')
        self.assertEqual(self.callback.call_args.kwargs['quality_percent'],85)

    def test_quality_zero_and_hundred_are_supported(self):
        for quality in (0,100):
            self.plan=dict(plan(),revision=self.plan['revision']+1)
            self.queue(quality)
            self.dispatch()
            self.assertEqual(self.callback.call_args.kwargs['quality_percent'],quality)

    def test_expired_and_backward_clock_codes_do_not_queue(self):
        code=self.issue()
        for now in (10900,9999):
            with self.subTest(now=now):
                self.now=now
                self.send('/approve '+code)
                self.assertEqual(self.board.status()['pending_approvals'],0)
        self.callback.assert_not_called()

    def test_new_plan_request_invalidates_old_unused_code(self):
        first=self.issue();second=self.issue()
        self.assertNotEqual(first,second)
        self.send('/approve '+first)
        self.assertEqual(self.board.status()['pending_approvals'],0)
        self.send('/approve '+second)
        self.assertEqual(self.board.status()['pending_approvals'],1)

    def test_exact_plan_changes_reject_old_nonce(self):
        code=self.issue()
        for change in ({'revision':124},{'game_minutes':45},{'study_session_id':'d'*32},
                       {'quality_criteria':'Different agreed criteria'}):
            with self.subTest(change=change):
                # A fresh nonce for each independent snapshot.
                self.plan=plan();code=self.issue();self.plan.update(change)
                self.send('/approve '+code)
                self.assertEqual(self.board.status()['pending_approvals'],0)
        self.callback.assert_not_called()

    def test_changed_task_text_or_status_rejects_old_nonce(self):
        code=self.issue();self.plan['tasks'][0]['text']='Different'
        self.send('/approve '+code)
        self.assertEqual(self.board.status()['pending_approvals'],0)

    def test_binding_change_invalidates_other_channel_tokens(self):
        code=self.issue()
        self.send('/plan',binding=OTHER)
        self.send('/approve '+code,binding=BINDING)
        self.assertEqual(self.board.status()['pending_approvals'],0)
        self.assertTrue(all(t['state']=='invalidated' for t in self.read()['tokens']))

    def test_foreign_binding_cannot_use_parent_code(self):
        code=self.issue();self.send('/approve '+code,binding=OTHER)
        self.assertEqual(self.board.status()['pending_approvals'],0)
        self.callback.assert_not_called()

    def test_repeated_code_or_update_never_changes_quality_or_extends(self):
        code=self.queue(85)
        self.send('/approve '+code+' 100')
        first=self.dispatch()
        self.assertEqual(first['state'],'approved')
        self.send('/approve '+code+' 100')
        self.assertFalse(self.dispatch()['processed'])
        self.callback.assert_called_once()
        self.assertEqual(self.callback.call_args.kwargs['quality_percent'],85)

    def test_dispatch_has_exact_stable_root_coordinator_arguments(self):
        self.queue();token=self.read()['tokens'][0]
        self.dispatch()
        self.callback.assert_called_once_with(date=DAY.isoformat(),revision=123,study_id=STUDY,
            request_id=token['request_id'],quality_percent=100,comment='')

    def test_quality_below_hundred_requires_explanation_before_queue_or_dispatch(self):
        code=self.issue()
        for quality in (0,20,85,99):
            self.send('/approve '+code+' '+str(quality))
            self.assertEqual(self.board.status()['pending_approvals'],0)
            self.assertIn('пояснение',self.reply_text())
            self.assertEqual(self.read()['tokens'][0]['state'],'issued')
        self.callback.assert_not_called()
        self.send('/approve '+code+' 85 Check the last agreed task.')
        self.assertEqual(self.dispatch()['state'],'approved')
        self.callback.assert_called_once()

    def test_hundred_accepts_optional_comment_or_comment_without_percent(self):
        for suffix in ('100 Good work.','Good work.','100'):
            self.plan=dict(plan(),revision=self.plan['revision']+1)
            code=self.issue();self.send('/approve '+code+' '+suffix)
            self.assertEqual(self.dispatch()['state'],'approved')
            self.assertEqual(self.callback.call_args.kwargs['quality_percent'],100)
            self.assertEqual(self.callback.call_args.kwargs['comment'],'' if suffix=='100' else 'Good work.')

    def test_percentage_typos_do_not_silently_become_hundred_percent_comment(self):
        code=self.issue()
        for value in ('101','01','85%','85.0','9e2','-1','−1','١٠','.5'):
            with self.subTest(value=value):
                self.send('/approve '+code+' '+value+' fixture explanation')
                self.assertEqual(self.board.status()['pending_approvals'],0)
                self.assertEqual(self.read()['tokens'][0]['state'],'issued')
        self.callback.assert_not_called()

    def test_explanation_bounded_single_line_and_included_in_immutable_fingerprint(self):
        code=self.issue()
        for invalid in ('x'*241,'bad\tcomment','two\nlines'):
            self.send('/approve '+code+' 85 '+invalid)
            self.assertEqual(self.board.status()['pending_approvals'],0)
        comment='x'*239+'🙂'
        self.send('/approve '+code+' 85 '+comment)
        token=self.read()['tokens'][0]
        self.assertEqual(token['comment'],comment)
        self.send('/approve '+code+' 85 Different explanation')
        self.assertEqual(self.read()['tokens'][0]['comment'],comment)
        self.dispatch()
        self.assertEqual(self.callback.call_args.kwargs['comment'],comment)

    def test_legacy_hundred_token_reads_without_writing_and_dispatches_empty_comment(self):
        self.queue()
        state=self.read();token=state['tokens'][0]
        token.pop('comment');token['command_fingerprint']=hashlib.sha256((token['fingerprint']+':100').encode()).hexdigest()
        raw=json.dumps(state).encode();self.board.path.write_bytes(raw)
        self.assertEqual(self.board.status()['pending_approvals'],1)
        self.assertEqual(self.board.path.read_bytes(),raw)
        self.assertEqual(self.dispatch()['state'],'approved')
        self.assertEqual(self.callback.call_args.kwargs['comment'],'')

    def test_legacy_low_quality_without_comment_fails_closed_before_callback(self):
        self.queue(85)
        state=self.read();token=state['tokens'][0]
        token.pop('comment');token['command_fingerprint']=hashlib.sha256((token['fingerprint']+':85').encode()).hexdigest()
        self.board.path.write_text(json.dumps(state),encoding='utf-8')
        self.assertEqual(self.dispatch()['result_code'],'comment_required')
        self.callback.assert_not_called()

    def test_comment_fingerprint_tampering_fails_closed(self):
        self.queue(85)
        state=self.read();state['tokens'][0]['comment']='Changed behind approval'
        self.board.path.write_text(json.dumps(state),encoding='utf-8')
        with self.assertRaisesRegex(bt.BoardTelegramError,'ledger_invalid'):self.board.status()

    def test_unlinked_plan_remains_none_not_synthetic_study(self):
        self.plan.pop('study_session_id')
        self.queue();self.dispatch()
        self.assertIsNone(self.callback.call_args.kwargs['study_id'])

    def test_no_authorization_callback_fails_closed(self):
        self.queue()
        result=self.board.process_pending(self.callback)
        self.assertEqual(result['state'],'rejected')
        self.assertEqual(result['result_code'],'binding_inactive')
        self.callback.assert_not_called()

    def test_paused_or_unavailable_binding_never_dispatches(self):
        self.queue();self.authorized.side_effect=RuntimeError('private failure')
        self.assertEqual(self.dispatch()['state'],'rejected')
        self.callback.assert_not_called()

    def test_queued_plan_modified_before_worker_dispatch_rejected(self):
        self.queue();self.plan['revision']=999
        self.assertEqual(self.dispatch()['result_code'],'stale_plan')
        self.callback.assert_not_called()

    def test_queued_plan_expiry_and_new_day_rejected(self):
        self.queue();self.now=10900
        self.assertEqual(self.dispatch()['state'],'expired')
        self.callback.assert_not_called()

    def test_expiry_during_authorization_or_plan_read_never_dispatches(self):
        self.queue()
        def slow_plan():
            self.now=10900
            return deepcopy(self.plan)
        self.get_plan.side_effect=slow_plan
        self.assertEqual(self.dispatch()['state'],'expired')
        self.callback.assert_not_called()

    def test_new_day_after_acceptance_never_dispatches_yesterday(self):
        self.queue();self.day=date(2026,11,27)
        self.assertEqual(self.dispatch()['result_code'],'plan_unavailable')
        self.callback.assert_not_called()

    def test_callback_unknown_exception_stays_uncertain_without_retry(self):
        code=self.queue();self.callback.side_effect=RuntimeError('PRIVATE TOKEN')
        self.assertEqual(self.dispatch()['state'],'uncertain')
        self.assertFalse(self.dispatch()['processed'])
        self.send('/approve '+code);self.send('/plan')
        self.assertFalse(self.dispatch()['processed'])
        self.callback.assert_called_once()
        self.assertNotIn('PRIVATE TOKEN',self.board.path.read_text(encoding='utf-8'))

    def test_callback_result_error_or_unrecognized_is_unknown_no_retry(self):
        for result in ({'state':'error'},{'state':'uncertain'},None,{'secret':'not echoed'}):
            self.plan=dict(plan(),revision=self.plan['revision']+1)
            self.queue();self.callback.return_value=result
            self.assertEqual(self.dispatch()['state'],'uncertain')
            self.assertFalse(self.dispatch()['processed'])

    def test_accepted_result_does_not_claim_timer_started(self):
        self.queue();self.callback.return_value={'state':'accepted'}
        self.assertEqual(self.dispatch()['state'],'accepted')
        self.assertIn('ещё не подтверждены',self.reply_text())

    def test_rejected_result_has_no_automatic_retry(self):
        self.queue();self.callback.return_value={'state':'rejected'}
        self.assertEqual(self.dispatch()['state'],'rejected')
        self.assertFalse(self.dispatch()['processed'])

    def test_callback_runs_outside_ledger_lock_and_is_not_reentrant_dispatched(self):
        self.queue()
        def callback(**kwargs):
            self.assertEqual(self.board.status()['inflight'],1)
            # A concurrent process_pending sees applying and cannot dispatch it.
            self.assertFalse(self.board.process_pending(mock.Mock(),self.authorized)['processed'])
            return {'state':'approved'}
        self.callback.side_effect=callback
        self.assertEqual(self.dispatch()['state'],'approved')

    def test_recovery_only_marks_applying_as_unknown_never_calls_approve(self):
        self.queue()
        with self.board._change() as state:
            state['tokens'][0]['state']='applying'
        self.assertEqual(self.board.recover_inflight(),1)
        self.assertEqual(self.board.recover_inflight(),0)
        self.assertEqual(self.board.status()['uncertain'],1)
        self.assertFalse(self.dispatch()['processed'])
        self.callback.assert_not_called()

    def test_concurrent_dispatch_has_one_callback(self):
        self.queue();entered=threading.Event();release=threading.Event()
        def callback(**kwargs):
            entered.set();self.assertTrue(release.wait(5));return {'state':'approved'}
        self.callback.side_effect=callback
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.dispatch)
            self.assertTrue(entered.wait(5))
            second=pool.submit(self.dispatch)
            self.assertFalse(second.result(timeout=5)['processed'])
            release.set();self.assertEqual(first.result(timeout=5)['state'],'approved')
        self.callback.assert_called_once()

    def test_replies_use_stable_local_outbox_ids_and_no_delivery_claim(self):
        self.send('/plan')
        enqueue=mock.Mock(return_value={'id':'fixture-message','status':'queued'})
        result=self.board.flush_replies(enqueue,BINDING)
        self.assertEqual(result,{'queued_locally':1,'delivery_confirmed':False,'read_confirmed':False})
        row=self.read()['replies'][0]
        enqueue.assert_called_once_with(row['text'],client_id=row['client_id'])
        self.assertEqual(self.board.flush_replies(enqueue,BINDING)['queued_locally'],0)

    def test_reply_enqueue_failure_retries_same_id_only_not_network_send(self):
        self.send('/plan')
        enqueue=mock.Mock(side_effect=[RuntimeError('pause'),{'status':'sent'}])
        self.assertEqual(self.board.flush_replies(enqueue,BINDING)['queued_locally'],0)
        self.assertEqual(self.board.flush_replies(enqueue,BINDING)['queued_locally'],1)
        self.assertEqual(enqueue.call_args_list[0],enqueue.call_args_list[1])

    def test_reply_never_goes_to_other_binding(self):
        self.send('/plan')
        enqueue=mock.Mock()
        self.assertEqual(self.board.flush_replies(enqueue,OTHER)['queued_locally'],0)
        enqueue.assert_not_called()

    def test_reply_enqueue_runs_outside_own_file_lock(self):
        self.send('/plan')
        def enqueue(text,client_id):
            self.assertEqual(self.board.status()['pending_replies'],1)
            return {'status':'uncertain'}
        self.assertEqual(self.board.flush_replies(enqueue,BINDING)['queued_locally'],1)

    def test_corrupt_duplicate_or_unknown_schema_never_overwritten(self):
        self.root.mkdir()
        for raw in (b'{',b'{"version":1,"version":1,"tokens":[],"commands":[],"replies":[]}',
                    b'{"version":99,"tokens":[],"commands":[],"replies":[]}',
                    b'{"version":1,"tokens":[],"commands":[],"replies":[],"extra":1}'):
            self.board.path.write_bytes(raw)
            with self.assertRaises(bt.BoardTelegramError):self.send('/plan')
            self.assertEqual(self.board.path.read_bytes(),raw)

    def test_file_size_limit_fails_closed(self):
        self.root.mkdir();self.board.path.write_bytes(b' '*(bt.MAX_FILE_BYTES+1))
        with self.assertRaisesRegex(bt.BoardTelegramError,'ledger_too_large'):self.board.status()

    def test_capacity_failure_preserves_previous_state(self):
        self.issue();before=self.board.path.read_bytes()
        with mock.patch.object(bt,'MAX_COMMANDS',1):
            with self.assertRaises(bt.BoardTelegramError):self.send('/plan')
        self.assertEqual(self.board.path.read_bytes(),before)

    def test_atomic_failure_preserves_old_plan_and_no_partial_dispatch(self):
        self.issue();before=self.board.path.read_bytes()
        with mock.patch.object(bt.os,'replace',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):self.send('/plan')
        self.assertEqual(self.board.path.read_bytes(),before)
        self.callback.assert_not_called()
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_invalid_nonce_collision_is_not_persisted(self):
        self.issue();before=self.board.path.read_bytes()
        for nonce in ('short','!'*32,'1'.zfill(32)):
            self.board.nonce=lambda:nonce
            with self.assertRaises(bt.BoardTelegramError):self.send('/plan')
            self.assertEqual(self.board.path.read_bytes(),before)

    def test_status_never_discloses_code_tasks_or_binding(self):
        code=self.queue()
        value=json.dumps(self.board.status())
        self.assertNotIn(code,value);self.assertNotIn(BINDING,value);self.assertNotIn('Synthetic task',value)


if __name__=='__main__':unittest.main()
