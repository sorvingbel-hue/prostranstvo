"""Parent-authorized, delayed Windows power requests with a read-only poll."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk


DELAYS = (30, 60, 120, 300)
ACTION_LABELS = {'restart': 'Перезагрузка', 'shutdown': 'Выключение'}


class PowerPanel(ttk.Frame):
    def __init__(self, parent, control=None, authorize_parent=None):
        super().__init__(parent, padding=12)
        if control is None:
            from power_control import PowerControl
            control = PowerControl()
        self.control = control
        self.authorize_parent = authorize_parent
        self.closed = False
        self._timer = None
        self._current = {}
        self.columnconfigure(0, weight=1)
        self.bind('<Destroy>', self._on_destroy, add=True)

        ttk.Label(self, text='Питание компьютера', font=('Segoe UI', 17, 'bold')).grid(
            row=0, column=0, sticky='w')
        ttk.Label(
            self, wraplength=900, justify='left',
            text=('Сначала сохраните документы и игры. Windows получит обычный запрос '
                  'без принудительного закрытия программ. Для запуска и отмены нужен PIN родителя.'),
        ).grid(row=1, column=0, sticky='ew', pady=(8, 12))
        options = ttk.Frame(self)
        options.grid(row=2, column=0, sticky='ew')
        ttk.Label(options, text='Задержка, секунд').pack(side='left')
        self.delay = tk.StringVar(self, value='60')
        self.delay_box = ttk.Combobox(
            options, textvariable=self.delay, values=DELAYS, state='readonly', width=7)
        self.delay_box.pack(side='left', padx=8)
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky='ew', pady=12)
        self.restart_button = ttk.Button(
            actions, text='Перезагрузка', command=lambda: self.schedule('restart'), state='disabled')
        self.restart_button.pack(side='left')
        self.shutdown_button = ttk.Button(
            actions, text='Выключение', command=lambda: self.schedule('shutdown'), state='disabled')
        self.shutdown_button.pack(side='left', padx=8)
        self.cancel_button = ttk.Button(actions, text='Отменить', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left')
        self.status = tk.StringVar(self, value='Проверяю состояние…')
        self.notice = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, font=('Segoe UI', 12, 'bold'),
                  wraplength=900).grid(row=4, column=0, sticky='ew')
        ttk.Label(self, textvariable=self.notice, wraplength=900, justify='left').grid(
            row=5, column=0, sticky='ew', pady=(8, 0))
        self.refresh()
        self._schedule_poll()

    def _authorized(self, title):
        if self.authorize_parent is not None and self.authorize_parent(title):
            return True
        self.notice.set('Родительское подтверждение не получено. Действие не выполнено.')
        return False

    def schedule(self, action):
        if self.closed:
            return False
        request = None
        try:
            label = ACTION_LABELS[action]
            delay = int(self.delay.get())
            if delay not in DELAYS:
                raise ValueError('Выберите задержку из списка.')
            previous_warning = ''
            previous_action = ACTION_LABELS.get(self._current.get('action'), 'Питание компьютера')
            if self._current.get('state') == 'dispatched':
                previous_warning = (
                    f'Предыдущий запрос «{previous_action}» уже отправлен Windows; '
                    'его завершение не подтверждено. Новый запрос может повторить действие. '
                    'Сначала проверьте состояние компьютера.\n\n')
            elif self._current.get('state') == 'uncertain':
                previous_warning = (
                    f'Результат предыдущего запроса «{previous_action}» неизвестен: '
                    'Windows могла уже получить команду. Новый запрос может повторить действие. '
                    'Сначала проверьте состояние компьютера.\n\n')
            # One warning before the PIN; no extra confirmation after authorization.
            if not messagebox.askyesno(
                label + ' компьютера',
                previous_warning + f'Сохраните документы и игры.\n\nЧерез {delay} с. Windows получит обычный '
                f'запрос: {label.lower()}. Принудительное закрытие не используется; '
                'программы с несохранёнными данными могут остановить завершение работы.\n\n'
                'Запланировать действие?', parent=self.winfo_toplevel(),
            ):
                self.notice.set('Действие не запланировано.')
                return False
            if not self._authorized(label + ' компьютера'):
                return False
            request = self.control.schedule(action, delay_seconds=delay)
            self._render(request)
            started = self.control.start_worker(request['request_id'])
            self._render(started)
            self.notice.set('Запрос запланирован. До отправки его можно отменить с PIN родителя.'
                            if started.get('state') == 'queued' else '')
            return started.get('state') in ('queued', 'executing', 'dispatched')
        except Exception as error:
            if request is not None:
                self.notice.set('Запрос сохранён, но его запуск не подтверждён. Можно отменить. '
                                + str(error)[:160])
            else:
                self.notice.set('Не удалось запланировать действие. ' + str(error)[:160])
            self.refresh()
            return False

    def cancel(self):
        if self.closed:
            return False
        # Bind cancellation to the displayed request, even if a PIN dialog polls again.
        request_id = self._current.get('request_id')
        if not request_id or not self._current.get('can_cancel'):
            self.notice.set('Нет запроса, который можно отменить.')
            return False
        try:
            if not self._authorized('Отменить перезагрузку или выключение'):
                return False
            self._render(self.control.cancel(request_id))
            self.notice.set('Отложенный запрос отменён.')
            return True
        except Exception as error:
            self.notice.set('Не удалось отменить запрос. ' + str(error)[:160])
            self.refresh()
            return False

    def _render(self, value):
        self._current = dict(value)
        state = value.get('state')
        label = ACTION_LABELS.get(value.get('action'), 'Действие')
        if state == 'queued':
            seconds = max(0, math.ceil(value.get('remaining_seconds', 0)))
            minutes, seconds = divmod(seconds, 60)
            text = f'{label} через {minutes:02}:{seconds:02} · ожидает отправки Windows'
        else:
            text = {
                'idle': 'Нет отложенных запросов.',
                'executing': 'Отправляется запрос Windows…',
                'dispatched': 'Запрос отправлен Windows. Завершение работы не подтверждено.',
                'cancelled': 'Запрос отменён.',
                'failed': 'Не удалось отправить запрос Windows.',
                'uncertain': 'Результат отправки не подтверждён. Проверьте состояние компьютера.',
            }.get(state, 'Неизвестное состояние запроса.')
        self.status.set(text)
        available = state in ('idle', 'cancelled', 'failed', 'dispatched', 'uncertain')
        for button in (self.restart_button, self.shutdown_button):
            button.configure(state='normal' if available else 'disabled')
        self.delay_box.configure(state='readonly' if available else 'disabled')
        self.cancel_button.configure(
            state='normal' if state == 'queued' and value.get('can_cancel') else 'disabled')

    def refresh(self):
        if self.closed:
            return
        try:
            self._render(self.control.status())
        except Exception as error:
            self.notice.set('Не удалось проверить состояние. ' + str(error)[:160])
            for widget in (self.restart_button, self.shutdown_button, self.cancel_button, self.delay_box):
                widget.configure(state='disabled')

    def _schedule_poll(self):
        if not self.closed:
            self._timer = self.after(1000, self._poll)

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
