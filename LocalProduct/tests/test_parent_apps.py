"""Parent-app contracts with temporary settings and fake Win32 only."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parent_apps import APP_IDS, ParentApps, ParentAppsDesktop


class ParentAppsMockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parent-apps-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.root = self.fixture / "study"
        self.paths = {app_id: self.fixture / "installed" / app_id / "app.exe" for app_id in APP_IDS}
        for path in self.paths.values():
            path.parent.mkdir(parents=True)
            path.write_text("mock executable, never run", encoding="utf-8")
        browsers = {app_id: (self.paths[app_id],) for app_id in ("edge", "chrome", "yandex", "notion")}
        desktops = {app_id: (app_id, [self.paths[app_id]]) for app_id in ("vscode", "notion")}
        for patcher in (mock.patch("parent_apps.known_application_paths", return_value=browsers),
                        mock.patch("parent_apps.desktop_application_paths", return_value=desktops)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.desktop = mock.Mock()
        self.policy = ParentApps(self.root, desktop=self.desktop)
        self.desktop.is_interactive_desktop.return_value = True
        self.desktop.session_id = 1
        self.desktop._session.return_value = 1
        self.desktop._process.return_value = (str(self.paths["edge"]), 123456)
        self.desktop._enum_callback = lambda callback: callback
        user = self.desktop.user32
        user.IsWindow.return_value = True
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = False
        user.GetWindow.return_value = 0
        user.GetWindowLongW.return_value = 0
        user.ShowWindowAsync.return_value = True

        def class_name(handle, buffer, length):
            buffer.value = "Chrome_WidgetWin_1"
            return len(buffer.value)

        def rectangle(handle, rect):
            rect._obj.left, rect._obj.top = 0, 0
            rect._obj.right, rect._obj.bottom = 1000, 800
            return True

        def process(handle, pid):
            pid._obj.value = 70
            return 1

        user.GetClassNameW.side_effect = class_name
        user.GetWindowRect.side_effect = rectangle
        user.GetWindowThreadProcessId.side_effect = process
        user.EnumWindows.side_effect = lambda callback, parameter: bool(callback(100, parameter))

    def test_missing_policy_is_empty_inert_and_creates_nothing(self):
        self.assertEqual(self.policy.read(), {"version": 1, "app_ids": []})
        self.assertEqual(self.policy.app_ids(), [])
        self.assertEqual(self.policy.approved_executables(), ())
        self.assertFalse(self.policy.protects(str(self.paths["edge"])))
        self.assertEqual(self.policy.reconcile(True), {"enabled": False, "minimized": 0, "errors": 0})
        self.assertFalse(self.root.exists())
        self.desktop.is_interactive_desktop.assert_not_called()

    def test_strict_roundtrip_and_disable_is_explicit(self):
        self.policy.configure(["edge", "vscode"])
        self.assertEqual(ParentApps(self.root).read(), {"version": 1, "app_ids": ["edge", "vscode"]})
        self.policy.configure([])
        self.assertEqual(self.policy.app_ids(), [])
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.desktop.user32.EnumWindows.assert_not_called()

    def test_invalid_arguments_cannot_create_policy_or_grant_arbitrary_paths(self):
        for app_ids in (None, True, "edge", ("edge",), ["edge", "edge"], ["cmd"],
                        [str(self.paths["edge"])], ["Edge"], [1], [[]]):
            with self.subTest(app_ids=app_ids), self.assertRaises(ValueError):
                self.policy.configure(app_ids)
        self.assertFalse(self.root.exists())
        for active in (None, 1, "true"):
            with self.assertRaises(ValueError):
                self.policy.reconcile(active)

    def test_corrupt_schema_duplicate_keys_and_oversized_policy_raise(self):
        self.root.mkdir()
        for value in ({"version": True, "app_ids": []}, {"version": 2, "app_ids": []},
                      {"version": 1, "app_ids": [], "extra": 1}, {"version": 1}, [],
                      {"version": 1, "app_ids": ["unknown"]}, {"version": 1, "app_ids": ["edge", "edge"]}):
            self.policy.path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.policy.read()
        for raw in ('{"version":1,"app_ids":[],"app_ids":["edge"]}',
                    '{"version":NaN,"app_ids":[]}', "{broken", " " * 4097):
            self.policy.path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.policy.reconcile(True)
        self.desktop.user32.EnumWindows.assert_not_called()

    def test_atomic_failure_preserves_previous_policy_and_cleans_temporary(self):
        self.policy.configure(["edge"])
        with mock.patch("parent_apps.os.replace", side_effect=OSError("mock interruption")):
            with self.assertRaises(OSError):
                self.policy.configure(["chrome"])
        self.assertEqual(self.policy.app_ids(), ["edge"])
        self.assertEqual(list(self.root.glob(".parent-apps-*.tmp")), [])
        self.assertTrue((self.root / "parent-apps.lock").exists())

    def test_known_exact_paths_only_and_missing_or_linked_candidates_not_approved(self):
        self.policy.configure(["edge", "vscode", "notion"])
        self.assertTrue(self.policy.protects(str(self.paths["edge"]).upper()))
        self.assertTrue(self.policy.protects(str(self.paths["vscode"])))
        self.assertTrue(self.policy.protects(str(self.paths["notion"])))
        self.assertFalse(self.policy.protects(str(self.paths["chrome"])))
        self.assertFalse(self.policy.protects(str(self.fixture / "other" / "app.exe")))
        self.assertFalse(self.policy.protects("app.exe"))
        self.assertFalse(self.policy.protects(None))
        self.paths["edge"].unlink()
        self.assertFalse(self.policy.protects(str(self.paths["edge"])))
        original = __import__("parent_apps").no_links
        def reject_vscode(path, *args):
            if Path(path) == self.paths["vscode"]:
                raise ValueError("mock linked executable")
            return original(path, *args)
        with mock.patch("parent_apps.no_links", side_effect=reject_vscode):
            self.assertFalse(self.policy.protects(str(self.paths["vscode"])))

    def test_policy_symlink_and_hardlink_checks_fail_closed(self):
        self.policy.configure(["edge"])
        original = __import__("parent_apps").no_links
        def reject_settings(path, *args):
            if Path(path) == self.policy.path:
                raise ValueError("mock symlink/hardlink policy")
            return original(path, *args)
        with mock.patch("parent_apps.no_links", side_effect=reject_settings):
            with self.assertRaises(ValueError):
                self.policy.read()
            with self.assertRaises(ValueError):
                self.policy.configure(["chrome"])
        self.assertEqual(self.policy.app_ids(), ["edge"])

    def test_inactive_study_empty_paths_and_locked_desktop_do_not_touch_windows(self):
        self.policy.configure(["edge"])
        self.assertEqual(self.policy.reconcile(False)["minimized"], 0)
        self.desktop.is_interactive_desktop.assert_not_called()
        self.paths["edge"].unlink()
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.desktop.is_interactive_desktop.assert_not_called()
        self.paths["edge"].write_text("mock", encoding="utf-8")
        self.desktop.is_interactive_desktop.return_value = False
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.desktop.user32.EnumWindows.assert_not_called()

    def test_double_verified_main_window_receives_only_normal_minimize(self):
        self.policy.configure(["edge"])
        self.assertEqual(self.policy.reconcile(True), {"enabled": True, "minimized": 1, "errors": 0})
        self.assertEqual(self.desktop._process.call_count, 2)
        self.desktop.user32.ShowWindowAsync.assert_called_once_with(100, 6)
        self.desktop.user32.PostMessageW.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()
        self.assertEqual(self.desktop.user32.ShowWindowAsync.argtypes, [wintypes.HWND, ctypes.c_int])
        self.assertIs(self.desktop.user32.ShowWindowAsync.restype, wintypes.BOOL)
        self.assertEqual(self.desktop.user32.IsIconic.argtypes, [wintypes.HWND])
        self.assertEqual(self.desktop.user32.GetWindowRect.argtypes,
                         [wintypes.HWND, ctypes.POINTER(wintypes.RECT)])

    def test_user_restore_is_minimized_again_without_suspending_or_killing(self):
        self.policy.configure(["edge"])
        for iconic, expected in ((True, 0), (False, 1), (True, 0), (False, 1)):
            self.desktop.user32.IsIconic.return_value = iconic
            self.assertEqual(self.policy.reconcile(True)["minimized"], expected)
        self.assertEqual(self.desktop.user32.ShowWindowAsync.call_args_list,
                         [mock.call(100, 6), mock.call(100, 6)])
        self.desktop.kernel32.TerminateProcess.assert_not_called()
        self.desktop.kernel32.SuspendThread.assert_not_called()

    def test_deselected_other_session_or_reused_identity_is_skipped(self):
        self.policy.configure(["chrome"])
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.policy.configure(["edge"])
        self.desktop._session.return_value = 2
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.desktop._session.return_value = 1
        self.desktop._process.side_effect = [(str(self.paths["edge"]), 123), (str(self.paths["edge"]), 456)]
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        self.desktop.user32.ShowWindowAsync.assert_not_called()

    def test_invisible_owned_child_zero_area_or_non_chromium_class_is_skipped(self):
        self.policy.configure(["edge"])
        user = self.desktop.user32
        user.IsWindowVisible.return_value = False
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        user.IsWindowVisible.return_value = True
        user.GetWindow.return_value = 80
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        user.GetWindow.return_value = 0
        user.GetWindowLongW.return_value = 0x40000000
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        user.GetWindowLongW.return_value = 0
        user.GetClassNameW.side_effect = lambda handle, buffer, length: setattr(buffer, "value", "WinUIDesktopWin32WindowClass") or 1
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        user.GetClassNameW.side_effect = lambda handle, buffer, length: setattr(buffer, "value", "Chrome_WidgetWin_1") or 1
        user.GetWindowRect.side_effect = None
        user.GetWindowRect.return_value = True
        self.assertEqual(self.policy.reconcile(True)["minimized"], 0)
        user.ShowWindowAsync.assert_not_called()

    def test_minimize_failure_reports_error_and_never_closes_process(self):
        self.policy.configure(["edge"])
        self.desktop.user32.ShowWindowAsync.return_value = False
        self.assertEqual(self.policy.reconcile(True), {"enabled": True, "minimized": 0, "errors": 1})
        self.desktop.user32.PostMessageW.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()

    def test_desktop_proxy_filters_and_guards_close_only_while_active(self):
        self.policy.configure(["edge"])
        active = [True]
        proxy = ParentAppsDesktop(self.desktop, self.policy, active=lambda: active[0])
        parent = SimpleNamespace(executable=str(self.paths["edge"]))
        other = SimpleNamespace(executable=str(self.paths["chrome"]))
        self.desktop.list_windows.return_value = [parent, other]
        proxy.refresh_policy()
        self.assertEqual(proxy.selected_count, 1)
        self.assertEqual(proxy.list_windows(), [other])
        self.assertEqual(proxy.close_window(parent), "parent_app_preserved")
        self.desktop.close_window.assert_not_called()
        self.assertIs(proxy.user32, self.desktop.user32)
        active[0] = False
        self.assertEqual(proxy.list_windows(), [parent, other])
        proxy.close_window(parent)
        self.desktop.close_window.assert_called_once_with(parent)

    def test_proxy_reloads_changed_policy_and_retains_last_valid_snapshot_on_corruption(self):
        self.policy.configure(["edge"])
        proxy = ParentAppsDesktop(self.desktop, self.policy, active=lambda: True)
        proxy.refresh_policy()
        self.policy.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            proxy.refresh_policy()
        self.assertTrue(proxy.protects(str(self.paths["edge"])))
        self.policy.configure(["chrome"])
        proxy.refresh_policy()
        self.assertFalse(proxy.protects(str(self.paths["edge"])))
        self.assertTrue(proxy.protects(str(self.paths["chrome"])))

    def test_proxy_delegates_core_desktop_errors_without_swallowing_them(self):
        proxy = ParentAppsDesktop(self.desktop, self.policy, active=lambda: True)
        self.desktop.list_windows.side_effect = OSError("mock desktop unavailable")
        with self.assertRaises(OSError):
            proxy.list_windows()
        other = SimpleNamespace(executable=r"C:\Other\game.exe")
        self.desktop.close_window.side_effect = OSError("mock close failure")
        with self.assertRaises(OSError):
            proxy.close_window(other)


if __name__ == "__main__":
    unittest.main()
