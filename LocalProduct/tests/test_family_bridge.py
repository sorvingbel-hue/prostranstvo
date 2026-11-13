"""Explicit family bridge with synthetic data, fake Telegram and fake worker.

No real messages, tokens, Telegram connections, UI, registry or worker processes.
The existing Windows mutex is exercised; credential protection is a test substitute.
"""
import contextlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]
WORKSPACE = PRODUCT.parent
TOKEN = '654321:' + 'T' * 35
PRIVATE = 'Synthetic intentional text, never a real conversation'


class TestProtector:
    def protect(self, value):
        return b'fixture:' + bytes(b ^ 0xA5 for b in value)

    def unprotect(self, value):
        if not value.startswith(b'fixture:'):
            raise ValueError('invalid fixture')
        return bytes(b ^ 0xA5 for b in value[8:])


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.send_error = None

    def factory(self, token):
        assert token == TOKEN
        return self

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == 'getMe':
            return {'id': 654321, 'is_bot': True, 'username': 'FixtureFamilyBot'}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            return self.updates
        if method == 'sendMessage':
            if self.send_error:
                raise self.send_error
            return {'message_id': 501, 'chat': {'id': payload['chat_id']}}
        raise AssertionError('unexpected fake method')


@contextlib.contextmanager
def modules_for(root, legacy=False):
    paths = types.ModuleType('paths')
    paths.APP_ROOT = root / 'app'
    paths.DATA_ROOT = root / 'beta-data'
    paths.DEPLOY_ROOT = root / 'legacy-deploy'
    paths.SOURCE_ROOT = paths.APP_ROOT
    source = WORKSPACE / 'WindowsCompanion' if legacy else PRODUCT / 'src'
    with mock.patch.dict(sys.modules), mock.patch.object(sys, 'path', [str(source), *sys.path]):
        for name in ('paths', 'family_bridge', 'family_messages', 'family_telegram', 'family_agent'):
            sys.modules.pop(name, None)
        sys.modules['paths'] = paths
        yield types.SimpleNamespace(paths=paths,
            messages=importlib.import_module('family_messages'),
            telegram=importlib.import_module('family_telegram'),
            bridge=importlib.import_module('family_bridge'))


@unittest.skipUnless(os.name == 'nt', 'The transport uses a Windows user mutex')
class FamilyBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = PRODUCT / 'build' / 'family-bridge-tests'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='fixture-', dir=temporary)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        context = modules_for(self.root)
        self.m = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.channel = self.root / 'channel'
        self.api = FakeAPI()
        self.settings = self.m.telegram.TelegramSettings(self.channel,
            api_factory=self.api.factory, protector=TestProtector())
        self.worker = mock.Mock()

    def ready(self, store=True):
        self.settings.setup(TOKEN)
        pairing = self.settings.begin_pairing()
        self.api.updates = [{'update_id': 1, 'message': {'message_id': 10,
            'chat': {'id': 97531, 'type': 'private'},
            'from': {'id': 97531, 'is_bot': False, 'first_name': 'Fixture Parent', 'username': 'fixture_parent'},
            'text': '/start ' + pairing['nonce']}}]
        self.settings.poll_pairing()
        self.settings.confirm_pairing(pairing['nonce'])
        self.settings.enable()
        self.store = self.m.messages.MessageStore(self.channel) if store else None
        return self.m.bridge.FamilyBridge(self.store, self.settings, self.worker)

    def assert_error(self, expected, action):
        with self.assertRaises(self.m.bridge.FamilyBridgeError) as caught:
            action()
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)

    def test_constructor_and_missing_channel_status_are_lazy_and_redacted(self):
        bridge = self.m.bridge.FamilyBridge(settings=self.settings, ensure_worker=self.worker)
        self.assertFalse(self.channel.exists())
        self.assertEqual(bridge.channel_status(), {'transport': 'telegram', 'configured': False,
            'paired': False, 'enabled': False, 'channel_ready': False, 'state': 'not_configured'})
        self.assertFalse(self.channel.exists())
        self.assert_error('not_configured', lambda: bridge.send_message(PRIVATE, 'request-1'))
        self.assertFalse(self.channel.exists())
        self.worker.assert_not_called()

    def test_default_roots_preserve_beta_and_legacy_locations_without_writes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), modules_for(self.root / str(legacy), legacy) as m:
                bridge = m.bridge.FamilyBridge(ensure_worker=self.worker)
                self.assertIsNone(bridge._store)
                self.assertIsNone(bridge._settings)
                bridge.channel_status()
                expected = m.paths.DEPLOY_ROOT / 'data' / 'family-messages' if legacy else m.paths.DATA_ROOT / 'family-messages'
                self.assertEqual(bridge._settings.root, expected.absolute())
                self.assertEqual(m.messages.default_root(), expected)
                self.assertFalse(expected.exists())

    def test_paused_channel_does_not_enqueue_or_bind_queue(self):
        bridge = self.ready()
        self.settings.disable()
        self.assert_error('channel_paused', lambda: bridge.send_message(PRIVATE, 'request-1'))
        self.assertEqual(self.store.status()['total'], 0)
        self.assertIsNone(self.store.get_binding())
        self.worker.assert_not_called()

    def test_unpaired_channel_does_not_create_message_database(self):
        self.settings.setup(TOKEN)
        bridge = self.m.bridge.FamilyBridge(settings=self.settings, ensure_worker=self.worker)
        self.assert_error('not_paired', lambda: bridge.send_message(PRIVATE, 'request-1'))
        self.assertFalse((self.channel / 'messages.sqlite').exists())

    def test_pause_between_preflight_and_queue_is_rechecked_under_lock(self):
        bridge = self.ready()
        original = bridge._channel
        def pause_after_status(settings):
            state = original(settings)
            settings.disable()
            return state
        with mock.patch.object(bridge, '_channel', side_effect=pause_after_status):
            self.assert_error('channel_paused', lambda: bridge.send_message(PRIVATE, 'request-1'))
        self.assertEqual(self.store.status()['total'], 0)
        self.worker.assert_not_called()

    def test_queue_binding_refuses_different_recipient_before_insert(self):
        bridge = self.ready()
        self.store.bind_identity('a' * 64)
        calls = len(self.api.calls)
        self.assert_error('binding_changed', lambda: bridge.send_message(PRIVATE, 'request-1'))
        self.assertEqual(self.store.status()['total'], 0)
        self.assertEqual(len(self.api.calls), calls)
        self.worker.assert_not_called()

    def test_queue_mutex_serializes_concurrent_pause_from_another_settings_instance(self):
        bridge = self.ready()
        other = self.m.telegram.TelegramSettings(self.channel,
            api_factory=self.api.factory, protector=TestProtector())
        entered, release, pausing, paused = (threading.Event() for _ in range(4))
        original = self.store.enqueue_reply
        errors, results = [], []
        def slow_insert(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture release timeout')
            return original(*args, **kwargs)
        def send():
            try: results.append(bridge.send_message(PRIVATE, 'request-1'))
            except Exception as error: errors.append(error)
        def pause():
            pausing.set()
            try: other.disable()
            except Exception as error: errors.append(error)
            finally: paused.set()
        with mock.patch.object(self.store, 'enqueue_reply', side_effect=slow_insert):
            sender = threading.Thread(target=send)
            stopper = threading.Thread(target=pause)
            sender.start()
            try:
                self.assertTrue(entered.wait(2))
                stopper.start()
                self.assertTrue(pausing.wait(1))
                self.assertFalse(paused.wait(.15), 'Pause must wait for atomic binding/insertion')
            finally:
                release.set()
                sender.join(4)
                if stopper.ident is not None: stopper.join(4)
        self.assertFalse(errors, errors)
        self.assertEqual(results[0]['status'], 'queued')
        self.assertFalse(other.status()['enabled'])
        self.assertIsNotNone(self.store.get_binding())

    def test_duplicate_id_preserves_one_exact_message_and_rejects_changed_text(self):
        bridge = self.ready()
        calls = len(self.api.calls)
        one = bridge.send_message(PRIVATE, 'stable-request')
        two = bridge.send_message(PRIVATE, 'stable-request')
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(self.store.status()['total'], 1)
        self.assertEqual(self.store.list_messages()[0]['text'], PRIVATE)
        self.assertEqual(len(self.api.calls), calls, 'Bridge must not call Telegram directly')
        self.assert_error('queue_rejected', lambda: bridge.send_message('different fixture', 'stable-request'))
        self.assertEqual(self.store.status()['total'], 1)

    def test_sent_means_telegram_acceptance_and_duplicate_does_not_send_again(self):
        bridge = self.ready()
        self.worker.side_effect = lambda store: self.settings.process_outbox(store)
        one = bridge.send_message(PRIVATE, 'stable-request')
        two = bridge.send_message(PRIVATE, 'stable-request')
        self.assertTrue(one['accepted_by_telegram'])
        self.assertFalse(one['delivery_confirmed'])
        self.assertFalse(one['read_confirmed'])
        self.assertEqual(two['status'], 'sent')
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.api.calls), 1)
        self.worker.assert_called_once()

    def test_uncertain_is_not_claimed_delivered_or_automatically_retried(self):
        bridge = self.ready()
        self.api.send_error = RuntimeError('fixture private raw network error ' + TOKEN)
        self.worker.side_effect = lambda store: self.settings.process_outbox(store)
        one = bridge.send_message(PRIVATE, 'stable-request')
        two = bridge.send_message(PRIVATE, 'stable-request')
        self.assertEqual(one['status'], 'uncertain')
        self.assertFalse(one['accepted_by_telegram'])
        self.assertFalse(two['delivery_confirmed'])
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.api.calls), 1)
        self.worker.assert_called_once()
        self.assertNotIn(TOKEN, json.dumps(two))

    def test_failed_is_not_automatically_retried(self):
        bridge = self.ready()
        self.api.send_error = self.m.telegram.TelegramRejected(400)
        self.worker.side_effect = lambda store: self.settings.process_outbox(store)
        one = bridge.send_message(PRIVATE, 'stable-request')
        two = bridge.send_message(PRIVATE, 'stable-request')
        self.assertEqual(one['status'], 'failed')
        self.assertFalse(two['accepted_by_telegram'])
        self.worker.assert_called_once()

    def test_worker_failure_returns_safe_queued_receipt_without_losing_text(self):
        bridge = self.ready()
        self.worker.side_effect = RuntimeError(PRIVATE + TOKEN)
        value = bridge.send_message(PRIVATE, 'request-1')
        self.assertEqual(value['status'], 'queued')
        self.assertEqual(value['worker_state'], 'unavailable')
        self.assertFalse(value['accepted_by_telegram'])
        self.assertEqual(self.store.list_messages()[0]['text'], PRIVATE)
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertNotIn(TOKEN, json.dumps(value))

    def test_refresh_failure_after_enqueue_keeps_acknowledgement_and_stable_id(self):
        bridge = self.ready()
        with mock.patch.object(bridge, 'message_status', side_effect=self.m.bridge.FamilyBridgeError('receipt_unavailable')):
            value = bridge.send_message(PRIVATE, 'request-1')
        self.assertEqual(value['status'], 'queued')
        self.assertTrue(value['status_refresh_unavailable'])
        self.assertFalse(value['accepted_by_telegram'])
        self.assertFalse(value['delivery_confirmed'])
        self.assertEqual(bridge.message_status(value['id'])['status'], 'queued')
        self.assertEqual(self.store.status()['total'], 1)
        self.assertNotIn(PRIVATE, json.dumps(value))

    def test_receipt_query_does_not_initialize_missing_storage_or_change_existing_db(self):
        bridge = self.m.bridge.FamilyBridge(settings=self.settings, ensure_worker=self.worker)
        self.assert_error('receipt_unavailable', lambda: bridge.message_status('unknown-fixture'))
        self.assertFalse(self.channel.exists())
        bridge = self.ready()
        value = bridge.send_message(PRIVATE, 'request-1')
        before = self.store.db.read_bytes()
        self.assertEqual(bridge.message_status(value['id'])['status'], 'queued')
        self.assertEqual(self.store.db.read_bytes(), before)

    def test_status_and_receipts_redact_text_credentials_and_recipient_identity(self):
        bridge = self.ready()
        channel = bridge.channel_status()
        receipt = bridge.send_message(PRIVATE, 'request-1')
        value = json.dumps([channel, receipt, bridge.message_status(receipt['id'])])
        for private in (TOKEN, PRIVATE, 'Fixture Parent', 'fixture_parent', 'FixtureFamilyBot', '97531', '654321', str(self.channel)):
            self.assertNotIn(private, value)
        self.assertEqual(set(channel), {'transport','configured','paired','enabled','channel_ready','state'})

    def test_incoming_unknown_or_invalid_receipt_never_exposes_messages(self):
        bridge = self.ready()
        incoming = self.store.add_incoming(PRIVATE, 10, 20)
        self.assert_error('receipt_not_outgoing', lambda: bridge.message_status(incoming['id']))
        self.assert_error('receipt_unavailable', lambda: bridge.message_status('unknown-fixture-id'))
        self.assert_error('invalid_message_id', lambda: bridge.message_status('../private'))

    def test_invalid_text_or_request_id_do_not_create_storage_or_echo_input(self):
        bridge = self.m.bridge.FamilyBridge(settings=self.settings, ensure_worker=self.worker)
        for text in ('', 'x' * 4001, 'unsafe\x00fixture'):
            self.assert_error('invalid_message', lambda text=text: bridge.send_message(text, 'request-1'))
        self.assert_error('invalid_request_id', lambda: bridge.send_message(PRIVATE, None))
        self.assert_error('invalid_request_id', lambda: bridge.send_message(PRIVATE, 'bad/id'))
        self.assertFalse(self.channel.exists())


if __name__ == '__main__':
    unittest.main()
