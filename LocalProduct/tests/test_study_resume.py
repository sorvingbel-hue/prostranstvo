import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Finite deadline and startup tests using synthetic files and a fake registry."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from study_resume import RUN_KEY, StudyResume


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Registry:
    HKEY_CURRENT_USER = 'synthetic-user'
    REG_SZ = 1
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1

    def __init__(self):
        self.values = {}
        self.writes = []

    def OpenKey(self, hive, key, *args):
        assert hive == self.HKEY_CURRENT_USER and key == RUN_KEY
        return contextlib.nullcontext('synthetic-key')

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes.append(('set', name))
        self.values[name] = value, kind

    def DeleteValue(self, key, name):
        self.writes.append(('delete', name))
        del self.values[name]


class StudyResumeTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parent / 'tests-work'
        directory.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='study-resume-', dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'uncreated'
        self.clock = Clock()
        self.registry = Registry()
        self.resume = StudyResume(self.root, self.clock, self.registry)

    def test_read_does_not_create_files(self):
        self.assertIsNone(self.resume.read())
        self.assertEqual(self.resume.remaining_seconds(), 0)
        self.assertFalse(self.resume.clear('a' * 32))
        self.assertFalse(self.root.exists())

    def test_activation_saves_one_fixed_deadline(self):
        value = self.resume.activate(60)
        self.assertEqual(value, self.resume.read())
        self.assertEqual(value['expires_utc'], 4600)
        self.assertEqual(value['duration_seconds'], 3600)
        self.assertEqual(len(value['session_id']), 32)
        self.assertEqual(self.resume.remaining_seconds(), 3600)
        self.assertEqual(list(self.root.glob('.study-session-*.tmp')), [])

    def test_recreated_object_uses_original_deadline(self):
        value = self.resume.activate(60)
        self.clock.now += 1200
        after_reboot = StudyResume(self.root, self.clock, self.registry)
        self.assertEqual(after_reboot.remaining_seconds(), 2400)
        self.assertEqual(after_reboot.read()['expires_utc'], value['expires_utc'])

    def test_expired_lease_stays_expired_without_new_activation(self):
        value = self.resume.activate(60)
        self.clock.now = value['expires_utc'] + 100
        self.assertEqual(StudyResume(self.root, self.clock).remaining_seconds(), 0)
        self.assertEqual(self.resume.read(), value)

    def test_clock_rollback_is_capped_by_original_duration(self):
        self.resume.activate(30)
        self.clock.now = 100
        self.assertEqual(self.resume.remaining_seconds(), 1800)

    def test_fractional_seconds_round_up(self):
        value = self.resume.activate(1)
        self.clock.now = value['expires_utc'] - .1
        self.assertEqual(self.resume.remaining_seconds(), 1)
        self.clock.now = value['expires_utc']
        self.assertEqual(self.resume.remaining_seconds(), 0)

    def test_older_session_cannot_clear_replacement(self):
        old = self.resume.activate(30)
        new = self.resume.activate(60)
        self.assertNotEqual(old['session_id'], new['session_id'])
        self.assertFalse(self.resume.clear(old['session_id']))
        self.assertEqual(self.resume.read(), new)
        self.assertTrue(self.resume.clear(new['session_id']))
        self.assertIsNone(self.resume.read())

    def test_failed_replace_preserves_previous_deadline(self):
        old = self.resume.activate(30)
        with mock.patch('study_resume.os.replace', side_effect=OSError('synthetic fault')):
            with self.assertRaises(OSError):
                self.resume.activate(60)
        self.assertEqual(self.resume.read(), old)
        self.assertEqual(list(self.root.glob('.study-session-*.tmp')), [])

    def test_invalid_duration_does_not_replace_existing_lease(self):
        old = self.resume.activate(30)
        for minutes in (True, 0, 241, 1.5, '60'):
            with self.subTest(minutes=minutes):
                with self.assertRaises(ValueError):
                    self.resume.activate(minutes)
        self.assertEqual(self.resume.read(), old)

    def test_malformed_data_is_rejected(self):
        old = self.resume.activate(30)
        invalid = [[], None, {**old, 'version': True}, {**old, 'session_id': 'path'},
                   {**old, 'expires_utc': float('nan')}, {**old, 'expires_utc': float('inf')},
                   {**old, 'expires_utc': True}, {**old, 'duration_seconds': 999999},
                   {**old, 'duration_seconds': False}, {**old, 'unexpected': 1}]
        for value in invalid:
            with self.subTest(value=value):
                self.resume.path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.resume.read()
        self.resume.path.write_text('x' * 2049, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.resume.read()

    def test_invalid_clock_does_not_write_activation(self):
        for now in (float('nan'), float('inf'), 0, -1, True):
            self.clock.now = now
            with self.assertRaises(ValueError):
                self.resume.activate(60)
        self.assertFalse(self.root.exists())

    def test_only_owned_startup_entry_is_installed_and_removed(self):
        command = '"C:\\Synthetic\\pythonw.exe" -B "C:\\Synthetic\\study_runtime.py" --resume'
        self.registry.values['UnrelatedEntry'] = ('keep', self.registry.REG_SZ)
        with mock.patch.object(self.resume, '_startup_command', return_value=command):
            self.assertFalse(self.resume.startup_enabled())
            self.resume.enable_startup()
            self.assertTrue(self.resume.startup_enabled())
            self.resume.disable_startup()
            self.assertFalse(self.resume.startup_enabled())
        self.assertEqual(self.registry.values, {'UnrelatedEntry': ('keep', self.registry.REG_SZ)})
        self.assertFalse(self.root.exists())

    def test_foreign_command_or_registry_kind_is_preserved(self):
        for existing in [('foreign command', 1), ('ours', 99), (None, 0)]:
            with self.subTest(existing=existing), mock.patch.object(self.resume, '_startup_command', return_value='ours'):
                self.registry.values[self.resume.startup_name] = existing
                self.assertFalse(self.resume.startup_enabled())
                with self.assertRaises(ValueError):
                    self.resume.enable_startup()
                self.resume.disable_startup()
                self.assertEqual(self.registry.values[self.resume.startup_name], existing)
        self.assertEqual(self.registry.writes, [])

    def test_read_link_guard_is_enforced(self):
        with mock.patch('study_resume.no_links', side_effect=ValueError('synthetic junction')):
            with self.assertRaises(ValueError):
                self.resume.read()


if __name__ == '__main__':
    unittest.main()
