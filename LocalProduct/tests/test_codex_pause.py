import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Codex interface minimization with isolated files and fake Win32 calls."""
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_pause import CodexPause


class CodexPauseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='codex-pause-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'study'
        self.desktop = mock.Mock()
        self.pause = CodexPause(self.root, desktop=self.desktop)
        self.desktop.is_interactive_desktop.return_value = True
        self.desktop.session_id = 1
        self.desktop._session.return_value = 1
        self.path = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe'
        self.desktop._process.return_value = (self.path, 123456)
        self.desktop._enum_callback = lambda callback: callback
        user = self.desktop.user32
        user.IsWindow.return_value = True
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = False
        user.GetWindow.return_value = 0
        user.GetWindowLongW.return_value = 0
        user.ShowWindowAsync.return_value = True

        def class_name(handle, buffer, length):
            buffer.value = 'Chrome_WidgetWin_1'
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
        environment = mock.patch.dict(os.environ, {'ProgramFiles': r'C:\Program Files'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_is_off_and_creates_no_state_or_desktop_calls(self):
        self.assertFalse(self.pause.enabled())
        self.assertEqual(self.pause.reconcile(True), {'enabled': False, 'minimized': 0, 'errors': 0})
        self.assertFalse(self.root.exists())
        self.desktop.is_interactive_desktop.assert_not_called()

    def test_state_roundtrip_and_invalid_schema_or_argument(self):
        self.pause.set_enabled(True)
        self.assertTrue(CodexPause(self.root).enabled())
        self.pause.set_enabled(False)
        self.assertFalse(self.pause.enabled())
        for value in (1, None, 'yes'):
            with self.assertRaises(ValueError):
                self.pause.set_enabled(value)
        for value in ({'version': True, 'enabled': True}, {'version': 1, 'enabled': 1},
                      {'version': 1, 'enabled': True, 'extra': 0}, {'version': 2, 'enabled': True}, []):
            self.pause.path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.pause.enabled()

    def test_interrupted_write_retains_previous_preference(self):
        self.pause.set_enabled(False)
        with mock.patch('codex_pause.os.replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                self.pause.set_enabled(True)
        self.assertFalse(self.pause.enabled())

    def test_inactive_study_or_locked_desktop_never_minimizes(self):
        self.pause.set_enabled(True)
        self.assertEqual(self.pause.reconcile(False)['minimized'], 0)
        self.desktop.user32.EnumWindows.assert_not_called()
        self.desktop.is_interactive_desktop.return_value = False
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop.user32.ShowWindowAsync.assert_not_called()

    def test_verified_codex_root_window_only_gets_normal_minimize(self):
        self.pause.set_enabled(True)
        self.assertEqual(self.pause.reconcile(True), {'enabled': True, 'minimized': 1, 'errors': 0})
        self.desktop.user32.ShowWindowAsync.assert_called_once_with(100, 6)
        self.assertEqual(self.desktop._process.call_count, 2)
        self.desktop.user32.PostMessageW.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()

    def test_already_minimized_is_skipped_and_user_restore_is_minimized_again(self):
        self.pause.set_enabled(True)
        self.desktop.user32.IsIconic.return_value = True
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop.user32.IsIconic.return_value = False
        self.assertEqual(self.pause.reconcile(True)['minimized'], 1)
        self.desktop.user32.IsIconic.return_value = True
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop.user32.IsIconic.return_value = False
        self.assertEqual(self.pause.reconcile(True)['minimized'], 1)
        self.assertEqual(self.desktop.user32.ShowWindowAsync.call_count, 2)

    def test_untrusted_vendor_lookalike_or_cli_path_is_skipped(self):
        self.pause.set_enabled(True)
        for path in (
            r'C:\Programs\ChatGPT.exe',
            r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1111.2228.0_x64__otherpublisher\app\ChatGPT.exe',
            r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0\app\resources\codex.exe',
            r'C:\Windows\System32\ChatGPT.exe',
        ):
            with self.subTest(path=path):
                self.desktop._process.return_value = (path, 123456)
                self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop.user32.ShowWindowAsync.assert_not_called()

    def test_other_session_or_reused_process_identity_is_skipped(self):
        self.pause.set_enabled(True)
        self.desktop._session.return_value = 2
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop._session.return_value = 1
        self.desktop._process.side_effect = [(self.path, 123456), (self.path, 987654)]
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        self.desktop.user32.ShowWindowAsync.assert_not_called()

    def test_invisible_owned_zero_area_or_unknown_class_window_is_skipped(self):
        self.pause.set_enabled(True)
        user = self.desktop.user32
        user.IsWindowVisible.return_value = False
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        user.IsWindowVisible.return_value = True
        user.GetWindow.return_value = 80
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        user.GetWindow.return_value = 0
        user.GetWindowRect.side_effect = None
        user.GetWindowRect.return_value = True
        self.assertEqual(self.pause.reconcile(True)['minimized'], 0)
        user.ShowWindowAsync.assert_not_called()

    def test_failed_minimize_reports_failure_without_process_action(self):
        self.pause.set_enabled(True)
        self.desktop.user32.ShowWindowAsync.return_value = False
        self.assertEqual(self.pause.reconcile(True), {'enabled': True, 'minimized': 0, 'errors': 1})
        self.desktop.kernel32.TerminateProcess.assert_not_called()


if __name__ == '__main__':
    unittest.main()
