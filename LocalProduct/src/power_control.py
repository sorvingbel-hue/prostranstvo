"""Explicit local power requests; importing, planning and scheduling are inert.

The caller must authenticate the parent and obtain confirmation before schedule
and start_worker. A caller-supplied boolean is not authentication. This module
does not register startup tasks, elevate privileges or use the SYSTEM broker.

The cancellable countdown is local. Only when it finishes do we call the fixed
Windows shutdown.exe with /r or /s and /t 0, never /f or /a. Microsoft documents
that /t greater than zero implies /f:
https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown
No forced close is requested, but this is still a real power operation: callers
must ask the user to save work. Dispatched means command acceptance, not that
Windows finished restarting. Cancellation cannot undo a dispatched operation.
Interrupted execution is uncertain and is never retried automatically.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links

VERSION = 1
DEFAULT_DELAY = 60
MAX_DELAY = 24 * 60 * 60
MAX_BYTES = 64 * 1024
AUDIT_LIMIT = 32
LAUNCH_GRACE_SECONDS = 15
ID = re.compile(r"[0-9a-f]{32}\Z")
ACTIONS = {"restart": "/r", "shutdown": "/s"}
TERMINAL = frozenset(("dispatched", "failed", "cancelled", "uncertain"))
STATES = TERMINAL | {"queued", "executing"}
RECORD_FIELDS = frozenset((
    "request_id", "action", "delay_seconds", "created_utc", "deadline_utc",
    "updated_utc", "state", "error_code", "worker_id", "worker_started_utc",
    "worker_claimed",
))
AUDIT_FIELDS = frozenset(("request_id", "action", "state", "updated_utc", "error_code"))


class PowerControlError(ValueError):
    """Static error codes only; no subprocess output or personal data."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 253402300799


def _identifier(value):
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise PowerControlError("invalid_request_id")
    return value


def _options(action, delay_seconds):
    if not isinstance(action, str) or action not in ACTIONS:
        raise PowerControlError("invalid_action")
    if type(delay_seconds) is not int or not 1 <= delay_seconds <= MAX_DELAY:
        raise PowerControlError("invalid_delay")


def plan(action, delay_seconds=DEFAULT_DELAY):
    """Validate a proposal without reading files, a clock, or Windows state."""
    _options(action, delay_seconds)
    return {"action": action, "delay_seconds": delay_seconds, "force": False,
            "cancellable": True, "save_work_required": True}


def _default_root():
    import paths
    base = getattr(paths, "DATA_ROOT", None)
    if base is None:
        base = Path(paths.DEPLOY_ROOT) / "data"
    return Path(base) / "power-control"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PowerControlError("invalid_state")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise PowerControlError("invalid_state")


def _validate_record(value):
    if type(value) is not dict or value.keys() != RECORD_FIELDS:
        raise PowerControlError("invalid_state")
    _identifier(value["request_id"])
    _options(value["action"], value["delay_seconds"])
    if (not isinstance(value["state"], str) or value["state"] not in STATES or
            not all(_number(value[k]) for k in ("created_utc", "deadline_utc", "updated_utc")) or
            value["deadline_utc"] != value["created_utc"] + value["delay_seconds"] or
            value["updated_utc"] < value["created_utc"] or
            type(value["worker_claimed"]) is not bool):
        raise PowerControlError("invalid_state")
    error = value["error_code"]
    if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z_]{1,64}", error)):
        raise PowerControlError("invalid_state")
    if (value["state"] in {"queued", "executing", "dispatched", "cancelled"}) != (error is None):
        raise PowerControlError("invalid_state")
    if value["worker_id"] is None:
        if value["worker_started_utc"] is not None or value["worker_claimed"]:
            raise PowerControlError("invalid_state")
    else:
        _identifier(value["worker_id"])
        if not _number(value["worker_started_utc"]) or value["worker_started_utc"] < value["created_utc"]:
            raise PowerControlError("invalid_state")
    return value


def _validate(value):
    if (type(value) is not dict or set(value) != {"version", "current", "audit"} or
            type(value["version"]) is not int or value["version"] != VERSION or
            type(value["audit"]) is not list or len(value["audit"]) > AUDIT_LIMIT):
        raise PowerControlError("invalid_state")
    if value["current"] is not None:
        _validate_record(value["current"])
    for item in value["audit"]:
        if type(item) is not dict or item.keys() != AUDIT_FIELDS:
            raise PowerControlError("invalid_state")
        _identifier(item["request_id"])
        if (not isinstance(item["action"], str) or item["action"] not in ACTIONS or
                not isinstance(item["state"], str) or item["state"] not in TERMINAL or
                not _number(item["updated_utc"])):
            raise PowerControlError("invalid_state")
        if item["error_code"] is not None and (
                not isinstance(item["error_code"], str) or not re.fullmatch(r"[a-z_]{1,64}", item["error_code"])):
            raise PowerControlError("invalid_state")
    return value


def _shutdown_executable():
    """Resolve System32 using Windows, never PATH, cwd, or an environment value."""
    if os.name != "nt":
        raise PowerControlError("windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise PowerControlError("system_directory_unavailable")
    path = Path(buffer.value) / "shutdown.exe"
    # Windows owns System32; a serviced system file can have component-store
    # hardlinks. Reject reparse points, not legitimate Windows hardlinks.
    no_links(path)
    if not path.is_absolute() or not path.is_file():
        raise PowerControlError("shutdown_unavailable")
    return path


class PowerControl:
    def __init__(self, root=None, clock=time.time, runner=subprocess.run):
        self.root = Path(root if root is not None else _default_root()).absolute()
        self.path = self.root / "requests.json"
        self.lock_path = self.root / "requests.lock"
        self.execution_lock = self.root / "execution.lock"
        self.worker_lock = self.root / "worker.lock"
        self.clock = clock
        self.runner = runner

    @staticmethod
    def plan(action, delay_seconds=DEFAULT_DELAY):
        return plan(action, delay_seconds)

    def _now(self):
        value = self.clock()
        if not _number(value):
            raise PowerControlError("invalid_clock")
        return value

    def _read(self):
        no_links(self.path, self.path.exists())
        try:
            with self.path.open("rb") as stream:
                body = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {"version": VERSION, "current": None, "audit": []}
        if len(body) > MAX_BYTES:
            raise PowerControlError("invalid_state")
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise PowerControlError("invalid_state") from None
        return _validate(value)

    def _write(self, value):
        _validate(value)
        no_links(self.root)
        no_links(self.path, self.path.exists())
        descriptor, temporary = tempfile.mkstemp(prefix=".power-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(self.path, self.path.exists())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _transaction(self):
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with ExitStack() as stack:
            deadline = time.monotonic() + 3
            while True:
                try:
                    stack.enter_context(exclusive_lock(self.lock_path))
                    break
                except PermissionError as error:
                    # Windows may reject the initial byte read of a locked file.
                    if (os.name != "nt" or error.errno != errno.EACCES or
                            error.filename is not None or time.monotonic() >= deadline):
                        raise
                    time.sleep(0.01)
            value = self._read()
            yield value

    @staticmethod
    def _held(path):
        if not path.exists():
            return False
        try:
            with exclusive_lock(path, blocking=False):
                return False
        except (RuntimeError, PermissionError):
            return True

    def _recover(self, value):
        record = value["current"]
        if record is None:
            return
        abandoned = record["state"] == "executing" and not self._held(self.execution_lock)
        if record["state"] == "queued" and record["worker_id"] is not None:
            claimed_or_late = (record["worker_claimed"] or
                               self._now() - record["worker_started_utc"] > LAUNCH_GRACE_SECONDS)
            abandoned = claimed_or_late and not self._held(self.worker_lock)
        if abandoned:
            self._finish(value, "uncertain", "worker_interrupted")

    def _finish(self, value, state, error_code=None):
        record = value["current"]
        record.update(state=state, error_code=error_code,
                      updated_utc=max(record["updated_utc"], self._now()))
        self._write(value)

    def _snapshot(self, record):
        if record is None:
            return {"state": "idle", "request_id": None, "remaining_seconds": 0, "can_cancel": False}
        result = {k: v for k, v in record.items() if k not in {"worker_id", "worker_started_utc", "worker_claimed"}}
        result["remaining_seconds"] = (max(0, math.ceil(record["deadline_utc"] - self._now()))
                                       if record["state"] == "queued" else 0)
        result["can_cancel"] = record["state"] == "queued"
        return result

    @staticmethod
    def _matching(value, request_id):
        record = value["current"]
        if record is None or record["request_id"] != request_id:
            raise PowerControlError("request_not_found")
        return record

    def status(self):
        if not self.path.exists():
            no_links(self.path)
            return self._snapshot(None)
        with self._transaction() as value:
            self._recover(value)
            return self._snapshot(value["current"])

    def schedule(self, action, delay_seconds=DEFAULT_DELAY):
        plan(action, delay_seconds)
        now = self._now()
        with self._transaction() as value:
            self._recover(value)
            old = value["current"]
            if old is not None:
                if old["state"] not in TERMINAL:
                    raise PowerControlError("request_pending")
                value["audit"].append({key: old[key] for key in AUDIT_FIELDS})
                value["audit"] = value["audit"][-AUDIT_LIMIT:]
            value["current"] = {
                "request_id": uuid.uuid4().hex, "action": action, "delay_seconds": delay_seconds,
                "created_utc": now, "deadline_utc": now + delay_seconds, "updated_utc": now,
                "state": "queued", "error_code": None, "worker_id": None,
                "worker_started_utc": None, "worker_claimed": False,
            }
            self._write(value)
            return self._snapshot(value["current"])

    def cancel(self, request_id):
        _identifier(request_id)
        with self._transaction() as value:
            self._recover(value)
            record = self._matching(value, request_id)
            if record["state"] == "queued":
                self._finish(value, "cancelled")
            elif record["state"] != "cancelled":
                raise PowerControlError("request_not_cancellable")
            return self._snapshot(record)

    def execute_due(self, request_id):
        """Explicit worker step. A due request is dispatched at most once.

        No implicit retry is safe after the executing barrier. Callers must not
        use this method as an unauthenticated remote power-control endpoint.
        """
        _identifier(request_id)
        if not self.path.exists():
            raise PowerControlError("request_not_found")
        try:
            with exclusive_lock(self.execution_lock, blocking=False):
                return self._execute_locked(request_id)
        except (RuntimeError, PermissionError):
            raise PowerControlError("execution_busy") from None

    def _execute_locked(self, request_id):
        with self._transaction() as value:
            self._recover(value)
            record = self._matching(value, request_id)
            if record["state"] == "executing":
                self._finish(value, "uncertain", "execution_interrupted")
                return self._snapshot(record)
            if record["state"] != "queued" or self._now() < record["deadline_utc"]:
                return self._snapshot(record)
            self._finish(value, "executing")
            action = record["action"]
        try:
            command = [str(_shutdown_executable()), ACTIONS[action], "/t", "0"]
            result = self.runner(command, shell=False, check=False, capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if type(result.returncode) is not int:
                state, error = "uncertain", "invalid_runner_result"
            elif result.returncode == 0:
                state, error = "dispatched", None
            else:
                state, error = "failed", "shutdown_rejected"
        except (FileNotFoundError, PermissionError, PowerControlError):
            state, error = "failed", "shutdown_unavailable"
        except Exception:
            state, error = "uncertain", "dispatch_uncertain"
        with self._transaction() as value:
            record = self._matching(value, request_id)
            self._finish(value, state, error)
            return self._snapshot(record)

    def start_worker(self, request_id, *, popen=None):
        """Explicitly spawn our own worker. Never called by schedule/status.

        There is no Windows startup entry. A stopped worker cannot be restarted
        to resume a stale countdown; cancel and request fresh parent approval.
        The worker ID is a concurrency ticket, not a parent-authentication token.
        """
        _identifier(request_id)
        with self._transaction() as value:
            self._recover(value)
            record = self._matching(value, request_id)
            if record["state"] != "queued" or record["worker_id"] is not None:
                raise PowerControlError("worker_not_startable")
            worker_id = uuid.uuid4().hex
            record.update(worker_id=worker_id, worker_started_utc=self._now())
            self._write(value)
        try:
            command = self._worker_command(request_id, worker_id)
            (popen or subprocess.Popen)(command, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            with self._transaction() as value:
                record = self._matching(value, request_id)
                if record["state"] == "queued" and record["worker_id"] == worker_id:
                    self._finish(value, "failed", "worker_start_failed")
            return self.status()
        return self.status()

    def _worker_command(self, request_id, worker_id):
        interpreter = Path(sys.executable).resolve()
        no_links(interpreter, True)
        if getattr(sys, "frozen", False):
            # Frozen entry uses only its own normal data root, never a caller
            # supplied filesystem path or arbitrary dispatcher arguments.
            if self.root != _default_root().absolute():
                raise PowerControlError("worker_root_mismatch")
            return [str(interpreter), "power-worker", "--request-id", request_id,
                    "--worker-id", worker_id]
        script = Path(__file__).resolve()
        no_links(script, True)
        return [str(interpreter), "-B", str(script), "--worker", request_id,
                "--worker-id", worker_id, "--root", str(self.root)]

    def run_worker(self, request_id, worker_id, *, sleeper=time.sleep):
        """Claim a single-use worker ticket, then wait on the local countdown."""
        _identifier(request_id)
        _identifier(worker_id)
        if not self.path.exists():
            raise PowerControlError("request_not_found")
        try:
            with exclusive_lock(self.worker_lock, blocking=False):
                with self._transaction() as value:
                    record = self._matching(value, request_id)
                    if (record["state"] != "queued" or record["worker_id"] != worker_id or
                            record["worker_claimed"]):
                        raise PowerControlError("worker_not_startable")
                    if self._now() - record["worker_started_utc"] > LAUNCH_GRACE_SECONDS:
                        self._finish(value, "uncertain", "worker_start_expired")
                        return self._snapshot(record)
                    record["worker_claimed"] = True
                    self._write(value)
                while True:
                    state = self.status()
                    if state["request_id"] != request_id or state["state"] != "queued":
                        return state
                    if state["remaining_seconds"] == 0:
                        return self.execute_due(request_id)
                    sleeper(min(0.25, state["remaining_seconds"]))
        except (RuntimeError, PermissionError):
            raise PowerControlError("worker_busy") from None


def start_worker(request_id, root=None):
    return PowerControl(root).start_worker(request_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit local power countdown worker", allow_abbrev=False)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    try:
        value = PowerControl(args.root).run_worker(args.worker, args.worker_id)
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value["state"] in {"dispatched", "cancelled"} else 1
    except (PowerControlError, OSError, RuntimeError):
        print('{"state":"failed","error_code":"worker_unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
