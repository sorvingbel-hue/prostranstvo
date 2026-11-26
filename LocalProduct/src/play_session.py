"""Durable, opt-in game allowance state. No GUI, registry or Study mutation.

The parent-authorized caller owns StudyResume and its startup entry. This store
only describes identity-checked transitions; it cannot grant authority itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links
import paths

MAX_BYTES = 256 * 1024
MAX_APPROVALS = 2048
ACTIVE = frozenset({'pending_release', 'playing', 'return_requested'})
PHASES = ACTIVE | {'completed', 'cancelled'}
RESULTS = {None, 'elapsed', 'clock_rollback', 'study_replaced_before_release',
           'study_started_during_play', 'restrictions_already_present',
           'return_confirmed', 'parent_cancelled', 'parent_study_started'}
ID = re.compile(r'[0-9a-f]{32}\Z')


class PlaySessionError(ValueError):
    pass


class PlayConflict(PlaySessionError):
    pass


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _date(value):
    try:
        if type(value) is str and date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise PlaySessionError('Invalid plan date')


def _identifier(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or ID.fullmatch(value) is None:
        raise PlaySessionError('Invalid session identity')
    return value


def approval_key(plan_date, revision):
    _date(plan_date)
    if type(revision) is not int or not 0 < revision < 2**128:
        raise PlaySessionError('Invalid approval revision')
    return f'{plan_date}:{revision}'


def _key(value):
    if type(value) is not str:
        raise PlaySessionError('Invalid redeemed approval')
    try:
        day, revision = value.rsplit(':', 1)
        expected = approval_key(day, int(revision))
    except (ValueError, TypeError):
        raise PlaySessionError('Invalid redeemed approval') from None
    if expected != value:
        raise PlaySessionError('Invalid redeemed approval')


def _initial():
    return {'version': 1, 'redeemed': [], 'current': None}


def _validate(state):
    if type(state) is not dict or set(state) != {'version', 'redeemed', 'current'}:
        raise PlaySessionError('Invalid play state')
    if type(state['version']) is not int or state['version'] != 1:
        raise PlaySessionError('Unsupported play state version')
    used = state['redeemed']
    if type(used) is not list or len(used) > MAX_APPROVALS:
        raise PlaySessionError('Invalid redeemed approvals')
    for value in used:
        _key(value)
    if len(set(used)) != len(used):
        raise PlaySessionError('Duplicate redeemed approval')
    current = state['current']
    if current is None:
        return state
    expected = {'play_id', 'approval_key', 'plan_date', 'plan_revision', 'linked_study_id',
                'duration_seconds', 'created_utc', 'expires_utc', 'max_seen_utc',
                'remaining_seconds', 'phase', 'return_study_id', 'result'}
    if type(current) is not dict or set(current) != expected:
        raise PlaySessionError('Invalid current play record')
    _identifier(current['play_id'])
    _identifier(current['linked_study_id'], True)
    _identifier(current['return_study_id'], True)
    if current['approval_key'] != approval_key(current['plan_date'], current['plan_revision']):
        raise PlaySessionError('Approval identity mismatch')
    if current['approval_key'] not in used:
        raise PlaySessionError('Current approval missing from durable ledger')
    duration, remaining = current['duration_seconds'], current['remaining_seconds']
    if type(duration) is not int or duration % 60 or not 60 <= duration <= 7200:
        raise PlaySessionError('Invalid play duration')
    if type(remaining) is not int or not 0 <= remaining <= duration:
        raise PlaySessionError('Invalid remaining play time')
    if any(not _number(current[key]) for key in ('created_utc', 'expires_utc', 'max_seen_utc')):
        raise PlaySessionError('Invalid play clock')
    if (current['expires_utc'] != current['created_utc'] + duration or
            current['max_seen_utc'] < current['created_utc']):
        raise PlaySessionError('Invalid fixed play deadline')
    if (type(current['phase']) is not str or current['phase'] not in PHASES or
            (current['result'] is not None and type(current['result']) is not str) or
            current['result'] not in RESULTS):
        raise PlaySessionError('Invalid play phase')
    if current['phase'] == 'return_requested' and (remaining or current['return_study_id'] is None):
        raise PlaySessionError('Incomplete return request')
    if current['phase'] == 'return_requested' and current['result'] not in ('elapsed', 'clock_rollback'):
        raise PlaySessionError('Invalid return reason')
    if current['phase'] in ('pending_release', 'playing') and (
            current['return_study_id'] is not None or current['result'] is not None):
        raise PlaySessionError('Invalid active play record')
    if current['phase'] in ('completed', 'cancelled') and current['result'] is None:
        raise PlaySessionError('Missing terminal result')
    return state


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PlaySessionError('Duplicate state key')
        value[key] = item
    return value


class PlaySession:
    def __init__(self, root=None, *, clock=time.time, monotonic=time.monotonic,
                 today=date.today, nonce=lambda: uuid.uuid4().hex):
        self.root = Path(root) if root is not None else paths.DEPLOY_ROOT / 'data' / 'play-time'
        self.path = self.root / 'session.json'
        self.clock, self.monotonic, self.today, self.nonce = clock, monotonic, today, nonce
        self._anchor = None

    def read(self):
        try:
            with no_links(self.path, True).open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return _initial()
        if len(raw) > MAX_BYTES:
            raise PlaySessionError('Play record is too large')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            return _validate(value)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise PlaySessionError('Play record is damaged; allowance not granted') from error

    def _write(self, state):
        raw = json.dumps(_validate(state), ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise PlaySessionError('Play record storage is full')
        descriptor, temporary = tempfile.mkstemp(prefix='.play-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(self.path, self.path.exists())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _transaction(self):
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'session.lock'):
            state = self.read()
            previous = deepcopy(state)
            yield state
            if state != previous:
                self._write(state)

    def _now(self):
        now, mono = self.clock(), self.monotonic()
        if not _number(now) or type(mono) not in (int, float) or not math.isfinite(mono) or mono < 0:
            raise PlaySessionError('Clock unavailable; allowance not granted')
        return now, mono

    @staticmethod
    def _current(state, play_id):
        _identifier(play_id)
        current = state['current']
        if current is None or current['play_id'] != play_id:
            raise PlayConflict('A different play session is current')
        return current

    def _view(self, state):
        current = deepcopy(state['current'])
        action = 'none'
        if current is not None:
            action = {'pending_release': 'release_linked_study', 'playing': 'wait',
                      'return_requested': 'restore_study', 'completed': 'none',
                      'cancelled': 'none'}[current['phase']]
        return {'current': current, 'action': action,
                'requires_startup': current is not None and current['phase'] in ACTIVE}

    def status(self):
        """Pure last durable state; use reconcile for deadline progression."""
        return self._view(self.read())

    def prepare(self, plan_date, revision, minutes, linked_study_id, *, current_study_id):
        key = approval_key(plan_date, revision)
        if type(minutes) is not int or not 1 <= minutes <= 120:
            raise PlaySessionError('Play time must be 1..120 minutes')
        _identifier(linked_study_id, True)
        _identifier(current_study_id, True)
        if linked_study_id != current_study_id:
            # A retry after an already-cleared lease may return its existing
            # record, but an initial stale binding must not create state.
            previous = self.read()['current']
            if previous is None or previous['approval_key'] != key:
                raise PlayConflict('The parent-approved Study session changed')
        if plan_date != self.today().isoformat():
            raise PlayConflict('Only the current day may grant play time')
        now, _ = self._now()
        with self._transaction() as state:
            current = state['current']
            if key in state['redeemed']:
                if current is None or current['approval_key'] != key:
                    raise PlayConflict('This approval was already used')
                if (current['duration_seconds'] != minutes * 60 or
                        current['linked_study_id'] != linked_study_id):
                    raise PlayConflict('An already-used approval cannot be changed')
                return self._view(state)
            if linked_study_id != current_study_id:
                raise PlayConflict('The parent-approved Study session changed')
            if current is not None and current['phase'] in ACTIVE:
                raise PlayConflict('Another play allowance is still active')
            if len(state['redeemed']) >= MAX_APPROVALS:
                raise PlaySessionError('Approval ledger is full; existing records preserved')
            ident = _identifier(self.nonce())
            duration = minutes * 60
            state['redeemed'].append(key)
            state['current'] = {'play_id':ident, 'approval_key':key, 'plan_date':plan_date,
                'plan_revision':revision, 'linked_study_id':linked_study_id,
                'duration_seconds':duration, 'created_utc':now, 'expires_utc':now+duration,
                'max_seen_utc':now, 'remaining_seconds':duration, 'phase':'pending_release',
                'return_study_id':None, 'result':None}
        self._anchor = None
        return self._view(state)

    def _advance(self, current, actual):
        if current['phase'] not in ACTIVE:
            return
        now, mono = self._now()
        previous = current['remaining_seconds']
        wall_left = max(0, math.ceil(current['expires_utc'] - max(now, current['max_seen_utc'])))
        remaining = min(previous, wall_left)
        rollback = now < current['max_seen_utc'] - 2
        if self._anchor is not None and self._anchor[0] == current['play_id']:
            _, start_mono, start_remaining = self._anchor
            if mono < start_mono:
                rollback = True
            else:
                remaining = min(remaining, max(0, start_remaining - math.floor(mono-start_mono)))
        else:
            self._anchor = current['play_id'], mono, remaining
        current['max_seen_utc'] = max(now, current['max_seen_utc'])
        if rollback:
            remaining = 0
        current['remaining_seconds'] = remaining
        phase = current['phase']
        if phase == 'pending_release' and actual is not None and actual != current['linked_study_id']:
            current.update(phase='cancelled', result='study_replaced_before_release')
            return
        if phase == 'playing' and actual is not None:
            current.update(phase='cancelled', result='study_started_during_play')
            return
        if phase == 'return_requested':
            if actual is not None:
                result = 'return_confirmed' if actual == current['return_study_id'] else 'restrictions_already_present'
                current.update(phase='completed', result=result)
            return
        if remaining <= 0:
            if actual is not None:
                current.update(phase='completed', result='restrictions_already_present')
            else:
                current.update(phase='return_requested', return_study_id=_identifier(self.nonce()),
                               result='clock_rollback' if rollback else 'elapsed')
        elif phase == 'pending_release' and actual is None:
            current['phase'] = 'playing'

    def reconcile(self, *, current_study_id):
        """Persist clock progress and return a plan, never touches Study itself."""
        _identifier(current_study_id, True)
        # An idle read must not create an application data directory.
        if self.read()['current'] is None:
            return self.status()
        with self._transaction() as state:
            self._advance(state['current'], current_study_id)
        return self._view(state)

    def confirm_release(self, play_id, *, current_study_id):
        """After caller's exact-ID clear; also recovers an interrupted handoff."""
        _identifier(current_study_id, True)
        with self._transaction() as state:
            current = self._current(state, play_id)
            self._advance(current, current_study_id)
        return self._view(state)

    def prepare_return(self, play_id, *, current_study_id):
        """Return stable proposed Study ID only after expiration/revocation.

        Caller must create that lease with an empty-slot CAS. Never overwrite
        a current Study lease, even if the lease changed after this method.
        """
        _identifier(current_study_id, True)
        with self._transaction() as state:
            current = self._current(state, play_id)
            self._advance(current, current_study_id)
            if current['phase'] not in ('return_requested', 'completed', 'cancelled'):
                raise PlayConflict('Play time has not expired')
        return self._view(state)

    def confirm_return(self, play_id, *, current_study_id):
        _identifier(current_study_id)
        with self._transaction() as state:
            current = self._current(state, play_id)
            if current['phase'] in ('completed', 'cancelled'):
                return self._view(state)
            if current['phase'] != 'return_requested':
                raise PlayConflict('No matching return request')
            current.update(phase='completed', result='return_confirmed'
                if current_study_id == current['return_study_id'] else 'restrictions_already_present')
        return self._view(state)

    def cancel(self, play_id, reason='parent_cancelled'):
        """Parent-authorized cancellation only; no Study lease is cleared."""
        if reason not in ('parent_cancelled', 'parent_study_started'):
            raise PlaySessionError('Invalid parent cancellation reason')
        with self._transaction() as state:
            current = self._current(state, play_id)
            if current['phase'] in ACTIVE:
                current.update(phase='cancelled', result=reason)
        return self._view(state)
