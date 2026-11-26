"""Transfer ownership/cancellation regressions, only owned temporary documents."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_engine_regressions import isolated_engine

PRODUCT = Path(__file__).resolve().parents[1]


class TransferLifecycleTests(unittest.TestCase):
    def setUp(self):
        base = PRODUCT / 'build' / 'transfer-lifecycle'
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='owned-', dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        context = isolated_engine(self.root)
        self.module, paths = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.src, self.dst = self.root/'source', self.root/'destination'
        self.src.mkdir(); self.dst.mkdir()
        self.executor = self.module.Executor(self.root/'state', [self.src,self.dst], self.root/'missing-index.sqlite', [])

    def plan(self, content=b'owned source', mode='move'):
        source = self.src/'document.txt'
        source.write_bytes(content)
        job = self.executor.plan_transfer([str(source)], str(self.dst), mode)
        self.executor.approve(job)
        return source, self.dst/source.name, job

    def test_existing_partial_is_preserved_after_exclusive_open_refusal(self):
        source,target,job = self.plan(mode='copy')
        partial = self.dst/('.companion-'+job+'-0.partial')
        partial.write_bytes(b'other operation must keep this')
        with self.assertRaises(FileExistsError):self.executor.execute(job)
        self.assertEqual(partial.read_bytes(), b'other operation must keep this')
        self.assertEqual(source.read_bytes(), b'owned source')
        self.assertFalse(target.exists())
        self.assertEqual(self.executor.get(job)['status'], 'failed')

    def test_source_validation_failure_never_cleans_existing_partial(self):
        source,target,job = self.plan()
        partial = self.dst/('.companion-'+job+'-0.partial')
        partial.write_bytes(b'keep existing partial')
        source.write_bytes(b'changed source')
        with self.assertRaises(ValueError):self.executor.execute(job)
        self.assertEqual(partial.read_bytes(), b'keep existing partial')
        self.assertEqual(source.read_bytes(), b'changed source')
        self.assertFalse(target.exists())

    def test_zero_byte_move_cancelled_before_open_keeps_source(self):
        source,target,job = self.plan(b'')
        real = self.module.locked_file
        def cancelled(path, delete=False):
            self.executor.cancel(job)
            return real(path, delete)
        with mock.patch.object(self.module,'locked_file',side_effect=cancelled):
            with self.assertRaises(InterruptedError):self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.dst.iterdir()), [])
        self.assertEqual(self.executor.get(job)['status'], 'cancelled')

    def test_cancel_after_target_verification_rolls_back_own_copy(self):
        source,target,job = self.plan()
        real = self.module.digest
        fired = []
        def cancelled(path):
            result = real(path)
            if Path(path)==target and not fired:
                fired.append(True); self.executor.cancel(job)
            return result
        with mock.patch.object(self.module,'digest',side_effect=cancelled):
            with self.assertRaises(InterruptedError):self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'owned source')
        self.assertFalse(target.exists())
        self.assertEqual(self.executor.get(job)['status'], 'cancelled')
        with self.executor.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE job=?',(job,)).fetchone()[0],'rolled_back')

    def test_replacement_partial_is_not_deleted_during_error_cleanup(self):
        source,target,job = self.plan()
        partial = self.dst/('.companion-'+job+'-0.partial')
        real = self.module.shutil.copystat
        saved = self.dst/'owned-old-partial.saved'
        def replace(src,dst,*args,**kwargs):
            if Path(dst)==partial:
                partial.rename(saved)
                partial.write_bytes(b'new unrelated contents')
                raise OSError('injected failure after partial replacement')
            return real(src,dst,*args,**kwargs)
        with mock.patch.object(self.module.shutil,'copystat',side_effect=replace):
            with self.assertRaises(OSError):self.executor.execute(job)
        self.assertEqual(partial.read_bytes(), b'new unrelated contents')
        self.assertEqual(saved.read_bytes(), b'owned source')
        self.assertEqual(source.read_bytes(), b'owned source')
        self.assertFalse(target.exists())

    def test_partial_replacement_before_publish_is_preserved_without_rename(self):
        source,target,job = self.plan()
        partial = self.dst/('.companion-'+job+'-0.partial')
        saved = self.dst/'owned-old-partial.saved'
        real = self.module.shutil.copystat
        def replace(src,dst,*args,**kwargs):
            if Path(dst)==partial:
                partial.rename(saved)
                partial.write_bytes(b'replacement must stay at its own name')
                return
            return real(src,dst,*args,**kwargs)
        with mock.patch.object(self.module.shutil,'copystat',side_effect=replace):
            with self.assertRaises(ValueError):self.executor.execute(job)
        self.assertEqual(partial.read_bytes(), b'replacement must stay at its own name')
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), b'owned source')

    def test_cancel_during_index_update_before_delete_keeps_source(self):
        source,target,job = self.plan(b'')
        real = self.executor.update_index
        def cancelled(connection, record):
            real(connection, record)
            self.executor.cancel(job)
        with mock.patch.object(self.executor,'update_index',side_effect=cancelled):
            with self.assertRaises(InterruptedError):self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'')
        self.assertFalse(target.exists())
        self.assertEqual(self.executor.get(job)['status'],'cancelled')


if __name__=='__main__':unittest.main()
