"""Finite, explicitly started study sessions. No persistent activation or process kills."""
import json
import math
import ntpath
import os
from pathlib import Path
import tempfile
import time

from activity_core import exclusive_lock, no_links
from paths import DEPLOY_ROOT


def executable_key(path):
    return ntpath.normcase(ntpath.normpath(path))


def validate_options(duration_minutes, delay_seconds, allowed_paths):
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 240:
        raise ValueError('Длительность должна быть от 1 до 240 минут')
    if type(delay_seconds) is not int or delay_seconds not in (5, 10):
        raise ValueError('Задержка должна быть 5 или 10 секунд')
    if not isinstance(allowed_paths, list) or len(allowed_paths) > 100:
        raise ValueError('Разрешено до 100 исключений')
    paths = []
    for path in allowed_paths:
        if not isinstance(path, str) or not ntpath.isabs(path) or '\x00' in path or not path.lower().endswith('.exe'):
            raise ValueError('Выберите полный путь к программе .exe')
        key = executable_key(path)
        if key not in paths:
            paths.append(key)
    return {'duration_minutes': duration_minutes, 'delay_seconds': delay_seconds, 'allowed_paths': paths}


class StudySettings:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'settings.json'

    def load(self):
        try:
            no_links(self.path, True)
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            value = {'duration_minutes': 30, 'delay_seconds': 10, 'allowed_paths': []}
        if not isinstance(value, dict) or set(value) != {'duration_minutes', 'delay_seconds', 'allowed_paths'}:
            raise ValueError('Неверные настройки режима уроков')
        return validate_options(**value)

    def save(self, *, duration_minutes, delay_seconds, allowed_paths):
        value = validate_options(duration_minutes, delay_seconds, allowed_paths)
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'settings.lock'):
            no_links(self.path, self.path.exists())
            descriptor, temporary = tempfile.mkstemp(prefix='.study-', suffix='.tmp', dir=self.root)
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
        return value


class StudySession:
    def __init__(self, desktop, clock=time.monotonic):
        self.desktop = desktop
        self.clock = clock
        self.active = False
        self.preview = True
        self._end = 0
        self._windows = {}
        self._allowed = set()
        self._delay = 10
        self._last_tick = None

    @property
    def remaining_seconds(self):
        return max(0, math.ceil(self._end - self.clock())) if self.active else 0

    @property
    def pending(self):
        now = self.clock()
        return [{'window': item['window'], 'remaining_seconds': max(0, math.ceil(item['due'] - now)),
                 'requested': item['attempted'], 'result': item.get('result')} for item in self._windows.values()]

    def start(self, duration_minutes, delay_seconds, preview, allowed_paths):
        value = validate_options(duration_minutes, delay_seconds, allowed_paths)
        if type(preview) is not bool:
            raise ValueError('Неверный режим проверки')
        self.stop()
        self._allowed = set(value['allowed_paths'])
        self._delay = delay_seconds
        self.preview = preview
        self._end = self.clock() + duration_minutes * 60
        self._last_tick = self.clock()
        self.active = True

    def stop(self):
        self.active = False
        self._end = 0
        self._windows.clear()
        self._last_tick = None

    def set_remaining(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 14400:
            raise ValueError('Неверное оставшееся время уроков')
        if seconds == 0:
            self.stop()
            return
        if not self.active:
            raise ValueError('Сначала включите режим уроков')
        self._end = self.clock() + seconds

    @staticmethod
    def _event(action, window=None):
        event = {'action': action}
        if window is not None:
            event['application'] = ntpath.basename(window.executable)
        return event

    def tick(self):
        if not self.active:
            return []
        if self.clock() >= self._end:
            self.stop()
            return [self._event('expired')]
        try:
            windows = self.desktop.list_windows()
        except Exception:
            self.stop()
            raise
        now = self.clock()
        # Never act after expiry, even when a slow desktop enumeration crossed it.
        if now >= self._end:
            self.stop()
            return [self._event('expired')]
        if self._last_tick is not None and now - self._last_tick > 2:
            for item in self._windows.values():
                if not item['attempted']:
                    item['due'] = now + self._delay
        self._last_tick = now
        candidates = {w.identity: w for w in windows if executable_key(w.executable) not in self._allowed}
        self._windows = {key: item for key, item in self._windows.items() if key in candidates}
        events = []
        for key, window in candidates.items():
            item = self._windows.setdefault(key, {'window': window, 'due': now + self._delay, 'attempted': False})
            item['window'] = window
            if item['attempted'] or now < item['due']:
                continue
            if self.clock() >= self._end:
                self.stop()
                events.append(self._event('expired'))
                break
            # One request per window. A save dialog can be completed without a close loop.
            item['attempted'] = True
            if self.preview:
                action = 'preview'
            else:
                try:
                    action = self.desktop.close_window(window)
                except Exception:
                    self.stop()
                    raise
            item['result'] = action
            events.append(self._event(action, window))
        return events
