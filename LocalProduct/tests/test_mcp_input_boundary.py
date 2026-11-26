"""Protocol-only regressions: no executor data, OS service, network or GUI."""
import io
import json
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1]
LIMIT = 1024 * 1024


class Stream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


class BoundedInput(Stream):
    def __init__(self, text):
        super().__init__(text)
        self.read_sizes = []

    def readline(self, size=-1):
        self.read_sizes.append(size)
        if size != LIMIT + 1:
            raise AssertionError('MCP attempted an unbounded input read')
        return super().readline(size)


def backends():
    result = [PRODUCT / 'src' / 'mcp_server.py']
    legacy = PRODUCT.parent / 'WindowsCompanion' / 'mcp_server.py'
    if legacy.is_file():
        result.append(legacy)
    return result


def serve(path, incoming):
    calls = []
    engine = types.ModuleType('engine')
    engine.Executor = lambda: types.SimpleNamespace(status=lambda: {'ok': True})
    broker = types.ModuleType('system_broker')
    broker.request = lambda command: calls.append(command) or {'accepted': True}
    version = types.ModuleType('version')
    version.VERSION = 'fixture'
    modules = {'engine': engine, 'system_broker': broker, 'version': version}
    for name in ('agent_apps', 'agent_control'):
        module = types.ModuleType(name)
        module.TOOLS, module.TOOL_NAMES = [], ()
        module.call = lambda *args: (_ for _ in ()).throw(AssertionError('Unexpected app call'))
        modules[name] = module
    output = Stream()
    with patch.dict(sys.modules, modules), patch.object(sys, 'stdin', incoming), patch.object(sys, 'stdout', output):
        state = runpy.run_path(str(path), run_name='__main__')
    return [json.loads(line) for line in output.getvalue().splitlines()], calls, state


def request(command):
    return json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                       'params': {'name': 'system_maintenance', 'arguments': {'command': command}}}) + '\n'


class McpInputBoundaryTests(unittest.TestCase):
    def test_normal_requests_use_bounded_reads(self):
        for path in backends():
            with self.subTest(path=path.parent.name):
                incoming = BoundedInput('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
                rows, calls, _ = serve(path, incoming)
                self.assertEqual(rows, [{'jsonrpc': '2.0', 'id': 1, 'result': {}}])
                self.assertEqual(calls, [])
                self.assertEqual(incoming.read_sizes, [LIMIT + 1, LIMIT + 1])

    def test_oversized_input_closes_before_following_command(self):
        for path in backends():
            with self.subTest(path=path.parent.name):
                incoming = BoundedInput('x' * (LIMIT + 1) + '\n' + request('health'))
                rows, calls, _ = serve(path, incoming)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['error']['message'], 'Request too large')
                self.assertEqual(calls, [])
                self.assertEqual(incoming.read_sizes, [LIMIT + 1])

    def test_exact_limit_is_accepted(self):
        raw = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
        incoming_text = raw + ' ' * (LIMIT - len(raw) - 1) + '\n'
        self.assertEqual(len(incoming_text), LIMIT)
        for path in backends():
            with self.subTest(path=path.parent.name):
                rows, calls, _ = serve(path, BoundedInput(incoming_text))
                self.assertEqual(rows[0]['result'], {})
                self.assertEqual(calls, [])

    def test_system_mutations_are_rejected_before_broker(self):
        for path in backends():
            for command in ('repair', 'cleanup', 'health x', '', None, ['health']):
                with self.subTest(backend=path.parent.name, command=command):
                    rows, calls, _ = serve(path, Stream(request(command)))
                    self.assertTrue(rows[0]['result']['isError'])
                    if path.parent.name == 'WindowsCompanion':
                        self.assertIn('needs_local_parent_approval', rows[0]['result']['content'][0]['text'])
                    else:
                        self.assertIn('Unknown tool', rows[0]['result']['content'][0]['text'])
                    self.assertEqual(calls, [])

    def test_system_tool_scope_is_explicit(self):
        portable = PRODUCT / 'src' / 'mcp_server.py'
        _, calls, state = serve(portable, Stream(''))
        self.assertNotIn('system_maintenance', [item['name'] for item in state['TOOLS']])
        self.assertEqual(calls, [])
        legacy = PRODUCT.parent / 'WindowsCompanion' / 'mcp_server.py'
        if not legacy.is_file():
            return
        for command in ('diagnostics', 'health', 'scan'):
            with self.subTest(command=command):
                rows, calls, state = serve(legacy, Stream(request(command)))
                self.assertEqual(calls, [command])
                self.assertFalse(rows[0]['result'].get('isError', False))
                tool = next(item for item in state['TOOLS'] if item['name'] == 'system_maintenance')
                self.assertEqual(tool['inputSchema']['properties']['command']['enum'],
                                 ['diagnostics', 'health', 'scan'])
                self.assertTrue(tool['annotations']['destructiveHint'])


if __name__ == '__main__':
    unittest.main()
