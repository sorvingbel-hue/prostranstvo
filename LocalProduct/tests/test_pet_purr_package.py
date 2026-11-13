"""Compile both kitten variants in an isolated folder; run only pure diagnostics.

No window, cursor replacement, speaker playback, personal data or process cleanup
is requested by this fixture. Its self-test entry point exits before GUI setup.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PRODUCT = Path(__file__).resolve().parents[1]
NATIVE = PRODUCT / 'native'
CSC = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'


@unittest.skipUnless(os.name == 'nt' and CSC.is_file(), 'Windows .NET Framework compiler required')
class PetPurrPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = PRODUCT / 'build' / 'pet-purr-tests'
        base.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='compiled-', dir=base)
        cls.root = Path(cls.temporary.name).resolve()
        if not cls.root.is_relative_to(base.resolve()):
            raise AssertionError('Unexpected fixture directory')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.executables = {}
        for mode in ('BETA', 'PERSONAL'):
            output = cls.root / (mode + '-NativeDesktopPet.exe')
            command = [str(CSC), '/nologo', '/platform:x64', '/target:exe', '/out:' + str(output),
                       '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll']
            if mode == 'PERSONAL':
                command.append('/define:PERSONAL')
            command += [str(NATIVE / 'DesktopPet.cs'), str(NATIVE / 'PetPurr.cs'), str(NATIVE / 'PetAmbience.cs'), str(NATIVE / 'PetTricks.cs')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=40,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.executables[mode] = output

    def run_native(self, mode, *arguments):
        environment = dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        return subprocess.run([str(self.executables[mode]), *arguments],
                              capture_output=True, text=True, timeout=15, env=environment,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_beta_combines_animation_and_sound_helper_without_initializing_desktop(self):
        result = self.run_native('BETA', '--self-test')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout.strip().lstrip('\ufeff'))
        self.assertEqual(data['status'], 'self_test_ok')
        self.assertGreaterEqual(data['checks'], 18)
        self.assertIs(data['desktop_mutations'], False)
        self.assertIs(data['network'], False)
        self.assertFalse((self.root / 'profile').exists())

    def test_personal_build_uses_the_same_pure_self_test_contract(self):
        result = self.run_native('PERSONAL', '--self-test')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout.strip().lstrip('\ufeff'))
        self.assertEqual(data['status'], 'self_test_ok')
        self.assertIs(data['desktop_mutations'], False)
        self.assertIs(data['network'], False)
        self.assertFalse((self.root / 'profile').exists())

    def test_unknown_option_exits_before_desktop_or_audio_setup(self):
        for mode in ('BETA', 'PERSONAL'):
            with self.subTest(mode=mode):
                result = self.run_native(mode, '--unknown-fixture-option')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((self.root / 'profile').exists())


if __name__ == '__main__':
    unittest.main()
