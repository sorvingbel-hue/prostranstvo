"""Explicit, durable continuation of an already authorized repair job.

Only the existing broker's fixed cleanup and health commands can be requested.
There is no scheduler, autostart, shell, network, reboot, deletion or AI export.
Construction/status are read-only; start observes the exact initial repair before
persisting. Running this module explicitly authorizes continuation, not a new
repair. A durable request intent surviving any crash is uncertain and is never
replayed automatically. Restart-required results stop for human attention.

Receipts contain the FULL broker result, including potentially private log paths
and summaries. They remain local under this module's data root, not AI payloads.
This is crash/dedup protection, not a security boundary against the same user
editing local application files. No service commands run merely on import.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links
import paths


POLL_SECONDS = 30
MAX_SECONDS = 6 * 60 * 60
MAX_STATE = 64 * 1024
MAX_RECEIPT = 1024 * 1024
STAGES = ("repair", "cleanup", "health")
STEPS = {"repair": ("component-repair", "system-files-repair"),
         "cleanup": ("component-cleanup",),
         "health": ("component-health", "system-files-verify")}
REASONS = {"request_uncertain", "broker_unavailable", "job_mismatch", "invalid_job",
           "job_failed", "job_interrupted", "invalid_steps", "restart_required",
           "not_verified", "timeout", "clock_moved_backwards", "receipt_error"}


def _identifier(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("invalid_json_number")


def _json_value(value, depth=0):
    if depth > 32:
        raise ValueError("json_too_deep")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("invalid_json_key")
        for item in value.values():
            _json_value(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("invalid_json_value")
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("invalid_json_number")


def _encode(value, limit):
    _json_value(value)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise ValueError("sequence_file_too_large")
    return raw


def _read(path, limit):
    no_links(path, True)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("sequence_file_too_large")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)


def _validate(state):
    keys = {"version", "sequence_id", "repair_id", "status", "stage", "job_id",
            "started_at", "deadline", "updated_at", "reason", "intent", "receipts"}
    if type(state) is not dict or set(state) != keys:
        raise ValueError("invalid_sequence_state")
    if type(state["version"]) is not int or state["version"] != 1:
        raise ValueError("invalid_sequence_version")
    if not all(_identifier(state[key]) for key in ("sequence_id", "repair_id", "job_id")):
        raise ValueError("invalid_sequence_id")
    if state["stage"] not in STAGES or state["status"] not in ("observing", "attention", "completed"):
        raise ValueError("invalid_sequence_phase")
    if (not all(_number(state[key]) for key in ("started_at", "deadline", "updated_at"))
            or not 0 < state["deadline"] - state["started_at"] <= MAX_SECONDS
            or state["updated_at"] < state["started_at"]):
        raise ValueError("invalid_sequence_time")
    if ((state["status"] == "attention" and state["reason"] not in REASONS)
            or (state["status"] != "attention" and state["reason"] is not None)):
        raise ValueError("invalid_sequence_reason")
    index = STAGES.index(state["stage"])
    receipts = state["receipts"]
    if type(receipts) is not list or len(receipts) not in (index, index + 1):
        raise ValueError("invalid_sequence_receipts")
    seen = set()
    for offset, row in enumerate(receipts):
        if (type(row) is not dict or set(row) != {"command", "job_id", "sha256"}
                or row["command"] != STAGES[offset] or not _identifier(row["job_id"])
                or type(row["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                or row["job_id"] in seen):
            raise ValueError("invalid_sequence_receipt")
        seen.add(row["job_id"])
    if (index == 0 and state["job_id"] != state["repair_id"]) or (
            receipts and receipts[0]["job_id"] != state["repair_id"]) or (
            len(receipts) == index + 1 and receipts[-1]["job_id"] != state["job_id"]) or (
            len(receipts) == index and state["job_id"] in seen):
        raise ValueError("invalid_sequence_binding")
    intent = state["intent"]
    if intent is not None:
        if (type(intent) is not dict or set(intent) != {"command", "created_at"}
                or index >= 2 or intent["command"] != STAGES[index + 1]
                or len(receipts) != index + 1 or not _number(intent["created_at"])
                or not state["started_at"] <= intent["created_at"] <= state["updated_at"]):
            raise ValueError("invalid_sequence_intent")
    if state["status"] == "completed" and (index != 2 or len(receipts) != 3 or intent is not None):
        raise ValueError("invalid_sequence_completion")
    return state


class MaintenanceSequence:
    def __init__(self, root=None, *, status_provider=None, request_provider=None, clock=time.time):
        base = getattr(paths, "DATA_ROOT", None)
        if base is None:
            base = paths.DEPLOY_ROOT / "data"
        self.root = Path(root).absolute() if root is not None else Path(base) / "maintenance-sequence"
        self.path = self.root / "state.json"
        self.lock_path = self.root / "sequence.lock"
        if any(value is not None and not callable(value) for value in (status_provider, request_provider)) or not callable(clock):
            raise ValueError("invalid_sequence_provider")
        self.status_provider = status_provider or self._broker_status
        self.request_provider = request_provider or self._broker_request
        self.clock = clock

    @staticmethod
    def _broker_status():
        from system_broker import status
        return status()

    @staticmethod
    def _broker_request(command):
        if command not in ("cleanup", "health"):
            raise ValueError("invalid_sequence_command")
        from system_broker import request
        return request(command)

    def _now(self):
        value = self.clock()
        if not _number(value):
            raise ValueError("invalid_sequence_clock")
        return float(value)

    def _prepare(self):
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)

    def status(self):
        """Read saved state only; no broker calls, writes or automatic recovery."""
        try:
            return _validate(_read(self.path, MAX_STATE))
        except FileNotFoundError:
            return None

    def _atomic(self, path, value, limit):
        raw = _encode(value, limit)
        no_links(path, path.exists())
        descriptor, temporary = tempfile.mkstemp(prefix=".sequence-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(path, path.exists())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save(self, state):
        self._atomic(self.path, _validate(state), MAX_STATE)

    def _snapshot(self, job_id, command):
        try:
            value = self.status_provider()
            # Copy before examining: a provider must not mutate the archived view.
            value = json.loads(_encode(value, MAX_RECEIPT).decode("utf-8"))
        except Exception as error:
            raise RuntimeError("broker_unavailable") from error
        if (type(value) is not dict or value.get("available") is not True
                or value.get("state") != "running" or value.get("stopping") is True
                or value.get("ok") is False):
            raise RuntimeError("broker_unavailable")
        job = value.get("job")
        if type(job) is not dict:
            raise RuntimeError("invalid_job")
        if job.get("id") != job_id or job.get("command") != command:
            raise RuntimeError("job_mismatch")
        if job.get("state") not in ("running", "completed", "failed", "interrupted"):
            raise RuntimeError("invalid_job")
        return value

    def _start_locked(self, repair_id, max_seconds):
        saved = self.status()
        if saved is not None:
            if saved["repair_id"] != repair_id:
                raise ValueError("sequence_already_bound")
            return saved
        self._snapshot(repair_id, "repair")  # Validate BEFORE any state is persisted.
        now = self._now()
        state = {"version": 1, "sequence_id": uuid.uuid4().hex, "repair_id": repair_id,
                 "status": "observing", "stage": "repair", "job_id": repair_id,
                 "started_at": now, "deadline": now + max_seconds, "updated_at": now,
                 "reason": None, "intent": None, "receipts": []}
        self._save(state)
        return state

    @staticmethod
    def _start_arguments(repair_id, max_seconds):
        if not _identifier(repair_id):
            raise ValueError("invalid_repair_id")
        if type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SECONDS:
            raise ValueError("invalid_sequence_timeout")

    def start(self, repair_id, max_seconds=MAX_SECONDS):
        """Bind once to an existing exact repair. Never request a new job here."""
        self._start_arguments(repair_id, max_seconds)
        self._prepare()
        with exclusive_lock(self.lock_path, blocking=False):
            return self._start_locked(repair_id, max_seconds)

    def _attention(self, state, reason, now):
        state.update(status="attention", reason=reason, updated_at=max(now, state["updated_at"]))
        self._save(state)
        return state

    def _receipt_path(self, state):
        return self.root / (state["sequence_id"] + "-" + state["stage"] + "-" + state["job_id"] + ".json")

    def _archive(self, state, snapshot, now):
        path = self._receipt_path(state)
        if path.exists():
            value = _read(path, MAX_RECEIPT)
            if (type(value) is not dict or set(value) != {"version", "sequence_id", "command", "job_id", "observed_at", "broker_result"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or value["sequence_id"] != state["sequence_id"] or value["command"] != state["stage"]
                    or value["job_id"] != state["job_id"] or not _number(value["observed_at"])
                    or type(value["broker_result"]) is not dict
                    or _encode(value["broker_result"].get("job"), MAX_RECEIPT) != _encode(snapshot["job"], MAX_RECEIPT)):
                raise ValueError("receipt_conflict")
        else:
            value = {"version": 1, "sequence_id": state["sequence_id"], "command": state["stage"],
                     "job_id": state["job_id"], "observed_at": now, "broker_result": snapshot}
            self._atomic(path, value, MAX_RECEIPT)
        digest = hashlib.sha256(_encode(value, MAX_RECEIPT)).hexdigest()
        index = STAGES.index(state["stage"])
        row = {"command": state["stage"], "job_id": state["job_id"], "sha256": digest}
        if len(state["receipts"]) == index:
            state["receipts"].append(row)
            state["updated_at"] = now
            self._save(state)
        elif state["receipts"][-1] != row:
            raise ValueError("receipt_digest_mismatch")
        return value["broker_result"]["job"]

    @staticmethod
    def _result_reason(job, stage):
        if job["state"] != "completed":
            return "job_failed" if job["state"] == "failed" else "job_interrupted"
        steps = job.get("steps")
        if (type(steps) is not list or len(steps) != len(STEPS[stage])
                or any(type(row) is not dict or row.get("name") != name
                       or type(row.get("exitCode")) is not int or row["exitCode"] not in (0, 3010)
                       for row, name in zip(steps, STEPS[stage]))):
            return "invalid_steps"
        if job.get("restart_required") is True or any(row["exitCode"] == 3010 for row in steps):
            return "restart_required"
        if stage == "health" and job.get("review_required") is True:
            return "not_verified"
        return None

    def _advance_locked(self):
        state = self.status()
        if state is None:
            raise ValueError("sequence_missing")
        if state["status"] != "observing":
            return state
        now = self._now()
        if state["intent"] is not None:
            return self._attention(state, "request_uncertain", now)
        if now < state["updated_at"]:
            return self._attention(state, "clock_moved_backwards", now)
        if now >= state["deadline"]:
            return self._attention(state, "timeout", now)
        try:
            snapshot = self._snapshot(state["job_id"], state["stage"])
        except RuntimeError as error:
            return self._attention(state, str(error), now)
        now = self._now()
        if now < state["updated_at"]:
            return self._attention(state, "clock_moved_backwards", now)
        if now >= state["deadline"]:
            return self._attention(state, "timeout", now)
        if snapshot["job"]["state"] == "running":
            state["updated_at"] = now
            self._save(state)
            return state
        try:
            job = self._archive(state, snapshot, now)
        except Exception:
            return self._attention(state, "receipt_error", now)
        reason = self._result_reason(job, state["stage"])
        if reason:
            return self._attention(state, reason, now)
        now = self._now()
        if now < state["updated_at"]:
            return self._attention(state, "clock_moved_backwards", now)
        if now >= state["deadline"]:
            return self._attention(state, "timeout", now)
        index = STAGES.index(state["stage"])
        if index == 2:
            state.update(status="completed", updated_at=now)
            self._save(state)
            return state
        command = STAGES[index + 1]  # The ONLY possible mutation commands.
        state.update(intent={"command": command, "created_at": now}, updated_at=now)
        self._save(state)  # Write-ahead intent: any interrupted request is never replayed.
        # Persistence can block. Recheck at the final boundary before dispatch.
        now = self._now()
        if now < state["updated_at"]:
            return self._attention(state, "clock_moved_backwards", now)
        if now >= state["deadline"]:
            return self._attention(state, "timeout", now)
        try:
            reply = self.request_provider(command)
            new_id = reply.get("job_id") if type(reply) is dict else None
            if (type(reply) is not dict or reply.get("ok") is not True or reply.get("accepted") is not True
                    or not _identifier(new_id) or new_id in {row["job_id"] for row in state["receipts"]}):
                raise ValueError("invalid_request_receipt")
        except Exception:
            return self._attention(state, "request_uncertain", now)
        state.update(stage=command, job_id=new_id, intent=None, updated_at=now)
        self._save(state)  # Failure here leaves the durable intent on disk, not a replay ticket.
        return state

    def advance(self):
        """One observation/transition, protected by the same lock as the CLI runner."""
        self._prepare()
        with exclusive_lock(self.lock_path, blocking=False):
            return self._advance_locked()

    def run(self, repair_id, max_seconds=MAX_SECONDS, *, sleep=time.sleep):
        """Explicit bounded runner; hold one lock throughout, including polling waits."""
        self._start_arguments(repair_id, max_seconds)
        self._prepare()
        with exclusive_lock(self.lock_path, blocking=False):
            state = self._start_locked(repair_id, max_seconds)
            while state["status"] == "observing":
                state = self._advance_locked()
                if state["status"] == "observing":
                    sleep(min(POLL_SECONDS, max(0.0, state["deadline"] - self._now())))
            return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="Continue one exact authorized repair, then fixed cleanup and health.")
    parser.add_argument("--repair-id", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    arguments = parser.parse_args(argv)
    try:
        result = MaintenanceSequence(arguments.root).run(arguments.repair_id, arguments.max_seconds)
        print(json.dumps(result, ensure_ascii=False))  # Minimal state, never raw private receipts.
        return 0 if result["status"] == "completed" else 2
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "attention", "reason": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
