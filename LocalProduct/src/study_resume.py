"""Saved local study session and its current-user Windows startup entry."""
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links
import paths

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def _validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'session_id', 'expires_utc', 'duration_seconds'}:
        raise ValueError('Повреждено время окончания уроков')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Неизвестная версия режима уроков')
    if not isinstance(value['session_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['session_id']):
        raise ValueError('Повреждён идентификатор режима уроков')
    duration = value['duration_seconds']
    expiry = value['expires_utc']
    if type(duration) is not int or not (duration == 0 or 60 <= duration <= 14400):
        raise ValueError('Повреждена продолжительность уроков')
    if duration == 0 and expiry is None:
        return dict(value)
    if duration == 0:
        raise ValueError('Повреждено время окончания уроков')
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError('Повреждено время окончания уроков')
    return dict(value)


class StudyResume:
    def __init__(self, root=None, clock=time.time, registry=None):
        self.root = Path(root) if root is not None else paths.DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'active-session.json'
        self.clock = clock
        self.registry = registry
        self.beta = hasattr(paths, 'APP_ROOT')
        self.startup_name = 'ProstranstvoLocalBetaStudySession' if self.beta else 'ProstranstvoStudySession'

    def read(self):
        try:
            no_links(self.path, True)
            if self.path.stat().st_size > 2048:
                raise ValueError('Слишком большой файл времени уроков')
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return None
        return _validate(value)

    def activate(self, minutes=60):
        if type(minutes) is not int or not 0 <= minutes <= 240:
            raise ValueError('Выберите «Без таймера» или от 1 до 240 минут')
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ValueError('Не удалось определить время Windows')
        value = _validate({'version': 1, 'session_id': uuid.uuid4().hex,
                           'expires_utc': now + minutes * 60 if minutes else None,
                           'duration_seconds': minutes * 60})
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'session.lock'):
            no_links(self.path, self.path.exists())
            descriptor, temporary = tempfile.mkstemp(prefix='.study-session-', suffix='.tmp', dir=self.root)
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

    def remaining_seconds(self, lease=None):
        value = self.read() if lease is None else _validate(lease)
        if value is None:
            return 0
        if value['expires_utc'] is None:
            return math.inf
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('Не удалось определить время Windows')
        return min(value['duration_seconds'], max(0, math.ceil(value['expires_utc'] - now)))

    def clear(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
            raise ValueError('Неверный идентификатор режима уроков')
        if not self.root.exists():
            return False
        no_links(self.root)
        with exclusive_lock(self.root / 'session.lock'):
            current = self.read()
            if current is None or current['session_id'] != session_id:
                return False
            no_links(self.path, True)
            self.path.unlink()
        return True

    def _registry(self):
        if self.registry is None:
            import winreg
            return winreg
        return self.registry

    def _startup_command(self):
        if self.beta:
            app = paths.APP_ROOT
            if getattr(sys, 'frozen', False):
                target = no_links(app / 'Prostranstvo.exe', True)
                if not target.is_file():
                    raise ValueError('Не найден исполняемый файл приложения')
                return '"' + str(target) + '" --study-resume'
            python = Path(getattr(sys, '_base_executable', sys.executable))
            app_script = app / 'study_runtime.py'
        else:
            app = paths.DEPLOY_ROOT / 'app'
            config = no_links(app / 'python-path.txt', True)
            python = Path(config.read_text(encoding='utf-8-sig').strip())
            app_script = app / 'study_runtime.py'
        if python.name.lower() == 'python.exe':
            python = python.with_name('pythonw.exe')
        if not python.is_absolute() or python.name.lower() != 'pythonw.exe':
            raise ValueError('Неверный путь Python для возобновления уроков')
        no_links(python, True)
        no_links(app_script, True)
        if not python.is_file() or not app_script.is_file():
            raise ValueError('Не установлен модуль возобновления уроков')
        return '"' + str(python) + '" -B "' + str(app_script) + '" --resume'

    def startup_enabled(self):
        registry = self._registry()
        command = self._startup_command()
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                previous, kind = registry.QueryValueEx(key, self.startup_name)
                return previous == command and kind == registry.REG_SZ
        except FileNotFoundError:
            return False

    def enable_startup(self):
        registry = self._registry()
        command = self._startup_command()
        with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
            try:
                previous, kind = registry.QueryValueEx(key, self.startup_name)
                occupied = True
            except FileNotFoundError:
                previous, kind = None, None
                occupied = False
            if occupied and (previous != command or kind != registry.REG_SZ):
                raise ValueError('Имя автозапуска занято другой программой')
            registry.SetValueEx(key, self.startup_name, 0, registry.REG_SZ, command)

    def disable_startup(self):
        registry = self._registry()
        command = self._startup_command()
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE) as key:
                try:
                    previous, kind = registry.QueryValueEx(key, self.startup_name)
                except FileNotFoundError:
                    return
                if previous == command and kind == registry.REG_SZ:
                    registry.DeleteValue(key, self.startup_name)
        except FileNotFoundError:
            return
