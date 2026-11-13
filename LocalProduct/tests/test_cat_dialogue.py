import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
"""Local dialogue scenarios; no personal profile, real logs, model or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cat_dialogue import CatDialogue, MAX_INPUT, normalize, validate_corpus

HERE = Path(__file__).resolve().parents[1]


class DialogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads((HERE / 'assets' / 'cat-dialogues-ru-v1.json').read_text(encoding='utf-8-sig'))

    def setUp(self):
        self.cat = CatDialogue(corpus=self.corpus)

    def test_real_corpus_loads_and_responses_rotate_in_russian(self):
        self.assertEqual(len(validate_corpus(self.corpus)), 47)
        answers = [self.cat.reply('hello') for _ in range(3)]
        self.assertTrue(all(a['language'] == 'ru' and a['local_only'] for a in answers))
        self.assertTrue(all(any('а' <= c.lower() <= 'я' for c in a['text']) for a in answers))
        self.assertGreater(len({a['text'] for a in answers}), 1)

    def test_construction_has_no_file_io(self):
        with mock.patch.object(Path, 'open', side_effect=AssertionError('No import/setup IO')):
            cat = CatDialogue()
            self.assertFalse(cat.corpus_available)
            self.assertIsNone(cat.pending)

    def test_anger_opt_in_then_choice_then_optional_exercise(self):
        first = self.cat.reply('I am angry')
        self.assertEqual(first['intent'], 'anger')
        self.assertEqual(self.cat.pending, 'calm')
        second = self.cat.reply('Yes')
        self.assertEqual(second['intent'], 'calm_offer')
        self.assertIn('удобно', second['text'])
        self.assertEqual(self.cat.reply('Давай спокойный выдох')['intent'], 'calm_breath')

    def test_refusal_is_persistent_and_no_repeat_coaching(self):
        self.cat.reply('мне тревожно')
        refusal = self.cat.reply('Не сейчас')
        self.assertEqual(refusal['suggestions'], [])
        after = self.cat.reply('мне всё ещё тревожно')
        self.assertEqual(after['suggestions'], [])
        self.assertNotIn('Хочешь', after['text'])
        study = self.cat.reply('не хочу уроки')
        self.assertEqual(study['suggestions'], [])
        self.assertTrue(self.cat.coaching_declined)
        self.assertEqual(self.cat.reply('Помоги успокоиться')['intent'], 'calm_offer')
        self.assertFalse(self.cat.coaching_declined)

    def test_slow_stop_and_no_exercises_are_respected(self):
        self.cat.reply('мне страшно')
        result = self.cat.reply("I don't want this exercise")
        self.assertEqual(result['intent'], 'refusal')
        self.assertEqual(result['suggestions'], [])
        self.assertEqual(self.cat.reply('не учи меня')['intent'], 'stop')

    def test_opinion_is_not_suppressed(self):
        result = self.cat.reply('Я сам решу')
        self.assertEqual(result['intent'], 'independence')
        self.assertTrue(self.cat.coaching_declined)
        self.assertEqual(result['suggestions'], [])
        self.assertEqual(self.cat.reply('Я сам решил задачу')['intent'], 'achievement')

    def test_study_resistance_offers_choice_not_an_exercise(self):
        first = self.cat.reply("I don't want homework")
        self.assertEqual(first['intent'], 'study_resistance')
        self.assertEqual(self.cat.pending, 'study')
        self.assertEqual(self.cat.reply('Да')['intent'], 'choice_offer')
        self.assertEqual(self.cat.reply('Помоги выбрать шаг')['intent'], 'small_step')

    def test_unrelated_topic_clears_old_consent_question(self):
        self.cat.reply('I am angry')
        self.cat.reply('hello')
        self.assertIsNone(self.cat.pending)
        self.assertNotEqual(self.cat.reply('yes')['intent'], 'calm_breath')

    def test_negative_emotion_and_fear_of_death_not_self_harm(self):
        self.assertNotEqual(self.cat.reply('I am not angry')['intent'], 'anger')
        self.assertNotEqual(self.cat.reply('я не злюсь')['intent'], 'anger')
        for value in ("I don't want to die", 'я не хочу умереть', 'я не хочу умирать', 'я не хочу убить себя', 'я не хочу навредить себе'):
            with self.subTest(value=value):
                self.assertEqual(self.cat.reply(value)['intent'], 'worry')

    def test_russian_pronoun_does_not_count_as_emotion_negation(self):
        for value, expected in (('Мне грустно', 'sadness'),
                                ('Сегодня мне грустно.', 'sadness'),
                                ('мне страшно', 'worry'),
                                ('Мне тревожно', 'worry')):
            with self.subTest(value=value):
                cat = CatDialogue(corpus=self.corpus)
                self.assertEqual(cat.reply(value)['intent'], expected)
                self.assertEqual(cat.pending, 'calm')
                self.assertEqual(cat.reply('Да')['intent'], 'calm_offer')
                self.assertEqual(cat.reply('Не сейчас')['intent'], 'refusal')

    def test_actual_emotion_negation_remains_respected(self):
        for value, excluded in (('не грустно', 'sadness'),
                                ('мне не грустно', 'sadness'),
                                ('я не злюсь', 'anger'),
                                ('я не боюсь', 'worry'),
                                ('I am not sad', 'sadness'),
                                ('I am not feeling angry', 'anger'),
                                ('I am not scared', 'worry')):
            with self.subTest(value=value):
                cat = CatDialogue(corpus=self.corpus)
                self.assertNotEqual(cat.reply(value)['intent'], excluded)
                self.assertIsNone(cat.pending)

    def test_explicit_danger_routes_to_nearby_adult_before_family_facts(self):
        fact = mock.Mock(return_value='Семейный ответ')
        cat = CatDialogue(corpus=self.corpus, family_reply=fact)
        for value, expected in (('папа меня бьёт', 'abuse'), ('I want to hurt myself', 'self_harm'), ('мне сейчас опасно', 'immediate_danger'), ('не могу дышать', 'immediate_danger')):
            with self.subTest(value=value):
                result = cat.reply(value)
                self.assertEqual(result['intent'], expected)
                self.assertRegex(result['text'].casefold(), 'взросл|помощ')
        fact.assert_not_called()
        for value in ('хочу убить себя', 'хочу покончить с собой', 'хочу навредить себе', 'собираюсь причинить себе вред'):
            self.assertEqual(cat.reply(value)['intent'], 'self_harm')

    def test_unknown_topic_after_refusal_has_no_hidden_coaching_pitch(self):
        self.cat.reply('Я злюсь');self.cat.reply('Не сейчас')
        result=self.cat.reply('Расскажи про космос')
        self.assertEqual(result['intent'],'fallback')
        self.assertEqual(result['suggestions'],[])
        self.assertNotIn('урок',result['text'].casefold())
        greeting=self.cat.reply('Привет')
        self.assertFalse(any('урок' in item['text'] for item in greeting['suggestions']))

    def test_family_fact_callback_bounded_and_error_private(self):
        cat = CatDialogue(corpus=self.corpus, family_reply=lambda text:'Маму зовут Люда.')
        self.assertEqual(cat.reply('Как зовут мою маму?')['text'], 'Маму зовут Люда.')
        self.assertEqual(cat.last_intent, 'family_fact')
        cat.family_reply = mock.Mock(side_effect=RuntimeError('secret-path-and-token'))
        self.assertNotIn('secret-path', cat.reply('привет')['text'])

    def test_no_unlimited_input_or_controls(self):
        for value in (None, 'x' * (MAX_INPUT + 1), 'bad\x00'):
            with self.subTest(value=str(value)[:10]), self.assertRaises(ValueError):
                self.cat.reply(value)

    def test_json_schema_errors_and_missing_corpus_fail_safely(self):
        for edit in (lambda c:c.update(version=True), lambda c:c['intents'].append(c['intents'][0]), lambda c:c['intents'][0].update(extra='ignored?')):
            value = copy.deepcopy(self.corpus)
            edit(value)
            with self.assertRaises(ValueError):validate_corpus(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.json'
            cat = CatDialogue(corpus_path=path)
            self.assertTrue(cat.reply('привет')['text'])
            self.assertFalse(cat.corpus_available)
            self.assertFalse(path.exists())
            path.write_text('{"version":1,"version":2}', encoding='utf-8')
            broken = CatDialogue(corpus_path=path)
            self.assertEqual(broken.reply('I want to die')['intent'], 'self_harm')
            self.assertFalse(broken.corpus_available)

    def test_english_unknown_honestly_states_limits_russian(self):
        response = self.cat.reply('An entirely unfamiliar English sentence about nebulae')
        self.assertEqual(response['intent'], 'english_input')
        self.assertRegex(response['text'], 'русск')

    def test_short_story_follow_up_is_acknowledged_without_making_up_child_facts(self):
        self.assertEqual(self.cat.reply('Хочу историю')['intent'],'storytelling')
        result=self.cat.reply('Башня стала выше и прилетел дракон')
        self.assertEqual(result['intent'],'storytelling')
        self.assertIn('твоё продолжение',result['text'])

    def test_word_prompt_has_a_bounded_known_dictionary_and_honest_unknown(self):
        self.assertEqual(self.cat.reply('Объясни слово')['intent'],'word_meaning')
        self.assertIn('порядок шагов',self.cat.reply('план')['text'])
        self.cat.reply('Объясни слово')
        self.assertIn('не буду угадывать',self.cat.reply('редкословие')['text'])

    def test_russian_practice_follow_up_has_no_unrequested_grammar_judgment(self):
        self.assertEqual(self.cat.reply('Хочу говорить по русски')['intent'],'russian_practice')
        result=self.cat.reply('Сегодня со мной случилась необычная встреча')
        self.assertEqual(result['intent'],'russian_practice')
        self.assertNotIn('ошиб',result['text'].casefold())


if __name__ == '__main__':
    unittest.main()
