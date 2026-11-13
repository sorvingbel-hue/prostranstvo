import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Hidden Tk fixtures with invented family facts and mocked OS opening only."""
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from family_context import ChatLog, FamilyContext, empty_profile
from family_context_ui import FamilyProfileWindow


class FamilyProfileWindowTests(unittest.TestCase):
    def setUp(self):
        # Collect previous destroyed Tk fixtures on the GUI thread before a
        # later background worker can trigger Python's cyclic collector.
        gc.collect()
        base = Path(__file__).resolve().parent / 'data' / 'tests-family-context-ui'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='fixture-', dir=base)
        self.directory = Path(self.temporary.name)
        self.context = FamilyContext(self.directory / 'profile')
        self.log = ChatLog(self.directory / 'history')
        self.original = empty_profile()
        self.original['child'].update(name='Тестовый ребёнок', aliases=['FixtureKid', 'Тестик'], age=11,
                                      grade='5Б', school='Тестовая школа')
        self.original['mother'].update(name='Тестовая мама', age=43)
        self.original['father'].update(name='Тестовый папа', occupation='Тестовое занятие')
        self.context.save(self.original)
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = FamilyProfileWindow(self.root, context=self.context, log=self.log)
        self.window.withdraw()
        self.root.update()

    def tearDown(self):
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.root = None
        gc.collect()
        self.temporary.cleanup()

    def pump_idle(self, seconds=4):
        deadline = time.monotonic() + seconds
        while self.window.body._busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.window.body._busy, 'Fixture background operation timed out')

    def test_initial_load_preserves_known_values_and_unknown_blank_fields(self):
        fields = self.window.fields
        self.assertEqual(fields[('child', 'name')].get(), 'Тестовый ребёнок')
        self.assertEqual(fields[('child', 'aliases')].get(), 'FixtureKid, Тестик')
        self.assertEqual(fields[('child', 'age')].get(), '11')
        self.assertEqual(fields[('father', 'age')].get(), '')
        self.assertFalse(self.log.root.exists())

    def test_form_parses_trimmed_aliases_age_and_blank_unknown(self):
        fields = self.window.fields
        fields[('child', 'aliases')].set(' One, Two , , Три ')
        fields[('child', 'age')].set(' 12 ')
        fields[('mother', 'age')].set('')
        fields[('child', 'school')].set('')
        profile = self.window.form()
        self.assertEqual(profile['child']['aliases'], ['One', 'Two', 'Три'])
        self.assertEqual(profile['child']['age'], 12)
        self.assertIsNone(profile['mother']['age'])
        self.assertIsNone(profile['child']['school'])
        self.assertEqual(self.context.profile()['child']['age'], 11)

    def test_save_uses_worker_without_gui_access_and_updates_form_on_main_thread(self):
        fields = self.window.fields
        fields[('child', 'name')].set('Новое тестовое имя')
        fields[('child', 'age')].set('12')
        fields[('child', 'aliases')].set('FixtureA, FixtureB')
        thread_ids = []
        original_save = self.context.save
        main = threading.get_ident()
        def save(profile):
            thread_ids.append(threading.get_ident())
            return original_save(profile)
        with mock.patch.object(self.context, 'save', side_effect=save):
            self.window.save()
            self.pump_idle()
        self.assertEqual(len(thread_ids), 1)
        self.assertNotEqual(thread_ids[0], main)
        value = self.context.profile()
        self.assertEqual(value['child']['name'], 'Новое тестовое имя')
        self.assertEqual(value['child']['age'], 12)
        self.assertEqual(value['child']['aliases'], ['FixtureA', 'FixtureB'])
        self.assertIn('сохранены', self.window.status.get())

    def test_invalid_age_and_duplicate_aliases_keep_old_profile_unchanged(self):
        original = self.context.file.read_bytes()
        for age, aliases in [('eleven', 'One'), ('-1', 'One'), ('121', 'One'), ('11', 'One, one')]:
            with self.subTest(age=age, aliases=aliases):
                self.window.fields[('child', 'age')].set(age)
                self.window.fields[('child', 'aliases')].set(aliases)
                with mock.patch.object(self.context, 'save') as save:
                    self.window.save()
                    save.assert_not_called()
                self.assertFalse(self.window.body._busy)
                self.assertIn('Проверьте', self.window.status.get())
                self.assertEqual(self.context.file.read_bytes(), original)

    def test_open_logs_creates_only_fixture_folder_and_calls_mocked_os(self):
        self.assertFalse(self.log.root.exists())
        with mock.patch('family_context_ui.os.startfile') as start:
            self.window.open_logs()
        start.assert_called_once_with(str(self.log.root))
        self.assertTrue(self.log.root.is_dir())
        self.assertFalse(self.log.db.exists())

    def test_export_is_explicit_utf8_and_does_not_open_external_app(self):
        self.log.append('user', 'Тестовая русская беседа', session_id='fixture-session')
        self.log.append('kitten', 'Тестовый ответ')
        with mock.patch('family_context_ui.os.startfile') as start:
            self.window.export()
            self.pump_idle()
            start.assert_not_called()
        exports = list(self.log.root.glob('history-export-*.jsonl'))
        self.assertEqual(len(exports), 1)
        rows = [json.loads(line) for line in exports[0].read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['text'] for r in rows}, {'Тестовая русская беседа', 'Тестовый ответ'})
        self.assertIn(exports[0].name, self.window.status.get())

    def test_corrupt_profile_is_reported_without_automatic_replacement(self):
        self.window.destroy()
        self.window = None
        original = b'corrupted fake private profile'
        self.context.file.write_bytes(original)
        self.window = FamilyProfileWindow(self.root, context=self.context, log=self.log)
        self.window.withdraw()
        self.root.update()
        self.assertIn('прочитать не удалось', self.window.status.get())
        self.assertEqual(self.context.file.read_bytes(), original)
        self.assertTrue(all(not field.get() for field in self.window.fields.values()))

    def test_failed_export_reports_error_without_raw_private_payload(self):
        with mock.patch.object(self.log, 'export_jsonl', side_effect=OSError('fixture private filename')):
            self.window.export()
            self.pump_idle()
        self.assertIn('не удалось', self.window.status.get())
        self.assertNotIn('private filename', self.window.status.get())


if __name__ == '__main__':
    unittest.main()
