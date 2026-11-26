"""Desktop panel/marker tests without Tk roots, screen/input, clients or real data."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import desktop_control_ui as ui


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'data' / 'desktop-control' / 'STOP'
        self.marker = ui.StopMarker(self.path)

    def test_construct_and_status_do_not_create_directory_or_claim_client(self):
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.marker.status(), {'stop_requested': False,
                         'marker': str(self.path), 'client_connection_verified': False,
                         'process_state': 'unknown'})
        self.assertFalse(self.path.parent.exists())

    def test_stop_is_complete_same_directory_atomic_replace_and_fsync(self):
        self.marker.request_stop()
        previous = self.path.read_bytes()
        real_replace = os.replace
        seen = []
        def replace(source, target):
            source, target = Path(source), Path(target)
            self.assertEqual(source.parent, self.path.parent)
            self.assertEqual(target, self.path)
            self.assertEqual(source.read_bytes(), b'STOP\n')
            self.assertEqual(target.read_bytes(), previous)
            seen.append(True)
            return real_replace(source, target)
        with patch.object(ui.os, 'replace', side_effect=replace), patch.object(ui.os, 'fsync', wraps=os.fsync) as sync:
            result = self.marker.request_stop()
        self.assertEqual(seen, [True])
        sync.assert_called_once()
        self.assertTrue(result['stop_requested'])
        self.assertFalse(result['client_connection_verified'])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['STOP'])

    def test_failed_replace_keeps_existing_stop_and_removes_only_temporary(self):
        self.marker.request_stop()
        self.path.write_bytes(b'prior-stop')
        with patch.object(ui.os, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.marker.request_stop()
        self.assertEqual(self.path.read_bytes(), b'prior-stop')
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['STOP'])

    def test_changed_stop_cannot_be_cleared_by_previous_authorization(self):
        self.marker.request_stop()
        previous = self.marker.identity()
        self.marker.request_stop()
        self.assertNotEqual(previous, self.marker.identity())
        with self.assertRaisesRegex(ValueError, 'изменился'):
            self.marker.clear_for_future(previous)
        self.assertTrue(self.path.is_file())

    def test_new_stop_during_authorization_preserves_it(self):
        self.assertIsNone(self.marker.identity())
        self.marker.request_stop()
        with self.assertRaises(ValueError):
            self.marker.clear_for_future(None)
        self.assertTrue(self.path.exists())

    def test_clear_only_removes_exact_regular_marker_and_never_claims_reenabled(self):
        self.marker.request_stop()
        result = self.marker.clear_for_future(self.marker.identity())
        self.assertFalse(self.path.exists())
        self.assertFalse(result['stop_requested'])
        self.assertEqual(result['process_state'], 'unknown')
        self.assertFalse(result['client_connection_verified'])

    def test_nonregular_marker_is_rejected_and_not_removed(self):
        self.path.mkdir(parents=True)
        for operation in (self.marker.status, self.marker.request_stop,
                          lambda: self.marker.clear_for_future(None)):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
        self.assertTrue(self.path.is_dir())

    def test_hardlinked_marker_is_rejected_without_touching_other_file(self):
        self.path.parent.mkdir(parents=True)
        original = self.root / 'original.txt'
        original.write_text('preserve', encoding='utf-8')
        os.link(original, self.path)
        with self.assertRaisesRegex(ValueError, 'hardlink'):
            self.marker.request_stop()
        with self.assertRaises(ValueError):
            self.marker.clear_for_future(None)
        self.assertEqual(original.read_text(encoding='utf-8'), 'preserve')

    def test_reparse_and_symlink_ancestor_are_rejected_with_mock_lstat(self):
        actual = Path.lstat
        for attrs, mode in ((0x400, stat.S_IFDIR), (0, stat.S_IFLNK)):
            def inspected(path):
                if path == self.root:
                    return types.SimpleNamespace(st_file_attributes=attrs, st_mode=mode, st_nlink=1)
                return actual(path)
            with self.subTest(attrs=attrs, mode=mode), patch.object(Path, 'lstat', inspected):
                with self.assertRaisesRegex(ValueError, 'junction'):
                    ui.StopMarker(self.path)

    def test_relative_parent_unc_ads_marker_paths_rejected(self):
        candidates = ['relative/STOP', self.root / '..' / 'STOP']
        if os.name == 'nt':
            candidates += [r'\\server\share\STOP', r'C:\test\STOP:stream']
        for value in candidates:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.StopMarker(value)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='desktop config ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.marker = self.root / 'данные' / 'desktop-control' / 'STOP'

    def config(self, **kwargs):
        return ui.build_configuration(app_root=self.root / 'Программа с пробелами',
                                      stop_file=self.marker, **kwargs)

    def test_legacy_uses_console_python_not_pythonw_and_exact_script_arguments(self):
        with patch.dict(sys.modules, {'paths': types.SimpleNamespace()}):
            value = self.config(frozen=False, executable=self.root / 'pythonw.exe',
                                python_base=self.root / 'runtime')
        self.assertEqual(Path(value['command']), self.root / 'runtime' / 'python.exe')
        self.assertEqual(value['args'], ['-B', str(self.root / 'Программа с пробелами' / 'desktop_control_mcp.py'),
                         '--session-seconds', '1800', '--stop-file', str(self.marker)])
        self.assertNotIn('--enable-input', value['args'])
        self.assertFalse(self.marker.parent.exists())

    def test_legacy_prefers_owned_secure_runtime_only_when_present(self):
        deployment = self.root / 'deployment'
        runtime = deployment / 'runtime' / 'python-3.13.18' / 'python.exe'
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b'fixture-not-executed')
        value = self.config(frozen=False, deployment_root=deployment,
                            python_base=self.root / 'fallback')
        self.assertEqual(Path(value['command']), runtime)

    def test_unsafe_owned_runtime_does_not_silently_fall_back(self):
        deployment = self.root / 'deployment'
        runtime = deployment / 'runtime' / 'python-3.13.18' / 'python.exe'
        runtime.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.config(frozen=False, deployment_root=deployment,
                        python_base=self.root / 'fallback')

    def test_frozen_configuration_uses_core_mode_and_session_options(self):
        value = self.config(frozen=True, minutes=60, enable_input=True,
                            executable=self.root / 'unrelated.exe')
        self.assertEqual(Path(value['command']), self.root / 'Программа с пробелами' / 'ProstranstvoCore.exe')
        self.assertEqual(value['args'], ['desktop-mcp', '--session-seconds', '3600',
                         '--stop-file', str(self.marker), '--enable-input'])
        self.assertNotIn('python', value['command'].lower())

    def test_toml_round_trip_preserves_spaces_backslashes_unicode_and_argument_boundaries(self):
        value = self.config(frozen=True, enable_input=True)
        value['command'] = 'C:\\Path with spaces\\Кот\\ProstranstvoCore.exe'
        text = ui.configuration_text(value)
        parsed = tomllib.loads(text)['mcp_servers'][ui.SERVER_NAME]
        self.assertEqual(parsed, {k: v for k, v in value.items() if k != 'name'})
        self.assertIn('клиент ещё не подключён', text)

    def test_only_presets_and_boolean_input_are_accepted(self):
        for minutes in (True, '30', 0, 31, 120):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.config(minutes=minutes)
        for enabled in ('yes', 1, None):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.config(enable_input=enabled)

    def test_default_marker_uses_beta_data_or_legacy_deployment_without_eager_fallback(self):
        beta = types.SimpleNamespace(DATA_ROOT=self.root / 'beta-data')
        with patch.dict(sys.modules, {'paths': beta}):
            self.assertEqual(ui.default_stop_file(), beta.DATA_ROOT / 'desktop-control' / 'STOP')
        legacy = types.SimpleNamespace(DEPLOY_ROOT=self.root / 'legacy')
        with patch.dict(sys.modules, {'paths': legacy}):
            self.assertEqual(ui.default_stop_file(), legacy.DEPLOY_ROOT / 'data' / 'desktop-control' / 'STOP')
        self.assertEqual(list(self.root.iterdir()), [])


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'control' / 'STOP'
        self.panel = ui.DesktopControlPanel.__new__(ui.DesktopControlPanel)
        self.panel.marker = ui.StopMarker(self.path)
        self.panel.authorize_parent = Mock(return_value=True)
        self.panel.closed = False
        self.panel._timer = None
        self.panel.duration = Value('30')
        self.panel.input_enabled = Value(False)
        self.panel.status = Value()
        self.panel.notice = Value()
        self.panel.configuration = Mock()
        self.panel.after = Mock(return_value='poll-token')
        self.panel.after_cancel = Mock()
        self.addCleanup(self.panel.close)

    def test_status_poll_is_readonly_never_claims_connected_or_active(self):
        self.panel._poll()
        self.assertIn('не подтверждены', self.panel.status.get())
        self.assertIn('неизвестно', self.panel.status.get())
        self.assertFalse(self.path.parent.exists())
        self.panel.authorize_parent.assert_not_called()
        self.panel.after.assert_called_once_with(1500, self.panel._poll)

    def test_stop_does_not_require_pin_and_records_real_marker_without_delivery_claim(self):
        self.assertTrue(self.panel.stop_access())
        self.assertEqual(self.path.read_bytes(), b'STOP\n')
        self.panel.authorize_parent.assert_not_called()
        self.assertIn('не подтверждена', self.panel.status.get())
        self.assertIn('ещё не получено', self.panel.notice.get())

    def test_denied_or_missing_parent_authorization_cannot_clear_stop(self):
        self.panel.marker.request_stop()
        for authorize in (None, Mock(return_value=False)):
            with self.subTest(authorize=authorize):
                self.panel.authorize_parent = authorize
                self.assertFalse(self.panel.allow_future())
                self.assertTrue(self.path.exists())
        self.assertIn('не получено', self.panel.notice.get())

    def test_authorized_clear_does_not_claim_current_session_resumed(self):
        self.panel.marker.request_stop()
        self.assertTrue(self.panel.allow_future())
        self.panel.authorize_parent.assert_called_once_with('Разрешить будущий сеанс управления ИИ')
        self.assertFalse(self.path.exists())
        self.assertIn('не возобновляется', self.panel.notice.get())
        self.assertIn('не включался', self.panel.notice.get())
        self.assertIn('неизвестно', self.panel.status.get())

    def test_stop_issued_during_pin_is_not_cleared(self):
        def authorize(title):
            self.panel.marker.request_stop()
            return True
        self.panel.authorize_parent.side_effect = authorize
        self.assertFalse(self.panel.allow_future())
        self.assertTrue(self.path.exists())
        self.assertIn('Не удалось', self.panel.notice.get())

    def test_configuration_display_never_writes_client_files_or_starts_server(self):
        prepared = {'command': 'fixture.exe', 'args': ['desktop-mcp'], 'cwd': 'fixture', 'enabled': True}
        with patch.object(ui, 'build_configuration', return_value=prepared) as build:
            self.assertTrue(self.panel.show_configuration())
        build.assert_called_once_with(30, False, stop_file=self.path)
        self.panel.configuration.configure.assert_any_call(state='normal')
        self.panel.configuration.configure.assert_called_with(state='disabled')
        self.panel.configuration.insert.assert_called_once_with('1.0', ui.configuration_text(prepared))
        self.assertIn('сервер не запускался', self.panel.notice.get())
        self.assertFalse(self.path.parent.exists())
        self.panel.authorize_parent.assert_not_called()

    def test_failed_stop_reports_unconfirmed_not_success(self):
        with patch.object(self.panel.marker, 'request_stop', side_effect=PermissionError('private fixture')):
            self.assertFalse(self.panel.stop_access())
        self.assertIn('Остановка не подтверждена', self.panel.notice.get())
        self.assertNotIn('private fixture', self.panel.notice.get())

    def test_read_error_does_not_claim_access_allowed(self):
        with patch.object(self.panel.marker, 'status', side_effect=ValueError('private fixture')):
            self.panel.refresh()
        self.assertIn('Не считайте доступ разрешённым', self.panel.status.get())
        self.assertNotIn('private fixture', self.panel.status.get())

    def test_destroy_cancels_poll_only_and_closed_handlers_do_nothing(self):
        self.panel._schedule_poll()
        self.panel._on_destroy(types.SimpleNamespace(widget=object()))
        self.assertFalse(self.panel.closed)
        self.panel._on_destroy(types.SimpleNamespace(widget=self.panel))
        self.panel.after_cancel.assert_called_once_with('poll-token')
        self.panel.after.reset_mock()
        self.panel._poll()
        self.panel.after.assert_not_called()
        self.assertFalse(self.panel.show_configuration())
        self.assertFalse(self.panel.stop_access())
        self.assertFalse(self.panel.allow_future())
        self.assertFalse(self.path.parent.exists())


if __name__ == '__main__':
    unittest.main()
