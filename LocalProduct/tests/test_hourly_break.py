"""Fake clocks and owned temporary files only; no Study/Play/GUI/registry/network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date,timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]/'LocalProduct/src'))
sys.path.insert(0,str(HERE))
import hourly_break as module
from hourly_break import HourlyBreakStore,HourlyBreakError,HourlyBreakConflict


def ident(value):return f'{value:032x}'


class Clock:
    def __init__(self):self.wall=10000.0;self.mono=100.0
    def advance(self,seconds):self.wall+=seconds;self.mono+=seconds


class HourlyBreakTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='hourly-fixture-',dir=HERE)
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)/'data'
        self.clock=Clock();self.day=date(2026,11,26);self.seq=10
        self.store=self.make();self.study=ident(100);self.play=ident(1)

    def nonce(self):self.seq+=1;return ident(self.seq)

    def make(self):
        return HourlyBreakStore(self.root,clock=lambda:self.clock.wall,monotonic=lambda:self.clock.mono,
                                today=lambda:self.day,nonce=self.nonce)

    def enable(self):
        return self.store.configure(True,'Навести порядок на согласованной с мамой полке.',self.store.status()['revision'])

    def completed(self,play=None,study=None):
        return {'current':{'play_id':play or self.play,'return_study_id':study or self.study,
            'phase':'completed','result':'return_confirmed','duration_seconds':3600,
            'remaining_seconds':0,'created_utc':self.clock.wall-3600,
            'expires_utc':self.clock.wall,'max_seen_utc':self.clock.wall}}

    def begin(self):
        self.enable();self.clock.advance(3600)
        self.view=self.store.observe_completed(self.completed(),self.study)
        return self.view

    def ready(self):
        self.begin();self.clock.advance(600)
        self.view=self.store.observe_completed(self.completed(),self.study)
        return self.view

    def submitted(self):
        self.ready();self.view=self.store.request_done(self.view['break_id'],self.view['revision']);return self.view

    def approved(self):
        self.submitted();self.submitted_revision=self.view['revision']
        self.view=self.store.approve(self.view['break_id'],self.view['revision'],self.study);return self.view

    def test_reads_are_pure_and_disabled_by_default(self):
        self.assertFalse(self.root.exists())
        self.assertFalse(self.store.status()['enabled'])
        self.assertEqual(self.store.status()['phase'],'idle')
        self.assertIsNone(self.store.status()['grant'])
        self.assertFalse(self.root.exists())

    def test_enabling_does_not_grant_or_change_play(self):
        view=self.enable();self.assertTrue(view['enabled']);self.assertEqual(view['phase'],'idle')
        self.assertEqual((view['play_minutes'],view['break_minutes']),(60,10));self.assertIsNone(view['grant'])

    def test_invalid_configuration_never_creates_state(self):
        for enabled,task in [(1,'Useful'),(None,'Useful'),(True,''),(True,'a\nb'),(True,' x'),(True,'x'*241)]:
            with self.subTest(enabled=enabled,task=task),self.assertRaises(HourlyBreakError):self.store.configure(enabled,task,0)
        self.assertFalse(self.root.exists())

    def test_configuration_revision_is_strict(self):
        self.enable();before=self.store.path.read_bytes()
        for revision in [True,1.0,'1',None,0]:
            with self.subTest(revision=revision),self.assertRaises(HourlyBreakError):self.store.configure(False,'Useful',revision)
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_natural_hour_creates_one_fixed_break(self):
        view=self.begin();record=view['current']
        self.assertEqual(view['phase'],'waiting');self.assertEqual(view['remaining_break_seconds'],600)
        self.assertEqual(record['ready_utc'],record['started_utc']+600)
        self.assertEqual(record['source_play_id'],self.play);self.assertEqual(record['study_id'],self.study)
        self.assertFalse(view['can_request_done']);self.assertFalse(view['can_approve'])

    def test_ten_minutes_elapsed_never_auto_unlocks(self):
        view=self.ready();self.assertTrue(view['can_request_done']);self.assertFalse(view['can_approve'])
        self.assertIsNone(view['grant']);self.assertEqual(view['phase'],'waiting')

    def test_child_done_is_only_request_not_parent_authority(self):
        view=self.submitted();self.assertEqual(view['phase'],'submitted');self.assertTrue(view['can_approve'])
        self.assertIsNone(view['grant'])

    def test_child_cannot_submit_early(self):
        view=self.begin();self.clock.advance(599)
        with self.assertRaises(HourlyBreakError):self.store.request_done(view['break_id'],view['revision'])
        self.assertEqual(self.store.status()['remaining_break_seconds'],1)
        self.assertEqual(self.store.status()['phase'],'waiting')

    def test_parent_cannot_skip_child_request(self):
        view=self.ready()
        with self.assertRaises(HourlyBreakConflict):self.store.approve(view['break_id'],view['revision'],self.study)
        self.assertIsNone(self.store.status()['grant'])

    def test_parent_approval_emits_one_stable_grant(self):
        view=self.approved();grant=view['grant']
        self.assertEqual(grant['minutes'],60);self.assertEqual(grant['linked_study_id'],self.study)
        self.assertEqual(grant['date'],self.day.isoformat());self.assertEqual(grant['request_id'],view['break_id'])
        retry=self.store.approve(view['break_id'],self.submitted_revision,self.study)
        self.assertEqual(retry['grant'],grant);self.assertEqual(retry['revision'],view['revision'])

    def test_parent_revision_rejects_bool_float_even_on_retry(self):
        view=self.approved();before=self.store.path.read_bytes()
        for revision in [True,float(self.submitted_revision),None,'3']:
            with self.subTest(revision=revision),self.assertRaises(HourlyBreakError):self.store.approve(view['break_id'],revision,self.study)
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_unrelated_study_blocks_parent_approval(self):
        view=self.submitted()
        with self.assertRaises(HourlyBreakConflict):self.store.approve(view['break_id'],view['revision'],ident(101))
        after=self.store.status();self.assertEqual(after['phase'],'blocked');self.assertIsNone(after['grant'])
        with self.assertRaises(HourlyBreakConflict):self.store.approve(after['break_id'],after['revision'],self.study)

    def test_missing_study_does_not_authorize_replayed_parent_grant(self):
        view=self.approved()
        with self.assertRaises(HourlyBreakConflict):self.store.approve(view['break_id'],view['revision'],None)
        self.assertEqual(self.store.status()['phase'],'blocked')

    def test_recovery_after_actual_next_play_can_confirm_without_study(self):
        view=self.approved();reopened=self.make()
        done=reopened.confirm_next(view['break_id'],ident(200))
        self.assertEqual(done['phase'],'consumed');self.assertIsNone(done['grant'])
        self.assertEqual(reopened.confirm_next(view['break_id'],ident(200)),done)

    def test_different_next_play_cannot_reuse_consumed_break(self):
        view=self.approved();self.store.confirm_next(view['break_id'],ident(200))
        before=self.store.path.read_bytes()
        with self.assertRaises(HourlyBreakConflict):self.store.confirm_next(view['break_id'],ident(201))
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_each_new_hour_requires_new_task_and_parent_approval(self):
        first=self.approved();self.store.confirm_next(first['break_id'],ident(200))
        self.clock.advance(3600)
        second=self.store.observe_completed(self.completed(ident(200),ident(300)),ident(300))
        self.assertNotEqual(second['break_id'],first['break_id']);self.assertEqual(second['remaining_break_seconds'],600)
        self.assertIsNone(second['grant']);self.assertEqual(len(self.store.read()['seen_play_ids']),2)

    def test_configured_task_changes_only_future_break(self):
        view=self.begin();old=view['task']
        self.store.configure(True,'Другое полезное дело.',view['revision'])
        self.assertEqual(self.store.status()['task'],old)
        self.assertEqual(self.store.status()['configured_task'],'Другое полезное дело.')

    def test_disable_cancels_current_without_erasing_seen(self):
        view=self.approved();cancelled=self.store.configure(False,'Useful',view['revision'])
        self.assertEqual(cancelled['phase'],'cancelled');self.assertIsNone(cancelled['grant'])
        self.assertIn(self.play,self.store.read()['seen_play_ids'])

    def test_cancel_before_break_consumes_source_identity(self):
        self.enable();self.clock.advance(3600);completed=self.completed()
        cancelled=self.store.cancel(source_play_id=self.play)
        self.store.configure(True,'Useful',cancelled['revision'])
        after=self.store.observe_completed(completed,self.study)
        self.assertIsNone(after['current']);self.assertFalse(after['can_approve'])

    def test_reenable_never_revives_cancelled_break(self):
        old=self.approved();cancelled=self.store.cancel(source_play_id=self.play)
        self.store.configure(True,'Useful',cancelled['revision'])
        view=self.store.observe_completed(self.completed(),self.study)
        self.assertEqual(view['phase'],'cancelled');self.assertIsNone(view['grant'])
        with self.assertRaises(HourlyBreakConflict):self.store.approve(old['break_id'],view['revision'],self.study)

    def test_old_unobserved_hour_before_enable_is_not_redeemable(self):
        old=self.completed();self.clock.advance(1);self.enable()
        view=self.store.observe_completed(old,self.study);self.assertIsNone(view['current'])

    def test_ignored_play_on_configure_is_durable(self):
        self.store.configure(True,'Useful',0,ignored_play_id=self.play)
        self.clock.advance(3600)
        self.assertIsNone(self.store.observe_completed(self.completed(),self.study)['current'])

    def test_duplicate_old_completion_after_consumption_is_not_new_break(self):
        view=self.approved();self.store.confirm_next(view['break_id'],ident(200))
        after=self.store.observe_completed(self.completed(),self.study)
        self.assertEqual(after['phase'],'consumed');self.assertEqual(after['break_id'],view['break_id'])

    def test_foreign_study_seen_by_keeper_blocks_existing_break(self):
        view=self.begin();completed=self.completed()
        after=self.store.observe_completed(completed,ident(222))
        self.assertEqual(after['phase'],'blocked');self.assertEqual(after['current']['result'],'study_replaced')

    def test_approved_grant_cannot_move_to_next_day(self):
        view=self.approved();self.day+=timedelta(days=1)
        with self.assertRaises(HourlyBreakConflict):self.store.approve(view['break_id'],view['revision'],self.study)
        self.assertEqual(self.store.status()['phase'],'cancelled')

    def test_break_can_cross_midnight_before_parent_approval(self):
        self.submitted();self.day+=timedelta(days=1)
        view=self.store.status();approved=self.store.approve(view['break_id'],view['revision'],self.study)
        self.assertEqual(approved['grant']['date'],self.day.isoformat())

    def test_restart_keeps_deadline_and_elapsed_time(self):
        first=self.begin();deadline=first['current']['ready_utc'];self.clock.advance(500)
        self.store.observe_completed(self.completed(),self.study);self.clock.advance(100)
        reopened=self.make();view=reopened.observe_completed(self.completed(),self.study)
        self.assertEqual(view['current']['ready_utc'],deadline);self.assertTrue(view['can_request_done'])

    def test_clock_checkpoints_do_not_stale_pin_revision(self):
        view=self.begin();revision=view['revision']
        self.clock.advance(100);after=self.store.observe_completed(self.completed(),self.study)
        self.assertEqual(after['revision'],revision);self.assertEqual(after['remaining_break_seconds'],500)

    def test_wall_rollback_blocks_and_preserves_data(self):
        view=self.begin();self.clock.advance(100);self.store.observe_completed(self.completed(),self.study)
        self.clock.wall-=10
        with self.assertRaises(HourlyBreakConflict):self.store.request_done(view['break_id'],view['revision'])
        after=self.store.status();self.assertEqual(after['phase'],'blocked');self.assertIsNone(after['grant'])

    def test_monotonic_rollback_blocks(self):
        view=self.begin();self.store.observe_completed(self.completed(),self.study);self.clock.mono-=1
        with self.assertRaises(HourlyBreakConflict):self.store.request_done(view['break_id'],view['revision'])
        self.assertEqual(self.store.status()['current']['result'],'clock_rollback')

    def test_frozen_wall_uses_monotonic_without_shortening_break(self):
        view=self.begin();self.store.observe_completed(self.completed(),self.study)
        self.clock.mono+=599
        with self.assertRaises(HourlyBreakError):self.store.request_done(view['break_id'],view['revision'])
        self.clock.mono+=1
        submitted=self.store.request_done(view['break_id'],view['revision'])
        self.assertEqual(submitted['phase'],'submitted')

    def test_only_natural_full_hour_is_eligible(self):
        self.enable();self.clock.advance(3600);base=self.completed()
        for changes in [{'duration_seconds':1800},{'duration_seconds':True},{'remaining_seconds':1},
                        {'phase':'playing'},{'result':'restrictions_already_present'},
                        {'max_seen_utc':base['current']['expires_utc']-1}]:
            value=deepcopy(base);value['current'].update(changes)
            with self.subTest(changes=changes):self.assertIsNone(self.store.observe_completed(value,self.study)['current'])

    def test_no_own_returned_study_means_no_break(self):
        self.enable();self.clock.advance(3600)
        for study in [None,ident(300)]:self.assertIsNone(self.store.observe_completed(self.completed(),study)['current'])

    def test_malformed_id_is_rejected_without_reset(self):
        view=self.begin();before=self.store.path.read_bytes()
        for value in ['',False,1,'x'*32]:
            with self.subTest(value=value),self.assertRaises(HourlyBreakError):self.store.request_done(value,view['revision'])
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_corrupt_duplicate_or_unknown_state_is_not_reset(self):
        self.enable();original=self.store.read()
        variants=[b'{',b'{"version":1,"version":1}',json.dumps({**original,'surprise':1}).encode(),
                  json.dumps({**original,'version':True}).encode(),b'\xff']
        for raw in variants:
            self.store.path.write_bytes(raw)
            with self.subTest(raw=raw),self.assertRaises(HourlyBreakError):self.store.status()
            self.assertEqual(self.store.path.read_bytes(),raw)

    def test_oversized_file_is_preserved(self):
        self.enable();raw=b'x'*(module.MAX_BYTES+1);self.store.path.write_bytes(raw)
        with self.assertRaises(HourlyBreakError):self.store.status()
        self.assertEqual(self.store.path.read_bytes(),raw)

    def test_failed_atomic_replace_preserves_old_complete_state(self):
        view=self.begin();before=self.store.path.read_bytes()
        with mock.patch.object(module.os,'replace',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.store.configure(False,'Useful',view['revision'])
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(list(self.root.glob('.hourly-*.tmp')),[])

    def test_actual_owned_hardlink_state_rejected(self):
        self.enable();alias=self.root/'linked.json'
        os.link(self.store.path,alias)
        with self.assertRaises(ValueError):self.store.status()
        self.assertTrue(alias.exists())

    def test_concurrent_parent_retries_share_one_grant(self):
        view=self.submitted()
        def approve(_):return self.make().approve(view['break_id'],view['revision'],self.study)['grant']
        with ThreadPoolExecutor(max_workers=2) as pool:grants=list(pool.map(approve,range(2)))
        self.assertEqual(grants[0],grants[1]);self.assertEqual(self.store.status()['phase'],'approved')

    def test_concurrent_child_submission_has_one_semantic_revision(self):
        view=self.ready()
        def submit(_):
            try:return self.make().request_done(view['break_id'],view['revision'])['phase']
            except HourlyBreakConflict:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(results),['conflict','submitted'])
        self.assertEqual(self.store.status()['revision'],view['revision']+1)

    def test_full_ledger_blocks_new_cycle_without_pruning(self):
        self.enable();state=self.store.read();state['seen_play_ids']=[ident(n) for n in range(1,module.MAX_SEEN+1)]
        self.store.path.write_text(json.dumps(state),encoding='utf-8');before=self.store.path.read_bytes()
        self.clock.advance(3600)
        with self.assertRaises(HourlyBreakError):self.store.observe_completed(self.completed(ident(9000)),self.study)
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_full_ledger_does_not_block_parent_cancellation(self):
        self.enable();state=self.store.read();state['seen_play_ids']=[ident(n) for n in range(1,module.MAX_SEEN+1)]
        self.store.path.write_text(json.dumps(state),encoding='utf-8')
        view=self.store.cancel(source_play_id=ident(9000))
        self.assertFalse(view['enabled']);self.assertIsNone(view['grant'])
        self.assertEqual(len(self.store.read()['seen_play_ids']),module.MAX_SEEN)


if __name__=='__main__':unittest.main(verbosity=2)
