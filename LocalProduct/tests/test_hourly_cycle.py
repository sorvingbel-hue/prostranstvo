"""Multiple real temporary stores, fake clocks/registry and no OS/GUI/network."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, timedelta
import itertools
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from board_approval import BoardApproval
from daily_plan import DailyPlan
from family_rewards import RewardBook
from hourly_break import HourlyBreakStore,HourlyBreakError,HourlyBreakConflict
from hourly_cycle import HourlyCycleControl
from play_session import PlaySession,approval_key
from study_resume import StudyResume,RUN_KEY


class Clock:
    def __init__(self):self.wall=10000.0;self.mono=100.0
    def advance(self,seconds):self.wall+=seconds;self.mono+=seconds


class Registry:
    HKEY_CURRENT_USER=1
    REG_SZ=1
    KEY_QUERY_VALUE=1
    KEY_SET_VALUE=2
    def __init__(self):self.values={};self.writes=[]
    def OpenKey(self,hive,key,*args):
        if hive!=self.HKEY_CURRENT_USER or key!=RUN_KEY:raise AssertionError('Outside owned fixture registry key')
        return nullcontext(self)
    CreateKey=OpenKey
    def QueryValueEx(self,key,name):
        if name not in self.values:raise FileNotFoundError(name)
        return self.values[name]
    def SetValueEx(self,key,name,reserved,kind,value):
        self.writes.append(('set',name));self.values[name]=(value,kind)
    def DeleteValue(self,key,name):self.writes.append(('delete',name));del self.values[name]


class HourlyCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='hourly-integration-')
        self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)
        self.clock,self.registry=Clock(),Registry();self.day=date(2026,11,26)
        self.nonces=itertools.count(1);self.ensure=mock.Mock();self.service=self.make_service()
        self.control=HourlyCycleControl(self.service);self.args=None

    def nonce(self):return f'{next(self.nonces):032x}'

    def make_service(self):
        study=StudyResume(self.root/'study',clock=lambda:self.clock.wall,registry=self.registry)
        study._startup_command=lambda:'"owned synthetic controller" --resume'
        return BoardApproval(plans=DailyPlan(self.root/'plan',today=lambda:self.day),study=study,
            play=PlaySession(self.root/'play',clock=lambda:self.clock.wall,monotonic=lambda:self.clock.mono,
                             today=lambda:self.day,nonce=self.nonce),
            rewards=RewardBook(self.root/'rewards',clock=lambda:self.clock.wall),root=self.root/'approval',
            ensure_running=self.ensure,breaks=HourlyBreakStore(self.root/'cycle',clock=lambda:self.clock.wall,
                monotonic=lambda:self.clock.mono,today=lambda:self.day,nonce=self.nonce))

    def enable(self):
        view=self.control.status()
        return self.control.configure(enabled=True,task='Навести порядок на согласованной с родителем полке.',
                                      expected_revision=view['revision'])

    def make_submitted(self,minutes=60):
        study=self.service.study.read()
        if study is None:study=self.service.study.activate(0)
        plan=self.service.plans.publish(['Синтетическое полезное дело'],minutes,expected_revision=None)
        plan=self.service.plans.set_task(0,'done',expected_revision=plan['revision'])
        plan=self.service.plans.submit(expected_revision=plan['revision'],study_session_id=study['session_id'])
        self.args={'plan_date':plan['date'],'revision':plan['revision'],'study_id':study['session_id'],
                   'request_id':self.nonce()}
        return plan

    def first_hour(self,minutes=60):
        self.enable();self.make_submitted(minutes)
        result=self.service.approve(**self.args)
        self.assertEqual(result['status'],'approved')
        self.assertEqual(result['play']['current']['phase'],'playing')
        self.assertIsNone(self.service.study.read())
        return deepcopy(result['play']['current'])

    def break_started(self):
        first=self.first_hour();self.clock.advance(3600)
        view=self.service.pump();cycle=self.control.status()
        self.assertEqual(view['current']['phase'],'completed')
        self.assertEqual(cycle['phase'],'waiting');self.assertEqual(cycle['remaining_break_seconds'],600)
        self.assertEqual(self.service.study.read()['duration_seconds'],0)
        return first,cycle

    def break_submitted(self):
        first,cycle=self.break_started();self.clock.advance(600);self.service.pump()
        cycle=self.control.status()
        submitted=self.control.request_done(break_id=cycle['break_id'],expected_revision=cycle['revision'])
        self.assertEqual(submitted['phase'],'submitted')
        return first,submitted

    def prepared_parent_grant(self):
        first,submitted=self.break_submitted()
        with self.service.locked():
            lease=self.service._study()
            view=self.service.breaks.approve(submitted['break_id'],submitted['revision'],lease['session_id'])
        return first,view

    def test_two_complete_hours_each_get_a_new_ten_minute_parent_gate(self):
        first,cycle=self.break_started();returned=deepcopy(self.service.study.read())
        play_bytes=self.service.play.path.read_bytes()
        with self.assertRaises(HourlyBreakError):self.control.request_done(break_id=cycle['break_id'],expected_revision=cycle['revision'])
        with self.assertRaises(HourlyBreakConflict):self.control.approve_break(break_id=cycle['break_id'],expected_revision=cycle['revision'])
        self.assertEqual(self.service.study.read(),returned);self.assertEqual(self.service.play.path.read_bytes(),play_bytes)
        self.clock.advance(600);self.service.pump();ready=self.control.status()
        self.assertTrue(ready['can_request_done']);self.assertFalse(ready['can_approve'])
        self.assertEqual(self.service.study.read(),returned)
        submitted=self.control.request_done(break_id=ready['break_id'],expected_revision=ready['revision'])
        confirmed=self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        self.assertEqual(confirmed['phase'],'consumed')
        second=deepcopy(self.service.play.status()['current'])
        self.assertEqual(second['phase'],'playing');self.assertEqual(second['duration_seconds'],3600)
        self.assertNotEqual(second['play_id'],first['play_id']);self.assertIsNone(self.service.study.read())
        self.clock.advance(3600);self.service.pump();new=self.control.status()
        self.assertEqual(new['phase'],'waiting');self.assertEqual(new['remaining_break_seconds'],600)
        self.assertNotEqual(new['break_id'],cycle['break_id'])
        self.assertEqual(new['current']['source_play_id'],second['play_id'])
        self.assertEqual(self.service.study.read()['session_id'],new['current']['study_id'])

    def test_elapsed_time_alone_does_not_start_second_hour(self):
        _,cycle=self.break_started();returned=self.service.study.read()
        self.clock.advance(3600);self.service.pump()
        self.assertEqual(self.control.status()['phase'],'waiting')
        self.assertTrue(self.control.status()['can_request_done']);self.assertIsNone(self.control.status()['grant'])
        self.assertEqual(self.service.study.read(),returned)

    def test_child_request_alone_does_not_start_second_hour(self):
        _,submitted=self.break_submitted();returned=self.service.study.read();self.service.pump()
        self.assertEqual(self.control.status()['phase'],'submitted');self.assertIsNone(self.control.status()['grant'])
        self.assertEqual(self.service.study.read(),returned)

    def test_duplicate_parent_approval_cannot_extend_next_hour(self):
        _,submitted=self.break_submitted()
        self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        first=deepcopy(self.service.play.status()['current']);self.clock.advance(100)
        with self.assertRaises(HourlyBreakConflict):self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        self.service.pump();now=self.service.play.status()['current']
        self.assertEqual(now['play_id'],first['play_id']);self.assertEqual(now['expires_utc'],first['expires_utc'])
        self.assertEqual(self.service.plans.progress()['total_points'],30)

    def test_daily_board_reapproval_during_break_cannot_bypass_minimum(self):
        _,cycle=self.break_started();returned=self.service.study.read();old_play=deepcopy(self.service.play.status()['current'])
        result=self.service.approve(**self.args)
        self.assertEqual(result['status'],'approved_without_play');self.assertEqual(result['game_minutes'],0)
        self.assertEqual(result['meaning'],'hourly_break_requires_parent_review')
        self.assertEqual(self.service.study.read(),returned);self.assertEqual(self.service.play.status()['current'],old_play)
        self.assertEqual(self.service.plans.progress()['total_points'],30)

    def test_new_daily_plan_approval_still_cannot_bypass_pending_break(self):
        self.break_started();returned=self.service.study.read();old=deepcopy(self.service.play.status()['current'])
        self.day+=timedelta(days=1);self.clock.advance(86400)
        self.make_submitted()
        result=self.service.approve(**self.args)
        self.assertEqual(result['status'],'approved_without_play');self.assertEqual(self.service.plans.progress()['total_points'],60)
        self.assertEqual(self.service.study.read(),returned);self.assertEqual(self.service.play.status()['current'],old)

    def test_cancel_disable_reenable_does_not_revive_old_completed_hour(self):
        _,cycle=self.break_started();source=self.service.play.status()['current']['play_id']
        self.service.cancel();cancelled=self.control.status()
        self.assertFalse(cancelled['enabled']);self.assertEqual(cancelled['phase'],'cancelled')
        self.assertIsNone(self.service.study.read());self.assertIn(source,self.service.breaks.read()['seen_play_ids'])
        self.control.configure(enabled=True,task='Новое полезное дело.',expected_revision=cancelled['revision'])
        self.service.pump();self.assertIsNone(self.control.status()['grant']);self.assertEqual(self.control.status()['phase'],'cancelled')
        self.assertIsNone(self.service.study.read())

    def test_disable_settings_consumes_current_source_before_new_enable(self):
        first=self.first_hour();view=self.control.status()
        disabled=self.control.configure(enabled=False,task='Useful',expected_revision=view['revision'])
        self.assertIn(first['play_id'],self.service.breaks.read()['seen_play_ids'])
        self.clock.advance(3600);self.service.pump()
        self.control.configure(enabled=True,task='Useful',expected_revision=disabled['revision'])
        self.service.pump();self.assertIsNone(self.control.status()['current'])

    def test_foreign_study_before_parent_review_is_preserved(self):
        _,submitted=self.break_submitted();newer=self.service.study.activate(0)
        play=deepcopy(self.service.play.status()['current'])
        with self.assertRaises(HourlyBreakConflict):self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        self.assertEqual(self.service.study.read(),newer);self.assertEqual(self.service.play.status()['current'],play)
        self.assertEqual(self.control.status()['phase'],'blocked')

    def test_foreign_study_after_durable_approval_prevents_preparation(self):
        _,approved=self.prepared_parent_grant();newer=self.service.study.activate(0)
        before=deepcopy(self.service.play.status()['current']);self.service.pump()
        self.assertEqual(self.service.study.read(),newer);self.assertEqual(self.service.play.status()['current'],before)
        self.assertEqual(self.control.status()['phase'],'blocked');self.assertIsNone(self.control.status()['grant'])

    def test_crash_after_next_play_prepare_recovers_without_new_deadline(self):
        _,approved=self.prepared_parent_grant();grant=approved['grant'];actual=self.service._study()
        prepared=self.service.play.prepare(grant['date'],grant['revision'],60,grant['linked_study_id'],current_study_id=actual['session_id'])
        next_id=prepared['current']['play_id'];deadline=prepared['current']['expires_utc']
        self.clock.advance(120);reopened=self.make_service();view=reopened.pump()
        self.assertEqual(view['current']['play_id'],next_id);self.assertEqual(view['current']['expires_utc'],deadline)
        self.assertEqual(view['current']['phase'],'playing');self.assertIsNone(reopened.study.read())
        self.assertEqual(reopened.breaks.status()['phase'],'consumed')
        self.assertEqual(reopened.breaks.status()['current']['next_play_id'],next_id)

    def test_crash_after_exact_study_clear_before_cycle_confirmation_recovers(self):
        _,approved=self.prepared_parent_grant();grant=approved['grant'];actual=self.service._study()
        prepared=self.service.play.prepare(grant['date'],grant['revision'],60,grant['linked_study_id'],current_study_id=actual['session_id'])
        self.service.study.clear(actual['session_id'])
        reopened=self.make_service();view=reopened.pump()
        self.assertEqual(view['current']['play_id'],prepared['current']['play_id'])
        self.assertEqual(view['current']['expires_utc'],prepared['current']['expires_utc'])
        self.assertEqual(view['current']['phase'],'playing');self.assertEqual(reopened.breaks.status()['phase'],'consumed')

    def test_approved_but_unprepared_grant_cannot_mint_time_next_day(self):
        _,approved=self.prepared_parent_grant();returned=self.service.study.read();play=deepcopy(self.service.play.status()['current'])
        self.day+=timedelta(days=1);self.clock.advance(86400)
        reopened=self.make_service()
        with self.assertRaises(HourlyBreakConflict):reopened.pump()
        self.assertEqual(reopened.study.read(),returned);self.assertEqual(reopened.play.status()['current'],play)
        self.assertEqual(reopened.breaks.status()['phase'],'cancelled')

    def test_already_prepared_grant_can_recover_across_midnight_without_reset(self):
        _,approved=self.prepared_parent_grant();grant=approved['grant'];actual=self.service._study()
        prepared=self.service.play.prepare(grant['date'],grant['revision'],60,grant['linked_study_id'],current_study_id=actual['session_id'])
        self.day+=timedelta(days=1);self.clock.advance(120)
        reopened=self.make_service();view=reopened.pump()
        self.assertEqual(view['current']['play_id'],prepared['current']['play_id'])
        self.assertEqual(view['current']['expires_utc'],prepared['current']['expires_utc'])
        self.assertEqual(reopened.breaks.status()['phase'],'consumed')

    def test_corrupt_break_ledger_never_erases_valid_restrictions(self):
        self.break_started();returned=self.service.study.read();play=self.service.play.path.read_bytes()
        broken=b'{damaged cycle';self.service.breaks.path.write_bytes(broken)
        with self.assertRaises(HourlyBreakError):self.make_service().pump()
        self.assertEqual(self.service.study.read(),returned);self.assertEqual(self.service.play.path.read_bytes(),play)
        self.assertEqual(self.service.breaks.path.read_bytes(),broken)

    def test_disabled_policy_preserves_existing_two_hour_allowance_behavior(self):
        self.make_submitted(120);result=self.service.approve(**self.args)
        self.assertEqual(result['play']['current']['duration_seconds'],7200)
        self.clock.advance(7200);self.service.pump()
        self.assertEqual(self.service.study.read()['duration_seconds'],0)
        self.assertFalse(self.control.status()['enabled']);self.assertIsNone(self.control.status()['current'])

    def test_enabled_policy_caps_first_allowance_at_sixty_minutes(self):
        first=self.first_hour(minutes=120)
        self.assertEqual(first['duration_seconds'],3600)
        self.assertEqual(self.service.plans.read()['game_minutes'],120)

    def test_enabling_policy_does_not_restart_or_extend_running_hour(self):
        self.make_submitted();self.service.approve(**self.args)
        first=deepcopy(self.service.play.status()['current']);self.clock.advance(100)
        self.enable();self.service.pump();now=self.service.play.status()['current']
        self.assertEqual(now['play_id'],first['play_id']);self.assertEqual(now['expires_utc'],first['expires_utc'])
        self.assertIsNone(self.control.status()['current'])

    def test_startup_failure_before_next_prepare_keeps_returned_study(self):
        _,submitted=self.break_submitted();returned=self.service.study.read();old=deepcopy(self.service.play.status()['current'])
        with mock.patch.object(self.service.study,'enable_startup',side_effect=ValueError('fixture occupied')):
            with self.assertRaises(ValueError):self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        self.assertEqual(self.service.study.read(),returned);self.assertEqual(self.service.play.status()['current'],old)
        self.assertEqual(self.control.status()['phase'],'approved')
        self.service.pump();self.assertEqual(self.control.status()['phase'],'consumed')
        self.assertIsNone(self.service.study.read())

    def test_failed_keeper_launch_after_prepare_is_recoverable_once(self):
        _,submitted=self.break_submitted();self.ensure.side_effect=RuntimeError('fixture launch')
        with self.assertRaises(RuntimeError):self.control.approve_break(break_id=submitted['break_id'],expected_revision=submitted['revision'])
        prepared=deepcopy(self.service.play.status()['current'])
        self.assertEqual(prepared['phase'],'pending_release');self.assertIsNotNone(self.service.study.read())
        self.clock.advance(60);self.ensure.side_effect=None;self.make_service().pump()
        after=self.service.play.status()['current'];self.assertEqual(after['expires_utc'],prepared['expires_utc'])
        self.assertEqual(after['play_id'],prepared['play_id']);self.assertEqual(after['phase'],'playing')

    def test_cancel_after_durable_approval_revokes_it_and_preserves_foreign_study(self):
        _,approved=self.prepared_parent_grant();foreign=self.service.study.activate(0)
        self.service.cancel();self.assertEqual(self.service.study.read(),foreign)
        cancelled=self.control.status();self.assertEqual(cancelled['phase'],'cancelled');self.assertFalse(cancelled['enabled'])
        self.assertIsNone(cancelled['grant'])

    def test_status_and_configuration_do_not_claim_parent_identity_or_start_game(self):
        view=self.control.status();self.assertIsNone(view['grant']);self.assertFalse(view['enabled'])
        self.enable();self.assertIsNone(self.service.play.status()['current'])
        self.assertFalse(self.ensure.called);self.assertEqual(self.registry.writes,[])

    def test_parent_cancel_before_first_expiry_cannot_be_revived_by_reenabling(self):
        first=self.first_hour();self.clock.advance(100)
        cancelled=self.service.cancel();cycle=self.control.status()
        self.assertEqual(cancelled['current']['phase'],'cancelled')
        self.assertFalse(cycle['enabled']);self.assertIsNone(cycle['current'])
        self.assertIn(first['play_id'],self.service.breaks.read()['seen_play_ids'])
        self.enable();self.clock.advance(4000);self.make_service().pump()
        self.assertIsNone(self.control.status()['current']);self.assertIsNone(self.control.status()['grant'])
        self.assertIsNone(self.service.study.read())

    def test_clock_rollback_return_is_not_a_completed_hour(self):
        first=self.first_hour();self.clock.advance(100);self.service.pump()
        self.clock.wall-=50;self.clock.mono+=1;view=self.service.pump()
        self.assertEqual(view['current']['phase'],'completed')
        self.assertLess(view['current']['max_seen_utc'],first['expires_utc'])
        returned=self.service.study.read();self.assertIsNotNone(returned)
        self.assertIsNone(self.control.status()['current'])
        self.clock.advance(600);self.make_service().pump()
        self.assertEqual(self.service.study.read(),returned)
        self.assertIsNone(self.control.status()['grant'])

    def test_shorter_reward_does_not_masquerade_as_full_play_hour(self):
        first=self.first_hour(minutes=30);self.assertEqual(first['duration_seconds'],1800)
        self.clock.advance(1800);view=self.service.pump()
        self.assertEqual(view['current']['phase'],'completed')
        returned=self.service.study.read();self.assertIsNotNone(returned)
        self.assertIsNone(self.control.status()['current'])
        self.clock.advance(600);self.service.pump()
        self.assertEqual(self.service.study.read(),returned)
        self.assertIsNone(self.control.status()['grant'])

    def assert_parent_study_survives_invisible_expiry(self):
        """The replacement finishes between ticks: no current-ID evidence remains."""
        previous=self.service.study.read();self.ensure.reset_mock()
        self.service.start_parent_study(1)
        replacement=self.service.study.read()
        self.assertIsNotNone(replacement);self.assertEqual(replacement['duration_seconds'],60)
        self.assertEqual(replacement['expires_utc'],self.clock.wall+60)
        if previous is not None:self.assertNotEqual(replacement['session_id'],previous['session_id'])
        self.assertFalse(self.ensure.called)
        cancelled=self.control.status()
        self.assertFalse(cancelled['enabled']);self.assertIsNone(cancelled['grant'])
        if cancelled['current'] is not None:
            self.assertEqual(cancelled['phase'],'cancelled')
            self.assertEqual(cancelled['current']['result'],'study_replaced')
        old_play=deepcopy(self.service.play.status()['current'])
        self.assertIn(old_play['phase'],('cancelled','completed'))
        # Deliberately do not pump until after expiry and exact-ID cleanup.
        self.clock.advance(61)
        self.assertEqual(self.service.study.remaining_seconds(),0)
        self.assertTrue(self.service.study.clear(replacement['session_id']))
        reopened=self.make_service();reopened.pump()
        self.assertIsNone(reopened.study.read())
        self.assertEqual(reopened.play.status()['current'],old_play)
        # Re-enabling future cycles must not resurrect the unseen old source.
        cycle=reopened.breaks.status()
        HourlyCycleControl(reopened).configure(enabled=True,task='Новое согласованное дело.',
                                              expected_revision=cycle['revision'])
        reopened.pump();self.clock.advance(7200);reopened.pump()
        self.assertIsNone(reopened.study.read());self.assertIsNone(reopened.breaks.status()['grant'])
        self.assertEqual(reopened.play.status()['current'],old_play)

    def test_parent_study_cancels_play_even_if_replacement_expires_before_next_tick(self):
        self.first_hour();self.clock.advance(100)
        self.assert_parent_study_survives_invisible_expiry()

    def test_parent_study_cancels_waiting_break_before_invisible_expiry(self):
        self.break_started();self.assert_parent_study_survives_invisible_expiry()

    def test_parent_study_cancels_submitted_break_before_invisible_expiry(self):
        self.break_submitted();self.assert_parent_study_survives_invisible_expiry()

    def test_parent_study_revokes_durable_approved_grant_before_invisible_expiry(self):
        self.prepared_parent_grant();self.assert_parent_study_survives_invisible_expiry()

    def test_parent_study_cancels_prepared_next_hour_before_invisible_expiry(self):
        _,approved=self.prepared_parent_grant();grant=approved['grant'];actual=self.service._study()
        self.service.play.prepare(grant['date'],grant['revision'],60,grant['linked_study_id'],
                                  current_study_id=actual['session_id'])
        self.assertEqual(self.service.play.status()['current']['phase'],'pending_release')
        self.assert_parent_study_survives_invisible_expiry()

    def test_parent_study_invalid_minutes_do_not_mutate_any_store_or_registry(self):
        self.break_submitted();self.ensure.reset_mock()
        before={str(path.relative_to(self.root)):path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}
        registry=deepcopy(self.registry.values);writes=list(self.registry.writes)
        for value in (True,False,-1,241,1.5,'60',None):
            with self.subTest(minutes=value):
                with self.assertRaises(ValueError):self.service.start_parent_study(value)
                self.assertEqual({str(path.relative_to(self.root)):path.read_bytes()
                    for path in self.root.rglob('*') if path.is_file()},before)
                self.assertEqual(self.registry.values,registry);self.assertEqual(self.registry.writes,writes)
                self.assertFalse(self.ensure.called)

    def test_parent_study_invalid_clock_does_not_mutate_existing_play_or_grant(self):
        self.prepared_parent_grant();self.ensure.reset_mock()
        before={str(path.relative_to(self.root)):path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}
        registry=deepcopy(self.registry.values);writes=list(self.registry.writes)
        for value in (float('nan'),float('inf'),float('-inf'),0,-1,True,'now',None):
            with self.subTest(clock=value):
                self.clock.wall=value
                with self.assertRaises(ValueError):self.service.start_parent_study(60)
                self.assertEqual({str(path.relative_to(self.root)):path.read_bytes()
                    for path in self.root.rglob('*') if path.is_file()},before)
                self.assertEqual(self.registry.values,registry);self.assertEqual(self.registry.writes,writes)
                self.assertFalse(self.ensure.called)

    def test_parent_study_accepts_indefinite_and_maximum_without_launching_worker(self):
        self.ensure.reset_mock()
        self.service.start_parent_study(0);indefinite=self.service.study.read()
        self.assertEqual(indefinite['duration_seconds'],0);self.assertIsNone(indefinite['expires_utc'])
        self.service.start_parent_study(240);maximum=self.service.study.read()
        self.assertEqual(maximum['duration_seconds'],14400)
        self.assertEqual(maximum['expires_utc'],self.clock.wall+14400)
        self.assertNotEqual(maximum['session_id'],indefinite['session_id'])
        self.assertFalse(self.ensure.called)

    def test_parent_study_startup_failure_preserves_durable_approval_and_restrictions(self):
        self.prepared_parent_grant();self.ensure.reset_mock()
        before={str(path.relative_to(self.root)):path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}
        registry=deepcopy(self.registry.values);writes=list(self.registry.writes)
        with mock.patch.object(self.service.study,'enable_startup',side_effect=ValueError('fixture startup collision')):
            with self.assertRaises(ValueError):self.service.start_parent_study(60)
        self.assertEqual({str(path.relative_to(self.root)):path.read_bytes()
            for path in self.root.rglob('*') if path.is_file()},before)
        self.assertEqual(self.registry.values,registry);self.assertEqual(self.registry.writes,writes)
        self.assertFalse(self.ensure.called)


if __name__=='__main__':unittest.main(verbosity=2)
