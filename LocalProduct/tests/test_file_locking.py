"""Real Windows byte-lock contention on owned synthetic files only.

The holder is a hidden child Python process. The blocking regression observes
the real native lock refusal, then releases that holder; no error is simulated
and PermissionError is never retried by the fixture.
"""

import contextlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


PRODUCT = Path(__file__).resolve().parents[1]
WORKSPACE = PRODUCT.parent
sys.path.insert(0, str(PRODUCT / 'src'))
SOURCES = [('portable', PRODUCT / 'src')]
# The public/source distribution contains LocalProduct alone. Always exercise
# its helper; add the private legacy backend only when this checkout owns it.
if (WORKSPACE / 'WindowsCompanion' / 'activity_core.py').is_file():
    SOURCES.append(('personal', WORKSPACE / 'WindowsCompanion'))

HOLDER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from activity_core import exclusive_lock
lock, ready, release, released = map(Path, sys.argv[2:])
with exclusive_lock(lock):
    ready.write_text('held', encoding='ascii')
    deadline = time.monotonic() + 8
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned holder release deadline exceeded')
        time.sleep(.01)
released.write_text('released', encoding='ascii')
'''


class FileLockingTests(unittest.TestCase):
    def setUp(self):
        self.base = PRODUCT / 'build' / 'file-locking-tests'
        self.base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='owned-', dir=self.base)).resolve()
        self.addCleanup(self._cleanup_owned_fixture)

    def _cleanup_owned_fixture(self):
        if self.root.parent != self.base.resolve() or not self.root.name.startswith('owned-'):
            raise RuntimeError('Refusing cleanup outside this newly owned fixture')
        shutil.rmtree(self.root)

    def _load(self, branch, source):
        specification = importlib.util.spec_from_file_location(
            '_file_lock_regression_' + branch, source / 'activity_core.py',
        )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def _wait_for(self, path, process, seconds=3):
        deadline = time.monotonic() + seconds
        while not path.exists():
            if process.poll() is not None:
                _, error = process.communicate(timeout=1)
                self.fail(f'Owned holder exited before {path.name}: {error!r}')
            if time.monotonic() >= deadline:
                self.fail(f'Owned holder did not signal {path.name} before deadline')
            time.sleep(.01)

    @contextlib.contextmanager
    def _held(self, source, lock):
        ready = lock.with_suffix('.ready')
        release = lock.with_suffix('.release')
        released = lock.with_suffix('.released')
        environment = dict(os.environ, LOCALAPPDATA=str(self.root / 'LocalAppData'),
                           APPDATA=str(self.root / 'Roaming'), TEMP=str(self.root), TMP=str(self.root))
        process = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', HOLDER, str(source), str(lock),
             str(ready), str(release), str(released)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.root, env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self._wait_for(ready, process)
            yield release, released, process
        finally:
            release.touch(exist_ok=True)
            try:
                _, error = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                # This exact Popen child only; no process-name or desktop search.
                process.terminate()
                process.communicate(timeout=3)
                self.fail('Owned holder failed to exit after release')
            self.assertEqual(process.returncode, 0, error.decode('utf-8', errors='replace'))

    @unittest.skipUnless(os.name == 'nt', 'Requires real Windows mandatory byte locks')
    def test_blocking_contender_waits_for_native_holder_release(self):
        import msvcrt

        for branch, source in SOURCES:
            module = self._load(branch, source)
            for index, initial in enumerate((b'', b'0')):
                with self.subTest(branch=branch, initial=initial):
                    lock = self.root / f'{branch}-blocking-{index}.lock'
                    lock.write_bytes(initial)
                    with self._held(source, lock) as (release, released, holder):
                        native_locking = msvcrt.locking
                        attempts = []
                        contention = []

                        def release_after_real_refusal(descriptor, mode, size):
                            if mode == msvcrt.LK_NBLCK:
                                attempts.append(mode)
                            try:
                                return native_locking(descriptor, mode, size)
                            except OSError:
                                if mode == msvcrt.LK_NBLCK:
                                    # A real cross-process lock failure is the
                                    # deterministic barrier for holder release.
                                    contention.append(True)
                                    release.touch(exist_ok=True)
                                raise

                        with mock.patch.object(msvcrt, 'locking', side_effect=release_after_real_refusal):
                            with module.exclusive_lock(lock):
                                self.assertTrue(contention, 'Holder was not observed owning byte zero')
                                self.assertGreaterEqual(len(attempts), 2)
                                self._wait_for(released, holder)
                    self.assertEqual(lock.read_bytes(), initial)

    @unittest.skipUnless(os.name == 'nt', 'Requires real Windows mandatory byte locks')
    def test_nonblocking_contender_reports_busy_instead_of_permission_error(self):
        for branch, source in SOURCES:
            module = self._load(branch, source)
            for index, initial in enumerate((b'', b'0')):
                with self.subTest(branch=branch, initial=initial):
                    lock = self.root / f'{branch}-nonblocking-{index}.lock'
                    lock.write_bytes(initial)
                    with self._held(source, lock):
                        with self.assertRaises(RuntimeError):
                            with module.exclusive_lock(lock, blocking=False):
                                self.fail('A held byte lock must not be acquired')
                    # Release must leave the lock reusable for both old and new
                    # file layouts without initializing or rewriting contents.
                    with module.exclusive_lock(lock, blocking=False):
                        pass
                    self.assertEqual(lock.read_bytes(), initial)

    def test_empty_and_preexisting_one_byte_files_remain_unchanged_and_reusable(self):
        for branch, source in SOURCES:
            module = self._load(branch, source)
            for index, initial in enumerate((b'', b'0')):
                with self.subTest(branch=branch, initial=initial):
                    lock = self.root / f'{branch}-contents-{index}.lock'
                    lock.write_bytes(initial)
                    for _ in range(2):
                        with module.exclusive_lock(lock, blocking=False):
                            pass
                    self.assertEqual(lock.read_bytes(), initial)


if __name__ == '__main__':
    unittest.main()
