"""Local desktop MCP configuration and an actual shared stop marker.

Imports, configuration display and status polling never launch an MCP server,
capture the screen, inject input or edit a client's configuration. A STOP file
revokes servers configured with that exact --stop-file; this panel does not
claim to have an IPC acknowledgement from a running client.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tkinter as tk
from tkinter import ttk


DURATIONS = (30, 60)
SERVER_NAME = 'prostranstvo_desktop'


def _no_links(path, regular=False):
    """Reject symlinks, junctions/reparse points and non-unique marker files."""
    path = Path(path)
    for entry in (path, *path.parents):
        try:
            details = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or getattr(details, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Ссылки и junction недопустимы в пути STOP.')
        if entry == path and regular and (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1):
            raise ValueError('STOP должен быть обычным файлом без hardlink.')
    return path


def _marker_path(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise ValueError('Нужен абсолютный локальный путь STOP.')
    if '..' in path.parts:
        raise ValueError('Переходы к родительской папке недопустимы в пути STOP.')
    if any(':' in part for part in path.parts[1:]):
        raise ValueError('Потоки ADS недопустимы в пути STOP.')
    _no_links(path)
    return path


def default_stop_file():
    import paths
    data_root = getattr(paths, 'DATA_ROOT', None)
    if data_root is None:
        data_root = Path(paths.DEPLOY_ROOT) / 'data'
    return _marker_path(Path(data_root) / 'desktop-control' / 'STOP')


class StopMarker:
    def __init__(self, path):
        self.path = _marker_path(path)

    def identity(self):
        _no_links(self.path)
        try:
            details = self.path.lstat()
        except FileNotFoundError:
            return None
        _no_links(self.path, regular=True)
        return details.st_dev, details.st_ino, details.st_mtime_ns, details.st_size

    def status(self):
        return {'stop_requested': self.identity() is not None,
                'marker': str(self.path), 'client_connection_verified': False,
                'process_state': 'unknown'}

    def request_stop(self):
        # Prepare a complete file in the same checked directory, then replace.
        self.identity()
        _no_links(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _no_links(self.path.parent)
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix='.desktop-stop-', suffix='.tmp',
                                                dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(b'STOP\n')
                stream.flush()
                os.fsync(stream.fileno())
            _no_links(temporary, regular=True)
            self.identity()
            os.replace(temporary, self.path)
            temporary = None
            _no_links(self.path, regular=True)
            return self.status()
        finally:
            if temporary is not None:
                try:
                    _no_links(temporary, regular=True)
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def clear_for_future(self, expected_identity):
        # A STOP issued while the parent enters their PIN must not be cleared.
        if self.identity() != expected_identity:
            raise ValueError('STOP изменился во время подтверждения. Запрет сохранён.')
        if expected_identity is not None:
            self.path.unlink()
        return self.status()


def build_configuration(minutes=30, enable_input=False, *, app_root=None,
                        frozen=None, executable=None, python_base=None,
                        deployment_root=None, stop_file=None):
    """Return configuration only; no process launch, screenshot or file write."""
    if type(minutes) is not int or minutes not in DURATIONS:
        raise ValueError('Выберите 30 или 60 минут.')
    if type(enable_input) is not bool:
        raise ValueError('Неверный флаг управления.')
    if frozen is None:
        frozen = bool(getattr(sys, 'frozen', False))
    executable = Path(executable or sys.executable)
    root = Path(app_root) if app_root is not None else (
        executable.parent if frozen else Path(__file__).absolute().parent)
    marker = _marker_path(stop_file if stop_file is not None else default_stop_file())
    if frozen:
        command = root / 'ProstranstvoCore.exe'
        arguments = ['desktop-mcp']
    else:
        if deployment_root is None:
            import paths
            deployment_root = getattr(paths, 'DEPLOY_ROOT', None)
        preferred = (Path(deployment_root) / 'runtime' / 'python-3.13.18' / 'python.exe'
                     if deployment_root is not None else None)
        if preferred is not None:
            _no_links(preferred)
        if preferred is not None and preferred.exists():
            command = _no_links(preferred, regular=True)
        else:
            command = Path(python_base or sys.base_prefix) / 'python.exe'
        arguments = ['-B', str(root / 'desktop_control_mcp.py')]
    arguments += ['--session-seconds', str(minutes * 60), '--stop-file', str(marker)]
    if enable_input:
        arguments.append('--enable-input')
    return {'name': SERVER_NAME, 'command': str(command), 'args': arguments,
            'cwd': str(root), 'enabled': True}


def configuration_text(value):
    """JSON string escaping is valid for these TOML basic strings/arrays."""
    quote = lambda text: json.dumps(text, ensure_ascii=False)
    return ('# Подготовленная конфигурация; клиент ещё не подключён.\n'
            f'[mcp_servers.{SERVER_NAME}]\n'
            f'command = {quote(value["command"])}\n'
            f'args = {quote(value["args"])}\n'
            f'cwd = {quote(value["cwd"])}\n'
            'enabled = true\n')


class DesktopControlPanel(ttk.Frame):
    def __init__(self, parent, authorize_parent=None, *, marker=None):
        super().__init__(parent, padding=12)
        self.authorize_parent = authorize_parent
        self.marker = marker if marker is not None else StopMarker(default_stop_file())
        self.closed = False
        self._timer = None
        self.columnconfigure(0, weight=1)
        self.bind('<Destroy>', self._on_destroy, add=True)
        ttk.Label(self, text='Управление экраном для ИИ', font=('Segoe UI', 15, 'bold')).grid(
            row=0, column=0, sticky='w')
        ttk.Label(self, wraplength=900, justify='left', text=(
            'Снимки экрана, мышь, клавиатура и окна доступны через отдельный локальный MCP. '
            'Нужен новый процесс и подключение в вашем ИИ-клиенте; показ настроек сам по себе доступ не включает. '
            'Это текущий общий экран: одновременная игра или работа будут мешать управлению. '
            'Пароль, 2FA и запросы UAC подтверждает владелец вручную.'),
        ).grid(row=1, column=0, sticky='ew', pady=(6, 10))
        options = ttk.Frame(self)
        options.grid(row=2, column=0, sticky='ew')
        ttk.Label(options, text='Сеанс, минут:').pack(side='left')
        self.duration = tk.StringVar(self, value='30')
        ttk.Combobox(options, values=DURATIONS, state='readonly', width=5,
                     textvariable=self.duration).pack(side='left', padx=8)
        self.input_enabled = tk.BooleanVar(self, value=False)
        ttk.Checkbutton(options, text='Мышь и клавиатура в новом процессе',
                        variable=self.input_enabled).pack(side='left')
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky='ew', pady=10)
        ttk.Button(actions, text='Показать конфигурацию', command=self.show_configuration).pack(side='left')
        ttk.Button(actions, text='Остановить доступ ИИ', command=self.stop_access).pack(side='left', padx=8)
        ttk.Button(actions, text='Разрешить будущий сеанс…', command=self.allow_future).pack(side='left')
        self.status = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=900, justify='left').grid(
            row=4, column=0, sticky='ew')
        ttk.Label(self, textvariable=self.notice, wraplength=900, justify='left').grid(
            row=5, column=0, sticky='ew', pady=(4, 8))
        self.configuration = tk.Text(self, height=9, wrap='none', state='disabled',
                                     font=('Consolas', 9), takefocus=True)
        self.configuration.grid(row=6, column=0, sticky='ew')
        ttk.Label(self, wraplength=900, justify='left', text=(
            'STOP действует для процессов с указанным --stop-file. Удаление STOP не оживляет остановленный '
            'процесс: потребуется новый запуск MCP. Без связи с клиентом приложение не подтверждает, '
            'что сервер запущен, подключён или уже обработал остановку.'),
        ).grid(row=7, column=0, sticky='ew', pady=(8, 0))
        self.show_configuration()
        self.refresh()
        self._schedule_poll()

    def show_configuration(self):
        if self.closed:
            return False
        try:
            minutes = int(self.duration.get())
            value = build_configuration(minutes, self.input_enabled.get(), stop_file=self.marker.path)
            text = configuration_text(value)
            self.configuration.configure(state='normal')
            try:
                self.configuration.delete('1.0', 'end')
                self.configuration.insert('1.0', text)
            finally:
                self.configuration.configure(state='disabled')
            self.notice.set('Настройки показаны. Файлы клиента не изменены; сервер не запускался.')
            return True
        except Exception:
            self.notice.set('Не удалось подготовить безопасную конфигурацию. Доступ не включён.')
            return False

    def refresh(self):
        if self.closed:
            return
        try:
            value = self.marker.status()
            if value['stop_requested']:
                self.status.set('STOP установлен: запрос отзыва доступа записан. '
                                'Обработка работающим процессом не подтверждена через IPC.')
            else:
                self.status.set('STOP отсутствует. Запуск сервера и подключение ИИ не подтверждены; '
                                'состояние процесса неизвестно.')
        except Exception:
            self.status.set('Состояние STOP прочитать не удалось. Не считайте доступ разрешённым.')

    def stop_access(self):
        if self.closed:
            return False
        try:
            self.marker.request_stop()
            self.refresh()
            self.notice.set('STOP атомарно записан. Сервер с этим --stop-file прекращает ввод и снимки '
                            'при обработке маркера; подтверждение клиента ещё не получено.')
            return True
        except Exception:
            self.notice.set('Записать STOP не удалось. Остановка не подтверждена; '
                            'используйте кнопку СТОП в окне MCP или отключите сервер в клиенте.')
            self.refresh()
            return False

    def allow_future(self):
        if self.closed:
            return False
        try:
            previous = self.marker.identity()
            if self.authorize_parent is None or not self.authorize_parent('Разрешить будущий сеанс управления ИИ'):
                self.notice.set('Подтверждение родителя не получено. Запрет не изменён.')
                return False
            self.marker.clear_for_future(previous)
            self.refresh()
            self.notice.set('STOP снят только для будущего нового процесса. '
                            'Работающий остановленный MCP не возобновляется; доступ сейчас не включался.')
            return True
        except Exception:
            self.notice.set('Не удалось снять STOP безопасно. Доступ не включён; '
                            'проверьте маркер и повторите подтверждение.')
            self.refresh()
            return False

    def _schedule_poll(self):
        if not self.closed:
            self._timer = self.after(1500, self._poll)

    def _poll(self):
        self._timer = None
        if not self.closed:
            self.refresh()
            self._schedule_poll()

    def _on_destroy(self, event):
        if event.widget is self:
            self.close()

    def close(self):
        # This panel owns no server. Closing the app must not pretend to revoke it.
        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
