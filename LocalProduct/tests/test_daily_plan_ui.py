"""Exercise the daily plan UI with synthetic tasks."""

import sys
from pathlib import Path as BootstrapPath

sys.path.insert(0, str(BootstrapPath(__file__).resolve().parents[1] / 'src'))

import tkinter as tk
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
        self.addCleanup(self.root.destroy)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.day = date(2026, 11, 1)
        self.store = DailyPlan(directory.name, today=lambda: self.day)
        self.guard = Mock()
        self.guard.configured.return_value = True
        self.guard.verify.return_value = False
        self.panel = DailyPlanPanel(self.root, store=self.store, guard=self.guard)

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
        self.assertEqual(results['value'], (['Собрать рюкзак', 'Прочитать страницу'], 45, [None, 12], [20, 10]))

    def test_scene_animates_only_newly_saved_completion(self):
        self.publish()
        revision = self.panel.plan['revision']
        with patch.object(self.panel.scene, 'celebrate') as celebrate:
            self.panel._task(0, 'done', revision)
            celebrate.assert_called_once_with([(0, 15, True), (1, 15, False)], 15, 0)
            self.assertIn('15 / 30', self.panel.steve_today.cget('text'))
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
            self.store.set_task(0, 'done', expected_revision=plan['revision'])
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
        self.assertIn('Путь Стива 2 · 0 / 600', self.panel.steve_score.cget('text'))

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
