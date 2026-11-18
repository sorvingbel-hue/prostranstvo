"""Mock-only browser tests: no real process, GUI, profile, page or MCP starts."""
import importlib
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import browser_access as access


class BrowserUrlTests(unittest.TestCase):
    def test_normalizes_https_host_and_keeps_page_target(self):
        self.assertEqual(access.validate_https_url("HTTPS://Example.COM/page?q=one#two"),
                         "https://example.com/page?q=one#two")
        self.assertEqual(access.validate_https_url("https://[::1]:8443"), "https://[::1]:8443/")
        self.assertEqual(access.validate_https_url("https://пример.рф/"), "https://xn--e1afmkfd.xn--p1ai/")

    def test_rejects_schemes_credentials_controls_and_ambiguous_authorities(self):
        invalid = [None, "", "http://example.com", "javascript:alert(1)", "file:///C:/secret",
                   "https://user:pass@example.com/", "https://@example.com/",
                   "https://example.com\n/path", "https://example.com/%0d%0aheader",
                   "https://example.com/%00", "https://example.com/\u200b", " https://example.com/",
                   "https://example.com/a b", "https://example.com\\@evil.test/",
                   "https://example.com/%5cfile", "https://example.com/%zz", "https://example.com/%ff",
                   "https://example.com:99999/", "https://example.com:0/", "https://[invalid]/",
                   "https://[::1]example.com/", "https://[::1]:/", "https://[v1.example]/",
                   "https://[fe80::1%25ethernet]/", "https://example.com:/",
                   "https:///missing", "https://%65xample.com/", "https://-bad.example/",
                   "https://example.com/" + "a" * 8192]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid_https_url"):
                access.validate_https_url(value)


class BrowserAccessMockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "application-data"
        self.edge = access.known_application_paths()["edge"][0]
        self.chrome = access.known_application_paths()["chrome"][0]
        self.yandex = access.known_application_paths()["yandex"][0]
        self.addCleanup(patch.stopall)
        patch.object(access, "DATA_ROOT", self.data).start()
        self.probe = patch.object(access, "_path_is_file",
                                  side_effect=lambda path: path in (self.edge, self.chrome, self.yandex)).start()
        # This patch catches accidental starts even in planning/inventory tests.
        self.actual_spawn = patch.object(access.subprocess, "Popen",
                                         side_effect=AssertionError("Real process start forbidden in tests")).start()

    def test_inventory_checks_only_known_candidates_without_claiming_login(self):
        checked = []
        def fake_is_file(path):
            checked.append(path)
            return path in (self.edge, self.yandex)
        result = access.inventory_browsers(is_file=fake_is_file)
        rows = {row["browser_id"]: row for row in result["applications"]}
        expected = {path for paths in access.known_application_paths().values() for path in paths}
        self.assertEqual(set(checked), expected)
        self.assertEqual(rows["edge"]["status"], "found")
        self.assertEqual(rows["notion"]["status"], "not_found_in_known_paths")
        self.assertTrue(all(row["login_state"] == "not_inspected" for row in rows.values()))
        self.assertFalse(any(row["existing_session_access"] for row in rows.values()))
        self.assertFalse(result["process_started"])

    def test_unreadable_candidate_is_not_treated_as_proof_of_absence(self):
        result = access.inventory_browsers(is_file=Mock(side_effect=PermissionError()))
        self.assertTrue(all(row["status"] == "unreadable" for row in result["applications"]))

    def test_headless_config_is_stdio_with_own_separate_profiles_and_no_download_command(self):
        profiles = []
        for browser_id, channel in (("edge", "msedge"), ("chrome", "chrome")):
            plan = access.build_browser_access_plan(browser_id, "https://example.test/")
            server = plan["mcp_config"]["mcpServers"]["prostranstvo_browser"]
            self.assertEqual(server["type"], "stdio")
            self.assertEqual(server["command"], "playwright-mcp")
            self.assertEqual(server["args"], ["--headless", "--browser", channel,
                                             "--user-data-dir", plan["profile_path"], "--image-responses", "omit"])
            self.assertEqual(plan["runtime_status"], "planned_not_started")
            self.assertEqual(plan["browser_control"], "not_connected_until_MCP_started")
            self.assertEqual(plan["session_access"], "separate_profile_login_required")
            self.assertFalse(plan["existing_session_access"])
            self.assertFalse(plan["controllable"])
            profile = Path(plan["profile_path"])
            self.assertTrue(profile.is_relative_to(self.data / "browser-access/profiles"))
            profiles.append(profile)
        self.assertNotEqual(*profiles)
        self.assertFalse(self.data.exists())
        self.actual_spawn.assert_not_called()

    def test_visible_plan_and_launch_block_without_explicit_true_on_every_request(self):
        spawn = Mock()
        for authorization in (False, None, 1, "true", {}):
            result = access.launch_visible_browser("edge", "https://example.test/",
                                                    foreground_authorized=authorization, spawn=spawn)
            self.assertEqual(result["status"], "blocked_focus_window")
            self.assertIsNone(result["launch"])
            self.assertFalse(result["process_started"])
        spawn.assert_not_called()
        self.actual_spawn.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_mock_authorized_launch_uses_allowlisted_executable_safe_argv_and_cwd(self):
        spawn = Mock(return_value=SimpleNamespace(pid=12345))
        result = access.launch_visible_browser("yandex", "https://example.test/page?x=1&y=2",
                                                foreground_authorized=True, spawn=spawn)
        spawn.assert_called_once_with([str(self.yandex), "--user-data-dir=" + result["profile_path"],
                                       "https://example.test/page?x=1&y=2"],
                                      cwd=str(self.yandex.parent), shell=False)
        self.assertTrue(result["process_started"])
        self.assertFalse(result["controllable"])
        self.assertFalse(result["existing_session_access"])
        self.assertEqual(result["runtime_status"], "launch_requested_not_verified")
        # Authorization is never stored or reused by the next call.
        blocked = access.launch_visible_browser("yandex", "https://example.test/", spawn=spawn)
        self.assertEqual(blocked["status"], "blocked_focus_window")
        self.assertEqual(spawn.call_count, 1)
        self.actual_spawn.assert_not_called()

    def test_default_spawn_is_guarded_and_only_mocked_when_authorized(self):
        access.launch_visible_browser("edge", "https://example.test/")
        self.actual_spawn.assert_not_called()
        self.actual_spawn.side_effect = None
        self.actual_spawn.return_value = SimpleNamespace(pid=456)
        result = access.launch_visible_browser("edge", "https://example.test/", foreground_authorized=True)
        self.assertTrue(result["process_started"])
        self.actual_spawn.assert_called_once()

    def test_mock_process_failure_does_not_claim_start_or_control(self):
        result = access.launch_visible_browser("edge", "https://example.test/", foreground_authorized=True,
                                                spawn=Mock(side_effect=OSError("mock failure")))
        self.assertEqual(result["status"], "launch_failed")
        self.assertFalse(result["process_started"])
        self.assertFalse(result["controllable"])

    def test_yandex_headless_and_notion_are_explicitly_unsupported(self):
        plan = access.build_browser_access_plan("yandex", "https://example.test/")
        self.assertEqual(plan["status"], "unsupported_headless_browser")
        self.assertIsNone(plan["mcp_config"])
        self.assertIn("unverified/experimental", plan["support_note"])
        notion = access.build_browser_access_plan("notion", "https://www.notion.so/")
        self.assertEqual(notion["status"], "unsupported_application")
        self.assertFalse(notion["controllable"])

    def test_missing_browser_never_starts_or_installs_anything(self):
        self.probe.side_effect = lambda path: False
        spawn = Mock()
        result = access.launch_visible_browser("edge", "https://example.test/",
                                                foreground_authorized=True, spawn=spawn)
        self.assertEqual(result["status"], "blocked_browser_not_found_in_known_paths")
        spawn.assert_not_called()

    def test_arbitrary_executable_and_flags_are_rejected_before_spawn(self):
        spawn = Mock()
        for browser_id in ("C:/Windows/System32/cmd.exe", "edge --remote-debugging-port=9222", "dot", None):
            with self.subTest(browser_id=browser_id), self.assertRaisesRegex(ValueError, "unknown_browser"):
                access.launch_visible_browser(browser_id, "https://example.test/",
                                                foreground_authorized=True, spawn=spawn)
        with self.assertRaisesRegex(ValueError, "unknown_browser_mode"):
            access.build_browser_access_plan("edge", "https://example.test/", mode="shell")
        spawn.assert_not_called()

    def test_invalid_url_never_reaches_authorized_spawn(self):
        spawn = Mock()
        with self.assertRaisesRegex(ValueError, "invalid_https_url"):
            access.launch_visible_browser("edge", "javascript:alert(1)",
                                            foreground_authorized=True, spawn=spawn)
        spawn.assert_not_called()
        self.actual_spawn.assert_not_called()

    def test_symlink_or_windows_junction_profile_ancestry_is_rejected(self):
        fake_metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake_metadata):
            plan = access.build_browser_access_plan("edge", "https://example.test/")
        self.assertEqual(plan["status"], "blocked_unsafe_profile_path")
        self.assertIsNone(plan["mcp_config"])

    def test_profile_cannot_be_a_file_or_relative_data_root(self):
        self.data.mkdir()
        (self.data / "browser-access").write_text("mock file", encoding="utf-8")
        plan = access.build_browser_access_plan("edge", "https://example.test/")
        self.assertEqual(plan["status"], "blocked_unsafe_profile_path")
        with patch.object(access, "DATA_ROOT", Path("relative")):
            plan = access.build_browser_access_plan("edge", "https://example.test/")
        self.assertEqual(plan["status"], "blocked_unsafe_profile_path")

    def test_reimport_is_inert(self):
        importlib.reload(access)
        self.actual_spawn.assert_not_called()
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
