"""Daily recurrence and approval-ledger contracts, using only isolated data."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from daily_plan import DailyPlan, DailyPlanConflict, DailyPlanError, MAX_HISTORY_PLANS


class DailyRepeatTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix='daily-repeat-')
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name) / 'plan'
        self.day = date(2026, 11, 19)
        self.store = DailyPlan(self.root, today=lambda: self.day)

    def publish(self, **kwargs):
        return self.store.publish(
            ['Прочитать страницу', 'Подготовить тетради'], 60, expected_revision=None,
            task_minutes=[20, None], task_points=[20, 10], **kwargs,
        )

    def finish(self, plan):
        for index in range(len(plan['tasks'])):
            plan = self.store.set_task(index, 'done', expected_revision=plan['revision'])
        return self.store.submit(expected_revision=plan['revision'], study_session_id='b' * 32)

    def test_repeat_is_opt_in_and_reading_never_rolls_over(self):
        plan = self.publish()
        before = self.store.path.read_bytes()
        self.day += timedelta(days=1)
        self.assertIsNone(self.store.read())
        self.assertIsNone(self.store.snapshot()[0])
        self.assertEqual(self.store.history(), [])
        self.assertIsNone(self.store.ensure_today())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.read(plan['date']), plan)
        self.assertFalse(self.store.progress()['repeat_daily'])

    def test_nonrepeat_last_day_is_browsable_without_forcing_a_new_plan(self):
        for status in ('open', 'submitted', 'approved'):
            with self.subTest(status=status):
                self.day = date(2026, 11, 19)
                self.store = DailyPlan(self.root / status, today=lambda: self.day)
                plan = self.publish()
                if status != 'open':
                    plan = self.finish(plan)
                if status == 'approved':
                    plan = self.store.review(approved=True, feedback='', expected_revision=plan['revision'])
                self.assertEqual(self.store.past_days(), [])
                self.day += timedelta(days=1)
                before = self.store.path.read_bytes()
                self.assertIsNone(self.store.read())
                self.assertEqual(self.store.history(), [])
                self.assertEqual(self.store.past_days(), [plan])
                self.assertIsNone(self.store.ensure_today())
                self.assertEqual(self.store.path.read_bytes(), before)
                if status == 'submitted':
                    self.assertEqual(self.store.progress()['pending_reviews'], [plan])
                    plan = self.store.review(approved=True, feedback='Проверено позже',
                                             expected_revision=plan['revision'], plan_date=plan['date'])
                    self.assertIsNone(self.store.read())
                    self.assertEqual(self.store.history(), [])
                    self.assertEqual(self.store.past_days(), [plan])
                    self.assertEqual(self.store.progress()['total_points'], 30)
                    self.assertFalse(self.store.progress()['repeat_daily'])

    def test_repeat_preserves_template_progress_history_and_resets_only_daily_state(self):
        plan = self.finish(self.publish(repeat_daily=True))
        approved = self.store.review(approved=True, feedback='Спасибо!', expected_revision=plan['revision'])
        self.assertEqual(self.store.progress()['total_points'], 30)
        old_bytes = self.store.path.read_bytes()
        self.day += timedelta(days=1)
        self.assertIsNone(self.store.read())
        self.assertEqual(self.store.path.read_bytes(), old_bytes)
        fresh = self.store.ensure_today()
        self.assertEqual(fresh['date'], self.day.isoformat())
        self.assertNotEqual(fresh['revision'], approved['revision'])
        self.assertEqual(fresh['status'], 'open')
        self.assertEqual(fresh['game_minutes'], 60)
        self.assertEqual(fresh['feedback'], '')
        self.assertNotIn('study_session_id', fresh)
        self.assertEqual(fresh['tasks'], [
            {'text': 'Прочитать страницу', 'status': 'pending', 'estimated_minutes': 20, 'points': 20},
            {'text': 'Подготовить тетради', 'status': 'pending', 'points': 10},
        ])
        self.assertEqual(self.store.history(), [approved])
        self.assertEqual(self.store.past_days(), [approved])
        self.assertEqual(self.store.progress()['total_points'], 30)
        self.assertEqual(self.store.progress()['today_points'], 0)
        current_bytes = self.store.path.read_bytes()
        self.assertEqual(self.store.ensure_today(), fresh)
        self.assertEqual(self.store.path.read_bytes(), current_bytes)
        reopened = DailyPlan(self.root, today=lambda: self.day)
        self.assertEqual(reopened.ensure_today(), fresh)
        self.assertTrue(reopened.progress()['repeat_daily'])
        self.day += timedelta(days=4)
        self.assertEqual(reopened.past_days(), [fresh, approved])
        latest = reopened.ensure_today()
        self.assertEqual(latest['date'], self.day.isoformat())
        self.assertEqual(reopened.history(), [fresh, approved])
        self.assertEqual(reopened.past_days(), [fresh, approved])
        self.assertEqual(reopened.progress()['total_points'], 30)

    def test_publish_preserves_or_disables_repetition_and_rejects_bad_flag(self):
        plan = self.publish(repeat_daily=True)
        plan = self.store.publish(['Новая задача'], 45, expected_revision=plan['revision'], task_points=[15])
        self.day += timedelta(days=1)
        fresh = self.store.ensure_today()
        self.assertEqual([task['text'] for task in fresh['tasks']], ['Новая задача'])
        self.assertEqual(fresh['game_minutes'], 45)
        before = self.store.path.read_bytes()
        for invalid in (0, 1, '', 'true', [], {}):
            with self.subTest(flag=invalid), self.assertRaises(DailyPlanError):
                self.store.publish(['Другое'], 45, expected_revision=fresh['revision'], repeat_daily=invalid)
        self.assertEqual(self.store.path.read_bytes(), before)
        stopped = self.store.publish(['Разовое дело'], 45, expected_revision=fresh['revision'], repeat_daily=False)
        self.assertFalse(self.store.progress()['repeat_daily'])
        self.day += timedelta(days=1)
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.ensure_today())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.read(stopped['date']), stopped)

    def test_late_review_preserves_request_and_credits_once_without_changing_today(self):
        submitted = self.finish(self.publish(repeat_daily=True))
        self.day += timedelta(days=1)
        today = self.store.ensure_today()
        self.assertEqual(self.store.history(), [submitted])
        self.assertEqual(self.store.progress()['pending_reviews'], [submitted])
        selected, progress = self.store.snapshot(submitted['date'])
        self.assertEqual(selected, submitted)
        self.assertEqual(progress['today_points'], 30)
        self.assertEqual(progress['provisional_points'], 30)
        self.assertEqual(progress['approved_today_points'], 0)
        self.assertEqual(progress['total_points'], 0)
        with self.assertRaises(DailyPlanConflict):
            self.store.review(approved=True, feedback='', expected_revision=submitted['revision'])
        before = self.store.path.read_bytes()
        with mock.patch('daily_plan.os.replace', side_effect=PermissionError('synthetic failure')):
            with self.assertRaises(PermissionError):
                self.store.review(approved=True, feedback='', expected_revision=submitted['revision'],
                                  plan_date=submitted['date'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        approved = self.store.review(approved=True, feedback='Проверено позже',
                                     expected_revision=submitted['revision'], plan_date=submitted['date'])
        self.assertEqual(approved['study_session_id'], 'b' * 32)
        self.assertEqual(self.store.read(), today)
        self.assertEqual(self.store.progress()['total_points'], 30)
        self.assertEqual(self.store.progress()['pending_reviews'], [])
        _, progress = self.store.snapshot(submitted['date'])
        self.assertEqual(progress['approved_today_points'], 30)
        self.assertEqual(progress['provisional_points'], 0)
        with self.assertRaises(DailyPlanConflict):
            self.store.review(approved=True, feedback='', expected_revision=submitted['revision'],
                              plan_date=submitted['date'])
        with self.assertRaises(DailyPlanError):
            self.store.review(approved=True, feedback='', expected_revision=approved['revision'],
                              plan_date=approved['date'])
        self.assertEqual(self.store.progress()['total_points'], 30)

    def test_archived_return_can_be_completed_and_resubmitted_without_altering_today(self):
        submitted = self.finish(self.publish(repeat_daily=True))
        self.day += timedelta(days=1)
        today = self.store.ensure_today()
        target = submitted['date']
        returned = self.store.review(approved=False, feedback='Проверь тетради', task_index=1,
                                     expected_revision=submitted['revision'], plan_date=target)
        self.assertNotIn('study_session_id', returned)
        self.assertEqual(self.store.snapshot(target)[1]['provisional_points'], 20)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.assertEqual(self.store.read(), today)
        finished = self.store.set_task(1, 'done', expected_revision=returned['revision'], plan_date=target)
        resubmitted = self.store.submit(expected_revision=finished['revision'], plan_date=target)
        self.store.review(approved=True, feedback='', expected_revision=resubmitted['revision'], plan_date=target)
        self.assertEqual(self.store.read(), today)
        self.assertEqual(self.store.progress()['total_points'], 30)
        self.assertEqual(self.store.history()[0]['status'], 'approved')

    def test_approved_legacy_bank_migrates_without_rewrite_or_double_credit(self):
        plan = self.finish(self.publish())
        plan = self.store.review(approved=True, feedback='', expected_revision=plan['revision'])
        for payload in (
            plan,
            {'version': 1, 'plan': plan, 'past_points': 37, 'rebirth_count': 0, 'rebirth_date': None},
        ):
            with self.subTest(payload=payload):
                before = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.store.path.write_bytes(before)
                expected = 30 + (37 if 'version' in payload else 0)
                self.assertEqual(self.store.progress()['total_points'], expected)
                self.assertEqual(self.store.progress()['total_points'], expected)
                self.assertEqual(self.store.path.read_bytes(), before)
                self.day += timedelta(days=1)
                fresh = self.publish()
                saved = json.loads(self.store.path.read_text(encoding='utf-8'))
                self.assertEqual(saved['version'], 2)
                self.assertEqual(saved['past_points'], expected)
                self.assertEqual(self.store.history(), [plan])
                self.assertEqual(self.store.progress()['total_points'], expected)
                self.assertEqual(self.store.read(), fresh)
                self.day -= timedelta(days=1)

    def test_legacy_unapproved_plan_does_not_remove_saved_bank_or_invent_history(self):
        plan = self.finish(self.publish())
        for status in ('open', 'submitted'):
            with self.subTest(status=status):
                legacy_plan = {**plan, 'status': status}
                if status == 'open':
                    legacy_plan.pop('study_session_id', None)
                legacy = {'version': 1, 'plan': legacy_plan, 'past_points': 37,
                          'rebirth_count': 0, 'rebirth_date': None}
                before = json.dumps(legacy).encode('utf-8')
                self.store.path.write_bytes(before)
                progress = self.store.progress()
                self.assertEqual(progress['total_points'], 37)
                self.assertEqual(progress['provisional_points'], 30)
                self.assertEqual(progress['approved_today_points'], 0)
                self.assertEqual(self.store.history(), [])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_mutating_returned_snapshots_cannot_change_plan_or_template(self):
        original = self.publish(repeat_daily=True)
        plan, progress = self.store.snapshot()
        plan['tasks'][0]['text'] = 'Изменение только в копии'
        progress['current_tier']['armor'] = 'diamond'
        self.assertEqual(self.store.read(), original)
        self.day += timedelta(days=1)
        fresh = self.store.ensure_today()
        history = self.store.history()
        history[0]['tasks'][0]['points'] = 10
        self.assertEqual(self.store.history(), [original])
        self.assertEqual(fresh['tasks'][0]['text'], original['tasks'][0]['text'])
        self.assertEqual(fresh['tasks'][0]['points'], 20)

    def test_serialized_size_limit_refuses_rollover_without_replacing_file(self):
        self.publish(repeat_daily=True)
        before = self.store.path.read_bytes()
        self.day += timedelta(days=1)
        with mock.patch('daily_plan.MAX_FILE_BYTES', len(before)):
            with self.assertRaisesRegex(DailyPlanError, 'Файл плана слишком большой'):
                self.store.ensure_today()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.history(), [])

    def test_late_approval_after_rebirth_is_credited_to_new_cycle(self):
        submitted = self.finish(self.publish(repeat_daily=True))
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        state['past_points'] = 600
        self.store.path.write_text(json.dumps(state), encoding='utf-8')
        self.day += timedelta(days=1)
        today = self.store.ensure_today()
        today = self.store.rebirth(expected_revision=today['revision'], expected_cycle=0, expected_total_points=600)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.assertEqual(self.store.progress()['rebirth_count'], 1)
        self.store.review(approved=True, feedback='', expected_revision=submitted['revision'],
                          plan_date=submitted['date'])
        self.assertEqual(self.store.progress()['total_points'], 30)
        self.assertEqual(self.store.read(), today)
        self.day += timedelta(days=1)
        self.store.ensure_today()
        self.assertEqual(self.store.progress()['total_points'], 30)

    def test_late_approval_invalidates_rebirth_snapshot_with_or_without_today_plan(self):
        for has_today in (False, True):
            with self.subTest(has_today=has_today):
                self.day = date(2026, 11, 19)
                case_root = self.root / str(has_today)
                self.store = DailyPlan(case_root, today=lambda: self.day)
                submitted = self.finish(self.publish(repeat_daily=True))
                state = json.loads(self.store.path.read_text(encoding='utf-8'))
                state['past_points'] = 600
                self.store.path.write_text(json.dumps(state), encoding='utf-8')
                self.day += timedelta(days=1)
                if has_today:
                    self.store.ensure_today()
                plan, captured = self.store.snapshot()
                self.assertEqual(captured['total_points'], 600)
                self.assertEqual(plan is not None, has_today)
                revision = plan['revision'] if plan else None
                self.store.review(approved=True, feedback='', expected_revision=submitted['revision'],
                                  plan_date=submitted['date'])
                self.assertEqual(self.store.progress()['total_points'], 630)
                if has_today:
                    self.assertEqual(self.store.read()['revision'], revision)
                before = self.store.path.read_bytes()
                with self.assertRaises(DailyPlanConflict):
                    self.store.rebirth(expected_revision=revision, expected_cycle=captured['rebirth_count'],
                                       expected_total_points=captured['total_points'])
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertEqual(self.store.progress()['rebirth_count'], 0)
                for invalid in (True, -1, '630', None):
                    with self.subTest(expected=invalid), self.assertRaises(DailyPlanError):
                        self.store.rebirth(expected_revision=revision, expected_cycle=0,
                                           expected_total_points=invalid)
                self.assertEqual(self.store.path.read_bytes(), before)
                self.store.rebirth(expected_revision=revision, expected_cycle=0, expected_total_points=630)
                self.assertEqual(self.store.progress()['total_points'], 0)
                self.assertEqual(self.store.progress()['rebirth_count'], 1)

    def test_legacy_rebirth_already_consumed_approval_is_not_credited_again(self):
        approved = self.finish(self.publish())
        approved['status'] = 'approved'
        state = {'version': 1, 'plan': approved, 'past_points': 0,
                 'rebirth_count': 1, 'rebirth_date': approved['date']}
        before = json.dumps(state).encode('utf-8')
        self.store.path.write_bytes(before)
        self.assertEqual(self.store.progress()['total_points'], 0)
        self.assertEqual(self.store.progress()['approved_today_points'], 30)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.day += timedelta(days=1)
        self.publish()
        self.assertEqual(self.store.progress()['total_points'], 0)

    def test_repeated_rollover_is_serialized_and_replace_failure_keeps_old_snapshot(self):
        prior = self.publish(repeat_daily=True)
        self.day += timedelta(days=1)
        before = self.store.path.read_bytes()
        with mock.patch('daily_plan.os.replace', side_effect=PermissionError('synthetic failure')):
            with self.assertRaises(PermissionError):
                self.store.ensure_today()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: DailyPlan(self.root, today=lambda: self.day).ensure_today(), range(3)))
        self.assertEqual(results, [results[0]] * 3)
        self.assertEqual(self.store.history(), [prior])

    def test_midnight_or_clock_rollback_never_overwrites_saved_data(self):
        self.publish(repeat_daily=True)
        before = self.store.path.read_bytes()
        tomorrow = self.day + timedelta(days=1)
        rolling = DailyPlan(self.root, today=mock.Mock(side_effect=[tomorrow, tomorrow, tomorrow + timedelta(days=1)]))
        with self.assertRaises(DailyPlanConflict):
            rolling.ensure_today()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.day -= timedelta(days=1)
        with self.assertRaises(DailyPlanConflict):
            self.store.ensure_today()
        with self.assertRaises(DailyPlanError):
            self.store.read((self.day + timedelta(days=2)).isoformat())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_full_archive_refuses_rollover_without_pruning_or_losing_progress(self):
        plan = self.publish(repeat_daily=True)
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        state['past_points'] = 123
        for offset in reversed(range(1, MAX_HISTORY_PLANS + 1)):
            archived = deepcopy(plan)
            archived['date'] = (self.day - timedelta(days=offset)).isoformat()
            state['history'].append(archived)
        self.store.path.write_text(json.dumps(state), encoding='utf-8')
        self.day += timedelta(days=1)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(DailyPlanError, 'Архив планов заполнен'):
            self.store.ensure_today()
        with self.assertRaises(DailyPlanError):
            self.publish()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.progress()['total_points'], 123)
        self.assertEqual(len(self.store.history()), MAX_HISTORY_PLANS)
        self.assertEqual(self.store.read(plan['date']), plan)

    def test_corrupt_history_or_template_is_rejected_without_rewrite(self):
        plan = self.publish(repeat_daily=True)
        base = json.loads(self.store.path.read_text(encoding='utf-8'))
        yesterday = deepcopy(plan)
        yesterday['date'] = (self.day - timedelta(days=1)).isoformat()
        fixtures = []
        for changes in (
            {'history': [yesterday, yesterday]},
            {'history': [plan]},
            {'history': [{**yesterday, 'date': (self.day + timedelta(days=1)).isoformat()}]},
            {'history': [{**yesterday, 'revision': True}]},
            {'history': [{**yesterday, 'unknown': 'field'}]},
            {'history': [yesterday] * (MAX_HISTORY_PLANS + 1)},
            {'repeat_template': {'game_minutes': 60, 'tasks': [{'text': 'А', 'status': 'pending'}]}},
            {'repeat_template': {'game_minutes': 60, 'tasks': [{'text': 'А', 'points': True}]}},
        ):
            fixtures.append({**base, **changes})
        for invalid in fixtures:
            with self.subTest(state=invalid):
                before = json.dumps(invalid).encode('utf-8')
                self.store.path.write_bytes(before)
                with self.assertRaises(DailyPlanError):
                    self.store.snapshot()
                self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
