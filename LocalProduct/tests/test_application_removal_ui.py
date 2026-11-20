"""Withdrawn real Tk + owned in-memory provider; no registry, app launch or removal."""
from copy import deepcopy
import gc
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import types
import unittest
import weakref
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import application_removal_ui as ui


class FakeManager:
    def __init__(self):
        self.calls = []
        self.hold = None
        self.failure = None
        self.entered = threading.Event()
        self.items = [dict(id='allowed-one', name='Synthetic application', publisher='Fixture publisher',
                           install_path='C:/synthetic/application', source='synthetic', action='msi_uninstall',
                           reported_bytes=123456, status='available', reason=''),
                      dict(id='allowed-two', name='Another fixture', publisher='Fixture publisher',
                           install_path='', source='synthetic', action='windows_settings',
                           reported_bytes=None, status='available', reason='')]
        for status in ('protected','blocked_by_policy','unsupported'):
            self.items.append(dict(id=status,name='Fixture '+status,publisher='Fixture publisher',
                                   install_path='',source='synthetic',action='windows_settings',
                                   reported_bytes=None,status=status,reason='Preserved fixture restriction'))
        self.outcome='requested'

    def call(self, operation, *arguments):
        self.calls.append((operation, threading.get_ident(), *arguments));self.entered.set()
        if self.hold and not self.hold.wait(3):raise TimeoutError('Owned provider gate')
        if self.failure==operation:raise RuntimeError('private command or secret must not reach UI')

    def catalog(self):
        self.call('catalog');return dict(items=deepcopy(self.items),warnings=['Synthetic inventory only'],collected_utc='2026-11-19T22:00:00Z')

    def prepare(self, ident):
        self.call('prepare',ident);item=next(x for x in self.items if x['id']==ident)
        return dict(plan_id='plan-'+ident,id=ident,name=item['name'],action=item['action'],
                    target='synthetic-target-'+ident,created_utc='2026-11-19T22:00:00Z',
                    expires_utc='2026-11-19T22:15:00Z',reported_bytes=item['reported_bytes'])

    def request(self, plan_id, approved=False):
        self.call('request',plan_id,approved)
        return dict(plan_id=plan_id,id=plan_id.removeprefix('plan-'),state=self.outcome,
                    detail='Synthetic receipt',before_free={'C:':100},after_free={'C:':100})

    def verify(self, plan_id):
        self.call('verify',plan_id)
        return dict(plan_id=plan_id,id=plan_id.removeprefix('plan-'),state=self.outcome,
                    detail='Synthetic observation',before_free={'C:':100},after_free={'C:':100})


class ApplicationRemovalPanelTests(unittest.TestCase):
    def setUp(self):
        # Tk Python cycles must be collected on their owning thread, before any
        # fixture worker can trigger automatic collection in another thread.
        gc.collect()
        self.root=tk.Tk();self.root.withdraw()
        self.manager=FakeManager();self.guard=mock.Mock()
        self.guard.configured.return_value=True;self.guard.verify.return_value=True
        self.panel=ui.ApplicationRemovalPanel(self.root,manager=self.manager,parent_guard=self.guard)
        self.panel.pack(fill='both',expand=True);self.root.update()

    def tearDown(self):
        panel,root=self.panel,self.root
        try:
            if self.manager.hold:self.manager.hold.set()
            if not panel._closed:self.pump(lambda:not self.panel.busy)
        finally:
            # Cleanup is mandatory even if a pump assertion has already failed.
            try:
                panel.close()
                root.destroy()
            finally:
                self.panel=self.root=None
                del panel,root
                gc.collect()

    def pump(self, condition):
        deadline=time.monotonic()+3
        while not condition() and time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        self.assertTrue(condition())

    def calls(self, operation):return [c for c in self.manager.calls if c[0]==operation]

    def refresh(self):
        self.panel.refresh();self.pump(lambda:not self.panel.busy)

    def select(self, ident):
        iid=next(iid for iid,item in self.panel._items.items() if item['id']==ident)
        self.panel.table.selection_set(iid);self.root.update()

    def prepared(self, ident='allowed-one'):
        self.refresh();self.select(ident);self.panel.prepare();self.pump(lambda:not self.panel.busy)
        self.assertIsNotNone(self.panel._plan)

    def request(self):
        with mock.patch.object(ui.messagebox,'askyesno',return_value=True), \
             mock.patch('cat_ai_ui.simpledialog.askstring',return_value='246810'):
            self.panel.request()
        self.pump(lambda:not self.panel.busy)

    def test_constructor_is_inert_including_default_factory(self):
        self.assertEqual(self.manager.calls,[]);self.assertIsNone(self.panel._poll_id)
        self.assertTrue(self.panel.prepare_button.instate(['disabled']))
        self.assertTrue(self.panel.request_button.instate(['disabled']))
        self.assertTrue(self.panel.verify_button.instate(['disabled']))
        self.panel.prepare();self.panel.request();self.panel.verify()
        self.assertEqual(self.manager.calls,[])
        factory=mock.Mock(side_effect=AssertionError('Constructor performed backend IO'))
        with mock.patch.dict(sys.modules,{'application_removal':types.SimpleNamespace(ApplicationRemovalManager=factory)}):
            another=ui.ApplicationRemovalPanel(self.root,parent_guard=self.guard)
            self.root.update();another.close();another.destroy()
        factory.assert_not_called();self.assertEqual(self.root.state(),'withdrawn')

    def test_refresh_only_catalog_worker_and_displays_all_statuses(self):
        self.refresh()
        self.assertEqual(len(self.calls('catalog')),1)
        self.assertNotEqual(self.calls('catalog')[0][1],threading.get_ident())
        self.assertEqual(len(self.panel.table.get_children()),5)
        self.assertEqual(self.calls('request'),[])
        self.assertIn('Synthetic inventory only',self.panel.details.get('1.0','end'))

    def test_protected_blocked_and_unsupported_rows_cannot_prepare_or_request(self):
        self.refresh()
        for status in ('protected','blocked_by_policy','unsupported'):
            self.select(status)
            self.assertEqual(self.panel.table.selection(),())
            self.assertTrue(self.panel.prepare_button.instate(['disabled']))
            self.assertTrue(self.panel.request_button.instate(['disabled']))
            self.assertIn(ui.STATUSES[status],self.panel.notice.get())
            self.panel.prepare();self.panel.request()
        self.assertEqual(self.calls('prepare'),[]);self.assertEqual(self.calls('request'),[])

    def test_prepare_captures_exact_id_and_plan_without_opening_anything(self):
        self.prepared()
        self.assertEqual(self.calls('prepare')[0][2:],('allowed-one',))
        self.assertNotEqual(self.calls('prepare')[0][1],threading.get_ident())
        text=self.panel.details.get('1.0','end')
        for value in ('plan-allowed-one','allowed-one','synthetic-target-allowed-one','освобождение не обещается'):
            self.assertIn(value,text)
        self.assertEqual(self.calls('request'),[])

    def test_cancel_confirmation_never_requests_pin_or_dispatch(self):
        self.prepared()
        with mock.patch.object(ui.messagebox,'askyesno',return_value=False), \
             mock.patch.object(ui,'require_parent_pin') as pin:
            self.panel.request()
        pin.assert_not_called();self.assertEqual(self.calls('request'),[])
        self.assertFalse(self.panel.busy)

    def test_missing_wrong_or_cancelled_pin_never_dispatches(self):
        self.prepared()
        for configured,pin,verified in ((False,'246810',True),(True,'wrong',False),(True,None,True)):
            self.guard.configured.return_value=configured;self.guard.verify.return_value=verified
            with mock.patch.object(ui.messagebox,'askyesno',return_value=True), \
                 mock.patch('cat_ai_ui.simpledialog.askstring',return_value=pin):
                self.panel.request()
            self.assertEqual(self.calls('request'),[]);self.assertFalse(self.panel.busy)

    def test_exact_confirmation_and_pin_then_worker_request_is_not_removed(self):
        self.prepared();pin_threads=[]
        self.guard.verify.side_effect=lambda pin:pin_threads.append(threading.get_ident()) or True
        with mock.patch.object(ui.messagebox,'askyesno',return_value=True) as confirm, \
             mock.patch('cat_ai_ui.simpledialog.askstring',return_value='246810'):
            self.panel.request()
        self.pump(lambda:not self.panel.busy)
        call,=self.calls('request');self.assertEqual(call[2:],('plan-allowed-one',True))
        self.assertNotEqual(call[1],threading.get_ident());self.assertEqual(pin_threads,[threading.get_ident()])
        self.assertIn('synthetic-target-allowed-one',confirm.call_args.args[1])
        self.assertIn('plan-allowed-one',confirm.call_args.args[1]);self.assertIn('allowed-one',confirm.call_args.args[1])
        self.assertIn('ещё не подтверждено',self.panel.notice.get())
        self.assertNotIn('246810',str(self.manager.calls));self.assertIsNone(self.panel._plan)

    def test_selection_or_refresh_invalidates_plan(self):
        self.prepared();self.select('allowed-two');self.assertIsNone(self.panel._plan)
        self.panel.request();self.assertEqual(self.calls('request'),[])
        self.select('allowed-one');self.panel.prepare();self.pump(lambda:not self.panel.busy)
        self.panel.refresh();self.assertIsNone(self.panel._plan)
        self.pump(lambda:not self.panel.busy);self.assertEqual(self.calls('request'),[])

    def test_modal_selection_race_rejects_captured_plan(self):
        self.prepared()
        def change(*args,**kwargs):
            self.panel.refresh();self.panel.prepare();self.select('allowed-two');return True
        with mock.patch.object(ui.messagebox,'askyesno',side_effect=change), \
             mock.patch.object(ui,'require_parent_pin'):
            self.panel.request()
        self.assertEqual(self.calls('request'),[]);self.assertIsNone(self.panel._plan)
        self.assertEqual(len(self.calls('catalog')),1)

    def test_close_during_confirmation_never_requests_pin_or_dispatch(self):
        self.prepared()
        def close(*args,**kwargs):self.panel.close();return True
        with mock.patch.object(ui.messagebox,'askyesno',side_effect=close), \
             mock.patch.object(ui,'require_parent_pin') as pin:
            self.panel.request()
        pin.assert_not_called();self.assertEqual(self.calls('request'),[])

    def test_destroy_during_pin_never_reads_closed_widgets_or_dispatches(self):
        self.prepared()
        def close(*args,**kwargs):
            self.panel.close();self.panel.destroy()
            self.panel._selected=mock.Mock(side_effect=AssertionError('Closed widget queried'))
        with mock.patch.object(ui.messagebox,'askyesno',return_value=True), \
             mock.patch.object(ui,'require_parent_pin',side_effect=close):
            self.panel.request()
        self.panel._selected.assert_not_called();self.assertEqual(self.calls('request'),[])

    def test_busy_work_is_responsive_and_prevents_duplicates(self):
        self.manager.hold=threading.Event();self.panel.refresh()
        self.assertTrue(self.manager.entered.wait(1));tick=[]
        self.root.after(0,lambda:tick.append(True));self.root.update();self.assertEqual(tick,[True])
        self.panel.refresh();self.panel.prepare();self.panel.request();self.panel.verify()
        self.assertEqual(len(self.calls('catalog')),1)
        self.manager.hold.set();self.pump(lambda:not self.panel.busy)

    def test_verify_requires_receipt_and_never_requests_uninstall_again(self):
        self.panel.verify();self.assertEqual(self.calls('verify'),[])
        self.prepared();self.request();self.manager.outcome='registration_absent_remnants'
        self.panel.verify();self.pump(lambda:not self.panel.busy)
        self.assertEqual(self.calls('verify')[0][2:],('plan-allowed-one',))
        self.assertNotEqual(self.calls('verify')[0][1],threading.get_ident())
        self.assertEqual(len(self.calls('request')),1)
        self.assertIn('остались файлы',self.panel.notice.get());self.assertIn('ручной',self.panel.notice.get())
        self.manager.outcome='registration_absent';self.panel.verify();self.pump(lambda:not self.panel.busy)
        self.assertIn('не доказательство',self.panel.notice.get())

    def test_held_or_uncertain_request_has_no_success_or_automatic_retry(self):
        for outcome in ('blocked_by_policy','uncertain','expired','stale'):
            self.prepared();self.manager.outcome=outcome;self.request()
            count=len(self.calls('request'));self.panel.request();self.root.update()
            self.assertEqual(len(self.calls('request')),count)
            self.assertIsNone(self.panel._plan);self.assertEqual(self.panel.notice.get(),ui.RECEIPT_TEXT[outcome])

    def test_worker_error_is_uncertain_not_success_and_redacts_exception(self):
        self.prepared();self.manager.failure='request';self.request()
        self.assertIn('неоднозначно',self.panel.notice.get());self.assertIsNone(self.panel._plan)
        self.assertNotIn('secret',self.panel.notice.get());self.assertEqual(len(self.calls('request')),1)
        self.panel.request();self.root.update();self.assertEqual(len(self.calls('request')),1)

    def test_close_is_idempotent_and_late_worker_never_touches_tk(self):
        self.manager.hold=threading.Event();self.panel.refresh()
        self.assertTrue(self.manager.entered.wait(1));self.panel.close();self.panel.close()
        self.manager.hold.set();time.sleep(.05)
        self.panel._poll();self.panel.refresh();self.panel.request();self.panel.verify()
        self.assertEqual(self.panel.table.get_children(),());self.assertEqual(len(self.calls('catalog')),1)
        self.assertIsNone(self.panel._poll_id)

    def test_malformed_backend_identity_or_status_does_not_enable_dispatch(self):
        self.manager.items[0]['status']='invented_success';self.refresh()
        self.assertEqual(self.panel.table.get_children(),());self.assertIsNone(self.panel._plan)
        self.manager.items[0]['status']='available';self.refresh();self.select('allowed-one')
        wrong=self.manager.prepare('allowed-two')
        with mock.patch.object(self.manager,'prepare',return_value=wrong):
            self.panel.prepare();self.pump(lambda:not self.panel.busy)
        self.assertIsNone(self.panel._plan);self.assertEqual(self.calls('request'),[])

    def test_default_factory_is_created_only_inside_explicit_refresh_worker(self):
        factory_threads=[]
        def create():factory_threads.append(threading.get_ident());return self.manager
        module=types.SimpleNamespace(ApplicationRemovalManager=create)
        with mock.patch.dict(sys.modules,{'application_removal':module}):
            another=ui.ApplicationRemovalPanel(self.root,parent_guard=self.guard)
            self.assertEqual(factory_threads,[])
            another.refresh();self.pump(lambda:not another.busy)
            self.assertEqual(len(factory_threads),1);self.assertNotEqual(factory_threads[0],threading.get_ident())
            self.assertEqual(len(self.calls('catalog')),1)
            another.close();another.destroy()

    def test_all_core_states_have_russian_explanations_without_claiming_removal(self):
        expected={'not_approved','expired','stale','protected','blocked_by_policy','unsupported',
                  'dispatching','requested','failed','uncertain','not_requested','verification_incomplete',
                  'registration_absent','registration_absent_remnants','registration_absent_unverified_path'}
        self.assertEqual(set(ui.RECEIPT_TEXT),expected)
        context={'plan_id':'plan-allowed-one','id':'allowed-one'}
        for state in expected:
            with self.subTest(state=state):
                self.panel._show_receipt(dict(context,state=state,detail=''),context)
                text=self.panel.notice.get()
                self.assertTrue(any('\u0400'<=char<='\u04ff' for char in text))
                self.assertNotIn(state,text)
                self.assertNotIn('успешно удалено',text.casefold())
        self.assertEqual(self.manager.calls,[])

    def test_each_verification_state_never_dispatches_and_unknown_state_fails_closed(self):
        self.prepared();self.request();requests=len(self.calls('request'))
        for state in ('not_requested','requested','failed','uncertain','verification_incomplete',
                      'registration_absent','registration_absent_remnants','registration_absent_unverified_path'):
            with self.subTest(state=state):
                self.manager.outcome=state;self.panel.verify();self.pump(lambda:not self.panel.busy)
                self.assertEqual(self.panel.notice.get(),ui.RECEIPT_TEXT[state])
                self.assertEqual(len(self.calls('request')),requests)
        self.manager.outcome='removed';self.panel.verify();self.pump(lambda:not self.panel.busy)
        self.assertIsNone(self.panel._receipt)
        self.assertIn('не прошёл проверку',self.panel.notice.get())
        self.assertEqual(len(self.calls('request')),requests)


class ApplicationRemovalFixtureLifecycleTests(unittest.TestCase):
    def test_finished_case_releases_gui_before_later_worker_collection(self):
        # unittest can retain a finished TestCase (for example in results). It
        # must not retain that case's Tcl interpreter across another worker GC.
        gc.collect()
        references=[]
        original=ApplicationRemovalPanelTests.setUp
        def observe(case):
            original(case)
            references.extend(weakref.ref(value) for value in (case.root,case.panel,case.panel.notice))
        case=ApplicationRemovalPanelTests('test_exact_confirmation_and_pin_then_worker_request_is_not_removed')
        result=unittest.TestResult()
        with mock.patch.object(ApplicationRemovalPanelTests,'setUp',observe):
            case.run(result)
        self.assertTrue(result.wasSuccessful(),str(result.errors)+str(result.failures))
        self.assertTrue(all(reference() is None for reference in references),
                        'Finished fixture retained Tk root/panel/Variable before a later worker GC')
        unraisable=[];done=threading.Event()
        def collect():
            try:gc.collect()
            finally:done.set()
        with mock.patch.object(sys,'unraisablehook',side_effect=lambda event:unraisable.append(event.exc_type)):
            worker=threading.Thread(target=collect)
            worker.start();worker.join(3)
        self.assertTrue(done.is_set());self.assertFalse(worker.is_alive());self.assertEqual(unraisable,[])


if __name__=='__main__':unittest.main()
