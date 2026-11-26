"""Cross-store cancellation/recovery regressions; no Tk, network or registry."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from board_approval import BoardApproval
from play_session import PlaySession
from study_resume import StudyResume
from study_runtime import PersistentStudyPanel
import family_agent
import board_remote


class RegistryForbidden:
    def __getattr__(self,name):
        raise AssertionError('No registry access in this fixture')


class Value:
    def __init__(self):self.value=''
    def set(self,value):self.value=value


class FixturePanel(PersistentStudyPanel):
    """Exercise owning _sync_lease without initializing any Tk widget."""
    def __init__(self,resume,approval):
        self.resume_store=resume
        self._board_approval=approval
        self.play_store=approval.play
        self._play_view=None
        self._play_error_type=None
        self._lease=None
        self._runtime_state='inactive'
        self._runtime_error_type=None
        self._settings_source='primary'
        self._health_path=resume.root/'fixture-health.json'
        self._next_health_write=0
        self._codex_pause_enabled=False
        self._codex_minimized=0
        self._parent_apps_count=0
        self._parent_apps_minimized=0
        self._parent_apps_error=False
        self.notice=Value()
        self.session=SimpleNamespace(active=False,preview=False,pending=[],remaining_seconds=0)
        self.parent_status_updates=0

    def _adopt(self,lease,remaining):
        self._lease=lease
        self.session.active=True
        self.session.remaining_seconds=remaining
        self._runtime_state='restored'

    def _update_parent_status(self):
        self.parent_status_updates+=1


class BoardRecoveryTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).parent/'tests-work';base.mkdir(exist_ok=True)
        fixture=tempfile.TemporaryDirectory(prefix='board-recovery-',dir=base)
        self.addCleanup(fixture.cleanup)
        self.root=Path(fixture.name)
        self.wall=100000.0
        self.mono=500.0
        self.study=StudyResume(self.root/'study',clock=lambda:self.wall,registry=RegistryForbidden())
        self.study.enable_startup=mock.Mock()
        self.play=PlaySession(self.root/'play',clock=lambda:self.wall,monotonic=lambda:self.mono,
                              today=lambda:date(2026,11,26))
        self.approval=BoardApproval(study=self.study,play=self.play,
            root=self.root/'approval',ensure_running=mock.Mock())

    def expired_game(self):
        proposal=self.play.prepare('2026-11-26',1,1,None,current_study_id=None)
        self.play.confirm_release(proposal['current']['play_id'],current_study_id=None)
        self.wall+=60;self.mono+=60
        return proposal['current']['play_id']

    def test_expiry_and_parent_cancel_serialize_before_any_restore_after_cancel(self):
        self.expired_game()
        entered=threading.Event();release=threading.Event();cancel_started=threading.Event();cancel_done=threading.Event()
        order=[]
        def startup():
            entered.set()
            if not release.wait(3):raise AssertionError('Fixture release barrier timed out')
        self.study.enable_startup=startup
        original=self.study.activate_if_empty
        def activate(*args,**kwargs):
            value=original(*args,**kwargs);order.append('restore_committed');return value
        self.study.activate_if_empty=activate
        def cancel():
            cancel_started.set()
            try:return self.approval.cancel()
            finally:order.append('cancel_returned');cancel_done.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pumping=pool.submit(self.approval.pump)
            self.assertTrue(entered.wait(2))
            cancelling=pool.submit(cancel)
            try:
                self.assertTrue(cancel_started.wait(2))
                self.assertFalse(cancel_done.wait(0.15),'Cancel escaped while older pump still held a stale restore request')
            finally:release.set()
            pumping.result(timeout=5)
            cancelling.result(timeout=5)
        self.assertEqual(order,['restore_committed','cancel_returned'])
        self.assertIsNone(self.study.read(),'Parent cancellation must remove this allowance\'s just-returned lease')
        self.approval.pump()
        self.assertIsNone(self.study.read(),'A cancelled/finished allowance cannot restore Study later')

    def test_parent_cancel_before_expiry_pump_prevents_study_return(self):
        self.expired_game()
        self.approval.cancel()
        self.assertEqual(self.approval.pump()['action'],'none')
        self.assertIsNone(self.study.read())
        self.study.enable_startup.assert_not_called()

    def test_parent_cancel_removes_only_own_already_returned_lease(self):
        self.expired_game()
        completed=self.approval.pump()
        self.assertEqual(completed['current']['phase'],'completed')
        self.assertEqual(self.study.read()['session_id'],completed['current']['return_study_id'])
        self.approval.cancel()
        self.assertIsNone(self.study.read())
        self.assertFalse(self.play.status()['requires_startup'])

    def test_parent_cancel_preserves_newer_foreign_lease_after_return(self):
        self.expired_game();self.approval.pump()
        newer=self.study.activate(120)
        before=self.study.path.read_bytes()
        self.approval.cancel()
        self.assertEqual(self.study.read(),newer)
        self.assertEqual(self.study.path.read_bytes(),before)

    def test_cancel_of_pending_game_does_not_clear_its_original_study_lease(self):
        lease=self.study.activate(60)
        self.play.prepare('2026-11-26',1,60,lease['session_id'],current_study_id=lease['session_id'])
        before=self.study.path.read_bytes()
        self.approval.cancel()
        self.assertEqual(self.study.path.read_bytes(),before)

    def corrupt_play(self):
        self.play.root.mkdir()
        self.play.path.write_bytes(b'{broken ledger')
        return self.play.path.read_bytes()

    def test_valid_study_lease_is_adopted_despite_corrupt_optional_play_state(self):
        lease=self.study.activate(60)
        raw=self.corrupt_play()
        panel=FixturePanel(self.study,self.approval)
        panel._sync_lease()
        self.assertTrue(panel.session.active)
        self.assertEqual(panel._lease,lease)
        self.assertEqual(panel._runtime_state,'restored')
        self.assertIsNotNone(panel._play_error_type)
        self.assertEqual(self.play.path.read_bytes(),raw)
        self.assertEqual(self.study.read(),lease)

    def test_existing_enforcement_survives_play_ledger_failure_and_health_reports_both(self):
        lease=self.study.activate(60);raw=self.corrupt_play()
        panel=FixturePanel(self.study,self.approval)
        panel._adopt(lease,3600)
        panel._sync_lease()
        self.assertTrue(panel.session.active)
        panel._write_health_status(None)
        health=json.loads(panel._health_path.read_text(encoding='utf-8'))
        self.assertTrue(health['enforcement_active'])
        self.assertEqual(health['session_id'],lease['session_id'])
        self.assertIsNotNone(health['game_timer_error'])
        self.assertEqual(self.play.path.read_bytes(),raw)

    def test_corrupt_play_parent_stop_fails_controlled_without_stopping_valid_study(self):
        lease=self.study.activate(60);raw=self.corrupt_play()
        panel=FixturePanel(self.study,self.approval)
        panel._adopt(lease,3600)
        panel.authorize_parent=mock.Mock(return_value=False)
        self.assertFalse(panel.request_stop())
        self.assertTrue(panel.session.active)
        self.assertEqual(self.study.read(),lease)
        self.assertEqual(self.play.path.read_bytes(),raw)
        self.assertTrue(panel.notice.value)


class OptionalBoardWorkerRecoveryTests(unittest.TestCase):
    def worker(self,factory):
        stop=threading.Event();states=[];polls=[];outgoing=[];presented=[]
        store=SimpleNamespace(recover_sending=mock.Mock())
        class Settings:
            def status(self):return {'enabled':True}
            def process_outbox(self,value):outgoing.append(value)
            def poll_incoming(self,value,command_handler=None):polls.append(command_handler);return []
        class Presenter:
            def tick(self):presented.append('tick');stop.set()
            def close(self):presented.append('close')
        fake=SimpleNamespace(BoardRemote=factory)
        with mock.patch.dict(sys.modules,board_remote=fake), \
             mock.patch.object(family_agent,'WorkerLock',return_value=nullcontext()), \
             mock.patch.object(family_agent,'write_state',side_effect=lambda _store,**value:states.append(value)):
            family_agent.run(store=store,settings=Settings(),stop=stop,presenter=Presenter())
        return states,polls,outgoing,presented

    def assert_base_channel_alive(self,result):
        states,polls,outgoing,presented=result
        self.assertEqual(len(outgoing),1)
        self.assertEqual(polls,[None],'Unrecovered board commands must not be dispatched')
        self.assertEqual(presented,['tick','close'])
        self.assertTrue(any('board_unavailable' in value['errors'] for value in states))
        self.assertFalse(states[-1]['running'])

    def test_optional_board_constructor_error_does_not_stop_ordinary_family_channel(self):
        self.assert_base_channel_alive(self.worker(mock.Mock(side_effect=ValueError('fixture board unavailable'))))

    def test_corrupt_board_ledger_recovery_does_not_stop_ordinary_family_channel(self):
        board=SimpleNamespace(recover=mock.Mock(side_effect=ValueError('fixture corrupt ledger')),
                              tick=mock.Mock(),accept_message=mock.Mock())
        self.assert_base_channel_alive(self.worker(mock.Mock(return_value=board)))
        board.tick.assert_not_called()
        board.accept_message.assert_not_called()


class BoardReplyBindingRecoveryTests(unittest.TestCase):
    def case(self,rebind):
        old='a'*64;new='b'*64;queued=[]
        with tempfile.TemporaryDirectory(prefix='board-reply-binding-') as directory:
            store=SimpleNamespace(root=Path(directory)/'family',binding=old)
            store.get_binding=lambda:store.binding
            settings=SimpleNamespace(_lock=threading.RLock())
            def queue_reply(_store,text,client_id):
                # Models the real transport's validation of the CURRENT valid
                # channel. Both A and B are legitimate configured channels;
                # the caller must pin the reply to its original A identity.
                queued.append((store.binding,text,client_id))
                return {'status':'queued'}
            settings.queue_reply=queue_reply
            class Board:
                def process_pending(self,*args,**kwargs):return {'processed':False}
                def flush_replies(self,enqueue_reply,identity):
                    self.identity=identity
                    if rebind:store.binding=new
                    try:enqueue_reply('Owned fixture plan response',client_id='board:fixture')
                    except Exception:pass  # Real flush retains pending old-channel reply.
                    return {'delivery_confirmed':False}
            board=Board()
            remote=board_remote.BoardRemote(store,settings,board=board,approval=SimpleNamespace())
            with mock.patch.object(board_remote,'binding_authorized',
                    side_effect=lambda _settings,_store,expected:expected==store.binding):
                remote.tick()
            self.assertEqual(board.identity,old)
        return queued,old

    def test_pending_old_binding_reply_cannot_be_queued_to_newly_bound_channel(self):
        queued,_=self.case(rebind=True)
        self.assertEqual(queued,[],'An A-scoped plan/code must not be sent to new channel B')

    def test_unchanged_binding_can_queue_its_own_pending_reply(self):
        queued,old=self.case(rebind=False)
        self.assertEqual(len(queued),1)
        self.assertEqual(queued[0][0],old)


if __name__=='__main__':unittest.main(verbosity=2)
