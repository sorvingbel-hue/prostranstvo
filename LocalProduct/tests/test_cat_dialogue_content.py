import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Check that the curated child-facing choices form usable local conversations."""
import json
from pathlib import Path
import unittest

from cat_dialogue import CatDialogue, validate_corpus


class ChildDialogueContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Resolve next to the actual module, so the portable test harness can
        # import deployed code without reading a personal profile or chat log.
        import cat_dialogue
        path = cat_dialogue.ROOT / 'assets' / 'cat-dialogues-ru-v1.json'
        cls.corpus = json.loads(path.read_text(encoding='utf-8'))
        validate_corpus(cls.corpus)

    def test_visible_followup_buttons_never_dead_end_as_unrecognized_input(self):
        tested = 0
        for row in self.corpus['intents']:
            seeds = row['patterns']['ru'] + row['patterns']['en']
            seed = seeds[0] if seeds else 'незнакомая фраза'
            initial = CatDialogue(corpus=self.corpus).reply(seed)
            for button in initial['suggestions']:
                with self.subTest(source=row['id'], button=button['label']):
                    cat = CatDialogue(corpus=self.corpus)
                    cat.reply(seed)
                    result = cat.reply(button['text'])
                    self.assertNotIn(result['intent'], {'fallback', 'english_input'})
                    self.assertEqual(result['language'], 'ru')
                    self.assertRegex(result['text'], '[А-Яа-яЁё]')
                    tested += 1
        self.assertGreater(tested, 100)

    def test_child_can_decline_and_continue_neutral_russian_conversation(self):
        cat = CatDialogue(corpus=self.corpus)
        cat.reply('I am angry')
        self.assertEqual(cat.reply('Не сейчас')['suggestions'], [])
        for text in ('Расскажи про космос', 'I feel sad', 'привет'):
            result = cat.reply(text)
            self.assertEqual(result['language'], 'ru')
            self.assertTrue(cat.coaching_declined)
            for button in result['suggestions']:
                self.assertNotRegex(button['text'].casefold(), 'урок|выдох|упражн|маленький шаг')
        self.assertEqual(cat.reply('Помоги выбрать шаг')['intent'], 'small_step')
        self.assertFalse(cat.coaching_declined)


if __name__ == '__main__':
    unittest.main()
