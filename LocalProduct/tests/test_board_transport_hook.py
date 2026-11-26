"""The prepared transport hook with fake credentials/API and temporary storage."""
from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'LocalProduct/src'))
sys.path.insert(0,str(Path(__file__).parent))
from board_telegram import BoardTelegram,binding_authorized
from family_messages import MessageStore

candidate=Path(__file__).with_name('family_telegram_hook_fixture.py')
if not candidate.is_file():
    candidate=ROOT/'LocalProduct/src/family_telegram.py'
spec=importlib.util.spec_from_file_location('board_hook_fixture_telegram',candidate)
transport=importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

TOKEN='123456:'+'F'*35
DAY=date(2026,11,26)


class LockProbe:
    def __init__(self):self.lock=threading.RLock();self.depth=0
    def __enter__(self):self.lock.acquire();self.depth+=1;return self
    def __exit__(self,*args):self.depth-=1;self.lock.release()


class Protector:
    def protect(self,value):return b'fixture:'+value
    def unprotect(self,value):
        assert value.startswith(b'fixture:')
        return value[8:]


class FakeAPI:
    def __init__(self):self.updates=[];self.calls=[]
    def factory(self,token):assert token==TOKEN;return self
    def call(self,method,payload):
        self.calls.append((method,payload))
        if method=='getMe':return {'id':123456,'is_bot':True,'username':'FixtureParentBot'}
        if method=='getWebhookInfo':return {'url':''}
        if method=='getUpdates':return [u for u in self.updates if u['update_id']>=payload['offset']]
        raise AssertionError('No real sends allowed by this fixture')


def incoming(update,text,user=42,kind='private',sender=None):
    return {'update_id':update,'message':{'message_id':100+update,
        'chat':{'id':user,'type':kind},'from':{'id':user if sender is None else sender,
        'is_bot':False,'first_name':'Fixture Parent'},'text':text}}


class TransportHookTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.api=FakeAPI()
        self.settings=transport.TelegramSettings(self.root/'messages',api_factory=self.api.factory,
            protector=Protector(),clock=lambda:10000)
        self.settings._lock=LockProbe()
        self.settings.setup(TOKEN);pair=self.settings.begin_pairing()
        self.api.updates=[incoming(1,'/start '+pair['nonce'])]
        self.settings.poll_pairing();self.settings.confirm_pairing(pair['nonce']);self.settings.enable()
        self.api.updates=[]
        self.store=MessageStore(self.root/'messages')
        self.plan={'date':DAY.isoformat(),'revision':42,'status':'submitted','game_minutes':60,
                   'tasks':[{'text':'Synthetic fixture task','status':'done'}],'study_session_id':'a'*32}
        self.board=BoardTelegram(self.root/'board',lambda:deepcopy(self.plan),clock=lambda:10000,
                                 today=lambda:DAY,nonce=lambda:'N'*32)
        self.approve=mock.Mock(return_value={'state':'approved'})

    def poll(self,handler=None):
        return self.settings.poll_incoming(self.store,command_handler=self.board if handler is None else handler)

    def current_binding(self):
        return self.store.get_binding()

    def test_foreign_group_bot_and_mismatched_sender_never_reach_handler(self):
        handler=mock.Mock()
        bot=incoming(5,'/plan');bot['message']['from']['is_bot']=True
        self.api.updates=[incoming(2,'/plan',user=99),incoming(3,'/plan',kind='group'),
                          incoming(4,'/plan',sender=99),bot]
        self.assertEqual(self.poll(handler),[])
        handler.accept_message.assert_not_called()
        self.assertEqual(self.store.get_update_offset(),6)
        self.assertFalse(self.store.pending_incoming())

    def test_pinned_command_durably_handled_under_settings_lock_not_child_inbox(self):
        def accept(**kwargs):
            self.assertEqual(self.settings._lock.depth,1)
            return self.board.accept_message(**kwargs)
        handler=mock.Mock();handler.accept_message.side_effect=accept
        self.api.updates=[incoming(10,'/plan')]
        self.assertEqual(self.poll(handler),[])
        self.assertEqual(self.store.get_update_offset(),11)
        self.assertFalse(self.store.pending_incoming())
        self.assertEqual(self.board.status()['commands'],1)
        self.approve.assert_not_called()
        kwargs=handler.accept_message.call_args.kwargs
        expected=hashlib.sha256(json.dumps({'bot_id':123456,'chat_id':42,'user_id':42},
                                            sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(kwargs['binding_id'],expected)

    def test_ordinary_parent_message_retains_existing_child_delivery(self):
        self.api.updates=[incoming(12,'Synthetic intentional family message')]
        handler=mock.Mock()
        rows=self.poll(handler)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['text'],'Synthetic intentional family message')
        self.assertEqual(len(self.store.pending_incoming()),1)
        handler.accept_message.assert_not_called()

    def test_reserved_without_handler_fails_closed_no_offset_or_child_delivery(self):
        self.api.updates=[incoming(13,'/plan')]
        with self.assertRaisesRegex(transport.TelegramError,'board_handler_unavailable'):
            self.settings.poll_incoming(self.store)
        self.assertEqual(self.store.get_update_offset(),0)
        self.assertFalse(self.store.pending_incoming())

    def test_uppercase_command_also_never_reaches_child_inbox(self):
        self.api.updates=[incoming(14,'/PLAN')]
        self.assertEqual(self.poll(),[])
        self.assertEqual(self.board.status()['tokens'],1)
        self.assertFalse(self.store.pending_incoming())

    def test_handler_failure_redacted_and_does_not_advance(self):
        self.api.updates=[incoming(15,'/plan')]
        handler=mock.Mock();handler.accept_message.side_effect=RuntimeError(TOKEN+' PRIVATE')
        with self.assertRaises(transport.TelegramError) as caught:self.poll(handler)
        self.assertEqual(str(caught.exception),'board_command_write_failed')
        self.assertEqual(self.store.get_update_offset(),0)
        self.assertFalse(self.store.pending_incoming())

    def test_invalid_handler_receipt_cannot_acknowledge_update(self):
        self.api.updates=[incoming(16,'/plan')]
        for result in (None,{},False,{'handled':False},{'handled':1}):
            handler=mock.Mock();handler.accept_message.return_value=result
            with self.assertRaisesRegex(transport.TelegramError,'board_command_write_failed'):
                self.poll(handler)
            self.assertEqual(self.store.get_update_offset(),0)

    def test_forwarded_or_inline_bot_command_never_issues_token(self):
        for index,key in enumerate(('forward_origin','forward_from','forward_from_chat','forward_date','via_bot'),20):
            update=incoming(index,'/plan');update['message'][key]={}
            self.api.updates=[update]
            self.poll()
        self.assertEqual(self.board.status()['tokens'],0)
        self.assertFalse(self.store.pending_incoming())

    def test_approve_dispatch_runs_only_later_outside_settings_mutex(self):
        self.api.updates=[incoming(30,'/plan')];self.poll()
        text=json.loads(self.board.path.read_text(encoding='utf-8'))['replies'][-1]['text']
        code=text.split('/approve ',1)[1].split('\n',1)[0]
        self.api.updates=[incoming(31,'/approve '+code+' 85 Check the agreed fixture detail.')];self.poll()
        self.approve.assert_not_called()
        def apply(**kwargs):
            self.assertEqual(self.settings._lock.depth,0)
            self.assertEqual(kwargs['quality_percent'],85)
            self.assertEqual(kwargs['comment'],'Check the agreed fixture detail.')
            return {'state':'approved'}
        self.approve.side_effect=apply
        result=self.board.process_pending(self.approve,
            lambda identity:self.settings.status()['enabled'] and identity==self.store.get_binding())
        self.assertEqual(result['state'],'approved')
        self.approve.assert_called_once()
        self.assertFalse(self.store.pending_incoming())

    def test_paused_channel_prevents_pending_coordinator_dispatch(self):
        self.api.updates=[incoming(40,'/plan')];self.poll()
        text=json.loads(self.board.path.read_text(encoding='utf-8'))['replies'][-1]['text']
        code=text.split('/approve ',1)[1].split('\n',1)[0]
        self.api.updates=[incoming(41,'/approve '+code)];self.poll()
        self.settings.disable()
        result=self.board.process_pending(self.approve,
            lambda identity:self.settings.status()['enabled'] and identity==self.store.get_binding())
        self.assertEqual(result['state'],'rejected')
        self.approve.assert_not_called()

    def test_parent_replies_enter_original_outbox_not_direct_network_send(self):
        self.api.updates=[incoming(50,'/plan')];self.poll()
        def enqueue(text,client_id):
            return self.settings.queue_reply(self.store,text,client_id)
        result=self.board.flush_replies(enqueue,self.current_binding())
        self.assertEqual(result['queued_locally'],1)
        self.assertFalse(result['delivery_confirmed'])
        rows=self.store.list_messages(direction='outgoing')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'queued')
        self.assertTrue(rows[0]['client_id'].startswith('board:'))
        self.assertNotIn('sendMessage',[method for method,_ in self.api.calls])

    def test_duplicate_update_does_not_create_second_card(self):
        self.api.updates=[incoming(60,'/plan')];self.poll()
        self.store.advance_update_offset(0)
        self.poll()
        self.assertEqual(self.board.status()['commands'],1)
        self.assertEqual(self.board.status()['tokens'],1)

    def test_binding_authorized_is_fresh_boolean_no_network_or_new_queue_binding(self):
        # Before the first authenticated poll, the queue is not bound. Gate
        # must fail closed instead of binding it as a side effect.
        identity=hashlib.sha256(json.dumps({'bot_id':123456,'chat_id':42,'user_id':42},
            sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertIsNone(self.store.get_binding())
        before_calls=len(self.api.calls)
        self.assertFalse(binding_authorized(self.settings,self.store,identity))
        self.assertIsNone(self.store.get_binding())
        self.api.updates=[incoming(70,'/plan')];self.poll()
        before_calls=len(self.api.calls)
        self.assertTrue(binding_authorized(self.settings,self.store,identity))
        self.assertEqual(len(self.api.calls),before_calls)
        self.assertFalse(binding_authorized(self.settings,self.store,'b'*64))
        self.settings.disable()
        self.assertFalse(binding_authorized(self.settings,self.store,identity))

    def test_binding_authorized_rejects_corruption_or_mismatched_bot_without_secret_output(self):
        self.api.updates=[incoming(71,'/plan')];self.poll()
        identity=self.current_binding()
        with mock.patch.object(self.settings,'_load',side_effect=RuntimeError(TOKEN+' PRIVATE')):
            self.assertIs(binding_authorized(self.settings,self.store,identity),False)
        original=self.settings._load()
        for changes in ({'bot':{'id':654321}}, {'paired':{'chat_id':99,'user_id':99}}, {'token':None}):
            value=deepcopy(original);value.update(changes)
            with mock.patch.object(self.settings,'_load',return_value=value):
                self.assertIs(binding_authorized(self.settings,self.store,identity),False)


if __name__=='__main__':unittest.main()
