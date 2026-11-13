import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Desktop-pet shim fixtures. Native execution and personal assets are never used."""
import importlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import desktop_pet


class DesktopPetShimTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='desktop-pet-shim-',dir=base)
        self.root=Path(self.temporary.name)
        self.executable=self.root/'NativeDesktopPet.exe'
        self.executable.write_bytes(b'MZ fixture only; never executed')
        self.image=self.root/'assets'/'kitten-sprites-v1.png'
        self.image.parent.mkdir()
        self.png()

    def tearDown(self):self.temporary.cleanup()

    def png(self,width=1024,height=1024,size=1024,signature=b'\x89PNG\r\n\x1a\n',kind=b'IHDR'):
        header=signature+struct.pack('>I',13)+kind+struct.pack('>II',width,height)
        with self.image.open('wb') as output:
            output.write(header)
            output.truncate(size)

    def test_import_is_inert(self):
        with mock.patch('pathlib.Path.mkdir') as mkdir,mock.patch('subprocess.Popen') as spawn:
            importlib.reload(desktop_pet)
            mkdir.assert_not_called();spawn.assert_not_called()

    def test_only_fixed_executable_and_sprite_path(self):
        (self.root/'OtherPet.exe').write_bytes(b'other fixture')
        (self.root/'kitten-sprites-v1.png').write_bytes(b'wrong location')
        self.assertEqual(desktop_pet.checked_assets(self.root),self.executable)
        self.executable.unlink()
        with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)

    def test_missing_sprite_is_refused(self):
        self.image.unlink()
        with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)

    def test_non_file_executable_is_refused(self):
        self.executable.unlink();self.executable.mkdir()
        with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)

    def test_png_signature_and_ihdr_type_are_required(self):
        for signature,kind in ((b'notPNG!!',b'IHDR'),(b'\x89PNG\r\n\x1a\n',b'IDAT')):
            with self.subTest(signature=signature,kind=kind):
                self.png(signature=signature,kind=kind)
                with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)

    def test_size_bounds_refuse_truncation_and_oversized_blob(self):
        for size in (0,23,1023,20*1024**2+1):
            with self.subTest(size=size):
                self.png(size=size)
                with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)

    def test_dimension_bounds_and_square_layout(self):
        for width,height in ((0,0),(511,511),(4097,4097),(8193,8193),(1024,2048),(0xffffffff,0xffffffff)):
            with self.subTest(width=width,height=height):
                self.png(width,height)
                with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)
        for width in (512,1024,2048,4096):
            self.png(width,width)
            self.assertEqual(desktop_pet.checked_assets(self.root),self.executable)

    def test_hardlinked_asset_or_binary_is_refused(self):
        if not hasattr(os,'link'):self.skipTest('Hardlinks unavailable')
        for name,target in (('extra-image-link',self.image),('extra-exe-link',self.executable)):
            linked=self.root/name
            try:os.link(target,linked)
            except OSError as error:self.skipTest('Fixture hardlink unavailable: '+type(error).__name__)
            try:
                with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)
            finally:linked.unlink()

    def test_symlink_asset_is_refused_when_supported(self):
        actual=self.root/'real-sprite.png'
        self.image.rename(actual)
        try:os.symlink(actual,self.image)
        except (OSError,NotImplementedError):
            actual.rename(self.image)
            self.skipTest('Fixture symlink unavailable')
        try:
            with self.assertRaises(ValueError):desktop_pet.checked_assets(self.root)
        finally:
            self.image.unlink();actual.rename(self.image)

    def test_unknown_arguments_refused_before_validation_or_launch(self):
        with mock.patch.object(desktop_pet,'launch') as launch,mock.patch.object(desktop_pet,'checked_assets') as check:
            for arguments in (['--token','private'],['--mini'],['C:\\arbitrary.exe']):
                with self.subTest(arguments=arguments),self.assertRaises(ValueError):desktop_pet.main(arguments)
            with mock.patch.object(desktop_pet.sys,'argv',['desktop_pet.py','--unknown']):
                with self.assertRaises(ValueError):desktop_pet.main()
            launch.assert_not_called();check.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows native launch contract')
    def test_launch_has_fixed_argv_cwd_and_no_shell_or_secrets(self):
        process=mock.Mock()
        with mock.patch.object(desktop_pet.subprocess,'Popen',return_value=process) as spawn:
            self.assertIs(desktop_pet.launch(self.root),process)
            spawn.assert_called_once_with([str(self.executable)],cwd=self.root,creationflags=desktop_pet.subprocess.CREATE_NO_WINDOW)
            self.assertNotIn('shell',spawn.call_args.kwargs)
            self.assertNotIn('env',spawn.call_args.kwargs)

    @unittest.skipUnless(os.name=='nt','Windows native launch contract')
    def test_invalid_assets_never_spawn_a_process(self):
        self.image.unlink()
        with mock.patch.object(desktop_pet.subprocess,'Popen') as spawn:
            with self.assertRaises(ValueError):desktop_pet.launch(self.root)
            spawn.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows native launch contract')
    def test_spawn_failure_is_sanitized(self):
        with mock.patch.object(desktop_pet.subprocess,'Popen',side_effect=OSError('token=private-secret')):
            with self.assertRaises(RuntimeError) as raised:desktop_pet.launch(self.root)
        self.assertNotIn('private-secret',str(raised.exception))

    def test_non_windows_fails_before_asset_validation(self):
        with mock.patch.object(desktop_pet.os,'name','posix'),mock.patch.object(desktop_pet,'checked_assets') as check:
            with self.assertRaises(ValueError):desktop_pet.launch(self.root)
            check.assert_not_called()

    def test_main_waits_for_only_our_launcher_process(self):
        process=mock.Mock();process.wait.return_value=7
        with mock.patch.object(desktop_pet,'launch',return_value=process) as launch:
            self.assertEqual(desktop_pet.main([]),7)
            launch.assert_called_once_with();process.wait.assert_called_once_with()


if __name__=='__main__':unittest.main()
