"""Withdrawn UI and in-memory cache fixtures only. No user files are scanned."""
from __future__ import annotations

from copy import deepcopy
import gc
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
import unittest
import weakref
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cache_cleanup_ui import CACHE_NOTICE, SCOPES, CacheCleanupPanel


def fixture_plan():
    return {
        'plan_id': 'synthetic-plan-001', 'created_utc': '2026-11-19T17:00:00Z',
        'expires_utc': '2026-11-19T17:30:00Z',
        'entries': [{'scope_id': 'nvidia_dxcache', 'path': 'C:/synthetic-cache/first.bin', 'size': 4096}],
        'totals': {'files': 1, 'logical_bytes': 4096}, 'partial': False, 'errors': [],
    }


class FakeCleaner:
    def __init__(self):
        self.calls = []
        self.plan = fixture_plan()
        self.receipt = {
            'status': 'partial', 'deleted_files': 0, 'deleted_bytes': 0,
            'skipped': [{'path': 'synthetic-locked-file'}], 'errors': [],
            'before_free': {'C:': 10000}, 'after_free': {'C:': 10000},
            'receipt_path': 'synthetic-receipt.json',
        }
        self.prepare_gate = None
        self.execute_gate = None
        self.started = threading.Event()
        self.failure = None

    def scope_catalog(self):
        self.calls.append(('catalog', threading.get_ident()))
        return [{'id': scope_id, 'title': title, 'root': 'C:/synthetic-cache'} for scope_id, title in SCOPES]

    def prepare(self, scope_ids, min_age_days=7):
        self.calls.append(('prepare', threading.get_ident(), scope_ids, min_age_days))
        self.started.set()
        if self.prepare_gate and not self.prepare_gate.wait(3):
            raise TimeoutError('synthetic prepare timeout')
        if self.failure == 'prepare':
            raise OSError('synthetic prepare failure')
        return deepcopy(self.plan)

    def execute(self, plan_id, approved=False):
        self.calls.append(('execute', threading.get_ident(), plan_id, approved))
        self.started.set()
        if self.execute_gate and not self.execute_gate.wait(3):
            raise TimeoutError('synthetic execute timeout')
        if self.failure == 'execute':
            raise OSError('synthetic partial failure')
        return deepcopy(self.receipt)


class CacheCleanupPanelTests(unittest.TestCase):
    def setUp(self):
        gc.collect()  # Own Tcl objects must be finalized on this GUI thread.
        self.root = tk.Tk()
        self.root.withdraw()
        self.cleaner = FakeCleaner()
        self.guard = mock.Mock()
        self.guard.configured.return_value = True
        self.guard.verify.return_value = True
        self.panel = CacheCleanupPanel(self.root, self.cleaner, self.guard)
        self.panel.pack(fill='both', expand=True)
        self.root.update()

    def tearDown(self):
        panel, root = self.panel, self.root
        try:
            if self.cleaner.prepare_gate:
                self.cleaner.prepare_gate.set()
            if self.cleaner.execute_gate:
                self.cleaner.execute_gate.set()
            if not panel._closed:
                self.pump(lambda: not self.panel.busy)
        finally:
            # A timeout must not leave a live interpreter in later tests.
            try:
                root.destroy()
            finally:
                self.panel = self.root = None
                del panel, root
                gc.collect()

    def pump(self, predicate):
        until = time.monotonic() + 3
        while not predicate() and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def analyze(self):
        self.panel.scope_vars['nvidia_dxcache'].set(True)
        self.panel.analyze()
        self.pump(lambda: not self.panel.busy)
        self.assertIsNotNone(self.panel._plan)

    def execute_calls(self):
        return [call for call in self.cleaner.calls if call[0] == 'execute']

    def test_constructor_is_inert_and_only_known_scopes_are_present(self):
        self.assertEqual(self.cleaner.calls, [])
        self.assertEqual(tuple(self.panel.scope_vars), tuple(scope_id for scope_id, title in SCOPES))
        self.assertTrue(self.panel.clear_button.instate(['disabled']))
        self.assertIsNone(self.panel._poll_id)
        self.panel.clear()
        self.assertEqual(self.cleaner.calls, [])

    def test_analysis_needs_selected_category(self):
        self.panel.analyze()
        self.assertEqual(self.cleaner.calls, [])
        self.assertIn('Выберите', self.panel.notice.get())

    def test_analysis_runs_off_tk_and_displays_exact_plan_before_any_delete(self):
        self.analyze()
        calls = self.cleaner.calls
        self.assertEqual([call[0] for call in calls], ['catalog', 'prepare'])
        self.assertTrue(all(call[1] != threading.get_ident() for call in calls))
        self.assertEqual(calls[1][2:], (['nvidia_dxcache'], 7))
        self.assertEqual(self.execute_calls(), [])
        text = self.panel.details.get('1.0', 'end')
        self.assertIn('synthetic-cache/first.bin', text)
        self.assertIn('4096 байт', text)
        self.assertIn('0.00 МиБ', self.panel.notice.get())
        self.assertTrue(self.panel.clear_button.instate(['!disabled']))

    def test_slow_analysis_keeps_tk_responsive_and_blocks_duplicate_operations(self):
        self.cleaner.prepare_gate = threading.Event()
        self.panel.scope_vars['nvidia_dxcache'].set(True)
        self.panel.analyze()
        self.assertTrue(self.cleaner.started.wait(1))
        tick = []
        self.root.after(0, lambda: tick.append(True))
        self.root.update()
        self.assertEqual(tick, [True])
        self.panel.analyze()
        self.panel.clear()
        self.assertEqual(len([call for call in self.cleaner.calls if call[0] == 'prepare']), 1)
        self.assertTrue(self.panel.analyze_button.instate(['disabled']))
        self.assertTrue(self.panel.clear_button.instate(['disabled']))
        self.cleaner.prepare_gate.set()
        self.pump(lambda: not self.panel.busy)

    def test_partial_analysis_is_explicit_and_unsupported_scopes_fail_closed(self):
        self.cleaner.plan.update(partial=True, errors=[{'error': 'synthetic locked file'}])
        self.analyze()
        self.assertIn('частичный', self.panel.notice.get())
        self.assertIn('synthetic locked file', self.panel.details.get('1.0', 'end'))
        self.cleaner.plan['entries'][0]['scope_id'] = 'windows_temp'
        self.panel.analyze()
        self.pump(lambda: not self.panel.busy)
        self.assertIsNone(self.panel._plan)
        self.assertTrue(self.panel.clear_button.instate(['disabled']))
        self.assertEqual(self.execute_calls(), [])

    def test_changed_selection_invalidates_reviewed_plan(self):
        self.analyze()
        self.panel.scope_vars['nvidia_glcache'].set(True)
        self.panel._selection_changed()
        self.assertIsNone(self.panel._plan)
        self.panel.clear()
        self.assertEqual(self.execute_calls(), [])

    def test_cancel_confirmation_does_not_request_pin_or_execute(self):
        self.analyze()
        with mock.patch('cache_cleanup_ui.messagebox.askyesno', return_value=False):
            with mock.patch('cache_cleanup_ui.require_parent_pin') as authorize:
                self.panel.clear()
        authorize.assert_not_called()
        self.assertEqual(self.execute_calls(), [])
        self.assertFalse(self.panel.busy)
        self.assertIsNotNone(self.panel._plan)

    def test_wrong_or_missing_pin_prevents_execute(self):
        self.analyze()
        for configured in (True, False):
            with self.subTest(configured=configured):
                self.guard.configured.return_value = configured
                self.guard.verify.return_value = False
                with mock.patch('cache_cleanup_ui.messagebox.askyesno', return_value=True):
                    with mock.patch('cat_ai_ui.simpledialog.askstring', return_value='246810') as dialog:
                        self.panel.clear()
                if not configured:
                    dialog.assert_not_called()
                self.assertEqual(self.execute_calls(), [])
                self.assertFalse(self.panel.busy)

    def test_confirmed_pin_executes_only_captured_plan_off_tk(self):
        self.analyze()
        verified_threads = []
        self.guard.verify.side_effect = lambda pin: verified_threads.append(threading.get_ident()) or True
        with mock.patch('cache_cleanup_ui.messagebox.askyesno', return_value=True) as confirm:
            with mock.patch('cat_ai_ui.simpledialog.askstring', return_value='246810'):
                self.panel.clear()
        self.pump(lambda: not self.panel.busy)
        call, = self.execute_calls()
        self.assertNotEqual(call[1], threading.get_ident())
        self.assertEqual(call[2:], ('synthetic-plan-001', True))
        self.assertEqual(verified_threads, [threading.get_ident()])
        self.assertIn('synthetic-plan-001', confirm.call_args.args[1])
        self.assertIn('1 файлов', confirm.call_args.args[1])
        self.assertIn(CACHE_NOTICE, confirm.call_args.args[1])
        self.assertNotIn('246810', str(self.cleaner.calls))
        self.assertIn('Пропущено 1', self.panel.notice.get())
        self.assertIn('Удалено 0', self.panel.notice.get())
        self.assertIsNone(self.panel._plan)

    def test_authorization_cannot_replace_or_change_captured_plan(self):
        self.analyze()

        def during_confirmation(*args, **kwargs):
            self.panel.analyze()
            self.panel.scope_vars['nvidia_glcache'].set(True)
            return True

        with mock.patch('cache_cleanup_ui.messagebox.askyesno', side_effect=during_confirmation):
            with mock.patch('cache_cleanup_ui.require_parent_pin'):
                self.panel.clear()
        self.assertEqual(len([call for call in self.cleaner.calls if call[0] == 'prepare']), 1)
        self.assertEqual(self.execute_calls(), [])
        self.assertIsNone(self.panel._plan)

    def test_execution_cannot_be_replaced_by_new_analysis(self):
        self.analyze()
        self.cleaner.started.clear()
        self.cleaner.execute_gate = threading.Event()
        with mock.patch('cache_cleanup_ui.messagebox.askyesno', return_value=True):
            with mock.patch('cache_cleanup_ui.require_parent_pin'):
                self.panel.clear()
        self.assertTrue(self.cleaner.started.wait(1))
        self.panel.analyze()
        self.panel.clear()
        self.assertEqual(len(self.execute_calls()), 1)
        self.assertEqual(len([call for call in self.cleaner.calls if call[0] == 'prepare']), 1)
        self.cleaner.execute_gate.set()
        self.pump(lambda: not self.panel.busy)

    def test_worker_failure_reports_possible_partial_execution(self):
        self.analyze()
        self.cleaner.failure = 'execute'
        with mock.patch('cache_cleanup_ui.messagebox.askyesno', return_value=True):
            with mock.patch('cache_cleanup_ui.require_parent_pin'):
                self.panel.clear()
        self.pump(lambda: not self.panel.busy)
        self.assertIn('Часть файлов могла быть удалена', self.panel.notice.get())
        self.assertIsNone(self.panel._plan)
        self.assertTrue(self.panel.clear_button.instate(['disabled']))

    def test_malformed_totals_never_enable_cleanup(self):
        self.cleaner.plan['totals']['files'] = 100
        self.panel.scope_vars['nvidia_dxcache'].set(True)
        self.panel.analyze()
        self.pump(lambda: not self.panel.busy)
        self.assertIsNone(self.panel._plan)
        self.assertEqual(self.execute_calls(), [])


class CacheCleanupFixtureLifecycleTests(unittest.TestCase):
    def test_completed_case_releases_tk_objects_on_the_owning_thread(self):
        references=[]
        original=CacheCleanupPanelTests.setUp
        def observe(case):
            original(case)
            references.extend(weakref.ref(value) for value in (case.root,case.panel,case.panel.notice))
        case=CacheCleanupPanelTests('test_confirmed_pin_executes_only_captured_plan_off_tk')
        result=unittest.TestResult()
        with mock.patch.object(CacheCleanupPanelTests,'setUp',observe):
            case.run(result)
        self.assertTrue(result.wasSuccessful(),str(result.errors)+str(result.failures))
        self.assertTrue(all(reference() is None for reference in references),
                        'Completed cache fixture retained a Tcl interpreter for later worker GC')

    def test_teardown_assertion_still_destroys_root_and_releases_tk_references(self):
        case=CacheCleanupPanelTests('test_analysis_needs_selected_category')
        case.setUp()
        references=[weakref.ref(value) for value in (case.root,case.panel,case.panel.notice)]
        case.pump=mock.Mock(side_effect=AssertionError('Owned synthetic pump failure'))
        with self.assertRaisesRegex(AssertionError,'Owned synthetic pump failure'):
            case.tearDown()
        self.assertIsNone(case.root);self.assertIsNone(case.panel)
        self.assertTrue(all(reference() is None for reference in references),
                        'Failed cache teardown left Tk objects alive in later fixtures')


if __name__ == '__main__':
    unittest.main()
