import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Fixed editor command only. Fixtures never open a browser or server."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pet_tricks_launch as launcher


class LaunchTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).absolute().parent/'tests-work';base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='tricks-launch-',dir=base)
        self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)
        self.python=self.root/'python.exe';self.python.write_bytes(b'fixture')
        (self.root/'python-path.txt').write_text(str(self.python),encoding='utf-8')
        (self.root/'pet_tricks_server.py').write_text('# fixed fixture',encoding='utf-8')
        self.spawn=mock.Mock()

    def test_source_launch_has_only_fixed_script_and_open_mode(self):
        with mock.patch.object(launcher.sys,'frozen',False,create=True):launcher.launch(self.root,popen=self.spawn)
        args,kwargs=self.spawn.call_args
        self.assertEqual(args[0],[str(self.python),'-B',str(self.root/'pet_tricks_server.py'),'--open'])
        self.assertIs(kwargs['shell'],False)
        self.assertEqual(kwargs['cwd'],str(self.root))
        self.assertEqual(kwargs['stdin'],launcher.subprocess.DEVNULL)

    def test_frozen_launch_uses_fixed_core_mode(self):
        (self.root/'ProstranstvoCore.exe').write_bytes(b'fixture')
        with mock.patch.object(launcher.sys,'frozen',True,create=True):launcher.launch(self.root,popen=self.spawn)
        self.assertEqual(self.spawn.call_args.args[0],[str(self.root/'ProstranstvoCore.exe'),'tricks','--open'])

    def test_bad_interpreter_missing_helper_and_links_never_spawn(self):
        for value in ('powershell.exe','C:\\Windows\\System32\\cmd.exe','python.exe','bad\x00'):
            (self.root/'python-path.txt').write_text(value,encoding='utf-8')
            with self.assertRaises(launcher.TricksLaunchError):launcher.launch(self.root,popen=self.spawn)
        (self.root/'python-path.txt').write_text(str(self.python),encoding='utf-8')
        with mock.patch.object(launcher,'no_links',side_effect=ValueError('private path')):
            with self.assertRaises(launcher.TricksLaunchError):launcher.launch(self.root,popen=self.spawn)
        self.spawn.assert_not_called()


if __name__=='__main__':unittest.main()
