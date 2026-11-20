"""Daily-plan contracts using synthetic tasks in a temporary directory."""

import sys
from pathlib import Path as BootstrapPath

sys.path.insert(0, str(BootstrapPath(__file__).resolve().parents[1] / 'src'))

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from daily_plan import (
    DailyPlan, DailyPlanConflict, DailyPlanError, MAX_TASK_LENGTH, MAX_TASKS,
)


class DailyPlanTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'tests-work'
        base.mkdir(exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix='daily-plan-', dir=base)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name) / 'plan'
        self.day = date(2026, 11, 11)
        self.store = DailyPlan(self.root, today=lambda: self.day)

    def publish(self):
        return self.store.publish(['Решить две задачи', 'Собрать рюкзак'], 45, expected_revision=None)

    def submit(self, *, study_session_id=None):
        plan = self.publish()
        for index in range(len(plan['tasks'])):
            plan = self.store.set_task(index, 'done', expected_revision=plan['revision'])
        return self.store.submit(expected_revision=plan['revision'], study_session_id=study_session_id)

    def test_reads_do_not_create_data(self):
        self.assertIsNone(self.store.read())
        self.assertIsNone(self.store.snapshot()[0])
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.past_days(), [])
        self.assertEqual(self.store.progress()['pending_reviews'], [])
        self.assertIsNone(self.store.ensure_today())
        self.assertFalse(self.root.exists())

    def test_checklist_round_trip_and_parent_confirmation(self):
        submitted = self.submit()
        self.assertEqual(submitted['status'], 'submitted')
        other = DailyPlan(self.root, today=lambda: self.day)
        approved = other.review(approved=True, feedback='Спасибо!', expected_revision=submitted['revision'])
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(approved['game_minutes'], 45)
        self.assertEqual(approved['feedback'], 'Спасибо!')
        self.assertEqual(self.store.read(), approved)
        with self.assertRaises(DailyPlanError):
            self.store.set_task(0, 'pending', expected_revision=approved['revision'])
        self.assertEqual(self.store.read(), approved)

    def test_help_is_visible_and_blocks_submission_until_done(self):
        published = self.publish()
        help_plan = self.store.set_task(1, 'help', expected_revision=published['revision'])
        self.assertEqual(self.store.read()['tasks'][1]['status'], 'help')
        with self.assertRaises(DailyPlanError):
            self.store.submit(expected_revision=help_plan['revision'])
        self.assertEqual(self.store.read(), help_plan)

    def test_review_returns_only_the_chosen_item_and_can_be_submitted_again(self):
        submitted = self.submit(study_session_id='a' * 32)
        self.assertEqual(self.store.read()['study_session_id'], 'a' * 32)
        with self.assertRaises(DailyPlanError):
            self.store.review(approved=False, feedback='', task_index=1, expected_revision=submitted['revision'])
        returned = self.store.review(
            approved=False, feedback='Положи тетрадь в рюкзак.', task_index=1, expected_revision=submitted['revision'],
        )
        self.assertEqual(returned['status'], 'open')
        self.assertNotIn('study_session_id', returned)
        self.assertEqual([task['status'] for task in returned['tasks']], ['done', 'pending'])
        self.assertEqual(returned['feedback'], 'Положи тетрадь в рюкзак.')
        finished = self.store.set_task(1, 'done', expected_revision=returned['revision'])
        resubmitted = self.store.submit(expected_revision=finished['revision'])
        self.assertEqual(resubmitted['status'], 'submitted')
        self.assertNotIn('study_session_id', resubmitted)

    def test_stale_revision_cannot_overwrite_another_window(self):
        initial = self.publish()
        current = self.store.set_task(0, 'done', expected_revision=initial['revision'])
        with self.assertRaises(DailyPlanConflict):
            self.store.set_task(1, 'help', expected_revision=initial['revision'])
        self.assertEqual(self.store.read(), current)

    def test_estimates_are_optional_and_survive_reopening_and_review(self):
        original = self.publish()
        self.assertNotIn('estimated_minutes', original['tasks'][0])
        self.assertEqual(self.store.read(), original)

        plan = self.store.publish(
            ['Прочитать страницу', 'Подготовить тетради'], 45,
            expected_revision=original['revision'], task_minutes=[20, None],
        )
        reopened = DailyPlan(self.root, today=lambda: self.day)
        self.assertEqual(reopened.read()['tasks'], [
            {'text': 'Прочитать страницу', 'status': 'pending', 'estimated_minutes': 20},
            {'text': 'Подготовить тетради', 'status': 'pending'},
        ])
        for index in range(len(plan['tasks'])):
            plan = reopened.set_task(index, 'done', expected_revision=plan['revision'])
        submitted = reopened.submit(expected_revision=plan['revision'])
        approved = reopened.review(approved=True, feedback='', expected_revision=submitted['revision'])
        self.assertEqual(approved['tasks'][0]['estimated_minutes'], 20)

    def test_invalid_estimate_cannot_replace_plan(self):
        original = self.publish()
        for invalid in ([], [20, 30], [True], [0], [121], ['20']):
            with self.subTest(estimate=invalid):
                with self.assertRaises(DailyPlanError):
                    self.store.publish(
                        ['Прочитать страницу'], 45, expected_revision=original['revision'], task_minutes=invalid,
                    )
        self.assertEqual(self.store.read(), original)

    def test_weighted_points_follow_current_checks_and_parent_review(self):
        plan = self.store.publish(['А', 'Б', 'В'], 45, expected_revision=None, task_points=[10, 15, 20])
        self.assertEqual([task['points'] for task in plan['tasks']], [10, 15, 20])
        for index, status, expected in (
            (2, 'done', 13), (2, 'done', 13), (0, 'done', 20), (2, 'pending', 6),
            (1, 'help', 6), (1, 'done', 16), (2, 'done', 30),
        ):
            plan = self.store.set_task(index, status, expected_revision=plan['revision'])
            stored, progress = self.store.snapshot()
            self.assertEqual(stored, plan)
            self.assertEqual(progress['today_points'], expected)
            self.assertEqual(progress['provisional_points'], expected)
            self.assertEqual(progress['approved_today_points'], 0)
            self.assertEqual(progress['total_points'], 0)
            self.assertEqual(progress['current_tier']['threshold'], 0)
        plan = self.store.submit(expected_revision=plan['revision'])
        returned = self.store.review(approved=False, feedback='Повтори пункт.', task_index=2,
                                     expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['provisional_points'], 16)
        self.assertEqual(self.store.progress()['total_points'], 0)
        plan = self.store.publish(['Новый список'], 45, expected_revision=returned['revision'], task_points=[15])
        self.assertEqual(self.store.progress()['total_points'], 0)
        plan = self.store.set_task(0, 'done', expected_revision=plan['revision'])
        plan = self.store.submit(expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['provisional_points'], 30)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.store.review(approved=True, feedback='', expected_revision=plan['revision'])
        progress = self.store.progress()
        self.assertEqual(progress['today_points'], 30)
        self.assertEqual(progress['provisional_points'], 0)
        self.assertEqual(progress['approved_today_points'], 30)
        self.assertEqual(progress['total_points'], 30)
        self.assertEqual(progress['current_tier']['threshold'], 10)

    def test_old_unapproved_file_is_read_without_rewrite_and_archived_without_credit(self):
        self.root.mkdir()
        legacy = {
            'date': self.day.isoformat(), 'revision': 1, 'status': 'open',
            'game_minutes': 45, 'feedback': '', 'tasks': [
                {'text': 'Дроби', 'status': 'done', 'estimated_minutes': 20},
                {'text': 'Чтение', 'status': 'pending'},
            ],
        }
        serialized = json.dumps(legacy).encode('utf-8')
        self.store.path.write_bytes(serialized)
        self.assertEqual(self.store.read(), legacy)
        self.assertEqual(self.store.progress()['today_points'], 15)
        self.assertEqual(self.store.progress()['provisional_points'], 15)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.assertEqual(self.store.path.read_bytes(), serialized)
        self.day += timedelta(days=1)
        self.assertIsNone(self.store.read())
        self.assertEqual(self.store.progress()['total_points'], 0)
        plan = self.store.publish(['Новое задание'], 45, expected_revision=None, task_points=[20])
        self.assertEqual(self.store.history(), [legacy])
        self.assertEqual(self.store.progress()['total_points'], 0)
        completed = self.store.set_task(0, 'done', expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['total_points'], 0)
        completed = self.store.set_task(0, 'done', expected_revision=completed['revision'])
        self.assertEqual(self.store.progress()['provisional_points'], 30)
        submitted = self.store.submit(expected_revision=completed['revision'])
        self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
        self.assertEqual(self.store.progress()['total_points'], 30)
        self.day -= timedelta(days=1)
        with self.assertRaises(DailyPlanConflict):
            self.publish()
        self.assertEqual(self.store.progress()['total_points'], 30)

    def test_invalid_weights_cannot_replace_plan(self):
        original = self.publish()
        for invalid in ([], [20, 15], [True], [5], [30], ['20']):
            with self.subTest(weights=invalid), self.assertRaises(DailyPlanError):
                self.store.publish(['Новое задание'], 45, expected_revision=original['revision'], task_points=invalid)
        self.assertEqual(self.store.read(), original)

    def test_full_plan_fits_snapshot_and_rejects_an_eleventh_task(self):
        tasks = ['📚' * MAX_TASK_LENGTH] * MAX_TASKS
        plan = self.store.publish(tasks, 45, expected_revision=None, task_minutes=[120] * MAX_TASKS)
        self.assertEqual(self.store.read(), plan)
        with self.assertRaises(DailyPlanError):
            self.store.publish(tasks + ['Лишний пункт'], 45, expected_revision=plan['revision'])
        self.assertEqual(self.store.read(), plan)

    def test_prior_day_review_cannot_apply_to_the_next_day(self):
        old = self.submit()
        self.day += timedelta(days=1)
        self.assertIsNone(self.store.read())
        current = self.publish()
        with self.assertRaises(DailyPlanConflict):
            self.store.review(approved=True, feedback='', expected_revision=old['revision'])
        self.assertEqual(self.store.read(), current)
        self.assertEqual(current['date'], self.day.isoformat())

    def test_replace_failure_preserves_snapshot_and_cleans_temporary_file(self):
        initial = self.publish()
        snapshot = self.store.snapshot()
        with mock.patch('daily_plan.os.replace', side_effect=PermissionError('synthetic failure')):
            with self.assertRaises(PermissionError):
                self.store.set_task(0, 'done', expected_revision=initial['revision'])
        self.assertEqual(self.store.snapshot(), snapshot)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_rebirth_after_rollover_is_atomic_and_stale_cycle_fails(self):
        self.root.mkdir()
        self.store.path.write_text(json.dumps({
            'version': 1, 'plan': None, 'past_points': 600, 'rebirth_count': 0, 'rebirth_date': None,
        }), encoding='utf-8')
        snapshot = self.store.snapshot()
        with mock.patch('daily_plan.os.replace', side_effect=PermissionError('synthetic failure')):
            with self.assertRaises(PermissionError):
                self.store.rebirth(expected_revision=None, expected_cycle=0, expected_total_points=600)
        self.assertEqual(self.store.snapshot(), snapshot)
        self.assertIsNone(self.store.rebirth(expected_revision=None, expected_cycle=0, expected_total_points=600))
        self.assertEqual(self.store.progress()['rebirth_count'], 1)
        self.assertEqual(self.store.progress()['total_points'], 0)
        with self.assertRaises(DailyPlanConflict):
            self.store.rebirth(expected_revision=None, expected_cycle=0, expected_total_points=0)
        with self.assertRaises(DailyPlanError):
            self.store.rebirth(expected_revision=None, expected_cycle=1, expected_total_points=0)

        plan = self.publish()
        self.store.set_task(0, 'done', expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['today_points'], 15)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.day += timedelta(days=1)
        plan = self.publish()
        plan = self.store.set_task(0, 'done', expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['provisional_points'], 15)
        self.assertEqual(self.store.progress()['total_points'], 0)
        plan = self.store.set_task(1, 'done', expected_revision=plan['revision'])
        plan = self.store.submit(expected_revision=plan['revision'])
        self.store.review(approved=True, feedback='', expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['total_points'], 30)

    def test_invalid_text_and_corrupt_snapshot_are_rejected(self):
        with self.assertRaises(DailyPlanError):
            self.store.publish(['строка\nстрока'], 45, expected_revision=None)
        plan = self.submit()
        plan['tasks'][1]['status'] = 'help'
        self.store.path.write_text(json.dumps(plan), encoding='utf-8')
        with self.assertRaises(DailyPlanError):
            self.store.read()


if __name__ == '__main__':
    unittest.main()
