import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import copy
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock
import uuid
import weakref
import zipfile

from engine import Executor, ScanLimitExceeded
from indexed_archives import ArchiveStore, _publish


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.src = self.root / 'source'
        self.dst = self.root / 'destination'
        self.src.mkdir()
        self.dst.mkdir()
        self.engine = Executor(self.root / 'state', [self.src, self.dst], self.root / 'unused.sqlite', [])
        self.store = ArchiveStore(self.engine, self.root / 'managed-archives')

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name='document.txt', data=b'important document\n' * 500):
        path = self.src / name
        path.write_bytes(data)
        return path

    def archive(self, files=None):
        plan = self.store.propose(files or [self.file()])
        return self.store.create(plan)

    def entries(self):
        return self.store.search()

    def test_unicode_empty_roundtrip_originals_preserved(self):
        first = self.file('Привет, Паша.txt', 'Русский текст 🌳\n'.encode('utf-8') * 1000)
        empty = self.file('empty.txt', b'')
        plan = self.store.propose([first, empty])
        self.assertLess(plan['estimated_archive_bytes'], plan['total_bytes'])
        metadata = self.store.create(plan)
        self.assertEqual(hashlib.sha256(Path(metadata['path']).read_bytes()).hexdigest(), metadata['sha256'])
        entries = self.store.search('ПРИВЕТ')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], first.name)
        results = self.store.restore([row['id'] for row in self.entries()], self.dst)
        self.assertEqual(len(results), 2)
        for source in (first, empty):
            self.assertEqual((self.dst / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(list(self.dst.glob('*.partial')), [])

    def test_folder_text_proposal_and_explicit_other_type(self):
        self.file('readme.md')
        picture = self.file('picture.jpg', b'image fixture')
        self.file('tool.exe', b'not a real executable')
        plan = self.store.propose(self.src)
        self.assertEqual([row['name'] for row in plan['files']], ['readme.md'])
        self.assertGreaterEqual(plan['skipped'], 1)
        self.assertEqual(self.store.propose([picture])['files'][0]['name'], 'picture.jpg')

    def test_folder_limit_has_no_partial_plan(self):
        for index in range(101):
            self.file(f'{index}.txt', b'')
        with self.assertRaises(ScanLimitExceeded):
            self.store.propose(self.src)
        self.assertEqual(list(self.store.objects.iterdir()), [])

    def test_selection_keeps_reviewed_hashes(self):
        first = self.file('first.txt')
        second = self.file('second.txt')
        plan = self.store.propose([first, second])
        selected = self.store.select(plan, [1])
        self.assertEqual(selected['files'][0]['sha256'], plan['files'][1]['sha256'])
        self.assertEqual(selected['files'][0]['member'], 'files/000/second.txt')
        self.store.create(selected)
        self.assertEqual([row['name'] for row in self.entries()], ['second.txt'])

    def test_stale_source_and_same_size_same_mtime_modification(self):
        source = self.file(data=b'original')
        plan = self.store.propose([source])
        stamp = source.stat()
        source.write_bytes(b'modified')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaises(ValueError):
            self.store.create(plan)
        self.assertEqual(source.read_bytes(), b'modified')
        self.assertEqual(list(self.store.objects.iterdir()), [])
        self.assertEqual(self.entries(), [])

    def test_idempotent_create_checks_existing_final_and_reindexes(self):
        plan = self.store.propose([self.file()])
        first = self.store.create(plan)
        self.assertEqual(self.store.create(plan), first)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(len(list(self.store.objects.iterdir())), 1)

    def test_failed_stream_cleans_partial_then_retry_works(self):
        source = self.file()
        plan = self.store.propose([source])
        with mock.patch('indexed_archives.os.fsync', side_effect=OSError('disk fixture failure')):
            with self.assertRaises(OSError):
                self.store.create(plan)
        self.assertTrue(source.exists())
        self.assertEqual(list(self.store.objects.iterdir()), [])
        self.store.create(plan)
        self.assertEqual(len(self.entries()), 1)

    def test_final_after_failed_index_commit_recovers(self):
        plan = self.store.propose([self.file()])
        with mock.patch.object(self.store, '_index', side_effect=sqlite3.OperationalError('commit fixture failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.create(plan)
        self.assertEqual(self.entries(), [])
        self.assertEqual(len(list(self.store.objects.glob('archive-*.zip'))), 1)
        result = self.store.recover()
        self.assertEqual(result, {'indexed_archives': 1, 'errors': []})
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.store.create(plan)['id'], plan['id'])

    def test_rebuild_catalog_from_manifest_and_ignore_partial(self):
        metadata = self.archive()
        with self.store._connect() as connection:
            connection.execute('DELETE FROM entries')
            connection.execute('DELETE FROM archives')
        (self.store.objects / '.archive-fixture.partial').write_bytes(b'interrupted')
        self.assertEqual(self.store.recover()['indexed_archives'], 1)
        self.assertEqual(self.entries()[0]['archive_id'], metadata['id'])
        self.assertEqual((self.store.objects / '.archive-fixture.partial').read_bytes(), b'interrupted')

    def test_corrupt_archive_rejected_before_restore(self):
        metadata = self.archive()
        Path(metadata['path']).write_bytes(b'corrupt fixture')
        with self.assertRaises(ValueError):
            self.store.restore([self.entries()[0]['id']], self.dst)
        self.assertEqual(list(self.dst.iterdir()), [])
        self.assertEqual(len(self.store.recover()['errors']), 1)

    def test_tampered_catalog_never_controls_zip_path_or_destination(self):
        self.archive()
        row = self.entries()[0]
        for column, value in [('name', '../escape.txt'), ('member', 'manifest.json'), ('sha256', '0' * 64)]:
            with self.subTest(column=column):
                with self.store._connect() as connection:
                    connection.execute(f'UPDATE entries SET {column}=? WHERE id=?', (value, row['id']))
                with self.assertRaises(ValueError):
                    self.store.restore([row['id']], self.dst)
                self.assertEqual(list(self.dst.iterdir()), [])
                self.store.recover()

    def test_existing_destination_collision_preserved(self):
        self.archive()
        target = self.dst / self.entries()[0]['name']
        target.write_bytes(b'keep existing')
        with self.assertRaises(ValueError):
            self.store.restore([self.entries()[0]['id']], self.dst)
        self.assertEqual(target.read_bytes(), b'keep existing')

    def test_late_collision_preserved_and_partial_removed(self):
        self.archive()
        row = self.entries()[0]

        def collision(partial, final):
            final.write_bytes(b'new competing file')
            return _publish(partial, final)

        with mock.patch('indexed_archives._publish', side_effect=collision):
            with self.assertRaises(OSError):
                self.store.restore([row['id']], self.dst)
        self.assertEqual((self.dst / row['name']).read_bytes(), b'new competing file')
        self.assertEqual(len(list(self.dst.iterdir())), 1)

    def test_restore_failure_rolls_back_only_new_files(self):
        files = [self.file('first.txt'), self.file('second.txt')]
        self.archive(files)
        existing = self.dst / 'keep.txt'
        existing.write_bytes(b'keep')
        original = os.fsync
        calls = 0

        def fail_second(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('second file fixture failure')
            return original(descriptor)

        with mock.patch('indexed_archives.os.fsync', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.store.restore([row['id'] for row in self.entries()], self.dst)
        self.assertEqual(list(self.dst.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b'keep')
        self.assertTrue(all(path.exists() for path in files))

    def test_free_space_checks_do_not_remove_sources(self):
        source = self.file()
        plan = self.store.propose([source])
        with mock.patch('indexed_archives.shutil.disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(ValueError):
                self.store.create(plan)
        self.assertTrue(source.exists())
        self.assertEqual(list(self.store.objects.iterdir()), [])

    def test_size_limit_checked_without_allocating_large_files(self):
        source = self.file(data=b'123456')
        with mock.patch('indexed_archives.MAX_BYTES', 5):
            with self.assertRaises(ValueError):
                self.store.propose([source])

    def test_hardlinked_source_is_refused(self):
        source = self.file()
        os.link(source, self.src / 'hardlink.txt')
        with self.assertRaises(ValueError):
            self.store.propose([source])

    def test_unallowed_paths_and_executable_refused(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'outside')
        for source in (outside, self.file('program.exe')):
            with self.assertRaises(ValueError):
                self.store.propose([source])
        self.archive()
        with self.assertRaises(ValueError):
            self.store.restore([self.entries()[0]['id']], self.root)

    def test_two_identical_basenames_refused_on_restore(self):
        source = self.file()
        other = self.src / 'subfolder'
        other.mkdir()
        duplicate = other / source.name
        duplicate.write_bytes(b'other file')
        self.archive([source, duplicate])
        with self.assertRaises(ValueError):
            self.store.restore([row['id'] for row in self.entries()], self.dst)
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_malicious_own_folder_zip_names_symlinks_and_extra_members(self):
        plan = self.store.propose([self.file()])
        for variant in ('traversal', 'duplicate', 'symlink', 'extra', 'oversized', 'bad-hash'):
            with self.subTest(variant=variant):
                candidate = copy.deepcopy(plan)
                candidate['id'] = uuid.uuid4().hex
                manifest = self.store._manifest(candidate)
                row = manifest['files'][0]
                data = Path(row['source']).read_bytes()
                if variant == 'traversal':
                    row['member'] = '../escape.txt'
                if variant == 'oversized':
                    row['size'] = 3 * 1024 ** 3
                if variant == 'bad-hash':
                    row['sha256'] = '0' * 64
                path = self.store.objects / f"archive-{candidate['id']}.zip"
                with zipfile.ZipFile(path, 'w') as archive:
                    info = zipfile.ZipInfo(row['member'])
                    if variant == 'symlink':
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, data)
                    if variant == 'duplicate':
                        archive.writestr(row['member'].upper(), data)
                    if variant == 'extra':
                        archive.writestr('unexpected.txt', b'extra')
                    archive.writestr('manifest.json', json.dumps(manifest).encode('utf-8'))
                self.assertGreaterEqual(len(self.store.recover()['errors']), 1)
                self.assertEqual(self.entries(), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows directory handles')
    def test_destination_parent_cannot_be_swapped_during_restore(self):
        self.archive()
        attempts = []

        def check_directory_lock(partial, final):
            with self.assertRaises(OSError):
                self.dst.rename(self.root / 'renamed-destination')
            attempts.append(True)
            return _publish(partial, final)

        with mock.patch('indexed_archives._publish', side_effect=check_directory_lock):
            restored = self.store.restore([self.entries()[0]['id']], self.dst)
        self.assertEqual(attempts, [True])
        self.assertEqual(len(restored), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows source locking')
    def test_open_source_prevents_writer(self):
        source = self.file()
        with self.store._source(source):
            with self.assertRaises(OSError):
                source.open('r+b')

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate streams')
    def test_ntfs_alternate_stream_refused(self):
        source = self.file()
        Path(str(source) + ':fixture').write_bytes(b'metadata must not be lost')
        with self.assertRaises(ValueError):
            self.store.propose([source])

    def test_corrupt_index_panel_keeps_other_tabs_usable_and_preserves_database(self):
        import tkinter as tk
        from tkinter import ttk
        from archive_ui import ArchivePanel

        broken = self.root / 'corrupt-archive-store'
        broken.mkdir()
        database = broken / 'index.sqlite'
        original = b'corrupt database fixture; preserve for recovery'
        database.write_bytes(original)
        window = tk.Tk()
        window.withdraw()
        try:
            tabs = ttk.Notebook(window)
            tabs.pack()
            archive = ArchivePanel(tabs, self.engine, store_root=broken)
            tabs.add(archive, text='Archives')
            other = ttk.Frame(tabs)
            tabs.add(other, text='Study')
            invoked = []
            button = ttk.Button(other, text='Other tab action', command=lambda: invoked.append(True))
            button.pack()
            tabs.select(other)
            button.invoke()
            window.update()
            self.assertEqual(invoked, [True])
            self.assertEqual(tabs.select(), str(other))
            self.assertIsNone(archive.store)
            self.assertTrue(archive.init_error)
            self.assertIn('недоступны', archive.notice.get())
            self.assertFalse(archive.is_busy())
            self.assertIsNone(archive.timer)
            self.assertEqual(archive.buttons, [])
            self.assertEqual(database.read_bytes(), original)
            self.assertEqual(list((broken / 'objects').iterdir()), [])
        finally:
            window.destroy()
            button = other = archive = tabs = window = None
            gc.collect()

    def test_destroyed_archive_panel_releases_ui_callbacks_before_worker_finishes(self):
        import tkinter as tk
        from archive_ui import ArchivePanel
        entered, release, finished = (threading.Event() for _ in range(3))
        def blocked_model_work():
            entered.set()
            if not release.wait(4):
                raise RuntimeError('fixture timeout')
            gc.collect()
            finished.set()
            return {'fixture': True}
        window = tk.Tk()
        window.withdraw()
        panel = ArchivePanel(window, self.engine, store_root=self.root / 'ui-archive-store')
        thread = None
        retained_panel = None
        try:
            thread = panel._task(blocked_model_work, panel._show_plan, 'Fixture blocked work')
            self.assertTrue(entered.wait(2))
            self.assertEqual(len(panel._callbacks), 1)
            reference = weakref.ref(panel)
            window.destroy()
            self.assertEqual(panel._callbacks, {})
            panel = window = None
            gc.collect()
            retained_panel = reference()
            self.assertIsNone(retained_panel, 'Worker/callback retained the destroyed Tk panel')
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            if thread is not None:
                thread.join(4)
            if window is not None:
                window.destroy()
            panel = window = retained_panel = None
            gc.collect()
        self.assertTrue(finished.is_set())
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
