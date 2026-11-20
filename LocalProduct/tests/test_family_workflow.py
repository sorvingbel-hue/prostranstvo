"""Synthetic family workflow through real stores and withdrawn Tk callbacks.

This is controller integration, not live Windows/family acceptance. All profile
data and PINs are temporary. Startup registration, indicators, scheduled polling,
native desktop ownership, external launches and network access are excluded.
Version-2 recurrence/history checks include real persisted PIN attempts, late
approval credits and session-scoped completion across reopened controllers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from datetime import date, timedelta
import secrets
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

from daily_plan import DailyPlan
from daily_plan_ui import DailyPlanPanel
from study_core import StudySession, StudySettings
from study_guard import StudyGuard
from study_resume import StudyResume
from study_runtime import PersistentStudyPanel, StudyPlanPopup


class _Clock:
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.monotonic = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


class _EmptyDesktop:
    """An in-memory adapter; never inspect or operate on Windows windows."""

    def list_windows(self):
        return []

    def close_window(self, window):
        raise AssertionError('The family fixture provides no desktop windows')


class _WithdrawnToplevel(tk.Toplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.withdraw()

    def deiconify(self):
        # Exercise popup construction/dismissal without mapping it on the desktop.
        self.withdraw()

    wm_deiconify = deiconify

    def lift(self, *args):
        pass

    tkraise = lift


class FamilyWorkflowTests(unittest.TestCase):
    def _forbid(self, target, **kwargs):
        patcher = mock.patch(
            target, side_effect=AssertionError(f'Forbidden fixture side effect: {target}'),
            **kwargs,
        )
        blocked = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(blocked.assert_not_called)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='synthetic-family-workflow-')
        self.addCleanup(directory.cleanup)
        self.profile = Path(directory.name)
        self.clock = _Clock()
        self.day = date(2026, 11, 19)
        self.pin = f'{secrets.randbelow(100_000_000):08d}'
        self.wrong_pin = str((int(self.pin[0]) + 1) % 10) + self.pin[1:]
        self.callback_errors = []
        self.toplevels = []

        for target in (
            'subprocess.Popen', 'os.system', 'socket.socket',
            'socket.create_connection', 'tkinter.messagebox._show',
            'tkinter.simpledialog.askstring', 'study_resume.StudyResume._registry',
            'study_windows.WindowsDesktop', 'codex_pause.WindowsDesktop',
            'study_runtime.RuntimeLock',
        ):
            self._forbid(target)
        self._forbid('os.startfile', create=True)

        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Withdrawn Tk controller integration unavailable: {error}')
        # No update/idle processing may occur before the root is withdrawn.
        self.root.withdraw()
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(self._assert_no_callback_errors)
        self.addCleanup(self.root.destroy)

        def hidden_toplevel(*args, **kwargs):
            window = _WithdrawnToplevel(*args, **kwargs)
            self.toplevels.append(window)
            return window

        patches = (
            mock.patch('study_runtime.tk.Toplevel', side_effect=hidden_toplevel),
            mock.patch.object(PersistentStudyPanel, '_show_indicator'),
            mock.patch.object(PersistentStudyPanel, '_schedule'),
            mock.patch.object(StudyResume, 'enable_startup'),
        )
        self.adapters = []
        for patcher in patches:
            self.adapters.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.startup = self.adapters[-1]

        self.study_root = self.profile / 'study'
        self.plan_root = self.profile / 'plan'
        self.guard = StudyGuard(self.study_root, clock=lambda: self.clock.wall)
        self.guard.set_pin(self.pin)
        self.resume = self._resume()
        self.store = self._store()
        self.store.publish(
            ['Synthetic: solve two sums', 'Synthetic: pack the school bag'],
            45, expected_revision=None,
        )
        self.study = self._controller()
        self.plan = self._plan_panel()

    def tearDown(self):
        if not hasattr(self, 'root'):
            return
        self.root.update_idletasks()
        self.assertEqual(self.root.state(), 'withdrawn')
        for window in self.toplevels:
            if window.winfo_exists():
                self.assertEqual(window.state(), 'withdrawn')
        self._assert_no_callback_errors()

    def _assert_no_callback_errors(self):
        self.assertEqual(self.callback_errors, [], 'Tk callback raised an exception')

    def _resume(self):
        return StudyResume(self.study_root, clock=lambda: self.clock.wall)

    def _store(self):
        return DailyPlan(self.plan_root, today=lambda: self.day)

    def _controller(self):
        controller = PersistentStudyPanel(
            self.root, settings=StudySettings(self.study_root),
            session=StudySession(_EmptyDesktop(), clock=lambda: self.clock.monotonic),
            guard=StudyGuard(self.study_root, clock=lambda: self.clock.wall),
            resume=self._resume(),
        )
        self.addCleanup(controller.close)
        return controller

    def _plan_panel(self):
        return DailyPlanPanel(self.root, store=self._store(), guard=self.guard, study=self.study)

    def _start(self):
        with mock.patch('study_ui.simpledialog.askstring', return_value=self.pin) as prompt:
            self.plan.study_start.invoke()
        prompt.assert_called_once()
        self.assertTrue(self.study.session.active, self.study.notice.get())
        lease = self.resume.read()
        self.assertIsNotNone(lease)
        self.assertEqual(self.study.active_study_id(), lease['session_id'])
        return lease

    def _check_task(self, index):
        # Invoke the rendered child's checkbox; every commit rebuilds the rows.
        rows = [row for row in self.plan.rows.winfo_children() if isinstance(row, ttk.Frame)]
        check = next(widget for widget in rows[index].winfo_children()
                     if isinstance(widget, ttk.Checkbutton))
        self.assertTrue(check.instate(['!disabled']))
        check.invoke()

    def _submit(self):
        for index in range(len(self.store.read()['tasks'])):
            self._check_task(index)
        self.assertTrue(self.plan.submit_button.instate(['!disabled']))
        self.plan.submit_button.invoke()
        self.assertEqual(self._store().read()['status'], 'submitted')

    def _approve(self, pin=None):
        with mock.patch('study_ui.simpledialog.askstring', return_value=pin or self.pin) as prompt:
            self.plan.approve_button.invoke()
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.kwargs['show'], '•')

    def test_child_submission_and_parent_review_end_the_bound_study(self):
        lease = self._start()
        self._submit()
        submitted = self._store().read()
        self.assertEqual(submitted['study_session_id'], lease['session_id'])
        self.assertEqual(self._resume().read(), lease)
        self.assertTrue(self.study.session.active)

        self._approve()

        approved = self._store().read()
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(approved['study_session_id'], lease['session_id'])
        self.assertEqual(approved['game_minutes'], 45)
        self.assertTrue(all(task['status'] == 'done' for task in approved['tasks']))
        self.assertIsNone(self._resume().read())
        self.assertFalse(self.study.session.active)
        self.assertEqual(self.plan.play_button.winfo_manager(), 'pack')
        self.startup.assert_called_once()

    def test_unreviewed_work_and_bad_pin_cannot_end_study_or_grant_game_access(self):
        lease = self._start()
        self.assertTrue(self.plan.submit_button.instate(['disabled']))
        self.plan.submit_button.invoke()
        self.plan.approve_button.invoke()
        self.assertEqual(self.store.read()['status'], 'open')

        self._check_task(0)
        self.assertTrue(self.plan.submit_button.instate(['disabled']))
        self._check_task(1)
        self.plan.approve_button.invoke()
        self.assertEqual(self.store.read()['status'], 'open')
        self.assertEqual(self.plan.play_button.winfo_manager(), '')
        self.assertEqual(self._resume().read(), lease)

        self.plan.submit_button.invoke()
        self._approve(self.wrong_pin)
        self.assertEqual(self._store().read()['status'], 'submitted')
        self.assertEqual(self._resume().read(), lease)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.plan.play_button.winfo_manager(), '')
        self.assertIn('PIN', self.plan.notice.cget('text'))

        with mock.patch('study_ui.simpledialog.askstring', return_value=self.wrong_pin):
            self.plan.study_stop.invoke()
        self.assertTrue(self.study.session.active)
        self.assertEqual(self._resume().read(), lease)

    def test_old_plan_approval_during_pin_prompt_preserves_a_newer_study(self):
        old = self._start()
        self._submit()
        replacement = []

        def replace_during_parent_review(*args, **kwargs):
            replacement.append(self._resume().activate(120))
            return self.pin

        with mock.patch('study_ui.simpledialog.askstring', side_effect=replace_during_parent_review):
            self.plan.approve_button.invoke()

        self.assertEqual(self._store().read()['status'], 'approved')
        self.assertEqual(self._store().read()['study_session_id'], old['session_id'])
        self.assertNotEqual(replacement[0]['session_id'], old['session_id'])
        self.assertEqual(self._resume().read(), replacement[0])
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.study.active_study_id(), replacement[0]['session_id'])
        self.assertEqual(self.study.session.remaining_seconds, 7200)
        self.assertEqual(self.plan.play_button.winfo_manager(), '')

    def test_plan_submitted_before_study_cannot_stop_a_later_lesson(self):
        self._submit()
        self.assertNotIn('study_session_id', self._store().read())
        later = self._start()

        self._approve()

        self.assertEqual(self._store().read()['status'], 'approved')
        self.assertEqual(self._resume().read(), later)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.plan.play_button.winfo_manager(), '')

    def test_closing_saved_plan_popup_preserves_work_and_study_deadline(self):
        lease = self._start()
        self._check_task(0)
        saved = self._store().read()
        # Direct construction excludes the native cross-process ownership mutex.
        popup = StudyPlanPopup(self.study, self._store(), None)
        self.addCleanup(popup.close)
        popup._open()
        window = popup.window
        self.root.update_idletasks()
        self.assertEqual(window.state(), 'withdrawn')
        checklist = next(child for child in window.winfo_children()
                         if isinstance(child, DailyPlanPanel))
        self.assertEqual(checklist.plan, saved)

        # Dispatch the actual callback registered for the window close button.
        window.tk.call(window.protocol('WM_DELETE_WINDOW'))

        self.assertIsNone(popup.window)
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self._store().read(), saved)
        self.assertEqual(self._resume().read(), lease)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.study.session.remaining_seconds, 3600)

    def test_reopened_controllers_keep_deadline_and_can_review_saved_submission(self):
        lease = self._start()
        self._submit()
        submitted = self._store().read()
        self.plan.destroy()
        self.study.close()
        self.study.destroy()
        self.clock.advance(125)

        self.study = self._controller()
        self.plan = self._plan_panel()
        self.assertEqual(self._resume().read(), lease)
        self.assertEqual(self.plan.plan, submitted)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.study.session.remaining_seconds, 3475)
        self.startup.assert_called_once()

        self._approve()

        self.assertEqual(self._store().read()['status'], 'approved')
        self.assertIsNone(self._resume().read())
        self.assertFalse(self.study.session.active)

    def test_repeated_day_late_review_credits_once_and_preserves_today_study(self):
        initial = self.store.read()
        self.store.publish(
            [task['text'] for task in initial['tasks']], initial['game_minutes'],
            expected_revision=initial['revision'], repeat_daily=True,
        )
        self.plan.refresh()
        old_lease = self._start()
        self._submit()
        yesterday = self._store().read()

        self.day += timedelta(days=1)
        self.clock.advance(24 * 60 * 60)
        self.plan.refresh()
        today = self._store().read()
        self.assertEqual(today['date'], self.day.isoformat())
        self.assertEqual(today['status'], 'open')
        self.assertTrue(all(task['status'] == 'pending' for task in today['tasks']))
        self.assertNotIn('study_session_id', today)
        self.assertEqual(self._store().history(), [yesterday])
        self.assertEqual(self.plan.steve_progress['total_points'], 0)
        self.assertEqual(self.plan.steve_progress['provisional_points'], 0)
        self.assertEqual(self._store().snapshot(yesterday['date'])[1]['provisional_points'], 30)

        # Expire the previous real persisted lease before requesting a new one.
        self.study._sync_lease()
        self.assertFalse(self.study.session.active)
        self.assertIsNone(self._resume().read())
        new_lease = self._start()
        self.assertNotEqual(new_lease['session_id'], old_lease['session_id'])
        with mock.patch('study_ui.simpledialog.askstring', return_value=self.pin) as prompt:
            self.plan.history_button.invoke()
        prompt.assert_called_once()
        self.assertEqual(self.plan.plan, yesterday)
        self.assertEqual(self.plan._selected_date, yesterday['date'])

        self._approve()

        approved_yesterday = self._store().read(yesterday['date'])
        self.assertEqual(approved_yesterday['status'], 'approved')
        self.assertEqual(approved_yesterday['study_session_id'], old_lease['session_id'])
        self.assertEqual(self._store().read(), today)
        self.assertEqual(self._store().progress()['total_points'], 30)
        self.assertEqual(self._resume().read(), new_lease)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.study.active_study_id(), new_lease['session_id'])
        self.assertEqual(self.plan.play_button.winfo_manager(), '')
        self.assertTrue(self.plan.approve_button.instate(['disabled']))
        self.plan.approve_button.invoke()
        self.assertEqual(self._store().progress()['total_points'], 30)
        self.assertEqual(self._resume().read(), new_lease)

        self.plan.today_button.invoke()
        self.assertEqual(self.plan.plan, today)
        self._submit()
        self.assertEqual(self._store().read()['study_session_id'], new_lease['session_id'])
        self._approve()
        self.assertEqual(self._store().progress()['total_points'], 60)
        self.assertEqual(self._store().read()['status'], 'approved')
        self.assertEqual(self._store().read(yesterday['date']), approved_yesterday)
        self.assertIsNone(self._resume().read())
        self.assertFalse(self.study.session.active)
        self.assertEqual(self.plan.play_button.winfo_manager(), 'pack')
        self.assertEqual(self.startup.call_count, 2)

    def test_reopened_guard_after_rollover_requires_pin_for_review_and_stop(self):
        initial = self.store.read()
        self.store.publish(
            [task['text'] for task in initial['tasks']], initial['game_minutes'],
            expected_revision=initial['revision'], repeat_daily=True,
        )
        self.plan.refresh()
        self._start()
        self._submit()
        yesterday = self._store().read()
        old_guard = self.study.guard
        self._approve(self.wrong_pin)
        self.assertEqual(self._store().read(), yesterday)
        attempts_before = old_guard.attempts_path.read_bytes()
        self.plan.destroy()
        self.study.close()
        self.study.destroy()

        self.day += timedelta(days=1)
        self.clock.advance(24 * 60 * 60)
        new_lease = self._resume().activate(120)
        self.study = self._controller()
        self.plan = self._plan_panel()
        self.assertIsNot(self.study.guard, old_guard)
        self.assertTrue(self.study.guard.configured())
        self.assertEqual(self.study.guard.attempts_path.read_bytes(), attempts_before)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.study.active_study_id(), new_lease['session_id'])
        today = self._store().read()
        before = self.store.path.read_bytes()

        with mock.patch('study_ui.simpledialog.askstring', return_value=self.wrong_pin) as prompt:
            self.plan.history_button.invoke()
        prompt.assert_called_once()
        self.assertIsNone(self.plan._selected_date)
        self.assertEqual(self.study.guard._read_attempts()[0], 2)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self._store().progress()['total_points'], 0)
        self.assertEqual(self._resume().read(), new_lease)

        with mock.patch('study_ui.simpledialog.askstring', return_value=self.pin):
            self.plan.history_button.invoke()
        self.assertEqual(self.plan.plan, yesterday)
        self._approve(self.wrong_pin)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self._store().read(yesterday['date'])['status'], 'submitted')
        self.assertEqual(self._store().progress()['total_points'], 0)
        with mock.patch('study_ui.simpledialog.askstring', return_value=self.wrong_pin):
            self.plan.study_stop.invoke()
        self.assertEqual(self._resume().read(), new_lease)
        self.assertTrue(self.study.session.active)
        self.assertEqual(self.store.path.read_bytes(), before)

        self._approve()
        self.assertEqual(self._store().read(yesterday['date'])['status'], 'approved')
        self.assertEqual(self._store().read(), today)
        self.assertEqual(self._store().progress()['total_points'], 30)
        self.assertEqual(self._resume().read(), new_lease)
        self.assertTrue(self.study.session.active)
        with mock.patch('study_ui.simpledialog.askstring', return_value=self.pin) as prompt:
            self.plan.study_stop.invoke()
        prompt.assert_called_once()
        self.assertIsNone(self._resume().read())
        self.assertFalse(self.study.session.active)
        self.assertEqual(self._store().progress()['total_points'], 30)


if __name__ == '__main__':
    unittest.main()
