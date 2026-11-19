"""Finite, explicitly requested local storage audits; no constructor side effects.

Only this module's own worker command is launched. The scanner remains the
metadata-only StorageAudit implementation. Job records and reports stay local.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from activity_core import exclusive_lock, no_links
import storage_audit as audit

MAX_JOBS = 256
MAX_TASK_BYTES = 512 * 1024
LAUNCH_GRACE_SECONDS = 60
PROGRESS_STALE_SECONDS = 30
MAX_COUNTER_VALUE = 2 ** 127 - 1
ACTIVE = frozenset(("queued", "running"))
TERMINAL = frozenset(("completed", "partial", "interrupted", "failed", "busy", "uncertain"))
_FIELDS = frozenset(("version", "task_id", "state", "roots", "created_utc", "updated_utc",
                     "started_utc", "finished_utc", "worker_pid", "error_code",
                     "report_started_utc", "report_finished_utc"))


class StorageTaskError(ValueError):
    """A bounded machine-readable error; never includes scanned file contents."""


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise StorageTaskError("invalid_task_id")
    return value


def _default_root():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise StorageTaskError("local_appdata_unavailable")
    return audit._local_absolute(base) / "ProstranstvoLocalBeta" / "data" / "storage-tasks"


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise StorageTaskError("invalid_task_record")
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None or not math.isfinite(stamp.timestamp()):
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise StorageTaskError("invalid_task_record") from None


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path, maximum):
    # Inspect native cloud/reparse flags before opening our own local files.
    audit._safe_output(path.parent)
    audit._safe_report_file(path)
    no_links(path, regular=True)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise StorageTaskError("record_too_large")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise StorageTaskError("invalid_json_record") from None
    if not isinstance(result, dict):
        raise StorageTaskError("invalid_json_record")
    return result


class StorageTasks:
    """A per-user job ledger. ``root`` is for local fixture integration only.

    start() accepts only explicit roots; executable, arguments and report paths
    cannot be supplied. Production subprocesses always use the normal profile
    root. status() never launches or resumes work.
    """

    def __init__(self, root=None):
        self.root = audit._local_absolute(_default_root() if root is None else root)
        if os.name == "nt" and self.root.drive.casefold() != "c:":
            raise StorageTaskError("jobs_require_c_drive")
        if audit._configured_cloud(self.root):
            raise StorageTaskError("jobs_must_be_outside_cloud")
        self.jobs = self.root / "jobs"
        self.lock = self.root / "tasks.lock"
        self.worker_lock = self.root / "worker.lock"

    def _safe(self):
        audit._safe_output(self.root)
        no_links(self.root)

    def _ensure(self):
        self._safe()
        self.jobs.mkdir(parents=True, exist_ok=True)
        self._safe()
        audit._safe_output(self.jobs)
        # Create the lock before a worker is spawned. Status does not create it.
        audit._safe_report_file(self.worker_lock)
        with exclusive_lock(self.worker_lock, blocking=False):
            pass

    @contextlib.contextmanager
    def _transaction(self):
        self._safe()
        audit._safe_report_file(self.lock)
        with exclusive_lock(self.lock):
            self._safe()
            yield

    def _directory(self, task_id):
        return self.jobs / _identifier(task_id)

    def _read_task(self, task_id):
        path = self._directory(task_id) / "task.json"
        try:
            value = _read_json(path, MAX_TASK_BYTES)
        except FileNotFoundError:
            raise StorageTaskError("task_not_found") from None
        if (set(value) != _FIELDS or type(value.get("version")) is not int or value.get("version") != 1
                or value.get("task_id") != task_id or not isinstance(value.get("state"), str)
                or value.get("state") not in ACTIVE | TERMINAL):
            raise StorageTaskError("invalid_task_record")
        for key in ("created_utc", "updated_utc"):
            _timestamp(value[key])
        for key in ("started_utc", "finished_utc", "report_started_utc", "report_finished_utc"):
            if value[key] is not None:
                _timestamp(value[key])
        if value["worker_pid"] is not None and (type(value["worker_pid"]) is not int or value["worker_pid"] <= 0):
            raise StorageTaskError("invalid_task_record")
        if value["error_code"] is not None and (not isinstance(value["error_code"], str)
                                                or not re.fullmatch(r"[a-z_]{1,80}", value["error_code"])):
            raise StorageTaskError("invalid_task_record")
        if (not isinstance(value["roots"], list) or not 1 <= len(value["roots"]) <= 16
                or any(not isinstance(path, str) or len(path) > audit.MAX_PATH_CHARS for path in value["roots"])):
            raise StorageTaskError("invalid_task_record")
        if value["state"] == "running" and value["started_utc"] is None:
            raise StorageTaskError("invalid_task_record")
        if (value["report_started_utc"] is None) != (value["report_finished_utc"] is None):
            raise StorageTaskError("invalid_task_record")
        if value["state"] in {"completed", "partial"}:
            if (any(value[key] is None for key in ("started_utc", "finished_utc", "report_started_utc", "report_finished_utc"))
                    or value["error_code"] is not None):
                raise StorageTaskError("invalid_task_record")
        if value["report_started_utc"] is not None:
            if (value["started_utc"] is None or value["finished_utc"] is None
                    or _timestamp(value["report_started_utc"]) < _timestamp(value["started_utc"]) - 1
                    or _timestamp(value["report_finished_utc"]) < _timestamp(value["report_started_utc"])
                    or _timestamp(value["finished_utc"]) < _timestamp(value["report_finished_utc"]) - 1):
                raise StorageTaskError("invalid_task_record")
        # Constructor validation is metadata-inert; do not revisit source paths
        # on status (a drive can disappear after a job has finished).
        checker = audit.StorageAudit(value["roots"], self._directory(task_id))
        if [str(path) for path in checker.roots] != value["roots"]:
            raise StorageTaskError("invalid_task_record")
        return value

    def _write_task(self, value):
        value["updated_utc"] = _now()
        audit._atomic_json(self._directory(value["task_id"]) / "task.json", value)

    def _finish(self, value, state, error_code=None):
        value.update(state=state, error_code=error_code, finished_utc=_now())
        self._write_task(value)

    def _last_id(self):
        try:
            value = _read_json(self.root / "last.json", 1024)
        except FileNotFoundError:
            return None
        if set(value) != {"version", "task_id"} or value.get("version") != 1:
            raise StorageTaskError("invalid_last_record")
        return _identifier(value.get("task_id"))

    def _worker_is_locked(self):
        audit._safe_report_file(self.worker_lock)
        no_links(self.worker_lock, regular=True)
        try:
            with exclusive_lock(self.worker_lock, blocking=False):
                return False
        except (RuntimeError, BlockingIOError, PermissionError):
            return True

    def _recover(self, value):
        if value["state"] == "running" and not self._worker_is_locked():
            self._finish(value, "interrupted", "worker_stopped_before_commit")
        elif value["state"] == "queued" and time.time() - _timestamp(value["created_utc"]) > LAUNCH_GRACE_SECONDS:
            self._finish(value, "uncertain", "worker_start_unconfirmed")

    def _validate_roots(self, roots, directory):
        checker = audit.StorageAudit(roots, directory)
        for path in checker.roots:
            if len(str(path)) > audit.MAX_PATH_CHARS:
                raise StorageTaskError("root_path_too_long")
            audit._safe_output(path)
            if not stat.S_ISDIR(audit._native_stat(path, follow_symlinks=False).st_mode):
                raise StorageTaskError("root_must_be_directory")
        return [str(path) for path in checker.roots]

    def _worker_command(self, task_id):
        # A caller-selected fixture ledger must never silently spawn a child
        # pointed at a different profile, nor become a command-line output path.
        if self.root != _default_root():
            raise StorageTaskError("worker_root_mismatch")
        executable = no_links(Path(sys.executable), regular=True)
        if getattr(sys, "frozen", False):
            return [str(executable), "storage-worker", "--task-id", _identifier(task_id)]
        script = no_links(Path(__file__).absolute(), regular=True)
        return [str(executable), "-B", str(script), "--worker", _identifier(task_id)]

    def start(self, roots):
        """Persist intent, then launch exactly one trusted, hidden worker."""
        task_id = uuid.uuid4().hex
        directory = self._directory(task_id)
        selected = self._validate_roots(roots, directory)
        command = self._worker_command(task_id)  # failures here cannot have spawned
        try:
            self._ensure()
        except (RuntimeError, BlockingIOError, PermissionError):
            raise StorageTaskError("storage_busy") from None
        with self._transaction():
            previous = self._last_id()
            if previous:
                old = self._read_task(previous)
                self._recover(old)
                if old["state"] in ACTIVE:
                    raise StorageTaskError("storage_busy")
            audit._safe_output(self.jobs)
            with os.scandir(self.jobs) as entries:
                if sum(1 for _ in entries) >= MAX_JOBS:
                    raise StorageTaskError("job_limit_reached")
            directory.mkdir(exist_ok=False)
            audit._safe_output(directory)
            now = _now()
            value = dict(version=1, task_id=task_id, state="queued", roots=selected,
                         created_utc=now, updated_utc=now, started_utc=None,
                         finished_utc=None, worker_pid=None, error_code=None,
                         report_started_utc=None, report_finished_utc=None)
            self._write_task(value)
            audit._atomic_json(self.root / "last.json", {"version": 1, "task_id": task_id})
        try:
            child = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)))
        except Exception:
            # Popen exceptions do not prove that no child was created. Never
            # retry or kill a possibly running child. Its claim is authoritative.
            with self._transaction():
                value = self._read_task(task_id)
                if value["state"] == "queued":
                    self._finish(value, "uncertain", "worker_launch_uncertain")
        else:
            with self._transaction():
                value = self._read_task(task_id)
                if value["state"] == "queued":
                    pid = getattr(child, "pid", None)
                    if type(pid) is int and pid > 0:
                        value["worker_pid"] = pid
                        self._write_task(value)
        return self.status(task_id)

    def _progress(self, value):
        path = self._directory(value["task_id"]) / "progress.json"
        try:
            progress = _read_json(path, 128 * 1024)
        except FileNotFoundError:
            return None
        if (type(progress.get("version")) is not int or progress.get("version") != 1
                or not isinstance(progress.get("status"), str)
                or progress.get("status") not in {"running", "completed", "partial", "interrupted"}
                or not isinstance(progress.get("counters"), dict)):
            raise StorageTaskError("invalid_progress")
        # Progress is advisory. It never changes the ledger to completed.
        started = _timestamp(progress.get("started_utc"))
        updated = _timestamp(progress.get("updated_utc"))
        if value["started_utc"] is None or started < _timestamp(value["started_utc"]) - 1:
            raise StorageTaskError("progress_does_not_match_task")
        return {"status": progress["status"], "counters": self._counters(progress["counters"]),
                "started_utc": progress["started_utc"], "updated_utc": progress["updated_utc"],
                "stale": time.time() - updated > PROGRESS_STALE_SECONDS}

    @staticmethod
    def _counters(value):
        if len(value) > 64 or any(not isinstance(key, str) or len(key) > 80 or type(number) is not int
                                  or not 0 <= number <= MAX_COUNTER_VALUE
                                  for key, number in value.items()):
            raise StorageTaskError("invalid_counters")
        return dict(value)

    def _report(self, value, *, commit=False):
        path = self._directory(value["task_id"]) / "report.json"
        report = _read_json(path, audit.MAX_REPORT_BYTES)
        if (type(report.get("version")) is not int or report.get("version") != 1
                or not isinstance(report.get("status"), str)
                or report.get("status") not in {"completed", "partial", "interrupted"}
                or report.get("roots") != value["roots"] or report.get("report_path") != str(path)
                or type(report.get("traversal_finished")) is not bool
                or report["traversal_finished"] != (report["status"] != "interrupted")
                or not isinstance(report.get("counters"), dict)):
            raise StorageTaskError("invalid_report")
        started = _timestamp(report.get("started_utc"))
        if value["started_utc"] is None or started < _timestamp(value["started_utc"]) - 1:
            raise StorageTaskError("report_does_not_match_task")
        if _timestamp(report.get("finished_utc")) < started:
            raise StorageTaskError("invalid_report")
        if not commit and (report["started_utc"] != value["report_started_utc"]
                           or report["finished_utc"] != value["report_finished_utc"]
                           or report["status"] != value["state"]):
            raise StorageTaskError("report_does_not_match_task")
        self._counters(report["counters"])
        limits = report.get("output_limits", {})
        if not isinstance(limits, dict) or len(limits) > 32:
            raise StorageTaskError("invalid_report_limits")
        for key, number in limits.items():
            if not isinstance(key, str) or len(key) > 80:
                raise StorageTaskError("invalid_report_limits")
            if key == "budget_truncated":
                if type(number) is not bool:
                    raise StorageTaskError("invalid_report_limits")
            elif type(number) is not int or not 0 <= number <= MAX_COUNTER_VALUE:
                raise StorageTaskError("invalid_report_limits")
        for field, size_key, cap in (("folder_aggregates", "logical_bytes_raw", audit.MAX_AGGREGATE_ROWS),
                                      ("largest_files", "logical_bytes", 200)):
            rows = report.get(field, [])
            if not isinstance(rows, list) or len(rows) > cap:
                raise StorageTaskError("invalid_report_rows")
            for row in rows:
                if (not isinstance(row, dict) or len(row) > 32 or not isinstance(row.get("path"), str)
                        or len(row["path"]) > audit.MAX_PATH_CHARS + 1 or type(row.get(size_key)) is not int
                        or not 0 <= row[size_key] <= MAX_COUNTER_VALUE):
                    raise StorageTaskError("invalid_report_rows")
        return report

    def _snapshot(self, value):
        result = dict(value, progress=None, report=None, report_path=None, progress_error=None, report_error=None)
        if value["started_utc"] is not None:
            try:
                result["progress"] = self._progress(value)
            except (OSError, ValueError) as error:
                result["progress_error"] = type(error).__name__
        if (value["state"] in {"completed", "partial"}
                or value["state"] == "interrupted" and value["report_started_utc"] is not None):
            try:
                result["report"] = self._report(value)
                result["report_path"] = str(self._directory(value["task_id"]) / "report.json")
            except (OSError, ValueError):
                result.update(state="failed", error_code="saved_report_unavailable", report_error="invalid_report")
        return result

    def status(self, task_id=None):
        """Read a saved job, detecting a stopped worker; never launch/retry."""
        if task_id is not None:
            _identifier(task_id)
        self._safe()
        if not self.root.exists():
            if task_id is not None:
                raise StorageTaskError("task_not_found")
            return {"version": 1, "state": "idle", "task_id": None, "report": None, "progress": None}
        with self._transaction():
            selected = task_id or self._last_id()
            if selected is None:
                return {"version": 1, "state": "idle", "task_id": None, "report": None, "progress": None}
            value = self._read_task(selected)
            self._recover(value)
            return self._snapshot(value)

    def worker(self, task_id):
        """Claim the persisted ticket once, holding the profile lock to finish."""
        _identifier(task_id)
        self._safe()
        # Do not create any ledger or lock for a nonexistent caller ticket.
        self._read_task(task_id)
        audit._safe_report_file(self.worker_lock)
        try:
            with exclusive_lock(self.worker_lock, blocking=False):
                with self._transaction():
                    value = self._read_task(task_id)
                    if value["state"] != "queued":
                        return self._snapshot(value)
                    self._recover(value)
                    if value["state"] != "queued":
                        return self._snapshot(value)
                    value.update(state="running", started_utc=_now(), worker_pid=os.getpid())
                    self._write_task(value)
                    try:
                        self._validate_roots(value["roots"], self._directory(task_id))
                    except (OSError, ValueError):
                        self._finish(value, "failed", "roots_unavailable")
                        return self._snapshot(value)
                    value.update(state="running", started_utc=_now(), worker_pid=os.getpid())
                    self._write_task(value)
                try:
                    audit._below_normal_priority()
                    scanner = audit.StorageAudit(value["roots"], self._directory(task_id))
                    scanner.run()
                    with self._transaction():
                        value = self._read_task(task_id)
                        report = self._report(value, commit=True)
                        value.update(report_started_utc=report["started_utc"], report_finished_utc=report["finished_utc"])
                        self._finish(value, report["status"])
                        return self._snapshot(value)
                except BaseException as error:
                    with self._transaction():
                        value = self._read_task(task_id)
                        self._finish(value, "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed",
                                     "worker_interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "audit_failed")
                        return self._snapshot(value)
        except (RuntimeError, BlockingIOError, PermissionError):
            with self._transaction():
                value = self._read_task(task_id)
                if value["state"] == "queued":
                    self._finish(value, "busy", "another_storage_worker_running")
                return self._snapshot(value)
def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit local metadata-audit worker", allow_abbrev=False)
    parser.add_argument("--worker", required=True)
    args = parser.parse_args(argv)
    try:
        value = StorageTasks().worker(args.worker)
        return 0 if value["state"] in {"completed", "partial"} else 1
    except (OSError, ValueError, RuntimeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
