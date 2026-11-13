import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Synthetic folder permissions and bounded read-only inventory tests."""
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import engine
from folder_policy import FolderPolicy, MAX_POLICY_BYTES, contains


class FolderPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).parent / 'tests-work'
        temporary.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='folders-', dir=temporary)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.documents = self.root / 'Documents'
        self.downloads = self.root / 'Downloads'
        self.app = self.documents / 'installed-app'
        self.state = self.root / 'state'
        self.other = self.root / 'Other'
        self.index = self.root / 'catalog' / 'catalog.sqlite'
        for folder in (self.documents, self.downloads, self.app, self.state, self.other, self.index.parent):
            folder.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.multiple(engine, ROOTS=[self.documents, self.downloads], BLOCKED=[self.app], BASE=self.app, INDEX=self.index)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.executor = engine.Executor(data=self.state)

    def policy(self):
        return self.executor._folder_policy

    def test_legacy_defaults_do_not_create_policy_or_touch_catalog(self):
        self.assertEqual(self.executor.allowed_roots(), [str(self.documents), str(self.downloads)])
        self.assertFalse(self.policy().path.exists())
        self.assertFalse(self.index.exists())
        self.executor.checked(self.documents / 'regular.txt')
        with self.assertRaises(ValueError):
            self.executor.checked(self.app / 'regular.txt')

    def test_policy_constructor_has_no_filesystem_side_effect(self):
        path = self.root / 'not-created' / 'folders.json'
        FolderPolicy(path, [self.documents], [self.app])
        self.assertFalse(path.parent.exists())

    def test_add_existing_local_folder_persists_across_executors(self):
        roots = [str(self.documents), str(self.downloads), str(self.other)]
        self.assertEqual(self.executor.set_allowed_roots(roots), roots)
        other = engine.Executor(data=self.state)
        self.assertEqual(other.allowed_roots(), roots)
        self.assertEqual(other.checked(self.other / 'notes.txt'), self.other / 'notes.txt')
        value = json.loads(self.policy().path.read_text(encoding='utf-8'))
        self.assertEqual(value, {'version': 1, 'roots': roots})
        self.assertEqual(list(self.state.glob('.folders-*')), [])

    def test_revocation_reloads_in_existing_other_process_instance(self):
        other = engine.Executor(data=self.state)
        self.assertEqual(other.checked(self.downloads / 'notes.txt'), self.downloads / 'notes.txt')
        self.executor.set_allowed_roots([str(self.documents)])
        with self.assertRaisesRegex(ValueError, 'вне разрешённых'):
            other.checked(self.downloads / 'notes.txt')
        self.executor.set_allowed_roots([])
        self.assertEqual(other.allowed_roots(), [])
        with self.assertRaises(ValueError):
            other.checked(self.documents / 'notes.txt')

    def test_status_reloads_permissions(self):
        other = engine.Executor(data=self.state)
        self.executor.set_allowed_roots([])
        with mock.patch.dict('sys.modules', {'system_broker': types.SimpleNamespace(status=lambda: {})}), mock.patch.object(engine.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=1, total=2)):
            self.assertEqual(other.status()['roots'], [])

    def test_custom_roots_ignore_persisted_policy(self):
        self.policy().path.write_text('bad json', encoding='utf-8')
        explicit = engine.Executor(self.root / 'custom-state', roots=[self.other], index=self.index, blocked=[])
        self.assertEqual(explicit.allowed_roots(), [str(self.other)])
        self.assertEqual(explicit.checked(self.other / 'note.txt'), self.other / 'note.txt')
        with self.assertRaisesRegex(ValueError, 'при его создании'):
            explicit.set_allowed_roots([])

    def test_does_not_create_missing_folder(self):
        missing = self.root / 'missing'
        with self.assertRaisesRegex(ValueError, 'должна существовать'):
            self.executor.set_allowed_roots([str(missing)])
        self.assertFalse(missing.exists())

    def test_offline_existing_root_can_be_revoked(self):
        self.executor.set_allowed_roots([str(self.other), str(self.downloads)])
        self.other.rmdir()
        self.assertEqual(self.executor.set_allowed_roots([str(self.other)]), [str(self.other)])
        self.assertEqual(self.executor.set_allowed_roots([]), [])

    def test_save_cannot_race_running_executor(self):
        other = engine.Executor(data=self.state)
        with self.executor.lock(), self.assertRaises(engine.Busy):
            other.set_allowed_roots([])
        self.assertFalse(self.policy().path.exists())

    def test_bad_root_shapes_do_not_replace_valid_policy(self):
        self.executor.set_allowed_roots([str(self.documents)])
        before = self.policy().path.read_bytes()
        cases = [[None], ['relative'], [str(self.other), str(self.other)], 'all', [str(self.other)] * 33]
        for roots in cases:
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                self.executor.set_allowed_roots(roots)
        self.assertEqual(self.policy().path.read_bytes(), before)

    def test_rejects_disk_profile_app_data_catalog_and_ancestors(self):
        for path in [Path(self.root.anchor), Path.home(), self.root, self.app, self.app / 'child', self.state, self.index.parent, self.documents.parent]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.executor.set_allowed_roots([str(path)])
        # A legacy broad Documents grant still excludes the installed app.
        self.assertEqual(self.executor.set_allowed_roots([str(self.documents)]), [str(self.documents)])

    def test_rejects_cloud_system_secret_and_world_folders(self):
        for name in ['AppData', 'Windows', 'ProgramData', 'OneDrive', 'OneDrive - Personal', 'Google Drive', 'Dropbox', '.aws', '.minecraft', 'saves', 'worlds', '.env-data', 'credentials', 'secret-notes']:
            path = self.other / name
            path.mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.executor.set_allowed_roots([str(path)])

    def test_windows_case_dot_segments_trailing_separator_and_component_boundary(self):
        self.assertTrue(contains(str(self.documents).upper(), str(self.documents / 'child')))
        self.assertTrue(contains(str(self.documents) + os.sep, self.documents / 'child'))
        self.assertTrue(contains(self.documents, self.documents / 'child' / '..' / 'notes.txt'))
        self.assertFalse(contains(self.documents, self.root / 'Documents-other'))
        roots = self.executor.set_allowed_roots([str(self.other / '..' / 'Other') + os.sep])
        self.assertEqual(roots, [str(self.other)])
        with self.assertRaises(ValueError):
            self.executor.set_allowed_roots([str(self.other), str(self.other).upper()])

    def test_network_and_non_cd_drive_grants_are_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows drive policy')
        for value in [r'\\server\share\folder', r'E:\Folder', r'\\?\C:\Folder']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.executor.set_allowed_roots([value])

    def test_mapped_network_volume_is_rejected(self):
        with mock.patch('folder_policy.local_volume', return_value=False), self.assertRaisesRegex(ValueError, 'локальном диске'):
            self.executor.set_allowed_roots([str(self.other)])

    def test_corrupt_and_unknown_policy_fail_closed(self):
        for value in ['bad json', '{}', '{"version":true,"roots":[]}', '{"version":2,"roots":[]}', '{"version":1,"roots":[false]}', '{"version":1,"roots":[],"allow_all":true}']:
            self.policy().path.write_text(value, encoding='utf-8')
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.executor.checked(self.documents / 'note.txt')
        self.policy().path.write_text(' ' * (MAX_POLICY_BYTES + 1), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'слишком большой'):
            self.executor.allowed_roots()

    def test_policy_hardlink_is_rejected_without_modifying_other_file(self):
        original = self.other / 'original.json'
        original.write_text('{"version":1,"roots":[]}', encoding='utf-8')
        os.link(original, self.policy().path)
        with self.assertRaisesRegex(ValueError, 'hardlink'):
            self.executor.allowed_roots()
        with self.assertRaises(ValueError):
            self.executor.set_allowed_roots([])
        self.assertEqual(original.read_text(encoding='utf-8'), '{"version":1,"roots":[]}')

    def test_executor_lock_hardlink_is_rejected_before_write(self):
        original = self.other / 'precious.txt'
        original.write_bytes(b'precious')
        os.link(original, self.state / 'executor.lock')
        with self.assertRaisesRegex(ValueError, 'hardlink'):
            self.executor.set_allowed_roots([])
        self.assertEqual(original.read_bytes(), b'precious')

    def test_mock_reparse_grant_is_rejected(self):
        original_lstat = Path.lstat
        def lstat(path):
            result = original_lstat(path)
            if path == self.other:
                return types.SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode, st_nlink=result.st_nlink)
            return result
        with mock.patch.object(Path, 'lstat', lstat), self.assertRaisesRegex(ValueError, 'junction'):
            self.executor.set_allowed_roots([str(self.other)])


class BoundedScanTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).parent / 'tests-work'
        temporary.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='bounded-', dir=temporary)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.executor = engine.Executor(self.root / 'state', roots=[self.source], index=self.root / 'missing.sqlite', blocked=[])

    def file(self, name, text='abc'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_read_only_scan_without_job_preserves_fingerprints(self):
        path = self.file('folder/note.txt')
        before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        rows, skipped = self.executor.scan(self.source, max_files=1)
        self.assertEqual(rows, [dict(path=str(path), **engine.fingerprint(path))])
        self.assertEqual(skipped, 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertEqual(self.executor.jobs(), [])

    def test_limit_raises_instead_of_returning_partial_inventory(self):
        self.file('first.txt')
        self.file('second.txt')
        with self.assertRaises(engine.ScanLimitExceeded):
            self.executor.scan(self.source, max_files=1)
        self.assertEqual(len(self.executor.scan(self.source, max_files=2)[0]), 2)

    def test_invalid_limits_are_rejected(self):
        for value in [False, 0, -1, 100001, '2', 1.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.executor.scan(self.source, max_files=value)

    def test_unsafe_files_and_folders_are_excluded_from_bound(self):
        self.file('safe.txt')
        self.file('payload.exe')
        self.file('secret-notes.txt')
        self.file('worlds/level.txt')
        rows, skipped = self.executor.scan(self.source, max_files=1)
        self.assertEqual([Path(row['path']).name for row in rows], ['safe.txt'])
        self.assertEqual(skipped, 3)

    def test_legacy_job_cancellation_still_works(self):
        job = self.executor.create('inventory', {'root': str(self.source)})
        self.executor.cancel(job)
        with self.assertRaises(InterruptedError):
            self.executor.scan(self.source, job)

    def test_nonfolder_rejected(self):
        with self.assertRaisesRegex(ValueError, 'существующая папка'):
            self.executor.scan(self.source / 'missing', max_files=1)

    def test_job_limit_reports_failure(self):
        self.file('one.txt')
        self.file('two.txt')
        job = self.executor.create('inventory', {'root': str(self.source), 'max_files': 1})
        with self.assertRaises(engine.ScanLimitExceeded):
            self.executor.execute(job)
        self.assertEqual(self.executor.get(job)['status'], 'failed')
        self.assertFalse((self.executor.data / (job + '.json')).exists())

    def test_exact_inventory_does_not_rescan_unselected_files(self):
        selected = self.file('selected.txt')
        self.file('unselected.txt')
        record = dict(path=str(selected), **engine.fingerprint(selected))
        job = self.executor.create('inventory', {'root': str(self.source), 'rule_records': [record]})
        with mock.patch.object(self.executor, 'scan', side_effect=AssertionError('No rescan')):
            result = self.executor.execute(job)['result']
        self.assertEqual(result['files'], 1)
        self.assertEqual(result['largest'], [record])

    def test_exact_inventory_rechecks_changes_and_schema(self):
        selected = self.file('selected.txt')
        record = dict(path=str(selected), **engine.fingerprint(selected))
        cases = [[], [record] * 101, [{**record, 'extra': True}], [{**record, 'size': True}], [record, record], [{**record, 'path': str(self.root / 'outside.txt')}]]
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.executor.inventory_records(self.source, records)
        selected.write_text('changed size', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'изменён после'):
            self.executor.inventory_records(self.source, [record])


if __name__ == '__main__':
    unittest.main()
