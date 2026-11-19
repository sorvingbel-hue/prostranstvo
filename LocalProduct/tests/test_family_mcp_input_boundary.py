"""family_mcp: bounded stdio input (same contract as mcp_server/security_mcp).

A too-large line ends the session after one bounded error; it is never fully
buffered and no following command is executed. Invalid UTF-8 fails closed.
No DB, DPAPI, Telegram, worker, process or file fixture.
"""
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]
LIMIT = 1024 * 1024


class BoundedInput(io.StringIO):
    def __init__(self, text):
        super().__init__(text)
        self.read_sizes = []

    def reconfigure(self, **kwargs):
        pass

    def readline(self, size=-1):
        self.read_sizes.append(size)
        if size != LIMIT + 1:
            raise AssertionError('family MCP attempted an unbounded input read')
        return super().readline(size)

    def __iter__(self):
        raise AssertionError('family MCP iterated stdin (unbounded line read)')


class TelegramErrorFixture(RuntimeError):
    def __init__(self, code='not_configured'):
        self.code = code
        super().__init__('private exception detail must never escape')


class FakeBridge:
    def __init__(self):
        self.calls = []

    def channel_status(self):
        self.calls.append(('status',))
        return {'transport': 'telegram', 'configured': False, 'state': 'not_configured'}

    def send_message(self, text, request_id):
        self.calls.append(('send', text, request_id))
        return {'id': 'message_fixture', 'status': 'queued'}

    def message_status(self, message_id):
        self.calls.append(('receipt', message_id))
        return {'id': message_id, 'status': 'queued'}


def send_request(ident=7):
    return json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                       'params': {'name': 'send_family_message',
                                  'arguments': {'text': 'fixture', 'request_id': 'nonce'}}}) + '\n'


class FamilyMcpInputBoundaryTests(unittest.TestCase):
    def setUp(self):
        family = types.ModuleType('family_bridge')
        family.FamilyBridge = mock.Mock(side_effect=AssertionError('unexpected default bridge construction'))
        telegram = types.ModuleType('family_telegram')
        telegram.TelegramError = TelegramErrorFixture
        self.modules = mock.patch.dict(sys.modules, {'family_bridge': family, 'family_telegram': telegram})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location('isolated_family_mcp_boundary', PRODUCT / 'src' / 'family_mcp.py')
        self.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.protocol)
        self.bridge = FakeBridge()

    def serve(self, incoming):
        output = io.StringIO()
        self.protocol.main(incoming, output, self.bridge)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_normal_requests_use_bounded_reads(self):
        incoming = BoundedInput('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        rows = self.serve(incoming)
        self.assertEqual(rows, [{'jsonrpc': '2.0', 'id': 1, 'result': {}}])
        self.assertEqual(incoming.read_sizes, [LIMIT + 1, LIMIT + 1])
        self.assertEqual(self.bridge.calls, [])

    def test_oversized_input_closes_before_following_command(self):
        incoming = BoundedInput('x' * (LIMIT + 1) + '\n' + send_request())
        rows = self.serve(incoming)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['error']['message'], 'Request too large')
        self.assertIsNone(rows[0]['id'])
        self.assertEqual(incoming.read_sizes, [LIMIT + 1])
        self.assertEqual(self.bridge.calls, [])

    def test_exact_limit_is_accepted(self):
        raw = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
        text = raw + ' ' * (LIMIT - len(raw) - 1) + '\n'
        self.assertEqual(len(text), LIMIT)
        rows = self.serve(BoundedInput(text + send_request(2)))
        self.assertEqual([r['id'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['result'], {})
        self.assertEqual(self.bridge.calls, [('send', 'fixture', 'nonce')])

    def test_invalid_utf8_fails_closed_without_traceback(self):
        incoming = io.TextIOWrapper(io.BytesIO(b'\xff\xfe\n' + send_request().encode('utf-8')), encoding='utf-8')
        rows = self.serve(incoming)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['error']['code'], -32700)
        self.assertEqual(self.bridge.calls, [])


if __name__ == '__main__':
    unittest.main()
