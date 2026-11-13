import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Real local kitten→bridge→queue pipeline with fake Telegram and no network."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from family_bridge import FamilyBridge, FamilyBridgeError
from family_messages import MessageStore
from family_telegram import DeliveryUncertain, TelegramSettings
from kitten_messenger import MessengerController, _InstanceSignal

TOKEN = '987654:' + 'X' * 35
PARENT = 97531


class FixtureProtector:
    def protect(self, value):
        return b'fixture:' + value

    def unprotect(self, value):
        if not value.startswith(b'fixture:'):
            raise ValueError('invalid fixture')
        return value[8:]


class FixtureAPI:
    def __init__(self):
        self.updates = []
        self.sends = []
        self.uncertain = False

    def factory(self, token):
        if token != TOKEN:
            raise AssertionError('unexpected synthetic token')
        return self

    def call(self, method, payload):
        if method == 'getMe':
            return {'id': 987654, 'is_bot': True, 'username': 'SyntheticFamilyBot'}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            return self.updates
        if method == 'sendMessage':
            self.sends.append(dict(payload))
            if self.uncertain:
                raise DeliveryUncertain()
            return {'message_id': 99, 'chat': {'id': payload['chat_id']}}
        raise AssertionError('unexpected fake method')


@unittest.skipUnless(os.name == 'nt', 'Telegram settings use the Windows mutex')
class KittenChannelContractTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / 'tests-work'
        fixtures.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='kitten-channel-', dir=fixtures)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MessageStore(self.root)
        self.api = FixtureAPI()
        self.settings = TelegramSettings(self.root, api_factory=self.api.factory, protector=FixtureProtector())
        self.worker = mock.Mock()
        self.bridge = FamilyBridge(self.store, self.settings, self.worker)
        self.controller = MessengerController(self.store, self.bridge, self.worker)

    def ready(self):
        self.settings.setup(TOKEN)
        pairing = self.settings.begin_pairing()
        self.api.updates = [{'update_id': 1, 'message': {'message_id': 1,
            'chat': {'id': PARENT, 'type': 'private'},
            'from': {'id': PARENT, 'is_bot': False, 'first_name': 'Synthetic parent'},
            'text': '/start ' + pairing['nonce']}}]
        self.settings.poll_pairing()
        self.settings.confirm_pairing(pairing['nonce'])
        self.settings.enable()
        self.api.updates = []

    def test_unconfigured_send_cannot_create_a_future_recipient_message(self):
        with self.assertRaises(FamilyBridgeError):
            self.controller.send('Тестовое сообщение', 'intent-1')
        self.assertEqual(self.store.status()['queued'], 0)
        self.assertFalse(self.controller.snapshot()['worker_ready'])
        self.worker.assert_not_called()
        self.assertEqual(self.api.sends, [])

    def test_deliberate_send_receipt_and_fixed_recipient_are_distinct(self):
        self.ready()
        queued = self.controller.send('Привет из теста', 'intent-1')
        self.assertEqual(queued['status'], 'queued')
        self.assertFalse(queued['accepted_by_telegram'])
        self.assertFalse(queued['delivery_confirmed'])
        self.controller.send('Привет из теста', 'intent-1')
        self.assertEqual(self.store.status()['queued'], 1)
        self.settings.process_outbox(self.store)
        self.assertEqual(len(self.api.sends), 1)
        self.assertEqual(self.api.sends[0]['chat_id'], PARENT)
        receipt = self.bridge.message_status(queued['id'])
        self.assertTrue(receipt['accepted_by_telegram'])
        self.assertFalse(receipt['read_confirmed'])

    def test_parent_only_incoming_survives_popup_shown_status(self):
        self.ready()
        self.api.updates = [
            {'update_id': 2, 'message': {'message_id': 2, 'chat': {'id': PARENT, 'type': 'private'}, 'from': {'id': 24680, 'is_bot': False}, 'text': 'Untrusted sender'}},
            {'update_id': 3, 'message': {'message_id': 3, 'chat': {'id': 24680, 'type': 'private'}, 'from': {'id': 24680, 'is_bot': False}, 'text': 'Untrusted chat'}},
            {'update_id': 4, 'message': {'message_id': 4, 'chat': {'id': PARENT, 'type': 'private'}, 'from': {'id': PARENT, 'is_bot': False}, 'text': 'Папа отвечает в тесте'}},
        ]
        accepted = self.settings.poll_incoming(self.store)
        self.assertEqual(len(accepted), 1)
        snapshot = self.controller.snapshot()
        self.assertEqual([row['text'] for row in snapshot['messages']], ['Папа отвечает в тесте'])
        self.assertEqual(snapshot['messages'][0]['status'], 'received')
        self.assertEqual(self.store.status()['received'], 1)
        self.store.mark_incoming_shown(accepted[0]['id'])
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['messages'][0]['status'], 'shown')
        self.assertEqual(self.api.sends, [])

    def test_paused_after_readiness_snapshot_never_queues_the_draft(self):
        self.ready()
        self.assertTrue(self.controller.snapshot()['channel']['channel_ready'])
        self.settings.disable()
        with self.assertRaises(FamilyBridgeError):
            self.controller.send('Тест после паузы', 'intent-1')
        self.assertEqual(self.store.status()['queued'], 0)
        self.assertEqual(self.api.sends, [])

    def test_unknown_delivery_is_not_retried_by_refresh_or_same_request(self):
        self.ready()
        queued = self.controller.send('Один тест', 'intent-1')
        self.api.uncertain = True
        self.settings.process_outbox(self.store)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['messages'][0]['status'], 'uncertain')
        same = self.controller.send('Один тест', 'intent-1')
        self.assertEqual(same['id'], queued['id'])
        self.assertEqual(same['status'], 'uncertain')
        self.settings.process_outbox(self.store)
        self.assertEqual(len(self.api.sends), 1)

    def test_native_single_instance_signal_roundtrip_without_ui_or_files(self):
        first, second = _InstanceSignal(self.root), _InstanceSignal(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue(first.requested())
            self.assertFalse(first.requested())
        finally:
            second.close()
            first.close()
        replacement = _InstanceSignal(self.root)
        try:
            self.assertTrue(replacement.acquire())
        finally:
            replacement.close()


if __name__ == '__main__':
    unittest.main()
