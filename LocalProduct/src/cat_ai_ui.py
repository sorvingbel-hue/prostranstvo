"""Explicit one-time OpenAI API settings; never makes a provider request.

The key entry is empty on open, masked, and cleared on Save or Close. Workers
receive plain values and a queue, never Tk widgets or bound window methods.
"""
from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from tkinter import simpledialog, ttk

from study_guard import StudyGuard


BACKGROUND = '#fffaf0'
PAPER = '#ffffff'
INK = '#40352e'
MUTED = '#75695e'
ACCENT = '#457d6a'

DISCLOSURE = (
    'Текст сообщения и подготовленный контекст будут передаваться с этого '
    'компьютера во внешний OpenAI API. Запросы оплачиваются по условиям '
    'вашего API-проекта. Это окно не отправляет тестовых запросов.'
)
ZDR_NOTICE = (
    'Для обработки личных данных ребёнка младше 13 лет сначала требуется '
    'Zero Data Retention в API. Параметр store=false сам по себе не включает '
    'Zero Data Retention. Одобрение и настройка выполняются у провайдера.'
)
ATTESTATION_NOTICE = (
    'Эта галочка и ссылка — заявление оператора. Приложение не проверяет '
    'одобрение и настройки хранения данных в OpenAI '
    '(provider_privacy_verified=false).'
)
ACCOUNT_NOTICE = (
    'Для открытия и сохранения настроек нужен PIN родителя. Он ограничивает '
    'действия в интерфейсе. При общей учётной записи Windows доступ к файлам '
    'и запуск собственного кода остаются общими.'
)


def require_parent_pin(master, guard=None):
    """Authorize only a visible UI action; never pass the PIN to a worker."""
    guard = guard if guard is not None else StudyGuard()
    try:
        configured = guard.configured()
    except Exception:
        raise PermissionError('Не удалось проверить родительский PIN. Попробуйте позже.') from None
    if configured is not True:
        raise PermissionError('Сначала настройте родительский PIN на вкладке «Уроки».')
    pin = None
    try:
        pin = simpledialog.askstring(
            'Подтверждение родителя', 'Введите PIN родителя.',
            show='•', parent=master.winfo_toplevel(),
        )
        if pin is None:
            notice = 'Подтверждение родителя отменено.'
        elif guard.verify(pin) is True:
            return
        else:
            notice = 'PIN родителя не подошёл.'
    except Exception:
        notice = 'Не удалось проверить родительский PIN. Попробуйте позже.'
    finally:
        pin = None
    raise PermissionError(notice)


def collect_values(enabled, model, daily_limit, output_limit, consent, zdr, reference):
    """Small UI validation; ApiSettings remains the authoritative validator."""
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', model.strip()):
        raise ValueError('model')
    for value in (daily_limit, output_limit):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,6}', value.strip()):
            raise ValueError('limits')
    daily, output = int(daily_limit.strip()), int(output_limit.strip())
    if not 1 <= daily <= 500 or not 128 <= output <= 1200:
        raise ValueError('limits')
    if (not isinstance(reference, str) or len(reference) > 200
            or any(ord(c) < 32 for c in reference)
            or reference.strip().lower().startswith(('sk-', 'sess-'))):
        raise ValueError('reference')
    return {
        'enabled': bool(enabled), 'model': model.strip(),
        'daily_request_limit': daily, 'max_output_tokens': output,
        'parental_consent': bool(consent), 'zdr_attested': bool(zdr),
        'approval_reference': reference.strip(),
    }


def local_status_text(snapshot):
    """Readiness is a local gate, not proof of live provider access or privacy."""
    if not isinstance(snapshot, dict):
        return 'Не удалось прочитать локальные настройки. API-доступ не проверялся.'
    if not snapshot.get('enabled'):
        return 'Внешний ИИ выключен. Котик использует локальные ответы.'
    missing = []
    if not snapshot.get('key_configured'):
        missing.append('API-ключ')
    if not snapshot.get('parental_consent'):
        missing.append('согласие на передачу данных')
    if not snapshot.get('zdr_attested'):
        missing.append('заявление об одобренном и включённом ZDR')
    if not snapshot.get('approval_reference'):
        missing.append('ссылка или номер подтверждения ZDR')
    if missing:
        return 'Включение сохранено, но отправка пока недоступна. Требуется: ' + ', '.join(missing) + '.'
    if snapshot.get('ready') is True:
        return ('Локальные условия запуска выполнены. Доступ к модели, баланс '
                'и одобрение ZDR у провайдера этим окном не проверялись.')
    return ('Настройки сохранены, но шлюз пока не готов к отправке. '
            'Для локальных ответов можно отдельно выбрать «Офлайн».')


def _snapshot(settings, gateway):
    result = dict(settings.snapshot())
    if gateway is not None:
        status = gateway.status()
        if isinstance(status, dict):
            for field in ('ready', 'state', 'provider_privacy_verified', 'mode'):
                if field in status:
                    result[field] = status[field]
    # UI must never convert an operator checkbox into a provider verification.
    result['provider_privacy_verified'] = False
    return result


def read_settings_worker(settings, gateway, replies):
    try:
        snapshot = _snapshot(settings, gateway)
    except Exception:
        snapshot = None
    replies.put({'snapshot': snapshot})


def save_settings_worker(settings, gateway, values, key_action, key_value, replies):
    """No widgets, provider calls, arbitrary exceptions or secrets in results."""
    key_changed = False
    try:
        if key_action == 'set':
            settings.set_api_key(key_value)
            key_changed = True
        elif key_action == 'clear':
            settings.clear_api_key()
            key_changed = True
        elif key_action != 'keep':
            raise ValueError('invalid_key_action')
        settings.configure(**values)
        snapshot = _snapshot(settings, gateway)
        replies.put({'status': 'saved', 'snapshot': snapshot})
    except Exception:
        # A key operation may have committed before another setting failed.
        snapshot = None
        try:
            snapshot = _snapshot(settings, gateway)
        except Exception:
            pass
        replies.put({'status': 'failed', 'key_changed': key_changed, 'snapshot': snapshot})
    finally:
        key_value = None


class AiSettingsWindow(tk.Toplevel):
    def __init__(self, master, settings, gateway, *, parent_guard=None):
        require_parent_pin(master, parent_guard)
        super().__init__(master)
        self._parent_guard = parent_guard
        self.settings, self.gateway = settings, gateway
        self.title('Мурчик · настройки внешнего ИИ')
        self.geometry('690x760')
        self.minsize(580, 520)
        self.configure(background=BACKGROUND)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._closed = False
        self._operation = None
        self._status_loaded = False
        self._close_after_save = False
        self._key_action = 'keep'
        self._suppress_key_trace = False
        self._queue = queue.Queue(maxsize=1)
        self._poll_id = None
        self._controls = []
        self._wrap_labels = []
        self._build()
        self.key_var.trace_add('write', self._key_edited)
        self.refresh()

    def _build(self):
        head = tk.Frame(self, background=BACKGROUND)
        head.pack(fill='x', padx=22, pady=(17, 12))
        tk.Label(head, text='Внешний ИИ для Мурчика', background=BACKGROUND,
                 foreground=INK, font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        tk.Label(head, text='Одна настройка · локальные ответы доступны без API',
                 background=BACKGROUND, foreground=MUTED, font=('Segoe UI', 10)).pack(anchor='w', pady=(3, 0))

        shell = tk.Frame(self, background=BACKGROUND)
        shell.pack(fill='both', expand=True, padx=(22, 12))
        self.canvas = tk.Canvas(shell, background=PAPER, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.page = tk.Frame(self.canvas, background=PAPER, padx=16, pady=15)
        self._page_id = self.canvas.create_window((0, 0), window=self.page, anchor='nw')
        self.page.columnconfigure(0, weight=1)
        self.page.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._resize)
        self.bind('<MouseWheel>', self._mousewheel)

        self.enabled_var = tk.BooleanVar(value=False)
        self._check('Включить ответы через OpenAI API', self.enabled_var, 0, bold=True)
        self._paragraph(DISCLOSURE, 1)

        self._field_label('API-ключ', 2)
        key_row = tk.Frame(self.page, background=PAPER)
        key_row.grid(row=3, column=0, sticky='ew', pady=(4, 5))
        key_row.columnconfigure(0, weight=1)
        self.key_var = tk.StringVar(value='')
        self.key_entry = ttk.Entry(key_row, textvariable=self.key_var, show='●', font=('Segoe UI', 11))
        self.key_entry.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self._controls.append(self.key_entry)
        self.clear_key_button = ttk.Button(key_row, text='Удалить ключ', command=self.stage_key_clear)
        self.clear_key_button.grid(row=0, column=1)
        self._controls.append(self.clear_key_button)
        self.key_status = self._paragraph('Ключ не загружается в это поле. Пустое поле сохраняет прежний ключ.', 4)

        self._field_label('Модель API', 5)
        self.model_var = tk.StringVar(value='gpt-6-luna')
        self.model_entry = ttk.Entry(self.page, textvariable=self.model_var, font=('Segoe UI', 11))
        self.model_entry.grid(row=6, column=0, sticky='ew', pady=(4, 10))
        self._controls.append(self.model_entry)

        limits = tk.Frame(self.page, background=PAPER)
        limits.grid(row=7, column=0, sticky='ew', pady=(0, 10))
        limits.columnconfigure(0, weight=1)
        limits.columnconfigure(1, weight=1)
        tk.Label(limits, text='Запросов в день', background=PAPER, foreground=INK,
                 font=('Segoe UI', 10)).grid(row=0, column=0, sticky='w')
        tk.Label(limits, text='Токенов в одном ответе', background=PAPER, foreground=INK,
                 font=('Segoe UI', 10)).grid(row=0, column=1, sticky='w')
        self.daily_var, self.output_var = tk.StringVar(value='50'), tk.StringVar(value='400')
        self.daily_entry = ttk.Spinbox(limits, from_=1, to=500, textvariable=self.daily_var, width=12)
        self.output_entry = ttk.Spinbox(limits, from_=128, to=1200, increment=64, textvariable=self.output_var, width=12)
        self.daily_entry.grid(row=1, column=0, sticky='ew', padx=(0, 10), pady=(4, 0))
        self.output_entry.grid(row=1, column=1, sticky='ew', pady=(4, 0))
        self._controls.extend((self.daily_entry, self.output_entry))
        self._paragraph('Лимит запросов ограничивает использование, но не является гарантированным денежным бюджетом. Ограничения расходов API-проекта задаются у провайдера.', 8)

        self.consent_var = tk.BooleanVar(value=False)
        self._check('Я как родитель/оператор разрешаю указанную передачу данных во внешний API.', self.consent_var, 9)
        self._paragraph(ZDR_NOTICE, 10)
        self.zdr_var = tk.BooleanVar(value=False)
        self._check('Подтверждаю как оператор: для выбранного API-проекта заранее одобрен и включён Zero Data Retention.', self.zdr_var, 11)
        self._field_label('Ссылка или номер подтверждения ZDR — без ключей и паролей', 12)
        self.reference_var = tk.StringVar(value='')
        self.reference_entry = ttk.Entry(self.page, textvariable=self.reference_var)
        self.reference_entry.grid(row=13, column=0, sticky='ew', pady=(4, 9))
        self._controls.append(self.reference_entry)
        self._paragraph(ATTESTATION_NOTICE, 14)
        self._paragraph(ACCOUNT_NOTICE, 15)

        self.status_label = tk.Label(self, text='', background=BACKGROUND, foreground=INK,
                                     font=('Segoe UI', 10), justify='left', anchor='w', wraplength=640)
        self.status_label.pack(fill='x', padx=22, pady=(12, 7))
        footer = tk.Frame(self, background=BACKGROUND)
        footer.pack(fill='x', padx=22, pady=(0, 17))
        self.save_button = ttk.Button(footer, text='Сохранить настройки', command=self.save)
        self.save_button.pack(side='left')
        self.refresh_button = ttk.Button(footer, text='Обновить локальный статус', command=self.refresh)
        self.refresh_button.pack(side='left', padx=8)
        ttk.Button(footer, text='Закрыть', command=self.close).pack(side='right')
        self._controls.extend((self.save_button, self.refresh_button))

    def _field_label(self, text, row):
        label = tk.Label(self.page, text=text, background=PAPER, foreground=INK,
                         font=('Segoe UI', 10, 'bold'), justify='left', anchor='w')
        label.grid(row=row, column=0, sticky='ew', pady=(8, 0))
        self._wrap_labels.append(label)
        return label

    def _paragraph(self, text, row):
        label = tk.Label(self.page, text=text, background=PAPER, foreground=MUTED,
                         font=('Segoe UI', 10), justify='left', anchor='w', wraplength=590)
        label.grid(row=row, column=0, sticky='ew', pady=(5, 10))
        self._wrap_labels.append(label)
        return label

    def _check(self, text, variable, row, bold=False):
        control = tk.Checkbutton(self.page, text=text, variable=variable, background=PAPER,
                                 foreground=INK, activebackground=PAPER, justify='left', anchor='w',
                                 font=('Segoe UI', 11, 'bold') if bold else ('Segoe UI', 10),
                                 wraplength=590)
        control.grid(row=row, column=0, sticky='ew', pady=(4, 3))
        self._controls.append(control)
        self._wrap_labels.append(control)
        return control

    def _resize(self, event):
        self.canvas.itemconfigure(self._page_id, width=event.width)
        for label in self._wrap_labels:
            label.configure(wraplength=max(300, event.width - 38))
        self.status_label.configure(wraplength=max(300, self.winfo_width() - 48))

    def _mousewheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(-int(event.delta / 120), 'units')

    @property
    def _busy(self):
        return self._operation is not None

    def _set_operation(self, operation):
        self._operation = operation
        for control in self._controls:
            available = not self._busy and (self._status_loaded or control is self.refresh_button)
            control.configure(state='normal' if available else 'disabled')

    def _start(self, operation, worker, *arguments):
        self._set_operation(operation)
        thread = threading.Thread(target=worker,
                                  args=(self.settings, self.gateway, *arguments, self._queue),
                                  daemon=True, name='kitten-api-settings-' + operation)
        try:
            thread.start()
        except RuntimeError:
            self._set_operation(None)
            self.status_label.configure(text='Не удалось запустить операцию с настройками. Попробуйте ещё раз.')
            return
        self._poll_id = self.after(50, self._poll)

    def _clear_key_entry(self):
        self._suppress_key_trace = True
        try:
            self.key_var.set('')
        finally:
            self._suppress_key_trace = False

    def _reset_privacy(self):
        self.consent_var.set(False)
        self.zdr_var.set(False)
        self.reference_var.set('')

    def _key_edited(self, *unused):
        if self._suppress_key_trace or self._closed:
            return
        self._reset_privacy()
        if self.key_var.get():
            self._key_action = 'set'
            self.key_status.configure(text='Новый ключ может относиться к другому API-проекту. После ввода заново подтвердите передачу данных и ZDR для этого проекта.')
        else:
            self._key_action = 'keep'

    def _load(self, snapshot):
        self.enabled_var.set(bool(snapshot.get('enabled', False)))
        self.model_var.set(str(snapshot.get('model', 'gpt-6-luna'))[:120])
        self.daily_var.set(str(snapshot.get('daily_request_limit', 50)))
        self.output_var.set(str(snapshot.get('max_output_tokens', 400)))
        self.consent_var.set(bool(snapshot.get('parental_consent', False)))
        self.zdr_var.set(bool(snapshot.get('zdr_attested', False)))
        reference = snapshot.get('approval_reference', '')
        self.reference_var.set(reference[:200] if isinstance(reference, str) else '')
        self._clear_key_entry()
        self._key_action = 'keep'
        self.key_status.configure(text=('Ключ сохранён. Пустое поле оставит его без изменения.' if snapshot.get('key_configured') else 'Ключ не задан. Введите его только в это скрытое поле.'))
        self.status_label.configure(text=local_status_text(snapshot))

    def refresh(self):
        if self._busy or self._closed:
            return
        self.status_label.configure(text='Читаю локальные настройки…')
        self._start('read', read_settings_worker)

    def stage_key_clear(self):
        if self._busy or self._closed or not self._status_loaded:
            return
        self._clear_key_entry()
        self._reset_privacy()
        self._key_action = 'clear'
        self.key_status.configure(text='Ключ будет удалён только после нажатия «Сохранить настройки».')

    def save(self):
        if self._busy or self._closed or not self._status_loaded:
            return
        try:
            require_parent_pin(self, self._parent_guard)
        except PermissionError as error:
            self._clear_key_entry()
            self.status_label.configure(text=str(error))
            return
        try:
            values = collect_values(self.enabled_var.get(), self.model_var.get(),
                                    self.daily_var.get(), self.output_var.get(),
                                    self.consent_var.get(), self.zdr_var.get(), self.reference_var.get())
            key_value = self.key_var.get().strip()
            if len(key_value) > 2048 or any(ord(c) < 33 for c in key_value):
                raise ValueError('key')
        except (ValueError, tk.TclError):
            self._clear_key_entry()
            self.status_label.configure(text='Проверьте модель, целые числовые лимиты и ссылку подтверждения. Ключ не должен содержать пробелы. Настройки не сохранялись.')
            return
        key_action = 'set' if key_value else ('clear' if self._key_action == 'clear' else 'keep')
        self._clear_key_entry()
        self.status_label.configure(text='Сохраняю настройки на этом компьютере. Запрос к OpenAI не выполняется…')
        self._start('save', save_settings_worker, values, key_action, key_value)

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(50, self._poll)
            return
        operation = self._operation
        snapshot = result.get('snapshot')
        self._status_loaded = isinstance(snapshot, dict)
        if self._status_loaded:
            self._load(snapshot)
        self._set_operation(None)
        if operation == 'read':
            if not self._status_loaded:
                self.status_label.configure(text='Не удалось прочитать локальные настройки. Нажмите «Обновить локальный статус».')
            return
        if result.get('status') == 'saved':
            self.status_label.configure(text='Настройки сохранены. ' + local_status_text(snapshot))
        else:
            partial = ' Операция с ключом выполнена; остальные настройки могли сохраниться не полностью.' if result.get('key_changed') else ''
            self.status_label.configure(text='Сохранение не завершено.' + partial + ' Проверьте локальный статус перед повтором. Подробности ошибок и ключ не выводятся.')
        if self._close_after_save:
            self._close_after_save = False
            if result.get('status') == 'saved':
                self.close()

    @property
    def saving(self):
        return self._operation == 'save'

    def close(self):
        if self._closed:
            return
        self._clear_key_entry()
        if self.saving:
            self._close_after_save = True
            self.status_label.configure(text='Окно закроется после сохранения настроек.')
            return
        self._closed = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.destroy()


def open_ai_settings(master, settings, gateway, *, parent_guard=None):
    """Open explicitly; the caller may keep a reference for single-window use."""
    return AiSettingsWindow(master, settings, gateway, parent_guard=parent_guard)
