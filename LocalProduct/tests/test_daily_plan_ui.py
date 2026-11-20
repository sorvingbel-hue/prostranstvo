"""Exercise the daily plan UI with synthetic tasks."""

import sys
from pathlib import Path as BootstrapPath

sys.path.insert(0, str(BootstrapPath(__file__).resolve().parents[1] / 'src'))

import tkinter as tk
import gc
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from daily_plan import DailyPlan, MAX_TASKS
from daily_plan_ui import DailyPlanPanel, PlanEditor


class DailyPlanPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.root.withdraw()
        self.addCleanup(self._cleanup_tk)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = BootstrapPath(directory.name)
        self.day = date(2026, 11, 1)
        self.store = DailyPlan(self.directory / 'daily-plan', today=lambda: self.day)
        self.guard = Mock()
        self.guard.configured.return_value = True
        self.guard.verify.return_value = False
        self.panel = DailyPlanPanel(self.root, store=self.store, guard=self.guard)

    def _cleanup_tk(self):
        # Finalize Tk variables and reference cycles here, on the creating thread.
        root = self.root
        if root is not None:
            root.destroy()
        self.panel = self.guard = self.store = None
        gc.collect()
        self.root = None
        root = None
        gc.collect()

    def approve_store(self, plan):
        for index in range(len(plan['tasks'])):
            plan = self.store.set_task(index, 'done', expected_revision=plan['revision'])
        plan = self.store.submit(expected_revision=plan['revision'])
        return self.store.review(approved=True, feedback='', expected_revision=plan['revision'])

    def study_panel(self):
        study = Mock()
        study.status = tk.StringVar(self.root, 'Уроки')
        study.notice = tk.StringVar(self.root, '')
        study.session.active = True
        study.authorize_parent.return_value = True
        study.active_study_id.return_value = 'new-study-session'
        panel = DailyPlanPanel(self.root, store=self.store, guard=self.guard, study=study)
        return panel, study

    def publish(self):
        self.store.publish(['Решить две задачи', 'Собрать рюкзак'], 45, expected_revision=None)
        self.panel.refresh()

    def submit(self):
        for index in range(2):
            self.panel._task(index, 'done', self.panel.plan['revision'])
        self.panel.submit_button.invoke()

    def test_help_keeps_submit_disabled_until_all_tasks_are_done(self):
        self.publish()
        self.panel._task(0, 'help', self.panel.plan['revision'])
        self.assertEqual(self.store.read()['tasks'][0]['status'], 'help')
        self.assertTrue(self.panel.submit_button.instate(['disabled']))
        self.submit()
        self.assertEqual(self.store.read()['status'], 'submitted')
        self.assertTrue(self.panel.approve_button.instate(['!disabled']))

    def test_parent_approval_requires_valid_pin(self):
        self.publish()
        self.submit()
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
            self.panel.approve_button.invoke()
            self.assertEqual(self.store.read()['status'], 'submitted')
            self.guard.verify.return_value = True
            self.panel.approve_button.invoke()
        self.assertEqual(self.store.read()['status'], 'approved')
        self.assertIn('подтвердил', self.panel.status.cget('text'))

    def test_approved_plan_offers_living_society_without_automatic_open(self):
        release = self.store.root / 'living-society-v0.1.13'
        self.panel.living_society_target = release
        self.publish()
        with patch('daily_plan_ui.os.startfile') as open_folder:
            self.assertEqual(self.panel.play_button.winfo_manager(), '')
            self.panel.play_button.invoke()
            self.submit()
            self.assertEqual(self.panel.play_button.winfo_manager(), '')
            self.panel.play_button.invoke()

            self.guard.verify.return_value = True
            with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
                self.panel.approve_button.invoke()
            self.assertEqual(self.panel.play_button.winfo_manager(), 'pack')
            self.assertIn('Хорошей игры!', self.panel.status.cget('text'))
            open_folder.assert_not_called()

            self.panel.play_button.invoke()
            self.assertIn('Не удалось открыть', self.panel.notice.cget('text'))
            release.mkdir()
            (release / 'README.md').write_text('Living Society', encoding='utf-8')
            (release / 'living-society-gametest-0.1.13.jar').touch()
            self.panel.play_button.invoke()
            self.assertIn('В папке нет JAR-файла выпуска', self.panel.notice.cget('text'))
            open_folder.assert_not_called()

            (release / 'living-society-0.1.13.jar').touch()
            self.panel.play_button.invoke()
            open_folder.assert_called_once_with(str(release))
            self.assertEqual(self.panel.notice.cget('text'), '')

            self.day += timedelta(days=1)
            self.panel.play_button.invoke()
            open_folder.assert_called_once()
            self.assertEqual(self.panel.play_button.winfo_manager(), '')

    def test_parent_can_return_one_task_with_explanation(self):
        self.publish()
        self.submit()
        self.guard.verify.return_value = True
        with patch('daily_plan_ui.simpledialog.askstring', side_effect=['123456', 'Проверь знак во второй задаче.']):
            self.panel._return_task(0, self.panel.plan['revision'])
        saved = self.store.read()
        self.assertEqual(saved['status'], 'open')
        self.assertEqual([task['status'] for task in saved['tasks']], ['pending', 'done'])
        self.assertIn('Проверь знак', self.panel.feedback.cget('text'))
        self.assertTrue(self.panel.submit_button.instate(['disabled']))

    def test_cancelled_pin_does_not_open_plan_editor(self):
        with patch('daily_plan_ui.simpledialog.askstring', return_value=None), patch('daily_plan_ui.PlanEditor') as editor:
            self.panel.edit_button.invoke()
        editor.assert_not_called()
        self.assertIsNone(self.store.read())

    def test_parent_can_save_and_display_optional_estimates(self):
        self.guard.verify.return_value = True
        result = (['Решить две задачи', 'Собрать рюкзак'], 45, [20, None], [10, 20])
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'), patch(
            'daily_plan_ui.PlanEditor', return_value=SimpleNamespace(result=result)
        ):
            self.panel.edit_button.invoke()
        saved = self.store.read()['tasks']
        self.assertEqual(saved[0]['estimated_minutes'], 20)
        self.assertNotIn('estimated_minutes', saved[1])
        self.assertEqual([task['points'] for task in saved], [10, 20])
        first_row = self.panel.rows.winfo_children()[0]
        self.assertTrue(any(
            widget.winfo_class() == 'TLabel' and 'около 20 мин.' in widget.cget('text')
            for widget in first_row.winfo_children()
        ))

    def test_editor_restores_estimates_and_keeps_them_aligned_when_edited(self):
        saved = self.store.publish(
            ['Решить две задачи', 'Собрать рюкзак'], 45, expected_revision=None,
            task_minutes=[20, None], task_points=[10, 20],
        )
        results = {}

        class EditorUnderTest(PlanEditor):
            def buttonbox(editor):
                def edit():
                    results['original'] = [entry.get() for entry in editor.estimates]
                    results['weights'] = [entry.get() for entry in editor.weights]
                    editor.entries[0].delete(0, 'end')
                    with patch('daily_plan_ui.messagebox.showwarning') as warning:
                        results['valid_missing_task'] = editor.validate()
                    results['warned'] = warning.called
                    editor.estimates[0].delete(0, 'end')
                    editor.entries[-1].insert(0, 'Прочитать страницу')
                    editor.estimates[-1].insert(0, '12')
                    editor.weights[-1].set('10')
                    results['valid'] = editor.validate()
                    results['value'] = editor.result
                    editor.cancel()
                editor.after(0, edit)

        EditorUnderTest(self.root, saved)
        self.assertEqual(results['original'], ['20'] + [''] * (MAX_TASKS - 1))
        self.assertEqual(results['weights'], ['10', '20'] + ['15'] * (MAX_TASKS - 2))
        self.assertFalse(results['valid_missing_task'])
        self.assertTrue(results['warned'])
        self.assertTrue(results['valid'])
        self.assertEqual(results['value'], (['Собрать рюкзак', 'Прочитать страницу'], 45, [None, 12], [20, 10], False))

    def test_scene_animates_only_newly_saved_completion(self):
        self.publish()
        revision = self.panel.plan['revision']
        with patch.object(self.panel.scene, 'celebrate') as celebrate:
            self.panel._task(0, 'done', revision)
            celebrate.assert_called_once_with([(0, 15, True), (1, 15, False)], 0, 0)
            self.assertIn('15 / 30', self.panel.steve_today.cget('text'))
            self.assertIn('ждёт проверки: 15', self.panel.steve_today.cget('text'))
            self.assertIn('подтверждено 0 / 600', self.panel.steve_score.cget('text'))
            self.panel.refresh()
            self.panel._task(0, 'done', self.panel.plan['revision'])
            self.panel._task(1, 'done', revision)
            celebrate.assert_called_once()

            self.panel._task(1, 'done', self.panel.plan['revision'])
            self.assertEqual(celebrate.call_count, 2)
            self.assertIn('Молодец', self.panel.status.cget('text'))
            self.assertIn('30 / 30', self.panel.steve_today.cget('text'))

            self.panel._task(0, 'pending', self.panel.plan['revision'])
            self.assertEqual(celebrate.call_count, 2)
            self.assertIn('15 / 30', self.panel.steve_today.cget('text'))

    def test_rebirth_confirmation_preserves_today_and_starts_new_path(self):
        self.assertEqual(self.panel.rebirth_button.winfo_manager(), '')
        for offset in range(20):
            self.day = date(2026, 11, 1) + timedelta(days=offset)
            plan = self.store.publish(['Решить две задачи'], 45, expected_revision=None)
            self.approve_store(plan)
        self.panel.refresh()
        self.assertEqual(self.panel.rebirth_button.winfo_manager(), 'pack')
        self.assertIn('600 / 600', self.panel.steve_score.cget('text'))

        with patch('daily_plan_ui.messagebox.askokcancel', return_value=False):
            self.panel.rebirth_button.invoke()
        self.assertEqual(self.store.snapshot()[1]['rebirth_count'], 0)

        with patch('daily_plan_ui.messagebox.askokcancel', return_value=True):
            self.panel.rebirth_button.invoke()
        saved, progress = self.store.snapshot()
        self.assertEqual(progress['rebirth_count'], 1)
        self.assertEqual(progress['total_points'], 0)
        self.assertEqual(saved['tasks'][0]['status'], 'done')
        self.assertEqual(self.panel.rebirth_button.winfo_manager(), '')
        self.assertIn('Путь Стива 2 · подтверждено 0 / 600', self.panel.steve_score.cget('text'))

    def test_blank_panel_refresh_and_date_poll_do_not_create_plan_files(self):
        self.assertFalse(self.store.root.exists())
        self.panel.refresh()
        self.day += timedelta(days=1)
        self.panel.after_cancel(self.panel._date_after)
        self.panel._poll_date()
        self.assertIsNone(self.panel.plan)
        self.assertFalse(self.store.root.exists())

    def test_parent_repeat_checkbox_saves_template_and_can_disable_it(self):
        self.guard.verify.return_value = True
        enabled = (['Решить две задачи'], 45, [20], [15], True)
        disabled = (['Прочитать страницу'], 45, [None], [20], False)
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'), patch(
            'daily_plan_ui.PlanEditor', return_value=SimpleNamespace(result=enabled)
        ):
            self.panel.edit_button.invoke()
        self.assertTrue(self.store.snapshot()[1]['repeat_daily'])
        self.day += timedelta(days=1)
        self.panel.refresh()
        self.assertEqual(self.panel.plan['tasks'][0]['text'], 'Решить две задачи')
        self.assertEqual(self.panel.plan['tasks'][0]['status'], 'pending')
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'), patch(
            'daily_plan_ui.PlanEditor', return_value=SimpleNamespace(result=disabled)
        ) as editor, patch('daily_plan_ui.messagebox.askokcancel', return_value=True):
            self.panel.edit_button.invoke()
        self.assertTrue(editor.call_args.kwargs['repeat_daily'])
        self.assertFalse(self.store.snapshot()[1]['repeat_daily'])
        self.day += timedelta(days=1)
        self.panel.refresh()
        self.assertIsNone(self.panel.plan)

    def test_new_plan_editor_exposes_parent_daily_repeat_choice(self):
        results = {}

        class EditorUnderTest(PlanEditor):
            def buttonbox(editor):
                def choose():
                    results['label'] = editor.repeat_daily_check.cget('text')
                    results['default_repeat'] = editor.repeat_daily_var.get()
                    editor.entries[0].insert(0, 'Прочитать страницу')
                    editor.repeat_daily_var.set(True)
                    results['valid'] = editor.validate()
                    results['repeat'] = editor.result[4]
                    editor.cancel()
                editor.after(0, choose)

        EditorUnderTest(self.root, None)
        self.assertTrue(results['valid'])
        self.assertFalse(results['default_repeat'])
        self.assertTrue(results['repeat'])
        self.assertIn('каждый день', results['label'])

    def test_open_panel_date_poll_materializes_once_without_same_day_redraw(self):
        plan = self.store.publish(['Прочитать страницу'], 45, expected_revision=None, repeat_daily=True)
        self.panel.refresh()
        self.panel._task(0, 'done', plan['revision'])
        yesterday = self.panel.plan
        self.panel.after_cancel(self.panel._date_after)
        with patch.object(self.panel, 'refresh', wraps=self.panel.refresh) as refresh, patch(
            'daily_plan_ui.simpledialog.askstring'
        ) as prompt, patch('daily_plan_ui.os.startfile') as open_folder:
            self.panel._poll_date()
            refresh.assert_not_called()
            self.assertEqual(self.panel.plan['revision'], yesterday['revision'])
            self.day += timedelta(days=1)
            self.panel.after_cancel(self.panel._date_after)
            self.panel._poll_date()
            refresh.assert_called_once_with()
            prompt.assert_not_called()
            open_folder.assert_not_called()
        self.assertEqual(self.panel.plan['date'], self.day.isoformat())
        self.assertEqual(self.panel.plan['tasks'][0]['status'], 'pending')
        self.assertEqual(self.store.read(yesterday['date'])['tasks'][0]['status'], 'done')
        self.assertEqual(self.store.snapshot()[1]['total_points'], 0)
        timer = self.panel._date_after
        self.assertIn(timer, self.root.tk.call('after', 'info'))
        self.panel.destroy()
        self.assertIsNone(self.panel._date_after)
        self.assertNotIn(timer, self.root.tk.call('after', 'info'))

    def test_approval_separates_checked_xp_from_confirmed_equipment(self):
        self.publish()
        self.submit()
        self.assertEqual(self.panel.steve_progress['provisional_points'], 30)
        self.assertEqual(self.panel.steve_progress['total_points'], 0)
        self.assertIn('ждёт проверки: 30', self.panel.steve_today.cget('text'))
        self.guard.verify.return_value = True
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
            self.panel.approve_button.invoke()
        self.assertEqual(self.panel.steve_progress['total_points'], 30)
        self.assertEqual(self.panel.steve_progress['provisional_points'], 0)
        self.assertIn('подтверждено за день: 30 / 30 XP', self.panel.steve_today.cget('text'))
        self.assertIn('подтверждено 30 / 600 XP', self.panel.steve_score.cget('text'))

    def test_history_requires_pin_and_late_approval_never_ends_today_study(self):
        self.publish()
        self.submit()
        old_date = self.day.isoformat()
        self.day += timedelta(days=1)
        current = self.store.publish(['Новое дело'], 45, expected_revision=None)
        self.panel.refresh()
        with patch('daily_plan_ui.simpledialog.askstring', return_value='wrong'):
            self.panel.history_button.invoke()
        self.assertIsNone(self.panel._selected_date)
        self.assertEqual(self.panel.plan['date'], self.day.isoformat())
        panel, study = self.study_panel()
        panel.history_button.invoke()
        self.assertEqual(panel._selected_date, old_date)
        self.assertIn(old_date, panel.heading.cget('text'))
        self.assertIn('Ждут проверки: 1', panel.pending_reviews_label.cget('text'))
        self.assertTrue(panel.edit_button.instate(['disabled']))
        self.assertEqual(panel.rebirth_button.winfo_manager(), '')
        panel.approve_button.invoke()
        self.assertEqual(self.store.read(old_date)['status'], 'approved')
        self.assertEqual(self.store.read(), current)
        self.assertEqual(self.store.snapshot()[1]['total_points'], 30)
        self.assertEqual(panel.play_button.winfo_manager(), '')
        study.complete_plan_study.assert_not_called()
        study.active_study_id.assert_not_called()
        study.start_lessons.assert_not_called()
        study.request_stop.assert_not_called()
        panel.today_button.invoke()
        self.assertIsNone(panel._selected_date)
        self.assertEqual(panel.plan, current)

    def test_nonrepeating_yesterday_plan_is_reviewable_without_creating_today(self):
        self.publish()
        self.submit()
        yesterday = self.day.isoformat()
        self.day += timedelta(days=1)
        before = self.store.path.read_bytes()
        self.panel.refresh()
        self.assertIsNone(self.panel.plan)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(self.panel.history_button.instate(['!disabled']))
        self.guard.verify.return_value = True
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
            self.panel.history_button.invoke()
        self.assertEqual(self.panel.plan['date'], yesterday)
        with patch('daily_plan_ui.simpledialog.askstring', side_effect=['123456', 'Проверь решение.']):
            self.panel._return_task(0, self.panel.plan['revision'])
        self.assertEqual(self.panel.plan['status'], 'open')
        self.assertIn(yesterday, self.panel.history_date.get())
        self.panel._task(0, 'done', self.panel.plan['revision'])
        self.panel.submit_button.invoke()
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
            self.panel.approve_button.invoke()
        self.assertEqual(self.store.read(yesterday)['status'], 'approved')
        self.assertIsNone(self.store.read())
        self.assertEqual(self.store.snapshot()[1]['total_points'], 30)
        self.assertEqual(self.store.history(), [])

    def test_historical_rework_and_resubmit_are_scoped_and_do_not_bind_new_study(self):
        self.publish()
        self.submit()
        old_date = self.day.isoformat()
        self.day += timedelta(days=1)
        current = self.store.publish(['Сегодняшнее дело'], 45, expected_revision=None)
        panel, study = self.study_panel()
        panel.history_button.invoke()
        with patch('daily_plan_ui.simpledialog.askstring', return_value='Проверь решение.'):
            panel._return_task(0, panel.plan['revision'])
        self.assertEqual(self.store.read(old_date)['status'], 'open')
        self.assertEqual(self.store.read(old_date)['tasks'][0]['status'], 'pending')
        panel._task(0, 'done', panel.plan['revision'])
        panel.submit_button.invoke()
        historical = self.store.read(old_date)
        self.assertEqual(historical['status'], 'submitted')
        self.assertNotIn('study_session_id', historical)
        self.assertEqual(self.store.read(), current)
        study.active_study_id.assert_not_called()
        study.complete_plan_study.assert_not_called()

    def test_history_selection_change_during_pin_cannot_approve_another_day(self):
        self.publish()
        self.submit()
        old_date = self.day.isoformat()
        self.day += timedelta(days=1)
        self.publish()
        self.submit()
        today = self.store.read()
        self.guard.verify.return_value = True
        with patch('daily_plan_ui.simpledialog.askstring', return_value='123456'):
            self.panel.history_button.invoke()

        def switch_to_today(*_args, **_kwargs):
            self.panel._show_today()
            return '123456'

        with patch('daily_plan_ui.simpledialog.askstring', side_effect=switch_to_today):
            self.panel._approve()
        self.assertEqual(self.store.read(old_date)['status'], 'submitted')
        self.assertEqual(self.store.read(), today)
        self.assertEqual(self.store.snapshot()[1]['total_points'], 0)

    def test_destroy_during_parent_pin_cancels_review(self):
        self.publish()
        self.submit()
        submitted = self.store.read()
        self.guard.verify.return_value = True

        def close_panel(*_args, **_kwargs):
            self.panel.destroy()
            return '123456'

        with patch('daily_plan_ui.simpledialog.askstring', side_effect=close_panel):
            self.panel._approve()
        self.assertEqual(self.store.read(), submitted)
        self.guard.verify.assert_not_called()

    def test_archive_capacity_failure_is_visible_without_losing_saved_plan(self):
        self.publish()
        before = self.store.path.read_bytes()
        with patch.object(self.store, 'ensure_today', side_effect=ValueError('Архив планов заполнен.')):
            self.assertFalse(self.panel.refresh())
        self.assertIn('Архив планов заполнен', self.panel.notice.cget('text'))
        self.assertTrue(self.panel.edit_button.instate(['disabled']))
        self.assertTrue(self.panel.approve_button.instate(['disabled']))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_rebirth_rejects_historical_xp_added_during_confirmation(self):
        early = self.store.publish(['Проверить потом'], 45, expected_revision=None)
        early = self.store.set_task(0, 'done', expected_revision=early['revision'])
        early = self.store.submit(expected_revision=early['revision'])
        for offset in range(1, 21):
            self.day = date(2026, 11, 1) + timedelta(days=offset)
            plan = self.store.publish(['Подтверждённое дело'], 45, expected_revision=None)
            self.approve_store(plan)
        self.panel.refresh()
        self.assertEqual(self.panel.steve_progress['total_points'], 600)
        current_revision = self.panel.plan['revision']

        def confirm_after_late_approval(*_args, **_kwargs):
            self.store.review(approved=True, feedback='', expected_revision=early['revision'], plan_date=early['date'])
            self.assertEqual(self.store.read()['revision'], current_revision)
            return True

        with patch('daily_plan_ui.messagebox.askokcancel', side_effect=confirm_after_late_approval), patch.object(
            self.store, 'rebirth', wraps=self.store.rebirth
        ) as rebirth:
            self.panel.rebirth_button.invoke()
        self.assertEqual(rebirth.call_args.kwargs['expected_total_points'], 600)
        self.assertEqual(self.store.snapshot()[1]['total_points'], 630)
        self.assertEqual(self.store.snapshot()[1]['rebirth_count'], 0)
        self.assertIn('Баллы уже изменились', self.panel.notice.cget('text'))

    def test_full_plan_scrolls_at_small_window_size(self):
        self.root.geometry('860x420')
        self.root.deiconify()
        self.panel.pack(fill='both', expand=True)
        tasks = [f'Дело {index + 1}' for index in range(MAX_TASKS)]
        self.store.publish(tasks, 45, expected_revision=None)
        self.panel.refresh()
        self.root.update()

        canvas = self.panel.task_list.canvas
        self.assertLess(canvas.yview()[1], 1)
        canvas.yview_moveto(1)
        self.root.update()
        last_row = self.panel.rows.winfo_children()[-2]
        self.assertLess(last_row.winfo_rooty(), canvas.winfo_rooty() + canvas.winfo_height())
        self.assertTrue(self.panel.edit_button.winfo_viewable())
        self.assertGreater(self.panel.edit_button.winfo_rooty(), canvas.winfo_rooty() + canvas.winfo_height())

    def test_integrated_controls_use_study_panel_and_follow_live_state(self):
        study = Mock()
        study.status = tk.StringVar(self.root, 'Выключено')
        study.notice = tk.StringVar(self.root, 'Неверный PIN.')
        study.session.active = False
        study.authorize_parent.return_value = False
        panel = DailyPlanPanel(self.root, store=self.store, guard=self.guard, study=study)
        panel.edit_button.invoke()
        study.authorize_parent.assert_called_once_with('Для родителей', allow_setup=True)
        self.assertEqual(panel.notice.cget('text'), 'Неверный PIN.')

        panel.study_duration.current(1)
        panel.study_start.invoke()
        study.start_lessons.assert_called_once_with(duration_minutes=120)
        self.assertTrue(panel.study_stop.instate(['disabled']))
        study.session.active = True
        study.status.set('Уроки · 120:00')
        self.assertTrue(panel.study_start.instate(['disabled']))

        study.request_stop.return_value = False
        panel.study_stop.invoke()
        study.request_stop.assert_called_once_with()
        self.assertEqual(panel.notice.cget('text'), 'Неверный PIN.')
        self.assertTrue(panel.study_stop.instate(['!disabled']))
        study.session.active = False
        study.status.set('Выключено')
        self.assertTrue(panel.study_stop.instate(['disabled']))
        panel.destroy()
        self.assertEqual(study.status.trace_info(), [])
