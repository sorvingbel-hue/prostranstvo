"""Crash-boundary recovery uses only synthetic files below LocalProduct/build."""
import json
import os
from pathlib import Path
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_portable_data import PRODUCT, fixture_modules


class TransferRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = PRODUCT / 'build' / 'test-temporary'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='recovery-', dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        context = fixture_modules(self.root)
        self.modules = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.engine = self.modules.engine
        self.executor = self.engine.Executor()
        self.source = self.modules.paths.DEFAULT_ALLOWED_ROOTS[0] / 'fixture.txt'
        self.destination = self.modules.paths.DEFAULT_ALLOWED_ROOTS[1]
        self.target = self.destination / self.source.name
        self.body = b'Verified fixture payload\r\n' * 4096
        self.source.write_bytes(self.body)

    def stage_receipt(self, mode='move', source='absent', destination='valid', index=None, index_path='destination'):
        if index:
            self.executor.index = self.root / 'catalog.sqlite'
            with contextlib.closing(sqlite3.connect(self.executor.index)) as db, db:
                db.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT)')
                db.execute('INSERT INTO files VALUES(1,?)', (str(self.source),))
        job = self.executor.plan_transfer([str(self.source)], str(self.destination), mode)
        record = self.executor.get(job)['payload']['records'][0]
        if destination != 'absent':
            self.target.write_bytes(self.body if destination == 'valid' else b'X' + self.body[1:])
        if source == 'absent':
            self.source.unlink()
        elif source == 'conflicting':
            self.source.write_bytes(b'new source that must survive')
        if index:
            with contextlib.closing(sqlite3.connect(self.executor.index)) as db, db:
                db.execute('UPDATE files SET path=? WHERE id=1', (str(self.target if index_path == 'destination' else self.source),))
        with self.executor.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',
                       (job, 0, record['src'], record['dst'], record['sha'], record['stamp']['size'], record['index_id'], 'copied'))
        return job

    def receipt(self, job):
        with self.executor.connect() as db:
            return dict(db.execute('SELECT * FROM receipts WHERE job=?', (job,)).fetchone())

    def test_real_process_exit_after_source_close_becomes_reviewable_undo(self):
        script = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import engine
config=json.loads(sys.argv[2])
executor=engine.Executor(data=config['data'], roots=config['roots'], index=config['index'], blocked=[])
job=executor.plan_transfer([config['source']], config['destination'], 'move')
executor.approve(job)
original=engine.locked_file
class CrashAfterClose:
 def __init__(self, stream): self.stream=stream
 def __getattr__(self, name): return getattr(self.stream,name)
 def close(self):
  self.stream.close()
  os._exit(73)
engine.locked_file=lambda *a,**k:CrashAfterClose(original(*a,**k))
executor.execute(job)
raise SystemExit(99)
'''
        config = {'data': str(self.executor.data), 'roots': [str(x) for x in self.executor.roots],
                  'index': str(self.executor.index), 'source': str(self.source), 'destination': str(self.destination)}
        child = subprocess.run([sys.executable, '-B', '-c', script, str(PRODUCT / 'src'), json.dumps(config)],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 73, child.stdout + child.stderr)
        job = self.executor.jobs()[0]['id']
        self.assertEqual(self.receipt(job)['state'], 'copied')
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.read_bytes(), self.body)
        summary = self.executor.recover()
        self.assertEqual(summary['verified_moves'], 1)
        self.assertEqual(summary['blocked'], [])
        self.assertEqual(self.executor.get(job)['status'], 'interrupted')
        self.assertEqual(self.receipt(job)['state'], 'complete')
        self.assertFalse(self.source.exists(), 'Recovery itself must not move files')
        self.assertEqual(self.target.read_bytes(), self.body)
        undo = self.executor.undo(job)
        self.assertEqual(self.executor.get(undo)['status'], 'needs_approval')
        self.executor.approve(undo)
        self.executor.execute(undo)
        self.assertEqual(self.source.read_bytes(), self.body)
        self.assertFalse(self.target.exists())

    def test_recovery_is_idempotent_and_does_not_retry_move(self):
        job = self.stage_receipt()
        self.assertEqual(self.executor.recover()['verified_moves'], 1)
        self.assertEqual(self.executor.recover(), {'verified_moves': 0, 'blocked': []})
        self.assertEqual(self.receipt(job)['state'], 'complete')
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.read_bytes(), self.body)

    def assert_refused(self, job):
        before = {p: p.read_bytes() if p.exists() else None for p in (self.source, self.target)}
        summary = self.executor.recover()
        self.assertEqual(summary['verified_moves'], 0)
        self.assertTrue(summary['blocked'])
        self.assertEqual(self.receipt(job)['state'], 'copied')
        self.assertEqual(self.executor.get(job)['status'], 'interrupted')
        self.assertIn('ручной', self.executor.get(job)['error'])
        self.assertEqual({p: p.read_bytes() if p.exists() else None for p in before}, before)
        with self.assertRaisesRegex(ValueError, 'Нет завершённых'):
            self.executor.undo(job)

    def test_changed_destination_is_refused_without_touching_it(self):
        self.assert_refused(self.stage_receipt(destination='changed'))

    def test_both_matching_files_present_is_ambiguous(self):
        self.assert_refused(self.stage_receipt(source='present'))

    def test_conflicting_source_is_preserved(self):
        self.assert_refused(self.stage_receipt(source='conflicting'))

    def test_missing_destination_is_refused(self):
        self.assert_refused(self.stage_receipt(destination='absent'))

    def test_copy_receipt_is_not_promoted_into_a_move(self):
        job = self.stage_receipt(mode='copy', source='present')
        summary = self.executor.recover()
        self.assertEqual(summary, {'verified_moves': 0, 'blocked': []})
        self.assertEqual(self.receipt(job)['state'], 'copied')
        self.assertEqual(self.source.read_bytes(), self.body)
        self.assertEqual(self.target.read_bytes(), self.body)
        with self.assertRaisesRegex(ValueError, 'для перемещений'):
            self.executor.undo(job)

    def test_consistent_index_allows_recovery_without_index_writes(self):
        job = self.stage_receipt(index=True)
        before = self.executor.index.read_bytes()
        self.assertEqual(self.executor.recover()['verified_moves'], 1)
        self.assertEqual(self.receipt(job)['state'], 'complete')
        self.assertEqual(self.executor.index.read_bytes(), before)

    def test_stale_index_refuses_recovery_without_modifying_it(self):
        job = self.stage_receipt(index=True, index_path='source')
        before = self.executor.index.read_bytes()
        self.assert_refused(job)
        self.assertEqual(self.executor.index.read_bytes(), before)

    def test_missing_index_refuses_an_indexed_move(self):
        job = self.stage_receipt(index=True)
        self.executor.index.unlink()
        self.assert_refused(job)
        self.assertFalse(self.executor.index.exists())

    def test_receipt_that_disagrees_with_plan_is_refused(self):
        job = self.stage_receipt()
        with self.executor.connect() as db:
            db.execute("UPDATE receipts SET dst=? WHERE job=?", (str(self.root / 'outside.txt'), job))
        self.assert_refused(job)

    def test_recovery_cannot_interrupt_a_live_locked_executor(self):
        job = self.stage_receipt()
        with self.executor.lock(), self.assertRaises(self.engine.Busy):
            self.executor.recover()
        self.assertEqual(self.executor.get(job)['status'], 'running')
        self.assertEqual(self.receipt(job)['state'], 'copied')

    def test_changed_completed_receipt_still_refuses_undo(self):
        job = self.stage_receipt()
        self.executor.recover()
        self.target.write_bytes(b'edited after recovery')
        with self.assertRaisesRegex(ValueError, 'Файлы изменены'):
            self.executor.undo(job)
        self.assertEqual(self.target.read_bytes(), b'edited after recovery')
        self.assertFalse(self.source.exists())

    def test_receipt_update_exception_after_source_close_is_reviewable(self):
        job = self.executor.plan_transfer([str(self.source)], str(self.destination), 'move')
        self.executor.approve(job)
        connect = self.executor.connect
        injected = []
        class ReceiptWriteFailure:
            def __init__(self, connection):
                self.connection = connection
            def __enter__(self):
                self.connection.__enter__()
                return self
            def __exit__(self, *args):
                return self.connection.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, *args):
                if "UPDATE receipts SET state='complete'" in sql and not injected:
                    injected.append(True)
                    raise sqlite3.OperationalError('fixture receipt write failure after source close')
                return self.connection.execute(sql, *args)
        with mock.patch.object(self.executor, 'connect', side_effect=lambda: ReceiptWriteFailure(connect())):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture receipt write failure'):
                self.executor.execute(job)
        self.assertTrue(injected)
        self.assertEqual(self.executor.get(job)['status'], 'failed')
        original_error = self.executor.get(job)['error']
        self.assertEqual(self.receipt(job)['state'], 'copied')
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.read_bytes(), self.body)
        self.assertEqual(self.executor.recover()['verified_moves'], 1)
        recovered = self.executor.get(job)
        self.assertEqual(recovered['status'], 'failed')
        self.assertTrue(recovered['error'].startswith(original_error + '\n'))
        self.assertIn('можно подготовить обратный план', recovered['error'])
        self.assertEqual(self.receipt(job)['state'], 'complete')
        self.assertEqual(self.executor.recover(), {'verified_moves': 0, 'blocked': []})
        self.assertEqual(self.executor.get(job)['error'], recovered['error'])
        self.assertFalse(self.source.exists(), 'Recovery itself never restores automatically')
        undo = self.executor.undo(job)
        self.assertEqual(self.executor.get(undo)['status'], 'needs_approval')
        self.executor.approve(undo)
        self.executor.execute(undo)
        self.assertEqual(self.source.read_bytes(), self.body)
        self.assertFalse(self.target.exists())

    def test_unrelated_failed_move_without_receipt_is_unchanged(self):
        job = self.executor.plan_transfer([str(self.source)], str(self.destination), 'move')
        with self.executor.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", ('fixture original error', job))
        before = self.executor.get(job)
        self.assertEqual(self.executor.recover(), {'verified_moves': 0, 'blocked': []})
        self.assertEqual(self.executor.get(job), before)
        self.assertEqual(self.source.read_bytes(), self.body)
        self.assertFalse(self.target.exists())

    def test_ambiguous_failed_move_keeps_original_error_without_duplicate_notes(self):
        job = self.stage_receipt(source='conflicting')
        with self.executor.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", ('fixture original error', job))
        first = self.executor.recover()
        self.assertEqual(first['verified_moves'], 0)
        self.assertTrue(first['blocked'])
        error = self.executor.get(job)['error']
        self.assertTrue(error.startswith('fixture original error\n'))
        self.assertIn('требуют ручной проверки', error)
        self.assertEqual(self.executor.get(job)['status'], 'failed')
        self.assertEqual(self.receipt(job)['state'], 'copied')
        self.assertEqual(self.executor.recover(), first)
        self.assertEqual(self.executor.get(job)['error'], error)
        self.assertEqual(self.source.read_bytes(), b'new source that must survive')
        self.assertEqual(self.target.read_bytes(), self.body)


if __name__ == '__main__':
    unittest.main()
