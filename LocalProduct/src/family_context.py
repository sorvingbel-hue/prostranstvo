"""Private local family facts and intentional kitten-chat history. No network."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from activity_core import no_links, exclusive_lock
from paths import DEPLOY_ROOT

BASE = DEPLOY_ROOT / 'data' / 'cat-companion'
APP_ID = 0x43415443
MAX_PROFILE = 32768
MAX_DB = 512 * 1024**2
ROLES = {'user', 'kitten', 'parent', 'system'}
KINDS = {'cat', 'family'}


class ContextError(ValueError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def empty_profile():
    return {'version': 1, 'updated_utc': None,
            'child': {'name': None, 'aliases': [], 'age': None, 'grade': None, 'school': None},
            'mother': {'name': None, 'age': None, 'occupation': None},
            'father': {'name': None, 'age': None, 'occupation': None}}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContextError('duplicate_profile_field')
        value[key] = item
    return value


def _text(value, maximum=160, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ContextError('invalid_profile_text')
    value = value.strip()
    if not value:
        return None if optional else ''
    return value


class FamilyContext:
    def __init__(self, root=None):
        self.root = Path(root if root is not None else BASE).absolute()
        self.file = self.root / 'family-profile.json'

    @staticmethod
    def validate(value):
        default = empty_profile()
        if not isinstance(value, dict) or set(value) != set(default) or type(value['version']) is not int or value['version'] != 1:
            raise ContextError('invalid_family_profile')
        result = empty_profile()
        stamp = value['updated_utc']
        if stamp is not None:
            if not isinstance(stamp, str) or len(stamp) > 64:
                raise ContextError('invalid_profile_time')
            try:
                if dt.datetime.fromisoformat(stamp).utcoffset() is None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ContextError('invalid_profile_time') from None
        result['updated_utc'] = stamp
        for person in ('child', 'mother', 'father'):
            record = value[person]
            if not isinstance(record, dict) or set(record) != set(default[person]):
                raise ContextError('invalid_family_member')
            for field in record:
                item = record[field]
                if field == 'age':
                    if item is not None and (type(item) is not int or not 0 <= item <= 120):
                        raise ContextError('invalid_family_age')
                elif field == 'aliases':
                    if not isinstance(item, list) or len(item) > 8:
                        raise ContextError('invalid_family_aliases')
                    item = [_text(x, 60, False) for x in item]
                    if any(not x for x in item) or len({x.casefold() for x in item}) != len(item):
                        raise ContextError('invalid_family_aliases')
                else:
                    item = _text(item, 160 if field in ('school', 'occupation') else 60)
                result[person][field] = item
        return result

    def profile(self):
        try:
            no_links(self.file, True)
            with self.file.open('rb') as stream:
                body = stream.read(MAX_PROFILE + 1)
            if len(body) > MAX_PROFILE:
                raise ContextError('family_profile_too_large')
            return self.validate(json.loads(body.decode('utf-8-sig'), object_pairs_hook=_object))
        except FileNotFoundError:
            return empty_profile()
        except (OSError, ValueError, UnicodeError):
            raise ContextError('family_profile_unavailable') from None

    def save(self, value):
        value = self.validate(value)
        value['updated_utc'] = now()
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'profile.lock', blocking=False):
            no_links(self.file, self.file.exists())
            fd, temporary = tempfile.mkstemp(prefix='.profile-', suffix='.tmp', dir=self.root)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.flush(); os.fsync(stream.fileno())
                no_links(self.file, self.file.exists())
                os.replace(temporary, self.file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return value

    def family_reply(self, text):
        if not isinstance(text, str) or len(text) > 4000:
            return None
        value = text.casefold().replace('ё', 'е')
        # A family/school word inside a feeling or opinion is not a fact query.
        if re.search(r'ненавиж|бесит|груст|злю|злит|устал|боюсь|страш|\bhate\b|\bsad\b|\bangry\b|\bupset\b|\bscared\b', value):
            return None
        age = bool(re.search(r'сколько.{0,35}лет|возраст|how old|\bage\b', value))
        name = bool(re.search(r'как.{0,25}зовут|\bимя\b|\bname\b|кто.{0,15}(мама|папа)', value))
        school = bool(re.search(r'школ|\bschool\b|где.{0,15}учусь', value)) and bool(re.search(r'в какой|какая.{0,12}школ|назван|как.{0,15}называ|где.{0,15}учусь|\b(which|what|where).{0,25}school', value))
        grade = bool(re.search(r'в каком.{0,15}класс|какой.{0,15}класс|\b(what|which).{0,15}grade', value))
        job = bool(re.search(r'(где|кем|чем).{0,25}работ|чем.{0,25}занима|какая.{0,25}(работ|профес)|\bwhat.{0,30}(job|occupation|does)|\bwhere.{0,30}work', value))
        summary = bool(re.search(r'(расскажи|кто).{0,25}семь|tell.{0,25}family', value))
        if not any((age, name, school, grade, job, summary)):
            return None
        try:
            profile = self.profile()
        except ContextError:
            return 'Семейная память сейчас недоступна. Лучше уточнить у мамы или папы.'
        if summary:
            facts = []
            for person, label in (('child', 'Ты'), ('mother', 'Мама'), ('father', 'Папа')):
                item = profile[person]
                if item['name']:
                    facts.append(label + ' — ' + item['name'] + (', возраст: ' + str(item['age']) if item['age'] is not None else ''))
            return '. '.join(facts) + '.' if facts else 'Родители ещё не заполнили семейную память. Я не буду придумывать сведения о семье.'
        person = None
        for key, pattern in (('mother', r'мам|мать|\bmom\b|\bmother\b'), ('father', r'пап|отец|\bdad\b|\bfather\b')):
            aliases = [profile[key]['name']] if profile[key]['name'] else []
            if re.search(pattern, value) or any(re.search(r'\b' + re.escape(x.casefold().replace('ё', 'е')) + r'\b', value) for x in aliases):
                person = key
                break
        if person is None and (school or grade or re.search(r'\bмне\b|\bменя\b|\bмое\b|\bмой\b|\bя\b|\bi\b|\bmy\b|\bme\b', value)):
            person = 'child'
        if person is None:
            for alias in [profile['child']['name'], *profile['child']['aliases']]:
                if alias and re.search(r'\b' + re.escape(alias.casefold().replace('ё', 'е')) + r'\b', value):
                    person = 'child'; break
        if person is None:
            return None
        item = profile[person]
        label = {'child': 'Твой', 'mother': 'Мамин', 'father': 'Папин'}[person]
        if age:
            return label + ' возраст в семейной записи: ' + str(item['age']) + '.' if item['age'] is not None else 'Точный возраст я пока не знаю. Можно уточнить у родителей.'
        if name:
            if not item['name']:
                return 'Имя пока не записано. Родители могут добавить его в семейную память.'
            return {'child': 'Тебя зовут ', 'mother': 'Маму зовут ', 'father': 'Папу зовут '}[person] + item['name'] + '.'
        field = 'school' if school else 'grade' if grade else 'occupation' if job else None
        if field is None or field not in item:
            return None
        fact = item[field]
        if not fact:
            return {'school': 'В какой школе ты учишься, мне пока не рассказали.', 'grade': 'Точный класс пока не записан. Я не буду угадывать.', 'occupation': 'Чем занимается этот член семьи, мне пока не рассказали.'}[field]
        return {'school': 'Твоя школа: ', 'grade': 'Твой класс: ', 'occupation': 'В семейной памяти записано: '}[field] + fact + '.'


class ChatLog:
    """Separate append-only local history, with explicit JSONL export for review."""
    def __init__(self, root=None):
        self.root = Path(root if root is not None else BASE / 'chat-logs').absolute()
        self.db = self.root / 'chat-history.sqlite'

    @contextmanager
    def _connect(self, write=True):
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for suffix in ('', '-journal', '-wal', '-shm'):
            p = Path(str(self.db) + suffix)
            no_links(p, p.exists())
        if self.db.exists() and self.db.stat().st_size > MAX_DB:
            raise ContextError('chat_history_full')
        connection = sqlite3.connect(self.db, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('BEGIN IMMEDIATE')
            ident = connection.execute('PRAGMA application_id').fetchone()[0]
            if ident == 0:
                if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ContextError('unexpected_chat_database')
                connection.execute('PRAGMA application_id=' + str(APP_ID))
                connection.execute('PRAGMA user_version=1')
                connection.execute('CREATE TABLE entries(id TEXT PRIMARY KEY,created_utc TEXT NOT NULL,role TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,source_key TEXT UNIQUE,session_id TEXT,person_verified INTEGER NOT NULL)')
            if connection.execute('PRAGMA application_id').fetchone()[0] != APP_ID or connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ContextError('unexpected_chat_database')
            connection.commit()
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(record):
        if not isinstance(record, dict) or set(record) - {'role', 'text', 'kind', 'message_id', 'session_id'}:
            raise ContextError('invalid_chat_record')
        role, text, kind = record.get('role'), record.get('text'), record.get('kind', 'cat')
        if role not in ROLES or kind not in KINDS or not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\x00' in text:
            raise ContextError('invalid_chat_record')
        ids = []
        for key in ('message_id', 'session_id'):
            value = record.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', value)):
                raise ContextError('invalid_chat_id')
            ids.append(value)
        return role, text, kind, ids[0], ids[1]

    def append(self, role, text, kind='cat', message_id=None, session_id=None):
        return self.append_many([dict(role=role, text=text, kind=kind, message_id=message_id, session_id=session_id)])[0]

    def append_many(self, records):
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise ContextError('invalid_chat_batch')
        values = [self._record(record) for record in records]
        receipts = []
        try:
            with self._connect() as connection:
                for role, text, kind, message_id, session in values:
                    key = kind + ':' + role + ':' + message_id if message_id else None
                    old = connection.execute('SELECT id,created_utc,text FROM entries WHERE source_key=?', (key,)).fetchone() if key else None
                    if old:
                        if old['text'] != text:
                            raise ContextError('chat_source_changed')
                        receipts.append(dict(id=old['id'], created_utc=old['created_utc'], inserted=False))
                        continue
                    ident, stamp = uuid.uuid4().hex, now()
                    connection.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)', (ident, stamp, role, kind, text, key, session, 0))
                    receipts.append(dict(id=ident, created_utc=stamp, inserted=True))
        except (sqlite3.Error, OSError):
            raise ContextError('chat_history_unavailable') from None
        return receipts

    def export_jsonl(self):
        target = self.root / ('history-export-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.jsonl')
        try:
            with self._connect(write=False) as connection, target.open('x', encoding='utf-8') as output:
                for row in connection.execute('SELECT id,created_utc,role,kind,text,session_id,person_verified FROM entries ORDER BY created_utc,id'):
                    output.write(json.dumps(dict(row), ensure_ascii=False) + '\n')
                output.flush(); os.fsync(output.fileno())
            no_links(target, True)
        except (OSError, sqlite3.Error):
            raise ContextError('chat_export_unavailable') from None
        return target
