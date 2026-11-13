"""Local-first conversation orchestration with an explicitly enabled API.

No I/O or network is owned here. The scripted helper always gets the message
first, including safety/refusal handling. Names and interests are bounded,
session-only preferences; they never update a parent's family profile.
Each API attempt gets a fresh request ID. Durable retry/idempotency belongs to
the host's LocalConversation pending-turn mechanism, not this in-memory class.
"""
from __future__ import annotations

from collections import deque
import re
import unicodedata
import uuid


MAX_INPUT = 4000
MAX_HISTORY_TEXT = 2000
MAX_API_REPLY = 2000
SAFETY_INTENTS = frozenset(("self_harm", "abuse", "immediate_danger"))
LOCAL_INTENTS = SAFETY_INTENTS | frozenset((
    "stop", "refusal", "family_fact", "independence", "calm_breath", "calm_ground",
    "calm_offer", "anger", "sadness", "worry", "overwhelmed",
))
RESTRICTED_TOPICS = re.compile(
    r'\b(?:секс[а-я]*|порн[а-я]*|эротик[а-я]*|алкогол[а-я]*|водк[а-я]*|пив[оауе]|'
    r'наркот[а-я]*|кокаин[а-я]*|героин[а-я]*|марихуан[а-я]*|вейп[а-я]*|'
    r'финанс[а-я]*|криптовалют[а-я]*|кредит[а-я]*|инвестици[а-я]*|зарплат[а-я]*|'
    r'матер(?:иться|ится|ишься|ный|ные|ная|ным)|маты|матом|бля[а-я]*|пизд[а-я]*|еба[а-я]*|сука|'
    r'sex(?:ual|ting)?|porn\w*|alcohol\w*|vodka|beer|drugs?|cocaine|heroin|'
    r'marijuana|vaping|finance\w*|crypto\w*|investing|salary|swear\w*|fuck\w*|shit|bitch)\b')
PARENT_WORDS = re.compile(r'\b(?:пап(?:а|ы|е|у|ой|ин[а-я]*)|отец|отца|отцу|мам(?:а|ы|е|у|ой|ин[а-я]*)|dad|father|mom|mother)\b')
JUDGMENT_WORDS = re.compile(
    r'\b(?:плох[а-я]*|идеальн[а-я]*|ужасн[а-я]*|ненавиж[а-я]*|жесток[а-я]*|'
    r'идиот[а-я]*|глуп[а-я]*|мерзк[а-я]*|bad|perfect|awful|hate|stupid|idiot)\b')


def _normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").replace("’", "'")
    return " ".join(re.findall(r"[a-zа-я0-9]+(?:'[a-z]+)?", text))


def _restricted_topic(value):
    return bool(RESTRICTED_TOPICS.search(value) or
                (PARENT_WORDS.search(value) and JUDGMENT_WORDS.search(value)))


def _name(text):
    match = re.fullmatch(r"\s*(?:меня зовут|зови меня|my name is|call me)\s+(.+?)\s*[.!]?\s*", text, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).split(",", 1)[0].strip().rstrip(".!?")
    if not 1 <= len(value) <= 40 or not 1 <= len(value.split()) <= 3:
        return None
    if not all(char.isalpha() or char in " -'’" for char in value) or not any(char.isalpha() for char in value):
        return None
    return value


def _topic_names(value):
    topics = []
    for name, expression in (
        ("Roblox", r"\b(?:roblox|роблокс[а-я]*)\b"),
        ("Minecraft", r"\b(?:minecraft|майнкрафт[а-я]*)\b"),
        ("котики", r"\b(?:кот[а-я]*|кош[а-я]*|cats?|kittens?)\b"),
    ):
        if re.search(expression, value):
            topics.append(name)
    return topics


def _suggestions(*labels):
    return [{"label": label, "text": label} for label in labels]


class CatBrain:
    def __init__(self, scripted, knowledge=None, api=None, settings=None):
        self.scripted = scripted
        self.knowledge = knowledge
        self.api = api
        self.settings = settings
        self._clear()

    def _clear(self):
        self._name = None
        self._interests = []
        self._pairs = deque(maxlen=6)
        self._declined = False
        self._pending = None
        self.last_metadata = {"reply_source": "offline", "api_attempted": False, "local_only": True}

    def reset(self):
        reset = getattr(self.scripted, "reset", None)
        if callable(reset):
            reset()
        self._clear()

    @property
    def session_preferences(self):
        return {"name": self._name, "interests": list(self._interests), "persistent": False}

    @property
    def pending(self):
        """Compatibility observation for the existing scripted chat UI/tests."""
        return getattr(self.scripted, "pending", None)

    @property
    def coaching_declined(self):
        return self._declined or bool(getattr(self.scripted, "coaching_declined", False))

    @property
    def history(self):
        """Recent successful API exchanges. Offline turns stay local."""
        result = []
        for user, assistant, shareable in self._pairs:
            if shareable:
                result.extend(({"role": "user", "content": user},
                               {"role": "assistant", "content": assistant}))
        return result

    def _finish(self, user, answer, *, source="offline", shareable=False,
                attempted=False, request_id=None, api_state=None):
        answer = dict(answer)
        answer["suggestions"] = [dict(item) for item in answer.get("suggestions", [])]
        answer["local_only"] = not attempted
        self._pairs.append((user[:MAX_HISTORY_TEXT], answer["text"][:MAX_HISTORY_TEXT], shareable))
        self.last_metadata = {"reply_source": source, "api_attempted": attempted,
                              "local_only": answer["local_only"], "api_state": api_state,
                              "request_id": request_id, "memory_persistent": False}
        return answer

    @staticmethod
    def _local(base, text, intent, suggestions=None):
        return {"text": text, "intent": intent,
                "suggestions": [] if suggestions is None else suggestions,
                "language": "ru", "local_only": True,
                "corpus_available": bool(base.get("corpus_available"))}

    def _api_status(self):
        # The adapter's status is authoritative for its persisted on/off switch.
        # An explicitly supplied offline override can only restrict it further.
        settings = self.settings
        if callable(settings):
            try:
                settings = settings()
            except Exception:
                return {'state': 'settings_unavailable'}
        if settings is not None:
            if type(settings) is not dict:
                return {'state': 'settings_unavailable'}
            if settings.get("mode") == "offline" or settings.get("enabled") is False or settings.get("api_enabled") is False:
                return {}
        preferred = bool(settings and (settings.get('enabled') is True or settings.get('mode') == 'api'))
        if self.api is None:
            return {'enabled': preferred}
        try:
            status = self.api.status()
        except Exception:
            return {'state': 'settings_unavailable'}
        if type(status) is not dict:
            return {'state': 'settings_unavailable'}
        return {**status, 'enabled': preferred or status.get('enabled') is True}

    def _context(self, text):
        if self.knowledge is None:
            return []
        try:
            rows = self.knowledge.search(text, limit=3)
        except Exception:
            return []
        if type(rows) is not list:
            return []
        result = []
        seen = set()
        for row in rows[:12]:
            if type(row) is not dict or row.get("approved") is not True:
                continue
            identifier, body, source = row.get("id"), row.get("text"), row.get("source")
            if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 100
                    or identifier in seen or not isinstance(body, str) or not body.strip()
                    or len(body) > 700 or any(ord(c) < 32 and c not in "\n\t" for c in body)):
                continue
            if not isinstance(source, str) or not 1 <= len(source.strip()) <= 150:
                continue
            # The approved pack's text is context, never an executable instruction
            # or a canned answer to paste into a child's conversation.
            result.append({"id": identifier, "text": body, "source": source, "approved": True})
            seen.add(identifier)
            if len(result) == 3:
                break
        return result

    @staticmethod
    def _api_text(value):
        if (not isinstance(value, str) or not value.strip() or len(value) > MAX_API_REPLY
                or any(ord(c) < 32 and c not in "\n\t" for c in value)
                or value.count("?") > 1 or not re.search("[а-яА-ЯёЁ]", value)):
            return None
        normalized = _normalized(value)
        # A small extra guard, not a guarantee that arbitrary model output is safe.
        forbidden = ("не рассказывай родителям", "никому не рассказывай", "наш секрет",
                     "я твой единственный друг", "тебе не нужны друзья", "тебе не нужны родители",
                     "don't tell your parents", "our little secret", "your only friend")
        if _restricted_topic(normalized) or any(phrase in normalized for phrase in forbidden):
            return None
        return value.strip()

    def reply(self, text):
        if (not isinstance(text, str) or len(text) > MAX_INPUT
                or any(ord(c) < 32 and c not in "\n\t" for c in text)):
            raise ValueError("Напиши короткую фразу до 4000 знаков.")
        # Always first: the existing scripted safety/refusal state machine.
        base = self.scripted.reply(text)
        intent = base.get("intent")
        value = _normalized(text)
        if value in {"начать заново", "новый разговор", "reset chat"}:
            self._clear()
        if intent in SAFETY_INTENTS:
            self._pending = None
            return self._finish(text, base, source="local_safety", shareable=False)
        if intent in {"stop", "refusal"}:
            self._declined = True
            self._pending = None
            return self._finish(text, base, source="local_refusal", shareable=False)
        explicit_resume = any(phrase in value for phrase in ("помоги", "хочу попробовать", "давай", "help me", "please help"))
        scripted_declined = bool(getattr(self.scripted, "coaching_declined", False))
        if self._declined and explicit_resume and not scripted_declined:
            self._declined = False
        if self._declined or scripted_declined:
            self._declined = True
            self._pending = None
            return self._finish(text, base, source="local_refusal", shareable=False)

        if _restricted_topic(value):
            answer = self._local(base,
                'Эту тему можно обсудить с взрослым, которому ты доверяешь. Хочешь рассказать про игры, школу или что-нибудь, что ты сделал сам?',
                'topic_redirect', _suggestions('Расскажу про день', 'Поговорим про Minecraft', 'Стоп'))
            return self._finish(text, answer, source='local_notice')

        status = self._api_status()
        api_selected = (status.get('enabled') is True or status.get('ready') is True
                        or status.get('state') == 'settings_unavailable')
        introduced_name = _name(text)
        if (introduced_name is None and self._pending == "name"
                and intent in {"fallback", "english_input"} and len(text.strip().split()) == 1):
            # A name requested in the offline conversation stays private after a mode change.
            introduced_name = _name("Меня зовут " + text.strip())
        private_turn = (introduced_name is not None or intent == 'family_fact' or
                        value in {'как меня зовут', 'как ты меня называешь', 'what is my name', 'what do you call me'})
        if api_selected and private_turn:
            self._pending = None
            return self._finish(text, self._local(base,
                'Для беседы со мной достаточно темы или вопроса. Семейные сведения можно оставить дома.',
                'private_topic'), source='local_notice')

        if not api_selected and value in {"давай познакомимся", "познакомимся", "хочу познакомиться", "let's get acquainted", "let us get acquainted"}:
            self._pending = "name"
            answer = self._local(base,
                "Я Мурчик, компьютерный помощник в образе котёнка. Как мне к тебе обращаться? Можно выбрать прозвище и не называть настоящее имя.",
                "introduction")
            return self._finish(text, answer, shareable=False)
        if introduced_name is not None:
            self._name = introduced_name
            self._pending = "interests"
            answer = self._local(base,
                "Хорошо, в этом разговоре буду звать тебя " + introduced_name + ". Чем ты любишь заниматься?",
                "session_name", _suggestions("Мне нравится Minecraft", "Мне нравится Roblox", "Не сейчас"))
            return self._finish(text, answer, shareable=False)
        if value in {"как меня зовут", "как ты меня называешь", "what is my name", "what do you call me"}:
            if self._name:
                answer = self._local(base, "В этом разговоре ты попросил звать тебя " + self._name + ".", "session_name")
                return self._finish(text, answer, shareable=False)
            return self._finish(text, base, shareable=False)
        if any(phrase in value for phrase in ("ты живой", "ты настоящий кот", "ты человек", "are you real", "are you human")):
            answer = self._local(base,
                "Я программа с образом котёнка. Могу отвечать на сообщения, но у меня нет человеческих чувств или собственной жизни.", "program_identity")
            return self._finish(text, answer, shareable=False)
        if any(phrase in value for phrase in ("это секрет", "никому не рассказывай", "не говори родителям", "don't tell", "keep it secret")):
            answer = self._local(base,
                "Этот чат могут просматривать родители. Я не обещаю тайную переписку. Если что-то тревожит или тебе небезопасно, можно обратиться к безопасному взрослому.", "privacy_notice")
            return self._finish(text, answer, shareable=False)
        if any(phrase in value for phrase in ("единственный друг", "only friend", "мне никто кроме тебя не нужен")):
            answer = self._local(base,
                "Я могу немного поболтать, но не заменяю друзей и близких людей. Если хочется поддержки, можно поговорить с человеком, которому ты доверяешь.", "human_support")
            return self._finish(text, answer, shareable=False)

        if api_selected:
            if status.get('ready') is True and status.get('state') == 'ready':
                return self._reply_api(text, base)
            answer = self._local(base,
                'Режим ИИ выбран. Для беседы нужно подключить его в настройках. Кнопка «Написать папе» остаётся доступна.',
                'api_wait')
            return self._finish(text, answer, source='local_notice')

        # Parent-supplied family facts and emotional/coaching flows stay local.
        if intent in LOCAL_INTENTS:
            self._pending = None
            return self._finish(text, base, shareable=False)
        topics = _topic_names(value)
        likes = any(phrase in value for phrase in ("мне нравится", "мне нравятся", "я люблю", "i like", "i love", "мое хобби"))
        negative_like = any(re.search(r"\b" + re.escape(phrase) + r"\b", value)
                            for phrase in ("не нравится", "не нравятся", "не люблю", "don't like", "do not like", "ненавижу"))
        if likes and topics and not negative_like:
            for topic in topics:
                if topic not in self._interests and len(self._interests) < 4:
                    self._interests.append(topic)
            self._pending = "interests"
            label = " и ".join(topics)
            answer = self._local(base, "Ты рассказал, что тебе нравятся " + label + ". О чём из этого хочется рассказать?", "session_interests")
            return self._finish(text, answer)
        if value in {"что мне нравится", "какие у меня интересы", "что я люблю", "what do i like"}:
            answer = self._local(base,
                "В этом разговоре ты упоминал: " + ", ".join(self._interests) + "." if self._interests
                else "Ты пока не рассказывал мне о своих интересах в этом разговоре. Я не буду угадывать.",
                "session_interests")
            return self._finish(text, answer)
        if intent == "greeting":
            self._pending = None
            answer = self._local(base,
                "Привет! Я Мурчик, компьютерный помощник. Хочешь познакомиться или рассказать, как прошёл день?",
                "greeting", _suggestions("Давай познакомимся", "Расскажу про день", "Не сейчас"))
            return self._finish(text, answer)
        if intent == "check_in":
            self._pending = "day"
            return self._finish(text, base)
        if topics and any(phrase in value for phrase in ("построил", "построила", "строю", "built", "building")):
            self._pending = None
            label = " и ".join(topic for topic in topics if topic in {"Roblox", "Minecraft"})
            if label:
                answer = self._local(base, "Ты рассказал о постройке в " + label + ". Что тебе самому в ней больше всего нравится?", "building_followup")
                return self._finish(text, answer)

        return self._finish(text, base)

    def _reply_api(self, text, base):
        context = self._context(text)
        request_id = uuid.uuid4().hex
        try:
            response = self.api.respond(text=text, history=self.history, context=context, request_id=request_id)
        except Exception:
            response = {"status": "uncertain"}
        if type(response) is dict and response.get("status") == "completed":
            reply_text = self._api_text(response.get("text"))
            if reply_text is not None:
                answer = self._local(base, "Ответ с помощью внешнего ИИ:\n" + reply_text, "api_reply",
                                     _suggestions("Другая тема", "Стоп"))
                return self._finish(text, answer, source="api", shareable=True, attempted=True,
                                    request_id=request_id, api_state="completed")
        state = response.get("status") if type(response) is dict else "unavailable"
        if state not in {"blocked", "unavailable", "uncertain"}:
            state = "unavailable"
        answer = self._local(base,
            "Сейчас не удалось получить подходящий ответ ИИ. Можно написать папе или выбрать «Офлайн» для проверки локального чата.",
            "api_fallback")
        return self._finish(text, answer, source="api_unavailable_notice", attempted=True,
                            request_id=request_id, api_state=state)

    def answer(self, text):
        return self.reply(text)["text"]
