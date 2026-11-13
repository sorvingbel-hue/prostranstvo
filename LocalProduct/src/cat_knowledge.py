"""Small approved original knowledge pack; bounded local lexical retrieval.

Import and construction are inert. The first usable search reads one explicit
asset, never a directory inventory, raw corpus, chat log, network or model.
The validated pack is an immutable per-instance snapshot. Recreate the object
after an intentional approved-pack update; changes are never auto-imported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from urllib.parse import urlsplit


ASSET_NAME = 'cat-knowledge-approved-v1.json'
MAX_PACK_BYTES = 1024 * 1024
MAX_RECORDS = 100
MAX_TEXT = 700
MAX_QUERY = 4000
MAX_RESULTS = 3
ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
if not getattr(sys, 'frozen', False) and ROOT.name == 'src':
    ROOT = ROOT.parent
_ID = re.compile(r'[a-z][a-z0-9_]{1,63}\Z')
_WORDS = re.compile(r'[a-zа-я0-9]+')
_STOP = frozenset(('а', 'без', 'бы', 'был', 'была', 'быть', 'в', 'во', 'вот', 'все',
    'вы', 'да', 'для', 'до', 'его', 'ее', 'есть', 'же', 'и', 'или', 'к', 'как',
    'какая', 'какие', 'какой', 'когда', 'ли', 'мне', 'меня', 'мой', 'мы', 'на',
    'надо', 'нам', 'нас', 'не', 'нет', 'но', 'ну', 'о', 'об', 'он', 'она', 'они',
    'от', 'очень', 'по', 'пожалуйста', 'про', 'с', 'со', 'так', 'тебе', 'тебя',
    'то', 'ты', 'у', 'хочу', 'чем', 'что', 'чтобы', 'это', 'я', 'можно',
    'a', 'an', 'and', 'are', 'can', 'do', 'does', 'for', 'how', 'i', 'in', 'is',
    'it', 'me', 'my', 'of', 'on', 'please', 'the', 'to', 'what', 'you'))
_SHORT_PHRASES = frozenset(('нет', 'стоп', 'no', 'stop'))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_field')
        value[key] = item
    return value


def _constant(_):
    raise ValueError('nonfinite_number')


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value)):
        raise ValueError('invalid_text')
    value.encode('utf-8', 'strict')
    return value


def _normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    return ' '.join(_WORDS.findall(value))


def _tokens(value):
    return frozenset(token for token in _normalize(value).split()
                     if len(token) >= 2 and token not in _STOP)


def _url(value):
    _text(value, 600)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('invalid_reference')


def _no_links(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked_asset_path')


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_asset(path):
    path = path.absolute()
    _no_links(path)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size > MAX_PACK_BYTES:
        raise ValueError('invalid_asset_file')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _signature(initial) != _signature(opened):
            raise ValueError('asset_changed')
        body = stream.read(MAX_PACK_BYTES + 1)
        ended = os.fstat(stream.fileno())
        _no_links(path)
        current = path.lstat()
        if (len(body) != opened.st_size or len(body) > MAX_PACK_BYTES
                or _signature(opened) != _signature(ended)
                or _signature(opened) != _signature(current)
                or current.st_nlink != 1):
            raise ValueError('asset_changed')
    return json.loads(body.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)


def _validate(value):
    expected = {'version', 'pack_id', 'language', 'origin', 'approved',
                'source_refs', 'dataset_research_refs', 'records'}
    if (type(value) is not dict or set(value) != expected
            or type(value['version']) is not int or value['version'] != 1
            or value['pack_id'] != 'cat-knowledge-approved-v1' or value['language'] != 'ru'
            or value['origin'] != 'original_project' or value['approved'] is not True):
        raise ValueError('invalid_pack')
    refs = value['source_refs']
    if type(refs) is not list or not 1 <= len(refs) <= 10:
        raise ValueError('invalid_references')
    reference_ids = set()
    for reference in refs:
        if (type(reference) is not dict or set(reference) != {'id', 'url', 'role'}
                or not isinstance(reference['id'], str) or not _ID.fullmatch(reference['id'])
                or reference['id'] in reference_ids
                or reference['role'] != 'principle_reference_not_quoted'):
            raise ValueError('invalid_reference')
        _url(reference['url'])
        reference_ids.add(reference['id'])
    research = value['dataset_research_refs']
    if type(research) is not list or len(research) > 6:
        raise ValueError('invalid_research_references')
    for reference in research:
        if (type(reference) is not dict
                or set(reference) != {'id', 'url', 'status', 'runtime_approved', 'approval'}
                or not isinstance(reference['id'], str) or not _ID.fullmatch(reference['id'])
                or reference['id'] in reference_ids or reference['status'] != 'research_only'
                or reference['runtime_approved'] is not False or reference['approval'] != 'RAW_NOT_APPROVED'):
            raise ValueError('invalid_research_reference')
        _url(reference['url'])
        reference_ids.add(reference['id'])
    rows = value['records']
    if type(rows) is not list or not 1 <= len(rows) <= MAX_RECORDS:
        raise ValueError('invalid_record_count')
    approved, record_ids = [], set()
    for row in rows:
        if (type(row) is not dict or set(row) != {'id', 'text', 'keywords', 'source', 'approved'}
                or not isinstance(row['id'], str) or not _ID.fullmatch(row['id'])
                or row['id'] in record_ids or row['approved'] is not True
                or row['source'] != 'original_project'):
            raise ValueError('invalid_record')
        text = _text(row['text'], MAX_TEXT)
        if type(row['keywords']) is not list or not 1 <= len(row['keywords']) <= 20:
            raise ValueError('invalid_keywords')
        phrases = []
        for keyword in row['keywords']:
            phrase = _normalize(_text(keyword, 80))
            if not phrase or phrase in phrases:
                raise ValueError('invalid_keyword')
            phrases.append(phrase)
        keyword_tokens = frozenset(token for phrase in phrases for token in _tokens(phrase))
        if not keyword_tokens and not any(phrase in _SHORT_PHRASES for phrase in phrases):
            raise ValueError('empty_keywords')
        # Internal tuples/frozensets retain no mutable object from the parsed JSON.
        approved.append((row['id'], text, row['source'], tuple(phrases), keyword_tokens, _tokens(text)))
        record_ids.add(row['id'])
    return tuple(approved)


class ApprovedKnowledge:
    """Read a single explicitly approved asset lazily; return at most three hits.

    ``root`` is the directory holding ASSET_NAME. ``file`` optionally names one
    explicit asset; a relative file is joined to root when root is supplied.
    Bad input or any invalid/unapproved row fails closed without partial results.
    This is lexical matching, not semantic comprehension or a personal memory.
    """
    def __init__(self, root=None, file=None):
        self._loaded = False
        self._records = ()
        try:
            if file is None:
                self._path = (Path(root) if root is not None else ROOT / 'assets') / ASSET_NAME
            else:
                self._path = Path(file)
                if root is not None and not self._path.is_absolute():
                    self._path = Path(root) / self._path
        except (TypeError, ValueError, OSError):
            self._path = None

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            self._records = _validate(_read_asset(self._path)) if self._path is not None else ()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            self._records = ()

    def search(self, text, limit=3):
        if type(text) is not str or not text.strip() or len(text) > MAX_QUERY:
            return []
        if type(limit) is not int or limit < 1:
            return []
        try:
            text.encode('utf-8', 'strict')
            normalized = _normalize(text)
            if not normalized or len(normalized.split()) > 256:
                return []
            tokens = _tokens(normalized)
            self._load()
            scored = []
            padded = ' ' + normalized + ' '
            for identifier, body, source, phrases, keywords, body_tokens in self._records:
                exact = sum(1 for phrase in phrases if ' ' + phrase + ' ' in padded
                            and (_tokens(phrase) or phrase in _SHORT_PHRASES))
                keyword_matches = len(tokens & keywords)
                body_matches = len(tokens & body_tokens)
                # A lone incidental body word is not enough to pretend relevance.
                if not exact and not keyword_matches and body_matches < 2:
                    continue
                score = 20 * exact + 5 * keyword_matches + body_matches
                scored.append((-score, identifier, body, source))
            scored.sort()
            return [{'id': identifier, 'text': body, 'source': source, 'approved': True}
                    for _, identifier, body, source in scored[:min(limit, MAX_RESULTS)]]
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return []
