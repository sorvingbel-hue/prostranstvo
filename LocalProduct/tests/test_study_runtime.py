import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Finite lease recovery with fake data/desktop and an unshown Tk root."""
import json
import math
from pathlib import Path
import tempfile
import tkinter as tk
import types
import unittest
from unittest import mock

from daily_plan import DailyPlan
from daily_plan_ui import DailyPlanPanel
from study_core import StudySession, StudySettings
from study_resume import StudyResume
from study_windows import WindowInfo
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


class FixtureDesktop(Desktop):
    """Own only in-memory window records; never call a Windows API."""

    def __init__(self, windows):
        super().__init__()
        self.windows = list(windows)
        self.closed = []

    def list_windows(self):
        self.enumerations += 1
        return list(self.windows)

    def close_window(self, window):
        self.closed.append(window)
        return 'requested'


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

    def file_recovery(self, payload, *, minutes=60, elapsed=123, activate=True):
        directory = tempfile.TemporaryDirectory(prefix='study-recovery-')
        self.addCleanup(directory.cleanup)
        study_root = Path(directory.name) / 'study'
        study_root.mkdir()
        self.settings = StudySettings(study_root)
        self.settings.path.write_bytes(payload)
        self.resume = StudyResume(study_root, clock=lambda: self.clock.wall)
        lease = self.resume.activate(minutes) if activate else None
        self.clock.wall += elapsed
        window = WindowInfo(101, 99001, 1, r'C:\Fixture\game.exe',
                            'FixtureWindow', 'Synthetic game')
        self.desktop = FixtureDesktop([window])
        self.session = StudySession(self.desktop, clock=lambda: self.clock.monotonic)
        panel = self.create()
        return lease, panel, window

    def assert_default_recovery(self, lease, panel, payload):
        self.assertTrue(self.session.active)
        self.assertFalse(self.session.preview)
        self.assertEqual(panel._runtime_state, 'restored')
        self.assertEqual(panel._settings_source, 'defaults')
        self.assertIn('Восстановлен', panel.runtime_status.get())
        self.assertIn('запасн', panel.runtime_status.get().casefold())
        self.assertEqual(self.resume.read(), lease)
        self.assertEqual(self.settings.path.read_bytes(), payload)
        self.assertEqual(self.session.remaining_seconds,
                         math.ceil(lease['expires_utc'] - self.clock.wall))

    def advance_ticks(self, seconds):
        # Advancing one second at a time avoids the deliberate sleep-resume grace reset.
        for _ in range(seconds):
            self.clock.wall += 1
            self.clock.monotonic += 1
            self.tick()

    def test_no_lease_stays_off_without_startup_mutation(self):
        self.create()
        self.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertEqual(self.panel._runtime_state, 'inactive')

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
        self.assertEqual(self.panel._runtime_state, 'restored')

    def test_new_activation_persists_startup_and_finite_deadline(self):
        self.create().start_lessons()
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 3600)
        self.assertEqual(self.resume.value['expires_utc'], 4600)
        self.assertEqual(self.resume.startup_calls, 1)
        self.assertEqual(self.panel._runtime_state, 'active')

    def test_corrupt_json_settings_resume_enforces_until_original_expiry(self):
        payload = b'{"duration_minutes":'
        lease, panel, window = self.file_recovery(payload)
        self.assert_default_recovery(lease, panel, payload)
        panel.enable_health_status()
        self.tick()
        state = json.loads(panel._health_path.read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'restored')
        self.assertEqual(state['settings_source'], 'defaults')
        self.assertTrue(state['settings_fallback'])
        self.assertTrue(state['enforcement_active'])
        self.assertEqual(state['session_id'], lease['session_id'])
        self.assertEqual(state['expires_utc'], lease['expires_utc'])
        self.assertEqual(self.desktop.closed, [])
        self.advance_ticks(10)
        self.assertEqual(self.desktop.closed, [window])
        self.assertIn('запасн', panel.runtime_status.get().casefold())
        self.assertEqual(self.resume.read(), lease)
        self.assertEqual(self.settings.path.read_bytes(), payload)

        enumerations = self.desktop.enumerations
        self.clock.monotonic += self.session.remaining_seconds + 1
        self.clock.wall = lease['expires_utc'] + 1
        self.tick()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.read())
        self.assertEqual(panel._runtime_state, 'completed')
        self.assertEqual(self.desktop.enumerations, enumerations)
        self.assertEqual(self.desktop.closed, [window])
        self.assertEqual(self.settings.path.read_bytes(), payload)

    def test_invalid_settings_schema_uses_defaults_without_rewriting(self):
        payload = b'{"duration_minutes":60,"delay_seconds":0,"allowed_paths":[]}'
        lease, panel, _ = self.file_recovery(payload)
        self.assert_default_recovery(lease, panel, payload)
        self.tick()
        self.assertTrue(self.session.active)
        self.assertIn('запасн', panel.runtime_status.get().casefold())

    def test_invalid_utf8_settings_uses_defaults_without_rewriting(self):
        payload = b'\xff\xfe\xfa'
        lease, panel, _ = self.file_recovery(payload)
        self.assert_default_recovery(lease, panel, payload)
        self.tick()
        self.assertTrue(self.session.active)

    def test_unreadable_settings_uses_defaults_without_touching_active_lease(self):
        payload = b'{"duration_minutes":30,"delay_seconds":5,"allowed_paths":[]}'
        with mock.patch.object(StudySettings, 'load', side_effect=PermissionError('fixture denied')):
            lease, panel, _ = self.file_recovery(payload)
            self.assert_default_recovery(lease, panel, payload)
            self.tick()
        self.assertTrue(self.session.active)
        self.assertEqual(self.resume.read(), lease)

    def test_expired_lease_with_bad_settings_does_not_activate(self):
        payload = b'{'
        _, panel, _ = self.file_recovery(payload, minutes=1, elapsed=61)
        self.tick()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.read())
        self.assertEqual(panel._runtime_state, 'completed')
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertEqual(self.desktop.closed, [])
        self.assertEqual(self.settings.path.read_bytes(), payload)

    def test_bad_settings_without_lease_remains_inactive(self):
        payload = b'{'
        _, panel, _ = self.file_recovery(payload, activate=False)
        self.tick()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.resume.read())
        self.assertEqual(panel._runtime_state, 'inactive')
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertEqual(self.settings.path.read_bytes(), payload)

    def test_corrupt_lease_reports_failure_without_rewriting_or_enforcement(self):
        directory = tempfile.TemporaryDirectory(prefix='study-invalid-lease-')
        self.addCleanup(directory.cleanup)
        study_root = Path(directory.name)
        self.settings = StudySettings(study_root)
        self.resume = StudyResume(study_root, clock=lambda: self.clock.wall)
        payload = b'{"version":1'
        self.resume.path.write_bytes(payload)
        panel = self.create()
        panel.enable_health_status()
        self.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(panel._runtime_state, 'recovery_failed')
        self.assertIn('не работают', panel.runtime_status.get().casefold())
        self.assertEqual(self.resume.path.read_bytes(), payload)
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertIsNotNone(panel._timer)
        state = json.loads(panel._health_path.read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'recovery_failed')
        self.assertFalse(state['enforcement_active'])
        self.assertIsNotNone(state['error_type'])

    def test_parent_extension_keeps_last_valid_allowlist_if_settings_corrupt(self):
        directory = tempfile.TemporaryDirectory(prefix='study-last-settings-')
        self.addCleanup(directory.cleanup)
        study_root = Path(directory.name)
        self.settings = StudySettings(study_root)
        school = WindowInfo(102, 99002, 2, r'C:\Fixture\school.exe',
                            'FixtureWindow', 'Synthetic schoolwork')
        game = WindowInfo(103, 99003, 3, r'C:\Fixture\game.exe',
                          'FixtureWindow', 'Synthetic game')
        self.settings.save(duration_minutes=30, delay_seconds=5,
                           allowed_paths=[school.executable])
        self.resume = StudyResume(study_root, clock=lambda: self.clock.wall)
        self.resume.activate(30)
        self.desktop = FixtureDesktop([school, game])
        self.session = StudySession(self.desktop, clock=lambda: self.clock.monotonic)
        panel = self.create()
        payload = b'{"broken"'
        self.settings.path.write_bytes(payload)
        replacement = self.resume.activate(60)
        self.tick()
        self.assertTrue(self.session.active)
        self.assertEqual(panel._settings_source, 'last_valid')
        self.assertEqual(panel._runtime_state, 'restored')
        self.assertIn('последн', panel.runtime_status.get().casefold())
        self.assertEqual(self.resume.read(), replacement)
        self.assertEqual(self.session.remaining_seconds, 3600)
        self.advance_ticks(5)
        self.assertEqual(self.desktop.closed, [game])
        self.assertIn('последн', panel.runtime_status.get().casefold())
        self.assertEqual(self.resume.read(), replacement)
        self.assertEqual(self.settings.path.read_bytes(), payload)

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

    def submit_daily_plan(self, *, start_study=True, duration_minutes=60):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.resume = StudyResume(root / 'study', clock=lambda: self.clock.wall)
        lease = self.resume.activate(duration_minutes) if start_study else None
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

    def test_indefinite_lessons_stay_on_until_valid_plan_approval(self):
        lease, panel = self.submit_daily_plan(duration_minutes=0)
        self.clock.wall += 86400
        self.clock.monotonic += 86400
        self.tick()
        self.assertTrue(self.session.active)
        self.assertIn('Без таймера', self.panel.status.get())
        self.assertEqual(self.panel.active_study_id(), lease['session_id'])
        self.dialog.return_value = '000000'
        panel.approve_button.invoke()
        self.assertEqual(self.resume.read(), lease)
        self.dialog.return_value = '123456'
        panel.approve_button.invoke()
        self.assertEqual(panel.store.read()['status'], 'approved')
        self.assertIsNone(self.resume.read())
        self.assertFalse(self.session.active)

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
        self.assertEqual(str(self.panel.stop_button.cget('state')), 'normal')
        self.assertEqual(self.desktop.enumerations, 0)
        self.assertIsNotNone(self.resume.value)
        self.assertEqual(self.panel._runtime_state, 'recovery_failed')
        self.assertIn('не работают', self.panel.runtime_status.get().casefold())

    def test_missing_pin_cannot_discard_a_valid_saved_lease_via_close(self):
        lease = self.resume.activate(60)
        self.guard.exists = False
        panel = self.create()
        self.assertFalse(self.session.active)
        self.assertEqual(panel._lease, lease)
        self.assertFalse(panel.request_stop())
        self.assertEqual(self.resume.read(), lease)
        self.assertEqual(self.resume.cleared, [])
        self.assertEqual(panel._runtime_state, 'recovery_failed')
        self.assertIn('не работают', panel.runtime_status.get().casefold())

    def test_preview_does_not_enable_startup_or_create_lease(self):
        self.create().start_preview()
        self.assertTrue(self.session.preview)
        self.assertIsNone(self.resume.value)
        self.assertEqual(self.resume.startup_calls, 0)
        self.assertEqual(self.panel._runtime_state, 'preview')
        self.assertTrue(self.panel.request_stop())

    def test_expired_preview_reports_completed_and_never_enforcement_active(self):
        directory = tempfile.TemporaryDirectory(prefix='study-preview-expiry-')
        self.addCleanup(directory.cleanup)
        self.resume.root = Path(directory.name)
        panel = self.create()
        panel.start_preview()
        self.assertEqual(panel._runtime_state, 'preview')
        self.assertTrue(self.session.active)
        panel.enable_health_status()
        self.clock.monotonic += self.session.remaining_seconds + 1
        self.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(panel._runtime_state, 'completed')
        self.assertIn('Завершён', panel.runtime_status.get())
        self.assertNotIn('ограничения работают', panel.runtime_status.get().casefold())
        self.assertIsNone(self.resume.read())
        state = json.loads(panel._health_path.read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'completed')
        self.assertFalse(state['enforcement_active'])


    def test_saved_plan_popover_opens_once_and_dismissal_keeps_study_running(self):
        panel = self.create()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = DailyPlan(Path(directory.name) / 'plan')
        store.publish(['Подготовить тетради'], 45, expected_revision=None)
        panel.enable_plan_popup(store=store)
        popup = panel._plan_popup

        panel.start_preview()
        self.tick()
        self.assertIsNone(popup.window)
        panel.request_stop()

        with mock.patch.object(tk.Toplevel, 'deiconify'):
            self.assertTrue(panel.start_lessons(duration_minutes=30))
            self.tick()
        window = popup.window
        self.assertIsNotNone(window)
        self.assertEqual(window.title(), study_runtime.PLAN_WINDOW_TITLE)
        checklist = next(child for child in window.winfo_children() if isinstance(child, DailyPlanPanel))
        self.assertEqual(checklist.plan['tasks'][0]['text'], 'Подготовить тетради')
        self.tick()
        self.assertIs(popup.window, window)
        popup._dismiss()
        self.tick()
        self.assertIsNone(popup.window)
        self.assertTrue(panel.session.active)
        self.assertIsNotNone(panel.active_study_id())

    def test_startup_session_without_current_plan_does_not_open_popup(self):
        self.resume.activate(30)
        panel = self.create()
        store = mock.Mock()
        store.read.return_value = None
        panel.enable_plan_popup(store=store)
        self.tick()
        self.tick()
        store.read.assert_called_once()
        self.assertIsNone(panel._plan_popup.window)
        self.assertTrue(panel.session.active)

    def test_startup_session_uses_only_one_popup_across_controllers(self):
        self.resume.activate(30)
        panel = self.create()
        store = mock.Mock()
        store.read.return_value = {'date': '2026-11-15'}
        panel.enable_plan_popup(store=store)
        first = panel._plan_popup
        second = study_runtime.StudyPlanPopup(panel, store, None)
        self.addCleanup(second.close)

        with mock.patch.object(first, '_open') as show_first, mock.patch.object(second, '_open') as show_second:
            self.tick()
            second.show_for(panel.active_study_id())
        show_first.assert_called_once()
        show_second.assert_not_called()


    def test_popup_failure_preserves_polling_and_saved_session(self):
        self.resume.activate(30)
        panel = self.create()
        panel._plan_popup = mock.Mock()
        panel._plan_popup.show_for.side_effect = [AttributeError('fixture popup failure'), None]
        self.tick()
        self.assertIsNotNone(panel._timer)
        self.assertFalse(self.session.active)
        self.assertIsNotNone(self.resume.value)
        self.tick()
        self.assertTrue(self.session.active)
        self.assertIsNotNone(panel._timer)

    def test_runtime_health_confirms_actual_active_session(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.resume.root = Path(directory.name)
        lease = self.resume.activate(30)
        panel = self.create()
        panel.enable_health_status()
        self.tick()
        import json
        import os
        state = json.loads(panel._health_path.read_text(encoding='utf-8'))
        self.assertEqual(state['pid'], os.getpid())
        self.assertEqual(state['session_id'], lease['session_id'])
        self.assertTrue(state['active'])
        self.assertFalse(state['preview'])
        self.assertIsNone(state['error_type'])
        self.assertEqual(state['observed_windows'], 0)
        self.assertEqual(state['state'], 'restored')
        self.assertEqual(state['settings_source'], 'primary')
        self.assertFalse(state['settings_fallback'])
        self.assertTrue(state['enforcement_active'])

    def test_unwritable_health_file_keeps_enforcement_polling(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.resume.root = Path(directory.name)
        self.resume.activate(30)
        panel = self.create()
        panel.enable_health_status()
        with mock.patch.object(study_runtime.os, 'replace', side_effect=OSError('fixture full disk')):
            self.tick()
        self.assertTrue(self.session.active)
        self.assertIsNotNone(panel._timer)
        self.assertFalse(panel._health_path.exists())


    def test_codex_minimizer_runs_only_during_active_lessons(self):
        panel = self.create()
        policy = mock.Mock()
        policy.enabled.return_value = True
        policy.reconcile.return_value = {'enabled': True, 'minimized': 1, 'errors': 0}
        panel.bind_codex_pause(policy)
        self.tick()
        policy.reconcile.assert_called_with(active=False)
        self.resume.activate(30)
        self.tick()
        policy.reconcile.assert_called_with(active=True)
        self.assertEqual(panel._codex_minimized, 2)

    def test_codex_minimizer_error_does_not_stop_lessons(self):
        self.resume.activate(30)
        panel = self.create()
        policy = mock.Mock()
        policy.enabled.return_value = True
        policy.reconcile.side_effect = OSError('fixture unavailable')
        panel.bind_codex_pause(policy)
        self.tick()
        self.assertTrue(panel.session.active)
        self.assertIsNotNone(panel._timer)


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

    def test_resume_only_hides_a_working_primary_settings_session(self):
        cases = (
            (False, 'recovery_failed', 'primary', False),
            (True, 'restored', 'defaults', False),
            (True, 'restored', 'last_valid', False),
            (True, 'restored', 'primary', True),
        )
        for active, state, source, should_hide in cases:
            with self.subTest(active=active, state=state, settings_source=source):
                self.resume.activate(60)
                lock = mock.MagicMock()
                lock.__enter__.return_value.existed = False
                root = mock.MagicMock()
                panel = mock.MagicMock()
                panel.session.active = active
                panel.session.preview = False
                panel._runtime_state = state
                panel._settings_source = source
                shell = mock.MagicMock()
                with mock.patch.object(study_runtime, 'RuntimeLock', return_value=lock), \
                     mock.patch.object(study_runtime.tk, 'Tk', return_value=root), \
                     mock.patch.object(study_runtime.ttk, 'Style'), \
                     mock.patch.object(study_runtime, 'PersistentStudyPanel', return_value=panel), \
                     mock.patch.dict('sys.modules', {'study_shell': types.SimpleNamespace(StudyShell=mock.Mock(return_value=shell))}):
                    self.assertEqual(study_runtime.main(['--resume']), 0)
                shell.install.assert_called_once()
                if should_hide:
                    shell.hide.assert_called_once()
                else:
                    shell.hide.assert_not_called()
                root.mainloop.assert_called_once()

    def test_resume_corrupt_lease_preflight_opens_visible_failure_panel(self):
        lock = mock.MagicMock()
        lock.__enter__.return_value.existed = False
        root = mock.MagicMock()
        panel = mock.MagicMock()
        panel.session.active = False
        panel.session.preview = False
        panel._runtime_state = 'recovery_failed'
        panel._settings_source = 'primary'
        shell = mock.MagicMock()
        with mock.patch.object(self.resume, 'read', side_effect=ValueError('fixture corrupt lease')), \
             mock.patch.object(study_runtime, 'RuntimeLock', return_value=lock), \
             mock.patch.object(study_runtime.tk, 'Tk', return_value=root) as create_root, \
             mock.patch.object(study_runtime.ttk, 'Style'), \
             mock.patch.object(study_runtime, 'PersistentStudyPanel', return_value=panel) as create_panel, \
             mock.patch.dict('sys.modules', {'study_shell': types.SimpleNamespace(StudyShell=mock.Mock(return_value=shell))}):
            self.assertEqual(study_runtime.main(['--resume']), 0)
        create_root.assert_called_once()
        create_panel.assert_called_once_with(root, resume=self.resume)
        shell.install.assert_called_once()
        shell.hide.assert_not_called()
        root.mainloop.assert_called_once()
        panel.close.assert_called_once()

    def test_shell_failure_keeps_visible_gui_and_pin_close(self):
        self.resume.activate(60)
        lock = mock.MagicMock()
        lock.__enter__.return_value.existed = False
        root = mock.MagicMock()
        panel = mock.MagicMock()
        panel.session.active = True
        panel.session.preview = False
        panel._runtime_state = 'restored'
        panel._settings_source = 'primary'
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
