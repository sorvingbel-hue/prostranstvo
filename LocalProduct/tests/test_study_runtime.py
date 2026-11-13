import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Finite lease recovery with fake data/desktop and an unshown Tk root."""
import math
from pathlib import Path
import tempfile
import tkinter as tk
import types
import unittest
from unittest import mock

from daily_plan import DailyPlan
from daily_plan_ui import DailyPlanPanel
from study_core import StudySession
from study_resume import StudyResume
import study_runtime
from study_runtime import PersistentStudyPanel


class Clock:
    def __init__(self):
        self.wall = 1000.0
        self.monotonic = 100.0


class Resume:
    def __init__(self, clock):
        self.clock = clock
        self.value = None
        self.startup_calls = 0
        self.activations = 0
        self.cleared = []
        self.root = Path('synthetic-study-root')

    def read(self):
        return dict(self.value) if self.value is not None else None

    def activate(self, minutes):
        self.activations += 1
        self.value = {'version': 1, 'session_id': f'{self.activations:032x}',
                      'expires_utc': self.clock.wall + minutes * 60,
                      'duration_seconds': minutes * 60}
        return dict(self.value)

    def remaining_seconds(self, lease):
        return min(lease['duration_seconds'], max(0, math.ceil(lease['expires_utc'] - self.clock.wall)))

    def clear(self, session_id):
        self.cleared.append(session_id)
        if self.value is not None and self.value['session_id'] == session_id:
            self.value = None
            return True
        return False

    def enable_startup(self):
        self.startup_calls += 1


class Settings:
    def __init__(self):
        self.value = {'duration_minutes': 60, 'delay_seconds': 10, 'allowed_paths': []}

    def load(self):
        return {**self.value, 'allowed_paths': list(self.value['allowed_paths'])}

    def save(self, **value):
        self.value = value
        return self.load()


class Guard:
    def __init__(self, configured=True):
        self.exists = configured

    def configured(self):
        return self.exists

    def verify(self, pin):
        return pin == '123456'


class Desktop:
    def __init__(self):
        self.enumerations = 0

    def list_windows(self):
        self.enumerations += 1
        return []

    def close_window(self, window):
        raise AssertionError('No windows are provided in this runtime fixture')


class PersistentPanelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.resume = Resume(self.clock)
        self.settings = Settings()
        self.guard = Guard()
        self.desktop = Desktop()
        self.session = StudySession(self.desktop, clock=lambda: self.clock.monotonic)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.indicator = mock.patch.object(PersistentStudyPanel, '_show_indicator').start()
        self.dialog = mock.patch('study_ui.simpledialog.askstring', return_value='123456').start()
        self.addCleanup(mock.patch.stopall)
        self.panel = None

    def create(self):
        self.panel = PersistentStudyPanel(self.root, settings=self.settings, session=self.session,
                                          guard=self.guard, resume=self.resume)
        self.addCleanup(self.panel.close)
        return self.panel

    def tick(self):
        if self.panel._timer is not None:
            self.panel.after_cancel(self.panel._timer)
        self.panel._poll()

    def test_no_lease_stays_off_without_startup_mutation(self):
        self.create()
        self.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertEqual(self.desktop.enumerations, 0)

    def test_recovery_preserves_original_absolute_expiry(self):
        lease = self.resume.activate(60)
        self.clock.wall += 123
        self.create()
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 3477)
        self.assertEqual(self.resume.value, lease)
        self.assertEqual(self.resume.activations, 1)
        self.assertEqual(self.resume.startup_calls, 0)
        self.dialog.assert_not_called()

    def test_new_activation_persists_startup_and_finite_deadline(self):
        self.create().start_lessons()
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 3600)
        self.assertEqual(self.resume.value['expires_utc'], 4600)
        self.assertEqual(self.resume.startup_calls, 1)

    def test_bad_pin_cannot_start_or_change_saved_duration(self):
        self.dialog.return_value = '000000'
        self.assertFalse(self.create().start_lessons(duration_minutes=120))
        self.assertFalse(self.session.active)
        self.assertEqual(self.settings.value['duration_minutes'], 60)
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertIsNone(self.resume.value)

    def test_parent_can_start_two_hours_and_stop_with_the_same_pin(self):
        self.assertTrue(self.create().start_lessons(duration_minutes=120))
        self.assertEqual(self.settings.value['duration_minutes'], 120)
        self.assertEqual(self.resume.value['expires_utc'], 8200)
        self.assertEqual(self.session.remaining_seconds, 7200)
        self.dialog.return_value = '000000'
        self.assertFalse(self.panel.request_stop())
        self.assertTrue(self.session.active)
        self.dialog.return_value = '123456'
        self.assertTrue(self.panel.request_stop())
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.value)

    def test_start_does_not_replace_lease_created_during_pin_dialog(self):
        panel = self.create()

        def prompt(*args, **kwargs):
            self.resume.activate(45)
            return '123456'

        self.dialog.side_effect = prompt
        self.assertFalse(panel.start_lessons(duration_minutes=120))
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 45 * 60)
        self.assertEqual(self.resume.activations, 1)
        self.assertEqual(self.settings.value['duration_minutes'], 60)

    def test_parent_extension_adopted_when_panel_idle(self):
        self.create()
        self.resume.activate(60)
        self.tick()
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 3600)

    def test_parent_extension_replaces_older_deadline(self):
        self.resume.activate(30)
        self.create()
        self.clock.wall += 60
        self.clock.monotonic += 60
        replacement = self.resume.activate(60)
        self.tick()
        self.assertEqual(self.session.remaining_seconds, 3600)
        self.assertEqual(self.panel._lease, replacement)

    def test_expired_lease_stops_before_desktop_enumeration(self):
        self.resume.activate(1)
        self.create()
        enumerations = self.desktop.enumerations
        self.clock.wall += 61
        self.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(self.desktop.enumerations, enumerations)
        self.assertIsNone(self.resume.value)

    def test_wrong_parent_pin_does_not_clear_lease(self):
        self.resume.activate(60)
        self.create()
        self.dialog.return_value = '000000'
        self.assertFalse(self.panel.request_stop())
        self.assertTrue(self.session.active)
        self.assertIsNotNone(self.resume.value)

    def test_verified_parent_stop_clears_matching_lease(self):
        lease = self.resume.activate(60)
        self.create()
        self.assertTrue(self.panel.request_stop())
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.value)
        self.assertEqual(self.resume.cleared, [lease['session_id']])
        self.tick()
        self.assertFalse(self.session.active)

    def test_stale_parent_controller_preserves_newer_lease(self):
        self.resume.activate(30)
        self.create()
        replacement = self.resume.activate(60)
        self.panel.force_stop()
        self.assertEqual(self.resume.value, replacement)
        self.tick()
        self.assertEqual(self.session.remaining_seconds, 3600)

    def submit_daily_plan(self, *, start_study=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.resume = StudyResume(root / 'study', clock=lambda: self.clock.wall)
        lease = self.resume.activate(60) if start_study else None
        self.create()
        store = DailyPlan(root / 'plan')
        plan = store.publish(['Подготовить тетради'], 45, expected_revision=None)
        store.set_task(0, 'done', expected_revision=plan['revision'])
        panel = DailyPlanPanel(self.root, store=store, study=self.panel)
        panel.submit_button.invoke()
        self.assertEqual(store.read().get('study_session_id'), lease['session_id'] if lease else None)
        return lease, panel

    def test_approval_after_reopening_ends_only_the_submitted_session(self):
        lease, panel = self.submit_daily_plan()
        store = DailyPlan(panel.store.root)
        panel.destroy()
        panel = DailyPlanPanel(self.root, store=store, study=self.panel)
        self.dialog.return_value = '000000'
        panel.approve_button.invoke()
        self.assertEqual(store.read()['status'], 'submitted')
        self.assertEqual(self.resume.read(), lease)

        self.dialog.return_value = '123456'
        self.dialog.reset_mock()
        panel.approve_button.invoke()
        self.dialog.assert_called_once()
        self.assertEqual(store.read()['status'], 'approved')
        self.assertIsNone(self.resume.read())
        self.assertFalse(self.session.active)
        self.assertIn('завершён', panel.notice.cget('text'))
        self.assertEqual(panel.play_button.winfo_manager(), 'pack')

    def test_replacement_created_during_pin_survives_approval(self):
        lease, panel = self.submit_daily_plan()

        def replace_session(*args, **kwargs):
            self.resume.activate(120)
            return '123456'

        self.dialog.side_effect = replace_session
        panel.approve_button.invoke()
        saved = panel.store.read()
        self.assertEqual(saved['status'], 'approved')
        self.assertEqual(saved['study_session_id'], lease['session_id'])
        self.assertNotEqual(self.resume.read()['session_id'], lease['session_id'])
        self.assertEqual(self.session.remaining_seconds, 7200)
        self.assertIn('продолжается', panel.notice.cget('text'))
        self.assertEqual(panel.play_button.winfo_manager(), '')

    def test_approval_without_a_bound_session_does_not_stop_later_lessons(self):
        _, panel = self.submit_daily_plan(start_study=False)
        later = self.resume.activate(30)
        panel.approve_button.invoke()
        self.assertEqual(panel.store.read()['status'], 'approved')
        self.assertEqual(self.resume.read(), later)
        self.assertEqual(panel.play_button.winfo_manager(), '')

    def test_busy_session_file_preserves_approval_and_reports_manual_stop(self):
        lease, panel = self.submit_daily_plan()
        with mock.patch.object(self.resume, 'clear', side_effect=RuntimeError('Модуль занят')):
            panel.approve_button.invoke()
        self.assertEqual(panel.store.read()['status'], 'approved')
        self.assertEqual(self.resume.read(), lease)
        self.assertIn('Завершить уроки', panel.notice.cget('text'))
        self.assertEqual(panel.play_button.winfo_manager(), '')
        panel.study_stop.invoke()
        self.assertIsNone(self.resume.read())
        self.assertEqual(panel.play_button.winfo_manager(), 'pack')

    def test_shutdown_cleanup_keeps_lease_for_next_login(self):
        lease = self.resume.activate(60)
        self.create().close()
        self.assertFalse(self.session.active)
        self.assertEqual(self.resume.value, lease)
        self.assertEqual(self.resume.cleared, [])

    def test_backward_wall_clock_does_not_extend_current_run(self):
        self.resume.activate(1)
        self.create()
        self.clock.monotonic += 10
        self.clock.wall -= 100
        self.tick()
        self.assertEqual(self.session.remaining_seconds, 50)
        self.clock.monotonic += 51
        self.tick()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.value)
        self.tick()
        self.assertFalse(self.session.active)

    def test_missing_pin_cannot_resume_enforcement(self):
        self.resume.activate(60)
        self.guard.exists = False
        self.create()
        self.assertFalse(self.session.active)
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertIsNotNone(self.resume.value)

    def test_preview_does_not_enable_startup_or_create_lease(self):
        self.create().start_preview()
        self.assertTrue(self.session.preview)
        self.assertIsNone(self.resume.value)
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertTrue(self.panel.request_stop())


class MainTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.resume = Resume(self.clock)
        self.store_patch = mock.patch.object(study_runtime, 'StudyResume', return_value=self.resume).start()
        self.addCleanup(mock.patch.stopall)

    def test_resume_without_future_lease_never_creates_gui(self):
        with mock.patch.object(study_runtime.tk, 'Tk') as create:
            self.assertEqual(study_runtime.main(['--resume']), 0)
        create.assert_not_called()

    def test_invalid_minutes_do_not_write_startup(self):
        for minutes in ('0', '241', '-1'):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                study_runtime.main(['--start-minutes', minutes])
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertIsNone(self.resume.value)

    def test_duplicate_start_persists_extension_before_singleton_exit(self):
        lock = mock.MagicMock()
        lock.__enter__.return_value.existed = True
        with mock.patch.object(study_runtime, 'StudyGuard', return_value=Guard()), \
             mock.patch.object(study_runtime, 'RuntimeLock', return_value=lock), \
             mock.patch.object(study_runtime.tk, 'Tk') as create:
            self.assertEqual(study_runtime.main(['--start-minutes', '60']), 0)
        self.assertEqual(self.resume.value['expires_utc'], 4600)
        create.assert_not_called()

    def test_shell_failure_keeps_visible_gui_and_pin_close(self):
        self.resume.activate(60)
        lock = mock.MagicMock()
        lock.__enter__.return_value.existed = False
        root = mock.MagicMock()
        panel = mock.MagicMock()
        shell = mock.MagicMock()
        shell.install.side_effect = OSError('synthetic tray failure')
        with mock.patch.object(study_runtime, 'RuntimeLock', return_value=lock), \
             mock.patch.object(study_runtime.tk, 'Tk', return_value=root), \
             mock.patch.object(study_runtime.ttk, 'Style'), \
             mock.patch.object(study_runtime, 'PersistentStudyPanel', return_value=panel), \
             mock.patch.dict('sys.modules', {'study_shell': types.SimpleNamespace(StudyShell=mock.Mock(return_value=shell))}):
            self.assertEqual(study_runtime.main(['--resume']), 0)
        root.mainloop.assert_called_once()
        panel.close.assert_called_once()
        shell.hide.assert_not_called()
        close_callback = root.protocol.call_args.args[1]
        panel.request_stop.return_value = False
        close_callback()
        root.destroy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
