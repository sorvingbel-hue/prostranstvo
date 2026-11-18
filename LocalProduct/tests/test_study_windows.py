import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Backend regression tests. No real desktop windows receive messages."""
import dataclasses
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from study_windows import WindowInfo, WindowsDesktop, candidate_allowed


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(100, 70, 123456789, r'C:\Programs\game.exe', 'GameWindow', 'Game')

    def allowed(self, **changes):
        window = dataclasses.replace(self.window, **changes)
        return candidate_allowed(window, {10}, r'C:\Windows')

    def test_game_and_browser_are_candidates(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(executable=r'C:\Users\test\browser.exe', class_name='Chrome_WidgetWin_1'))

    def test_ancestor_and_missing_identity_are_protected(self):
        self.assertFalse(self.allowed(pid=10))
        self.assertFalse(self.allowed(pid=0))
        self.assertFalse(self.allowed(process_started=0))
        self.assertFalse(self.allowed(executable=''))

    def test_agent_hosts_are_protected_without_excluding_all_python(self):
        for name in ('Codex.exe', 'codex-computer-use.exe', 'ChatGPT.exe', 'WindowsCompanion.exe', 'ProstranstvoCore.exe', 'KittenMessenger.exe', 'NativeDesktopPet.exe'):
            with self.subTest(name=name):
                self.assertFalse(self.allowed(executable='C:\\Programs\\' + name))
        self.assertTrue(self.allowed(executable=r'C:\Python\pythonw.exe'))

    def test_windows_directory_and_components_are_protected(self):
        self.assertFalse(self.allowed(executable=r'C:\Windows\System32\app.exe'))
        self.assertFalse(self.allowed(executable=r'C:\WINDOWS\SystemApps\app.exe'))
        self.assertFalse(self.allowed(executable=r'C:\Other\RuntimeBroker.exe'))
        self.assertTrue(self.allowed(executable=r'C:\WindowsTools\app.exe'))

    def test_only_verified_explorer_folder_windows_are_candidates(self):
        self.assertTrue(self.allowed(executable=r'C:\Windows\explorer.exe', class_name='CabinetWClass'))
        self.assertTrue(self.allowed(executable=r'C:\Windows\explorer.exe', class_name='ExploreWClass'))
        self.assertFalse(self.allowed(executable=r'C:\Other\explorer.exe', class_name='CabinetWClass'))
        self.assertFalse(self.allowed(executable=r'C:\Windows\explorer.exe', class_name='Progman'))

    def test_explorer_ancestor_still_allows_folder_windows(self):
        self.assertTrue(self.allowed(pid=10, executable=r'C:\Windows\explorer.exe', class_name='CabinetWClass'))
        self.assertFalse(self.allowed(pid=10, executable=r'C:\Windows\explorer.exe', class_name='Shell_TrayWnd'))
        self.assertFalse(self.allowed(pid=os.getpid(), executable=r'C:\Windows\explorer.exe', class_name='CabinetWClass'))

    def test_dialogs_and_shell_windows_are_protected(self):
        for name in ('Progman', 'WorkerW', 'Shell_TrayWnd', '#32770', 'ApplicationFrameWindow'):
            with self.subTest(name=name):
                self.assertFalse(self.allowed(class_name=name))

    def test_only_exact_tk_control_titles_are_excluded(self):
        for title in ('Пространство · Уроки', 'Пространство · Время уроков',
                      'Пространство · Помощник компьютера', 'Пространство · Local Beta', 'Мурчик · Написать папе'):
            with self.subTest(title=title):
                self.assertFalse(self.allowed(class_name='TkTopLevel', title=title))
                self.assertTrue(self.allowed(class_name='Chrome_WidgetWin_1', title=title))
        self.assertTrue(self.allowed(class_name='TkTopLevel', title='Another Python application'))


class CloseTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(100, 70, 123456789, r'C:\Programs\game.exe', 'GameWindow', 'Game')
        self.desktop = WindowsDesktop.__new__(WindowsDesktop)
        self.desktop.user32 = Mock()
        self.desktop.user32.IsWindow.return_value = True
        self.desktop.user32.PostMessageW.return_value = True
        self.desktop._window = Mock(return_value=self.window)
        self.desktop.is_interactive_desktop = Mock(return_value=True)

    def test_close_only_posts_normal_close_message(self):
        self.assertEqual(self.desktop.close_window(self.window), 'requested')
        self.desktop.user32.PostMessageW.assert_called_once_with(100, 0x0010, 0, 0)

    def test_gone_window_does_not_receive_a_message(self):
        self.desktop.user32.IsWindow.return_value = False
        self.assertEqual(self.desktop.close_window(self.window), 'gone')
        self.desktop.user32.PostMessageW.assert_not_called()

    def test_lost_access_or_new_save_dialog_does_not_receive_a_message(self):
        self.desktop._window.return_value = None
        self.assertEqual(self.desktop.close_window(self.window), 'unavailable')
        self.desktop.user32.PostMessageW.assert_not_called()

    def test_reused_pid_handle_or_changed_program_does_not_receive_a_message(self):
        for change in ({'pid': 71}, {'process_started': 99}, {'class_name': 'Other'}, {'executable': r'C:\Programs\other.exe'}):
            with self.subTest(change=change):
                self.desktop._window.return_value = dataclasses.replace(self.window, **change)
                self.assertEqual(self.desktop.close_window(self.window), 'changed')
                self.desktop.user32.PostMessageW.assert_not_called()

    def test_changed_window_title_still_closes_same_window(self):
        self.desktop._window.return_value = dataclasses.replace(self.window, title='New page')
        self.assertEqual(self.desktop.close_window(self.window), 'requested')

    def test_message_rejection_returns_unavailable(self):
        self.desktop.user32.PostMessageW.return_value = False
        self.assertEqual(self.desktop.close_window(self.window), 'unavailable')

    def test_protected_desktop_prevents_inventory_and_close(self):
        self.desktop.is_interactive_desktop.return_value = False
        self.assertEqual(self.desktop.list_windows(), [])
        self.assertEqual(self.desktop.close_window(self.window), 'unavailable')
        self.desktop.user32.EnumWindows.assert_not_called()
        self.desktop.user32.PostMessageW.assert_not_called()


class RobloxTerminationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': r'C:\Users\test\AppData\Local'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        executable = r'C:\Users\test\AppData\Local\Roblox\Versions\version-2c6fd5e09aaf4cfa\RobloxPlayerBeta.exe'
        self.window = WindowInfo(100, 70, 123456789, executable, 'WINDOWSCLIENT', 'Roblox')
        self.desktop = WindowsDesktop.__new__(WindowsDesktop)
        self.desktop.user32 = Mock()
        self.desktop.user32.IsWindow.return_value = True
        self.desktop.kernel32 = Mock()
        self.desktop.kernel32.OpenProcess.return_value = 901
        self.desktop.kernel32.TerminateProcess.return_value = True
        self.desktop._window = Mock(return_value=self.window)
        self.desktop._session = Mock(return_value=1)
        self.desktop.session_id = 1
        self.desktop.is_interactive_desktop = Mock(return_value=True)
        self.desktop._process_details = Mock(return_value=(executable, self.window.process_started))

    def test_verified_game_uses_minimal_access_and_same_open_handle(self):
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'terminated')
        self.desktop.kernel32.OpenProcess.assert_called_once_with(0x1001, False, 70)
        self.desktop._process_details.assert_called_once_with(901)
        self.desktop.kernel32.TerminateProcess.assert_called_once_with(901, 0)
        self.desktop.kernel32.CloseHandle.assert_called_once_with(901)

    def test_only_expected_versioned_game_installation_can_be_terminated(self):
        for executable in (
            r'C:\Programs\RobloxPlayerBeta.exe',
            r'C:\Users\other\AppData\Local\Roblox\Versions\version-2c6fd5e09aaf4cfa\RobloxPlayerBeta.exe',
            r'C:\Users\test\AppData\Local\Roblox\Versions\version-nothex\RobloxPlayerBeta.exe',
            r'C:\Users\test\AppData\Local\Roblox\Versions\version-2c6fd5e09aaf4cfa\other.exe',
            r'C:\Users\test\AppData\Local\Roblox\Versions\version-2c6fd5e09aaf4cfa\extra\RobloxPlayerBeta.exe',
        ):
            with self.subTest(executable=executable):
                window = dataclasses.replace(self.window, executable=executable)
                self.assertEqual(self.desktop.terminate_roblox(window), 'unavailable')
        self.desktop.kernel32.OpenProcess.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()

    def test_other_session_and_protected_desktop_refuse_termination(self):
        self.desktop._session.return_value = 2
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')
        self.desktop._session.return_value = 1
        self.desktop.is_interactive_desktop.return_value = False
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')
        self.desktop.kernel32.OpenProcess.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()

    def test_gone_or_disabled_or_owned_window_does_not_open_process(self):
        self.desktop.user32.IsWindow.return_value = False
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'gone')
        self.desktop.user32.IsWindow.return_value = True
        self.desktop._window.return_value = None
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')
        self.desktop.kernel32.OpenProcess.assert_not_called()

    def test_changed_window_identity_prevents_termination(self):
        for change in ({'pid': 71}, {'process_started': 99}, {'class_name': 'Other'},
                       {'executable': r'C:\Other\RobloxPlayerBeta.exe'}):
            with self.subTest(change=change):
                self.desktop._window.return_value = dataclasses.replace(self.window, **change)
                self.assertEqual(self.desktop.terminate_roblox(self.window), 'changed')
        self.desktop.kernel32.OpenProcess.assert_not_called()
        self.desktop.kernel32.TerminateProcess.assert_not_called()

    def test_process_reused_after_window_check_is_rejected_and_handle_closed(self):
        self.desktop._process_details.return_value = (self.window.executable, 9999)
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'changed')
        self.desktop.kernel32.TerminateProcess.assert_not_called()
        self.desktop.kernel32.CloseHandle.assert_called_once_with(901)

    def test_image_changed_after_window_check_is_rejected(self):
        self.desktop._process_details.return_value = (r'C:\Other\app.exe', self.window.process_started)
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'changed')
        self.desktop.kernel32.TerminateProcess.assert_not_called()
        self.desktop.kernel32.CloseHandle.assert_called_once_with(901)

    def test_access_query_or_termination_failure_returns_unavailable(self):
        self.desktop.kernel32.OpenProcess.return_value = 0
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')
        self.desktop.kernel32.CloseHandle.assert_not_called()
        self.desktop.kernel32.OpenProcess.return_value = 901
        self.desktop._process_details.return_value = None
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')
        self.desktop.kernel32.TerminateProcess.assert_not_called()
        self.desktop._process_details.return_value = (self.window.executable, self.window.process_started)
        self.desktop.kernel32.TerminateProcess.return_value = False
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'unavailable')

    def test_real_details_helper_queries_creation_time_on_termination_handle(self):
        del self.desktop._process_details
        def image(handle, flags, buffer, length):
            self.assertEqual(handle, 901)
            buffer.value = self.window.executable
            return True

        def times(handle, started, ended, kernel, user):
            self.assertEqual(handle, 901)
            started._obj.dwLowDateTime = self.window.process_started
            started._obj.dwHighDateTime = 0
            return True

        self.desktop.kernel32.QueryFullProcessImageNameW.side_effect = image
        self.desktop.kernel32.GetProcessTimes.side_effect = times
        self.assertEqual(self.desktop.terminate_roblox(self.window), 'terminated')
        self.desktop.kernel32.TerminateProcess.assert_called_once_with(901, 0)


if __name__ == '__main__':
    unittest.main()
