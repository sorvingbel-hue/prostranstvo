"""Real temporary stores across approval/restart boundaries; no GUI/network/OS registry."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
import itertools
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from board_approval import BoardApproval
from daily_plan import DailyPlan, DailyPlanConflict, DailyPlanError
from family_rewards import RewardBook, RewardConflict
from play_session import PlaySession, PlayConflict, PlaySessionError
from study_resume import StudyResume, RUN_KEY


class Clock:
    def __init__(self):self.wall=1000.0;self.mono=100.0
    def advance(self,seconds):self.wall+=seconds;self.mono+=seconds


class Registry:
    HKEY_CURRENT_USER='fixture-user'
    REG_SZ=1
    KEY_SET_VALUE=2
    KEY_QUERY_VALUE=1
    def __init__(self):self.values={};self.writes=[]
    def OpenKey(self,hive,key,*args):
        if hive!=self.HKEY_CURRENT_USER or key!=RUN_KEY:raise AssertionError('Unexpected registry scope')
        return nullcontext('fixture-key')
    CreateKey=OpenKey
    def QueryValueEx(self,key,name):
        if name not in self.values:raise FileNotFoundError(name)
        return self.values[name]
    def SetValueEx(self,key,name,reserved,kind,value):
        self.writes.append(('set',name));self.values[name]=(value,kind)
    def DeleteValue(self,key,name):
        self.writes.append(('delete',name));del self.values[name]


class BoardApprovalIntegrationTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='board-approval-',dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.day=date(2026,11,26)
        self.clock,self.registry=Clock(),Registry()
        self.ids=itertools.count(1)
        self.ensure=mock.Mock()
        self.service=self.make_service()
        self.request='a'*32

    def make_service(self):
        study=StudyResume(self.root/'study',clock=lambda:self.clock.wall,registry=self.registry)
        # This is the only substituted StudyResume operation: generating a
        # startup command must never inspect a real installed runtime in tests.
        study._startup_command=lambda:'"fixture-owned-runtime" --resume'
        return BoardApproval(
            plans=DailyPlan(self.root/'plan',today=lambda:self.day),study=study,
            play=PlaySession(self.root/'play',clock=lambda:self.clock.wall,
                monotonic=lambda:self.clock.mono,today=lambda:self.day,
                nonce=lambda:format(next(self.ids),'032x')),
            rewards=RewardBook(self.root/'rewards',clock=lambda:self.clock.wall),
            root=self.root/'approval',ensure_running=self.ensure)

    def submit(self, *, minutes=60, agreement=False, goal=False, linked=True):
        service=self.service
        study=service.study.activate(0) if linked else None
        plan=service.plans.publish(['Первое синтетическое дело','Второе синтетическое дело'],minutes,
            expected_revision=None,task_points=[20,10])
        goal_id=None
        if agreement:
            if goal:
                state=service.rewards.snapshot()
                proposed=service.rewards.propose_goal('Синтетическая цель',100,'баллы',expected_revision=state['revision'])
                goal_id=proposed['goal']['goal_id']
                service.rewards.confirm_goal(goal_id,expected_revision=proposed['state_revision'])
            items=[{'reward_id':'play','kind':'game_minutes','amount':minutes,'label':'Минуты игры'}]
            if goal_id:
                items.append({'reward_id':'goal','kind':'goal','amount':20,'label':'Синтетическая цель','goal_id':goal_id})
            service.rewards.prepare_plan(plan['date'],plan['contract_revision'],
                [{'task_id':'plan','criteria':'Выполнены оба заранее согласованных синтетических дела.','rewards':items}],
                expected_revision=service.rewards.snapshot()['revision'])
        for index in range(2):
            plan=service.plans.set_task(index,'done',expected_revision=plan['revision'])
        plan=service.plans.submit(expected_revision=plan['revision'],study_session_id=study['session_id'] if study else None)
        self.args={'plan_date':plan['date'],'revision':plan['revision'],
                   'study_id':study['session_id'] if study else None,'request_id':self.request}
        return plan,study,goal_id

    def approve(self,service=None,**overrides):
        return (service or self.service).approve(**{**self.args,**overrides})

    def records(self):
        return {name:(self.root/name).read_bytes() if (self.root/name).exists() else None
                for name in ('plan/today.json','study/active-session.json','play/session.json','rewards/rewards.json')}

    def test_idle_runtime_pump_is_pure(self):
        self.assertEqual(self.service.pump(),{'current':None,'action':'none','requires_startup':False})
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(self.registry.values,{})
        self.ensure.assert_not_called()

    def test_parent_approval_releases_linked_study_and_starts_exact_hour(self):
        plan,study,_=self.submit()
        result=self.approve()
        self.assertEqual(result['status'],'approved')
        self.assertIsNone(self.service.study.read())
        current=self.service.play.status()['current']
        self.assertEqual((current['linked_study_id'],current['phase']),(study['session_id'],'playing'))
        self.assertEqual((current['duration_seconds'],current['expires_utc']),(3600,4600))
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertTrue(self.service.study.startup_enabled())
        self.ensure.assert_called_once_with(self.service.study)

    def test_fresh_runtime_after_hour_restores_one_indefinite_study(self):
        self.submit();self.approve();self.clock.advance(3601)
        fresh=self.make_service()
        result=fresh.pump()
        lease=fresh.study.read()
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(result['current']['return_study_id'],lease['session_id'])
        self.assertIsNone(lease['expires_utc'])
        self.assertEqual(fresh.study.remaining_seconds(),math.inf)
        before=(self.root/'study/active-session.json').read_bytes()
        self.assertEqual(fresh.pump()['current']['phase'],'completed')
        self.assertEqual((self.root/'study/active-session.json').read_bytes(),before)

    def test_restart_during_play_preserves_absolute_deadline_and_minutes(self):
        self.submit();self.approve();self.clock.advance(900)
        fresh=self.make_service()
        status=fresh.pump()
        self.assertEqual(status['current']['phase'],'playing')
        self.assertEqual(status['current']['expires_utc'],4600)
        self.assertEqual(status['current']['remaining_seconds'],2700)
        self.assertIsNone(fresh.study.read())

    def test_same_approval_retry_cannot_reset_hour_or_credit_xp_twice(self):
        self.submit();self.approve();self.clock.advance(500)
        before_plan=(self.root/'plan/today.json').read_bytes()
        before=self.service.play.status()['current']
        fresh=self.make_service();result=self.approve(fresh)
        current=result['play']['current']
        self.assertEqual((current['play_id'],current['expires_utc']),(before['play_id'],before['expires_utc']))
        self.assertEqual(current['remaining_seconds'],3100)
        self.assertEqual(fresh.plans.progress()['total_points'],30)
        self.assertEqual((self.root/'plan/today.json').read_bytes(),before_plan)

    def test_newer_study_before_parent_approval_is_not_released(self):
        self.submit();new=self.service.study.activate(25)
        before=self.records()
        result=self.approve()
        self.assertEqual(result['status'],'approved_without_play')
        self.assertIn('newer_study_preserved',result['meaning'])
        self.assertEqual(result['game_minutes'],0)
        self.assertEqual(self.service.plans.read()['status'],'approved')
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertEqual(self.records()['study/active-session.json'],before['study/active-session.json'])
        self.assertIsNone(self.service.play.status()['current'])
        self.assertEqual(self.service.study.read(),new)
        after=self.records()
        repeated=self.approve(self.make_service())
        self.assertEqual(repeated['status'],'approved_without_play')
        self.assertEqual(self.records(),after)
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertEqual(self.registry.writes,[])
        self.ensure.assert_not_called()

    def test_newer_study_racing_exact_clear_survives_and_cancels_play(self):
        self.submit();real_clear=self.service.study.clear;new=[]
        def race_clear(identity):
            new.append(self.service.study.activate(25))
            return real_clear(identity)
        with mock.patch.object(self.service.study,'clear',side_effect=race_clear):result=self.approve()
        self.assertEqual(self.service.study.read(),new[0])
        self.assertEqual(result['play']['current']['phase'],'cancelled')
        self.assertEqual(result['play']['current']['result'],'study_replaced_before_release')

    def test_new_study_started_during_play_is_preserved(self):
        self.submit();self.approve()
        new=self.service.study.activate(45)
        result=self.service.pump()
        self.assertEqual(result['current']['result'],'study_started_during_play')
        self.assertEqual(self.service.study.read(),new)

    def test_return_cas_cannot_overwrite_parent_study_inserted_at_expiry(self):
        self.submit();self.approve();self.clock.advance(3600)
        real=self.service.study.activate_if_empty;new=[]
        def race(minutes,*,session_id):
            new.append(self.service.study.activate(45))
            return real(minutes,session_id=session_id)
        with mock.patch.object(self.service.study,'activate_if_empty',side_effect=race):result=self.service.pump()
        self.assertEqual(self.service.study.read(),new[0])
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(result['current']['result'],'restrictions_already_present')

    def test_startup_registration_failure_keeps_restrictions_and_retry_recovers(self):
        _,study,_=self.submit()
        with mock.patch.object(self.service.study,'enable_startup',side_effect=OSError('fixture registry denied')):
            with self.assertRaises(OSError):self.approve()
        self.assertEqual(self.service.study.read(),study)
        self.assertEqual(self.service.play.status()['current']['phase'],'pending_release')
        self.ensure.assert_not_called()
        result=self.approve(self.make_service())
        self.assertEqual(result['play']['current']['phase'],'playing')
        self.assertEqual(self.service.plans.progress()['total_points'],30)

    def test_failed_worker_launch_cannot_release_study(self):
        _,study,_=self.submit()
        self.ensure.side_effect=OSError('fixture runtime start failed')
        with self.assertRaises(OSError):self.approve()
        self.assertEqual(self.service.study.read(),study)
        self.assertEqual(self.service.play.status()['current']['phase'],'pending_release')
        self.ensure.side_effect=None
        result=self.approve(self.make_service())
        self.assertEqual(result['play']['current']['phase'],'playing')

    def test_crash_immediately_after_plan_review_recovers_with_same_approval_id(self):
        _,study,_=self.submit()
        real=self.service.plans.review
        def interrupted(**kwargs):
            real(**kwargs)
            raise OSError('fixture crash after durable plan review')
        with mock.patch.object(self.service.plans,'review',side_effect=interrupted):
            with self.assertRaises(OSError):self.approve()
        approved=self.service.plans.read()
        self.assertEqual(approved['approval']['request_id'],self.request)
        self.assertEqual(approved['approval']['submitted_revision'],self.args['revision'])
        self.assertEqual(self.service.study.read(),study)
        self.assertIsNone(self.service.play.status()['current'])
        self.assertEqual(self.approve(self.make_service())['play']['current']['phase'],'playing')
        self.assertEqual(self.service.plans.progress()['total_points'],30)

    def test_study_read_failure_after_durable_approval_never_grants_or_releases(self):
        _,study,_=self.submit()
        before=(self.root/'study/active-session.json').read_bytes()
        with mock.patch.object(self.service.study,'read',side_effect=OSError('fixture Study storage unavailable')):
            with self.assertRaises(OSError):self.approve()
        approved=self.service.plans.read()
        self.assertEqual(approved['status'],'approved')
        self.assertEqual(approved['approval']['request_id'],self.request)
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertIsNone(self.service.play.status()['current'])
        self.assertEqual(self.service.study.read(),study)
        self.assertEqual((self.root/'study/active-session.json').read_bytes(),before)
        self.assertEqual(self.registry.writes,[])
        self.ensure.assert_not_called()
        recovered=self.approve(self.make_service())
        self.assertEqual(recovered['play']['current']['phase'],'playing')
        self.assertEqual(self.service.plans.progress()['total_points'],30)

    def test_crash_after_reward_credit_before_play_never_double_credits(self):
        self.submit(agreement=True,goal=True)
        with mock.patch.object(self.service.play,'prepare',side_effect=OSError('fixture crash before play record')):
            with self.assertRaises(OSError):self.approve(quality_percent=50,comment='Нужно закончить второе дело.')
        book_before=self.service.rewards.snapshot()
        self.assertEqual(book_before['goals'][0]['balance'],10)
        result=self.approve(self.make_service(),quality_percent=50,comment='Нужно закончить второе дело.')
        self.assertEqual(result['play']['current']['duration_seconds'],1800)
        self.assertEqual(self.service.rewards.snapshot(),book_before)
        self.assertEqual(self.service.plans.progress()['total_points'],15)

    def test_crash_after_linked_clear_recovers_pending_handoff_without_new_hour(self):
        self.submit()
        with mock.patch.object(self.service.play,'confirm_release',side_effect=OSError('fixture interrupted confirmation')):
            with self.assertRaises(OSError):self.approve()
        self.assertIsNone(self.service.study.read())
        before=self.service.play.status()['current']
        self.assertEqual(before['phase'],'pending_release')
        self.clock.advance(100)
        result=self.make_service().pump()['current']
        self.assertEqual((result['phase'],result['expires_utc']),('playing',before['expires_utc']))
        self.assertEqual(result['remaining_seconds'],3500)

    def test_crash_after_return_lease_is_durable_does_not_create_another_lease(self):
        self.submit();self.approve();self.clock.advance(3600)
        with mock.patch.object(self.service.play,'confirm_return',side_effect=OSError('fixture return confirmation crash')):
            with self.assertRaises(OSError):self.service.pump()
        lease=self.service.study.read()
        self.assertIsNotNone(lease)
        self.assertEqual(self.service.play.status()['current']['phase'],'return_requested')
        result=self.make_service().pump()
        self.assertEqual(result['current']['phase'],'completed')
        self.assertEqual(self.service.study.read(),lease)

    def test_parent_cancellation_persists_across_restart_and_original_approval_replay(self):
        self.submit();self.approve()
        cancelled=self.service.cancel()['current']
        self.clock.advance(4000)
        fresh=self.make_service()
        self.assertEqual(fresh.pump()['current']['phase'],'cancelled')
        self.assertIsNone(fresh.study.read())
        replay=self.approve(fresh)['play']['current']
        self.assertEqual((replay['play_id'],replay['phase'],replay['expires_utc']),
                         (cancelled['play_id'],'cancelled',cancelled['expires_utc']))
        self.assertEqual(fresh.plans.progress()['total_points'],30)

    def test_quality_twenty_percent_scales_minutes_xp_and_goal_once(self):
        plan,_,_=self.submit(agreement=True,goal=True)
        result=self.approve(quality_percent=20,comment='Нужно закончить второе дело.')
        self.assertEqual(result['game_minutes'],12)
        self.assertEqual(result['play']['current']['duration_seconds'],720)
        self.assertEqual(self.service.plans.progress()['total_points'],6)
        self.assertEqual(self.service.rewards.snapshot()['goals'][0]['balance'],4)
        self.assertNotEqual(plan['revision'],plan['contract_revision'])
        before=self.service.rewards.snapshot()
        self.approve(quality_percent=20,comment='Нужно закончить второе дело.')
        self.assertEqual(self.service.rewards.snapshot(),before)

    def test_zero_quality_or_rounding_to_zero_never_releases_study(self):
        for minutes,quality in [(60,0),(5,1)]:
            with self.subTest(minutes=minutes,quality=quality):
                # Each subcase is a fresh isolated family store.
                self.root=Path(self.temporary.name)/f'case-{minutes}-{quality}'
                self.service=self.make_service()
                _,study,_=self.submit(minutes=minutes,agreement=True)
                result=self.approve(quality_percent=quality,comment='Нужно закончить дело.')
                self.assertEqual(result['status'],'approved_without_play')
                self.assertEqual(self.service.study.read(),study)
                self.assertIsNone(self.service.play.status()['current'])
        self.assertEqual(self.registry.writes,[])
        self.ensure.assert_not_called()

    def test_less_than_full_quality_needs_agreement_and_explanation(self):
        self.submit();before=self.records()
        with self.assertRaises(DailyPlanError):self.approve(quality_percent=50,comment='Есть комментарий.')
        self.assertEqual(self.records(),before)
        self.ensure.assert_not_called()

    def test_history_approval_only_credits_past_xp_not_new_game_or_new_study(self):
        self.day-=timedelta(days=1)
        old,_,_=self.submit()
        self.day+=timedelta(days=1)
        new=self.service.study.activate(45)
        result=self.approve()
        self.assertEqual(result['status'],'approved_without_play')
        self.assertEqual(self.service.plans.read(plan_date=old['date'])['status'],'approved')
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertEqual(self.service.study.read(),new)
        self.assertIsNone(self.service.play.status()['current'])
        self.ensure.assert_not_called()
        self.assertEqual(self.registry.writes,[])

    def test_history_review_during_today_play_cannot_replace_or_extend_today_allowance(self):
        self.day-=timedelta(days=1)
        self.submit()
        old_args={**self.args,'request_id':'b'*32}
        self.day+=timedelta(days=1)
        self.submit(minutes=45)
        self.approve()
        current_plan=self.service.plans.read()
        current_play=self.service.play.status()['current']
        result=self.service.approve(**old_args)
        self.assertEqual(result['status'],'approved_without_play')
        self.assertEqual(self.service.play.status()['current'],current_play)
        self.assertEqual(self.service.plans.read(),current_plan)
        self.assertEqual(self.service.plans.progress()['total_points'],60)
        self.assertIsNone(self.service.study.read())

    def test_foreign_startup_entry_collision_does_not_release_study(self):
        _,study,_=self.submit()
        name=self.service.study.startup_name
        self.registry.values[name]=('foreign-owned-command',self.registry.REG_SZ)
        with self.assertRaises(ValueError):self.approve()
        self.assertEqual(self.service.study.read(),study)
        self.assertEqual(self.registry.values[name],('foreign-owned-command',self.registry.REG_SZ))
        self.assertEqual(self.registry.writes,[])
        self.ensure.assert_not_called()

    def test_concurrent_exact_approval_retries_share_one_hour_and_one_xp_award(self):
        self.submit()
        first,second=self.make_service(),self.make_service()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda service:self.approve(service),[first,second]))
        ids={result['play']['current']['play_id'] for result in results}
        deadlines={result['play']['current']['expires_utc'] for result in results}
        self.assertEqual(len(ids),1)
        self.assertEqual(deadlines,{4600})
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertIsNone(self.service.study.read())

    def test_original_approval_cannot_remove_indefinite_return_study_after_expiry(self):
        self.submit();self.approve();self.clock.advance(3600)
        self.service.pump()
        returned=self.service.study.read()
        records=self.records()
        writes=list(self.registry.writes)
        self.ensure.reset_mock()
        result=self.approve(self.make_service())
        self.assertEqual(result['status'],'approved_without_play')
        self.assertIn('newer_study_preserved',result['meaning'])
        self.assertEqual(result['game_minutes'],0)
        self.assertEqual(self.records(),records)
        self.assertEqual(self.service.study.read(),returned)
        self.assertEqual(self.service.plans.progress()['total_points'],30)
        self.assertEqual(self.registry.writes,writes)
        self.ensure.assert_not_called()

    def test_improved_reward_review_has_delta_but_does_not_extend_timer_implicitly(self):
        plan,_,_=self.submit(agreement=True,goal=True)
        self.approve(quality_percent=20,comment='Нужно закончить второе дело.')
        play_before=self.service.play.status()['current']
        result=self.service.rewards.evaluate(plan['date'],plan['contract_revision'],{'plan':100},
            review_id='b'*32,expected_revision=self.service.rewards.snapshot()['revision'])
        self.assertEqual(result['game_minutes_awarded'],48)
        self.assertEqual(self.service.rewards.snapshot()['goals'][0]['balance'],20)
        self.assertEqual(self.service.play.status()['current'],play_before)
        # This is a separate parent bonus decision, not an automatic extension
        # by a later quality-book receipt or duplicate original approval.
        self.approve(quality_percent=20,comment='Нужно закончить второе дело.')
        self.assertEqual(self.service.play.status()['current']['expires_utc'],play_before['expires_utc'])

    def test_stale_revision_or_study_binding_cannot_release_or_credit(self):
        self.submit();before=self.records()
        for changes in [{'revision':1},{'study_id':None},{'study_id':'b'*32}]:
            with self.subTest(changes=changes),self.assertRaises((DailyPlanError,DailyPlanConflict)):
                self.approve(**changes)
            self.assertEqual(self.records(),before)
        self.ensure.assert_not_called()

    def test_explicit_invalid_request_ids_are_not_treated_as_absent(self):
        self.submit();before=self.records()
        for invalid in ('',False,0,[],{},'bad','A'*32):
            with self.subTest(invalid=invalid),self.assertRaises(DailyPlanError):self.approve(request_id=invalid)
            self.assertEqual(self.records(),before)
        self.ensure.assert_not_called()

    def test_invalid_types_quality_and_comments_do_not_change_records(self):
        self.submit();before=self.records()
        for changes in [{'revision':True},{'revision':0},{'plan_date':None},{'plan_date':'not-a-day'},
                        {'quality_percent':True},{'quality_percent':101},{'quality_percent':-1},
                        {'study_id':123},{'study_id':'A'*32},{'comment':'bad\ncomment'}]:
            with self.subTest(changes=changes),self.assertRaises((DailyPlanError,DailyPlanConflict)):
                self.approve(**changes)
            self.assertEqual(self.records(),before)

    def test_corrupted_play_ledger_never_authorizes_restriction_release(self):
        _,study,_=self.submit()
        self.service.play.root.mkdir()
        self.service.play.path.write_bytes(b'{damaged')
        with self.assertRaises(PlaySessionError):self.approve()
        self.assertEqual(self.service.study.read(),study)
        self.assertEqual(self.service.play.path.read_bytes(),b'{damaged')
        self.ensure.assert_not_called()
        self.assertEqual(self.registry.writes,[])

    def test_backward_clock_cannot_create_an_extra_game_hour(self):
        self.submit();self.approve();self.clock.advance(100);self.service.pump()
        self.clock.wall-=200
        view=self.make_service().pump()
        self.assertEqual(view['current']['phase'],'completed')
        self.assertEqual(view['current']['remaining_seconds'],0)
        self.assertIsNone(self.service.study.read()['expires_utc'])


if __name__=='__main__':unittest.main()
