"""Fixed entry-point routing: fake module only, no desktop or client access."""
from pathlib import Path
import contextlib
import io
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import local_main


class DesktopEntryTests(unittest.TestCase):
    def invoke(self, arguments):
        handler = mock.Mock(return_value=17)
        fake = SimpleNamespace(main=handler)
        with mock.patch.dict(sys.modules, desktop_control_mcp=fake), \
                mock.patch.object(sys, 'argv', ['ProstranstvoCore.exe', *arguments]):
            value = local_main.main()
        return value, handler

    def test_default_desktop_entry_never_enables_input_implicitly(self):
        value, handler = self.invoke(['desktop-mcp'])
        self.assertEqual(value, 17)
        handler.assert_called_once_with([])

    def test_explicit_options_route_exact_values_without_shell(self):
        value, handler = self.invoke(['desktop-mcp', '--enable-input', '--session-seconds',
                                      '1800', '--stop-file', r'D:\test-owned\STOP'])
        self.assertEqual(value, 17)
        handler.assert_called_once_with(['--enable-input', '--session-seconds', '1800',
                                         '--stop-file', r'D:\test-owned\STOP'])

    def test_input_options_rejected_in_other_modes_before_execution(self):
        for mode in ('ui', 'mcp', 'family-mcp', 'security-mcp', 'self-test', 'study-resume'):
            for option in (['--enable-input'], ['--session-seconds', '0'], ['--stop-file', 'STOP']):
                with self.subTest(mode=mode, option=option), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        self.invoke([mode, *option])
                    self.assertEqual(error.exception.code, 2)

    def test_other_actions_rejected_in_desktop_mode(self):
        options = (['--activity'], ['--messages'], ['--setup'], ['--background'],
                   ['--ready-stdout'], ['--reply-to', 'one'], ['--mini'], ['--greet'],
                   ['--open'], ['--serve-only'], ['--request-id', 'one'],
                   ['--worker-id', 'one'], ['--task-id', 'one'], ['--no-gui'], ['--runtime-only'])
        for option in options:
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.invoke(['desktop-mcp', *option])
                self.assertEqual(error.exception.code, 2)

    def test_module_self_test_inventory_contains_no_eager_desktop_actions(self):
        self.assertTrue({'desktop_control', 'desktop_control_mcp', 'desktop_control_ui'} <=
                        set(local_main.SELF_TEST_MODULES))


if __name__ == '__main__':
    unittest.main()
