"""Read-only study timer observations for the kitten; never starts/stops lessons.

A saved ordinary study lease is not proof that its closing worker is running.
This module never claims enforcement or attributes messages to a parent.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import time

from activity_core import no_links
from study_resume import _validate
import paths

MAX_LEASE_BYTES = 2048
FIVE_MINUTES = 300


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate study field')
        result[key] = value
    return result


class CatStudyMonitor:
    """Bounded local status and once-per-process timer notices.

    Construction/import is inert. ``poll`` returns a status plus new events.
    The same lease can only lose time in this observer: clock rollback cannot
    extend it, expiry cannot resurrect it, and parent extensions use a new ID.
    A bad/unreadable file reports unknown state, never "lessons ended".
    """
    def __init__(self, root=None, clock=time.time, monotonic=time.monotonic, reader=None):
        self.root = Path(root) if root is not None else paths.DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'active-session.json'
        self.clock, self.monotonic = clock, monotonic
        self.reader = reader
        self._lease = None
        self._mono_deadline = None
        self._last_mono = None
        self._published = None
        self._five_sent = set()
        self._ended_sent = set()

    def _read(self):
        if self.reader is not None:
            value = self.reader()
            return None if value is None else _validate(value)
        try:
            no_links(self.path, True)
            source = self.path.open('rb')
        except FileNotFoundError:
            return None
        with source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_LEASE_BYTES:
                raise ValueError('Invalid study file')
            body = source.read(MAX_LEASE_BYTES + 1)
            if len(body) > MAX_LEASE_BYTES:
                raise ValueError('Study file too large')
            no_links(self.path, True)
            current = self.path.stat()
            if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns):
                raise ValueError('Study file changed while reading')
        return _validate(json.loads(body.decode('utf-8-sig'), object_pairs_hook=_object))

    @staticmethod
    def _base(state, active, **values):
        return dict(source='ordinary_study_lease', state=state, active=active,
                    enforcement_confirmed=False, **values)

    def snapshot(self):
        try:
            now, mono = self.clock(), self.monotonic()
            if (type(now) not in (int, float) or type(mono) not in (int, float)
                    or not math.isfinite(now) or not math.isfinite(mono)
                    or now <= 0 or mono < 0 or (self._last_mono is not None and mono < self._last_mono)):
                raise ValueError('Clock unavailable')
            self._last_mono = mono
            lease = self._read()
            if lease is None:
                return self._base('idle', False, session_id=None, remaining_seconds=0,
                                  expires_utc=None, duration_seconds=0)
            expiry = lease['expires_utc']
            remaining = None
            if expiry is not None:
                remaining = min(lease['duration_seconds'], max(0, math.ceil(expiry - now)))
            if self._lease is None or self._lease['session_id'] != lease['session_id']:
                self._lease = dict(lease)
                self._mono_deadline = None if remaining is None else mono + remaining
            elif lease != self._lease:
                # The owning runtime replaces the ID when extending/restarting.
                raise ValueError('Same study ID was edited')
            if remaining is None:
                return self._base('active', True, session_id=lease['session_id'],
                                  remaining_seconds=None, expires_utc=None, duration_seconds=0)
            remaining = min(remaining, max(0, math.ceil(self._mono_deadline - mono)))
            if remaining == 0:
                self._mono_deadline = min(self._mono_deadline, mono)
            return self._base('active' if remaining else 'expired', bool(remaining),
                              session_id=lease['session_id'], remaining_seconds=remaining,
                              expires_utc=lease['expires_utc'], duration_seconds=lease['duration_seconds'])
        except Exception:
            # No source values or exception payloads are exposed to a child UI.
            return self._base('unavailable', None, session_id=None, remaining_seconds=None,
                              expires_utc=None, duration_seconds=None,
                              error='study_state_unavailable')

    @staticmethod
    def _event(kind, session_id, remaining, text):
        return dict(id=session_id + ':' + kind, kind=kind, session_id=session_id,
                    remaining_seconds=remaining, text=text, author='local_study_timer')

    def poll(self):
        status = self.snapshot()
        events = []
        state, session_id = status['state'], status['session_id']
        previous = self._published
        if state == 'unavailable':
            # Keep prior observation for recovery; an error must not emit an end.
            return dict(status=status, events=events)
        if state == 'active':
            remaining = status['remaining_seconds']
            if previous is None or previous['session_id'] != session_id:
                events.append(self._event('study_started', session_id, remaining,
                              'Сейчас время уроков. Я рядом: можно написать папе.'))
                if remaining is not None and remaining <= FIVE_MINUTES:
                    self._five_sent.add(session_id)
            elif (remaining is not None and previous['state'] == 'active'
                  and previous['remaining_seconds'] is not None and previous['remaining_seconds'] > FIVE_MINUTES
                  and remaining <= FIVE_MINUTES and session_id not in self._five_sent):
                events.append(self._event('study_five_minutes', session_id, remaining,
                              'До конца таймера уроков осталось не больше пяти минут.'))
                self._five_sent.add(session_id)
        elif state == 'expired' and previous is not None and previous['session_id'] == session_id:
            if previous['state'] == 'active' and session_id not in self._ended_sent:
                events.append(self._event('study_ended', session_id, 0,
                              'Время по таймеру уроков истекло. Дальнейший план можно уточнить у родителей.'))
                self._ended_sent.add(session_id)
        elif state == 'idle' and previous is not None and previous['state'] == 'active':
            text = ('Сохранённый режим уроков больше не активен.' if previous['remaining_seconds'] is None
                    else 'Сохранённый таймер уроков завершён или сброшен.')
            events.append(self._event('study_cleared', previous['session_id'], None,
                          text + ' Если нужно, уточни у родителей.'))
        self._published = dict(status)
        # Only bounded process-local de-duplication; no files are created.
        if len(self._five_sent) > 32:
            self._five_sent = {session_id} if session_id in self._five_sent else set()
        if len(self._ended_sent) > 32:
            self._ended_sent = {session_id} if session_id in self._ended_sent else set()
        return dict(status=status, events=events)
