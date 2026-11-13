"""Isolated beta data tests: no desktop, real history, service, network or D drive."""
import contextlib
import datetime
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock


PRODUCT = Path(__file__).resolve().parents[1]
MODULES = ('engine', 'activity_core', 'browser_history', 'history_schedule', 'system_broker')


@contextlib.contextmanager
def fixture_modules(root):
    """Load the source using the documented paths contract in a synthetic profile."""
    data = root / 'local-profile' / 'ProstranstvoLocalBeta' / 'data'
    app = root / 'installed-app'
    folders = [root / 'Documents', root / 'Downloads']
    paths = types.ModuleType('paths')
    paths.APP_ROOT = app
    paths.DATA_ROOT = data
    paths.BASE_ROOT = data.parent
    paths.DEPLOY_ROOT = paths.BASE_ROOT
    paths.SOURCE_ROOT = app
    paths.EXECUTOR_DATA = data / 'executor'
    paths.MAINTENANCE_ROOT = data / 'maintenance'
    paths.CATALOG_PATH = data / 'catalog.sqlite'
    paths.DEFAULT_ALLOWED_ROOTS = folders
    paths.DEFAULT_BLOCKED_ROOTS = [app, data, root / '.codex', root / '.ssh', root / '.aws']
    for folder in [app, *folders]:
        folder.mkdir(parents=True)
    with mock.patch.dict(sys.modules), mock.patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'browser-profile')}), mock.patch.object(sys, 'path', [str(PRODUCT / 'src'), *sys.path]):
        for name in MODULES:
            sys.modules.pop(name, None)
        sys.modules['paths'] = paths
        loaded = {name: importlib.import_module(name) for name in MODULES}
        yield types.SimpleNamespace(paths=paths, **loaded)


class PortableDataTests(unittest.TestCase):
    def setUp(self):
        # Keep all synthetic data inside the explicitly writable workspace.
        temporary = PRODUCT / 'build' / 'test-temporary'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='portable-', dir=temporary)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.context = fixture_modules(self.root)
        self.modules = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def test_clean_profile_is_local_and_optional_features_stay_disabled(self):
        m = self.modules
        executor = m.engine.Executor()
        executor.recover()
        with mock.patch.object(m.engine, 'drive_usage', return_value=[]):
            status = executor.status()
        self.assertEqual(executor.data, m.paths.EXECUTOR_DATA)
        self.assertEqual(executor.roots, m.paths.DEFAULT_ALLOWED_ROOTS)
        self.assertEqual(executor.blocked, m.paths.DEFAULT_BLOCKED_ROOTS)
        self.assertEqual(executor.catalog('anything'), [])
        self.assertFalse(status['catalog_exists'])
        self.assertFalse(m.paths.CATALOG_PATH.exists())
        self.assertFalse(status['system_service']['available'])
        activity = m.activity_core.ActivityStore()
        archive = m.history_schedule.HistoryArchive()
        self.assertEqual(activity.base, m.paths.DATA_ROOT / 'activity')
        self.assertEqual(archive.base, m.paths.DATA_ROOT / 'browser-history')
        self.assertFalse(activity.config()['enabled'])
        self.assertFalse(archive.config()['enabled'])
        self.assertEqual(activity.recent_captures(), [])
        with mock.patch.object(m.history_schedule, 'helper', side_effect=AssertionError('No task should run')):
            self.assertEqual(archive.collect(), {'skipped': 'disabled'})
        self.assertEqual(list(m.paths.APP_ROOT.iterdir()), [])

    def test_drive_status_includes_real_non_cd_drives_and_skips_unavailable_volume(self):
        m = self.modules
        usage = types.SimpleNamespace(free=120, total=200)
        with mock.patch.object(m.engine.os, 'listdrives', return_value=['C:\\', 'F:\\', 'Z:\\'], create=True), mock.patch.object(m.engine.shutil, 'disk_usage', side_effect=[usage, usage, OSError('media absent')]) as disk:
            self.assertEqual(m.engine.drive_usage(), [
                {'drive': 'C:\\', 'free': 120, 'total': 200},
                {'drive': 'F:\\', 'free': 120, 'total': 200},
            ])
        self.assertEqual(disk.call_count, 3)

    def test_service_stub_never_invokes_process_or_service_api(self):
        m = self.modules
        with mock.patch('subprocess.run', side_effect=AssertionError('Unexpected external process')):
            self.assertFalse(m.system_broker.status()['installed'])
            self.assertEqual(m.system_broker.status()['state'], 'not_included')
            for command in ('health', 'cleanup', 'anything'):
                with self.assertRaisesRegex(RuntimeError, 'SYSTEM'):
                    m.system_broker.request(command)
        self.assertNotIn('BINARY', vars(m.system_broker))

    def test_sensitive_locations_and_files_remain_protected(self):
        m = self.modules
        executor = m.engine.Executor(roots=[self.root])
        for path in [m.paths.APP_ROOT / 'notes.txt', m.paths.DATA_ROOT / 'notes.txt', self.root / '.codex' / 'notes.txt', self.root / 'Documents' / 'secret-notes.txt', self.root / 'Documents' / 'payload.exe', self.root / 'Documents' / '.minecraft' / 'world.txt']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                executor.checked(path)
        safe = self.root / 'Documents' / 'regular-note.txt'
        safe.write_text('fixture', encoding='utf-8')
        self.assertEqual(executor.checked(safe), safe)

    def test_verified_copy_works_without_any_catalog(self):
        m = self.modules
        source = m.paths.DEFAULT_ALLOWED_ROOTS[0] / 'example.txt'
        source.write_bytes(b'A portable fixture\r\n')
        destination = m.paths.DEFAULT_ALLOWED_ROOTS[1]
        executor = m.engine.Executor()
        job = executor.plan_transfer([str(source)], str(destination), 'copy')
        planned = executor.get(job)
        self.assertEqual(planned['status'], 'needs_approval')
        self.assertIsNone(planned['payload']['records'][0]['index_id'])
        executor.approve(job)
        completed = executor.execute(job)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['result']['verified_files'], 1)
        self.assertEqual((destination / source.name).read_bytes(), source.read_bytes())
        self.assertFalse(m.paths.CATALOG_PATH.exists())

    def test_missing_yandex_profile_reports_unavailable_without_creating_it(self):
        m = self.modules
        expected = self.root / 'browser-profile' / 'Yandex' / 'YandexBrowser' / 'User Data'
        self.assertEqual(m.browser_history.PROFILE_ROOT, expected)
        self.assertEqual(m.browser_history.WORKSPACE, m.paths.DATA_ROOT / 'browser-history')
        with self.assertRaisesRegex(RuntimeError, 'не найден'):
            m.browser_history.read_history()
        self.assertFalse(expected.exists())
        self.assertFalse(m.browser_history.WORKSPACE.exists())

    def test_synthetic_browser_history_is_copied_into_private_archive(self):
        m = self.modules
        profile = m.browser_history.PROFILE_ROOT / 'Default'
        profile.mkdir(parents=True)
        history = profile / 'History'
        raw_time = int((datetime.datetime.now(datetime.timezone.utc) - m.browser_history.EPOCH).total_seconds() * 1_000_000)
        with contextlib.closing(sqlite3.connect(history)) as db, db:
            db.executescript('CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);')
            db.execute('INSERT INTO urls VALUES(?,?,?)', (1, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=fixture', 'Example clip'))
            db.execute('INSERT INTO visits VALUES(?,?,?)', (9, 1, raw_time))
        # An unrelated sentinel demonstrates that only History/transaction sidecars are used.
        unrelated = profile / 'unrelated-fixture.txt'
        unrelated.write_bytes(b'leave this fixture alone')
        before = history.read_bytes()
        result = m.browser_history.read_history()
        self.assertEqual(result['returned'], 1)
        self.assertEqual(result['youtube'][0]['url'], 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(unrelated.read_bytes(), b'leave this fixture alone')
        archive = m.history_schedule.HistoryArchive()
        state = archive.collect(force=True, reader=lambda **kwargs: result)
        self.assertEqual(state['phase'], 'completed')
        self.assertEqual(state['new_records'], 1)
        self.assertEqual(archive.report()['returned'], 1)
        self.assertTrue(Path(state['report']).is_relative_to(m.paths.DATA_ROOT))
        self.assertFalse(archive.config()['enabled'])
        self.assertEqual(list((archive.base / '_tmp').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
