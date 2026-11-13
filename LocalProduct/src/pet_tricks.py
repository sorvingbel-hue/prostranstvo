"""Bounded, data-only kitten rules and preview IPC. Importing is inert."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from activity_core import no_links
from paths import DEPLOY_ROOT

DEFAULT_ROOT = DEPLOY_ROOT / 'data' / 'cat-companion' / 'tricks'
MAX_CONFIG_BYTES = 32 * 1024
HEX_ID = re.compile(r'[0-9a-f]{32}\Z')
EVENTS = frozenset(('greet', 'petted', 'manual'))
ACTIONS = frozenset(('turn', 'heart', 'blink', 'purr', 'wait', 'stretch'))
TERMINAL = frozenset(('completed', 'blocked', 'stopped', 'failed'))
REASONS = frozenset(('started', 'completed', 'requested', 'context_blocked', 'config_invalid',
    'rule_missing', 'cooldown', 'interrupted', 'config_changed', 'io_unavailable',
    'idle', 'queued', 'expired', 'native_unavailable', 'status_invalid', 'command_invalid',
    'ack_invalid', 'stale_status', 'stopped', 'failed', 'manual', 'finished',
    'busy', 'disabled', 'clock_invalid'))


class PetTrickError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,48}', code) else 'tricks_unavailable'
        super().__init__(self.code)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PetTrickError('duplicate_key')
        value[key] = item
    return value


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(PetTrickError('invalid_json')))
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise PetTrickError('invalid_json') from None


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PetTrickError('invalid_schema')


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise PetTrickError('invalid_number')
    return value


def _id(value, empty=False):
    if empty and value == '': return value
    if not isinstance(value, str) or not HEX_ID.fullmatch(value):
        raise PetTrickError('invalid_id')
    return value


def validate_config(config):
    _keys(config, ('version', 'enabled', 'rules'))
    if type(config['version']) is not int or config['version'] != 1 or type(config['enabled']) is not bool:
        raise PetTrickError('invalid_schema')
    if not isinstance(config['rules'], list) or len(config['rules']) > 8:
        raise PetTrickError('rule_limit')
    ids = set()
    for rule in config['rules']:
        _keys(rule, ('id', 'name', 'enabled', 'event', 'cooldown_seconds', 'steps'))
        ident = _id(rule['id'])
        if ident in ids: raise PetTrickError('duplicate_rule')
        ids.add(ident)
        name = rule['name']
        if (not isinstance(name, str) or not 1 <= len(name) <= 60 or name != name.strip()
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
            raise PetTrickError('invalid_name')
        if type(rule['enabled']) is not bool or not isinstance(rule['event'], str) or rule['event'] not in EVENTS:
            raise PetTrickError('invalid_rule')
        _int(rule['cooldown_seconds'], 10, 3600)
        if not isinstance(rule['steps'], list) or not 1 <= len(rule['steps']) <= 8:
            raise PetTrickError('step_limit')
        total = purr = 0
        for step in rule['steps']:
            _keys(step, ('action', 'duration_ms'))
            if not isinstance(step['action'], str) or step['action'] not in ACTIONS: raise PetTrickError('invalid_action')
            duration = _int(step['duration_ms'], 100, 5000)
            total += duration
            if step['action'] == 'purr':
                if duration > 2000: raise PetTrickError('purr_limit')
                purr += duration
        if total > 30000: raise PetTrickError('duration_limit')
        if purr > 3000: raise PetTrickError('purr_limit')
    try: encoded = json.dumps(config, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError): raise PetTrickError('invalid_json') from None
    if len(encoded) > MAX_CONFIG_BYTES: raise PetTrickError('config_too_large')
    return parse_json(encoded)


class FileLock:
    """Non-destructive OS lock; never reads a byte held by another Windows owner."""
    def __init__(self, path, timeout=1): self.path, self.timeout, self.file = Path(path), timeout, None
    def __enter__(self):
        no_links(self.path, self.path.exists())
        self.file = self.path.open('a+b')
        try:
            no_links(self.path, True)
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0'); self.file.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self.file.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() >= deadline: raise PetTrickError('store_busy') from None
                    time.sleep(.025)
        except BaseException:
            self.file.close(); self.file = None
            raise
    def __exit__(self, *_):
        if self.file:
            self.file.close(); self.file = None


class PetTrickStore:
    def __init__(self, root=None, clock=time.time):
        self.root = Path(root if root is not None else DEFAULT_ROOT).absolute()
        self.clock = clock
        self._thread_lock = threading.RLock()

    @contextmanager
    def lock(self):
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with self._thread_lock, FileLock(self.root / 'tricks.lock'):
            yield

    def _read(self, name, limit=4096):
        path = self.root / name
        no_links(path, path.exists())
        try:
            with path.open('rb') as stream:
                if os.fstat(stream.fileno()).st_nlink != 1: raise PetTrickError('unsafe_file')
                data = stream.read(limit + 1)
        except FileNotFoundError: return None
        if len(data) > limit: raise PetTrickError('file_too_large')
        return parse_json(data.decode('utf-8-sig'))

    def _atomic(self, name, value):
        no_links(self.root)
        target = self.root / name
        no_links(target, target.exists())
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        descriptor, temporary = tempfile.mkstemp(prefix='.tricks-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data); output.flush(); os.fsync(output.fileno())
            for attempt in range(8):
                no_links(target, target.exists())
                try: os.replace(temporary, target); break
                except PermissionError:
                    if attempt == 7: raise
                    time.sleep(.025)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    def _config(self):
        value = self._read('rules.json', MAX_CONFIG_BYTES)
        return {'version': 1, 'enabled': False, 'rules': []} if value is None else validate_config(value)

    def load(self):
        with self.lock(): return self._config()

    def save(self, config):
        value = validate_config(config)
        with self.lock(): self._atomic('rules.json', value)
        return value

    def _native(self, now):
        fallback = {'available': False, 'version': 1, 'pid': 0, 'updated_utc': 0,
                    'config_ok': False, 'active_rule_id': '', 'reason': 'native_unavailable'}
        try:
            value = self._read('status.json')
            if value is None: return fallback
            _keys(value, ('version', 'pid', 'updated_utc', 'config_ok', 'active_rule_id'))
            if type(value['version']) is not int or value['version'] != 1 or type(value['config_ok']) is not bool:
                raise PetTrickError('status_invalid')
            _int(value['pid'], 1, 2**31 - 1); _int(value['updated_utc'], 1, 2**53)
            _id(value['active_rule_id'], empty=True)
            if not -2 <= now - value['updated_utc'] <= 6:
                return dict(fallback, reason='stale_status')
            return dict(value, available=True, reason='idle')
        except (OSError, ValueError, UnicodeError, TypeError):
            return dict(fallback, reason='status_invalid')

    def _command(self):
        value = self._read('command.json')
        if value is None: return None
        _keys(value, ('version', 'request_id', 'action', 'rule_id', 'expires_utc'))
        if type(value['version']) is not int or value['version'] != 1 or value['action'] not in ('run', 'stop'):
            raise PetTrickError('command_invalid')
        _id(value['request_id']); _id(value['rule_id'], empty=value['action'] == 'stop')
        if value['action'] == 'stop' and value['rule_id'] != '': raise PetTrickError('command_invalid')
        _int(value['expires_utc'], 1, 2**53)
        return value

    def _ack(self, now):
        try:
            value = self._read('preview-status.json')
            if value is None: return None
            _keys(value, ('version', 'request_id', 'state', 'reason', 'updated_utc'))
            if type(value['version']) is not int or value['version'] != 1 or value['state'] not in TERMINAL | {'started'}:
                raise PetTrickError('ack_invalid')
            _id(value['request_id']); _int(value['updated_utc'], 1, 2**53)
            if value['updated_utc'] > now + 2: raise PetTrickError('ack_invalid')
            if value['reason'] not in REASONS: value['reason'] = 'failed'
            return value
        except (OSError, ValueError, UnicodeError, TypeError): return None

    def _snapshot(self):
        now = int(self.clock())
        config = self._config()
        native = self._native(now)
        preview = {'state': 'idle', 'action': '', 'request_id': '', 'rule_id': '', 'reason': 'idle', 'updated_utc': 0}
        try: command = self._command()
        except (OSError, ValueError, UnicodeError, TypeError):
            command = None; preview.update(state='failed', reason='command_invalid')
        if command is not None:
            preview.update(action=command['action'], request_id=command['request_id'], rule_id=command['rule_id'], expires_utc=command['expires_utc'])
            ack = self._ack(now)
            if ack is not None and ack['request_id'] == command['request_id']:
                preview.update({k: ack[k] for k in ('state', 'reason', 'updated_utc')})
            elif command['expires_utc'] <= now:
                preview.update(state='expired', reason='expired')
            else:
                preview.update(state='queued', reason='queued')
        return {'config': config, 'preview': preview, 'native': native}

    def snapshot(self):
        with self.lock(): return self._snapshot()

    def preview(self, rule_id):
        _id(rule_id)
        with self.lock():
            state = self._snapshot()
            if not state['native']['available']: raise PetTrickError('native_unavailable')
            if not state['native']['config_ok']: raise PetTrickError('config_invalid')
            if not any(r['id'] == rule_id for r in state['config']['rules']): raise PetTrickError('rule_missing')
            previous = state['preview']; now = int(self.clock())
            # Expiry limits pickup; a fresh active heartbeat also prevents overlapping 30s runs.
            if state['native']['active_rule_id'] or (previous['state'] in ('queued', 'started')
                    and previous.get('expires_utc', 0) > now):
                raise PetTrickError('preview_busy')
            command = {'version': 1, 'request_id': uuid.uuid4().hex, 'action': 'run',
                       'rule_id': rule_id, 'expires_utc': now + 20}
            self._atomic('command.json', command)
            return command

    def stop(self):
        with self.lock():
            command = {'version': 1, 'request_id': uuid.uuid4().hex, 'action': 'stop',
                       'rule_id': '', 'expires_utc': int(self.clock()) + 20}
            # Stop deliberately supersedes an outstanding run, even without a heartbeat.
            self._atomic('command.json', command)
            return command

    @staticmethod
    def templates():
        return [
            {'id': uuid.uuid4().hex, 'name': 'Ласковый ответ', 'enabled': False, 'event': 'petted',
             'cooldown_seconds': 20, 'steps': [{'action': 'turn', 'duration_ms': 450},
                 {'action': 'heart', 'duration_ms': 1200}, {'action': 'purr', 'duration_ms': 1500}]},
            {'id': uuid.uuid4().hex, 'name': 'Доброе приветствие', 'enabled': False, 'event': 'greet',
             'cooldown_seconds': 60, 'steps': [{'action': 'blink', 'duration_ms': 500},
                 {'action': 'stretch', 'duration_ms': 1500}]}]
