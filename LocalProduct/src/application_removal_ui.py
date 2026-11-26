"""Explicit application-manager UI; construction performs no discovery or launch."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from cat_ai_ui import require_parent_pin


STATUSES = {'available': 'Доступно', 'protected': 'Защищено',
            'blocked_by_policy': 'Заблокировано политикой', 'unsupported': 'Только сведения'}
ACTIONS = {'steam_uninstall': 'Штатное удаление через Steam',
           'msi_uninstall': 'Штатное удаление Windows Installer',
           'windows_settings': 'Открыть «Установленные приложения» Windows'}
RECEIPT_TEXT = {
    'not_approved': 'План не подтверждён. Штатное удаление не запускалось.',
    'expired': 'Срок плана истёк. Подготовьте новый план. Штатное удаление не запускалось.',
    'stale': 'Сведения о приложении изменились. Подготовьте новый план. Штатное удаление не запускалось.',
    'protected': 'Приложение защищено. Запрос отклонён; штатное удаление не запускалось.',
    'blocked_by_policy': 'Действие заблокировано политикой. Повтор другим способом не выполняется.',
    'unsupported': 'Для этой записи нет поддерживаемого штатного действия. Удаление не запускалось.',
    'dispatching': ('Журнал содержит передачу запроса. Окно могло открыться; результат неизвестен. '
                    'Удаление не подтверждено. Автоматического повтора нет.'),
    'requested': 'Штатное окно/запрос открыт. Приложение ещё не подтверждено как удалённое.',
    'failed': 'Штатный обработчик сообщил ошибку. Удаление не подтверждено; автоматического повтора нет.',
    'uncertain': ('Результат запроса неоднозначен; окно могло открыться. '
                  'Удаление не подтверждено. Автоматического повтора нет.'),
    'not_requested': ('В журнале нет подтверждённого запуска штатного удаления. '
                      'Эта проверка не открывает деинсталлятор.'),
    'verification_incomplete': ('Не удалось полностью проверить регистрацию и остатки. '
                                'Удаление не подтверждено; нужен ручной разбор.'),
    'registration_absent': ('Запись регистрации не найдена. Это наблюдение, не доказательство '
                            'удаления всех файлов или освобождения места.'),
    'registration_absent_remnants': ('Запись регистрации не найдена; остались файлы. '
                                     'Нужен ручной разбор, автоматического удаления остатков нет.'),
    'registration_absent_unverified_path': ('Запись регистрации не найдена. Папку установки '
                                            'безопасно проверить не удалось. Это не доказательство '
                                            'удаления файлов или освобождения места; нужен ручной разбор.'),
}
NOTICE = ('Панель открывает штатное удаление после проверки плана и PIN родителя. '
          'Окно открывается только по вашему явному нажатию; автоматически во время игр не запускается. '
          'Открытое окно ещё не означает удаление. Защищённые и ранее заблокированные '
          'цели недоступны; обход прав или политики не выполняется.')


def _text(value, limit=8192):
    return isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def _size(value):
    if value is None:
        return 'не заявлен'
    if type(value) is not int or value < 0:
        raise ValueError('Invalid reported size')
    return f'{value / 1024 ** 2:,.2f} МиБ'.replace(',', ' ')


def _catalog(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list):
        raise ValueError('Invalid catalog')
    items, seen = [], set()
    for item in value['items']:
        if (not isinstance(item, dict) or not _text(item.get('id'), 256) or not item['id']
                or item['id'] in seen or not _text(item.get('name'), 1024) or not item['name']
                or item.get('status') not in STATUSES
                or not all(_text(item.get(key, '')) for key in ('publisher', 'install_path', 'source', 'reason'))):
            raise ValueError('Invalid catalog item')
        _size(item.get('reported_bytes'))
        if item['status'] == 'available' and item.get('action') not in ACTIONS:
            raise ValueError('Unsupported action')
        seen.add(item['id']);items.append(deepcopy(item))
    warnings = value.get('warnings', [])
    if not isinstance(warnings, list) or not all(isinstance(v, str) for v in warnings):
        raise ValueError('Invalid warnings')
    return items, list(warnings)


def _plan(value, item):
    if (not isinstance(value, dict) or not _text(value.get('plan_id'), 256) or not value['plan_id']
            or value.get('id') != item['id'] or value.get('name') != item['name']
            or value.get('action') != item['action'] or value['action'] not in ACTIONS
            or not _text(value.get('target')) or not value['target']
            or not _text(value.get('created_utc')) or not _text(value.get('expires_utc'))):
        raise ValueError('Invalid application plan')
    _size(value.get('reported_bytes'))
    return deepcopy(value)


def _application_worker(manager, operation, argument, replies):
    """Only plain data crosses the queue; this function never touches Tk or PIN."""
    try:
        if manager is None:
            from application_removal import ApplicationRemovalManager
            manager = ApplicationRemovalManager()
        if operation == 'catalog':
            result = manager.catalog()
        elif operation == 'prepare':
            result = manager.prepare(argument)
        elif operation == 'request':
            result = manager.request(argument, approved=True)
        elif operation == 'verify':
            result = manager.verify(argument)
        else:
            raise ValueError('Unknown operation')
        replies.put({'operation': operation, 'result': result})
    except Exception:
        # Never expose raw command lines, exceptions or secrets from a provider.
        replies.put({'operation': operation, 'failed': True})


class ApplicationRemovalPanel(ttk.Frame):
    def __init__(self, parent, manager=None, parent_guard=None):
        super().__init__(parent, padding=16)
        self.manager, self.parent_guard = manager, parent_guard
        self._closed = False
        self._operation = None
        self._poll_id = None
        self._replies = queue.Queue(maxsize=1)
        self._items = {}
        self._selected_id = None
        self._plan = None
        self._receipt = None
        self._context = None
        ttk.Label(self, text='Приложения · штатное удаление', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(self, text=NOTICE, wraplength=950, justify='left').pack(fill='x', pady=(8, 12))
        buttons = ttk.Frame(self);buttons.pack(fill='x')
        self.refresh_button = ttk.Button(buttons, text='Обновить список', command=self.refresh)
        self.prepare_button = ttk.Button(buttons, text='Подготовить план', command=self.prepare)
        self.request_button = ttk.Button(buttons, text='Открыть штатное удаление', command=self.request)
        self.verify_button = ttk.Button(buttons, text='Проверить результат', command=self.verify)
        for button in (self.refresh_button, self.prepare_button, self.request_button, self.verify_button):
            button.pack(side='left', padx=(0, 8))
        self.notice = tk.StringVar(self, value='Список ещё не запрошен. Нажмите «Обновить список».')
        ttk.Label(self, textvariable=self.notice, wraplength=950, justify='left').pack(fill='x', pady=12)
        listing = ttk.Frame(self);listing.pack(fill='both', expand=True)
        self.table = ttk.Treeview(listing, columns=('name', 'publisher', 'size', 'source', 'status'),
                                  show='headings', selectmode='browse', height=9)
        for key, title, width in (('name', 'Название', 260), ('publisher', 'Издатель', 170),
                                  ('size', 'Заявлено', 110), ('source', 'Источник', 120), ('status', 'Статус', 190)):
            self.table.heading(key, text=title);self.table.column(key, width=width, minwidth=70)
        self.table.tag_configure('held', foreground='#666666')
        scrollbar = ttk.Scrollbar(listing, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side='left', fill='both', expand=True);scrollbar.pack(side='right', fill='y')
        self.details = tk.Text(self, height=9, wrap='word', state='disabled')
        self.details.pack(fill='both', expand=True, pady=(10, 0))
        self.table.bind('<<TreeviewSelect>>', self._selection_changed)
        self.bind('<Destroy>', self._destroyed, add='+')
        self._controls()

    @property
    def busy(self):
        return self._operation is not None

    def _selected(self):
        chosen = self.table.selection()
        return self._items.get(chosen[0]) if len(chosen) == 1 else None

    def _controls(self):
        enabled = not self.busy and not self._closed
        self.refresh_button.configure(state='normal' if enabled else 'disabled')
        item = self._selected()
        available = bool(item and item['status'] == 'available')
        self.prepare_button.configure(state='normal' if enabled and available else 'disabled')
        self.request_button.configure(state='normal' if enabled and available and self._plan is not None else 'disabled')
        self.verify_button.configure(state='normal' if enabled and self._receipt is not None else 'disabled')

    def _text(self, text):
        self.details.configure(state='normal');self.details.delete('1.0', 'end')
        self.details.insert('1.0', text);self.details.configure(state='disabled')

    def _selection_changed(self, _event=None):
        if self._closed:
            return
        item = self._selected()
        ident = item['id'] if item else None
        if self.busy:
            # A modal dialog can run nested Tk events. Preserve its exact capture;
            # request() checks the actual selection again after authorization.
            return
        if ident != self._selected_id:
            self._selected_id = ident;self._plan = None;self._receipt = None
        if item:
            self._text(f'{item["name"]}\nИздатель: {item.get("publisher", "")}\n'
                       f'Источник: {item.get("source", "")}\nПапка: {item.get("install_path", "")}\n'
                       f'Заявленный размер: {_size(item.get("reported_bytes"))}; освобождение не обещается.\n'
                       f'Статус: {STATUSES[item["status"]]}\n{item.get("reason", "")}')
            if item['status'] != 'available':
                self.table.selection_remove(self.table.selection())
                self._selected_id = None;self._plan = None;self._receipt = None
                self.notice.set('Эта цель недоступна: ' + STATUSES[item['status']] + '. ' + item.get('reason', ''))
        self._controls()

    def _start(self, operation, argument, context=None):
        self._operation, self._context = operation, context
        self._controls()
        worker = threading.Thread(target=_application_worker,
                                  args=(self.manager, operation, argument, self._replies),
                                  daemon=True, name='application-manager-' + operation)
        try:
            worker.start()
        except RuntimeError:
            self._operation = None;self._context = None
            self.notice.set('Операция не запущена. Подготовьте новый план.')
            if operation == 'request':
                self._plan = None
            self._controls();return
        self._poll_id = self.after(50, self._poll)

    def refresh(self):
        if self.busy or self._closed:
            return
        self._plan = None;self._receipt = None;self._selected_id = None
        self._items.clear();self.table.delete(*self.table.get_children());self._text('')
        self.notice.set('Запрашиваю сведения о приложениях. Удаление не запускается…')
        self._start('catalog', None)

    def prepare(self):
        if self.busy or self._closed:
            return
        item = self._selected()
        if not item or item['status'] != 'available':
            self.notice.set('Выберите доступное приложение. Заблокированные цели не запускаются.')
            return
        self._plan = None;self._receipt = None
        self.notice.set('Подготавливаю план. Штатное удаление пока не открывается…')
        self._start('prepare', item['id'], deepcopy(item))

    def request(self):
        if self.busy or self._closed or self._plan is None:
            return
        item = self._selected();captured = self._plan
        if not item or item['status'] != 'available' or item['id'] != captured['id']:
            self._plan = None;self._controls();return
        self._operation = 'authorize';self._controls()
        text = (f'Приложение: {captured["name"]}\nID: {captured["id"]}\n'
                f'План: {captured["plan_id"]}\nДействие: {ACTIONS[captured["action"]]}\n'
                f'Объект: {captured["target"]}\nДействителен до: {captured["expires_utc"]}\n\n'
                'Откроется штатное окно. Удаление не подтверждено, пока не проверен результат. '
                'Заявленный размер не означает, что столько места освободится.')
        try:
            confirmed = messagebox.askyesno('Открыть штатное удаление?', text, parent=self.winfo_toplevel())
            if self._closed:return
            if not confirmed:
                self.notice.set('Запрос отменён. Штатное удаление не запускалось.');return
            require_parent_pin(self, self.parent_guard)
            if self._closed:
                return
            current = self._selected()
            if self._plan is not captured or not current or current['id'] != captured['id'] or current['status'] != 'available':
                self._plan = None;self.notice.set('Выбор изменился. Подготовьте новый план.');return
            self._receipt = None
            self.notice.set('Передаю подтверждённый план штатному обработчику…')
            self._start('request', captured['plan_id'], deepcopy(captured))
        except PermissionError as error:
            if not self._closed:self.notice.set(str(error))
        except Exception:
            if not self._closed:self.notice.set('Подтверждение не завершено. Штатное удаление не запущено.')
        finally:
            if self._operation == 'authorize':self._operation = None
            if not self._closed:self._controls()

    def verify(self):
        if self.busy or self._closed or self._receipt is None:
            return
        receipt = deepcopy(self._receipt)
        self.notice.set('Проверяю регистрацию и остатки. Новое удаление не запускается…')
        self._start('verify', receipt['plan_id'], receipt)

    def _show_catalog(self, value):
        items, warnings = _catalog(value)
        for index, item in enumerate(items):
            iid = 'application-' + str(index);self._items[iid] = item
            self.table.insert('', 'end', iid=iid,
                              values=(item['name'], item.get('publisher', ''), _size(item.get('reported_bytes')),
                                      item.get('source', ''), STATUSES[item['status']]),
                              tags=() if item['status'] == 'available' else ('held',))
        self.notice.set(f'Получено записей: {len(items)}. Выберите доступное приложение и подготовьте план.')
        self._text('\n'.join(warnings) if warnings else 'Размеры заявлены источником; это не измерение освобождаемого места.')

    def _show_plan(self, value, item):
        current = self._selected()
        if not current or current['id'] != item['id'] or current['status'] != 'available':
            self._plan = None;self.notice.set('Выбор изменился. Подготовьте новый план.');return
        self._plan = _plan(value, item)
        self._text(f'План: {self._plan["plan_id"]}\nПриложение: {self._plan["name"]}\n'
                   f'ID: {self._plan["id"]}\nДействие: {ACTIONS[self._plan["action"]]}\n'
                   f'Объект: {self._plan["target"]}\nСоздан: {self._plan["created_utc"]}\n'
                   f'Действителен до: {self._plan["expires_utc"]}\n'
                   f'Заявлено: {_size(self._plan.get("reported_bytes"))}; освобождение не обещается.\n\n'
                   'Проверьте план. Следующая кнопка открывает штатное окно после PIN, а не подтверждает удаление.')
        self.notice.set('План готов. Проверьте приложение, объект и действие.')

    def _show_receipt(self, value, context):
        self._plan = None
        if (not isinstance(value, dict) or value.get('plan_id') != context['plan_id']
                or value.get('id') != context['id'] or not isinstance(value.get('state'), str)
                or value['state'] not in RECEIPT_TEXT):
            raise ValueError('Receipt identity mismatch')
        self._receipt = deepcopy(value)
        state = value['state']
        text = RECEIPT_TEXT[state]
        self.notice.set(text)
        lines = [text, 'План: ' + value['plan_id'], 'ID: ' + value['id']]
        if isinstance(value.get('detail'), str):lines.append(value['detail'])
        for key, label in (('before_free', 'Свободно до (наблюдение)'), ('after_free', 'Свободно после (наблюдение)')):
            if key in value:lines.append(label + ': ' + str(value[key]))
        self._text('\n\n'.join(lines))

    def _poll(self):
        self._poll_id = None
        if self._closed:return
        try:reply = self._replies.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(50, self._poll);return
        operation, context = self._operation, self._context
        self._operation = None;self._context = None
        try:
            if reply.get('operation') != operation:raise ValueError('Operation mismatch')
            if reply.get('failed'):
                self._plan = None
                if operation == 'request':
                    self._receipt = None
                    self.notice.set('Запрос завершился неоднозначно; окно могло открыться. Автоматического повтора нет. Проверьте журнал.')
                else:self.notice.set('Не удалось получить сведения. Новое удаление не запускалось.')
            elif operation == 'catalog':self._show_catalog(reply.get('result'))
            elif operation == 'prepare':self._show_plan(reply.get('result'), context)
            elif operation in ('request', 'verify'):self._show_receipt(reply.get('result'), context)
        except (TypeError, ValueError, KeyError):
            self._plan = None;self._receipt = None
            self.notice.set('Результат не прошёл проверку. Штатное удаление не подтверждено.')
        self._controls()

    def _destroyed(self, event):
        if event.widget is self:self.close()

    def close(self):
        """Stop UI polling only; never launch, cancel or repeat a backend action."""
        if self._closed:return
        self._closed = True
        if self._poll_id is not None:
            try:self.after_cancel(self._poll_id)
            except tk.TclError:pass
            self._poll_id = None
