"""Runtime health and safe popup dismissal; synthetic data, no real OS actions."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from board_approval import BoardApproval
from daily_plan import DailyPlan
from family_rewards import RewardBook
from hourly_break import HourlyBreakStore
from play_session import PlaySession
from study_resume import StudyResume, RUN_KEY
import study_runtime
from study_runtime import PersistentStudyPanel, StudyPlanPopup


class Registry:
    HKEY_CURRENT_USER = REG_SZ = KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    def __init__(self): self.values = {}
    def OpenKey(self, hive, key, *args):
        if hive != 1 or key != RUN_KEY: raise AssertionError('Unowned registry key')
        return nullcontext(self)
    CreateKey = OpenKey
    def QueryValueEx(self, key, name):
        if name not in self.values: raise FileNotFoundError(name)
        return self.values[name]
    def SetValueEx(self, key, name, reserved, kind, value): self.values[name] = value, kind
    def DeleteValue(self, key, name): del self.values[name]


class Value:
    def __init__(self): self.value = ''
    def set(self, value): self.value = value
    def get(self): return self.value


class Session:
    def __init__(self):
        self.active = self.preview = False
        self.pending = []
        self.remaining_seconds = 0
    def stop(self): self.active = False


class FixturePanel(PersistentStudyPanel):
    """Run owning control methods without constructing any Tk widget."""
    def __init__(self, resume, approval):
        self.resume_store, self._board_approval = resume, approval
        self.play_store = approval.play
        self._ensure_play_running = approval.ensure_running
        self._play_view = self._play_error_type = None
        self._hourly_view = self._hourly_error_type = None
        self._keeper_last_poll_utc = self._keeper_last_poll_ok = self._keeper_last_error_type = None
        self._lease = self._plan_popup = self._timer = None
        self._runtime_state, self._settings_source = 'inactive', 'primary'
        self._runtime_error_type = None
        self._health_path, self._next_health_write = resume.root/'runtime-status.json', 0
        self._codex_pause_enabled = self._parent_apps_error = False
        self._codex_minimized = self._parent_apps_count = self._parent_apps_minimized = 0
        self._codex_pause_policy = SimpleNamespace(reconcile=lambda **_: {'enabled':False,'minimized':0})
        self.session, self.notice, self.runtime_status, self.status = Session(), Value(), Value(), Value()
        self.stop_button = mock.Mock()
        self._hide_indicator = mock.Mock()
        self._set_controls = mock.Mock()
        self._render_rows = mock.Mock()
        self._schedule = mock.Mock()
        self.closed = False
        self.refreshes = 0

    def _adopt(self, lease, remaining):
        self._lease, self.session.active, self.session.remaining_seconds = lease, True, remaining
        self._runtime_state = 'restored'
        self._update_parent_status()

    def _refresh(self): self.refreshes += 1


class Window:
    def __init__(self):
        self.callbacks, self.cancelled = {}, []
        self.exists, self.destroyed, self.in_approval_callback = True, False, False
    def after_idle(self, callback):
        token = 'idle-' + str(len(self.callbacks)+1)
        self.callbacks[token] = callback
        return token
    def after_cancel(self, token):
        self.cancelled.append(token)
        self.callbacks.pop(token, None)
    def winfo_exists(self): return self.exists
    def destroy(self):
        if self.in_approval_callback: raise AssertionError('Destroyed DailyPlanPanel during approval callback')
        self.exists, self.destroyed = False, True
    def run_idle(self):
        callbacks = list(self.callbacks.items())
        for token, callback in callbacks:
            if self.callbacks.pop(token, None) is not None: callback()


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='runtime-reliability-')
        self.addCleanup(temp.cleanup); self.root = Path(temp.name)
        self.wall, self.mono = 10000.0, 100.0
        self.day = date(2026,11,26)
        self.study = StudyResume(self.root/'study', clock=lambda:self.wall, registry=Registry())
        self.study._startup_command = lambda:'owned synthetic runtime'
        self.play = PlaySession(self.root/'play', clock=lambda:self.wall,
                                monotonic=lambda:self.mono, today=lambda:self.day)
        self.breaks = HourlyBreakStore(self.root/'hourly', clock=lambda:self.wall,
                                      monotonic=lambda:self.mono, today=lambda:self.day)
        self.plans = DailyPlan(self.root/'plan', today=lambda:self.day)
        self.service = BoardApproval(self.plans,self.study,self.play,
            RewardBook(self.root/'rewards',clock=lambda:self.wall),root=self.root/'approval',
            breaks=self.breaks,ensure_running=mock.Mock())
        self.panel = FixturePanel(self.study,self.service)

    def advance(self, seconds): self.wall+=seconds; self.mono+=seconds

    def waiting_break(self):
        self.breaks.configure(True,'PRIVATE task which must never enter health JSON',0)
        original = self.study.activate(0)
        self.play.prepare(self.day.isoformat(),1,60,original['session_id'],current_study_id=original['session_id'])
        self.service.pump(); self.advance(3600); self.service.pump()
        self.panel._poll()
        return self.breaks.status()

    def health(self):
        self.panel._next_health_write = 0
        self.panel._write_health_status(None)
        return json.loads(self.panel._health_path.read_text(encoding='utf-8'))

    def corrupt_hourly(self):
        self.breaks.root.mkdir(exist_ok=True)
        self.breaks.path.write_bytes(b'{ damaged hourly ledger')
        return self.breaks.path.read_bytes()

    def test_corrupt_hourly_keeps_current_study_and_polling(self):
        lease = self.study.activate(60); saved = self.study.path.read_bytes()
        corrupt = self.corrupt_hourly()
        self.panel._poll(); state = self.health()
        self.assertTrue(self.panel.session.active); self.assertEqual(self.panel._lease,lease)
        self.assertEqual(self.panel.refreshes,1); self.panel._schedule.assert_called_once()
        self.assertEqual(self.study.path.read_bytes(),saved)
        self.assertEqual(self.breaks.path.read_bytes(),corrupt)
        self.assertTrue(state['enforcement_active'])
        self.assertEqual(state['keeper']['state'],'running')
        self.assertIsNotNone(state['hourly_cycle_error']); self.assertIsNone(state['hourly_cycle'])

    def test_timer_failure_clears_stale_playing_view_and_explains_it_to_parent(self):
        self.study.activate(60); self.corrupt_hourly()
        self.panel._play_view = {'current':{'phase':'playing','remaining_seconds':300,'expires_utc':10300}}
        self.panel._sync_lease()
        self.assertIsNone(self.panel._play_view)
        self.assertNotIn('Игровое время:',self.panel.runtime_status.get())
        self.assertIn('Часовой цикл недоступен',self.panel.runtime_status.get())
        self.assertIn('Игровой таймер недоступен',self.panel.runtime_status.get())

    def test_waiting_phase_and_time_are_in_health_without_task_text(self):
        cycle = self.waiting_break(); state = self.health()
        self.assertEqual(state['hourly_cycle']['phase'],'waiting')
        self.assertEqual(state['hourly_cycle']['remaining_break_seconds'],600)
        self.assertFalse(state['hourly_cycle']['can_approve'])
        self.assertEqual(set(state['hourly_cycle']), {'enabled','phase','remaining_break_seconds',
            'can_request_done','can_approve'})
        raw = json.dumps(state)
        self.assertNotIn('PRIVATE',raw); self.assertNotIn(cycle['break_id'],raw)
        self.assertNotIn('source_play_id',raw); self.assertNotIn('grant',state['hourly_cycle'])
        self.assertIn('Полезное дело: осталось 10:00',self.panel.runtime_status.get())

    def test_elapsed_break_still_explains_two_required_confirmations(self):
        self.waiting_break(); self.advance(600); self.panel._poll()
        self.assertIn('Нужны отметка ребёнка и подтверждение родителя',self.panel.runtime_status.get())
        self.assertFalse(self.health()['hourly_cycle']['can_approve'])
        self.assertTrue(self.panel.session.active)

    def test_submitted_phase_reports_waiting_for_parent(self):
        self.waiting_break(); self.advance(600); self.service.pump()
        ready = self.breaks.status()
        self.breaks.request_done(ready['break_id'],ready['revision'])
        self.panel._poll(); state = self.health()
        self.assertEqual(state['hourly_cycle']['phase'],'submitted')
        self.assertTrue(state['hourly_cycle']['can_approve'])
        self.assertIn('ожидает подтверждения родителя',self.panel.runtime_status.get())

    def test_parent_status_uses_cached_views_without_pumping_or_reading_stores(self):
        self.waiting_break()
        before = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with mock.patch.object(self.service,'pump',side_effect=AssertionError('Unexpected pump')), \
             mock.patch.object(self.breaks,'status',side_effect=AssertionError('Unexpected store read')), \
             mock.patch.object(self.study,'read',side_effect=AssertionError('Unexpected lease read')):
            self.panel._update_parent_status()
        self.assertEqual({str(p.relative_to(self.root)):p.read_bytes()
            for p in self.root.rglob('*') if p.is_file()},before)

    def test_loaded_runtime_marker_and_not_polled_state_do_not_claim_window_proof(self):
        self.study.activate(60); self.panel._sync_lease()
        state = self.health()
        self.assertEqual(state['runtime_contract_version'],2)
        self.assertEqual(set(state['runtime_features']),{'hourly_status_v1','deferred_plan_dismiss_v1'})
        self.assertEqual(state['keeper']['state'],'not_polled')
        self.assertIsNone(state['keeper']['last_poll_utc'])
        self.assertEqual(state['enforcement_observation'],'controller_state_only')

    def test_core_runtime_failure_reports_failed_keeper_and_stops_only_memory_session(self):
        lease = self.study.activate(60); self.panel._sync_lease(); saved=self.study.path.read_bytes()
        with mock.patch.object(self.study,'read',side_effect=ValueError('PRIVATE fixture path')):
            self.panel._poll()
        state = self.health()
        self.assertEqual(state['keeper']['state'],'error')
        self.assertEqual(state['keeper']['last_error_type'],'ValueError')
        self.assertEqual(state['state'],'recovery_failed'); self.assertFalse(state['enforcement_active'])
        self.assertNotIn('PRIVATE',json.dumps(state))
        self.assertEqual(self.study.path.read_bytes(),saved); self.assertEqual(self.study.read(),lease)
        self.panel._schedule.assert_called_once()

    def test_successful_retry_replaces_failed_keeper_marker(self):
        self.study.activate(60)
        with mock.patch.object(self.study,'read',side_effect=ValueError('fixture')): self.panel._poll()
        self.panel._poll(); state = self.health()
        self.assertEqual(state['keeper']['state'],'running')
        self.assertIsNone(state['keeper']['last_error_type']); self.assertTrue(state['enforcement_active'])

    def test_board_approval_schedules_popup_dismiss_after_callback_returns(self):
        lease=self.study.activate(0); self.panel._sync_lease()
        plan=self.plans.publish(['Synthetic task'],60,expected_revision=None)
        plan=self.plans.set_task(0,'done',expected_revision=plan['revision'])
        plan=self.plans.submit(expected_revision=plan['revision'],study_session_id=lease['session_id'])
        popup=StudyPlanPopup(self.panel,self.plans,None); window=popup.window=Window()
        self.panel._plan_popup=popup; window.in_approval_callback=True
        with mock.patch.object(study_runtime,'BoardApproval',return_value=self.service):
            result=self.panel.approve_board_plan(self.plans,plan,request_id='d'*32)
        self.assertEqual(result['play']['current']['phase'],'playing')
        self.assertFalse(window.destroyed); self.assertTrue(window.callbacks)
        window.in_approval_callback=False; window.run_idle()
        self.assertTrue(window.destroyed); self.assertIsNone(popup.window)
        self.assertIsNone(self.study.read()); self.assertFalse(self.panel.session.active)


class PopupDeferredDismissTests(unittest.TestCase):
    def setUp(self):
        self.current=None
        self.study=SimpleNamespace(active_study_id=lambda:self.current)
        self.popup=StudyPlanPopup(self.study,object(),None)
        self.window=self.popup.window=Window()

    def test_schedule_is_single_and_never_destroys_synchronously(self):
        self.popup.dismiss_when_inactive(); self.popup.dismiss_when_inactive()
        self.assertEqual(len(self.window.callbacks),1); self.assertFalse(self.window.destroyed)
        self.window.run_idle(); self.assertTrue(self.window.destroyed)

    def test_replacement_study_before_idle_preserves_window(self):
        self.popup.dismiss_when_inactive(); self.current='b'*32
        self.window.run_idle(); self.assertIs(self.popup.window,self.window)
        self.assertFalse(self.window.destroyed); self.assertIsNone(self.popup.dismiss_timer)

    def test_replacement_popup_is_not_destroyed_by_an_older_idle_callback(self):
        self.popup.dismiss_when_inactive(); new=Window(); self.popup.window=new
        self.popup.dismiss_timer='new-window-idle'
        self.window.run_idle(); self.assertIs(self.popup.window,new); self.assertFalse(new.destroyed)
        self.assertEqual(self.popup.dismiss_timer,'new-window-idle')

    def test_unreadable_lease_preserves_window(self):
        self.study.active_study_id=mock.Mock(side_effect=ValueError('unavailable'))
        self.popup.dismiss_when_inactive(); self.window.run_idle()
        self.assertIs(self.popup.window,self.window); self.assertFalse(self.window.destroyed)

    def test_close_cancels_idle_and_raise_callbacks_and_releases_owned_lock(self):
        self.popup.dismiss_when_inactive(); token=self.popup.dismiss_timer
        self.popup.raise_timer='raise-1'; self.popup.lock=mock.Mock(); lock=self.popup.lock
        self.popup.close()
        self.assertIn(token,self.window.cancelled); self.assertIn('raise-1',self.window.cancelled)
        self.assertIsNone(self.popup.dismiss_timer); self.assertIsNone(self.popup.window)
        self.window.run_idle(); lock.close.assert_called_once()

    def test_late_z_order_callback_is_safe_after_dismiss(self):
        self.popup._dismiss(); self.popup._restore_z_order()
        self.assertIsNone(self.popup.window)


class RuntimePopupNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name!='nt': raise unittest.SkipTest('Windows required')
        from test_desktop_ui_native import desktop_names
        thread,incoming=desktop_names()
        if not re.fullmatch(r'CodexQA_[0-9a-f]{32}',thread) or thread==incoming:
            raise unittest.SkipTest('Nonce-owned non-input QA desktop required')

    def test_real_daily_plan_callback_finishes_before_its_popup_is_destroyed(self):
        import tkinter as tk
        import winreg
        from daily_plan_ui import DailyPlanPanel
        import daily_plan_ui
        import paths
        from study_core import StudySession
        from test_study_runtime import Settings,Guard,Desktop
        with tempfile.TemporaryDirectory(prefix='runtime-popup-native-') as temp, \
             mock.patch('subprocess.Popen',side_effect=AssertionError('Real process forbidden')), \
             mock.patch.object(PersistentStudyPanel,'_show_indicator'), \
             mock.patch.object(paths,'DEPLOY_ROOT',Path(temp)/'deployment'):
            base=Path(temp); wall=[10000.0]; mono=[100.0]; day=date(2026,11,26)
            study=StudyResume(base/'study',clock=lambda:wall[0],registry=Registry())
            study._startup_command=lambda:'owned synthetic runtime'
            lease=study.activate(0)
            plans=DailyPlan(base/'plan',today=lambda:day)
            plan=plans.publish(['Synthetic household task'],60,expected_revision=None)
            plan=plans.set_task(0,'done',expected_revision=plan['revision'])
            plans.submit(expected_revision=plan['revision'],study_session_id=lease['session_id'])
            breaks=HourlyBreakStore(base/'hourly',clock=lambda:wall[0],monotonic=lambda:mono[0],today=lambda:day)
            original=BoardApproval
            def factory(*args,**kwargs):
                kwargs.setdefault('plans',plans); kwargs.setdefault('root',base/'approval')
                kwargs.setdefault('rewards',RewardBook(base/'rewards',clock=lambda:wall[0]))
                kwargs.setdefault('breaks',breaks)
                return original(*args,**kwargs)
            root=tk.Tk(); root.withdraw(); errors=[]
            root.report_callback_exception=lambda *error:errors.append(error)
            panel=None
            try:
                with mock.patch.object(study_runtime,'BoardApproval',side_effect=factory), \
                     mock.patch.object(daily_plan_ui,'BoardApproval',side_effect=factory), \
                     mock.patch.object(winreg,'OpenKey',side_effect=AssertionError('Real registry forbidden')):
                    session=StudySession(Desktop(),clock=lambda:mono[0])
                    panel=PersistentStudyPanel(root,settings=Settings(),session=session,guard=Guard(),
                        resume=study,ensure_play_running=mock.Mock())
                    panel.play_store.today=lambda:day
                    # Exercise the actual Tk-backed panel through an optional
                    # ledger failure before checking the approval callback.
                    saved_lease=study.path.read_bytes()
                    breaks.root.mkdir(exist_ok=True)
                    breaks.path.write_bytes(b'{ corrupt synthetic hourly ledger')
                    panel.enable_health_status()
                    panel._poll()
                    self.assertTrue(panel.session.active)
                    self.assertEqual(study.path.read_bytes(),saved_lease)
                    health=json.loads(panel._health_path.read_text(encoding='utf-8'))
                    self.assertIsNotNone(health['hourly_cycle_error'])
                    self.assertTrue(health['enforcement_active'])
                    breaks.path.unlink()
                    panel._poll()
                    self.assertIsNone(panel._hourly_error_type)
                    panel.enable_plan_popup(store=plans); panel._plan_popup._open()
                    popup=panel._plan_popup; window=popup.window
                    board=next(widget for widget in window.winfo_children() if isinstance(widget,DailyPlanPanel))
                    board._check_pin=mock.Mock(return_value=True)
                    board.approve_button.invoke()
                    self.assertIs(popup.window,window)
                    self.assertTrue(window.winfo_exists(),'Approval destroyed its own widgets before returning')
                    self.assertEqual(panel.play_store.status()['current']['phase'],'playing')
                    root.update_idletasks()
                    self.assertIsNone(popup.window); self.assertFalse(window.winfo_exists())
                    self.assertFalse(panel.session.active); self.assertIsNone(study.read()); self.assertEqual(errors,[])
            finally:
                if panel is not None: panel.close()
                root.destroy()


if __name__=='__main__': unittest.main(verbosity=2)
