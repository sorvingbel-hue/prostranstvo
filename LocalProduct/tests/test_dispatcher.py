"""Dispatcher checks use synthetic data and mocked Windows integrations."""
import importlib
import importlib.util
import ast
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]

class DispatcherTests(unittest.TestCase):
    @staticmethod
    def verifier():
        spec = importlib.util.spec_from_file_location('release_verifier_contract_fixture', PRODUCT / 'tests' / 'verify_release.py')
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {'SystemRoot': os.environ.get('SystemRoot', 'C:/Windows')}):
            spec.loader.exec_module(module)
        return module

    def test_paths_import_has_no_writes_and_honors_current_localappdata(self):
        with tempfile.TemporaryDirectory(prefix='prostranstvo-dispatcher-') as temp:
            root = Path(temp) / 'fresh-profile'
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(root)}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]), mock.patch.dict(sys.modules):
                sys.modules.pop('paths', None)
                paths = importlib.import_module('paths')
                self.assertEqual(paths.DATA_ROOT, root / 'ProstranstvoLocalBeta' / 'data')
                self.assertFalse(root.exists())
                self.assertEqual(paths.APP_ROOT, PRODUCT / 'src')

    def test_prepare_uninstall_only_uses_beta_helpers_and_preserves_absent_data(self):
        with tempfile.TemporaryDirectory(prefix='prostranstvo-dispatcher-') as temp:
            root = Path(temp)
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(root)}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]), mock.patch.dict(sys.modules):
                for name in ('paths', 'local_main', 'activity_startup', 'family_startup', 'activity_core', 'history_schedule', 'browser_history', 'family_messages', 'family_telegram'):
                    sys.modules.pop(name, None)
                main = importlib.import_module('local_main')
                with mock.patch('activity_startup.set_startup_enabled') as activity, mock.patch('family_startup.set_startup_enabled') as family, mock.patch('history_schedule.helper') as history, mock.patch('study_resume.StudyResume') as study:
                    study.return_value.read.return_value = None
                    result = main.prepare_uninstall()
                    activity.assert_called_once_with(False)
                    family.assert_called_once_with(False)
                    history.assert_called_once_with('remove')
                    study.return_value.disable_startup.assert_called_once_with()
                    self.assertTrue(result['prepared'])
                    self.assertFalse(main.DATA_ROOT.exists())

    def test_dispatch_rejects_arbitrary_mode_and_reply_identifier(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            for argv in (['core', 'shell'], ['core', 'reply', '--reply-to', 'bad/id']):
                with mock.patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as result:
                    main.main()
                self.assertEqual(result.exception.code, 2)

    def test_kitten_dispatch_passes_only_normal_or_mini_argument(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            helper = types.ModuleType('kitten_messenger')
            helper.main = mock.Mock(return_value=0)
            with mock.patch.dict(sys.modules, {'kitten_messenger': helper}):
                for arguments, expected in ((['kitten'], []), (['kitten', '--mini'], ['--mini'])):
                    with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]):
                        self.assertEqual(main.main(), 0)
                        helper.main.assert_called_once_with(expected)
                        helper.main.reset_mock()

    def test_mini_is_not_accepted_for_other_modes_and_kitten_has_no_setup_mode(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            for arguments in (['status', '--mini'], ['ui', '--mini'], ['kitten', '--setup'],
                              ['kitten', '--reply-to', 'valid-id'], ['kitten', '--background'],
                              ['kitten', '--mini', '--messages'], ['pet', '--mini'], ['pet', '--setup']):
                with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_pet_dispatch_has_no_arguments_or_child_setup_access(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            helper = types.ModuleType('desktop_pet')
            helper.main = mock.Mock(return_value=0)
            with mock.patch.dict(sys.modules, {'desktop_pet': helper}), mock.patch.object(sys, 'argv', ['core', 'pet']):
                self.assertEqual(main.main(), 0)
                helper.main.assert_called_once_with([])

    def test_speech_worker_dispatch_passes_no_command_or_path_arguments(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            worker = types.ModuleType('speech_worker')
            worker.main = mock.Mock(return_value=0)
            with mock.patch.dict(sys.modules, {'speech_worker': worker}), mock.patch.object(sys, 'argv', ['core', 'speech-transcribe']):
                self.assertEqual(main.main(), 0)
                worker.main.assert_called_once_with()
            for extra in ('--setup', '--activity', '--messages', '--background', '--ready-stdout', '--mini'):
                with self.subTest(extra=extra), mock.patch.object(sys, 'argv', ['core', 'speech-transcribe', extra]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_tricks_dispatch_only_selects_fixed_local_editor_modes(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            editor = types.ModuleType('pet_tricks_server')
            editor.main = mock.Mock(return_value=0)
            with mock.patch.dict(sys.modules, {'pet_tricks_server': editor}):
                for arguments, expected in ((['tricks'], ['--open']), (['tricks', '--open'], ['--open']),
                                            (['tricks', '--serve-only', '--ready-stdout'], ['--serve-only', '--ready-stdout'])):
                    with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]):
                        self.assertEqual(main.main(), 0)
                        editor.main.assert_called_once_with(expected)
                        editor.main.reset_mock()
            for arguments in (['tricks', '--setup'], ['tricks', '--mini'], ['tricks', '--open', '--serve-only'],
                              ['tricks', '--reply-to', 'valid-id'], ['status', '--open'], ['pet', '--serve-only']):
                with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_no_gui_is_rejected_before_dispatch_for_other_modes(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            for mode in ('ui', 'kitten', 'status', 'mcp', 'tricks', 'study-resume', 'power-worker'):
                with self.subTest(mode=mode), mock.patch.object(sys, 'argv', ['core', mode, '--no-gui']), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_storage_worker_dispatch_accepts_only_one_exact_owned_task_id(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            helper = types.ModuleType('storage_tasks')
            helper.main = mock.Mock(return_value=0)
            task_id = 'a' * 32
            with mock.patch.dict(sys.modules, {'storage_tasks': helper}):
                with mock.patch.object(sys, 'argv', ['core', 'storage-worker', '--task-id', task_id]):
                    self.assertEqual(main.main(), 0)
                helper.main.assert_called_once_with(['--worker', task_id])
                helper.main.reset_mock()
                invalid = (['storage-worker'], ['storage-worker', '--task-id', '../job'],
                           ['storage-worker', '--task-id', 'A' * 32],
                           ['storage-worker', '--task-id', task_id, '--setup'],
                           ['storage-worker', '--task-id', task_id, '--request-id', 'b' * 32],
                           ['status', '--task-id', task_id])
                for arguments in invalid:
                    with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                        main.main()
                    self.assertEqual(caught.exception.code, 2)
                helper.main.assert_not_called()

    def test_security_channel_dispatch_does_not_accept_other_mode_options(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            helper = types.ModuleType('security_mcp')
            helper.main = mock.Mock(return_value=0)
            with mock.patch.dict(sys.modules, {'security_mcp': helper}):
                with mock.patch.object(sys, 'argv', ['core', 'security-mcp']):
                    self.assertEqual(main.main(), 0)
                helper.main.assert_called_once_with()
                helper.main.reset_mock()
                for extra in ('--activity', '--messages', '--setup', '--background', '--ready-stdout',
                              '--mini', '--greet', '--open', '--serve-only', '--no-gui'):
                    with self.subTest(extra=extra), mock.patch.object(sys, 'argv', ['core', 'security-mcp', extra]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                        main.main()
                    self.assertEqual(caught.exception.code, 2)
                helper.main.assert_not_called()

    def test_runtime_only_needs_no_resources_profile_tk_or_app_imports(self):
        import ssl, sqlite3, zlib
        with tempfile.TemporaryDirectory(prefix='prostranstvo-runtime-dispatcher-') as temp:
            profile = Path(temp) / 'absent-profile'
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(profile)}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]), mock.patch.dict(sys.modules):
                for name in ('paths', 'local_main'):
                    sys.modules.pop(name, None)
                main = importlib.import_module('local_main')
                output = io.StringIO()
                with mock.patch.object(sys, 'argv', ['core', 'self-test', '--runtime-only']), mock.patch.object(sys, 'stdout', output), mock.patch.object(importlib, 'import_module', side_effect=AssertionError('No app/module probe')), mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('No process')), mock.patch.object(subprocess, 'run', side_effect=AssertionError('No command')):
                    self.assertEqual(main.main(), 0)
                receipt = json.loads(output.getvalue())
                self.assertEqual(receipt['check'], 'runtime_metadata_only')
                self.assertEqual(receipt['runtime'], {
                    'implementation': sys.implementation.name, 'python': sys.version.split()[0],
                    'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version,
                    'zlib': zlib.ZLIB_RUNTIME_VERSION})
                self.assertFalse(receipt['tk_tested'])
                self.assertFalse(receipt['data_created'])
                self.assertNotIn('imports_checked', receipt)
                self.assertFalse(profile.exists())

    def test_runtime_only_rejects_other_modes_and_action_options(self):
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            main = importlib.import_module('local_main')
            for mode in ('ui', 'kitten', 'status', 'mcp', 'family-mcp', 'security-mcp',
                         'study-resume', 'power-worker', 'storage-worker'):
                with self.subTest(mode=mode), mock.patch.object(sys, 'argv', ['core', mode, '--runtime-only']), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)
            for extra in ('--setup', '--background', '--activity', '--greet', '--serve-only'):
                with self.subTest(extra=extra), mock.patch.object(sys, 'argv', ['core', 'self-test', '--runtime-only', extra]), mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_self_test_no_gui_checks_inert_imports_without_tk_and_default_keeps_tk_check(self):
        import tkinter as tk
        import tkinter.ttk as ttk
        with tempfile.TemporaryDirectory(prefix='prostranstvo-no-gui-dispatcher-') as temp:
            profile = Path(temp) / 'fresh-profile'
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(profile)}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]), mock.patch.dict(sys.modules):
                for name in ('paths', 'local_main'):
                    sys.modules.pop(name, None)
                main = importlib.import_module('local_main')
                real_import = importlib.import_module
                output = io.StringIO()
                with mock.patch.object(sys, 'argv', ['core', 'self-test', '--no-gui']), mock.patch.object(sys, 'stdout', output), mock.patch.object(tk.Tk, '__init__', side_effect=AssertionError('No Tk construction')) as create, mock.patch('_tkinter.create', side_effect=AssertionError('No native Tk')) as native, mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('No child processes')), mock.patch.object(subprocess, 'run', side_effect=AssertionError('No commands')), mock.patch.object(importlib, 'import_module', wraps=real_import) as imported:
                    self.assertEqual(main.main(), 0)
                create.assert_not_called()
                native.assert_not_called()
                receipt = json.loads(output.getvalue())
                self.assertFalse(receipt['tk_tested'])
                self.assertIsNone(receipt['tk'])
                self.assertFalse(receipt['data_created'])
                # Report the executing runtime, not the interpreter that built a receipt.
                import ssl, sqlite3, zlib
                self.assertEqual(receipt['runtime'], {
                    'implementation': sys.implementation.name, 'python': sys.version.split()[0],
                    'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version,
                    'zlib': zlib.ZLIB_RUNTIME_VERSION})
                self.assertEqual(set(receipt['imports_checked']), set(main.SELF_TEST_MODULES))
                imported_names = {call.args[0] for call in imported.call_args_list}
                self.assertTrue(set(main.SELF_TEST_MODULES) <= imported_names)
                self.assertNotIn('mcp_server', imported_names)
                self.assertTrue({'application_launcher', 'browser_access', 'agent_control', 'headless_browser',
                                 'browser_session', 'power_control', 'maintenance_advisor',
                                 'maintenance_sequence', 'storage_audit'} <= imported_names)
                self.assertFalse(profile.exists())
                # Historical default still exercises Tk, using only a fake root here.
                fake_root = mock.Mock()
                fake_root.tk.call.return_value = 'mock-Tk'
                output = io.StringIO()
                with mock.patch.object(sys, 'argv', ['core', 'self-test']), mock.patch.object(sys, 'stdout', output), mock.patch.object(tk, 'Tk', return_value=fake_root) as create, mock.patch.object(ttk, 'Frame'):
                    self.assertEqual(main.main(), 0)
                create.assert_called_once_with()
                fake_root.withdraw.assert_called_once_with()
                fake_root.destroy.assert_called_once_with()
                receipt = json.loads(output.getvalue())
                self.assertTrue(receipt['tk_tested'])
                self.assertEqual(receipt['tk'], 'mock-Tk')
                self.assertFalse(profile.exists())

    def test_release_contract_matches_current_source_and_rejects_wrong_or_duplicate_tools(self):
        verifier = self.verifier()
        # Read the base declaration without importing its top-level stdio loop.
        tree = ast.parse((PRODUCT / 'src' / 'mcp_server.py').read_text(encoding='utf-8-sig'))
        declaration = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == 'TOOLS' for target in node.targets))
        core_names = {row['name'] for row in ast.literal_eval(declaration)}
        with mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
            apps = importlib.import_module('agent_apps')
            controls = importlib.import_module('agent_control')
            self.assertEqual(verifier.PORTABLE_TOOL_NAMES, core_names | apps.TOOL_NAMES | controls.TOOL_NAMES)
        self.assertEqual(len(verifier.PORTABLE_TOOL_NAMES), 23)
        self.assertNotIn('system_maintenance', verifier.PORTABLE_TOOL_NAMES)
        tools = [{'name': name, 'inputSchema': {'type': 'object', 'additionalProperties': False}}
                 for name in sorted(verifier.PORTABLE_TOOL_NAMES)]
        self.assertEqual(verifier.check_tool_contract(tools, verifier.PORTABLE_TOOL_NAMES)['tool_count'], 23)
        wrong = [{**tool} for tool in tools]
        wrong[0]['name'] = 'system_maintenance'
        for invalid in (tools[:-1], tools + [tools[0]], wrong):
            with self.subTest(invalid_count=len(invalid)), self.assertRaises(AssertionError):
                verifier.check_tool_contract(invalid, verifier.PORTABLE_TOOL_NAMES)

    def test_release_no_gui_accepts_absent_activity_but_never_enabled_settings(self):
        verifier = self.verifier()
        with tempfile.TemporaryDirectory(prefix='prostranstvo-release-config-fixture-') as temp:
            data = Path(temp)
            rules = data / 'cat-companion' / 'tricks' / 'rules.json'
            rules.parent.mkdir(parents=True)
            rules.write_text('{"enabled":false}', encoding='utf-8')
            self.assertEqual(verifier.disabled_features_check(data, gui_tested=False)['activity'], 'absent_inert')
            self.assertFalse((data / 'activity').exists())
            with self.assertRaises(AssertionError):
                verifier.disabled_features_check(data, gui_tested=True)
            activity = data / 'activity' / 'config.json'
            activity.parent.mkdir()
            activity.write_text('{"enabled":true}', encoding='utf-8')
            with self.assertRaises(AssertionError):
                verifier.disabled_features_check(data, gui_tested=False)
            activity.write_text('{"enabled":false}', encoding='utf-8')
            self.assertEqual(verifier.disabled_features_check(data, gui_tested=False)['activity'], 'disabled')

    def test_release_console_runner_never_requests_a_console_window(self):
        verifier = self.verifier()
        result = types.SimpleNamespace(returncode=0, stdout='ok', stderr='')
        with mock.patch.object(subprocess, 'run', return_value=result) as run:
            self.assertEqual(verifier.run(['fixture-core', 'status']), 'ok')
        self.assertEqual(run.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))

if __name__ == '__main__':
    unittest.main()
