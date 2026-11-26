"""Pure opt-in hourly chores cycle. No GUI, network, Play or Study mutation.

The owning caller authenticates a parent and serializes cross-store work under
the same approval lock. This JSON store is not a Windows security boundary.
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

MAX_BYTES = 512 * 1024
MAX_SEEN = 4096
PLAY_MINUTES = 60
BREAK_SECONDS = 600
ID = re.compile(r'[0-9a-f]{32}\Z')
PHASES = {'waiting', 'submitted', 'approved', 'consumed', 'cancelled', 'blocked'}
ACTIVE = {'waiting', 'submitted', 'approved'}
RESULTS = {None, 'parent_cancelled', 'disabled', 'study_replaced', 'clock_rollback', 'approval_day_changed'}
DEFAULT_TASK = 'Небольшая уборка в комнате: спроси у мамы, что сейчас действительно нужно сделать.'


class HourlyBreakError(ValueError):
    pass


class HourlyBreakConflict(HourlyBreakError):
    pass


def _id(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or not ID.fullmatch(value):
        raise HourlyBreakError('Invalid identity')
    return value


def _integer(value, low=0, high=2**128-1):
    if type(value) is not int or not low <= value <= high:
        raise HourlyBreakError('Invalid integer')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _task(value):
    if type(value) is not str or value != value.strip() or not 1 <= len(value) <= 240 or not value.isprintable():
        raise HourlyBreakError('Use one useful task in a short printable line')
    return value


def _day(value):
    try:
        if type(value) is str and date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise HourlyBreakError('Invalid grant day')


def _initial():
    return {'version':1, 'revision':0, 'generation':0,
            'config':{'enabled':False, 'task':DEFAULT_TASK, 'play_minutes':PLAY_MINUTES,
                      'break_seconds':BREAK_SECONDS, 'enabled_utc':None},
            'seen_play_ids':[], 'current':None}


def _validate(state):
    if type(state) is not dict or set(state) != set(_initial()):
        raise HourlyBreakError('Invalid cycle state')
    if type(state['version']) is not int or state['version'] != 1:
        raise HourlyBreakError('Unsupported cycle version')
    _integer(state['revision']); _integer(state['generation'])
    config = state['config']
    if type(config) is not dict or set(config) != set(_initial()['config']):
        raise HourlyBreakError('Invalid cycle configuration')
    if (type(config['enabled']) is not bool or type(config['play_minutes']) is not int or
            config['play_minutes'] != PLAY_MINUTES or type(config['break_seconds']) is not int or
            config['break_seconds'] != BREAK_SECONDS or
            (config['enabled_utc'] is not None and not _number(config['enabled_utc'])) or
            (config['enabled'] and config['enabled_utc'] is None)):
        raise HourlyBreakError('Invalid cycle timing')
    _task(config['task'])
    seen = state['seen_play_ids']
    if type(seen) is not list or len(seen) > MAX_SEEN:
        raise HourlyBreakError('Cycle ledger is full or damaged')
    for value in seen:_id(value)
    if len(set(seen)) != len(seen):raise HourlyBreakError('Duplicate cycle identity')
    current = state['current']
    if current is None:return state
    keys = {'break_id','generation','source_play_id','study_id','task','started_utc','ready_utc',
            'max_seen_utc','remaining_seconds','phase','requested_utc','approved_utc',
            'approval_revision','grant_date','grant_revision','next_play_id','result'}
    if type(current) is not dict or set(current) != keys:
        raise HourlyBreakError('Invalid break record')
    for key in ('break_id','source_play_id','study_id'):_id(current[key])
    _id(current['next_play_id'], True)
    _integer(current['generation']); _integer(current['grant_revision'],1)
    if current['generation'] > state['generation'] or current['source_play_id'] not in seen:
        raise HourlyBreakError('Break generation or source mismatch')
    _task(current['task']); _integer(current['remaining_seconds'],0,BREAK_SECONDS)
    if (any(not _number(current[key]) for key in ('started_utc','ready_utc','max_seen_utc')) or
            current['ready_utc'] != current['started_utc'] + BREAK_SECONDS or
            current['max_seen_utc'] < current['started_utc']):
        raise HourlyBreakError('Invalid fixed break deadline')
    phase = current['phase']
    if (type(phase) is not str or phase not in PHASES or
            (current['result'] is not None and type(current['result']) is not str) or
            current['result'] not in RESULTS):
        raise HourlyBreakError('Invalid break phase')
    if phase in ACTIVE and (not config['enabled'] or current['generation'] != state['generation']):
        raise HourlyBreakError('Inactive generation cannot authorize play')
    for key in ('requested_utc','approved_utc'):
        if current[key] is not None and not _number(current[key]):raise HourlyBreakError('Invalid parent timing')
    if current['approval_revision'] is not None:_integer(current['approval_revision'])
    if current['grant_date'] is not None:_day(current['grant_date'])
    if phase in ('submitted','approved','consumed') and (current['requested_utc'] is None or current['remaining_seconds'] != 0):
        raise HourlyBreakError('Unfinished break cannot be submitted')
    if phase in ('approved','consumed') and (current['approved_utc'] is None or current['grant_date'] is None or current['approval_revision'] is None):
        raise HourlyBreakError('Missing durable parent approval')
    if phase == 'consumed' and current['next_play_id'] is None:raise HourlyBreakError('Missing next game identity')
    if phase in ('blocked','cancelled') and current['result'] is None:raise HourlyBreakError('Missing stop reason')
    return state


def _pairs(items):
    value = {}
    for key,item in items:
        if key in value:raise HourlyBreakError('Duplicate state key')
        value[key] = item
    return value


class HourlyBreakStore:
    def __init__(self, root, *, clock=time.time, monotonic=time.monotonic,
                 today=date.today, nonce=lambda:uuid.uuid4().hex):
        self.root = Path(root)
        self.path = self.root / 'cycle.json'
        self.clock, self.monotonic, self.today, self.nonce = clock, monotonic, today, nonce
        self._anchor = None

    def read(self):
        try:
            with no_links(self.path, True).open('rb') as stream:raw = stream.read(MAX_BYTES+1)
        except FileNotFoundError:return _initial()
        if len(raw) > MAX_BYTES:raise HourlyBreakError('Cycle state is too large')
        try:
            return _validate(json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                parse_constant=lambda _:(_ for _ in ()).throw(ValueError('constant'))))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise HourlyBreakError('Cycle state is damaged; next game is not authorized') from error

    def _write(self, state):
        raw = json.dumps(_validate(state),ensure_ascii=True,separators=(',',':')).encode('utf-8')
        if len(raw)>MAX_BYTES:raise HourlyBreakError('Cycle storage limit reached')
        descriptor,name = tempfile.mkstemp(prefix='.hourly-',suffix='.tmp',dir=self.root)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            no_links(self.path,self.path.exists());os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    @contextmanager
    def _change(self):
        no_links(self.root).mkdir(parents=True,exist_ok=True);no_links(self.root)
        with exclusive_lock(self.root/'cycle.lock'):
            state=self.read();before=deepcopy(state)
            yield state
            if state != before:self._write(state)

    def _now(self):
        now,mono=self.clock(),self.monotonic()
        if not _number(now) or type(mono) not in (int,float) or not math.isfinite(mono) or mono<0:
            raise HourlyBreakError('Clock unavailable')
        return now,mono

    @staticmethod
    def _expected(state, revision):
        _integer(revision)
        if state['revision'] != revision:raise HourlyBreakConflict('Cycle changed; refresh the view')

    @staticmethod
    def _current(state, break_id):
        _id(break_id)
        current=state['current']
        if current is None or current['break_id'] != break_id:raise HourlyBreakConflict('A different break is current')
        return current

    @staticmethod
    def _seen(state, play_id, *, cancelling=False):
        if play_id is not None and play_id not in state['seen_play_ids']:
            if len(state['seen_play_ids'])>=MAX_SEEN:
                if cancelling:return  # Full ledger already blocks every unseen future grant.
                raise HourlyBreakError('Cycle ledger is full; old identities preserved')
            state['seen_play_ids'].append(_id(play_id))

    def _view(self, state):
        current=deepcopy(state['current']);active=state['config']['enabled'] and current is not None and current['generation']==state['generation']
        phase=current['phase'] if current else 'idle'
        grant=None
        if active and phase=='approved':
            grant={'date':current['grant_date'],'revision':current['grant_revision'], 'minutes':PLAY_MINUTES,
                   'linked_study_id':current['study_id'],'request_id':current['break_id']}
        return {'version':1,'revision':state['revision'],'generation':state['generation'],
            'enabled':state['config']['enabled'],'task':current['task'] if active else state['config']['task'],
            'configured_task':state['config']['task'],'phase':phase,'break_id':current['break_id'] if current else None,
            'remaining_break_seconds':current['remaining_seconds'] if current else 0,
            'can_request_done':bool(active and phase=='waiting' and current['remaining_seconds']==0),
            'can_approve':bool(active and phase=='submitted' and current['remaining_seconds']==0),
            'current':current,'grant':grant,'play_minutes':PLAY_MINUTES,'break_minutes':10}

    def status(self):
        """Pure persisted snapshot: does not prove a running keeper or parent identity."""
        return self._view(self.read())

    def configure(self, enabled, task, expected_revision, *, ignored_play_id=None):
        if type(enabled) is not bool:raise HourlyBreakError('Invalid enabled flag')
        _task(task);_id(ignored_play_id,True)
        now,_=self._now()
        with self._change() as state:
            self._expected(state,expected_revision);before=deepcopy(state)
            self._seen(state,ignored_play_id,cancelling=not enabled)
            config=state['config']
            if config['enabled'] != enabled:
                state['generation']+=1
                if enabled:config['enabled_utc']=now
                elif state['current'] is not None:
                    state['current'].update(phase='cancelled',result='disabled')
            config.update(enabled=enabled,task=task)
            if state != before:state['revision']+=1
        return self._view(state)

    def _advance(self, state, current, current_study_id=None, *, check_study=False):
        if current['phase'] not in ACTIVE:return
        if check_study and current_study_id != current['study_id']:
            current.update(phase='blocked',result='study_replaced');state['revision']+=1;return
        now,mono=self._now();rollback=now<current['max_seen_utc']-2
        elapsed=max(0,math.floor(max(now,current['max_seen_utc'])-current['started_utc']))
        if self._anchor is not None and self._anchor[0]==current['break_id']:
            _,start_mono,start_remaining=self._anchor
            if mono<start_mono:rollback=True
            else:elapsed=max(elapsed,BREAK_SECONDS-start_remaining+math.floor(mono-start_mono))
        else:self._anchor=(current['break_id'],mono,current['remaining_seconds'])
        current['max_seen_utc']=max(now,current['max_seen_utc'])
        current['remaining_seconds']=min(current['remaining_seconds'],max(0,BREAK_SECONDS-elapsed))
        if rollback:
            current.update(phase='blocked',result='clock_rollback');state['revision']+=1

    def observe_completed(self, playview, current_study_id):
        _id(current_study_id,True)
        if type(playview) is not dict:raise HourlyBreakError('Invalid Play view')
        play=playview.get('current')
        if play is None:return self.status()
        if type(play) is not dict:raise HourlyBreakError('Invalid Play record')
        eligible=(play.get('phase')=='completed' and play.get('result') in ('return_confirmed','elapsed') and
            type(play.get('duration_seconds')) is int and play['duration_seconds']==3600 and
            type(play.get('remaining_seconds')) is int and play['remaining_seconds']==0 and
            _number(play.get('expires_utc')) and _number(play.get('max_seen_utc')) and
            play['max_seen_utc']>=play['expires_utc'] and play.get('return_study_id')==current_study_id and
            current_study_id is not None)
        current_state=self.read()
        if not current_state['config']['enabled']:return self._view(current_state)
        source=play.get('play_id')
        _id(source)
        if not eligible:
            # A parent replacement can revoke an outstanding old completed break.
            current=current_state['current']
            if current is None or current['source_play_id']!=source or current['phase'] not in ACTIVE:
                return self._view(current_state)
        now,_=self._now()
        with self._change() as state:
            if not state['config']['enabled']:return self._view(state)
            current=state['current']
            if current is not None and current['source_play_id']==source:
                self._advance(state,current,current_study_id,check_study=True)
            elif eligible and source not in state['seen_play_ids'] and play['expires_utc']>=state['config']['enabled_utc']:
                self._seen(state,source)
                state['current']={'break_id':_id(self.nonce()),'generation':state['generation'],
                    'source_play_id':source,'study_id':current_study_id,'task':state['config']['task'],
                    'started_utc':now,'ready_utc':now+BREAK_SECONDS,'max_seen_utc':now,
                    'remaining_seconds':BREAK_SECONDS,'phase':'waiting','requested_utc':None,
                    'approved_utc':None,'approval_revision':None,'grant_date':None,
                    'grant_revision':int(_id(self.nonce()),16) or 1,'next_play_id':None,'result':None}
                state['revision']+=1;self._anchor=None
        return self._view(state)

    def request_done(self, break_id, expected_revision):
        error=None
        with self._change() as state:
            self._expected(state,expected_revision);current=self._current(state,break_id)
            self._advance(state,current)
            if not state['config']['enabled'] or current['generation']!=state['generation'] or current['phase'] not in ('waiting','submitted'):
                error=HourlyBreakConflict('This break cannot be submitted')
            elif current['remaining_seconds']:
                error=HourlyBreakError('Ten minutes must pass before requesting parent review')
            elif current['phase']=='waiting':
                current.update(phase='submitted',requested_utc=self._now()[0]);state['revision']+=1
        if error:raise error
        return self._view(state)

    def approve(self, break_id, expected_revision, current_study_id):
        _integer(expected_revision);_id(current_study_id,True);error=None
        with self._change() as state:
            current=self._current(state,break_id)
            if not (current['phase']=='approved' and expected_revision==current['approval_revision']):
                self._expected(state,expected_revision)
            self._advance(state,current,current_study_id,check_study=True)
            if (not state['config']['enabled'] or current['generation']!=state['generation'] or
                    current['phase'] not in ('submitted','approved') or current['remaining_seconds']):
                error=HourlyBreakConflict('Parent review is not eligible; restrictions remain')
            elif current['phase']=='approved':
                if current['grant_date'] != self.today().isoformat():
                    current.update(phase='cancelled',result='approval_day_changed');state['revision']+=1
                    error=HourlyBreakConflict('Approved grant belongs to another day')
            else:
                current.update(phase='approved',approved_utc=self._now()[0],
                    approval_revision=state['revision'],grant_date=_day(self.today().isoformat()))
                state['revision']+=1
        if error:raise error
        return self._view(state)

    def confirm_next(self, break_id, next_play_id):
        _id(next_play_id)
        with self._change() as state:
            current=self._current(state,break_id)
            if current['phase']=='consumed' and current['next_play_id']==next_play_id:return self._view(state)
            if (not state['config']['enabled'] or current['generation']!=state['generation'] or current['phase']!='approved'):
                raise HourlyBreakConflict('Next game is no longer authorized')
            current.update(phase='consumed',next_play_id=next_play_id);state['revision']+=1
        return self._view(state)

    def cancel(self, *, source_play_id=None, reason='parent_cancelled'):
        _id(source_play_id,True)
        if reason not in ('parent_cancelled','disabled','study_replaced'):raise HourlyBreakError('Invalid cancel reason')
        with self._change() as state:
            self._seen(state,source_play_id,cancelling=True)
            state['config']['enabled']=False;state['generation']+=1;state['revision']+=1
            if state['current'] is not None:state['current'].update(phase='cancelled',result=reason)
        self._anchor=None
        return self._view(state)
