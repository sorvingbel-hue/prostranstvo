"""Windows-only installer contract tests, using inert payloads and workspace data.

No installed personal instance, real startup key, scheduled task, network or
account is accessed. The fixture helper only returns 0 or waits in a test.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile


PRODUCT = Path(__file__).resolve().parents[1]
BUILD = PRODUCT / "build" / "installer-tests"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
IDENTITY = "prostranstvo-local-beta"


@unittest.skipUnless(os.name == "nt" and CSC.is_file(), "Windows Framework compiler required")
class InstallerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        BUILD.mkdir(parents=True, exist_ok=True)
        cls.fixture = Path(tempfile.mkdtemp(prefix="owned-", dir=BUILD)).resolve()
        helper_source = cls.fixture / "InertHelper.cs"
        helper_source.write_text(
            'using System.Threading; class TestHelper {'
            'static int Main(string[] args) {'
            'if(args.Length == 1 && args[0] == "hold") Thread.Sleep(30000);'
            'return 0; }}', encoding="utf-8"
        )
        helper = cls.fixture / "InertHelper.exe"
        subprocess.run([str(CSC), "/nologo", "/target:exe", "/out:" + str(helper), str(helper_source)], check=True, capture_output=True)
        cls.core = helper.read_bytes()
        files = {"ProstranstvoCore.exe": cls.core, "Prostranstvo.exe": cls.core,
                 "_internal/placeholder.bin": b"fixture runtime"}
        manifest = {"product": IDENTITY, "version": "0.1-test", "files": {
            name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in files.items()}}
        payload = cls.fixture / "payload.zip"
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
            archive.writestr("manifest.json", json.dumps(manifest).encode())
        cls.setup = cls.fixture / "FixtureSetup.exe"
        references = ["System.Windows.Forms.dll", "System.Drawing.dll", "System.Web.Extensions.dll",
                      "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll"]
        subprocess.run([str(CSC), "/nologo", "/target:winexe", "/out:" + str(cls.setup),
                        *["/reference:" + name for name in references],
                        "/resource:" + str(payload) + ",payload.zip",
                        str(PRODUCT / "packaging" / "SetupLocalBeta.cs")], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        # Windows recursive deletion is limited to this resolved fixture we own.
        if cls.fixture.parent != BUILD.resolve() or not cls.fixture.name.startswith("owned-"):
            raise RuntimeError("Refusing fixture cleanup outside installer-tests")
        shutil.rmtree(cls.fixture)

    def setUp(self):
        self.local = Path(tempfile.mkdtemp(prefix="local-", dir=self.fixture))
        self.root = self.local / "Programs" / "ProstranstvoLocalBeta"
        self.app = self.root / "app"
        self.data = self.local / "ProstranstvoLocalBeta" / "data"
        self.env = dict(os.environ, LOCALAPPDATA=str(self.local))

    def invoke(self, *args, ok=True):
        result = subprocess.run([str(self.setup), *args], env=self.env,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_inspect_and_selftest_do_not_create_install_or_data(self):
        self.assertEqual(self.invoke("--inspect-payload")["files"], 3)
        self.assertGreaterEqual(self.invoke("--self-test")["checks"], 23)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.data.exists())

    def test_install_update_uninstall_preserve_local_data(self):
        self.data.mkdir(parents=True)
        personal = self.data / "preserve.txt"
        personal.write_text("must survive", encoding="utf-8")
        first = self.invoke("--install")
        self.assertEqual(first["app"], str(self.app))
        self.assertFalse(first["shortcut"])
        self.assertEqual((self.app / "ProstranstvoCore.exe").read_bytes(), self.core)
        second = self.invoke("--install")
        self.assertTrue(Path(second["backup"]).is_dir())
        removed = self.invoke("--uninstall", "--yes")
        self.assertEqual(removed["status"], "removed_to_backup")
        self.assertFalse(self.app.exists())
        self.assertTrue(Path(removed["app_backup"]).is_dir())
        self.assertEqual(personal.read_text(encoding="utf-8"), "must survive")

    def test_unknown_occupied_destination_is_preserved(self):
        self.app.mkdir(parents=True)
        personal = self.app / "unrelated.txt"
        personal.write_bytes(b"unrelated")
        self.assertEqual(self.invoke("--install", ok=False)["status"], "error")
        self.assertEqual(personal.read_bytes(), b"unrelated")

    def test_existing_hardlink_is_refused(self):
        self.invoke("--install")
        try:
            os.link(self.fixture / "InertHelper.exe", self.app / "unexpected-hardlink.exe")
        except PermissionError:
            self.skipTest("This sandbox forbids creating hardlinks, so no hardlink fixture was created")
        self.assertEqual(self.invoke("--install", ok=False)["status"], "error")
        self.assertTrue((self.app / "unexpected-hardlink.exe").exists())

    def test_running_beta_refuses_update_and_uninstall(self):
        self.invoke("--install")
        process = subprocess.Popen([str(self.app / "ProstranstvoCore.exe"), "hold"], env=self.env)
        try:
            time.sleep(0.15)
            self.invoke("--install", ok=False)
            self.invoke("--uninstall", "--yes", ok=False)
            self.assertTrue(self.app.is_dir())
        finally:
            process.terminate()  # Only this inert test child, never a real beta process.
            process.wait(timeout=5)

    def test_uninstall_requires_embedded_manifest_anchor(self):
        self.invoke("--install")
        manifest = self.app / "manifest.json"
        value = json.loads(manifest.read_text())
        value["version"] = "different-build"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        marker = self.app / ".prostranstvo-install.json"
        identity = json.loads(marker.read_text())
        identity["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        marker.write_text(json.dumps(identity), encoding="utf-8")
        self.invoke("--uninstall", "--yes", ok=False)
        self.assertTrue(self.app.is_dir())

    def test_uninstall_refuses_unknown_runtime_file_before_helper(self):
        self.invoke("--install")
        extra = self.app / "unknown-library.dll"
        extra.write_bytes(b"preserve this unknown file")
        self.invoke("--uninstall", "--yes", ok=False)
        self.assertEqual(extra.read_bytes(), b"preserve this unknown file")

    def test_global_install_mutex_blocks_second_cycle_without_writes(self):
        identity = str(self.root.resolve()).rstrip("\\").upper().encode("utf-8")
        name = "Global\\ProstranstvoLocalBetaInstaller-" + hashlib.sha256(identity).hexdigest()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, True, name)
        self.assertTrue(handle)
        try:
            self.invoke("--install", ok=False)
            self.assertFalse(self.root.exists())
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
