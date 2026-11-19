"""Security evidence tests: isolated fixtures only, no device/UI/network work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security_report as security


class SecurityReportTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="security-report-fixture-")
        self.addCleanup(fixture.cleanup)
        self.parent = Path(fixture.name)
        self.root = self.parent / "reports"
        self.now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        self.snapshot = {
            "windows": {"error": None, "data": {"product": "windows_11", "release": "25H2", "build": 26200, "revision": 1234}},
            "updates": {"error": None, "data": {"latest_kb_id": "KB1234567", "latest_kb_installed_date": "2026-11-17"}},
            "defender": {"error": None, "data": {
                "antivirus_enabled": True, "realtime_enabled": False, "signatures_version": "1.451.22.0",
                "signatures_updated_utc": "2026-11-19T05:00:00Z", "quick_scan_end_utc": "2026-11-18T04:00:00Z",
                "full_scan_end_utc": None, "quick_scan_overdue": False, "full_scan_overdue": True}},
            "threats": {"error": None, "data": {"active_threat_count": 0}},
            "firewall": {"error": None, "data": {"domain_enabled": True, "private_enabled": False, "public_enabled": True}},
            "encryption": {"error": None, "data": {"system_volume_protection": "on", "system_volume_encryption": "fully_encrypted"}},
            "backup": {"error": None, "data": {"legacy_windows_backup_configuration_seen": True}},
        }
        self.provider = Mock(side_effect=lambda: deepcopy(self.snapshot))
        self.store = security.SecurityReport(self.root, providers={"snapshot": self.provider, "now": lambda: self.now})

    def field(self, report, group, name):
        return report["evidence"][group][name]

    def write(self, report):
        self.root.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps(report), encoding="utf-8")

    def test_constructor_and_missing_reads_are_inert(self):
        self.assertFalse(self.root.exists())
        self.assertIsNone(self.store.latest())
        with self.assertRaisesRegex(ValueError, "^security_report_missing$"):
            self.store.for_ai()
        self.assertFalse(self.root.exists())
        self.provider.assert_not_called()

    def test_default_location_uses_current_localappdata_never_deployment_root(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.parent / "profile")}, clear=False):
            store = security.SecurityReport(providers={"snapshot": self.provider})
        self.assertEqual(store.root, self.parent / "profile" / "ProstranstvoLocalBeta" / "data" / "security-report")
        self.assertFalse(store.root.exists())

    def test_collect_saves_only_own_bounded_report_and_lock(self):
        report = self.store.collect()
        self.assertEqual(report, self.store.latest())
        self.assertEqual(set(report), {"schema_version", "report_id", "collected_utc", "assessment", "evidence"})
        self.assertEqual(report["assessment"], "not_a_compromise_verdict")
        self.assertRegex(report["report_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(report["collected_utc"], "2026-11-19T08:00:00Z")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"latest.json", "report.lock"})
        self.assertLessEqual(self.store.path.stat().st_size, security.MAX_REPORT)
        self.provider.assert_called_once_with()

    def test_every_field_has_observation_source_and_explicit_unknown(self):
        report = self.store.collect()
        for group, (_, fields) in security._FIELDS.items():
            for name in fields:
                field = self.field(report, group, name)
                self.assertEqual(set(field), {"value", "observed_utc", "source", "unknown_reason"})
                self.assertEqual(field["observed_utc"], report["collected_utc"])
                self.assertTrue(field["source"])
                self.assertEqual(field["value"] is None, field["unknown_reason"] is not None)
        self.assertFalse(self.field(report, "defender", "realtime_enabled")["value"])
        self.assertEqual(self.field(report, "threats", "active_threat_count")["value"], 0)
        self.assertEqual(self.field(report, "defender", "full_scan_end_utc")["unknown_reason"], "missing_data")

    def test_backup_presence_never_becomes_restore_assurance(self):
        report = self.store.collect()
        self.assertTrue(self.field(report, "backup", "legacy_windows_backup_configuration_seen")["value"])
        restore = self.field(report, "backup_assurance", "restore_tested")
        self.assertIsNone(restore["value"])
        self.assertEqual(restore["unknown_reason"], "not_tested")
        self.snapshot["backup"]["data"]["legacy_windows_backup_configuration_seen"] = None
        report = self.store.collect()
        self.assertEqual(self.field(report, "backup", "legacy_windows_backup_configuration_seen")["unknown_reason"],
                         "no_supported_configuration_observed")

    def test_unmeasured_permissions_and_restore_ignore_supplied_healthy_claim(self):
        self.snapshot["permissions"] = {"error": None, "data": {"agent_permissions_coverage": "complete", "extension_permissions_coverage": "complete"}}
        self.snapshot["backup_assurance"] = {"error": None, "data": {"restore_tested": True}}
        report = self.store.collect()
        for field in report["evidence"]["permissions"].values():
            self.assertIsNone(field["value"])
            self.assertEqual(field["unknown_reason"], "not_collected")
        self.assertIsNone(self.field(report, "backup_assurance", "restore_tested")["value"])

    def test_provider_exception_never_leaks_raw_exception_or_becomes_healthy(self):
        self.provider.side_effect = RuntimeError("PRIVATE account C:/Users/SECRET token=VALUE")
        report = self.store.collect()
        raw = json.dumps(report)
        for value in ("PRIVATE", "SECRET", "VALUE", "C:/Users"):
            self.assertNotIn(value, raw)
        self.assertEqual(self.field(report, "threats", "active_threat_count")["unknown_reason"], "provider_error")
        self.assertIsNone(self.field(report, "threats", "active_threat_count")["value"])

    def test_denied_provider_is_explicit_unknown(self):
        self.provider.side_effect = PermissionError("PRIVATE")
        report = self.store.collect()
        self.assertEqual(self.field(report, "windows", "product")["unknown_reason"], "access_denied")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_fixed_default_failure_codes_are_preserved(self):
        for code in ("timeout", "output_limit", "unsupported", "invalid_data"):
            with self.subTest(code=code):
                self.provider.side_effect = security._ProviderFailure(code)
                report = self.store.collect()
                self.assertEqual(self.field(report, "firewall", "public_enabled")["unknown_reason"], code)

    def test_partial_provider_failure_retains_unrelated_facts(self):
        self.snapshot["defender"] = {"error": "access_denied", "data": {"antivirus_enabled": True}}
        self.snapshot["threats"] = {"error": "raw PRIVATE exception", "data": {"active_threat_count": 0}}
        report = self.store.collect()
        self.assertEqual(self.field(report, "windows", "build")["value"], 26200)
        self.assertEqual(self.field(report, "defender", "antivirus_enabled")["unknown_reason"], "access_denied")
        self.assertEqual(self.field(report, "threats", "active_threat_count")["unknown_reason"], "provider_error")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_missing_or_malformed_provider_does_not_default_to_true_or_zero(self):
        for snapshot in (None, [], {}, {"firewall": []}, {"threats": {"data": []}}, {"windows": {"data": {}}}):
            with self.subTest(snapshot=snapshot):
                self.provider.side_effect = lambda s=snapshot: s
                report = self.store.collect()
                self.assertIsNone(self.field(report, "threats", "active_threat_count")["value"])
                self.assertIsNone(self.field(report, "firewall", "public_enabled")["value"])

    def test_privacy_redaction_drops_extra_fields_and_rejects_untrusted_text(self):
        secret = "SYNTHETIC_PRIVATE_TOKEN"
        for group in self.snapshot.values():
            group["data"].update(hostname=secret, account=secret, sid=secret, rawlogs=secret,
                                 recovery_key=secret, userfiles=secret, browser_history=secret, messages=secret)
            group["raw_exception"] = secret
        self.snapshot["windows"]["data"]["product"] = "Windows " + secret
        self.snapshot["updates"]["data"]["latest_kb_id"] = "KB1234567\n" + secret
        self.snapshot["encryption"]["data"]["system_volume_protection"] = "on;" + secret
        self.snapshot["defender"]["data"]["signatures_version"] = "1.2 C:/Users/" + secret
        raw = json.dumps(self.store.for_ai()) if self.store.latest() else json.dumps(self.store.collect())
        self.assertNotIn(secret, raw)
        self.assertNotIn("C:/Users/", raw)
        self.assertNotIn("recovery_key", raw)
        self.assertIsNone(self.store.latest()["evidence"]["windows"]["product"]["value"])

    def test_scalar_types_ranges_and_dates_are_strict(self):
        invalid = (("windows", "build", True), ("windows", "revision", -1),
                   ("defender", "antivirus_enabled", 1), ("threats", "active_threat_count", False),
                   ("threats", "active_threat_count", 1000001), ("firewall", "public_enabled", "true"),
                   ("updates", "latest_kb_installed_date", "2026-02-30"),
                   ("defender", "full_scan_end_utc", "1601-01-01T00:00:00Z"),
                   ("defender", "quick_scan_end_utc", "2026-11-18T04:00:00"),
                   ("backup", "legacy_windows_backup_configuration_seen", False))
        for group, name, value in invalid:
            with self.subTest(group=group, name=name):
                fixture = deepcopy(self.snapshot)
                fixture[group]["data"][name] = value
                self.provider.side_effect = lambda f=fixture: f
                field = self.field(self.store.collect(), group, name)
                self.assertIsNone(field["value"])
                self.assertEqual(field["unknown_reason"], "invalid_data")

    def test_ai_packet_reuses_report_without_collecting_and_has_no_executor(self):
        report = self.store.collect()
        packet = self.store.for_ai(report["report_id"])
        self.assertEqual(packet["report"], report)
        self.assertFalse(packet["execution_available"])
        self.assertFalse(packet["network_available"])
        self.assertFalse(packet["freshness"]["stale"])
        self.provider.assert_called_once_with()
        self.assertFalse(hasattr(self.store, "execute"))

    def test_ai_freshness_marks_old_and_future_reports_unknown(self):
        self.store.collect()
        for offset, stale, age in ((security.STALE_SECONDS, False, security.STALE_SECONDS),
                                   (security.STALE_SECONDS + 1, True, security.STALE_SECONDS + 1), (-1, True, None)):
            with self.subTest(offset=offset):
                self.store.providers["now"] = lambda o=offset: self.now + timedelta(seconds=o)
                self.assertEqual(self.store.for_ai()["freshness"], {"age_seconds": age, "stale": stale})

    def test_report_id_guard_rejects_wrong_or_arbitrary_ids(self):
        self.store.collect()
        for ident in ("../latest.json", "A" * 32, 1, "a" * 33):
            with self.subTest(ident=ident), self.assertRaisesRegex(ValueError, "^invalid_security_report_id$"):
                self.store.for_ai(ident)
        with self.assertRaisesRegex(ValueError, "^security_report_changed$"):
            self.store.for_ai("a" * 32)

    def test_latest_rejects_unknown_fields_and_forged_healthy_assurance(self):
        report = self.store.collect()
        mutations = []
        forged = deepcopy(report); forged["hostname"] = "PRIVATE"; mutations.append(forged)
        forged = deepcopy(report); forged["schema_version"] = True; mutations.append(forged)
        forged = deepcopy(report); forged["report_id"] = "../PRIVATE"; mutations.append(forged)
        forged = deepcopy(report); forged["evidence"]["windows"]["build"]["source"] = "PRIVATE"; mutations.append(forged)
        forged = deepcopy(report); forged["evidence"]["windows"]["build"]["value"] = True; mutations.append(forged)
        forged = deepcopy(report); forged["evidence"]["firewall"]["public_enabled"]["unknown_reason"] = "unavailable"; mutations.append(forged)
        forged = deepcopy(report); forged["evidence"]["backup_assurance"]["restore_tested"] = {
            "value": True, "source": "not_collected", "observed_utc": report["collected_utc"], "unknown_reason": None}; mutations.append(forged)
        for forged in mutations:
            with self.subTest(forged=forged["report_id"]):
                self.write(forged)
                with self.assertRaisesRegex(ValueError, "^invalid_security_report$"):
                    self.store.latest()

    def test_latest_rejects_duplicate_keys_nan_non_json_and_oversize(self):
        self.root.mkdir()
        for raw in (b'{"schema_version":1,"schema_version":1}', b'NaN', b'[]', b'\xff', b'{'):
            self.store.path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "^invalid_security_report$"):
                self.store.latest()
        self.store.path.write_bytes(b" " * (security.MAX_REPORT + 1))
        with self.assertRaisesRegex(ValueError, "^security_report_too_large$"):
            self.store.latest()

    def test_atomic_failure_preserves_previous_report_and_removes_temporary(self):
        first = self.store.collect()
        with patch.object(security.os, "replace", side_effect=OSError("PRIVATE path")):
            with self.assertRaisesRegex(ValueError, "^security_report_store_unavailable$"):
                self.store.collect()
        self.assertEqual(self.store.latest(), first)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"latest.json", "report.lock"})

    def test_root_escape_and_changed_report_path_are_rejected_before_provider(self):
        store = security.SecurityReport(self.parent / ".." / "escaped", providers={"snapshot": self.provider})
        with self.assertRaisesRegex(ValueError, "^unsafe_security_report_path$"):
            store.collect()
        self.store.path = self.parent / "outside.json"
        with self.assertRaisesRegex(ValueError, "^unsafe_security_report_path$"):
            self.store.collect()
        self.provider.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_hardlinked_report_rejected_without_modifying_link_target(self):
        outside = self.parent / "private.json"
        outside.write_text("PRIVATE", encoding="utf-8")
        self.root.mkdir()
        os.link(outside, self.store.path)
        for action in (self.store.collect, self.store.latest):
            with self.assertRaisesRegex(ValueError, "^unsafe_security_report_path$"):
                action()
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRIVATE")
        self.provider.assert_not_called()

    def test_hardlinked_lock_rejected_without_modifying_link_target(self):
        outside = self.parent / "private.txt"
        outside.write_text("PRIVATE", encoding="utf-8")
        self.root.mkdir()
        os.link(outside, self.root / "report.lock")
        with self.assertRaisesRegex(ValueError, "^unsafe_security_report_path$"):
            self.store.collect()
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRIVATE")
        self.assertFalse(self.store.path.exists())

    def test_symlink_root_rejected_when_supported_without_writes(self):
        destination = self.parent / "outside"
        destination.mkdir()
        try:
            self.root.symlink_to(destination, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable for current test account")
        with self.assertRaisesRegex(ValueError, "^unsafe_security_report_path$"):
            self.store.collect()
        self.assertEqual(list(destination.iterdir()), [])
        self.provider.assert_not_called()

    def test_bad_provider_map_and_clock_are_rejected(self):
        for providers in ([], {"arbitrary_command": lambda: None}, {"snapshot": "cmd"}):
            with self.assertRaisesRegex(ValueError, "^invalid_security_providers$"):
                security.SecurityReport(self.root, providers=providers)
        self.store.providers["now"] = lambda: datetime(2026, 11, 19)
        with self.assertRaisesRegex(ValueError, "^invalid_security_clock$"):
            self.store.collect()
        self.assertFalse(self.root.exists())


class FixedSnapshotRunnerTests(unittest.TestCase):
    EXE = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

    def setUp(self):
        available = patch.object(security, "_pipe_available", side_effect=lambda pipe: len(pipe.getbuffer()) - pipe.tell() or None)
        read = patch.object(security, "_pipe_read", side_effect=lambda pipe, count: pipe.read(count))
        available.start()
        read.start()
        self.addCleanup(available.stop)
        self.addCleanup(read.stop)

    def process(self, raw=b"{}", code=0):
        process = Mock()
        process.stdout = io.BytesIO(raw)
        process.returncode = code
        process.wait.return_value = code
        process.poll.return_value = code
        return process

    def test_fixed_noninteractive_command_and_bounded_output(self):
        process = self.process(b'{"windows":{"error":null,"data":{}}}')
        with patch.dict(os.environ, {"PSModulePath": "C:/UNTRUSTED/Modules"}), patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                security.subprocess, "Popen", return_value=process) as popen:
            snapshot = security._default_snapshot()
        self.assertEqual(snapshot, {"windows": {"error": None, "data": {}}})
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:5], ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(args[0][-1], security._SNAPSHOT_SCRIPT)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(kwargs["cwd"], str(self.EXE.parents[2]))
        self.assertEqual(kwargs["env"]["PSModulePath"], str(self.EXE.parent / "Modules"))
        self.assertNotIn("UNTRUSTED", kwargs["env"]["PSModulePath"])
        self.assertEqual([k for k in kwargs["env"] if k.casefold() == "psmodulepath"], ["PSModulePath"])
        self.assertEqual(kwargs["bufsize"], 0)
        process.wait.assert_called_once()
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], security.TIMEOUT_SECONDS)
        self.assertIn("$env:PSModulePath=[System.IO.Path]::Combine($PSHOME,'Modules')", security._SNAPSHOT_SCRIPT)

    def test_output_limit_kills_only_snapshot_child_and_returns_fixed_code(self):
        process = self.process(b" " * (security.MAX_STDOUT + 200))
        process.poll.return_value = None
        with patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                security.subprocess, "Popen", return_value=process):
            with self.assertRaises(security._ProviderFailure) as caught:
                security._default_snapshot()
        self.assertEqual(caught.exception.code, "output_limit")
        process.kill.assert_called_once_with()

    def test_timeout_returns_fixed_code_and_closes_child_pipe(self):
        process = self.process()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("PRIVATE", 18), 1]
        with patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                security.subprocess, "Popen", return_value=process):
            with self.assertRaises(security._ProviderFailure) as caught:
                security._default_snapshot()
        self.assertEqual(str(caught.exception), "timeout")
        process.kill.assert_called_once_with()
        self.assertTrue(process.stdout.closed)

    def test_invalid_stdout_and_process_failure_never_return_raw_output(self):
        for raw, code, expected in ((b"PRIVATE", 0, "invalid_data"), (b"[]", 0, "invalid_data"),
                                     (b'{"x":1,"x":2}', 0, "invalid_data"), (b"{}", 1, "unavailable")):
            with self.subTest(raw=raw, code=code):
                process = self.process(raw, code)
                with patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                        security.subprocess, "Popen", return_value=process):
                    with self.assertRaises(security._ProviderFailure) as caught:
                        security._default_snapshot()
                self.assertEqual(str(caught.exception), expected)

    def test_exited_child_with_inherited_stdout_handle_cannot_block_capture_or_close(self):
        process = self.process()
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                security.subprocess, "Popen", return_value=process), patch.object(
                security, "_pipe_available", return_value=0), patch.object(
                security, "_pipe_read") as read, patch.object(
                security.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                security.time, "sleep", side_effect=advance):
            with self.assertRaises(security._ProviderFailure) as caught:
                security._default_snapshot()
        self.assertEqual(str(caught.exception), "timeout")
        self.assertLessEqual(clock[0], security.TIMEOUT_SECONDS + 0.025)
        read.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(process.stdout.closed)

    def test_unavailable_pipe_never_reads_and_cleanup_wait_has_a_limit(self):
        process = self.process()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("PRIVATE", 1)
        with patch.object(security, "_powershell_path", return_value=self.EXE), patch.object(
                security.subprocess, "Popen", return_value=process), patch.object(
                security, "_pipe_available", side_effect=security._ProviderFailure("unavailable")), patch.object(
                security, "_pipe_read") as read:
            with self.assertRaises(security._ProviderFailure) as caught:
                security._default_snapshot()
        self.assertEqual(str(caught.exception), "unavailable")
        read.assert_not_called()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)
        self.assertTrue(process.stdout.closed)

    def test_fixed_script_has_no_elevation_scan_repair_or_inventory_commands(self):
        script = security._SNAPSHOT_SCRIPT.lower()
        for disallowed in ("start-mpscan", "start-mpwsdo", "set-mp", "remove-mp", "set-netfirewall", "enable-bitlocker",
                           "disable-bitlocker", "get-bitlocker", "keyprotector", "recoverypassword", "get-childitem",
                           "get-content", "invoke-webrequest", "invoke-restmethod", "start-process", "-verb runas",
                           "restart-computer", "stop-service", "set-service", "get-process"):
            self.assertNotIn(disallowed, script)
        for required in ("get-mpcomputerstatus", "get-mpthreat", "get-netfirewallprofile", "getprotectionstatus", "getconversionstatus"):
            self.assertIn(required, script)


if __name__ == "__main__":
    unittest.main()
