"""Isolated files and fake broker only. No Windows processes, GUI or real jobs."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import maintenance_sequence as sequence


REPAIR, CLEANUP, HEALTH = "1" * 32, "2" * 32, "3" * 32


class FakeBroker:
    def __init__(self):
        self.requests = []
        self.calls = 0
        self.value = {"available": True, "state": "running", "system": True,
                      "job": {"id": REPAIR, "command": "repair", "state": "running", "steps": []}}

    def status(self):
        self.calls += 1
        return copy.deepcopy(self.value)

    def request(self, command):
        self.requests.append(command)
        ident = {"cleanup": CLEANUP, "health": HEALTH}[command]
        self.value["job"] = {"id": ident, "command": command, "state": "running", "steps": []}
        return {"ok": True, "accepted": True, "job_id": ident}

    def terminal(self, command="repair", state="completed", **extra):
        self.value["job"] = {"id": {"repair": REPAIR, "cleanup": CLEANUP, "health": HEALTH}[command],
                             "command": command, "state": state,
                             "steps": [{"name": name, "exitCode": 0,
                                        "summary": "private local summary", "log": "private local log path"}
                                       for name in sequence.STEPS[command]], **extra}


class MaintenanceSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="maintenance-sequence-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.now = 1000.0
        self.broker = FakeBroker()
        self.subject = self.make()

    def make(self):
        return sequence.MaintenanceSequence(self.root, status_provider=self.broker.status,
                                            request_provider=self.broker.request, clock=lambda: self.now)

    def start(self, max_seconds=sequence.MAX_SECONDS):
        return self.subject.start(REPAIR, max_seconds)

    def receipts(self):
        return list(self.root.glob("*-*-*.json"))

    def test_01_construction_and_status_are_inert(self):
        self.assertIsNone(self.subject.status())
        self.assertFalse(self.root.exists())
        self.assertEqual(self.broker.calls, 0)
        self.assertEqual(self.broker.requests, [])

    def test_02_initial_exact_repair_and_available_required_before_state_persist(self):
        cases = ({"available": False, "state": "not_included"},
                 {"available": True, "state": "running", "job": {"id": CLEANUP, "command": "repair", "state": "running"}},
                 {"available": True, "state": "running", "job": {"id": REPAIR, "command": "cleanup", "state": "running"}},
                 {"available": True, "state": "running"})
        for value in cases:
            with self.subTest(value=value):
                self.broker.value = value
                with self.assertRaises(RuntimeError):
                    self.start()
                self.assertFalse(self.subject.path.exists())
        self.assertEqual(self.broker.requests, [])

    def test_03_running_exact_job_is_observed_only_and_start_cannot_rebind(self):
        original = self.start()
        self.assertEqual(self.subject.advance(), original)
        self.assertEqual(self.start(), original)
        with self.assertRaisesRegex(ValueError, "sequence_already_bound"):
            self.subject.start(CLEANUP)
        self.assertEqual(self.broker.requests, [])

    def test_04_successful_fixed_chain_archives_full_private_results_once(self):
        self.start()
        for command, next_stage in (("repair", "cleanup"), ("cleanup", "health"), ("health", "health")):
            self.broker.terminal(command)
            result = self.subject.advance()
            self.assertEqual(result["stage"], next_stage)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.broker.requests, ["cleanup", "health"])
        self.assertEqual(len(result["receipts"]), 3)
        self.assertEqual(len(self.receipts()), 3)
        for path in self.receipts():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["broker_result"]["job"]["steps"][0]["summary"], "private local summary")
        self.subject.advance()
        self.make().advance()
        self.assertEqual(self.broker.requests, ["cleanup", "health"])
        self.assertNotIn("private local", json.dumps(result))

    def test_05_failed_terminal_is_archived_and_stops_without_finished_timestamp(self):
        self.start()
        self.broker.terminal(state="failed", error="private error")
        result = self.subject.advance()
        self.assertEqual(result["reason"], "job_failed")
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(self.broker.requests, [])

    def test_06_interrupted_is_terminal_without_finished_and_never_replayed(self):
        self.start()
        self.broker.terminal(state="interrupted", steps=[])
        result = self.subject.advance()
        self.assertEqual(result["reason"], "job_interrupted")
        self.assertEqual(len(self.receipts()), 1)
        self.make().advance()
        self.assertEqual(self.broker.requests, [])

    def test_07_completed_requires_exact_nonempty_steps_order_and_integer_exit_codes(self):
        valid = [{"name": name, "exitCode": 0} for name in sequence.STEPS["repair"]]
        cases = ([], valid[:1], list(reversed(valid)), [valid[0], valid[0]],
                 [valid[0], {"name": "system-files-repair", "exitCode": True}],
                 [valid[0], {"name": "system-files-repair", "exitCode": 0.0}],
                 [valid[0], {"name": "system-files-repair", "exitCode": 5}])
        for steps in cases:
            self.assertEqual(sequence.MaintenanceSequence._result_reason(
                {"state": "completed", "steps": steps}, "repair"), "invalid_steps")
        self.start()
        self.broker.terminal(steps=[])
        self.assertEqual(self.subject.advance()["reason"], "invalid_steps")
        self.assertEqual(self.broker.requests, [])

    def test_08_restart_flag_or_3010_stops_before_next_command(self):
        for use_flag in (False, True):
            with self.subTest(flag=use_flag):
                root = Path(self.temporary.name) / str(use_flag)
                subject = sequence.MaintenanceSequence(root, status_provider=self.broker.status,
                                                       request_provider=self.broker.request, clock=lambda: self.now)
                self.broker.value = FakeBroker().value
                subject.start(REPAIR)
                self.broker.terminal(restart_required=use_flag)
                if not use_flag:
                    self.broker.value["job"]["steps"][0]["exitCode"] = 3010
                self.assertEqual(subject.advance()["reason"], "restart_required")
        self.assertEqual(self.broker.requests, [])

    def test_09_repair_review_is_not_failure_but_health_review_is_not_verified(self):
        self.start()
        self.broker.terminal(review_required=True)
        self.assertEqual(self.subject.advance()["stage"], "cleanup")
        self.broker.terminal("cleanup")
        self.subject.advance()
        self.broker.terminal("health", review_required=True)
        self.assertEqual(self.subject.advance()["reason"], "not_verified")
        self.assertEqual(self.broker.requests, ["cleanup", "health"])
        self.assertEqual(len(self.receipts()), 3)

    def test_10_foreign_job_and_wrong_command_stop_instead_of_adopting(self):
        self.start()
        self.broker.terminal("cleanup")
        self.assertEqual(self.subject.advance()["reason"], "job_mismatch")
        self.assertEqual(self.broker.requests, [])
        self.assertEqual(self.receipts(), [])

    def test_11_unavailable_or_status_exception_stops_without_commands(self):
        self.start()
        self.subject.status_provider = mock.Mock(side_effect=OSError("private path"))
        result = self.subject.advance()
        self.assertEqual(result["reason"], "broker_unavailable")
        self.assertNotIn("private path", json.dumps(result))
        self.assertEqual(self.broker.requests, [])

    def test_12_deadline_and_backwards_clock_stop_without_touching_job(self):
        self.start(max_seconds=60)
        self.now += 60
        calls = self.broker.calls
        self.assertEqual(self.subject.advance()["reason"], "timeout")
        self.assertEqual(self.broker.calls, calls)
        self.assertEqual(self.broker.requests, [])
        state = self.subject.status()
        state.update(status="observing", reason=None)
        self.subject._save(state)
        self.now = 999
        self.assertEqual(self.subject.advance()["reason"], "clock_moved_backwards")
        # Slow observation cannot launch the next command after the deadline.
        other = sequence.MaintenanceSequence(Path(self.temporary.name) / "slow",
                    status_provider=self.broker.status, request_provider=self.broker.request,
                    clock=lambda: self.now)
        self.now = 1000
        self.broker.value = FakeBroker().value
        other.start(REPAIR, 1)
        self.broker.terminal()
        def slow_status():
            self.now += 2
            return self.broker.status()
        other.status_provider = slow_status
        self.assertEqual(other.advance()["reason"], "timeout")
        self.assertEqual(self.broker.requests, [])

    def test_13_restart_while_observing_resumes_exact_accepted_id_without_duplicate(self):
        self.start()
        self.broker.terminal()
        self.subject.advance()
        restarted = self.make()
        self.assertEqual(restarted.start(REPAIR)["job_id"], CLEANUP)
        restarted.advance()
        self.assertEqual(self.broker.requests, ["cleanup"])
        self.broker.terminal("cleanup")
        restarted.advance()
        self.assertEqual(self.broker.requests, ["cleanup", "health"])

    def test_14_crash_after_durable_intent_before_request_is_uncertain_no_replay(self):
        self.start()
        self.broker.terminal()
        self.subject.request_provider = mock.Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.subject.advance()
        self.assertEqual(self.subject.status()["intent"]["command"], "cleanup")
        self.assertEqual(self.make().advance()["reason"], "request_uncertain")
        self.assertEqual(self.broker.requests, [])

    def test_15_request_exception_bad_response_and_busy_race_are_never_retried(self):
        self.start()
        self.broker.terminal()
        self.subject.request_provider = mock.Mock(return_value={"ok": False, "error": "busy"})
        self.assertEqual(self.subject.advance()["reason"], "request_uncertain")
        self.subject.advance()
        self.subject.request_provider.assert_called_once_with("cleanup")
        for reply in ({"ok": 1, "accepted": True, "job_id": CLEANUP},
                      {"ok": True, "accepted": True, "job_id": REPAIR}, {}, None):
            with self.subTest(reply=reply):
                root = Path(self.temporary.name) / ("bad-" + str(len(list(Path(self.temporary.name).iterdir()))))
                subject = sequence.MaintenanceSequence(root, status_provider=self.broker.status,
                                                       request_provider=mock.Mock(return_value=reply), clock=lambda: self.now)
                subject.start(REPAIR)
                self.assertEqual(subject.advance()["reason"], "request_uncertain")
        root = Path(self.temporary.name) / "exception"
        subject = sequence.MaintenanceSequence(root, status_provider=self.broker.status,
                                               request_provider=mock.Mock(side_effect=TimeoutError), clock=lambda: self.now)
        subject.start(REPAIR)
        self.assertEqual(subject.advance()["reason"], "request_uncertain")

    def test_16_crash_after_request_before_accepted_id_persist_never_replays(self):
        self.start()
        self.broker.terminal()
        save = self.subject._save
        def crash(state):
            if state["stage"] == "cleanup":
                raise OSError("simulated persistence failure")
            save(state)
        with mock.patch.object(self.subject, "_save", side_effect=crash):
            with self.assertRaises(OSError):
                self.subject.advance()
        self.assertEqual(self.broker.requests, ["cleanup"])
        self.assertIsNotNone(self.make().status()["intent"])
        self.assertEqual(self.make().advance()["reason"], "request_uncertain")
        self.assertEqual(self.broker.requests, ["cleanup"])

    def test_17_crash_after_archive_before_state_save_recovers_receipt_idempotently(self):
        self.start()
        self.broker.terminal()
        # Simulate abrupt process death, not a handled ordinary persistence error.
        with mock.patch.object(self.subject, "_save", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.advance()
        archived = self.receipts()
        self.assertEqual(len(archived), 1)
        original = archived[0].read_bytes()
        self.assertEqual(self.make().advance()["stage"], "cleanup")
        self.assertEqual(archived[0].read_bytes(), original)
        self.assertEqual(self.broker.requests, ["cleanup"])

    def test_18_shared_lock_excludes_parallel_advance_and_runner_without_broker_calls(self):
        self.start()
        calls = self.broker.calls
        with sequence.exclusive_lock(self.subject.lock_path, blocking=False):
            for operation in (self.make().advance, lambda: self.make().run(REPAIR)):
                with self.assertRaises((OSError, RuntimeError)):
                    operation()
        self.assertEqual(self.broker.calls, calls)
        self.assertEqual(self.broker.requests, [])

    def test_19_strict_json_and_schema_refuse_corruption_without_recovery_or_requests(self):
        state = self.start()
        cases = (b'{"version":1,"version":1}', b'{"version":NaN}',
                 json.dumps({**state, "version": True}).encode(),
                 json.dumps({**state, "arbitrary_command": "scan"}).encode(),
                 json.dumps({**state, "stage": "scan"}).encode())
        for raw in cases:
            self.subject.path.write_bytes(raw)
            with self.assertRaises((ValueError, KeyError)):
                self.make().advance()
        self.assertEqual(self.broker.requests, [])

    def test_21_deadline_crossed_during_intent_persistence_never_dispatches(self):
        self.start(max_seconds=1)
        self.broker.terminal()
        save = self.subject._save
        def slow_save(state):
            save(state)
            if state["status"] == "observing" and state["intent"] is not None:
                self.now = 1002
        with mock.patch.object(self.subject, "_save", side_effect=slow_save):
            result = self.subject.advance()
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(self.broker.requests, [])
        self.assertEqual(self.make().advance()["reason"], "timeout")
        self.assertEqual(self.broker.requests, [])

    def test_20_bounded_runner_cli_and_restricted_commands_use_only_fakes(self):
        def advance_clock(seconds):
            self.assertLessEqual(seconds, sequence.POLL_SECONDS)
            self.now += seconds
        result = self.subject.run(REPAIR, max_seconds=61, sleep=advance_clock)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(self.now, 1061)
        self.assertEqual(self.broker.requests, [])
        for command in ("repair", "scan", "status", "cleanup C:/", "cmd.exe", None):
            with self.assertRaises(ValueError):
                sequence.MaintenanceSequence._broker_request(command)
        for ident, timeout in (("not-id", 10), (REPAIR, True), (REPAIR, 21601)):
            with self.assertRaises(ValueError):
                self.subject.start(ident, timeout)
        with mock.patch.object(sequence, "MaintenanceSequence") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.run.return_value = {"status": "completed"}
            self.assertEqual(sequence.main(["--repair-id", REPAIR, "--max-seconds", "90"]), 0)
            factory.return_value.run.assert_called_once_with(REPAIR, 90)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sequence.main(["--repair-id", REPAIR, "--command", "scan"])


if __name__ == "__main__":
    unittest.main()
