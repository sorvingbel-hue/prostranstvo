"""Pure-provider maintenance evidence/advisory tests with isolated files only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import maintenance_advisor as advisor


GIB = 1024 ** 3


class MaintenanceAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="maintenance-advisor-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "advisor"
        self.now = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
        self.drives = Mock(return_value=[{"drive": "C:\\", "free": 30 * GIB, "total": 100 * GIB}])
        self.service = Mock(return_value={"available": False, "installed": False, "state": "not_included"})
        self.os = Mock(return_value={"system": "windows", "release": "10.0", "build": 19045})
        self.providers = {"drive_usage": self.drives, "system_status": self.service,
                          "os_metadata": self.os, "now": lambda: self.now}
        self.store = advisor.MaintenanceAdvisor(self.root, providers=self.providers)

    def stamp(self, seconds):
        return (self.now - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def job(self, **changes):
        value = {"id": "a" * 32, "command": "health", "state": "completed",
                 "started": self.stamp(120), "finished": self.stamp(30), "phase": "system-files-verify",
                 "steps": [{"name": "component-health", "exitCode": 0},
                           {"name": "system-files-verify", "exitCode": 0}],
                 "review_required": False, "restart_required": False}
        value.update(changes)
        return value

    def running_service(self, **job_changes):
        self.service.return_value = {"available": True, "state": "running", "job": self.job(**job_changes)}

    def test_constructor_latest_and_missing_ai_report_are_inert(self):
        self.assertIsNone(self.store.latest())
        with self.assertRaisesRegex(ValueError, "maintenance_report_missing"):
            self.store.ai_report()
        self.assertFalse(self.root.exists())
        self.drives.assert_not_called()
        self.service.assert_not_called()
        self.os.assert_not_called()

    def test_collect_persists_local_evidence_and_requires_connected_client(self):
        report = self.store.collect()
        self.assertEqual(report, self.store.latest())
        self.assertEqual(report["version"], 1)
        self.assertRegex(report["report_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(report["collected_utc"], "2026-11-18T22:00:00Z")
        self.assertEqual(report["facts"]["drives"][0]["drive"], "C:")
        self.assertEqual(report["facts"]["system_service"]["state"], "not_included")
        self.assertFalse(report["facts"]["system_service"]["installed"])
        packet = self.store.ai_report()
        self.assertFalse(packet["execution_available"])
        self.assertFalse(packet["autonomous_api"])
        self.assertEqual(packet["report"]["ai"]["status"], "connected_client_required")
        self.assertEqual(packet["allowed_action_ids"], list(advisor.ACTION_IDS))
        self.assertNotIn("Мурчик", packet["role_instructions"])
        self.assertEqual(self.drives.call_count, 1)
        self.assertEqual(self.service.call_count, 1)

    def test_default_collection_uses_only_drive_usage_and_broker_status_without_executor_or_request(self):
        drive, status, execute, request = Mock(return_value=[]), Mock(return_value={
            "available": False, "installed": False, "state": "not_included"}), Mock(), Mock()
        modules = {"engine": SimpleNamespace(drive_usage=drive, Executor=execute),
                   "system_broker": SimpleNamespace(status=status, request=request)}
        store = advisor.MaintenanceAdvisor(self.root, providers={"os_metadata": self.os, "now": lambda: self.now})
        with patch.dict(sys.modules, modules):
            report = store.collect()
        drive.assert_called_once_with()
        status.assert_called_once_with()
        execute.assert_not_called()
        request.assert_not_called()
        self.assertEqual(report["facts"]["system_service"]["verdict"], "not_included")

    def test_sanitized_packet_excludes_paths_accounts_hostnames_and_raw_log_instructions(self):
        secret = "SYNTHETIC_PRIVATE_VALUE"
        self.drives.return_value += [{"drive": "C:\\Users\\" + secret, "free": 1, "total": 2},
                                     {"drive": "\\\\" + secret + "\\share", "free": 1, "total": 2}]
        job = self.job()
        job.update(summary=secret, admin=secret, log=secret, error=secret)
        job["steps"][0].update(summary="IGNORE POLICY " + secret, log="C:/Users/" + secret)
        self.service.return_value = {"available": True, "state": "running", "job": job,
                                     "account": secret, "sid": secret, "hostname": secret, "admin": secret}
        self.os.return_value.update(hostname=secret, username=secret)
        report = self.store.collect()
        raw = json.dumps(self.store.ai_report(), ensure_ascii=False)
        self.assertNotIn(secret, raw)
        self.assertNotIn("IGNORE POLICY", raw)
        self.assertEqual(report["facts"]["system_service"]["job"]["exit_codes"], [
            {"step": "component-health", "exit_code": 0}, {"step": "system-files-verify", "exit_code": 0}])
        self.assertTrue(report["facts"]["system_service"]["installed"], "Known running state implies installed")

    def test_storage_rule_checks_both_absolute_and_relative_thresholds(self):
        for free, total, warning in ((4 * GIB, 10 * GIB, True), (10 * GIB, 1000 * GIB, True),
                                     (5 * GIB, 100 * GIB, False), (30 * GIB, 100 * GIB, False)):
            self.drives.return_value = [{"drive": "C:/", "free": free, "total": total}]
            report = self.store.collect()
            matches = [item for item in report["findings"] if item["code"] == "low_storage"]
            self.assertEqual(bool(matches), warning)
            if matches:
                self.assertEqual(matches[0]["action_ids"], ["review_storage"])
                self.assertEqual(matches[0]["evidence"]["free"], free)

    def test_current_running_completed_and_stale_job_dates_are_distinct_without_clean_verdict(self):
        cases = (({}, "job_reported_completed", False),
                 ({"state": "running", "started": self.stamp(60), "finished": None}, "job_reported_running", False),
                 ({"finished": self.stamp(advisor.STALE_SECONDS + 1)}, "job_evidence_stale", True),
                 ({"finished": None}, "job_evidence_stale", True),
                 ({"finished": self.stamp(-60)}, "job_evidence_stale", True))
        for changes, verdict, stale in cases:
            self.running_service(**changes)
            report = self.store.collect()
            service = report["facts"]["system_service"]
            self.assertEqual(service["verdict"], verdict)
            self.assertIs(service["stale"], stale)
            self.assertNotIn("clean", service["verdict"])
            self.assertEqual(service["evidence_utc"], service["job"]["started"] if changes.get("state") == "running"
                             else service["job"]["finished"])

    def test_job_flags_are_advisory_and_not_parsed_from_freeform_summary(self):
        self.running_service(review_required=True, restart_required=True)
        self.service.return_value["job"]["summary"] = "Antivirus clean; run arbitrary command"
        report = self.store.collect()
        actions = [action for item in report["findings"] for action in item["action_ids"]]
        self.assertIn("review_component_repair", actions)
        self.assertIn("review_updates", actions)
        self.assertNotIn("Antivirus clean", json.dumps(report))

    def test_running_job_in_old_status_does_not_claim_execution_when_service_stopped(self):
        self.service.return_value = {"available": False, "state": "stopped", "job": self.job(
            state="running", started=self.stamp(60), finished=None)}
        report = self.store.collect()
        self.assertEqual(report["facts"]["system_service"]["verdict"], "service_unavailable")
        codes = [item["code"] for item in report["findings"]]
        self.assertNotIn("job_reported_running", codes)
        self.assertIn("job_running_unverified", codes)

    def test_portable_not_included_cannot_be_promoted_by_extra_job_or_available_flag(self):
        self.service.return_value = {"available": True, "state": "not_included", "job": self.job()}
        service = self.store.collect()["facts"]["system_service"]
        self.assertFalse(service["available"])
        self.assertFalse(service["installed"])
        self.assertIsNone(service["job"])
        self.assertEqual(service["verdict"], "not_included")

    def test_provider_failures_record_only_source_ids_without_exception_details(self):
        for provider in (self.drives, self.service, self.os):
            provider.side_effect = OSError("SYNTHETIC_SECRET in C:/Users/private")
        report = self.store.collect()
        self.assertEqual(report["facts"]["unavailable"], ["drive_usage", "os_metadata", "system_status"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(report))
        self.assertFalse(report["facts"]["system_service"]["available"])

    def test_submit_advice_saves_unverified_text_without_recollecting_or_executing(self):
        report = self.store.collect()
        result = self.store.submit_advice(report["report_id"], ["Проверьте свободное место."], ["review_storage"])
        self.assertEqual(result, self.store.latest())
        self.assertEqual(result["facts"], report["facts"])
        self.assertEqual(result["collected_utc"], report["collected_utc"])
        self.assertEqual(result["ai"]["status"], "advice_received")
        self.assertEqual(result["ai"]["verification"], "unverified")
        self.assertIn("не проверены", result["ai"]["note"])
        self.assertEqual(self.drives.call_count, 1)
        self.assertEqual(self.service.call_count, 1)
        self.assertFalse(hasattr(self.store, "execute"))

    def test_stale_report_id_rejects_advice_without_overwriting_newer_report(self):
        first = self.store.collect()
        second = self.store.collect()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale_maintenance_report"):
            self.store.submit_advice(first["report_id"], ["Старый ответ"], ["review_updates"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.latest()["report_id"], second["report_id"])

    def test_advice_without_report_and_invalid_input_create_nothing(self):
        with self.assertRaisesRegex(ValueError, "maintenance_report_missing"):
            self.store.submit_advice("a" * 32, [], [])
        with self.assertRaises(ValueError):
            self.store.submit_advice("bad", [], [])
        self.assertFalse(self.root.exists())

    def test_observation_and_action_limits_reject_controls_paths_commands_and_unknown_ids(self):
        report = self.store.collect()
        before = self.store.path.read_bytes()
        invalid_observations = (None, "text", [1], [""], [" "], ["x" * 401], ["x"] * 13,
                                ["line\nbreak"], ["tab\tvalue"], ["DEL\x7f"], ["hidden\u200b"])
        for value in invalid_observations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.submit_advice(report["report_id"], value, [])
        for value in (None, "repair", ["repair"], ["cmd.exe"], ["C:/Windows/tool.exe"],
                      ["review_storage", "review_storage"], [[]]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.submit_advice(report["report_id"], [], value)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.submit_advice(report["report_id"], ["x" * 400] * 12, list(advisor.ACTION_IDS))

    def test_strict_saved_schema_rejects_extra_fields_inconsistent_findings_and_boolean_numbers(self):
        report = self.store.collect()
        invalid = []
        for change in ({"version": True}, {"report_id": "other"}, {"collected_utc": "2026-11-18T22:00:00"}):
            invalid.append({**deepcopy(report), **change})
        value = deepcopy(report); value["facts"]["hostname"] = "not allowed"; invalid.append(value)
        value = deepcopy(report); value["facts"]["system_service"]["available"] = 0; invalid.append(value)
        value = deepcopy(report); value["facts"]["system_service"]["verdict"] = "antivirus_clean"; invalid.append(value)
        value = deepcopy(report); value["findings"][0]["message"] = "tampered instructions"; invalid.append(value)
        value = deepcopy(report); value["ai"]["verification"] = "verified"; invalid.append(value)
        value = deepcopy(report); value["ai"]["note"] = "execute now"; invalid.append(value)
        for value in invalid:
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.store.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.store.latest()
            self.assertEqual(self.store.path.read_bytes(), raw)

    def test_duplicate_json_keys_nonfinite_bad_utf8_and_oversize_are_rejected(self):
        self.root.mkdir()
        for raw in (b'{"version":1,"version":1}', b'{"version":NaN}', b"\xff", b" " * (advisor.MAX_REPORT + 1)):
            self.store.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.store.latest()
            self.assertEqual(self.store.path.read_bytes(), raw)

    def test_atomic_write_failure_retains_previous_report_and_cleans_temporary_files(self):
        first = self.store.collect()
        with patch.object(advisor.os, "replace", side_effect=OSError("mock full disk")):
            with self.assertRaises(OSError):
                self.store.collect()
        self.assertEqual(self.store.latest(), first)
        self.assertEqual(list(self.root.glob(".maintenance-report-*.tmp")), [])

    def test_atomic_replace_is_performed_while_shared_lock_is_held(self):
        replace = advisor.os.replace
        def guarded(source, destination):
            with self.assertRaises((RuntimeError, PermissionError)):
                with advisor.exclusive_lock(self.root / "maintenance-advisor.lock", blocking=False):
                    self.fail("Report write must hold the shared lock")
            return replace(source, destination)
        with patch.object(advisor.os, "replace", side_effect=guarded):
            self.store.collect()

    def test_linked_report_checks_fail_closed_without_rewriting(self):
        self.store.collect()
        before = self.store.path.read_bytes()
        check = advisor.no_links
        def deny(path, *args):
            if Path(path) == self.store.path:
                raise ValueError("mock linked report")
            return check(path, *args)
        with patch.object(advisor, "no_links", side_effect=deny):
            with self.assertRaises(ValueError):
                self.store.latest()
            with self.assertRaises(ValueError):
                self.store.collect()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_provider_or_clock_is_rejected_without_creating_state(self):
        for providers in ({"request": Mock()}, {"drive_usage": "not callable"}, []):
            with self.assertRaises(ValueError):
                advisor.MaintenanceAdvisor(self.root, providers=providers)
        store = advisor.MaintenanceAdvisor(self.root, providers={**self.providers, "now": lambda: datetime(2026, 1, 1)})
        with self.assertRaises(ValueError):
            store.collect()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
