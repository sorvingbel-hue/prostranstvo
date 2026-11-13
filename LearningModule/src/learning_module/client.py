"""Bounded local clients for the learning module's versioned JSON protocol.

Importing this module does not launch processes or connect to a service.
There are no shell commands, network listeners, or automatic request retries.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import uuid


MAX_MESSAGE_BYTES = 65536
MAX_REQUEST_BYTES = 32768
SCHEMA_VERSION = "1.0"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_STATUSES = frozenset(("completed", "needs_clarification", "unsupported",
                       "cancelled", "conflict", "unavailable", "failed"))


class ClientError(Exception):
    """A stable, non-sensitive code; server/OS exception text is never exposed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("nonfinite_number")


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ClientError("invalid_message") from None
    if len(raw) + 1 > MAX_MESSAGE_BYTES:
        raise ClientError("message_too_large")
    return raw + b"\n"


def _request(operation, params, expected_revision, request_id):
    if not isinstance(operation, str) or not 1 <= len(operation) <= 64:
        raise ClientError("invalid_operation")
    if params is None:
        params = {}
    if type(params) is not dict:
        raise ClientError("invalid_params")
    if request_id is None:
        request_id = uuid.uuid4().hex
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ClientError("invalid_request_id")
    message = dict(schema_version=SCHEMA_VERSION, request_id=request_id,
                   operation=operation, params=params)
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ClientError("invalid_revision")
        message["expected_revision"] = expected_revision
    # A JSON round trip takes a bounded snapshot of mutable caller arguments.
    raw = _encoded(message)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ClientError("message_too_large")
    return json.loads(raw, object_pairs_hook=_object), raw


def _response(raw, request_id):
    if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
        raise ClientError("response_too_large")
    if not raw.endswith(b"\n"):
        raise ClientError("incomplete_response")
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ClientError("invalid_response") from None
    if type(value) is not dict or value.get("schema_version") != SCHEMA_VERSION:
        raise ClientError("incompatible_response")
    if value.get("request_id") != request_id:
        raise ClientError("response_id_mismatch")
    if value.get("status") not in _STATUSES:
        raise ClientError("invalid_response")
    if "session_revision" not in value:
        raise ClientError("invalid_response")
    revision = value.get("session_revision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ClientError("invalid_response")
    version = value.get("module_version")
    if not isinstance(version, str) or not 1 <= len(version) <= 80:
        raise ClientError("invalid_response")
    if (type(value.get("result")) is not dict
            or type(value.get("evidence")) is not list
            or type(value.get("limitations")) is not list
            or len(value["evidence"]) > 64 or len(value["limitations"]) > 64):
        raise ClientError("invalid_response")
    return value


class InProcessClient:
    """The same contract for a trusted, bounded, local service object."""

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()

    def request(self, operation, params=None, expected_revision=None, request_id=None):
        message, _ = _request(operation, params, expected_revision, request_id)
        with self._lock:
            try:
                answer = self.service.handle(message)
            except Exception:
                raise ClientError("service_failed") from None
        try:
            raw = _encoded(answer)
        except ClientError:
            raise ClientError("invalid_response") from None
        return _response(raw, message["request_id"])

    def close(self):
        # The owner of an injected service owns its storage/lifecycle.
        return None


class StdioClient:
    """One owned local subprocess, one in-flight request, bounded pipe traffic.

    A timeout or malformed response permanently closes this client. The result
    of an interrupted mutating request is unknown: callers must not silently
    resubmit it using a fresh request ID. No retry is performed here.
    """

    def __init__(self, argv, *, timeout=5.0, cwd=None):
        if (type(argv) is not list or not argv or len(argv) > 64
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)
                or sum(map(len, argv)) > 16384):
            raise ClientError("invalid_argv")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0.05 <= timeout <= 30):
            raise ClientError("invalid_timeout")
        self.argv = tuple(argv)
        self.timeout = float(timeout)
        self.cwd = cwd
        self._process = None
        self._reader_thread = None
        self._writer_thread = None
        self._closed = threading.Event()
        self._responses = queue.Queue(maxsize=1)
        self._request_lock = threading.Lock()

    @property
    def process_id(self):
        return None if self._process is None else self._process.pid

    def _put(self, item):
        while not self._closed.is_set():
            try:
                self._responses.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _read_stdout(self):
        try:
            while not self._closed.is_set():
                raw = self._process.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    self._put(("error", "process_closed"))
                    return
                if len(raw) > MAX_MESSAGE_BYTES:
                    self._put(("error", "response_too_large"))
                    return
                self._put(("raw", raw))
        except (OSError, ValueError):
            self._put(("error", "process_closed"))

    def _start(self):
        if self._closed.is_set():
            raise ClientError("client_closed")
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, shell=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except (OSError, ValueError):
            self._closed.set()
            raise ClientError("process_start_failed") from None
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True,
                                               name="learning-stdio-reader")
        self._reader_thread.start()

    def _write(self, raw, completed, errors):
        try:
            remaining = memoryview(raw)
            while remaining and not self._closed.is_set():
                written = self._process.stdin.write(remaining)
                if not written:
                    raise OSError()
                remaining = remaining[written:]
            if remaining:
                raise OSError()
            self._process.stdin.flush()
        except (OSError, ValueError, TypeError):
            errors.append("process_write_failed")
        finally:
            completed.set()

    def request(self, operation, params=None, expected_revision=None, request_id=None):
        message, raw = _request(operation, params, expected_revision, request_id)
        with self._request_lock:
            self._start()
            deadline = time.monotonic() + self.timeout
            completed = threading.Event()
            errors = []
            self._writer_thread = threading.Thread(target=self._write,
                args=(raw, completed, errors), daemon=True, name="learning-stdio-writer")
            self._writer_thread.start()
            if not completed.wait(max(0, deadline - time.monotonic())):
                self.close()
                raise ClientError("request_timeout")
            if errors:
                self.close()
                raise ClientError(errors[0])
            try:
                kind, value = self._responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise ClientError("request_timeout") from None
            if kind != "raw":
                self.close()
                raise ClientError(value)
            try:
                return _response(value, message["request_id"])
            except ClientError:
                self.close()
                raise

    def close(self):
        self._closed.set()
        process = self._process
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass
            for thread in (self._writer_thread, self._reader_thread):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=0.5)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
