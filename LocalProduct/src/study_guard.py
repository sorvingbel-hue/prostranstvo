"""Local parent PIN for visible study controls on a shared Windows account."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

from activity_core import exclusive_lock, no_links
from paths import DEPLOY_ROOT

ITERATIONS = 310_000
PIN_FORMAT = re.compile(r'[0-9]{6,12}')


class StudyGuard:
    def __init__(self, root=None, clock=time.monotonic):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'parent-pin.json'
        self.clock = clock
        self._failures = 0
        self._blocked_until = 0
        self._mutex = threading.Lock()

    def _read(self):
        try:
            no_links(self.path, True)
            if self.path.stat().st_size > 2048:
                raise ValueError('Повреждены настройки родительского PIN')
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
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

    def configured(self):
        return self._read() is not None

    def set_pin(self, pin):
        if not isinstance(pin, str) or PIN_FORMAT.fullmatch(pin) is None:
            raise ValueError('PIN должен содержать от 6 до 12 цифр')
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'parent-pin.lock'):
            if self._read() is not None:
                raise ValueError('Родительский PIN уже задан')
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), salt, ITERATIONS)
            value = {'version': 1, 'salt': salt.hex(), 'iterations': ITERATIONS, 'pin_hash': digest.hex()}
            descriptor, temporary = tempfile.mkstemp(prefix='.parent-pin-', suffix='.tmp', dir=self.root)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                no_links(self.path, self.path.exists())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def verify(self, pin):
        with self._mutex:
            now = self.clock()
            if now < self._blocked_until:
                seconds = math.ceil(self._blocked_until - now)
                raise ValueError(f'Слишком много попыток. Повторите через {seconds} с.')
            if self._blocked_until:
                self._blocked_until = 0
                self._failures = 0
            value = self._read()
            if value is None:
                return False
            valid_format = isinstance(pin, str) and PIN_FORMAT.fullmatch(pin) is not None
            matched = False
            if valid_format:
                digest = hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), bytes.fromhex(value['salt']), value['iterations'])
                matched = hmac.compare_digest(digest, bytes.fromhex(value['pin_hash']))
            if matched:
                self._failures = 0
                return True
            self._failures += 1
            if self._failures >= 5:
                self._blocked_until = self.clock() + 30
            return False
