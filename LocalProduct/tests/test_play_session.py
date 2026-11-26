"""Owned temporary files, injected clocks and no registry/GUI/real Study state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import play_session as module
from play_session import PlaySession, PlaySessionError, PlayConflict, approval_key

DAY = '2026-11-26'
STUDY = 'a' * 32
OTHER = 'b' * 32


class Clocks:
    def __init__(self):
        self.wall = 100000.0
        self.mono = 500.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class PlaySessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='owned-play-', dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'uncreated'
        self.clock = Clocks()
        self.store = self.make()

    def make(self):
        return PlaySession(self.root, clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono, today=lambda: date.fromisoformat(DAY))

    def prepare(self, revision=1, minutes=60, linked=STUDY, actual=STUDY):
        return self.store.prepare(DAY, revision, minutes, linked, current_study_id=actual)

    def playing(self, minutes=60):
        result = self.prepare(minutes=minutes)
        return self.store.confirm_release(result['current']['play_id'], current_study_id=None)

    def expired(self):
        result = self.playing()
        self.clock.advance(3600)
        return self.store.reconcile(current_study_id=None)

    def bytes(self):
        return self.store.path.read_bytes()

    def write_state(self, value):
        self.root.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps(value), encoding='utf-8')

    def test_read_status_and_idle_reconcile_do_not_create_files(self):
        self.assertEqual(self.store.read(), {'version':1, 'redeemed':[], 'current':None})
        self.assertEqual(self.store.status(), {'current':None, 'action':'none', 'requires_startup':False})
        self.assertEqual(self.store.reconcile(current_study_id=None), self.store.status())
        self.assertFalse(self.root.exists())

    def test_prepare_is_durable_before_release_and_does_no_study_mutation(self):
        result = self.prepare()
        current = result['current']
        self.assertEqual(result['action'], 'release_linked_study')
        self.assertTrue(result['requires_startup'])
        self.assertEqual(current['phase'], 'pending_release')
        self.assertEqual(current['linked_study_id'], STUDY)
        self.assertEqual(current['created_utc'], 100000)
        self.assertEqual(current['expires_utc'], 103600)
        self.assertEqual(current['remaining_seconds'], 3600)
        self.assertEqual(self.make().read()['current'], current)
        self.assertEqual(self.store.read()['redeemed'], [approval_key(DAY,1)])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['session.json','session.lock'])

    def test_confirm_release_starts_no_new_deadline(self):
        prepared = self.prepare()
        self.clock.advance(50)
        result = self.store.confirm_release(prepared['current']['play_id'], current_study_id=None)
        self.assertEqual(result['current']['phase'], 'playing')
        self.assertEqual(result['current']['remaining_seconds'], 3550)
        self.assertEqual(result['current']['expires_utc'], 103600)

    def test_crash_after_clear_recovers_pending_release_as_playing(self):
        prepared = self.prepare()
        self.clock.advance(500)
        result = self.make().reconcile(current_study_id=None)
        self.assertEqual(result['current']['play_id'], prepared['current']['play_id'])
        self.assertEqual(result['current']['phase'], 'playing')
        self.assertEqual(result['current']['remaining_seconds'],3100)

    def test_clear_not_done_recovery_requests_only_exact_linked_study(self):
        self.prepare()
        result = self.make().reconcile(current_study_id=STUDY)
        self.assertEqual(result['action'], 'release_linked_study')
        self.assertEqual(result['current']['linked_study_id'], STUDY)
        self.assertEqual(result['current']['phase'], 'pending_release')

    def test_first_prepare_rejects_changed_missing_or_unlinked_current_study(self):
        for linked, actual in ((STUDY,OTHER),(STUDY,None),(None,STUDY)):
            with self.subTest(linked=linked,actual=actual), self.assertRaises(PlayConflict):
                self.prepare(linked=linked,actual=actual)
        self.assertFalse(self.root.exists())

    def test_prepare_without_study_is_explicit_supported_parent_allowance(self):
        result = self.prepare(linked=None,actual=None)
        self.assertIsNone(result['current']['linked_study_id'])
        result = self.store.confirm_release(result['current']['play_id'],current_study_id=None)
        self.assertEqual(result['action'],'wait')

    def test_prepare_exact_retry_before_and_after_release_does_not_reset(self):
        one = self.prepare()
        before = self.bytes()
        self.assertEqual(self.prepare(),one)
        self.assertEqual(self.bytes(),before)
        self.clock.advance(100)
        released = self.store.confirm_release(one['current']['play_id'],current_study_id=None)
        before = self.bytes()
        retry = self.prepare(actual=None)
        self.assertEqual(retry,released)
        self.assertEqual(self.bytes(),before)

    def test_redeemed_retry_cannot_change_duration_or_linked_identity(self):
        self.prepare()
        before = self.bytes()
        for minutes, linked, actual in ((120,STUDY,STUDY),(60,None,None),(60,OTHER,OTHER)):
            with self.subTest(minutes=minutes,linked=linked), self.assertRaises(PlayConflict):
                self.prepare(minutes=minutes,linked=linked,actual=actual)
        self.assertEqual(self.bytes(),before)

    def test_new_allowance_does_not_replace_active_play(self):
        self.playing()
        before = self.bytes()
        with self.assertRaises(PlayConflict):
            self.prepare(revision=2,linked=None,actual=None)
        self.assertEqual(self.bytes(),before)

    def test_old_redeemed_allowance_not_reusable_after_newer_session(self):
        old = self.prepare()
        self.store.cancel(old['current']['play_id'])
        newer = self.prepare(revision=2)
        before = self.bytes()
        with self.assertRaises(PlayConflict):
            self.prepare()
        self.assertEqual(self.bytes(),before)
        self.assertEqual(self.store.status()['current']['play_id'],newer['current']['play_id'])

    def test_past_future_days_and_noncanonical_dates_cannot_grant(self):
        for day in ('2026-11-25','2026-11-27','20261126','2026-11-026',None):
            with self.subTest(day=day), self.assertRaises(PlaySessionError):
                self.store.prepare(day,1,60,STUDY,current_study_id=STUDY)
        self.assertFalse(self.root.exists())

    def test_minutes_and_revisions_strictly_bounded(self):
        for minutes in (0,121,True,1.5,'60'):
            with self.subTest(minutes=minutes), self.assertRaises(PlaySessionError):
                self.prepare(minutes=minutes)
        for revision in (0,-1,2**128,True,'1'):
            with self.subTest(revision=revision), self.assertRaises(PlaySessionError):
                self.prepare(revision=revision)
        first=self.prepare(minutes=1)
        self.assertEqual(first['current']['duration_seconds'],60)

    def test_one_hour_recreated_store_does_not_restart_countdown(self):
        self.playing()
        self.clock.advance(1500)
        before=self.make().reconcile(current_study_id=None)
        self.assertEqual(before['current']['remaining_seconds'],2100)
        self.clock.advance(2100)
        after=self.make().reconcile(current_study_id=None)
        self.assertEqual(after['current']['phase'],'return_requested')
        self.assertEqual(after['current']['remaining_seconds'],0)
        self.assertTrue(after['requires_startup'])

    def test_countdown_uses_monotonic_if_wall_stops_and_no_reset_on_same_lease(self):
        self.playing()
        self.store.reconcile(current_study_id=None)
        self.clock.mono+=75
        result=self.store.reconcile(current_study_id=None)
        self.assertEqual(result['current']['remaining_seconds'],3525)
        self.clock.mono+=25
        self.assertEqual(self.store.reconcile(current_study_id=None)['current']['remaining_seconds'],3500)

    def test_monotonic_elapsed_seconds_are_durable_across_restart(self):
        self.playing()
        self.clock.mono+=120
        self.assertEqual(self.store.reconcile(current_study_id=None)['current']['remaining_seconds'],3480)
        self.assertEqual(self.make().reconcile(current_study_id=None)['current']['remaining_seconds'],3480)

    def test_wall_clock_rollback_after_saved_progress_expires_fail_closed(self):
        self.playing()
        self.clock.advance(60)
        self.store.reconcile(current_study_id=None)
        self.clock.wall-=30
        result=self.make().reconcile(current_study_id=None)
        self.assertEqual(result['current']['phase'],'return_requested')
        self.assertEqual(result['current']['result'],'clock_rollback')
        self.assertEqual(result['current']['remaining_seconds'],0)

    def test_small_wall_clock_adjustment_never_increases_durable_remaining(self):
        self.playing()
        self.clock.advance(60)
        self.store.reconcile(current_study_id=None)
        self.clock.wall-=1
        result=self.make().reconcile(current_study_id=None)
        self.assertEqual(result['current']['remaining_seconds'],3540)

    def test_invalid_clock_never_grants_or_resets(self):
        for bad in (float('nan'),float('inf'),0,True,'now'):
            self.clock.wall=bad
            with self.subTest(value=str(bad)), self.assertRaises(PlaySessionError):
                self.prepare()
        self.assertFalse(self.root.exists())

    def test_monotonic_rollback_expires_fail_closed(self):
        self.playing()
        self.clock.mono-=1
        result=self.store.reconcile(current_study_id=None)
        self.assertEqual(result['current']['result'],'clock_rollback')
        self.assertEqual(result['action'],'restore_study')

    def test_new_study_before_release_is_preserved_and_cancels_play(self):
        self.prepare()
        result=self.store.reconcile(current_study_id=OTHER)
        self.assertEqual(result['current']['phase'],'cancelled')
        self.assertEqual(result['current']['result'],'study_replaced_before_release')
        self.assertEqual(result['action'],'none')
        self.assertFalse(result['requires_startup'])

    def test_parent_study_during_play_cancels_without_clearing_that_study(self):
        self.playing()
        result=self.store.reconcile(current_study_id=OTHER)
        self.assertEqual(result['current']['result'],'study_started_during_play')
        self.assertEqual(result['action'],'none')

    def test_expired_pending_release_never_releases_old_study(self):
        self.prepare()
        self.clock.advance(3600)
        result=self.store.reconcile(current_study_id=STUDY)
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(result['current']['result'],'restrictions_already_present')
        self.assertEqual(result['action'],'none')

    def test_return_request_id_is_stable_across_recovery(self):
        expired=self.expired()
        proposal=expired['current']['return_study_id']
        self.assertRegex(proposal,r'^[0-9a-f]{32}$')
        recovered=self.make().prepare_return(expired['current']['play_id'],current_study_id=None)
        self.assertEqual(recovered['current']['return_study_id'],proposal)
        self.assertEqual(recovered['action'],'restore_study')

    def test_return_confirmation_exact_and_repeat_are_idempotent(self):
        expired=self.expired();play=expired['current']['play_id'];target=expired['current']['return_study_id']
        result=self.store.confirm_return(play,current_study_id=target)
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(result['current']['result'],'return_confirmed')
        self.assertFalse(result['requires_startup'])
        before=self.bytes()
        self.assertEqual(self.store.confirm_return(play,current_study_id=target),result)
        self.assertEqual(self.bytes(),before)

    def test_newer_study_wins_return_race_without_overwrite_request(self):
        expired=self.expired()
        result=self.store.prepare_return(expired['current']['play_id'],current_study_id=OTHER)
        self.assertEqual(result['action'],'none')
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(result['current']['result'],'restrictions_already_present')

    def test_return_confirmation_rejects_empty_identity_and_early_phase(self):
        playing=self.playing();play=playing['current']['play_id']
        with self.assertRaises(PlayConflict):self.store.prepare_return(play,current_study_id=None)
        with self.assertRaises(PlayConflict):self.store.confirm_return(play,current_study_id=OTHER)
        with self.assertRaises(PlaySessionError):self.store.confirm_return(play,current_study_id=None)

    def test_stale_play_identity_never_changes_newer_session(self):
        self.playing();before=self.bytes()
        for call in (lambda:self.store.confirm_release(OTHER,current_study_id=None),
                     lambda:self.store.prepare_return(OTHER,current_study_id=None),
                     lambda:self.store.confirm_return(OTHER,current_study_id=OTHER),
                     lambda:self.store.cancel(OTHER)):
            with self.assertRaises(PlayConflict):call()
        self.assertEqual(self.bytes(),before)

    def test_parent_cancel_all_active_phases_survives_restart(self):
        for phase in ('pending_release','playing','return_requested'):
            with self.subTest(phase=phase):
                local=self.make()
                if local.status()['current'] is not None:
                    old=local.status()['current'];local.cancel(old['play_id'])
                revision=len(local.read()['redeemed'])+1
                result=local.prepare(DAY,revision,60,None,current_study_id=None)
                if phase!='pending_release':result=local.confirm_release(result['current']['play_id'],current_study_id=None)
                if phase=='return_requested':
                    self.clock.advance(3600);result=local.reconcile(current_study_id=None)
                local.cancel(result['current']['play_id'])
                result=self.make().reconcile(current_study_id=None)
                self.assertEqual(result['current']['phase'],'cancelled')
                self.assertEqual(result['current']['result'],'parent_cancelled')
                self.assertFalse(result['requires_startup'])

    def test_parent_cancel_reason_is_fixed_not_arbitrary_log_input(self):
        result=self.prepare();play=result['current']['play_id']
        with self.assertRaises(PlaySessionError):self.store.cancel(play,reason='anything')
        result=self.store.cancel(play,reason='parent_study_started')
        self.assertEqual(result['current']['result'],'parent_study_started')

    def test_corrupt_file_does_not_reset_ledger_or_grant_again(self):
        self.prepare();self.store.path.write_bytes(b'{bad json');before=self.bytes()
        for call in (self.store.read,lambda:self.store.reconcile(current_study_id=None),lambda:self.prepare()):
            with self.assertRaises(PlaySessionError):call()
        self.assertEqual(self.bytes(),before)

    def test_strict_schema_rejects_unknown_keys_and_invalid_values(self):
        self.prepare();valid=self.store.read()
        mutations=[lambda d:d.update(extra=1),lambda d:d.update(version=True),
            lambda d:d['redeemed'].append(d['redeemed'][0]),
            lambda d:d['current'].update(remaining_seconds=True),
            lambda d:d['current'].update(expires_utc=1),
            lambda d:d['current'].update(linked_study_id='invalid'),
            lambda d:d['current'].update(phase='return_requested'),
            lambda d:d['current'].update(phase=[]),
            lambda d:d['current'].update(result={}),
            lambda d:d['current'].update(approval_key=approval_key(DAY,2))]
        for mutate in mutations:
            value=deepcopy(valid);mutate(value);self.write_state(value)
            with self.subTest(value=str(mutate)),self.assertRaises(PlaySessionError):self.store.read()

    def test_invalid_utf8_duplicate_keys_nan_and_size_are_rejected(self):
        self.root.mkdir()
        for raw in (b'\xff',b'{"version":1,"version":1,"redeemed":[],"current":null}',
                    b'{"version":NaN,"redeemed":[],"current":null}',b' '*(module.MAX_BYTES+1)):
            self.store.path.write_bytes(raw)
            with self.subTest(size=len(raw)),self.assertRaises(PlaySessionError):self.store.read()

    def test_failed_atomic_replace_preserves_previous_state_and_cleans_temp(self):
        first=self.prepare();before=self.bytes()
        with mock.patch.object(module.os,'replace',side_effect=OSError('fixture disk error')):
            with self.assertRaises(OSError):self.store.cancel(first['current']['play_id'])
        self.assertEqual(self.bytes(),before)
        self.assertEqual(list(self.root.glob('.play-*.tmp')),[])

    def test_full_ledger_preserves_all_old_approval_tombstones(self):
        state={'version':1,'redeemed':[approval_key(DAY,n) for n in range(1,module.MAX_APPROVALS+1)],'current':None}
        self.write_state(state);before=self.bytes()
        with self.assertRaises(PlaySessionError):self.prepare(revision=module.MAX_APPROVALS+1)
        self.assertEqual(self.bytes(),before)

    def test_hardlinked_state_is_rejected(self):
        self.prepare()
        alias=self.root/'alias.json'
        os.link(self.store.path,alias)
        with self.assertRaises(ValueError):self.store.read()

    def test_parallel_identical_approval_has_one_durable_deadline(self):
        def grant(_):return self.make().prepare(DAY,1,60,STUDY,current_study_id=STUDY)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(grant,range(8)))
        self.assertEqual(len({r['current']['play_id'] for r in results}),1)
        self.assertTrue(all(r['current']['expires_utc']==103600 for r in results))
        self.assertEqual(len(self.store.read()['redeemed']),1)

    def test_returned_values_are_detached_from_durable_state(self):
        result=self.prepare();result['current']['phase']='playing'
        state=self.store.read();state['redeemed'].clear()
        self.assertEqual(self.store.read()['current']['phase'],'pending_release')
        self.assertEqual(len(self.store.read()['redeemed']),1)


if __name__=='__main__':unittest.main(verbosity=2)
