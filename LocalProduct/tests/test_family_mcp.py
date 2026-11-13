"""Family MCP protocol tests with inert modules/bridge: no user data or network."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]


class TelegramErrorFixture(RuntimeError):
    def __init__(self, code='not_configured'):
        self.code = code
        super().__init__('private exception detail must never escape')


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.receipt = {'id': 'message_fixture', 'status': 'queued', 'transport': 'telegram',
                        'accepted_by_telegram': False, 'delivery_confirmed': False,
                        'read_confirmed': False, 'worker_state': 'ready'}

    def channel_status(self):
        self.calls.append(('status',))
        if self.failure:
            raise self.failure
        return {'transport': 'telegram', 'configured': False, 'paired': False,
                'enabled': False, 'channel_ready': False, 'state': 'not_configured'}

    def send_message(self, text, request_id):
        self.calls.append(('send', text, request_id))
        if self.failure:
            raise self.failure
        return dict(self.receipt)

    def message_status(self, message_id):
        self.calls.append(('receipt', message_id))
        if self.failure:
            raise self.failure
        return dict(self.receipt)


class FamilyMcpTests(unittest.TestCase):
    def setUp(self):
        # Never import the real family backend; these protocol tests need no DB,
        # DPAPI, mutex, worker, Telegram credentials, process or file fixture.
        family = types.ModuleType('family_bridge')
        family.FamilyBridge = mock.Mock(side_effect=AssertionError('unexpected default bridge construction'))
        telegram = types.ModuleType('family_telegram')
        telegram.TelegramError = TelegramErrorFixture
        self.default_bridge = family.FamilyBridge
        self.modules = mock.patch.dict(sys.modules, {'family_bridge': family, 'family_telegram': telegram})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location('isolated_family_mcp', PRODUCT / 'src' / 'family_mcp.py')
        self.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.protocol)
        self.bridge = FakeBridge()

    def exchange(self, *requests, raw=None):
        input_text = raw if raw is not None else ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in requests)
        output = io.StringIO()
        self.protocol.main(io.StringIO(input_text), output, self.bridge)
        body = output.getvalue()
        if body:
            self.assertTrue(body.endswith('\n'))
            self.assertEqual(len(body.splitlines()), body.count('\n'))
        return [json.loads(line) for line in body.splitlines()], body

    @staticmethod
    def request(name, arguments, ident=1):
        return {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}

    @staticmethod
    def content(response):
        return json.loads(response['result']['content'][0]['text'])

    def test_import_constructs_no_bridge_and_does_not_read_streams(self):
        self.default_bridge.assert_not_called()
        self.assertEqual(self.bridge.calls, [])

    def test_initialize_ping_list_and_notification_have_one_line_per_response(self):
        responses, _ = self.exchange(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
        )
        self.assertEqual([r['id'] for r in responses], [1, 2, 3])
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(responses[0]['result']['serverInfo']['name'], 'prostranstvo-family')
        self.assertEqual(responses[1]['result'], {})
        tools = responses[2]['result']['tools']
        self.assertEqual({x['name'] for x in tools}, {'family_channel_status', 'send_family_message', 'family_message_status'})
        self.assertEqual(self.bridge.calls, [])

    def test_surface_has_no_arbitrary_recipient_file_shell_or_history_tool(self):
        by_name = {x['name']: x for x in self.protocol.TOOLS}
        self.assertEqual(len(by_name), 3)
        for tool in by_name.values():
            self.assertFalse(tool['inputSchema']['additionalProperties'])
        send = by_name['send_family_message']
        self.assertEqual(set(send['inputSchema']['properties']), {'text', 'request_id'})
        self.assertEqual(set(send['inputSchema']['required']), {'text', 'request_id'})
        self.assertFalse(send['annotations']['readOnlyHint'])
        self.assertTrue(send['annotations']['idempotentHint'])

    def test_channel_status_and_single_receipt_use_only_expected_calls(self):
        responses, _ = self.exchange(self.request('family_channel_status', {}),
                                     self.request('family_message_status', {'message_id': 'message_fixture'}, 2))
        self.assertEqual(self.content(responses[0])['state'], 'not_configured')
        self.assertEqual(self.content(responses[1])['id'], 'message_fixture')
        self.assertEqual(self.bridge.calls, [('status',), ('receipt', 'message_fixture')])

    def test_unicode_send_preserves_text_request_id_and_queued_status(self):
        text = 'Привет, папа! Как дела? 👋\nПозвони мне.'
        args = {'text': text, 'request_id': 'stable_send_123'}
        responses, body = self.exchange(self.request('send_family_message', args))
        self.assertEqual(self.bridge.calls, [('send', text, 'stable_send_123')])
        result = self.content(responses[0])
        self.assertEqual(result['status'], 'queued')
        self.assertFalse(result['accepted_by_telegram'])
        self.assertFalse(result['delivery_confirmed'])
        self.assertFalse(result['read_confirmed'])
        self.assertNotIn(text, body)

    def test_same_client_request_id_passes_through_unchanged_on_retry(self):
        args = {'text': 'Explicit synthetic message', 'request_id': 'same_nonce'}
        responses, _ = self.exchange(self.request('send_family_message', args, 1),
                                     self.request('send_family_message', args, 99))
        self.assertEqual([x[2] for x in self.bridge.calls], ['same_nonce', 'same_nonce'])
        self.assertEqual(self.content(responses[0]), self.content(responses[1]))

    def test_missing_request_id_or_arbitrary_destination_is_rejected_before_bridge(self):
        bad_arguments = [
            {'text': 'fixture'},
            {'text': 'fixture', 'request_id': 'nonce', 'chat_id': 'arbitrary'},
            {'text': 'fixture', 'request_id': 'nonce', 'recipient': 'arbitrary'},
            {'text': 'fixture', 'request_id': 'nonce', 'file': 'not-permitted'},
        ]
        for arguments in bad_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.protocol.call(self.bridge, 'send_family_message', arguments)
        self.assertEqual(self.bridge.calls, [])

    def test_invalid_text_and_identifiers_are_rejected_before_bridge(self):
        for text in ('', 'x' * 4001, None, 8, ['fixture']):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(ValueError):
                self.protocol.call(self.bridge, 'send_family_message', {'text': text, 'request_id': 'nonce'})
        for identifier in ('', 'a' * 101, 'with space', '../other', 'секрет', 12, None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.protocol.call(self.bridge, 'send_family_message', {'text': 'fixture', 'request_id': identifier})
            with self.subTest(receipt_identifier=identifier), self.assertRaises(ValueError):
                self.protocol.call(self.bridge, 'family_message_status', {'message_id': identifier})
        self.assertEqual(self.bridge.calls, [])

    def test_unknown_tools_status_arguments_and_non_object_arguments_are_rejected(self):
        for name, arguments in [('shell', {}), ('read_history', {}), ('list_messages', {}),
                                ('family_channel_status', {'token': 'do-not-use'}),
                                ('family_channel_status', []), ('send_family_message', None)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.protocol.call(self.bridge, name, arguments)
        self.assertEqual(self.bridge.calls, [])

    def test_static_telegram_error_code_does_not_expose_exception_details(self):
        self.bridge.failure = TelegramErrorFixture('not_configured')
        responses, body = self.exchange(self.request('family_channel_status', {}))
        self.assertTrue(responses[0]['result']['isError'])
        self.assertEqual(self.content(responses[0]), {'error': 'not_configured'})
        self.assertNotIn('private exception detail', body)

    def test_unexpected_exception_and_invalid_request_errors_never_echo_input(self):
        secret = 'SYNTHETIC_PRIVATE_EXCEPTION_VALUE'
        self.bridge.failure = RuntimeError(secret)
        responses, body = self.exchange(self.request('send_family_message', {'text': 'fixture', 'request_id': 'nonce'}))
        self.assertEqual(self.content(responses[0]), {'error': 'family_operation_failed'})
        self.assertTrue(responses[0]['result']['isError'])
        self.assertNotIn(secret, body)
        responses, body = self.exchange(raw='not-json-' + secret + '\n[]\n')
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(r['error']['code'] == -32600 for r in responses))
        self.assertNotIn(secret, body)

    def test_unknown_rpc_method_is_framed_without_bridge_access(self):
        responses, _ = self.exchange({'jsonrpc': '2.0', 'id': 'fixture_rpc', 'method': 'unknown_method'})
        self.assertEqual(responses[0]['id'], 'fixture_rpc')
        self.assertEqual(responses[0]['error']['code'], -32601)
        self.assertEqual(self.bridge.calls, [])

    def test_uncertain_receipt_never_becomes_accepted_or_read(self):
        self.bridge.receipt.update(status='uncertain', accepted_by_telegram=False, worker_state='not_needed')
        responses, _ = self.exchange(self.request('family_message_status', {'message_id': 'message_fixture'}))
        receipt = self.content(responses[0])
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertFalse(receipt['accepted_by_telegram'])
        self.assertFalse(receipt['delivery_confirmed'])
        self.assertFalse(receipt['read_confirmed'])


if __name__ == '__main__':
    unittest.main()
