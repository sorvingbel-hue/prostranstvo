"""Explicit, parent-authorized cleanup of three regenerable shader caches."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from cat_ai_ui import require_parent_pin


SCOPES = (
    ('nvidia_dxcache', 'NVIDIA · DirectX shader cache'),
    ('nvidia_glcache', 'NVIDIA · OpenGL shader cache'),
    ('windows_d3dscache', 'Windows · Direct3D shader cache'),
)
CACHE_NOTICE = 'Кэши будут созданы заново; первое открытие игры может быть медленнее.'
MIN_AGE_DAYS = 7


def format_bytes(value):
    return f'{value / 1024 ** 2:,.2f} МиБ'.replace(',', ' ')


def _cleanup_worker(cleaner, operation, argument, replies):
    """Worker receives plain data and a queue, never a widget or parent PIN."""
    try:
        if cleaner is None:
            from cache_cleanup import CacheCleanup
            cleaner = CacheCleanup()
        if operation == 'prepare':
            scopes = cleaner.scope_catalog()
            result = cleaner.prepare(list(argument), min_age_days=MIN_AGE_DAYS)
            replies.put({'operation': operation, 'result': result, 'scopes': scopes})
        else:
            result = cleaner.execute(argument, approved=True)
            replies.put({'operation': operation, 'result': result})
    except Exception:
        # A partial execution can already have changed files. Never report success.
        replies.put({'operation': operation, 'failed': True})


def _validated_plan(value, scope_ids):
    if not isinstance(value, dict):
        raise ValueError('Invalid plan')
    if not isinstance(value.get('plan_id'), str) or not value['plan_id']:
        raise ValueError('Missing plan identity')
    entries, totals = value.get('entries'), value.get('totals')
    if not isinstance(entries, list) or not isinstance(totals, dict):
        raise ValueError('Missing plan entries')
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('scope_id') not in scope_ids
                or not isinstance(entry.get('path'), str)
                or type(entry.get('size')) is not int or entry['size'] < 0):
            raise ValueError('Invalid plan entry')
    if (type(totals.get('files')) is not int or totals['files'] != len(entries)
            or type(totals.get('logical_bytes')) is not int
            or totals['logical_bytes'] != sum(entry['size'] for entry in entries)):
        raise ValueError('Inconsistent plan totals')
    if type(value.get('partial')) is not bool or not isinstance(value.get('errors'), list):
        raise ValueError('Missing analysis outcome')
    return deepcopy(value)


def _count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, list):
        return len(value)
    return None


class CacheCleanupPanel(ttk.Frame):
    def __init__(self, parent, cleaner=None, parent_guard=None):
        super().__init__(parent, padding=16)
        self.cleaner = cleaner
        self.parent_guard = parent_guard
        self._operation = None
        self._closed = False
        self._plan = None
        self._plan_scopes = ()
        self._replies = queue.Queue(maxsize=1)
        self._poll_id = None
        self.scope_vars = {}
        self._scope_controls = []
        ttk.Label(self, text='Кэши графики', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(self, text='Выберите категории и сначала просмотрите план. В анализ входят файлы старше 7 дней.',
                  wraplength=880, justify='left').pack(anchor='w', pady=(8, 4))
        for scope_id, title in SCOPES:
            variable = tk.BooleanVar(self, value=False)
            self.scope_vars[scope_id] = variable
            control = ttk.Checkbutton(self, text=title, variable=variable,
                                      command=self._selection_changed)
            control.pack(anchor='w', pady=2)
            self._scope_controls.append(control)
        ttk.Label(self, text=CACHE_NOTICE, wraplength=880, justify='left').pack(anchor='w', pady=(8, 12))
        buttons = ttk.Frame(self)
        buttons.pack(fill='x')
        self.analyze_button = ttk.Button(buttons, text='Анализировать выбранные кэши', command=self.analyze)
        self.analyze_button.pack(side='left')
        self.clear_button = ttk.Button(buttons, text='Очистить по плану', command=self.clear)
        self.clear_button.pack(side='left', padx=(8, 0))
        self.notice = tk.StringVar(self, value='Выберите категории. Анализ и очистка запускаются отдельными кнопками.')
        ttk.Label(self, textvariable=self.notice, wraplength=880, justify='left').pack(fill='x', pady=12)
        listing = ttk.Frame(self)
        listing.pack(fill='both', expand=True)
        self.details = tk.Text(listing, wrap='none', height=16, state='disabled')
        vertical = ttk.Scrollbar(listing, orient='vertical', command=self.details.yview)
        horizontal = ttk.Scrollbar(listing, orient='horizontal', command=self.details.xview)
        self.details.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.details.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.bind('<Destroy>', self._destroyed, add='+')
        self._controls()

    @property
    def busy(self):
        return self._operation is not None

    def _selected(self):
        return tuple(scope_id for scope_id, _ in SCOPES if self.scope_vars[scope_id].get())

    def _controls(self):
        state = 'disabled' if self.busy else 'normal'
        for control in (*self._scope_controls, self.analyze_button):
            control.configure(state=state)
        ready = not self.busy and self._plan is not None and bool(self._plan['entries'])
        self.clear_button.configure(state='normal' if ready else 'disabled')

    def _text(self, value):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', value)
        self.details.configure(state='disabled')

    def _selection_changed(self):
        if self.busy or self._closed:
            return
        self._plan = None
        self._plan_scopes = ()
        self._text('')
        self.notice.set('Категории изменены. Подготовьте новый план.')
        self._controls()

    def _start(self, operation, argument):
        self._operation = operation
        self._controls()
        worker = threading.Thread(target=_cleanup_worker,
                                  args=(self.cleaner, operation, argument, self._replies),
                                  daemon=True, name='shader-cache-' + operation)
        try:
            worker.start()
        except RuntimeError:
            self._operation = None
            self.notice.set('Операция не запущена. Попробуйте ещё раз.')
            self._controls()
            return
        self._poll_id = self.after(50, self._poll)

    def analyze(self):
        if self.busy or self._closed:
            return
        selected = self._selected()
        if not selected:
            self.notice.set('Выберите хотя бы одну категорию кэша.')
            return
        self._plan = None
        self._plan_scopes = selected
        self._text('')
        self.notice.set('Анализирую выбранные кэши. Файлы пока не удаляются…')
        self._start('prepare', selected)

    def clear(self):
        if self.busy or self._closed or self._plan is None or not self._plan['entries']:
            return
        captured = self._plan
        plan_id = captured['plan_id']
        self._operation = 'authorize'
        self._controls()
        totals = captured['totals']
        summary = (f'План {plan_id}\nУдалить {totals["files"]} файлов, '
                   f'логический объём {format_bytes(totals["logical_bytes"])}?\n\n{CACHE_NOTICE}')
        if captured['partial']:
            summary += '\n\nАнализ частичный. Ошибок: ' + str(len(captured['errors'])) + '.'
        try:
            if not messagebox.askyesno('Очистить проверенные кэши?', summary, parent=self.winfo_toplevel()):
                self.notice.set('Очистка отменена. План сохранён для просмотра.')
                return
            require_parent_pin(self, self.parent_guard)
            if self._closed:
                return
            if self._plan is not captured or self._selected() != self._plan_scopes:
                self._plan = None
                self.notice.set('Выбор изменился. Подготовьте новый план.')
                return
            self.notice.set('Выполняю подтверждённый план ' + plan_id + '…')
            self._start('execute', plan_id)
        except PermissionError as error:
            self.notice.set(str(error))
        except Exception:
            self.notice.set('Подтверждение не завершено. Очистка не запущена.')
        finally:
            if self._operation == 'authorize':
                self._operation = None
            if not self._closed:
                self._controls()

    def _show_plan(self, plan, scopes):
        self._plan = _validated_plan(plan, self._plan_scopes)
        totals = self._plan['totals']
        text = (f'План {self._plan["plan_id"]}\nСоздан: {self._plan.get("created_utc", "—")}\n'
                f'Действителен до: {self._plan.get("expires_utc", "—")}\n'
                f'Файлов: {totals["files"]}\nЛогический объём: {format_bytes(totals["logical_bytes"])}\n')
        if isinstance(scopes, list):
            for scope in scopes:
                if isinstance(scope, dict) and scope.get('id') in self._plan_scopes:
                    text += '\n' + str(scope.get('title', scope['id'])) + '\n' + str(scope.get('root', '')) + '\n'
        text += '\nФайлы плана\n'
        text += '\n'.join(f'{entry["size"]} байт · {entry["path"]}' for entry in self._plan['entries'])
        if self._plan['errors']:
            text += '\n\nОшибки анализа\n' + '\n'.join(str(error) for error in self._plan['errors'])
        self._text(text)
        partial = 'Анализ частичный. ' if self._plan['partial'] else 'План подготовлен. '
        self.notice.set(partial + f'{totals["files"]} файлов, {format_bytes(totals["logical_bytes"])} логического объёма. Проверьте список.')

    def _show_receipt(self, receipt):
        self._plan = None
        if not isinstance(receipt, dict):
            self.notice.set('Получен неполный результат. Проверьте журнал очистки перед новым запуском.')
            return
        deleted, skipped, errors = (_count(receipt.get(key)) for key in ('deleted_files', 'skipped', 'errors'))
        size = receipt.get('deleted_bytes')
        if deleted is None or type(size) is not int or size < 0 or skipped is None or errors is None:
            self.notice.set('Получен неполный результат. Проверьте журнал очистки перед новым запуском.')
            return
        status = str(receipt.get('status', 'unknown'))
        self.notice.set(f'Результат {status}. Удалено {deleted} файлов ({format_bytes(size)} логического объёма). Пропущено {skipped}, ошибок {errors}.')
        lines = [self.notice.get()]
        for key, title in (('before_free', 'Свободное место до'), ('after_free', 'Свободное место после'),
                           ('receipt_path', 'Журнал')):
            if key in receipt:
                lines.append(title + ': ' + str(receipt[key]))
        if receipt.get('errors'):
            lines.extend(['Ошибки', str(receipt['errors'])])
        self._text('\n\n'.join(lines))

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            reply = self._replies.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(50, self._poll)
            return
        operation = self._operation
        self._operation = None
        try:
            if reply.get('failed'):
                if operation == 'execute':
                    self._plan = None
                    self.notice.set('Очистка не завершена. Часть файлов могла быть удалена. Проверьте журнал перед новым анализом.')
                else:
                    self.notice.set('Не удалось подготовить план. Очистка не запускалась.')
            elif operation == 'prepare':
                self._show_plan(reply.get('result'), reply.get('scopes'))
            elif operation == 'execute':
                self._show_receipt(reply.get('result'))
        except (TypeError, ValueError, KeyError):
            self._plan = None
            self.notice.set('Результат не прошёл проверку. Подготовьте новый план.')
        self._controls()

    def _destroyed(self, event):
        if event.widget is not self:
            return
        self._closed = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
