"""Owned-ledger fixtures only; no personal drive walk, UI, network or commands."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))
import storage_audit
import storage_tasks
from storage_tasks import StorageTasks, StorageTaskError
DEFAULT_ROOT_FUNCTION = storage_tasks._default_root


def save_fixture_report(subject, status="partial"):
    now = storage_tasks._now()
    counters = {"files": 3, "directories": 1, "entries_seen": 4,
                "logical_bytes_raw": 1234, "permission_denied": 1, "errors": 1,
                "skipped_entries": 2}
    report = {"version": 1, "status": status, "traversal_finished": status != "interrupted",
              "started_utc": now, "finished_utc": now, "roots": [str(path) for path in subject.roots],
              "report_path": str(subject.report_path), "counters": counters,
              "folder_aggregates": [], "largest_files": [], "output_limits": {"budget_truncated": False}}
    storage_audit._atomic_json(subject.report_path, report)
    storage_audit._atomic_json(subject.progress_path, {"version": 1, "status": status,
                              "started_utc": now, "updated_utc": now, "counters": counters})
    return report


CHILD = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import storage_tasks, storage_audit
root, ticket, marker, release = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
def fake_run(self):
    marker.write_text("lock-held", encoding="utf-8")
    deadline = time.monotonic() + 12
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("fixture release timeout")
        time.sleep(.02)
    now = storage_tasks._now()
    report = {"version":1, "status":"completed", "traversal_finished":True,
              "started_utc":now, "finished_utc":now, "roots":[str(p) for p in self.roots],
              "report_path":str(self.report_path), "counters":{"files":7}}
    storage_audit._atomic_json(self.report_path, report)
    return report
storage_audit.StorageAudit.run = fake_run
result = storage_tasks.StorageTasks(root).worker(ticket)
(root / "child-result.json").write_text(json.dumps(result), encoding="utf-8")
raise SystemExit(0 if result["state"] == "completed" else 1)
'''


class StorageTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storage-task-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.input = self.base / "input"
        self.input.mkdir()
        self.root = self.base / "ledger"
        self.tasks = StorageTasks(self.root)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(storage_tasks, "_default_root", return_value=self.root).start()
        self.run_patch = mock.patch.object(storage_audit.StorageAudit, "run", autospec=True,
                                          side_effect=AssertionError("No real scan"))
        self.run = self.run_patch.start()

    def queued(self):
        with mock.patch.object(storage_tasks.subprocess, "Popen", return_value=SimpleNamespace(pid=789)):
            value = self.tasks.start([self.input])
        self.assertEqual(value["state"], "queued")
        return value

    def test_constructor_and_missing_status_are_inert(self):
        self.assertFalse(self.root.exists())
        self.assertEqual(self.tasks.status()["state"], "idle")
        self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(StorageTaskError, "task_not_found"):
            self.tasks.status("a" * 32)
        self.assertFalse(self.root.exists())
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StorageTaskError):
                DEFAULT_ROOT_FUNCTION()

    def test_root_validation_rejects_missing_unsafe_and_overlapping_before_spawn(self):
        file = self.input / "file"
        file.write_bytes(b"fixture")
        cases = ([], ["relative"], [r"\\server\share"], [self.base / "missing"],
                 [file], [self.input, self.input / "nested"])
        with mock.patch.object(storage_tasks.subprocess, "Popen") as spawn:
            for roots in cases:
                with self.subTest(roots=roots), self.assertRaises((ValueError, OSError)):
                    self.tasks.start(roots)
            with mock.patch.object(storage_audit, "_configured_cloud", return_value=True):
                with self.assertRaises(ValueError):
                    self.tasks.start([self.input])
            spawn.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_identifier_and_unknown_ticket_never_make_a_directory(self):
        for ticket in ("../report", "A" * 32, "x", "a" * 33, "a" * 32 + "/", None):
            with self.subTest(ticket=ticket), self.assertRaises(StorageTaskError):
                self.tasks.worker(ticket)
        with self.assertRaisesRegex(StorageTaskError, "task_not_found"):
            self.tasks.worker("b" * 32)
        self.assertFalse(self.root.exists())
        self.run.assert_not_called()

    def test_queued_intent_is_atomic_before_trusted_hidden_spawn(self):
        def spawn(command, **kwargs):
            ticket = command[-1]
            record = json.loads((self.root / "jobs" / ticket / "task.json").read_text())
            self.assertEqual(record["state"], "queued")
            self.assertIsNone(record["started_utc"])
            self.assertEqual(command, [sys.executable, "-B", str(SOURCE / "storage_tasks.py"), "--worker", ticket])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            self.assertEqual(kwargs["creationflags"], flags)
            return SimpleNamespace(pid=234)
        with mock.patch.object(storage_tasks.subprocess, "Popen", side_effect=spawn):
            result = self.tasks.start([self.input])
        self.assertEqual(result["worker_pid"], 234)
        self.assertIsNone(result["report"])
        self.assertIsNone(result["progress"])
        self.assertEqual(self.tasks.status()["task_id"], result["task_id"])
        self.run.assert_not_called()

    def test_frozen_command_is_fixed_and_fixture_profile_cannot_leak_to_child(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(self.tasks._worker_command("a" * 32),
                             [sys.executable, "storage-worker", "--task-id", "a" * 32])
        with mock.patch.object(storage_tasks, "_default_root", return_value=self.base / "elsewhere"):
            with self.assertRaisesRegex(StorageTaskError, "worker_root_mismatch"):
                self.tasks.start([self.input])
        self.assertFalse(self.root.exists())

    def test_spawn_exception_is_uncertain_no_retry_or_late_scan(self):
        with mock.patch.object(storage_tasks.subprocess, "Popen", side_effect=OSError("fixture")) as spawn:
            result = self.tasks.start([self.input])
            self.assertEqual(result["state"], "uncertain")
            self.assertEqual(result["error_code"], "worker_launch_uncertain")
            self.tasks.status(result["task_id"])
            late = self.tasks.worker(result["task_id"])
            self.assertEqual(late["state"], "uncertain")
            spawn.assert_called_once()
        self.run.assert_not_called()

    def test_spawn_error_after_worker_completed_preserves_actual_outcome(self):
        self.run_patch.stop()
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True,
                               side_effect=lambda subject: save_fixture_report(subject, "completed")):
            def uncertain_spawn(command, **_kwargs):
                self.tasks.worker(command[-1])
                raise OSError("created then failed to return")
            with mock.patch.object(storage_tasks.subprocess, "Popen", side_effect=uncertain_spawn):
                result = self.tasks.start([self.input])
        self.assertEqual(result["state"], "completed")
        self.assertIsNone(result["error_code"])

    def test_worker_claims_running_under_lock_and_commits_partial_once(self):
        queued = self.queued()
        ticket = queued["task_id"]
        def run(subject):
            state = self.tasks.status(ticket)
            self.assertEqual(state["state"], "running")
            self.assertEqual(state["worker_pid"], os.getpid())
            self.assertIsNone(state["report"])
            self.assertTrue(self.tasks._worker_is_locked())
            return save_fixture_report(subject)
        self.run_patch.stop()
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True, side_effect=run) as runner:
            value = self.tasks.worker(ticket)
            again = self.tasks.worker(ticket)
        runner.assert_called_once()
        self.assertEqual(value["state"], "partial")
        self.assertEqual(again["state"], "partial")
        self.assertEqual(value["report"]["counters"]["files"], 3)
        self.assertEqual(value["report_path"], str(self.root / "jobs" / ticket / "report.json"))
        self.assertFalse(self.tasks._worker_is_locked())

    def test_worker_failure_and_keyboard_interrupt_are_explicit(self):
        first = self.queued()
        self.run.side_effect = PermissionError("fixture")
        self.assertEqual(self.tasks.worker(first["task_id"])["state"], "failed")
        second = self.queued()
        self.run.side_effect = KeyboardInterrupt()
        value = self.tasks.worker(second["task_id"])
        self.assertEqual(value["state"], "interrupted")
        self.assertEqual(value["error_code"], "worker_interrupted")
        self.assertIsNone(value["report"])

    def test_old_queued_ticket_expires_without_auto_retry(self):
        value = self.queued()
        with mock.patch.object(storage_tasks.time, "time", return_value=time.time() + 100):
            expired = self.tasks.status(value["task_id"])
        self.assertEqual(expired["state"], "uncertain")
        self.assertEqual(self.tasks.worker(value["task_id"])["state"], "uncertain")
        self.run.assert_not_called()

    def test_final_progress_never_masquerades_as_a_committed_report(self):
        queued = self.queued()
        ticket = queued["task_id"]
        with self.tasks._transaction():
            value = self.tasks._read_task(ticket)
            value.update(state="running", started_utc=storage_tasks._now())
            self.tasks._write_task(value)
        subject = storage_audit.StorageAudit([self.input], self.root / "jobs" / ticket)
        save_fixture_report(subject, "completed")
        with storage_tasks.exclusive_lock(self.tasks.worker_lock, blocking=False):
            with mock.patch.object(storage_tasks.time, "time", return_value=time.time() + 100):
                running = self.tasks.status(ticket)
            self.assertEqual(running["state"], "running")
            self.assertTrue(running["progress"]["stale"])
            self.assertEqual(running["progress"]["status"], "completed")
            self.assertIsNone(running["report"])
        interrupted = self.tasks.status(ticket)
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertIsNone(interrupted["report"])
        self.run.assert_not_called()

    def test_corrupt_or_linked_report_is_not_presented_as_complete(self):
        value = self.queued()
        self.run_patch.stop()
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True,
                               side_effect=lambda subject: save_fixture_report(subject, "completed")):
            self.tasks.worker(value["task_id"])
        report = self.root / "jobs" / value["task_id"] / "report.json"
        report.write_text('{"version":1,"report_path":"C:/somewhere-else"}', encoding="utf-8")
        result = self.tasks.status(value["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["report"])
        self.assertIsNone(result["report_path"])
        self.assertEqual(result["error_code"], "saved_report_unavailable")

    def test_hardlinked_report_is_refused_without_changing_either_file(self):
        value = self.queued()
        self.run_patch.stop()
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True,
                               side_effect=lambda subject: save_fixture_report(subject, "completed")):
            self.tasks.worker(value["task_id"])
        report = self.root / "jobs" / value["task_id"] / "report.json"
        unrelated = self.base / "unrelated-fixture"
        os.link(report, unrelated)
        before = unrelated.read_bytes()
        result = self.tasks.status(value["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["report"])
        self.assertEqual(unrelated.read_bytes(), before)
        self.assertEqual(report.read_bytes(), before)

    def test_cloud_root_is_rejected_before_any_process_or_ledger_write(self):
        with mock.patch.dict(os.environ, {"OneDrive": str(self.input)}), \
             mock.patch.object(storage_tasks.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                self.tasks.start([self.input])
            spawn.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_root_disappearing_before_claim_has_explicit_failure(self):
        value = self.queued()
        self.input.rmdir()
        result = self.tasks.worker(value["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "roots_unavailable")
        self.run.assert_not_called()

    def test_cli_reuses_worker_and_has_bounded_exit_codes(self):
        with mock.patch.object(storage_tasks, "StorageTasks") as factory:
            factory.return_value.worker.return_value = {"state": "partial"}
            self.assertEqual(storage_tasks.main(["--worker", "a" * 32]), 0)
            factory.return_value.worker.assert_called_once_with("a" * 32)
            factory.return_value.worker.return_value = {"state": "uncertain"}
            self.assertEqual(storage_tasks.main(["--worker", "a" * 32]), 1)
            factory.return_value.worker.side_effect = ValueError("fixture")
            self.assertEqual(storage_tasks.main(["--worker", "a" * 32]), 1)

    def test_active_job_blocks_a_second_start_and_ledger_has_finite_limit(self):
        value = self.queued()
        with mock.patch.object(storage_tasks.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(StorageTaskError, "storage_busy"):
                self.tasks.start([self.input])
            spawn.assert_not_called()
        with self.tasks._transaction():
            old = self.tasks._read_task(value["task_id"])
            self.tasks._finish(old, "failed", "fixture_failed")
        with mock.patch.object(storage_tasks, "MAX_JOBS", 1), mock.patch.object(storage_tasks.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(StorageTaskError, "job_limit_reached"):
                self.tasks.start([self.input])
            spawn.assert_not_called()

    def test_finished_ledger_requires_complete_commit_timestamps(self):
        queued = self.queued()
        ticket = queued["task_id"]
        original = self.tasks._read_task(ticket)
        variants = []
        for state in ("completed", "partial"):
            variants.append(dict(original, state=state))
            complete = dict(original, state=state, started_utc=storage_tasks._now(),
                            finished_utc=storage_tasks._now(), report_started_utc=storage_tasks._now(),
                            report_finished_utc=storage_tasks._now())
            for field in ("started_utc", "finished_utc", "report_started_utc", "report_finished_utc"):
                variants.append(dict(complete, **{field: None}))
        with mock.patch.object(storage_tasks.subprocess, "Popen") as spawn:
            for value in variants:
                with self.subTest(state=value["state"], value=value):
                    storage_audit._atomic_json(self.root / "jobs" / ticket / "task.json", value)
                    with self.assertRaisesRegex(StorageTaskError, "invalid_task_record"):
                        self.tasks.status(ticket)
            spawn.assert_not_called()
        self.run.assert_not_called()

    def test_saved_report_rejects_malformed_ui_fields_and_bounded_rows(self):
        queued = self.queued()
        ticket = queued["task_id"]
        self.run_patch.stop()
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True,
                               side_effect=lambda subject: save_fixture_report(subject, "completed")):
            completed = self.tasks.worker(ticket)
        path = self.root / "jobs" / ticket / "report.json"
        original = completed["report"]
        variants = [{"output_limits": []}, {"output_limits": None},
                    {"output_limits": {"budget_truncated": "false"}},
                    {"folder_aggregates": {}}, {"largest_files": [None]},
                    {"largest_files": [{"path": [], "logical_bytes": 5}]},
                    {"largest_files": [{"path": "fixture", "logical_bytes": True}]},
                    {"largest_files": [{"path": "fixture", "logical_bytes": 1}] * 201},
                    {"counters": {"logical_bytes_raw": 10 ** 1000}}]
        for changed in variants:
            with self.subTest(changed=changed):
                storage_audit._atomic_json(path, dict(original, **changed))
                result = self.tasks.status(ticket)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["error_code"], "saved_report_unavailable")
                self.assertIsNone(result["report"])
                self.assertIsNone(result["report_path"])
        storage_audit._atomic_json(path, original)
        self.assertEqual(self.tasks.status(ticket)["state"], "completed")

    def test_worker_does_not_commit_a_report_with_invalid_limits(self):
        queued = self.queued()
        self.run_patch.stop()
        def malformed(subject):
            report = save_fixture_report(subject, "completed")
            report["output_limits"] = []
            storage_audit._atomic_json(subject.report_path, report)
            return report
        with mock.patch.object(storage_audit.StorageAudit, "run", autospec=True, side_effect=malformed):
            result = self.tasks.worker(queued["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "audit_failed")
        self.assertIsNone(result["report"])

    def test_two_actual_fixture_workers_use_one_profile_lock(self):
        first = self.queued()
        with self.tasks._transaction():
            old = self.tasks._read_task(first["task_id"])
            self.tasks._finish(old, "failed", "fixture_failed")
        second = self.queued()
        with self.tasks._transaction():
            old = self.tasks._read_task(first["task_id"])
            old.update(state="queued", finished_utc=None, error_code=None)
            self.tasks._write_task(old)
        marker, release = self.base / "ready", self.base / "release"
        child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(SOURCE), str(self.root),
                                  first["task_id"], str(marker), str(release)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "Fixture worker did not acquire lock")
            busy = self.tasks.worker(second["task_id"])
            self.assertEqual(busy["state"], "busy")
            self.assertEqual(busy["error_code"], "another_storage_worker_running")
            self.run.assert_not_called()
            release.write_text("continue", encoding="utf-8")
            _out, err = child.communicate(timeout=7)
            self.assertEqual(child.returncode, 0, err.decode(errors="replace"))
            self.assertEqual(self.tasks.status(first["task_id"])["state"], "completed")
        finally:
            release.touch(exist_ok=True)
            if child.poll() is None:
                child.communicate(timeout=15)
            if child.stderr:
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()
