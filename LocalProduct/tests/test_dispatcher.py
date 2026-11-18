"""Dispatcher checks use synthetic data and mocked Windows integrations."""
import importlib
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]

class DispatcherTests(unittest.TestCase):
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

if __name__ == '__main__':
    unittest.main()
