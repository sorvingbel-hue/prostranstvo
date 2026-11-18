"""Local parent PIN for visible study controls on a shared Windows account."""
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from activity_core import exclusive_lock, no_links
from paths import DEPLOY_ROOT

ITERATIONS = 310_000
PIN_FORMAT = re.compile(r'[0-9]{6,12}')
MAX_FAILURES = 5
COOLDOWN_SECONDS = 30
MAX_TIMESTAMP = 253_402_300_799  # Last Unix second in year 9999.
_MISSING = object()


class StudyGuard:
    def __init__(self, root=None, clock=time.time):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'parent-pin.json'
        self.attempts_path = self.root / 'parent-pin-attempts.json'
        self._lock_path = self.root / 'parent-pin.lock'
        self.clock = clock

    @staticmethod
    def _read_json(path, error):
        try:
            no_links(path, True)
            if path.stat().st_size > 2048:
                raise ValueError(error)
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return _MISSING
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(error) from exc

    def _read(self):
        value = self._read_json(self.path, 'Повреждены настройки родительского PIN')
        if value is _MISSING:
            return None
        if not isinstance(value, dict) or set(value) != {'version', 'salt', 'iterations', 'pin_hash'}:
            raise ValueError('Повреждены настройки родительского PIN')
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError('Неизвестная версия родительского PIN')
        rounds = value['iterations']
        if type(rounds) is not int or not ITERATIONS <= rounds <= 2_000_000:
            raise ValueError('Повреждены параметры родительского PIN')
        if not isinstance(value['salt'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['salt']):
            raise ValueError('Повреждены параметры родительского PIN')
        if not isinstance(value['pin_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['pin_hash']):
            raise ValueError('Повреждены параметры родительского PIN')
        return value

    def _read_attempts(self):
        error = 'Повреждены данные о попытках ввода родительского PIN'
        value = self._read_json(self.attempts_path, error)
        if value is _MISSING:
            return 0, 0
        if not isinstance(value, dict) or set(value) != {'version', 'failures', 'blocked_until'}:
            raise ValueError(error)
        version, failures, blocked_until = value['version'], value['failures'], value['blocked_until']
        if type(version) is not int or version != 1:
            raise ValueError(error)
        if type(failures) is not int or not 0 <= failures <= MAX_FAILURES:
            raise ValueError(error)
        if type(blocked_until) not in (int, float) or not 0 <= blocked_until <= MAX_TIMESTAMP:
            raise ValueError(error)
        if (failures == MAX_FAILURES) != (blocked_until > 0):
            raise ValueError(error)
        return failures, blocked_until

    def _write_atomic(self, path, value):
        descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.stem}-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(path, path.exists())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_attempts(self, failures, blocked_until):
        self._write_atomic(self.attempts_path, {
            'version': 1, 'failures': failures, 'blocked_until': blocked_until,
        })

    @contextmanager
    def _locked(self):
        deadline = time.monotonic() + 3
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(exclusive_lock(self._lock_path))
                    break
                except PermissionError as error:
                    # On Windows the helper reads the first byte before locking it.
                    # Another process can hold that byte lock during this read.
                    if (os.name != 'nt' or error.errno != errno.EACCES or
                            error.filename is not None or time.monotonic() >= deadline):
                        raise
                    time.sleep(0.05)
            yield

    def configured(self):
        return self._read() is not None

    def set_pin(self, pin):
        if not isinstance(pin, str) or PIN_FORMAT.fullmatch(pin) is None:
            raise ValueError('PIN должен содержать от 6 до 12 цифр')
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with self._locked():
            if self._read() is not None:
                raise ValueError('Родительский PIN уже задан')
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), salt, ITERATIONS)
            value = {'version': 1, 'salt': salt.hex(), 'iterations': ITERATIONS, 'pin_hash': digest.hex()}
            self._write_atomic(self.path, value)

    def verify(self, pin):
        no_links(self.path, True)
        if not self.path.exists():
            return False
        with self._locked():
            value = self._read()
            if value is None:
                return False
            failures, blocked_until = self._read_attempts()
            now = self.clock()
            if blocked_until and blocked_until > now:
                # A backwards wall-clock adjustment must not impose a longer cooldown.
                if blocked_until - now > COOLDOWN_SECONDS:
                    blocked_until = now + COOLDOWN_SECONDS
                    self._write_attempts(failures, blocked_until)
                seconds = math.ceil(blocked_until - now)
                raise ValueError(f'Слишком много попыток. Повторите через {seconds} с.')
            valid_format = isinstance(pin, str) and PIN_FORMAT.fullmatch(pin) is not None
            matched = False
            if valid_format:
                digest = hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), bytes.fromhex(value['salt']), value['iterations'])
                matched = hmac.compare_digest(digest, bytes.fromhex(value['pin_hash']))
            if matched:
                if failures:
                    self._write_attempts(0, 0)
                return True
            failures = 1 if blocked_until else failures + 1
            delay_until = self.clock() + COOLDOWN_SECONDS if failures == MAX_FAILURES else 0
            self._write_attempts(failures, delay_until)
            return False
