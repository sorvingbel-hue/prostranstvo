"""Synthetic negotiated rewards; only temporary local fixtures, no real user data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'LocalProduct' / 'src'))
sys.path.insert(0, str(HERE))
import family_rewards as rewards
from family_rewards import RewardBook, RewardConflict, RewardError


class RewardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='reward-fixture-', dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        self.book = RewardBook(self.root, clock=lambda: 1000.5)
        self.day, self.plan_revision = '2026-11-26', 123456

    def revision(self):
        return self.book.snapshot()['revision']

    def tasks(self, goal_id=None):
        items = [{'reward_id': 'play', 'kind': 'game_minutes', 'amount': 60, 'label': 'Игра'}]
        if goal_id:
            items.append({'reward_id': 'fins', 'kind': 'goal', 'amount': 20,
                          'label': 'На ласты', 'goal_id': goal_id})
        return [{'task_id': 'plan', 'criteria': 'Все четыре дела выполнены по согласованному списку.', 'rewards': items}]

    def prepare(self, tasks=None, *, day=None, revision=None):
        return self.book.prepare_plan(day or self.day, revision or self.plan_revision,
            self.tasks() if tasks is None else tasks, expected_revision=self.revision())

    def evaluate(self, percent=100, *, ident='review_1', expected=None, comment=None):
        if comment is None:
            comment = '' if percent == 100 else 'На столе остались вещи: разложи их, затем попроси пересмотр.'
        return self.book.evaluate(self.day, self.plan_revision, {'plan': percent},
            review_id=ident, expected_revision=self.revision() if expected is None else expected,
            comment=comment)

    def goal(self, title='Ласты', target=100):
        proposed = self.book.propose_goal(title, target, 'баллы', expected_revision=self.revision())
        gid = proposed['goal']['goal_id']
        self.book.confirm_goal(gid, expected_revision=self.revision())
        return gid

    def test_pure_read_does_not_create_anything(self):
        self.assertEqual(self.book.snapshot(), {'version': 1, 'revision': 0, 'goals': [], 'agreements': []})
        self.assertFalse(self.root.exists())

    def test_exact_first_credit_and_fresh_process_read(self):
        self.prepare()
        result = self.evaluate(20)
        self.assertEqual(result['game_minutes_awarded'], 12)
        other = RewardBook(self.root)
        self.assertEqual(other.snapshot()['agreements'][0]['credited'], {'plan/play': 12})

    def test_replay_after_revision_advance_gives_zero_new_credit(self):
        self.prepare()
        old_revision = self.revision()
        first = self.evaluate(50, expected=old_revision)
        raw = self.book.path.read_bytes()
        again = self.evaluate(50, expected=old_revision)
        self.assertTrue(again['replayed'])
        self.assertEqual(again['game_minutes_awarded'], 0)
        self.assertEqual(again['review'], first['review'])
        self.assertEqual(self.book.path.read_bytes(), raw)

    def test_review_id_cannot_be_reused_for_changed_rating_or_comment(self):
        self.prepare(); self.evaluate(50)
        for percent, comment in [(60, 'Новый комментарий'), (50, 'Другой комментарий')]:
            with self.subTest(percent=percent), self.assertRaises(RewardConflict):
                self.evaluate(percent, comment=comment)

    def test_quality_upgrade_gives_only_positive_difference(self):
        self.prepare()
        self.assertEqual(self.evaluate(20)['game_minutes_awarded'], 12)
        self.assertEqual(self.evaluate(50, ident='review_2')['game_minutes_awarded'], 18)
        self.assertEqual(self.evaluate(100, ident='review_3')['game_minutes_awarded'], 30)
        self.assertEqual(self.evaluate(100, ident='review_4')['game_minutes_awarded'], 0)
        self.assertEqual(self.book.snapshot()['agreements'][0]['credited']['plan/play'], 60)

    def test_lower_rating_never_confiscates_or_allows_reearning_same_credit(self):
        self.prepare(); self.evaluate(100)
        self.assertEqual(self.evaluate(20, ident='down')['game_minutes_awarded'], 0)
        self.assertEqual(self.evaluate(100, ident='up')['game_minutes_awarded'], 0)
        item = self.book.snapshot()['agreements'][0]
        self.assertEqual(item['ratings']['plan'], 100)
        self.assertEqual(item['credited']['plan/play'], 60)

    def test_integer_rounding_and_zero_percent(self):
        task = self.tasks(); task[0]['rewards'][0]['amount'] = 59
        self.prepare(task)
        self.assertEqual(self.evaluate(0)['game_minutes_awarded'], 0)
        self.assertEqual(self.evaluate(33, ident='next')['game_minutes_awarded'], 19)

    def test_lower_quality_requires_explanation(self):
        self.prepare()
        with self.assertRaises(RewardError):
            self.evaluate(50, comment='')
        self.assertEqual(self.book.snapshot()['agreements'][0]['reviews'], [])

    def test_missing_contract_cannot_be_reviewed(self):
        with self.assertRaises(RewardError):
            self.evaluate()

    def test_contract_is_immutable_and_exact_retry_is_pure(self):
        data = self.tasks(); self.prepare(data)
        raw = self.book.path.read_bytes()
        again = self.book.prepare_plan(self.day, self.plan_revision, data, expected_revision=0)
        self.assertTrue(again['replayed'])
        self.assertEqual(self.book.path.read_bytes(), raw)
        data[0]['criteria'] = 'Задним числом другой критерий'
        with self.assertRaises(RewardConflict):
            self.prepare(data)

    def test_goals_are_child_proposals_until_parent_confirmation(self):
        proposed = self.book.propose_goal('Ласты', 100, 'баллы', expected_revision=0)
        gid = proposed['goal']['goal_id']
        self.assertEqual(proposed['goal']['status'], 'proposed')
        with self.assertRaises(RewardError):
            self.prepare(self.tasks(gid))
        self.book.confirm_goal(gid, expected_revision=self.revision())
        self.prepare(self.tasks(gid)); self.evaluate(50)
        self.assertEqual(self.book.snapshot()['goals'][0]['balance'], 10)

    def test_no_more_than_two_active_goals(self):
        self.goal('Ласты'); self.goal('Поход')
        with self.assertRaises(RewardError):
            self.goal('Третья цель')

    def test_retired_goal_keeps_earned_units_and_existing_contract(self):
        gid = self.goal(); self.prepare(self.tasks(gid)); self.evaluate(50)
        self.book.retire_goal(gid, expected_revision=self.revision())
        self.evaluate(100, ident='improved')
        goal = self.book.snapshot()['goals'][0]
        self.assertEqual((goal['balance'], goal['status']), (20, 'retired'))
        # An identical prepare replay is valid, but NEW agreements cannot
        # silently allocate future work to an already retired target.
        self.assertTrue(self.prepare(self.tasks(gid))['replayed'])
        with self.assertRaises(RewardError):
            self.prepare(self.tasks(gid), day='2026-11-27', revision=888)

    def test_goal_can_reach_target_without_losing_overflow(self):
        gid = self.goal(target=15)
        self.prepare(self.tasks(gid)); self.evaluate()
        goal = self.book.snapshot()['goals'][0]
        self.assertEqual(goal['balance'], 20)
        self.assertGreaterEqual(goal['balance'], goal['target'])
        self.goal('Новая цель 1'); self.goal('Новая цель 2')

    def test_custom_reward_is_local_credit_not_real_transfer(self):
        task = self.tasks()
        task[0]['rewards'] = [{'reward_id':'film', 'kind':'custom', 'label':'Выбор фильма', 'amount':100, 'unit':'баллы'}]
        self.prepare(task)
        value = self.evaluate(85)
        self.assertEqual(value['game_minutes_awarded'], 0)
        self.assertEqual(value['credits'], {'plan/film': 85})

    def test_revisit_never_awards_and_parent_review_resolves_request(self):
        self.prepare(); self.evaluate(50)
        value = self.book.request_revisit(self.day, self.plan_revision, 'plan', 'Я исправил стол.', expected_revision=self.revision())
        self.assertTrue(value['request']['requested'])
        self.assertEqual(self.book.snapshot()['agreements'][0]['credited']['plan/play'], 30)
        before = self.book.path.read_bytes()
        repeated = self.book.request_revisit(self.day, self.plan_revision, 'plan', 'Я исправил стол.', expected_revision=0)
        self.assertTrue(repeated['replayed'])
        self.assertEqual(self.book.path.read_bytes(), before)
        self.evaluate(100, ident='parent_again')
        request = self.book.snapshot()['agreements'][0]['revisits']['plan']
        self.assertFalse(request['requested'])
        self.assertEqual(request['resolved_review_id'], 'parent_again')

    def test_no_revisit_without_prior_review(self):
        self.prepare()
        with self.assertRaises(RewardError):
            self.book.request_revisit(self.day, self.plan_revision, 'plan', 'Хочу пересмотр.', expected_revision=self.revision())

    def test_stale_write_never_commits(self):
        self.prepare(); raw = self.book.path.read_bytes()
        with self.assertRaises(RewardConflict):
            self.evaluate(100, expected=0)
        self.assertEqual(self.book.path.read_bytes(), raw)

    def test_two_simultaneous_reviews_cannot_double_credit(self):
        self.prepare(); revision = self.revision()
        def evaluate(i):
            other = RewardBook(self.root, clock=lambda: 1001)
            try:
                return other.evaluate(self.day,self.plan_revision,{'plan':100},review_id=f'parallel_{i}',expected_revision=revision)['game_minutes_awarded']
            except RewardConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(evaluate, range(2)))
        self.assertCountEqual(results, [60, 'conflict'])
        self.assertEqual(self.book.snapshot()['agreements'][0]['credited']['plan/play'], 60)

    def test_pure_snapshot_returns_detached_data_and_preserves_file(self):
        self.prepare(); original = self.book.path.read_bytes(); before = self.book.path.stat().st_mtime_ns
        snapshot = self.book.snapshot(); snapshot['agreements'][0]['tasks'][0]['criteria'] = 'tamper caller only'
        self.assertNotEqual(self.book.snapshot()['agreements'][0]['tasks'][0]['criteria'], 'tamper caller only')
        self.assertEqual((self.book.path.read_bytes(), self.book.path.stat().st_mtime_ns), (original, before))

    def test_strict_schema_and_ledger_corruption_fail_closed_without_write(self):
        self.prepare(); self.evaluate(50)
        good = self.book.snapshot()
        corruptions = []
        value = deepcopy(good); value['extra'] = True; corruptions.append(value)
        value = deepcopy(good); value['version'] = True; corruptions.append(value)
        value = deepcopy(good); value['agreements'][0]['credited']['plan/play'] = 31; corruptions.append(value)
        value = deepcopy(good); value['agreements'][0]['reviews'][0]['credits']['plan/play'] = 31; value['agreements'][0]['credited']['plan/play'] = 31; corruptions.append(value)
        value = deepcopy(good); value['agreements'][0]['reviews'][0]['ratings']['plan'] = 60; corruptions.append(value)
        for bad in corruptions:
            with self.subTest(bad=bad):
                data=json.dumps(bad).encode(); self.book.path.write_bytes(data)
                with self.assertRaises(RewardError): self.book.snapshot()
                self.assertEqual(self.book.path.read_bytes(),data)

    def test_invalid_utf8_duplicate_keys_oversize_do_not_reset_data(self):
        self.prepare()
        for raw in (b'\xff', b'{"version":1,"version":1}', b'x'*(rewards.MAX_BYTES+1)):
            with self.subTest(size=len(raw)):
                self.book.path.write_bytes(raw)
                with self.assertRaises(RewardError): self.book.snapshot()
                self.assertEqual(self.book.path.read_bytes(),raw)

    def test_atomic_replace_failure_retains_previous_record(self):
        self.prepare(); original = self.book.path.read_bytes()
        with mock.patch.object(rewards.os,'replace',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): self.evaluate()
        self.assertEqual(self.book.path.read_bytes(),original)
        self.assertFalse(list(self.root.glob('.rewards-*')))

    def test_bounded_history_never_deletes_previous_records(self):
        self.prepare(); self.evaluate(50)
        raw = self.book.path.read_bytes()
        with mock.patch.object(rewards,'MAX_REVIEWS',1):
            with self.assertRaises(RewardError):self.evaluate(100,ident='extra')
        self.assertEqual(self.book.path.read_bytes(),raw)
        with mock.patch.object(rewards,'MAX_AGREEMENTS',1):
            with self.assertRaises(RewardError):self.prepare(day='2026-11-27',revision=999)
        self.assertEqual(self.book.path.read_bytes(),raw)

    def test_invalid_input_types_and_whole_contract_limits(self):
        for percent in (True, -1, 101, 50.5, '50'):
            with self.subTest(percent=percent),self.assertRaises(RewardError):self.evaluate(percent)
        for day,revision in [('20261126',1),('2026-02-31',1),('2026-11-26',True),('2026-11-26',0)]:
            with self.subTest(day=day,revision=revision),self.assertRaises(RewardError):
                self.book.prepare_plan(day,revision,self.tasks(),expected_revision=0)
        tasks = self.tasks()*3
        for index, task in enumerate(tasks):
            tasks[index]=deepcopy(task); tasks[index]['task_id']=f'part_{index}'
        with self.assertRaises(RewardError): self.prepare(tasks)

    def test_invalid_clock_no_partial_goal_or_award(self):
        self.prepare(); original=self.book.path.read_bytes()
        for clock in (True,float('nan'),float('inf'),-1,1e300):
            with self.subTest(clock=clock),mock.patch.object(self.book,'clock',return_value=clock),self.assertRaises(RewardError):
                self.evaluate()
            self.assertEqual(self.book.path.read_bytes(),original)

    def test_hardlink_or_reparse_file_is_not_read_or_overwritten(self):
        self.prepare(); original=self.book.path.read_bytes(); real_lstat=Path.lstat
        def lstat(path,*args,**kwargs):
            info=real_lstat(path,*args,**kwargs)
            if path==self.book.path:
                class Changed:
                    st_nlink=2
                    def __getattr__(self,key):return getattr(info,key)
                return Changed()
            return info
        with mock.patch.object(Path,'lstat',lstat):
            with self.assertRaises(ValueError):self.book.snapshot()
            with self.assertRaises(ValueError):self.evaluate()
        self.assertEqual(self.book.path.read_bytes(),original)

    def test_unsafe_root_paths_are_rejected_without_creating_data(self):
        for root in ('relative',r'\\server\share\data',str(self.root/'..'/'other')):
            with self.subTest(root=root),self.assertRaises(RewardError):RewardBook(root)


if __name__=='__main__': unittest.main()
