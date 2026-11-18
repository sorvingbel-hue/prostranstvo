"""Inert launch-contract tests. No actual desktop applications are started."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import tempfile
import types
import unittest
from unittest import mock

from application_launcher import ApplicationLauncher, resolve_app


class ApplicationLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = {'SystemRoot': str(self.root / 'Windows'),
                            'LOCALAPPDATA': str(self.root / 'Local'),
                            'ProgramFiles': str(self.root / 'Programs')}
        self.spawn = mock.Mock(return_value=types.SimpleNamespace(pid=24680))
        self.launcher = ApplicationLauncher(environment=self.environment, spawn=self.spawn)

    def fixture(self):
        path = self.root / 'Windows' / 'System32' / 'notepad.exe'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'inert test fixture, not an executable')
        return path

    def test_import_construction_and_catalog_do_not_create_directories_or_spawn(self):
        result = self.launcher.catalog()
        self.assertEqual(result['status'], 'inventory_only')
        self.assertFalse(any(item['installed'] for item in result['applications']))
        self.assertEqual(list(self.root.iterdir()), [])
        self.spawn.assert_not_called()

    def test_known_app_is_detected_without_starting(self):
        path = self.fixture()
        result = self.launcher.plan('Блокнот')
        self.assertEqual(result['argv'], [str(path)])
        self.assertFalse(result['started_anything'])
        self.assertFalse(result['automation_connected'])
        self.spawn.assert_not_called()

    def test_default_refusal_never_calls_spawn_even_for_an_installed_app(self):
        self.fixture()
        self.assertEqual(self.launcher.launch('notepad')['reason'], 'screen_use_not_authorized')
        self.spawn.assert_not_called()

    def test_explicit_screen_permission_uses_exact_list_no_shell_and_no_free_args(self):
        path = self.fixture()
        result = self.launcher.launch('Блокнот', foreground_authorized=True)
        self.spawn.assert_called_once_with([str(path)], cwd=path.parent, shell=False)
        self.assertEqual(result['pid'], 24680)
        self.assertFalse(result['window_open_verified'])
        self.assertFalse(result['automation_connected'])

    def test_not_installed_app_does_not_start_or_claim_global_absence(self):
        result = self.launcher.launch('notion', foreground_authorized=True)
        self.assertEqual(result['status'], 'not_found_in_known_locations')
        self.spawn.assert_not_called()

    def test_no_arbitrary_command_path_url_or_argument_passthrough(self):
        for app in ('cmd', 'powershell', 'notepad --plugin=x', 'calc.exe & whoami',
                    r'C:\Windows\System32\cmd.exe', 'https://facebook.com', '', None):
            with self.subTest(app=app), self.assertRaises(ValueError):
                self.launcher.launch(app, foreground_authorized=True)
        self.spawn.assert_not_called()

    def test_screen_permission_is_strict_boolean(self):
        for permission in ('true', 1, [], None):
            with self.subTest(permission=permission), self.assertRaises(ValueError):
                self.launcher.launch('notepad', foreground_authorized=permission)
        self.spawn.assert_not_called()

    def test_aliases_are_bounded_and_known(self):
        self.assertEqual(resolve_app(' VS Code '), 'vscode')
        self.assertEqual(resolve_app('КАЛЬКУЛЯТОР'), 'calculator')
        with self.assertRaises(ValueError):
            resolve_app('x' * 101)

    def test_windows_builtin_hardlinks_remain_valid_catalog_entries(self):
        path = self.fixture()
        other = self.root / 'other.exe'
        try:
            other.hardlink_to(path)
        except OSError:
            self.skipTest('Hard links are unavailable in this fixture')
        self.assertEqual(self.launcher.plan('notepad')['status'], 'planned_not_started')
        self.spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
