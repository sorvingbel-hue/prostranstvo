"""Security channel tests: inert reporter and in-memory stdio, no OS queries."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PRODUCT = Path(__file__).resolve().parents[1]
REPORT_ID = "a" * 32
PAYLOAD = {"report": {"report_id": REPORT_ID},
           "role_instructions": "Synthetic sanitized report fixture.",
           "freshness": {"age_seconds": 0, "stale": False},
           "execution_available": False, "network_available": False}


class FakeReporter:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.payload = PAYLOAD

    def collect(self):
        self.calls.append(("collect",))
        if self.failure:
            raise self.failure
        return {"report_id": REPORT_ID}

    def for_ai(self, report_id=None):
        self.calls.append(("for_ai", report_id))
        if self.failure:
            raise self.failure
        return self.payload


class SecurityMcpTests(unittest.TestCase):
    def setUp(self):
        backend = types.ModuleType("security_report")
        self.default_reporter = backend.SecurityReport = mock.Mock(side_effect=AssertionError("unexpected backend construction"))
        patch = mock.patch.dict(sys.modules, {"security_report": backend})
        patch.start()
        self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location("isolated_security_mcp", PRODUCT / "src/security_mcp.py")
        self.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.protocol)
        self.reporter = FakeReporter()

    @staticmethod
    def request(name, arguments=None, ident=1):
        return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": name, "arguments": {} if arguments is None else arguments}}

    def exchange(self, *requests, raw=None, input_stream=None):
        if input_stream is None:
            raw = raw if raw is not None else "".join(json.dumps(r) + "\n" for r in requests)
            input_stream = io.StringIO(raw)
        output = io.StringIO()
        self.protocol.main(input_stream, output, self.reporter)
        text = output.getvalue()
        self.assertTrue(not text or text.endswith("\n"))
        return [json.loads(line) for line in text.splitlines()], text

    @staticmethod
    def content(response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_import_and_handshake_never_construct_or_call_reporter(self):
        self.default_reporter.assert_not_called()
        responses, _ = self.exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "prostranstvo-security-report")
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(responses[2]["result"]["tools"], self.protocol.TOOLS)
        self.assertEqual(self.reporter.calls, [])
        # Even main without an injected reporter remains inert for tools/list.
        self.protocol.main(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'), io.StringIO())
        self.default_reporter.assert_not_called()

    def test_only_two_fixed_tools_with_closed_schemas_and_system_readonly_hints(self):
        tools = {tool["name"]: tool for tool in self.protocol.TOOLS}
        self.assertEqual(set(tools), {"collect_security_report", "get_security_report"})
        for tool in tools.values():
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
        self.assertEqual(tools["collect_security_report"]["inputSchema"]["properties"], {})
        self.assertEqual(set(tools["get_security_report"]["inputSchema"]["properties"]), {"report_id"})
        self.assertFalse(tools["collect_security_report"]["annotations"]["idempotentHint"])

    def test_collection_returns_only_pinned_for_ai_payload_and_get_never_collects(self):
        responses, _ = self.exchange(self.request("collect_security_report"),
                                     self.request("get_security_report", {"report_id": REPORT_ID}, 2),
                                     self.request("get_security_report", {}, 3))
        self.assertEqual([self.content(r) for r in responses], [PAYLOAD] * 3)
        self.assertEqual(self.reporter.calls, [("collect",), ("for_ai", REPORT_ID),
                                               ("for_ai", REPORT_ID), ("for_ai", None)])

    def test_arbitrary_os_actions_and_advice_tools_are_unavailable(self):
        names = ("shell", "exec", "run_command", "read_file", "browse_files", "power_shutdown",
                 "set_firewall", "scan_defender", "submit_advice", "connect_account", "start_service")
        for name in names:
            with self.subTest(name=name):
                response, _ = self.exchange(self.request(name))
                self.assertEqual(response[0]["error"]["code"], -32602)
                with self.assertRaises(ValueError):
                    self.protocol.call(self.reporter, name, {})
        self.assertEqual(self.reporter.calls, [])

    def test_extra_arguments_paths_urls_commands_and_invalid_identifiers_are_denied(self):
        cases = [("collect_security_report", {"path": "C:/private"}),
                 ("collect_security_report", {"command": "anything"}),
                 ("get_security_report", {"url": "https://example.invalid"}),
                 ("get_security_report", {"report_id": REPORT_ID, "force": True}),
                 ("get_security_report", {"report_id": None}),
                 ("get_security_report", {"report_id": "../latest.json"}),
                 ("get_security_report", {"report_id": REPORT_ID.upper()}),
                 ("get_security_report", {"report_id": "a" * 33}),
                 ("get_security_report", []), ("collect_security_report", True)]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                response, _ = self.exchange(self.request(name, arguments))
                self.assertEqual(response[0]["error"]["code"], -32602)
                with self.assertRaises(ValueError):
                    self.protocol.call(self.reporter, name, arguments)
        self.assertEqual(self.reporter.calls, [])

    def test_malformed_duplicate_nonfinite_and_wrong_rpc_requests_do_not_collect(self):
        for raw, code in (("not-json\n", -32700), ("[]\n", -32600),
                          ('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n', -32700),
                          ('{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n', -32700),
                          ('{"jsonrpc":"1.0","id":1,"method":"ping"}\n', -32600),
                          ('{"jsonrpc":"2.0","id":true,"method":"ping"}\n', -32600),
                          ('{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}\n', -32602)):
            with self.subTest(raw=raw):
                responses, _ = self.exchange(raw=raw)
                self.assertEqual(responses[0]["error"]["code"], code)
        self.assertEqual(self.reporter.calls, [])

    def test_oversize_line_reads_one_bounded_chunk_and_ends_session(self):
        stream = io.StringIO("x" * (self.protocol.MAX_REQUEST_BYTES * 20) + "\n" + json.dumps(self.request("collect_security_report")) + "\n")
        responses, body = self.exchange(input_stream=stream)
        self.assertEqual(stream.tell(), self.protocol.MAX_REQUEST_BYTES + 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["message"], "Request too large")
        self.assertLess(len(body), 200)
        self.assertEqual(self.reporter.calls, [])

    def test_utf8_byte_limit_and_invalid_utf8_are_bounded(self):
        responses, _ = self.exchange(raw="я" * self.protocol.MAX_REQUEST_BYTES + "\n")
        self.assertEqual(responses[0]["error"]["code"], -32600)
        responses, _ = self.exchange(input_stream=io.BytesIO(b"\xff\n"))
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_notifications_and_unknown_methods_never_invoke_reporter(self):
        request = self.request("collect_security_report")
        del request["id"]
        responses, _ = self.exchange(request, {"jsonrpc": "2.0", "id": 2, "method": "os/execute"})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(self.reporter.calls, [])

    def test_missing_and_changed_reports_are_clear_and_no_exception_details_escape(self):
        for code in ("security_report_missing", "security_report_changed"):
            with self.subTest(code=code):
                self.reporter.failure = ValueError(code)
                responses, _ = self.exchange(self.request("get_security_report"))
                self.assertTrue(responses[0]["result"]["isError"])
                self.assertEqual(self.content(responses[0]), {"error": code})
        for error in (RuntimeError("C:/Users/PRIVATE_SECRET/account.txt"),
                      ValueError("C:/Users/PRIVATE_SECRET/key")):
            self.reporter.failure = error
            responses, body = self.exchange(self.request("collect_security_report"))
            self.assertEqual(self.content(responses[0]), {"error": "security_report_unavailable"})
            self.assertNotIn("PRIVATE_SECRET", body)

    def test_oversize_or_nonjson_backend_response_is_replaced_with_static_error(self):
        for payload in ({"private": "secret-fixture" * self.protocol.MAX_RESPONSE_BYTES}, {"value": float("nan")}):
            self.reporter.payload = payload
            responses, body = self.exchange(self.request("get_security_report"))
            self.assertLess(len(body.encode("utf-8")), self.protocol.MAX_RESPONSE_BYTES)
            self.assertNotIn("secret-fixture", body)
            self.assertTrue("error" in responses[0] or responses[0]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
