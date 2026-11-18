"""Explicit launches of known desktop apps. Import and catalog reads are inert.

Starting an app can take focus. Callers must obtain permission for that screen
use on this request; a previously granted administrator right is not that consent.
No shell, arbitrary executable path, browser credential or free-form args API.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from activity_core import no_links


ALIASES = {
    'notepad': 'notepad', 'блокнот': 'notepad',
    'calculator': 'calculator', 'калькулятор': 'calculator',
    'vscode': 'vscode', 'vs code': 'vscode', 'visual studio code': 'vscode',
    'notion': 'notion',
}


def _candidates(environment):
    windows = Path(environment.get('SystemRoot', r'C:\Windows'))
    local = Path(environment.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    programs = Path(environment.get('ProgramFiles', r'C:\Program Files'))
    return {
        'notepad': ('Блокнот', [windows / 'System32' / 'notepad.exe']),
        'calculator': ('Калькулятор', [windows / 'System32' / 'calc.exe']),
        'vscode': ('Visual Studio Code', [
            local / 'Programs' / 'Microsoft VS Code' / 'Code.exe',
            programs / 'Microsoft VS Code' / 'Code.exe',
        ]),
        'notion': ('Notion', [local / 'Programs' / 'Notion' / 'Notion.exe',
                              programs / 'Notion' / 'Notion.exe']),
    }


def resolve_app(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError('Укажите имя приложения из каталога')
    try:
        return ALIASES[value.strip().casefold()]
    except KeyError:
        raise ValueError('Приложение ещё не добавлено в разрешённый каталог') from None


class ApplicationLauncher:
    def __init__(self, *, environment=None, spawn=None):
        self._environment = dict(os.environ if environment is None else environment)
        self._spawn = spawn

    def _resolve_executable(self, app_id):
        label, paths = _candidates(self._environment)[app_id]
        for path in paths:
            try:
                no_links(path)
                # Windows' own System32 executables legitimately use WinSxS
                # hard links. We only read/launch these exact paths, never edit.
                if path.is_file():
                    return label, path.absolute()
            except (OSError, ValueError):
                continue
        return label, None

    def catalog(self):
        result = []
        for app_id in _candidates(self._environment):
            label, executable = self._resolve_executable(app_id)
            result.append({'id': app_id, 'name': label, 'installed': executable is not None,
                           'executable': str(executable) if executable is not None else None,
                           'launch_requires_screen_permission': True,
                           'automation_connected': False})
        return {'applications': result, 'status': 'inventory_only',
                'started_anything': False, 'arbitrary_commands_supported': False}

    def plan(self, app):
        app_id = resolve_app(app)
        label, executable = self._resolve_executable(app_id)
        return {'application': app_id, 'name': label,
                'status': 'planned_not_started' if executable else 'not_found_in_known_locations',
                'argv': [str(executable)] if executable else None,
                'cwd': str(executable.parent) if executable else None,
                'screen_permission_required': True,
                'automation_connected': False, 'started_anything': False}

    def launch(self, app, *, foreground_authorized=False):
        if type(foreground_authorized) is not bool:
            raise ValueError('Разрешение на использование экрана должно быть явным')
        app_id = resolve_app(app)
        if not foreground_authorized:
            return {'application': app_id, 'status': 'blocked',
                    'reason': 'screen_use_not_authorized', 'started_anything': False}
        # Resolve again immediately before starting; do not accept a caller's argv.
        plan = self.plan(app_id)
        if plan['argv'] is None:
            return plan
        executable = no_links(Path(plan['argv'][0]))
        if not executable.is_file():
            raise ValueError('Изменился установленный файл приложения')
        spawn = subprocess.Popen if self._spawn is None else self._spawn
        process = spawn([str(executable)], cwd=executable.parent, shell=False)
        return {'application': app_id, 'status': 'process_started',
                'pid': process.pid, 'started_anything': True,
                'window_open_verified': False, 'automation_connected': False}
