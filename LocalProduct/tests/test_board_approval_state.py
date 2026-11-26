"""Board approval/CAS contracts on owned temporary data, without GUI or registry."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from daily_plan import DailyPlan, DailyPlanConflict, DailyPlanError
from study_resume import StudyResume

REQUEST = 'a' * 32
OTHER = 'b' * 32


class RegistryForbidden:
    def __getattr__(self, name):
        raise AssertionError('Registry must not be used in these fixtures')


class BoardApprovalStateTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'
        base.mkdir(exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix='board-approval-', dir=base)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.day = date(2026, 11, 26)
        self.store = DailyPlan(self.root / 'board', today=lambda:self.day)

    def publish(self, store=None, repeat=False):
        return (store or self.store).publish(['Решить задачу', 'Убрать стол'], 60,
            expected_revision=None, task_points=[20,10], repeat_daily=repeat)

    def submit(self, store=None, repeat=False):
        store = store or self.store
        plan = self.publish(store,repeat)
        for index in range(2):
            plan = store.set_task(index,'done',expected_revision=plan['revision'])
        return store.submit(expected_revision=plan['revision'],study_session_id=OTHER)

    def approve(self, submitted, quality=100, feedback='Спасибо!'):
        return self.store.review(approved=True,feedback=feedback,
            expected_revision=submitted['revision'],approval_id=REQUEST,quality_percent=quality)

    def raw(self):
        return self.store.path.read_bytes()

    def mutate(self, callback):
        value = json.loads(self.raw().decode('utf-8'))
        callback(value)
        self.store.path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

    def test_contract_stable_across_child_marks_submission_and_parent_return(self):
        plan = self.publish()
        contract = plan['contract_revision']
        revision = plan['revision']
        for index,status in ((0,'help'),(0,'done'),(1,'done')):
            plan = self.store.set_task(index,status,expected_revision=plan['revision'])
            self.assertEqual(plan['contract_revision'],contract)
            self.assertNotEqual(plan['revision'],revision)
            revision = plan['revision']
        plan = self.store.submit(expected_revision=revision)
        self.assertEqual(plan['contract_revision'],contract)
        plan = self.store.review(approved=False,feedback='Проверь ответ.',task_index=0,
                                 expected_revision=plan['revision'])
        self.assertEqual(plan['contract_revision'],contract)
        plan = self.store.set_task(0,'done',expected_revision=plan['revision'])
        submitted = self.store.submit(expected_revision=plan['revision'])
        approved = self.approve(submitted)
        self.assertEqual(approved['contract_revision'],contract)
        self.assertEqual(approved['approval']['submitted_revision'],submitted['revision'])

    def test_parent_republish_creates_new_contract_even_with_same_text(self):
        plan = self.publish()
        new = self.store.publish(['Решить задачу','Убрать стол'],60,expected_revision=plan['revision'])
        self.assertNotEqual(new['contract_revision'],plan['contract_revision'])

    def test_daily_materialization_has_new_contract_and_retains_previous_history(self):
        previous = self.publish(repeat=True)
        self.day += timedelta(days=1)
        today = self.store.ensure_today()
        self.assertNotEqual(today['contract_revision'],previous['contract_revision'])
        self.assertEqual(self.store.history()[0]['contract_revision'],previous['contract_revision'])
        self.assertEqual([x['status'] for x in today['tasks']],['pending','pending'])

    def test_legacy_read_remains_pure_and_first_edit_pins_original_contract(self):
        plan = self.publish()
        original_revision = plan['revision']
        self.mutate(lambda state:state['plan'].pop('contract_revision'))
        before = self.raw()
        self.assertNotIn('contract_revision',self.store.read())
        self.assertEqual(self.raw(),before)
        edited = self.store.set_task(0,'done',expected_revision=original_revision)
        self.assertEqual(edited['contract_revision'],original_revision)
        again = self.store.set_task(1,'done',expected_revision=edited['revision'])
        self.assertEqual(again['contract_revision'],original_revision)

    def test_approval_marker_persists_submitted_revision_and_normalized_feedback(self):
        submitted = self.submit()
        approved = self.approve(submitted,85,'  Спасибо!  ')
        self.assertEqual(approved['feedback'],'Спасибо!')
        self.assertEqual(approved['approval'],{'request_id':REQUEST,
            'submitted_revision':submitted['revision'],'quality_percent':85})
        self.assertEqual(self.store.read(),approved)

    def test_identical_approval_retry_is_byte_pure_and_does_not_double_xp(self):
        submitted = self.submit()
        approved = self.approve(submitted,50)
        before = self.raw()
        stat = self.store.path.stat()
        for _ in range(4):
            self.assertEqual(self.approve(submitted,50,' Спасибо! '),approved)
        self.assertEqual(self.store.progress()['total_points'],15)
        self.assertEqual(self.raw(),before)
        self.assertEqual(self.store.path.stat().st_mtime_ns,stat.st_mtime_ns)

    def test_same_id_changed_payload_is_conflict_and_preserves_original_decision(self):
        submitted = self.submit()
        self.approve(submitted,50)
        before = self.raw()
        for changes in ({'quality_percent':85},{'feedback':'Другая оценка'},
                        {'expected_revision':submitted['revision']+1},{'approval_id':OTHER}):
            arguments={'approved':True,'feedback':'Спасибо!','quality_percent':50,
                       'expected_revision':submitted['revision'],'approval_id':REQUEST,**changes}
            with self.subTest(changes=changes),self.assertRaises(DailyPlanConflict):
                self.store.review(**arguments)
        self.assertEqual(self.raw(),before)
        self.assertEqual(self.store.progress()['total_points'],15)

    def test_duplicate_approval_does_not_bypass_feedback_validation(self):
        submitted = self.submit()
        self.approve(submitted)
        before = self.raw()
        for bad in (None,{},True,'line\nbreak','x'*241):
            with self.subTest(feedback=bad),self.assertRaises(DailyPlanError):
                self.approve(submitted,feedback=bad)
        self.assertEqual(self.raw(),before)

    def test_duplicate_approval_requires_integer_revision_even_if_numeric_equal(self):
        submitted = self.submit()
        self.approve(submitted)
        # Revision 1 is schema-valid. bool/float equality must not substitute
        # for strict request typing on the idempotent fast path.
        self.mutate(lambda state:state['plan']['approval'].update(submitted_revision=1))
        before = self.raw()
        for bad in (True,1.0):
            with self.subTest(revision=bad),self.assertRaises(DailyPlanError):
                self.store.review(approved=True,feedback='Спасибо!',expected_revision=bad,
                                  approval_id=REQUEST)
        self.assertEqual(self.raw(),before)

    def test_quality_boundaries_scale_credited_points_and_leave_no_provisional_points(self):
        for quality in (0,1,20,50,85,100):
            with self.subTest(quality=quality):
                store=DailyPlan(self.root/('quality-'+str(quality)),today=lambda:self.day)
                submitted=self.submit(store)
                approved=store.review(approved=True,feedback='',expected_revision=submitted['revision'],
                                      quality_percent=quality,approval_id=REQUEST)
                progress=store.progress()
                self.assertEqual(approved['approval']['quality_percent'],quality)
                self.assertEqual(progress['total_points'],30*quality//100)
                self.assertEqual(progress['approved_today_points'],30*quality//100)
                self.assertEqual(progress['provisional_points'],0)

    def test_scaled_quality_requires_explicit_idempotent_approval_identity(self):
        submitted=self.submit();before=self.raw()
        with self.assertRaises(DailyPlanError):
            self.store.review(approved=True,feedback='',expected_revision=submitted['revision'],
                              quality_percent=50)
        self.assertEqual(self.raw(),before)

    def test_default_quality_legacy_approval_api_remains_supported(self):
        submitted=self.submit()
        approved=self.store.review(approved=True,feedback='',expected_revision=submitted['revision'])
        self.assertEqual(approved['status'],'approved')
        self.assertEqual(self.store.progress()['total_points'],30)
        self.assertEqual(self.store.progress()['approved_today_points'],30)

    def test_invalid_quality_and_approval_id_rejected_before_mutation(self):
        submitted=self.submit();before=self.raw()
        for bad in (-1,101,True,50.0,'50',None):
            with self.subTest(quality=bad),self.assertRaises(DailyPlanError):
                self.approve(submitted,bad)
        for bad in ('x'*32,'A'*32,'a'*31,True,{}):
            with self.subTest(approval_id=bad),self.assertRaises(DailyPlanError):
                self.store.review(approved=True,feedback='',expected_revision=submitted['revision'],
                                  approval_id=bad)
        self.assertEqual(self.raw(),before)

    def test_return_decision_cannot_carry_approval_request_identity(self):
        submitted=self.submit();before=self.raw()
        with self.assertRaises(DailyPlanError):
            self.store.review(approved=False,feedback='Поправь.',task_index=0,
                expected_revision=submitted['revision'],approval_id=REQUEST)
        self.assertEqual(self.raw(),before)

    def test_approval_fields_corruption_is_rejected_not_silently_migrated(self):
        submitted=self.submit();self.approve(submitted)
        original=json.loads(self.raw().decode('utf-8'))
        bad_markers=({'request_id':REQUEST},
            {'request_id':OTHER,'submitted_revision':1,'quality_percent':True},
            {'request_id':'x'*32,'submitted_revision':1,'quality_percent':100},
            {'request_id':REQUEST,'submitted_revision':0,'quality_percent':100},
            {'request_id':REQUEST,'submitted_revision':1,'quality_percent':101},
            {'request_id':REQUEST,'submitted_revision':1,'quality_percent':100,'extra':1})
        for marker in bad_markers:
            state=deepcopy(original);state['plan']['approval']=marker
            self.store.path.write_text(json.dumps(state),encoding='utf-8');before=self.raw()
            with self.subTest(marker=marker),self.assertRaises(DailyPlanError):self.store.read()
            self.assertEqual(self.raw(),before)

    def test_contract_marker_corruption_and_approval_on_open_plan_are_rejected(self):
        submitted=self.submit();self.approve(submitted)
        original=json.loads(self.raw().decode('utf-8'))
        for value in (0,-1,True,'1',2**128):
            state=deepcopy(original);state['plan']['contract_revision']=value
            self.store.path.write_text(json.dumps(state),encoding='utf-8')
            with self.subTest(contract=value),self.assertRaises(DailyPlanError):self.store.read()
        state=deepcopy(original);state['plan']['status']='open'
        self.store.path.write_text(json.dumps(state),encoding='utf-8')
        with self.assertRaises(DailyPlanError):self.store.read()

    def test_archived_approval_retry_does_not_credit_today_or_mutate_history(self):
        submitted=self.submit(repeat=True);approved=self.approve(submitted,50)
        old_day=self.day.isoformat();self.day+=timedelta(days=1);today=self.store.ensure_today()
        before=self.raw()
        retry=self.store.review(approved=True,feedback='Спасибо!',expected_revision=submitted['revision'],
            approval_id=REQUEST,quality_percent=50,plan_date=old_day)
        self.assertEqual(retry,approved)
        self.assertEqual(self.raw(),before)
        self.assertEqual(self.store.read(),today)
        self.assertEqual(self.store.progress()['approved_today_points'],0)
        self.assertEqual(self.store.progress()['total_points'],15)

    def test_stale_approval_cannot_apply_to_new_days_list(self):
        submitted=self.submit(repeat=True);self.approve(submitted)
        self.day+=timedelta(days=1);self.store.ensure_today();before=self.raw()
        with self.assertRaises(DailyPlanConflict):self.approve(submitted)
        self.assertEqual(self.raw(),before)


class ReturnStudyCasTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).parent/'tests-work';base.mkdir(exist_ok=True)
        fixture=tempfile.TemporaryDirectory(prefix='board-return-cas-',dir=base)
        self.addCleanup(fixture.cleanup);self.root=Path(fixture.name)/'study'
        self.now=1000.0
        self.resume=self.make()

    def make(self):
        return StudyResume(self.root,clock=lambda:self.now,registry=RegistryForbidden())

    def test_empty_slot_creates_stable_untimed_return_lease_without_registry(self):
        lease=self.resume.activate_if_empty(minutes=0,session_id=REQUEST)
        self.assertEqual(lease['session_id'],REQUEST)
        self.assertEqual(lease['duration_seconds'],0)
        self.assertIsNone(lease['expires_utc'])
        self.assertEqual(self.make().read(),lease)

    def test_same_id_retry_preserves_original_deadline_and_file_bytes(self):
        first=self.resume.activate_if_empty(30,session_id=REQUEST)
        raw=self.resume.path.read_bytes();stat=self.resume.path.stat();self.now+=600
        self.assertEqual(self.make().activate_if_empty(30,session_id=REQUEST),first)
        self.assertEqual(self.resume.path.read_bytes(),raw)
        self.assertEqual(self.resume.path.stat().st_mtime_ns,stat.st_mtime_ns)
        self.assertEqual(self.resume.remaining_seconds(),1200)

    def test_foreign_active_session_cannot_be_overwritten_by_timer_return(self):
        parent=self.resume.activate(120);before=self.resume.path.read_bytes()
        self.assertIsNone(self.resume.activate_if_empty(0,session_id=REQUEST))
        self.assertEqual(self.resume.path.read_bytes(),before)
        self.assertEqual(self.resume.read(),parent)

    def test_foreign_expired_session_is_not_implicitly_cleared_by_cas(self):
        parent=self.resume.activate(1);self.now+=61;before=self.resume.path.read_bytes()
        self.assertIsNone(self.resume.activate_if_empty(0,session_id=REQUEST))
        self.assertEqual(self.resume.path.read_bytes(),before)
        self.assertEqual(self.resume.read()['session_id'],parent['session_id'])

    def test_invalid_identity_and_duration_do_not_create_store(self):
        for value in (None,'x'*32,'a'*31,True):
            with self.subTest(identity=value),self.assertRaises(ValueError):
                self.resume.activate_if_empty(0,session_id=value)
        for minutes in (-1,241,True,1.5,'1'):
            with self.subTest(minutes=minutes),self.assertRaises(ValueError):
                self.resume.activate_if_empty(minutes,session_id=REQUEST)
        self.assertFalse(self.root.exists())

    def test_invalid_clock_never_creates_even_an_untimed_return_lease(self):
        bad_values=(float('nan'),float('inf'),True,0,-1,'now',None)
        for index,bad in enumerate(bad_values):
            for minutes in (0,1):
                root=self.root.parent/('clock-'+str(index)+'-'+str(minutes))
                resume=StudyResume(root,clock=lambda value=bad:value,registry=RegistryForbidden())
                with self.subTest(clock=bad,minutes=minutes),self.assertRaises(ValueError):
                    resume.activate_if_empty(minutes,session_id=REQUEST)
                self.assertFalse(root.exists())

    def test_corrupt_existing_lease_is_not_replaced(self):
        self.root.mkdir();self.resume.path.write_bytes(b'{broken');before=self.resume.path.read_bytes()
        with self.assertRaises(ValueError):self.resume.activate_if_empty(0,session_id=REQUEST)
        self.assertEqual(self.resume.path.read_bytes(),before)

    def test_parallel_different_ids_have_only_one_winner(self):
        identifiers=[f'{value:032x}' for value in range(1,9)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda identifier:self.make().activate_if_empty(0,session_id=identifier),identifiers))
        winners=[value for value in values if value is not None]
        self.assertEqual(len(winners),1)
        self.assertEqual(self.resume.read(),winners[0])

    def test_parallel_same_id_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda _:self.make().activate_if_empty(0,session_id=REQUEST),range(8)))
        self.assertTrue(all(value==values[0] for value in values))
        self.assertEqual(self.resume.read(),values[0])


if __name__=='__main__':unittest.main(verbosity=2)
