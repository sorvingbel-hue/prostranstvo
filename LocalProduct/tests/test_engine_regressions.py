"""File-executor regressions on synthetic workspace data and an isolated paths contract."""
import contextlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_engine(root):
    paths = types.ModuleType('paths')
    paths.APP_ROOT = root / 'installed-app'
    paths.DATA_ROOT = root / 'local-profile' / 'ProstranstvoLocalBeta' / 'data'
    paths.BASE_ROOT = paths.DATA_ROOT.parent
    paths.DEPLOY_ROOT = paths.BASE_ROOT
    paths.SOURCE_ROOT = paths.APP_ROOT
    paths.EXECUTOR_DATA = paths.DATA_ROOT / 'executor'
    paths.MAINTENANCE_ROOT = paths.DATA_ROOT / 'maintenance'
    paths.CATALOG_PATH = paths.DATA_ROOT / 'catalog.sqlite'
    paths.DEFAULT_ALLOWED_ROOTS = [root / 'source', root / 'destination']
    paths.DEFAULT_BLOCKED_ROOTS = [paths.APP_ROOT, paths.DATA_ROOT, root / '.codex']
    with mock.patch.dict(sys.modules), mock.patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'local-profile')}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
        sys.modules.pop('engine', None)
        sys.modules['paths'] = paths
        yield importlib.import_module('engine'), paths


class EngineRegressionTests(unittest.TestCase):
    def setUp(self):
        base = PRODUCT / 'build' / 'engine-regressions'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='fixture-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.context = isolated_engine(self.root)
        self.module, self.paths = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.src = self.root / 'source'
        self.dst = self.root / 'destination'
        self.src.mkdir()
        self.dst.mkdir()
        self.index = self.root / 'index.sqlite'
        with sqlite3.connect(self.index, factory=self.module.ClosingConnection) as db:
            db.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,name TEXT,extension TEXT,size INTEGER,modified_utc TEXT,source_root TEXT,category TEXT,placeholder INTEGER,hidden INTEGER,attributes INTEGER,newly_covered INTEGER);
CREATE VIRTUAL TABLE files_fts USING fts5(name,path,extension,category,source_root,content='files',content_rowid='id');""")
        self.executor = self.module.Executor(self.root / 'state', [self.src, self.dst], self.index, [])

    def file(self, name='example.txt', data=b'valuable fixture document'):
        path = self.src / name
        path.write_bytes(data)
        return path

    def indexed(self, path):
        with sqlite3.connect(self.index, factory=self.module.ClosingConnection) as db:
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (17, str(path), path.name, path.suffix, path.stat().st_size, '2026', str(self.src), 'documents', 0, 0, 0, 0))
            db.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")

    def execute_plan(self, path, mode='move'):
        job = self.executor.plan_transfer([str(path)], str(self.dst), mode)
        self.executor.approve(job)
        self.executor.execute(job)
        return job

    def indexed_path(self):
        with sqlite3.connect(self.index, factory=self.module.ClosingConnection) as db:
            return db.execute('SELECT path FROM files WHERE id=17').fetchone()[0]

    def test_verified_move_updates_same_index_id_and_fts_then_undo(self):
        source = self.file()
        self.indexed(source)
        job = self.execute_plan(source)
        target = self.dst / source.name
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b'valuable fixture document')
        self.assertEqual(self.indexed_path(), str(target))
        with sqlite3.connect(self.index, factory=self.module.ClosingConnection) as db:
            self.assertEqual(db.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH 'example'").fetchone()[0], 17)
        reverse = self.executor.undo(job)
        self.assertEqual(self.executor.get(reverse)['status'], 'needs_approval')
        self.executor.approve(reverse)
        self.executor.execute(reverse)
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertFalse(target.exists())
        self.assertEqual(self.indexed_path(), str(source))

    def test_unapproved_plan_does_not_change_files(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.assertEqual(self.executor.get(job)['status'], 'needs_approval')
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_early_collision_never_overwrites(self):
        source = self.file()
        target = self.dst / source.name
        target.write_bytes(b'existing fixture')
        with self.assertRaises(ValueError):
            self.executor.plan_transfer([str(source)], str(self.dst))
        self.assertEqual(target.read_bytes(), b'existing fixture')
        self.assertTrue(source.exists())

    def test_late_collision_preserves_both_files(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        target = self.dst / source.name
        target.write_bytes(b'new file since planning')
        self.executor.approve(job)
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertEqual(target.read_bytes(), b'new file since planning')
        self.assertEqual(source.read_bytes(), b'valuable fixture document')

    def test_changed_source_is_rejected(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        source.write_bytes(b'changed fixture')
        self.executor.approve(job)
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'changed fixture')
        self.assertFalse((self.dst / source.name).exists())

    def test_drifted_index_rolls_back_copy_without_deleting_source(self):
        source = self.file()
        self.indexed(source)
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        elsewhere = self.src / 'elsewhere.txt'
        with sqlite3.connect(self.index, factory=self.module.ClosingConnection) as db:
            db.execute('UPDATE files SET path=? WHERE id=17', (str(elsewhere),))
        self.executor.approve(job)
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertFalse((self.dst / source.name).exists())
        self.assertEqual(self.indexed_path(), str(elsewhere))

    def test_disk_full_fsync_failure_keeps_source_and_removes_partial(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        with mock.patch.object(self.module.os, 'fsync', side_effect=OSError('Synthetic disk-full failure')):
            with self.assertRaises(OSError):
                self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_copy_keeps_original_and_unchanged_index(self):
        source = self.file()
        self.indexed(source)
        job = self.execute_plan(source, 'copy')
        self.assertEqual(source.read_bytes(), (self.dst / source.name).read_bytes())
        self.assertEqual(self.indexed_path(), str(source))
        with self.assertRaises(ValueError):
            self.executor.undo(job)

    def test_cancelled_queued_plan_never_runs(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        self.executor.cancel(job)
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertTrue(source.exists())
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_other_executor_cannot_recover_live_work_under_lock(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        with self.executor.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        other = self.module.Executor(self.executor.data, [self.src, self.dst], self.index, [])
        with self.executor.lock():
            with self.assertRaises(self.module.Busy):
                other.recover()
            self.assertEqual(other.get(job)['status'], 'running')
        self.assertTrue(source.exists())

    def test_duplicate_groups_use_content_not_name_or_size_only(self):
        self.file('one.txt', b'same')
        self.file('two.txt', b'same')
        self.file('three.txt', b'else')
        job = self.executor.create('duplicates', {'root': str(self.src)})
        result = self.executor.execute(job)['result']
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual({Path(row['path']).name for row in result['groups'][0]['files']}, {'one.txt', 'two.txt'})
        self.assertEqual(len(list(self.src.iterdir())), 3)

    def test_index_commit_failure_keeps_source_and_old_index(self):
        source = self.file()
        self.indexed(source)
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        original = self.executor.index_connection
        class FailedCommit:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def commit(self):
                raise sqlite3.OperationalError('Synthetic commit failure')
        def connection(write=False):
            db = original(write)
            return FailedCommit(db) if write else db
        with mock.patch.object(self.executor, 'index_connection', side_effect=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.executor.execute(job)
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertFalse((self.dst / source.name).exists())
        self.assertEqual(self.indexed_path(), str(source))

    def test_verified_rollback_is_not_reported_for_manual_review_on_recover(self):
        # Regression supplied by Claude in 2e1530733fe01d7310a024801491d92c4acf86ac.
        source = self.file()
        self.indexed(source)
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        original = self.executor.index_connection
        class FailedCommit:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def commit(self):
                raise sqlite3.OperationalError('Synthetic commit failure')
        def connection(write=False):
            db = original(write)
            return FailedCommit(db) if write else db
        with mock.patch.object(self.executor, 'index_connection', side_effect=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.executor.execute(job)
        with self.executor.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE job=? AND n=0', (job,)).fetchone()[0], 'rolled_back')
        for _ in range(2):
            self.assertEqual(self.executor.recover(), {'verified_moves': 0, 'blocked': []})
        self.assertNotIn('ручной', self.executor.get(job)['error'])
        self.assertEqual(source.read_bytes(), b'valuable fixture document')
        self.assertFalse((self.dst / source.name).exists())
        self.assertEqual(self.indexed_path(), str(source))

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_open_writer_prevents_move(self):
        source = self.file()
        job = self.executor.plan_transfer([str(source)], str(self.dst))
        self.executor.approve(job)
        with source.open('r+b'):
            with self.assertRaises(OSError):
                self.executor.execute(job)
        self.assertTrue(source.exists())
        self.assertFalse((self.dst / source.name).exists())

    @unittest.skipUnless(os.name == 'nt', 'NTFS streams required')
    def test_alternate_stream_is_preserved_and_file_excluded(self):
        source = self.file()
        stream = Path(str(source) + ':fixture-stream')
        try:
            stream.write_bytes(b'important fixture metadata')
        except OSError as error:
            self.skipTest('Synthetic NTFS stream unsupported: ' + type(error).__name__)
        with self.assertRaises(ValueError):
            self.executor.plan_transfer([str(source)], str(self.dst))
        self.assertEqual(stream.read_bytes(), b'important fixture metadata')
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_changed_destination_blocks_undo_without_overwrite(self):
        source = self.file()
        job = self.execute_plan(source)
        target = self.dst / source.name
        target.write_bytes(b'edited after move')
        with self.assertRaises(ValueError):
            self.executor.undo(job)
        self.assertEqual(target.read_bytes(), b'edited after move')
        self.assertFalse(source.exists())

    def test_mcp_protocol_and_invalid_scope_use_only_fixture_data(self):
        values = {name: ([str(x) for x in value] if isinstance(value, list) else str(value))
                  for name, value in vars(self.paths).items() if name.isupper()}
        config = self.root / 'fixture-paths.json'
        config.write_text(json.dumps(values), encoding='utf-8')
        bootstrap = """import json,runpy,sys,types
from pathlib import Path
p=types.ModuleType('paths')
for key,value in json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')).items():
    setattr(p,key,[Path(x) for x in value] if isinstance(value,list) else Path(value))
sys.modules['paths']=p
sys.path.insert(0,sys.argv[1])
runpy.run_path(str(Path(sys.argv[1])/'mcp_server.py'),run_name='__main__')
"""
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'prepare_transfer', 'arguments': {'sources': [str(self.root / 'outside.txt')], 'destination': str(self.dst)}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'agent_apps_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'launch_agent_app', 'arguments': {'app': 'notepad'}}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {'name': 'prepare_browser_background', 'arguments': {'browser': 'edge', 'url': 'https://example.test/'}}},
            {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'prepare_power_action', 'arguments': {'action': 'restart'}}},
        ]
        env = dict(os.environ, LOCALAPPDATA=str(self.root / 'child-local-profile'), PYTHONPATH='', PYTHONHOME='')
        child = subprocess.run([sys.executable, '-B', '-c', bootstrap, str(PRODUCT / 'src'), str(config)],
                               input='\n'.join(json.dumps(x) for x in requests) + '\n',
                               text=True, encoding='utf-8', capture_output=True, env=env, timeout=20,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(child.stderr)
        responses = [json.loads(line) for line in child.stdout.splitlines()]
        self.assertEqual([row['id'] for row in responses], [1, 2, 3, 4, 5, 6, 7])
        tools = {item['name'] for item in responses[1]['result']['tools']}
        self.assertEqual(tools, {'computer_status', 'catalog_search', 'list_jobs', 'queue_inventory',
                                'queue_duplicates', 'prepare_transfer', 'prepare_undo',
                                'agent_apps_status', 'prepare_app_launch', 'launch_agent_app',
                                'prepare_browser_background', 'agent_browser_status', 'agent_browser_open',
                                'agent_browser_snapshot', 'agent_browser_click', 'agent_browser_fill',
                                'agent_browser_close', 'lock_computer', 'power_status', 'prepare_power_action',
                                'collect_maintenance_report', 'get_maintenance_report', 'submit_maintenance_advice'})
        self.assertTrue(responses[2]['result']['isError'])
        app_status = json.loads(responses[3]['result']['content'][0]['text'])
        self.assertTrue(app_status['this_call_starts_nothing'])
        self.assertFalse(app_status['existing_session_control_verified'])
        launch = json.loads(responses[4]['result']['content'][0]['text'])
        self.assertEqual(launch['status'], 'blocked')
        self.assertFalse(launch['started_anything'])
        background = json.loads(responses[5]['result']['content'][0]['text'])
        self.assertEqual(background['runtime_status'], 'planned_not_started')
        self.assertFalse(background['process_started'])
        self.assertFalse(background['controllable'])
        proposal = json.loads(responses[6]['result']['content'][0]['text'])
        self.assertFalse(proposal['scheduled'])
        self.assertEqual(proposal['status'], 'proposal_only')
        self.assertTrue((self.paths.EXECUTOR_DATA / 'queue.sqlite').is_file())
        self.assertFalse((self.root / 'child-local-profile').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
