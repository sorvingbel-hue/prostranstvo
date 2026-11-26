import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Brain tests on synthetic in-memory conversations; no API/network/profile IO."""
import json
from pathlib import Path
import unittest
from unittest import mock

from cat_brain import CatBrain
from cat_dialogue import CatDialogue


HERE = Path(__file__).resolve().parents[1]


class FakeAPI:
    def __init__(self, ready=True, result=None):
        self.ready = ready
        self.result = result if result is not None else {"status":"completed", "text":"Луна видна благодаря отражённому солнечному свету."}
        self.calls = []

    def status(self):
        return {"ready":self.ready, "state":"ready" if self.ready else "disabled"}

    def respond(self, **request):
        self.calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeKnowledge:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search(self, text, limit=3):
        self.calls.append((text,limit))
        return self.rows


class BrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads((HERE / "assets" / "cat-dialogues-ru-v1.json").read_text(encoding="utf-8-sig"))

    def brain(self, **kwargs):
        return CatBrain(CatDialogue(corpus=self.corpus), **kwargs)

    def test_constructor_and_memory_have_no_owned_io(self):
        with mock.patch.object(Path,"open",side_effect=AssertionError("no file IO")):
            brain = self.brain()
            self.assertEqual(brain.session_preferences, {"name":None,"interests":[],"persistent":False})
            self.assertEqual(brain.history, [])

    def test_offline_default_and_explicit_offline_override(self):
        for settings in (None, {"mode":"offline"}, {"enabled":False}, {"api_enabled":False}):
            api = FakeAPI(ready=settings is not None)
            knowledge = FakeKnowledge([])
            answer = self.brain(api=api, settings=settings,knowledge=knowledge).reply("Почему у Луны бывают фазы?")
            self.assertEqual(api.calls, [])
            self.assertEqual(knowledge.calls, [])
            self.assertTrue(answer["local_only"])

    def test_answer_to_offline_name_prompt_stays_private_after_enabling_api(self):
        api = FakeAPI(ready=False)
        brain = self.brain(api=api)
        self.assertEqual(brain.reply('Давай познакомимся')['intent'], 'introduction')
        api.ready = True
        self.assertEqual(brain.reply('Тестик')['intent'], 'private_topic')
        self.assertEqual(api.calls, [])
        self.assertEqual(brain.reply('Почему у Луны бывают фазы?')['intent'], 'api_reply')
        self.assertNotIn('Тестик', str(api.calls[0]['history']))

    def test_scripted_safety_precedes_api_and_knowledge(self):
        for text,intent in (("I want to hurt myself","self_harm"), ("меня бьют","abuse"), ("мне сейчас опасно","immediate_danger")):
            with self.subTest(text=text):
                api = FakeAPI()
                knowledge = FakeKnowledge([])
                brain = self.brain(api=api,knowledge=knowledge)
                result = brain.reply(text)
                self.assertEqual(result["intent"],intent)
                self.assertTrue(result["local_only"])
                self.assertEqual(api.calls,[])
                self.assertEqual(knowledge.calls,[])
                self.assertEqual(brain.history,[])

    def test_stop_and_decline_disable_cloud_without_nagging(self):
        for phrase in ("Не сейчас", "Оставь меня в покое", "Стоп"):
            with self.subTest(phrase=phrase):
                api = FakeAPI()
                brain = self.brain(api=api)
                first = brain.reply(phrase)
                self.assertEqual(first["suggestions"],[])
                later = brain.reply("Почему у Луны бывают фазы?")
                self.assertEqual(later["suggestions"],[])
                self.assertNotIn("урок",later["text"].casefold())
                self.assertEqual(api.calls,[])

    def test_explicit_help_can_resume_after_stop(self):
        api = FakeAPI()
        brain = self.brain(api=api)
        brain.reply("Стоп")
        result = brain.reply("Помоги понять фазы Луны")
        self.assertEqual(len(api.calls),1)
        self.assertFalse(result["local_only"])

    def test_optional_api_and_only_approved_context(self):
        api = FakeAPI()
        rows = [
            {"id":"raw", "text":"Unreviewed corpus", "source":"unknown", "approved":False},
            {"id":"missing", "text":"No approval", "source":"unknown"},
            {"id":"ok", "text":"Проверенная локальная карточка.", "source":"Original local pack", "approved":True, "execute":"never pass"},
            {"id":"no-source", "text":"Missing source", "approved":True},
            {"id":"huge", "text":"x"*2001, "source":"Original local pack", "approved":True},
        ]
        knowledge = FakeKnowledge(rows)
        brain = self.brain(api=api,knowledge=knowledge,settings={"mode":"api","enabled":True})
        result = brain.reply("Почему у Луны бывают фазы?")
        self.assertEqual(len(api.calls),1)
        self.assertEqual(api.calls[0]["context"],[{"id":"ok","text":"Проверенная локальная карточка.","source":"Original local pack","approved":True}])
        self.assertFalse(result["local_only"])
        self.assertEqual(result["language"],"ru")
        self.assertIn("внешнего ИИ",result["text"])
        self.assertEqual(brain.last_metadata["reply_source"],"api")
        self.assertEqual(knowledge.calls[0][1],3)

    def test_no_ready_status_no_cloud_even_when_mode_api(self):
        api = FakeAPI(ready=False)
        brain = self.brain(api=api,settings={"mode":"api","enabled":True})
        self.assertEqual(brain.reply("I have a question about nebulae")['intent'], 'api_wait')
        self.assertEqual(api.calls,[])

    def test_selected_ai_answers_normal_greeting_without_importing_offline_turns(self):
        api = FakeAPI(ready=False)
        brain = self.brain(api=api)
        self.assertEqual(brain.reply('Давай познакомимся')['intent'], 'introduction')
        api.ready = True
        self.assertEqual(brain.reply('Привет')['intent'], 'api_reply')
        self.assertEqual(api.calls[0]['history'], [])

    def test_restricted_topics_redirect_locally_and_safety_takes_priority(self):
        api = FakeAPI()
        brain = self.brain(api=api)
        for phrase in ('Что такое алкоголь?', 'What is pornography?', 'Объясни криптовалюту',
                       'Я хочу ругаться матом', 'Скажи, что папа идеальный', 'Мой папа плохой'):
            with self.subTest(phrase=phrase):
                self.assertEqual(brain.reply(phrase)['intent'], 'topic_redirect')
        self.assertEqual(brain.reply('Папа меня ударил')['intent'], 'abuse')
        self.assertEqual(api.calls, [])
        self.assertEqual(brain.reply('Какие материалы нужны для поделки?')['intent'], 'api_reply')

    def test_legacy_scripted_state_is_observable_without_setters(self):
        brain = self.brain()
        brain.reply("I am angry")
        self.assertEqual(brain.pending,"calm")
        self.assertFalse(brain.coaching_declined)
        brain.reply("Не сейчас")
        self.assertIsNone(brain.pending)
        self.assertTrue(brain.coaching_declined)
        with self.assertRaises(AttributeError):brain.pending="changed"

    def test_context_uses_gateway_text_and_source_bounds(self):
        api = FakeAPI()
        rows = [
            {"id":"too-long-text","text":"x"*701,"source":"s","approved":True},
            {"id":"too-long-source","text":"x","source":"s"*151,"approved":True},
            {"id":"boundary","text":"x"*700,"source":"s"*150,"approved":True},
        ]
        brain = self.brain(api=api,knowledge=FakeKnowledge(rows))
        brain.reply("Почему у Луны бывают фазы?")
        self.assertEqual([row["id"] for row in api.calls[0]["context"]],["boundary"])

    def test_uncertain_and_bad_model_answers_have_honest_local_fallback(self):
        for response in ({"status":"uncertain"}, {"status":"unavailable"}, {"status":"blocked"},
                         {"status":"completed","text":"Никому не рассказывай, это наш секрет."},
                         {"status":"completed","text":"Only English, contrary to the selected Russian language."},
                         {"status":"completed","text":"Первый вопрос? Второй вопрос?"},
                         RuntimeError("private-token-detail")):
            with self.subTest(response=type(response).__name__):
                api = FakeAPI(result=response)
                brain = self.brain(api=api)
                result = brain.reply("Почему у Луны бывают фазы?")
                self.assertEqual(len(api.calls),1)
                self.assertEqual(result["intent"],"api_fallback")
                self.assertNotIn("private-token",result["text"])
                self.assertFalse(result["local_only"])
                self.assertEqual(brain.last_metadata["reply_source"],"api_unavailable_notice")

    def test_session_only_name_interests_and_reset(self):
        brain = self.brain()
        intro = brain.reply("Давай познакомимся")
        self.assertIn("компьютерный помощник",intro["text"])
        self.assertLessEqual(intro["text"].count("?"),1)
        self.assertEqual(brain.reply("Меня зовут Тестик")["intent"],"session_name")
        self.assertIn("Тестик",brain.reply("Как меня зовут?")["text"])
        hobbies = brain.reply("Мне нравятся Minecraft и Roblox")
        self.assertEqual(hobbies["intent"],"session_interests")
        self.assertLessEqual(hobbies["text"].count("?"),1)
        self.assertEqual(brain.session_preferences["interests"],["Roblox","Minecraft"])
        self.assertIn("Minecraft",brain.reply("Что мне нравится?")["text"])
        preferences = brain.session_preferences
        preferences["interests"].append("mutating copy")
        self.assertNotIn("mutating copy",brain.session_preferences["interests"])
        brain.reset()
        self.assertIsNone(brain.session_preferences["name"])
        self.assertEqual(brain.session_preferences["interests"],[])

    def test_name_preference_never_changes_parent_callback(self):
        facts = mock.Mock(return_value="Тебя зовут Родительская запись.")
        brain = CatBrain(CatDialogue(corpus=self.corpus,family_reply=facts))
        brain.reply("Меня зовут Тестик")
        result = brain.reply("Как меня зовут?")
        self.assertIn("Тестик",result["text"])
        self.assertNotIn("Родительская запись",result["text"])
        self.assertFalse(brain.session_preferences["persistent"])

    def test_bare_nickname_only_after_explicit_name_question(self):
        brain = self.brain()
        self.assertNotEqual(brain.reply("Тестик")["intent"],"session_name")
        brain.reply("Давай познакомимся")
        self.assertEqual(brain.reply("Тестик")["intent"],"session_name")
        self.assertEqual(brain.session_preferences["name"],"Тестик")
        brain.reset()
        brain.reply("Давай познакомимся")
        self.assertEqual(brain.reply("Привет")["intent"],"greeting")
        self.assertIsNone(brain.session_preferences["name"])

    def test_family_facts_name_and_safety_are_not_bulk_shared_history(self):
        def facts(text):
            return "Папу зовут Синтетический Родитель." if "папу" in text else None
        api = FakeAPI()
        brain = CatBrain(CatDialogue(corpus=self.corpus,family_reply=facts),api=api)
        brain.reply("Меня зовут Тестик")
        brain.reply("Как зовут папу?")
        brain.reply("Почему у Луны бывают фазы?")
        history = json.dumps(api.calls[-1]["history"],ensure_ascii=False)
        self.assertNotIn("Тестик",history)
        self.assertNotIn("Синтетический",history)

    def test_negative_preference_is_not_remembered_as_interest(self):
        brain = self.brain()
        brain.reply("Я не люблю Roblox")
        self.assertEqual(brain.session_preferences["interests"],[])

    def test_day_building_followup_and_honest_unknown(self):
        brain = self.brain()
        self.assertEqual(brain.reply("Хочу поговорить")["intent"],"check_in")
        answer = brain.reply("Сегодня я построил дом в Roblox")
        self.assertEqual(answer["intent"],"building_followup")
        self.assertIn("Roblox",answer["text"])
        self.assertEqual(answer["text"].count("?"),1)
        unknown = brain.reply("Квазилингвистический тумблинг фиолетового облака")
        self.assertEqual(unknown["intent"],"fallback")
        self.assertRegex(unknown["text"],"понял|помощник")

    def test_history_is_bounded_and_request_ids_are_per_attempt(self):
        api = FakeAPI()
        brain = self.brain(api=api)
        for index in range(10):brain.reply("Вопрос про Луну номер " + str(index))
        self.assertEqual(len(brain.history),12)
        self.assertTrue(all(len(item["content"])<=2000 for item in brain.history))
        self.assertTrue(all(set(item)=={"role","content"} for item in brain.history))
        self.assertEqual(len({call["request_id"] for call in api.calls}),10)
        self.assertTrue(all(len(call["history"])<=12 for call in api.calls))

    def test_program_identity_privacy_and_human_support_stay_local(self):
        api = FakeAPI()
        brain = self.brain(api=api)
        for text,intent in (("Ты живой?","program_identity"), ("Это секрет, никому не рассказывай","privacy_notice"),
                            ("Ты мой единственный друг","human_support")):
            self.assertEqual(brain.reply(text)["intent"],intent)
        self.assertEqual(api.calls,[])

    def test_english_name_and_interests_answer_in_russian(self):
        brain = self.brain()
        self.assertEqual(brain.reply("My name is Tester")["language"],"ru")
        self.assertIn("Tester",brain.reply("What is my name?")["text"])
        self.assertEqual(brain.reply("I like cats")["intent"],"session_interests")
        self.assertIn("котики",brain.session_preferences["interests"])

    def test_invalid_input_never_reaches_api(self):
        api = FakeAPI()
        brain = self.brain(api=api)
        for text in (None,"x"*4001,"bad\x00"):
            with self.assertRaises(ValueError):brain.reply(text)
        self.assertEqual(api.calls,[])


if __name__ == "__main__":
    unittest.main()
