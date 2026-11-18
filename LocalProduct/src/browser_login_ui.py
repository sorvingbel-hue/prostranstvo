"""Explicit manual Facebook login handoff; status polling never launches anything."""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk


class BrowserLoginPanel(ttk.Frame):
    def __init__(self, parent, session=None, authorize_parent=None):
        super().__init__(parent, padding=12)
        if session is None:
            import browser_session
            session = browser_session
        self.session = session
        self.authorize_parent = authorize_parent
        self.closed = False
        self._timer = None
        self._current = {}
        self._completion_reported = False
        self.columnconfigure(0, weight=1)
        self.bind('<Destroy>', self._on_destroy, add=True)
        ttk.Label(self, text='Facebook для помощника', font=('Segoe UI', 14, 'bold')).grid(
            row=0, column=0, sticky='w')
        ttk.Label(self, wraplength=900, justify='left', text=(
            'Когда будет удобно, откройте отдельное окно и войдите сами. '
            'Пароль вводится только вами в браузере. Затем закройте это окно и подтвердите завершение.'),
        ).grid(row=1, column=0, sticky='ew', pady=(6, 8))
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky='ew')
        self.start_button = ttk.Button(actions, text='Открыть окно входа',
                                       command=self.launch, state='disabled')
        self.start_button.pack(side='left')
        self.finish_button = ttk.Button(actions, text='Я вошёл и закрыл окно',
                                        command=self.finish, state='disabled')
        self.finish_button.pack(side='left', padx=8)
        self.status = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=900).grid(
            row=3, column=0, sticky='ew', pady=(8, 0))
        ttk.Label(self, textvariable=self.notice, wraplength=900).grid(row=4, column=0, sticky='ew')
        self.refresh()
        self._schedule_poll()

    def _authorized(self, title):
        if self.authorize_parent is not None and self.authorize_parent(title):
            return True
        self.notice.set('Родительское подтверждение не получено. Действие не выполнено.')
        return False

    def _render(self, value):
        if not isinstance(value, dict):
            raise ValueError('invalid_manual_login_state')
        self._current = dict(value)
        state = value.get('status')
        pending = value.get('manual_login_pending') is True
        identifier = value.get('manual_session_id')
        valid_id = isinstance(identifier, str) and re.fullmatch(r'[0-9a-f]{32}', identifier) is not None
        if state == 'manual_login_finished':
            text = ('Вы подтвердили завершение ручного шага. Вход ещё не проверен. '
                    'Помощник проверит его при следующем подключении.')
        elif state == 'blocked_manual_session_changed':
            text = 'Сессия ручного входа изменилась. Новая сессия не завершена; подтвердите её отдельно.'
        elif state == 'launch_failed':
            text = 'Запустить окно не удалось. Ручной шаг остаётся незавершённым; вход не проверен.'
        elif state == 'manual_login_started':
            text = 'Запуск отдельного окна запрошен. Войдите сами и закройте окно. Вход пока не проверен.'
        elif pending:
            text = ('Ожидается завершение ручного входа. Закройте выделенное окно перед подтверждением.'
                    if valid_id else 'Состояние ручного входа неполное. Требуется проверка настроек.')
        elif state == 'blocked_study_active':
            text = 'Режим «Уроки» активен. Окно входа сейчас не запускается.'
        elif state == 'blocked_edge_not_found':
            text = 'Edge не найден в поддерживаемых местах. Окно не запускалось.'
        elif state == 'prepared_not_started':
            text = ('Вход ещё не проверен. Помощник проверит его при следующем подключении.'
                    if self._completion_reported else
                    'План отдельного окна подготовлен. Ничего не открыто; вход не проверен.')
        else:
            text = 'Действие не подтверждено. Вход в Facebook не проверен.'
        self.status.set(text)
        self.start_button.configure(state='normal' if state == 'prepared_not_started' and not pending else 'disabled')
        self.finish_button.configure(state='normal' if pending and valid_id else 'disabled')

    def _error(self, error):
        if isinstance(error, ValueError) and str(error) == 'invalid_manual_login_marker':
            text = 'Файл состояния ручного входа повреждён. Требуется отдельное восстановление.'
        elif isinstance(error, RuntimeError):
            text = 'Профиль занят или недоступен. Действие не подтверждено; автоматического повтора нет.'
        else:
            text = 'Не удалось безопасно прочитать или изменить состояние входа. Требуется проверка настроек.'
        self.status.set(text)
        self.start_button.configure(state='disabled')
        self.finish_button.configure(state='disabled')

    def refresh(self):
        if self.closed:
            return
        try:
            self._render(self.session.prepare_manual_login())
        except Exception as error:
            self._error(error)

    def launch(self):
        if self.closed:
            return False
        try:
            if not self._authorized('Открыть отдельное окно входа Facebook'):
                return False
            if not messagebox.askyesno(
                'Окно входа Facebook',
                'Сейчас откроется видимое окно Edge с Facebook и отдельным профилем помощника. '
                'Оно займёт экран. Вы войдёте сами; помощник не вводит пароль.\n\n'
                'Разрешаете открыть это окно сейчас?', parent=self.winfo_toplevel(),
            ):
                self.notice.set('Окно входа не запрошено.')
                return False
            result = self.session.launch_manual_login(foreground_authorized=True)
            self._render(result)
            started = result.get('status') == 'manual_login_started' and result.get('process_started') is True
            if started:
                self._completion_reported = False
            self.notice.set('Войдите сами, затем закройте выделенное окно.' if started else 'Окно входа не подтверждено.')
            return started
        except Exception as error:
            self._error(error)
            return False

    def finish(self):
        if self.closed:
            return False
        try:
            # Capture this exact marker before either dialog can run the poll.
            plan = self.session.prepare_manual_login()
            self._render(plan)
            identifier = plan.get('manual_session_id')
            if (plan.get('manual_login_pending') is not True or not isinstance(identifier, str)
                    or re.fullmatch(r'[0-9a-f]{32}', identifier) is None):
                self.notice.set('Нет проверенной ручной сессии для завершения.')
                return False
            if not self._authorized('Подтвердить завершение ручного входа Facebook'):
                return False
            if not messagebox.askyesno(
                'Завершить ручной вход',
                'Вы сами вошли в Facebook и уже закрыли выделенное окно Edge?\n\n'
                'Это подтверждает только ручной шаг. Вход ещё не проверен. '
                'Помощник проверит его при следующем подключении.',
                parent=self.winfo_toplevel(),
            ):
                self.notice.set('Ручной шаг остаётся незавершённым.')
                return False
            result = self.session.finish_manual_login(user_done=True, manual_session_id=identifier)
            finished = result.get('status') == 'manual_login_finished' and result.get('manual_login_pending') is False
            if finished:
                self._completion_reported = True
            self._render(result)
            self.notice.set('Вход ещё не проверен. Помощник проверит его при следующем подключении.'
                            if finished else 'Завершение ручного шага не подтверждено.')
            return finished
        except Exception as error:
            self._error(error)
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
        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
