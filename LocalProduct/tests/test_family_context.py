import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Synthetic family facts/history only. No personal profile, chat or network."""
import concurrent.futures
from contextlib import closing
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from family_context import APP_ID, ChatLog, ContextError, FamilyContext, MAX_PROFILE, empty_profile
import family_context as family_module


class FamilyContextTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'data' / 'tests-family-context'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='fixture-', dir=base)
        self.root = Path(self.temporary.name)
        self.context = FamilyContext(self.root / 'profile')

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def profile():
        # Every name/fact here is an artificial fixture, not this user's family.
        value = empty_profile()
        value['child'].update(name='Тестовый ребёнок', aliases=['FixtureKid'], age=11,
                              grade='5Б', school='Тестовая школа')
        value['mother'].update(name='Тестовая мама', age=44, occupation='Тестовый библиотекарь')
        value['father'].update(name='Тестовый папа', age=None, occupation='Тестовый инженер')
        return value

    def test_constructor_import_and_missing_profile_are_inert(self):
        missing = self.root / 'not-created'
        FamilyContext(missing)
        ChatLog(missing / 'history')
        self.assertFalse(missing.exists())
        self.assertEqual(FamilyContext(missing).profile(), empty_profile())
        self.assertFalse(missing.exists())
        spec = importlib.util.spec_from_file_location('family_context_import_fixture', Path(family_module.__file__))
        imported = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(imported)
        self.assertFalse(missing.exists())

    def test_known_facts_in_both_input_languages_use_only_saved_values(self):
        original = self.profile()
        saved = self.context.save(original)
        self.assertIsNone(original['updated_utc'])
        self.assertIsNotNone(saved['updated_utc'])
        cases = [('Как зовут мою маму?', 'Тестовая мама'),
                 ("What is my mom's name?", 'Тестовая мама'),
                 ('Сколько лет маме?', '44'), ('How old is mom?', '44'),
                 ('Сколько мне лет?', '11'), ('How old am I?', '11'),
                 ('В какой школе я учусь?', 'Тестовая школа'),
                 ('Which school do I go to?', 'Тестовая школа'),
                 ('В каком я классе?', '5Б'), ('What grade am I in?', '5Б'),
                 ('Чем работает папа?', 'Тестовый инженер'),
                 ("What is dad's job?", 'Тестовый инженер'),
                 ('Сколько лет FixtureKid?', '11')]
        for question, expected in cases:
            with self.subTest(question=question):
                answer = self.context.family_reply(question)
                self.assertIn(expected, answer)
                self.assertTrue(any('а' <= ch.casefold() <= 'я' for ch in answer))

    def test_unknown_facts_are_not_invented_and_unrelated_question_delegates(self):
        self.context.save(self.profile())
        self.assertIn('не знаю', self.context.family_reply('Сколько лет папе?'))
        self.assertIn('не знаю', self.context.family_reply('How old is dad?'))
        blank = FamilyContext(self.root / 'blank')
        self.assertIn('не записано', blank.family_reply('Как зовут мою маму?'))
        self.assertIn('не рассказали', blank.family_reply('В какой школе я учусь?'))
        self.assertIsNone(blank.family_reply('Какой мой любимый цвет?'))
        self.assertFalse(blank.root.exists())

    def test_summary_contains_only_present_family_facts(self):
        self.context.save(self.profile())
        answer = self.context.family_reply('Расскажи о моей семье')
        self.assertIn('Тестовый ребёнок', answer)
        self.assertIn('Тестовая мама', answer)
        self.assertIn('Тестовый папа', answer)
        self.assertNotIn('None', answer)
        self.assertIn('не буду придумывать', FamilyContext(self.root / 'blank').family_reply('Tell me about my family'))

    def test_feelings_and_general_school_talk_do_not_disclose_private_facts(self):
        self.context.save(self.profile())
        cases = ['Я ненавижу школу', 'Зачем нужна школа', 'Мама работает и мне грустно',
                 'I hate school', 'Why do we need school?', 'Mom is at work and I feel sad',
                 'В классе меня не понимают', 'Папа много работает']
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(self.context.family_reply(text))

    def test_family_callback_keeps_emotional_conversations_in_dialogue(self):
        from cat_dialogue import CatDialogue
        self.context.save(self.profile())
        for text in ('Я ненавижу школу', 'Зачем нужна школа', 'Мама работает и мне грустно'):
            with self.subTest(text=text):
                result = CatDialogue(family_reply=self.context.family_reply).reply(text)
                self.assertNotEqual(result['intent'], 'family_fact')
                self.assertNotIn('Тестовая школа', result['text'])
                self.assertNotIn('Тестовый библиотекарь', result['text'])
        factual = CatDialogue(family_reply=self.context.family_reply).reply('В какой школе я учусь?')
        self.assertEqual(factual['intent'], 'family_fact')
        self.assertIn('Тестовая школа', factual['text'])

    def test_validation_rejects_ambiguous_or_extra_private_fields(self):
        cases = []
        for age in (True, -1, 121, '11'):
            value = self.profile(); value['child']['age'] = age; cases.append(value)
        value = self.profile(); value['child']['aliases'] = ['Fixture', 'fixture']; cases.append(value)
        value = self.profile(); value['mother']['password'] = 'never accepted'; cases.append(value)
        value = self.profile(); value['child']['name'] = 'Bad\nName'; cases.append(value)
        value = self.profile(); value['updated_utc'] = '2026-01-01T00:00:00'; cases.append(value)
        value = self.profile(); value['version'] = True; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises((ContextError, ValueError)):
                self.context.save(value)
        self.assertFalse(self.context.root.exists())

    def test_corrupt_duplicate_or_oversized_profile_fails_without_exposing_bytes(self):
        self.context.root.mkdir()
        for payload in (b'not-json-private-fixture', b'{"version":1,"version":1}', b' ' * (MAX_PROFILE + 1)):
            self.context.file.write_bytes(payload)
            with self.assertRaises(ContextError) as caught:
                self.context.profile()
            self.assertNotIn('private-fixture', str(caught.exception))
            self.assertIn('недоступна', self.context.family_reply('Как зовут маму?'))

    @unittest.skipUnless(hasattr(os, 'link'), 'hardlinks required')
    def test_hardlinked_profile_is_never_read_or_overwritten(self):
        self.context.root.mkdir()
        target = self.root / 'keep.json'
        original = json.dumps(self.profile(), ensure_ascii=False).encode('utf-8')
        target.write_bytes(original)
        os.link(target, self.context.file)
        with self.assertRaises(ContextError):
            self.context.profile()
        with self.assertRaises(ValueError):
            self.context.save(self.profile())
        self.assertEqual(target.read_bytes(), original)


class ChatLogTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'data' / 'tests-family-context'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='history-', dir=base)
        self.root = Path(self.temporary.name)
        self.log = ChatLog(self.root / 'history')

    def tearDown(self):
        self.temporary.cleanup()

    def exported(self):
        target = self.log.export_jsonl()
        self.assertEqual(target.parent, self.log.root)
        return target, [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]

    def test_history_batch_preserves_roles_unicode_and_unverified_shared_identity(self):
        receipts = self.log.append_many([
            {'role': 'user', 'text': 'Проверка русского текста', 'session_id': 'fixture-session'},
            {'role': 'kitten', 'text': 'Тестовый ответ'},
            {'role': 'parent', 'kind': 'family', 'text': 'Тестовое сообщение родителя', 'message_id': 'family-fixture'},
            {'role': 'system', 'text': 'Тестовый учебный таймер'}])
        self.assertEqual(len(receipts), 4)
        self.assertTrue(all(row['inserted'] for row in receipts))
        target, rows = self.exported()
        self.assertEqual({row['role'] for row in rows}, {'user', 'kitten', 'parent', 'system'})
        self.assertTrue(all(row['person_verified'] == 0 for row in rows))
        self.assertIn('Проверка', target.read_text(encoding='utf-8'))
        self.assertNotIn('\\u041f', target.read_text(encoding='utf-8'))

    def test_duplicate_source_is_idempotent_but_role_and_kind_are_independent(self):
        first = self.log.append('parent', 'Один текст', 'family', 'message-one')
        second = self.log.append('parent', 'Один текст', 'family', 'message-one')
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(first['inserted']); self.assertFalse(second['inserted'])
        self.log.append('user', 'Другой автор', 'family', 'message-one')
        self.log.append('parent', 'Другой тип', 'cat', 'message-one')
        self.assertEqual(len(self.exported()[1]), 3)

    def test_changed_source_rolls_back_entire_batch(self):
        self.log.append('parent', 'Первоначальный текст', 'family', 'message-one')
        with self.assertRaises(ContextError):
            self.log.append_many([
                {'role': 'user', 'text': 'Не должен сохраниться', 'message_id': 'new-one'},
                {'role': 'parent', 'kind': 'family', 'text': 'Подменённый текст', 'message_id': 'message-one'}])
        rows = self.exported()[1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], 'Первоначальный текст')

    def test_invalid_batch_creates_no_history(self):
        bad = [[], [{'role': 'invalid', 'text': 'x'}], [{'role': 'user', 'text': ' '}],
               [{'role': 'user', 'text': 'x\x00y'}], [{'role': 'user', 'text': 'x', 'password': 'no'}],
               [{'role': 'user', 'text': 'x', 'message_id': '../bad'}], [{'role': 'user', 'text': 'x'}] * 101]
        for records in bad:
            with self.subTest(records=records), self.assertRaises(ContextError):
                self.log.append_many(records)
        self.assertFalse(self.log.root.exists())

    def test_foreign_sqlite_schema_is_not_adopted(self):
        self.log.root.mkdir()
        with closing(sqlite3.connect(self.log.db)) as connection:
            with connection:
                connection.execute('CREATE TABLE unrelated(secret TEXT)')
                connection.execute("INSERT INTO unrelated VALUES('fixture')")
        with self.assertRaises(ContextError):
            self.log.append('user', 'no')
        with closing(sqlite3.connect(self.log.db)) as connection:
            self.assertEqual(connection.execute('SELECT secret FROM unrelated').fetchone()[0], 'fixture')
            self.assertNotEqual(connection.execute('PRAGMA application_id').fetchone()[0], APP_ID)

    @unittest.skipUnless(hasattr(os, 'link'), 'hardlinks required')
    def test_hardlinked_history_database_is_refused(self):
        self.log.root.mkdir()
        target = self.root / 'keep.sqlite'
        target.write_bytes(b'artificial preserved bytes')
        os.link(target, self.log.db)
        with self.assertRaises((ContextError, ValueError)):
            self.log.append('user', 'no')
        self.assertEqual(target.read_bytes(), b'artificial preserved bytes')

    def test_concurrent_initial_append_serializes_schema_and_preserves_every_message(self):
        barrier = threading.Barrier(6)
        def append(number):
            barrier.wait(timeout=5)
            return ChatLog(self.log.root).append('user', 'Тест ' + str(number), message_id='fixture-' + str(number))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(append, range(6)))
        self.assertTrue(all(result['inserted'] for result in results))
        self.assertEqual(len(self.exported()[1]), 6)


if __name__ == '__main__':
    unittest.main()
