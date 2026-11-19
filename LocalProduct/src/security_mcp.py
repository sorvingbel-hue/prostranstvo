"""One-report stdio MCP surface. No listener, commands, paths or system actions.

Importing this module does not construct a reporter, read streams or observe the
computer. Only an explicit tool call can request the fixed local snapshot.
"""
from __future__ import annotations

import json
import re
import sys


MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 96 * 1024
_REPORT_ID = re.compile(r"[0-9a-f]{32}\Z")
_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_ERRORS = frozenset(("security_report_missing", "security_report_changed",
                     "security_report_unavailable", "security_report_too_large",
                     "invalid_security_report", "unsafe_security_report_path",
                     "security_report_store_unavailable", "invalid_security_clock"))
TOOLS = [
    {"name": "collect_security_report",
     "description": ("По явному запросу собрать один ограниченный локальный отчёт безопасности и вернуть его очищенную версию. "
                     "Читает фиксированные системные показатели и сохраняет только собственный отчёт; "
                     "не меняет настройки, не запускает сканирование и не подтверждает передачу удалённому сервису."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False}},
    {"name": "get_security_report",
     "description": ("Прочитать только последний очищенный локальный отчёт. Не собирает данные заново. "
                     "Необязательный report_id проверяет, что сохранён именно ожидаемый отчёт; истории отчётов нет."),
     "inputSchema": {"type": "object", "properties": {
         "report_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False}},
]


class SecurityMcp:
    def __init__(self, reporter=None):
        self.reporter = reporter

    def _backend(self):
        if self.reporter is None:
            from security_report import SecurityReport
            self.reporter = SecurityReport()
        return self.reporter

    def call(self, name, arguments):
        if type(name) is not str or type(arguments) is not dict:
            raise ValueError("invalid_arguments")
        if name == "collect_security_report" and not arguments:
            reporter = self._backend()
            collected = reporter.collect()
            # Pin the returned observation: a concurrent replacement is an error,
            # never an implicit acknowledgement of a different report.
            identifier = collected.get("report_id") if type(collected) is dict else None
            if type(identifier) is not str or _REPORT_ID.fullmatch(identifier) is None:
                raise ValueError("invalid_security_report")
            return reporter.for_ai(identifier)
        if name == "get_security_report" and not (set(arguments) - {"report_id"}):
            identifier = arguments.get("report_id")
            if "report_id" in arguments and (type(identifier) is not str or _REPORT_ID.fullmatch(identifier) is None):
                raise ValueError("invalid_arguments")
            return self._backend().for_ai(identifier)
        raise ValueError("unknown_tool_or_arguments")


def call(reporter, name, arguments):
    """Small direct-call adapter; the stdio loop additionally frames safe errors."""
    return SecurityMcp(reporter).call(name, arguments)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("invalid_constant")


def _identifier(value):
    return (value is None or type(value) is int and -(2 ** 53) < value < 2 ** 53
            or type(value) is str and len(value) <= 128)


def _error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _tool_error(error):
    code = error.args[0] if type(error) is ValueError and len(error.args) == 1 else None
    if type(code) is not str or code not in _ERRORS:
        code = "security_report_unavailable"
    return {"isError": True, "content": [{"type": "text", "text": json.dumps({"error": code})}]}


def _dispatch(server, request):
    if (type(request) is not dict or set(request) - {"jsonrpc", "id", "method", "params"}
            or request.get("jsonrpc") != "2.0" or type(request.get("method")) is not str
            or not 1 <= len(request["method"]) <= 128
            or ("id" in request and not _identifier(request["id"]))):
        return _error(None, -32600, "Invalid request")
    # Notifications can never collect data or invoke tools.
    if "id" not in request:
        return None
    ident, method, params = request["id"], request["method"], request.get("params", {})
    if type(params) is not dict:
        return _error(ident, -32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion", "2024-11-05")
        if (set(params) - {"protocolVersion", "capabilities", "clientInfo"}
                or type(version) is not str or re.fullmatch(r"\d{4}-\d\d-\d\d", version) is None
                or ("capabilities" in params and type(params["capabilities"]) is not dict)
                or ("clientInfo" in params and type(params["clientInfo"]) is not dict)):
            return _error(ident, -32602, "Invalid params")
        result = {"protocolVersion": version if version in _VERSIONS else _VERSIONS[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "prostranstvo-security-report", "version": "1.0.0"},
                  "instructions": ("Use only the two fixed security-report tools. A successful collection means a local report was saved; "
                                   "it does not confirm remote delivery. Check freshness and unknown_reason. This report does not establish "
                                   "compromise or safety. No command execution, file browsing, settings changes, advice submission, "
                                   "network service or account connection is available.")}
    elif method in ("ping", "tools/list"):
        if params:
            return _error(ident, -32602, "Invalid params")
        result = {} if method == "ping" else {"tools": TOOLS}
    elif method == "tools/call":
        if (set(params) - {"name", "arguments"} or type(params.get("name")) is not str
                or params["name"] not in ("collect_security_report", "get_security_report")):
            return _error(ident, -32602, "Invalid params")
        arguments = params.get("arguments", {})
        name = params["name"]
        if (type(arguments) is not dict or (name == "collect_security_report" and arguments)
                or (name == "get_security_report" and (set(arguments) - {"report_id"}
                    or ("report_id" in arguments and (type(arguments["report_id"]) is not str
                        or _REPORT_ID.fullmatch(arguments["report_id"]) is None))))):
            return _error(ident, -32602, "Invalid params")
        try:
            payload = server.call(name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, allow_nan=False)}]}
        except Exception as error:
            result = _tool_error(error)
    else:
        return _error(ident, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _write(output_stream, response):
    try:
        text = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            text = json.dumps(_error(response.get("id"), -32603, "Response unavailable"), separators=(",", ":"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        text = json.dumps(_error(None, -32603, "Response unavailable"), separators=(",", ":"))
    try:
        output_stream.write(text + "\n")
        output_stream.flush()
        return True
    except (OSError, ValueError):
        return False


def main(input_stream=None, output_stream=None, reporter=None):
    """Serve newline-delimited JSON on the owning client's stdio only.

    A too-large line ends the session after one bounded error. It is never fully
    buffered or drained, and no prefix of it is interpreted as a command.
    """
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    for stream in (input_stream, output_stream):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    server = SecurityMcp(reporter)
    while True:
        try:
            line = input_stream.readline(MAX_REQUEST_BYTES + 1)
        except (OSError, UnicodeError, ValueError):
            _write(output_stream, _error(None, -32700, "Parse error"))
            return
        if not line:
            return
        try:
            raw = line.encode("utf-8") if type(line) is str else line
            if len(raw) > MAX_REQUEST_BYTES:
                _write(output_stream, _error(None, -32600, "Request too large"))
                return
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            response = _error(None, -32700, "Parse error")
        else:
            response = _dispatch(server, request)
        if response is not None and not _write(output_stream, response):
            return


if __name__ == "__main__":
    main()
