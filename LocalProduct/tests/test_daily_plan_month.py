"""Simulate school months against a real daily-plan store and isolated dates."""
from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

source = Path(__file__).resolve().parents[1] / 'src'
if source.is_dir():
    sys.path.insert(0, str(source))

from daily_plan import DailyPlan, DailyPlanConflict, DailyPlanError


class SteveMonthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='steve-month-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.day = date(2026, 11, 2)
        self.reopen()

    def reopen(self):
        self.store = DailyPlan(self.root, today=lambda: self.day)

    def school_day(self):
        self.day += timedelta(days=1)
        while self.day.weekday() >= 5:
            self.day += timedelta(days=1)

    def publish(self, count, weights=None):
        return self.store.publish(
            [f'{self.day.isoformat()} · дело {index + 1}' for index in range(count)],
            45, expected_revision=None, task_points=weights,
            task_minutes=[12] + [None] * (count - 1),
        )

    def mark(self, plan, index, status='done'):
        return self.store.set_task(index, status, expected_revision=plan['revision'])

    def finish(self, plan):
        for index in reversed(range(len(plan['tasks']))):
            if plan['tasks'][index]['status'] != 'done':
                plan = self.mark(plan, index)
        return plan

    def assert_score(self, *, today, total, tier, next_tier):
        progress = self.store.progress()
        self.assertEqual(progress['today_points'], today)
        self.assertEqual(progress['total_points'], total)
        self.assertEqual(progress['current_tier']['threshold'], tier)
        self.assertEqual(progress['next_tier']['threshold'] if progress['next_tier'] else None, next_tier)

    def test_full_month_all_equipment_and_rebirth(self):
        # Variable plan lengths must reach the same end of the cycle.
        milestones = (
            (0, 'none', 'iron', False), (10, 'leather', 'iron', False),
            (75, 'copper', 'iron', False), (150, 'iron', 'iron', True),
            (240, 'gold', 'diamond', False), (360, 'diamond', 'diamond', False),
            (480, 'diamond', 'netherite', False), (600, 'netherite', 'netherite', True),
        )
        self.assert_score(today=0, total=0, tier=0, next_tier=10)
        with self.assertRaises(DailyPlanError):
            self.store.rebirth(expected_revision=None, expected_cycle=0, expected_total_points=0)
        seen = {0}
        for day_index in range(1, 21):
            count = (10, 1, 4, 7)[(day_index - 1) % 4]
            plan = self.publish(count, [(10, 15, 20)[i % 3] for i in range(count)])
            plan = self.finish(plan)
            submitted = self.store.submit(expected_revision=plan['revision'])
            approved = self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
            self.reopen()
            saved, progress = self.store.snapshot()
            self.assertEqual(saved, approved)
            self.assertEqual(progress['today_points'], 30)
            self.assertEqual(progress['total_points'], day_index * 30)
            expected = next(tier for tier in reversed(milestones) if tier[0] <= day_index * 30)
            self.assertEqual(tuple(progress['current_tier'][key] for key in (
                'threshold', 'armor', 'pickaxe', 'enchanted',
            )), expected)
            seen.add(expected[0])
            self.assertEqual(progress['can_rebirth'], day_index == 20)
            if day_index < 20:
                self.school_day()

        self.assertEqual(seen, {tier[0] for tier in milestones})
        reborn = self.store.rebirth(expected_revision=approved['revision'], expected_cycle=0, expected_total_points=600)
        self.reopen()
        self.assertEqual(self.store.read()['tasks'], approved['tasks'])
        self.assertEqual(reborn['status'], 'approved')
        self.assert_score(today=30, total=0, tier=0, next_tier=10)
        self.assertEqual(self.store.progress()['rebirth_count'], 1)
        with self.assertRaises(DailyPlanConflict):
            self.store.rebirth(expected_revision=reborn['revision'], expected_cycle=0, expected_total_points=0)

        self.school_day()
        self.assertIsNone(self.store.read())
        self.assert_score(today=0, total=0, tier=0, next_tier=10)
        plan = self.finish(self.publish(1))
        self.assert_score(today=30, total=0, tier=0, next_tier=10)
        submitted = self.store.submit(expected_revision=plan['revision'])
        self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
        self.assert_score(today=30, total=30, tier=10, next_tier=75)

    def test_mixed_month_missing_days_return_and_resume(self):
        total = 0
        for day_index in range(20):
            if day_index % 5 == 4:
                self.reopen()
                self.assertIsNone(self.store.read())
                self.assertEqual(self.store.progress()['total_points'], total)
            else:
                plan = self.publish(3, [10, 15, 20])
                plan = self.mark(plan, 1, 'help')
                self.assertEqual(self.store.progress()['today_points'], 0)
                plan = self.mark(plan, 2)
                self.assertEqual(self.store.progress()['today_points'], 13)
                plan = self.mark(plan, 2)
                self.assertEqual(self.store.progress()['today_points'], 13)
                plan = self.mark(plan, 2, 'pending')
                self.assertEqual(self.store.progress()['today_points'], 0)
                plan = self.mark(plan, 2)
                if day_index % 5 == 0:
                    # The plan can be left partially complete overnight.
                    self.assertEqual(self.store.progress()['provisional_points'], 13)
                elif day_index % 5 == 1:
                    plan = self.finish(plan)
                    submitted = self.store.submit(expected_revision=plan['revision'])
                    plan = self.store.review(approved=False, feedback='Проверь ещё раз.', task_index=2,
                                             expected_revision=submitted['revision'])
                    self.assertEqual(self.store.progress()['today_points'], 16)
                    self.assertEqual(plan['feedback'], 'Проверь ещё раз.')
                    self.assertEqual(self.store.progress()['provisional_points'], 16)
                else:
                    plan = self.finish(plan)
                    self.assertEqual(self.store.progress()['total_points'], total)
                    submitted = self.store.submit(expected_revision=plan['revision'])
                    plan = self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
                    total += 30
                self.reopen()
                self.assertEqual(self.store.read(), plan)
                self.assertEqual(self.store.progress()['total_points'], total)
            self.school_day()

        self.assertEqual(total, 240)
        self.assert_score(today=0, total=240, tier=240, next_tier=360)
        self.assertFalse(self.store.progress()['can_rebirth'])
        plan = self.finish(self.publish(10, [15] * 10))
        self.assert_score(today=30, total=240, tier=240, next_tier=360)
        submitted = self.store.submit(expected_revision=plan['revision'])
        self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
        self.assert_score(today=30, total=270, tier=240, next_tier=360)

    def test_replaced_plan_and_old_window_do_not_add_to_month(self):
        for day_index in range(20):
            plan = self.finish(self.publish(3, [10, 15, 20]))
            old_revision = plan['revision']
            plan = self.store.publish(
                ['Другой список на сегодня', 'Резервное дело'], 60,
                expected_revision=old_revision, task_points=[20, 10],
            )
            self.assertEqual(self.store.progress()['total_points'], 0)
            with self.assertRaises(DailyPlanConflict):
                self.mark({'revision': old_revision}, 0)
            plan = self.mark(plan, 0)
            self.assertEqual(self.store.progress()['today_points'], 20)
            self.assertEqual(self.store.progress()['provisional_points'], 20)
            self.assertEqual(self.store.progress()['total_points'], 0)
            self.school_day()
        self.reopen()
        self.assert_score(today=0, total=0, tier=0, next_tier=10)
        self.assertEqual(len(self.store.history()), 19)


if __name__ == '__main__':
    unittest.main()
