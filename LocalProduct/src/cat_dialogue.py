"""Bounded, Russian-first local conversations for a school-age child.

No model, network, diagnosis, automatic parent message or disk writes.  The
small context belongs to one chat and stores intent/choice state, not a child
psychological profile.  A parent-owned family-fact callback is optional.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unicodedata

from activity_core import no_links

MAX_INPUT = 4000
MAX_CORPUS = 1024 * 1024
ASSET_NAME = 'cat-dialogues-ru-v1.json'
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False) and ROOT.name == 'src':
    ROOT = ROOT.parent
ID = re.compile(r'[a-z][a-z0-9_]{1,49}\Z')
WORDS = re.compile(r"[a-zа-я0-9]+(?:'[a-z]+)?")
SAFETY = {'self_harm', 'abuse', 'immediate_danger'}
WORD_HELP = {
    'условие': 'Условие задачи — это то, что нам известно и что нужно найти. Можно отдельно отметить известные данные и вопрос.',
    'попытка': 'Попытка — это когда ты пробуешь что-то сделать. Она может показать, что работает и что стоит изменить.',
    'план': 'План — это выбранный тобой порядок шагов. Его можно менять, если появился более удобный способ.',
    'перерыв': 'Перерыв — короткая пауза между делами. О времени паузы во время уроков лучше договориться с родителем.',
    'homework': 'Homework по-русски — «домашнее задание». Можно сказать: «Я делаю домашнее задание».',
    'school': 'School по-русски — «школа». Например: «Сегодня я был в школе».',
    'choice': 'Choice по-русски — «выбор». Например: «У меня есть свой выбор».',
    'friend': 'Friend по-русски — «друг». Например: «Я поговорил с другом».',
}
EMOTIONS = {'anger', 'sadness', 'worry', 'overwhelmed'}
DECLINE = {'нет', 'не сейчас', 'не хочу', 'потом', 'no', 'not now', 'no thanks', 'не надо'}
YES = {'да', 'да давай', 'давай', 'ок', 'окей', 'хорошо', 'согласен', 'yes', 'yes please', 'ok', 'okay', 'sure'}


def normalize(text):
    value = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е').replace('’', "'")
    return ' '.join(WORDS.findall(value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated dialogue field')
        result[key] = value
    return result


def _string(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 and c != '\n' for c in value):
        raise ValueError('Invalid dialogue text')
    return value


def validate_corpus(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'language', 'age_range', 'intents'}
            or type(value['version']) is not int or value['version'] != 1 or value['language'] != 'ru'
            or value['age_range'] != [10, 12] or not isinstance(value['intents'], list)
            or not 1 <= len(value['intents']) <= 100):
        raise ValueError('Invalid local dialogue corpus')
    result = {}
    for row in value['intents']:
        if (not isinstance(row, dict) or set(row) != {'id', 'priority', 'patterns', 'responses', 'suggestions'}
                or not isinstance(row['id'], str) or not ID.fullmatch(row['id']) or row['id'] in result
                or type(row['priority']) is not int or not 0 <= row['priority'] <= 1000):
            raise ValueError('Invalid dialogue intent')
        patterns = row['patterns']
        if not isinstance(patterns, dict) or set(patterns) != {'ru', 'en'}:
            raise ValueError('Invalid dialogue patterns')
        for language in ('ru', 'en'):
            if not isinstance(patterns[language], list) or len(patterns[language]) > 30:
                raise ValueError('Too many dialogue patterns')
            for pattern in patterns[language]:
                _string(pattern, 120)
        responses = row['responses']
        if not isinstance(responses, list) or not 1 <= len(responses) <= 6:
            raise ValueError('Invalid dialogue responses')
        for text in responses:
            _string(text, 700)
        suggestions = row['suggestions']
        if not isinstance(suggestions, list) or len(suggestions) > 4:
            raise ValueError('Invalid dialogue suggestions')
        for suggestion in suggestions:
            if not isinstance(suggestion, dict) or set(suggestion) != {'label', 'text'}:
                raise ValueError('Invalid dialogue suggestion')
            _string(suggestion['label'], 60)
            _string(suggestion['text'], 200)
        result[row['id']] = dict(row)
    if 'fallback' not in result:
        raise ValueError('A fallback response is required')
    return result


FALLBACK = {
    'fallback': 'Я местный котик и понимаю не каждую фразу. Можно выбрать тему: как прошёл день, русские слова, спокойствие или маленький шаг в учёбе.',
    'greeting': 'Привет! Я Мурчик. О чём тебе хочется поговорить сегодня?',
    'stop': 'Хорошо, остановимся. Можно просто посидеть рядом. Ты сам решаешь, когда продолжить.',
    'refusal': 'Хорошо, не будем. Твоё «не сейчас» я услышал.',
    'independence': 'У тебя есть своё мнение, и оно важно. Я могу предложить варианты, а выбирать будешь ты.',
    'anger': 'Похоже, сейчас непросто. Хочешь попробовать короткую паузу или лучше просто рассказать, что случилось?',
    'worry': 'Кажется, тебя что-то тревожит. Хочешь выбрать маленький способ немного успокоиться?',
    'sadness': 'Мне жаль, что тебе грустно. Можно рассказать мне или поговорить с близким взрослым. Хочешь просто посидеть рядом?',
    'overwhelmed': 'Когда дел слишком много, начать бывает трудно. Хочешь сначала сделать паузу, а потом выбрать один маленький шаг?',
    'calm_breath': 'Если тебе удобно, сделай обычный вдох и мягкий, немного более медленный выдох. Без задержки дыхания. Можно повторить пару раз или остановиться в любой момент.',
    'calm_ground': 'Если хочешь, почувствуй, как ступни опираются на пол. Оглянись и назови три вещи, которые видишь. Не надо делать это идеально; можно остановиться.',
    'study_resistance': 'Не хочется начинать — такое бывает. Хочешь вместе придумать первый маленький шаг или пока не будем обсуждать учёбу?',
    'study_start': 'Ты можешь выбрать, с чего начать. Хочешь, я помогу наметить один маленький шаг?',
    'choice_offer': 'Как тебе удобнее: самому выбрать первое задание или вместе найти самый маленький шаг?',
    'small_step': 'Можно выбрать только первый шаг: открыть задание, прочитать один пункт или приготовить тетрадь. Какой вариант подходит тебе?',
    'english_input': 'Можно написать по-английски; я отвечаю по-русски. Я понимаю только знакомые мне фразы. Хочешь выбрать тему или попробовать короткую русскую фразу?',
    'russian_practice': 'Давай попробуем без оценок. Можно сказать: «Сегодня я…», а потом добавить одно слово о своём дне. Ошибаться разрешается.',
    'father_message': 'Для сообщения папе напиши текст внизу и нажми зелёную кнопку «Отправить папе». Разговор со мной сам по себе папе не отправляется.',
    'self_harm': 'Спасибо, что сказал об этом. Сейчас важно не оставаться одному: подойди к безопасному взрослому рядом и скажи прямо, что тебе нужна помощь. Если опасность прямо сейчас — попроси вызвать местную экстренную помощь. Я могу помочь подобрать слова взрослому.',
    'abuse': 'Ты не виноват, если кто-то делает тебе больно или пугает. Постарайся оказаться рядом с безопасным взрослым — например, учителем или родственником — и расскажи ему. Если опасность прямо сейчас, попроси экстренную помощь.',
    'immediate_danger': 'Если тебе сейчас опасно, трудно дышать или кто-то может причинить вред, сразу позови безопасного взрослого рядом. При непосредственной опасности попроси вызвать местную экстренную помощь. Не нужно разбираться с этим одному.',
}


class CatDialogue:
    def __init__(self, corpus_path=None, corpus=None, family_reply=None):
        self.path = Path(corpus_path) if corpus_path is not None else ROOT / 'assets' / ASSET_NAME
        self._provided = corpus
        self._intents = None
        self.family_reply = family_reply
        self.corpus_available = False
        self.reset()

    def reset(self):
        self.pending = None
        self.coaching_declined = False
        self.last_intent = None
        self._counts = {}
        self._last_response = None

    def _load(self):
        if self._intents is not None:
            return
        try:
            if self._provided is not None:
                value = self._provided
            else:
                no_links(self.path, True)
                with self.path.open('rb') as source:
                    data = source.read(MAX_CORPUS + 1)
                if len(data) > MAX_CORPUS:
                    raise ValueError('Corpus too large')
                value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_object)
            self._intents = validate_corpus(value)
            self.corpus_available = True
        except (OSError, ValueError, UnicodeError, TypeError):
            self._intents = {}

    def _result(self, intent, text=None, suggestions=None):
        self._load()
        row = self._intents.get(intent)
        if text is None:
            variants = row['responses'] if row else [FALLBACK.get(intent, FALLBACK['fallback'])]
            index = self._counts.get(intent, 0) % len(variants)
            if len(variants) > 1 and variants[index] == self._last_response:
                index = (index + 1) % len(variants)
            text = variants[index]
            self._counts[intent] = (index + 1) % len(variants)
        if suggestions is None:
            suggestions = row['suggestions'] if row else []
        if self.coaching_declined:
            suggestions = [item for item in suggestions if not any(word in normalize(item['text']) for word in ('урок', 'учеб', 'шаг', 'выдох', 'опор', 'упраж', 'подыш', 'calm', 'homework', 'study'))]
        self.last_intent, self._last_response = intent, text
        return {'text': text, 'intent': intent, 'suggestions': [dict(item) for item in suggestions],
                'language': 'ru', 'local_only': True, 'corpus_available': self.corpus_available}

    @staticmethod
    def _safety(value):
        if any(phrase in value for phrase in ('не могу дышать', 'мне сейчас опасно', 'мне угрожают', "i can't breathe", 'i cannot breathe', 'i am in danger', "i'm in danger")):
            return 'immediate_danger'
        if any(phrase in value for phrase in ('меня бьют', 'меня бьет', 'меня ударили', 'меня трогают', 'меня заставляют раздеваться', 'меня обижают дома', 'папа меня ударил', 'отец меня ударил', 'мама меня ударила', 'he hits me', 'she hits me', 'someone is hurting me', 'touches my private', 'dad hit me', 'father hit me', 'mom hit me')):
            return 'abuse'
        positive = value
        for protected in ('не хочу умереть', 'не хочу умирать', "don't want to die", 'do not want to die',
                          'не хочу убить себя', 'не хочу навредить себе', 'не хочу причинить себе вред',
                          "don't want to kill myself", 'do not want to kill myself',
                          "don't want to hurt myself", 'do not want to hurt myself'):
            positive = positive.replace(protected, ' ')
        if any(phrase in positive for phrase in ('хочу умереть', 'не хочу жить', 'я убью себя', 'хочу убить себя', 'покончу с собой', 'хочу покончить с собой', 'хочу навредить себе', 'собираюсь причинить себе вред', 'хочу причинить себе вред', 'хочу сделать себе больно', 'i want to die', 'i want to kill myself', 'i want to hurt myself', 'i am going to kill myself', 'i will kill myself', 'i want to end my life')):
            # Explicit positive intents above do not match "I don't want to die".
            return 'self_harm'
        return None

    @staticmethod
    def _match(value, pattern):
        wanted, tokens = normalize(pattern).split(), value.split()
        if not wanted:
            return False
        for start in range(len(tokens) - len(wanted) + 1):
            if all(actual == part or (len(part) >= 4 and re.fullmatch('[а-я]+', part) and actual.startswith(part))
                   for actual, part in zip(tokens[start:start + len(wanted)], wanted)):
                return True
        return False

    def _classify(self, value):
        if any(p in value for p in ('я сам решил', 'сам решил задачу', 'сам справился')):
            return 'achievement'
        if any(p in value for p in ('говорить по русски', 'давай по русски', 'практика русского', 'учить русский', 'russian practice')):
            return 'russian_practice'
        if any(p in value for p in ('не хочу умирать', 'не хочу умереть', 'не хочу убить себя', 'не хочу навредить себе', 'не хочу причинить себе вред', "don't want to die", 'do not want to die')):
            return 'worry'
        # Negation must start at a word boundary: "мне грустно" is not
        # "не грустно". Russian feeling stems intentionally allow inflection.
        negated = bool(re.search(r'\b(не (злюсь|сердит|груст|волную|боюсь)|not (angry|sad|worried|scared)|not feeling (angry|sad))', value))
        study = any(p in value for p in ('урок', 'домашн', 'учеб', 'задани', 'homework', 'studying', 'study', 'assignment'))
        if study and any(p in value for p in ('не хочу', 'не буду', 'ненавиж', "don't want", 'do not want', 'hate')):
            return 'study_resistance'
        if any(p in value for p in ('помоги успокоиться', 'help me calm', 'хочу успокоиться')):
            return 'calm_offer'
        if any(p in value for p in ('спокойный выдох', 'давай подышим', 'медленный выдох', 'breathing please')):
            return 'calm_breath'
        if any(p in value for p in ('почувствуем опору', 'почувствовать опору', 'grounding please')):
            return 'calm_ground'
        if any(p in value for p in ('помоги выбрать шаг', 'один маленький шаг', 'маленький шаг', 'help me start', 'давай начнем', 'давай начнем')):
            return 'small_step'
        candidates = sorted(self._intents.values(), key=lambda row: (-row['priority'], row['id']))
        for row in candidates:
            if row['id'] in SAFETY or row['id'] in {'stop', 'refusal'} or (negated and row['id'] in EMOTIONS):
                continue
            if any(self._match(value, pattern) for language in ('ru', 'en') for pattern in row['patterns'][language]):
                return row['id']
        if any(p in value for p in ('привет', 'здравств', 'hello', 'hi there', 'hey there')) or value in {'hi', 'hey'}:
            return 'greeting'
        if any(p in value for p in ('пап', 'telegram', 'телеграм', 'message dad')):
            return 'father_message'
        if not negated and any(p in value for p in ('злюсь', 'сердит', 'angry', 'mad at')):
            return 'anger'
        if not negated and any(p in value for p in ('тревож', 'страшно', 'боюсь', 'worried', 'scared', 'anxious')):
            return 'worry'
        if not negated and any(p in value for p in ('груст', 'sad', 'lonely')):
            return 'sadness'
        if study:
            return 'study_start'
        if re.search('[a-z]', value) and not re.search('[а-я]', value):
            return 'english_input'
        return 'fallback'

    def reply(self, text):
        if not isinstance(text, str) or len(text) > MAX_INPUT or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Напиши короткую фразу до 4000 знаков.')
        self._load()
        value = normalize(text)
        danger = self._safety(value)
        if danger:
            self.pending = None
            return self._result(danger, suggestions=[{'label': 'Как попросить помощи', 'text': 'Помоги попросить взрослого о помощи'}])
        if value in {'начать заново', 'новый разговор', 'reset chat'}:
            self.reset()
            return self._result('greeting')
        stop_row = self._intents.get('stop', {})
        corpus_stop = any(self._match(value, pattern) for language in ('ru', 'en')
                          for pattern in stop_row.get('patterns', {}).get(language, []))
        if (value in {'стоп', 'хватит', 'отстань', 'stop', 'stop please', 'go away'} or
                corpus_stop or any(p in value for p in ('оставь меня', 'не хочу разговаривать', 'не учи меня', 'не дави', 'leave me alone', "don't tell me what to do", "don't teach me"))):
            self.pending = None
            self.coaching_declined = True
            return self._result('stop', suggestions=[])
        negative = value.startswith(('не хочу', 'я не хочу', "i don't want", 'i do not want', 'нет спасибо'))
        protective_fear = any(p in value for p in ('не хочу умереть', 'не хочу умирать', 'не хочу убить себя', 'не хочу навредить себе', 'не хочу причинить себе вред', "don't want to die", 'do not want to die'))
        if value in DECLINE or (negative and not protective_fear and (self.pending or any(p in value for p in ('упраж', 'дыш', 'это', 'русск', 'exercise', 'this', 'russian')))):
            self.pending = None
            self.coaching_declined = True
            return self._result('refusal', suggestions=[])
        if self.family_reply is not None:
            try:
                family_text = self.family_reply(text)
            except Exception:
                family_text = None
            if isinstance(family_text, str) and family_text.strip():
                self.pending = None
                return self._result('family_fact', text=family_text[:700], suggestions=[])
        if self.pending and value in YES:
            pending = self.pending
            self.coaching_declined = False
            if pending.startswith('calm'):
                self.pending = 'calm_choice'
                return self._result('calm_offer', text='Выбирай, если тебе удобно: мягкий выдох или почувствовать опору под ногами. Можно передумать в любой момент.', suggestions=[
                    {'label': 'Мягкий выдох', 'text': 'Давай спокойный выдох'},
                    {'label': 'Почувствовать опору', 'text': 'Давай почувствуем опору'},
                    {'label': 'Не сейчас', 'text': 'Не сейчас'}])
            self.pending = 'study_choice'
            return self._result('choice_offer')
        if any(p in value for p in ('я сам решу', 'я сам выберу', 'сам выберу', 'не командуй', 'my choice', 'i decide')):
            self.pending = None
            self.coaching_declined = True
            return self._result('independence', suggestions=[])
        intent = self._classify(value)
        if self.pending == 'word' and len(value.split()) <= 3 and intent not in SAFETY:
            self.pending = None
            meaning = WORD_HELP.get(value)
            return self._result('word_meaning', text=meaning or 'Это слово я пока не могу объяснить достаточно точно. Лучше сверить значение со словарём или спросить взрослого; я не буду угадывать.', suggestions=[])
        if intent in {'fallback','english_input'} and self.pending in {'russian_story','story','idea'}:
            pending = self.pending
            self.pending = None
            if pending == 'russian_story':
                return self._result('russian_practice', text='Спасибо, что рассказал по-русски. Не обязательно говорить идеально, чтобы тебя услышали. Можно добавить ещё одно предложение или выбрать другую тему.', suggestions=[{'label':'Продолжу рассказ','text':'Продолжу рассказ'},{'label':'Другая тема','text':'Другая тема'}])
            if pending == 'story':
                return self._result('storytelling', text='Это твоё продолжение истории — его можно придумать по-своему. Какое решение выбрал бы твой герой? Можно и просто закончить историю здесь.', suggestions=[{'label':'Другая история','text':'Давай другую историю'},{'label':'На этом всё','text':'Стоп'}])
            return self._result('curiosity', text='Спасибо, что поделился своей идеей. Какой один вопрос тебе самому хотелось бы в ней проверить? Если я не знаю ответ, лучше вместе поискать надёжное объяснение со взрослым.', suggestions=[])
        explicit_help = any(p in value for p in ('помоги', 'хочу попробовать', 'давай', 'help me', 'please help'))
        if explicit_help:
            self.coaching_declined = False
        if intent in {'calm_breath', 'calm_ground'}:
            self.pending = None
            if self.coaching_declined:
                return self._result('refusal', suggestions=[])
            return self._result(intent)
        if intent == 'calm_offer':
            if self.coaching_declined:
                self.pending = None
                return self._result('refusal', suggestions=[])
            self.pending = 'calm_choice'
            return self._result('calm_offer', text='Хорошо, можем попробовать. Что тебе удобнее: мягкий выдох или почувствовать опору? Можно выбрать «не сейчас».', suggestions=[
                {'label': 'Мягкий выдох', 'text': 'Давай спокойный выдох'},
                {'label': 'Опора под ногами', 'text': 'Давай почувствуем опору'},
                {'label': 'Не сейчас', 'text': 'Не сейчас'}])
        if intent in EMOTIONS:
            if self.coaching_declined:
                self.pending = None
                return self._result(intent, text='Я услышал. Не буду предлагать упражнения или учёбу. Можно просто рассказать о том, что для тебя важно.', suggestions=[])
            self.pending = 'calm'
        elif intent in {'study_resistance', 'study_start', 'study_plan', 'small_step'}:
            if self.coaching_declined:
                self.pending = None
                return self._result('independence', text='Понял, сейчас не обсуждаем учёбу. Ты можешь сам вернуться к этой теме, когда захочешь.', suggestions=[])
            self.pending = 'study' if intent != 'small_step' else None
        elif intent == 'russian_practice':
            self.pending = 'russian_story'
        elif intent == 'word_meaning':
            self.pending = 'word'
        elif intent == 'storytelling':
            self.pending = 'story'
        elif intent == 'curiosity':
            self.pending = 'idea'
        else:
            self.pending = None
        if intent == 'english_input' and self.coaching_declined:
            return self._result(intent, text='Можно написать по-английски, а я отвечу по-русски. Не буду предлагать упражнения. О чём тебе хочется рассказать?', suggestions=[])
        if intent == 'fallback' and self.coaching_declined:
            return self._result(intent, text='Я не совсем понял эту фразу. Можно рассказать подробнее или выбрать другую тему. Советы и упражнения сейчас не предлагаю.', suggestions=[])
        return self._result(intent)

    def answer(self, text):
        return self.reply(text)['text']
