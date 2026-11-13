import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Startup path contracts in recipient fixtures, without real registry writes."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import study_resume


class StartupCommandTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / 'tests-work'
        fixtures.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='study-command-', dir=fixtures)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.app = self.base / 'Recipient Application'
        self.app.mkdir()
        self.python = self.base / 'Python With Spaces' / 'python.exe'
        self.python.parent.mkdir()
        self.python.write_bytes(b'synthetic executable')
        self.python.with_name('pythonw.exe').write_bytes(b'synthetic windowed executable')
        (self.app / 'study_runtime.py').write_text('# synthetic fixture', encoding='utf-8')

    def test_personal_command_uses_deployed_windowed_python_and_exact_script(self):
        deployment = self.base / 'Personal Deployment'
        deployed_app = deployment / 'app'
        deployed_app.mkdir(parents=True)
        (deployed_app / 'python-path.txt').write_text(str(self.python), encoding='utf-8')
        (deployed_app / 'study_runtime.py').write_text('# synthetic fixture', encoding='utf-8')
        with mock.patch.object(study_resume, 'paths', SimpleNamespace(DEPLOY_ROOT=deployment)):
            store = study_resume.StudyResume(self.base / 'data')
            command = store._startup_command()
        expected = f'"{self.python.with_name("pythonw.exe")}" -B "{deployed_app / "study_runtime.py"}" --resume'
        self.assertEqual(command, expected)
        self.assertEqual(store.startup_name, 'ProstranstvoStudySession')

    def test_beta_source_command_uses_base_python_not_venv_redirector(self):
        locations = SimpleNamespace(APP_ROOT=self.app, DEPLOY_ROOT=self.base)
        with mock.patch.object(study_resume, 'paths', locations), \
                mock.patch.object(study_resume.sys, '_base_executable', str(self.python)), \
                mock.patch.object(study_resume.sys, 'frozen', False, create=True):
            store = study_resume.StudyResume(self.base / 'data')
            command = store._startup_command()
        self.assertEqual(command, f'"{self.python.with_name("pythonw.exe")}" -B "{self.app / "study_runtime.py"}" --resume')
        self.assertEqual(store.startup_name, 'ProstranstvoLocalBetaStudySession')

    def test_frozen_beta_command_uses_windowed_launcher_fixed_resume_mode(self):
        core = self.app / 'Prostranstvo.exe'
        core.write_bytes(b'synthetic frozen core')
        with mock.patch.object(study_resume, 'paths', SimpleNamespace(APP_ROOT=self.app, DEPLOY_ROOT=self.base)), \
                mock.patch.object(study_resume.sys, 'frozen', True, create=True):
            store = study_resume.StudyResume(self.base / 'data')
            self.assertEqual(store._startup_command(), f'"{core}" --study-resume')

    def test_missing_runtime_refuses_to_prepare_startup_command(self):
        (self.app / 'study_runtime.py').unlink()
        with mock.patch.object(study_resume, 'paths', SimpleNamespace(APP_ROOT=self.app, DEPLOY_ROOT=self.base)), \
                mock.patch.object(study_resume.sys, '_base_executable', str(self.python)), \
                mock.patch.object(study_resume.sys, 'frozen', False, create=True):
            with self.assertRaises((ValueError, FileNotFoundError)):
                study_resume.StudyResume(self.base / 'data')._startup_command()


if __name__ == '__main__':
    unittest.main()
