"""API configuration UI tests with fakes; no keys, network, or user history."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import queue
import threading
import time
import tkinter as tk
import unittest

from cat_ai_ui import (ACCOUNT_NOTICE, ATTESTATION_NOTICE, ZDR_NOTICE,
                       collect_values, local_status_text, open_ai_settings,
                       save_settings_worker)


class FakeSettings:
    def __init__(self, key=True):
        self.data = dict(enabled=False, mode='offline', model='gpt-6-luna', key_configured=key,
                         daily_request_limit=50, max_output_tokens=400, parental_consent=False,
                         zdr_attested=False, approval_reference='', ready=False, state='disabled')
        self.changes = []
        self.fail_config = False
        self.block = None

    def snapshot(self):
        return dict(self.data)

    def configure(self, **values):
        if self.block:
            self.block.wait(2)
        if self.fail_config:
            raise ValueError('SECRET-should-not-appear')
        self.changes.append(('configure', dict(values)))
        self.data.update(values)
        return self.snapshot()

    def set_api_key(self, value):
        self.changes.append(('set_key', len(value)))
        self.data['key_configured'] = True

    def clear_api_key(self):
        self.changes.append(('clear_key',))
        self.data['key_configured'] = False


class FakeGateway:
    def __init__(self, settings):
        self.settings = settings
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        data = self.settings.snapshot()
        ready = all(data.get(k) for k in ('enabled', 'key_configured', 'parental_consent', 'zdr_attested', 'approval_reference'))
        return dict(ready=ready, state='ready' if ready else 'disabled', provider_privacy_verified=False)


def example_values():
    return collect_values(True, 'gpt-6-luna', '50', '400', True, True, 'approval-demo-001')


class WorkerBoundaryTests(unittest.TestCase):
    def test_empty_key_action_preserves_previous_key(self):
        settings = FakeSettings()
        replies = queue.Queue()
        save_settings_worker(settings, FakeGateway(settings), example_values(), 'keep', '', replies)
        result = replies.get_nowait()
        self.assertEqual(result['status'], 'saved')
        self.assertEqual([entry[0] for entry in settings.changes], ['configure'])
        self.assertFalse(result['snapshot']['provider_privacy_verified'])

    def test_partial_failure_is_explicit_without_key_or_exception(self):
        settings = FakeSettings(key=False)
        settings.fail_config = True
        replies = queue.Queue()
        save_settings_worker(settings, FakeGateway(settings), example_values(), 'set', 'synthetic-key-not-real', replies)
        result = replies.get_nowait()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['key_changed'])
        self.assertNotIn('synthetic-key-not-real', str(result))
        self.assertNotIn('SECRET', str(result))

    def test_local_readiness_is_not_provider_verification(self):
        data = example_values()
        data.update(key_configured=True, ready=True)
        text = local_status_text(data)
        self.assertIn('не проверялись', text)
        self.assertIn('провайдера', text)
        data['parental_consent'] = False
        self.assertIn('передачу данных', local_status_text(data))

    def test_notice_distinguishes_zdr_and_operator_attestation(self):
        self.assertIn('store=false', ZDR_NOTICE)
        self.assertIn('не включает', ZDR_NOTICE)
        self.assertIn('provider_privacy_verified=false', ATTESTATION_NOTICE)
        self.assertIn('не является защищённой', ACCOUNT_NOTICE)

    def test_invalid_values_do_not_reach_settings(self):
        for daily, output in [('1.5', '400'), ('0', '400'), ('50', '0'), ('501', '400'), ('50', '127'), ('50', '1201')]:
            with self.assertRaises(ValueError):
                collect_values(True, 'gpt-6-luna', daily, output, True, True, '')

    def test_possible_secret_in_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            collect_values(True, 'gpt-6-luna', '50', '400', True, True, 'sk-not-a-reference')

    def test_reference_limit_matches_backend_before_key_changes(self):
        self.assertEqual(collect_values(True, 'gpt-6-luna', '50', '400', True, True, 'r' * 200)['approval_reference'], 'r' * 200)
        with self.assertRaises(ValueError):
            collect_values(True, 'gpt-6-luna', '50', '400', True, True, 'r' * 201)


class WithdrawnSettingsWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest('Tk unavailable: ' + type(error).__name__)
        self.root.withdraw()
        self.settings = FakeSettings()
        self.gateway = FakeGateway(self.settings)
        self.window = open_ai_settings(self.root, self.settings, self.gateway)
        self.window.withdraw()
        self.wait_saved()

    def tearDown(self):
        if hasattr(self, 'window') and not self.window._closed:
            self.window.close()
        if hasattr(self, 'root'):
            self.root.destroy()

    def wait_saved(self):
        deadline = time.monotonic() + 3
        while self.window._busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.window._busy)

    def test_open_and_clear_button_do_not_write_until_save(self):
        self.assertEqual(self.settings.changes, [])
        self.assertEqual(self.window.key_var.get(), '')
        self.assertTrue(self.window.key_entry.cget('show'))
        self.window.stage_key_clear()
        self.assertEqual(self.settings.changes, [])
        self.window.save()
        self.wait_saved()
        self.assertIn(('clear_key',), self.settings.changes)
        self.assertFalse(self.settings.data['key_configured'])

    def test_slow_status_read_leaves_tk_responsive_and_serializes_controls(self):
        started, release = threading.Event(), threading.Event()
        reader_threads = []
        original_snapshot = self.settings.snapshot

        def slow_snapshot():
            reader_threads.append(threading.get_ident())
            started.set()
            if not release.wait(2):
                raise TimeoutError('synthetic read timeout')
            return original_snapshot()

        self.settings.snapshot = slow_snapshot
        self.settings.data['daily_request_limit'] = 42
        tick = threading.Event()
        self.root.after(0, tick.set)
        try:
            self.window.refresh()
            self.assertTrue(started.wait(1))
            self.assertNotEqual(reader_threads[0], threading.get_ident())
            self.root.update()
            self.assertTrue(tick.is_set())
            self.assertTrue(self.window._busy)
            self.assertFalse(self.window.saving)
            self.assertTrue(self.window.save_button.instate(['disabled']))
            self.window.refresh()
            self.window.save()
            self.assertEqual(len(reader_threads), 1)
            self.assertEqual(self.settings.changes, [])
        finally:
            release.set()
            self.wait_saved()
        self.assertEqual(self.window.daily_var.get(), '42')
        self.assertTrue(self.window.save_button.instate(['!disabled']))

    def test_failed_status_read_does_not_enable_saving_stale_fields(self):
        def unreadable():
            raise OSError('synthetic file error')

        self.settings.snapshot = unreadable
        self.window.refresh()
        self.wait_saved()
        self.assertTrue(self.window.save_button.instate(['disabled']))
        self.assertTrue(self.window.refresh_button.instate(['!disabled']))
        self.window.save()
        self.assertEqual(self.settings.changes, [])

    def test_save_uses_worker_and_clears_entry(self):
        self.settings.block = threading.Event()
        self.window.key_var.set('synthetic-key-not-real')
        self.window.enabled_var.set(True)
        self.window.save()
        self.assertEqual(self.window.key_var.get(), '')
        self.assertTrue(self.window._busy)
        self.settings.block.set()
        self.wait_saved()
        self.assertIn(('set_key', len('synthetic-key-not-real')), self.settings.changes)
        self.assertTrue(self.settings.data['enabled'])
        self.assertIn('отправка пока недоступна', self.window.status_label.cget('text'))

    def test_close_during_save_waits_for_the_settings_commit(self):
        self.settings.block = threading.Event()
        self.window.save()
        self.window.key_var.set('not-saved')
        self.window.close()
        self.assertEqual(self.window.key_var.get(), '')
        self.assertTrue(self.window.saving)
        self.assertTrue(self.window.winfo_exists())
        self.settings.block.set()
        self.wait_saved()
        self.assertTrue(self.window._closed)
        self.assertFalse(self.window.winfo_exists())

    def test_incomplete_enabled_settings_are_saved_with_honest_readiness(self):
        self.settings.data['key_configured'] = False
        self.window.refresh()
        self.wait_saved()
        self.window.enabled_var.set(True)
        self.window.save()
        self.wait_saved()
        self.assertTrue(self.settings.data['enabled'])
        text = self.window.status_label.cget('text')
        self.assertIn('API-ключ', text)
        self.assertIn('пока недоступна', text)

    def test_new_key_clears_old_privacy_attestation_before_reconfirmation(self):
        self.settings.data.update(parental_consent=True, zdr_attested=True, approval_reference='old-project')
        self.window.refresh()
        self.wait_saved()
        self.assertTrue(self.window.consent_var.get())
        self.window.key_var.set('synthetic-new-key')
        self.assertFalse(self.window.consent_var.get())
        self.assertFalse(self.window.zdr_var.get())
        self.assertEqual(self.window.reference_var.get(), '')
        self.window.consent_var.set(True)
        self.window.zdr_var.set(True)
        self.window.reference_var.set('new-project')
        self.window.save()
        self.wait_saved()
        self.assertTrue(self.settings.data['parental_consent'])
        self.assertEqual(self.settings.data['approval_reference'], 'new-project')

    def test_blank_save_after_validation_error_preserves_old_key(self):
        self.window.key_var.set('synthetic-new-key')
        self.window.daily_var.set('not-a-number')
        self.window.save()
        self.assertEqual(self.window.key_var.get(), '')
        self.assertEqual(self.settings.changes, [])
        self.assertFalse(self.window._busy)
        self.window.daily_var.set('50')
        self.window.save()
        self.wait_saved()
        self.assertEqual([row[0] for row in self.settings.changes], ['configure'])
        self.assertTrue(self.settings.data['key_configured'])

    def test_long_reference_does_not_partially_replace_key(self):
        self.window.key_var.set('synthetic-new-key')
        self.window.reference_var.set('r' * 201)
        self.window.save()
        self.assertEqual(self.settings.changes, [])
        self.assertTrue(self.settings.data['key_configured'])
        self.assertEqual(self.window.key_var.get(), '')
        self.assertFalse(self.window._busy)


if __name__ == '__main__':
    unittest.main()
