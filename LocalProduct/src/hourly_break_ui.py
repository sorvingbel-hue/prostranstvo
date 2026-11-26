"""Compact opt-in 60/10 cycle panel; importing it never creates a Tk window.

The caller supplies the coordinator and parent PIN callback. Reads and clock
refreshes never approve a task, release Study or grant another game interval.
"""
from __future__ import annotations

from copy import deepcopy
import re
import tkinter as tk
from tkinter import simpledialog, ttk


POLL_MS = 1000
MAX_TASK_CHARS = 240
DEFAULT_TASK = ('Небольшая уборка в комнате: договорись с мамой, '
                'что сейчас действительно нужно сделать.')
IDENTIFIER = re.compile(r'[0-9a-f]{32}\Z')


class CyclePanelError(ValueError):
    pass


def validate_status(value):
    required = {'enabled', 'phase', 'task', 'remaining_break_seconds',
                'break_id', 'revision', 'can_request_done', 'can_approve'}
    if type(value) is not dict or not required <= value.keys():
        raise CyclePanelError('Состояние цикла не подтверждено.')
    if any(type(value[key]) is not bool for key in ('enabled','can_request_done','can_approve')):
        raise CyclePanelError('Состояние цикла не подтверждено.')
    if (type(value['revision']) is not int or value['revision'] < 0
            or type(value['remaining_break_seconds']) is not int
            or not 0 <= value['remaining_break_seconds'] <= 600):
        raise CyclePanelError('Срок полезного дела не подтверждён.')
    if (not isinstance(value['phase'], str) or not 1 <= len(value['phase']) <= 60
            or not value['phase'].isprintable()):
        raise CyclePanelError('Состояние цикла не подтверждено.')
    if (not isinstance(value['task'], str) or len(value['task']) > MAX_TASK_CHARS
            or (value['task'] and not value['task'].isprintable())):
        raise CyclePanelError('Нужно короткое полезное дело в одну строку.')
    if value['break_id'] is not None and (not isinstance(value['break_id'], str)
                                           or not IDENTIFIER.fullmatch(value['break_id'])):
        raise CyclePanelError('Идентификатор перерыва не подтверждён.')
    return deepcopy(value)


def stable_identity(value):
    """Clock-only countdown does not invalidate a parent's open PIN dialog."""
    return tuple(value[key] for key in ('revision','enabled','phase','break_id','task'))


class HourlyBreakPanel(ttk.LabelFrame):
    def __init__(self, parent, service, authorize_parent=None):
        super().__init__(parent, text='Час игры → 10 минут полезного дела', padding=8)
        self.service, self.authorize_parent = service, authorize_parent
        self.closed = False
        self._timer = None
        self._current = None
        self.columnconfigure(0, weight=1)
        self.status = tk.StringVar(self)
        self.task = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        ttk.Label(self,textvariable=self.status,wraplength=760,justify='left').grid(row=0,column=0,sticky='ew')
        ttk.Label(self,textvariable=self.task,wraplength=760,justify='left').grid(row=1,column=0,sticky='ew',pady=(4,6))
        actions = ttk.Frame(self)
        actions.grid(row=2,column=0,sticky='w')
        self.done_button = ttk.Button(actions,text='Сделано, покажу маме',command=self.request_done,state='disabled')
        self.done_button.pack(side='left',padx=(0,8))
        self.approve_button = ttk.Button(actions,text='Мама проверила — следующий час',command=self.approve_break,state='disabled')
        self.approve_button.pack(side='left')
        settings = ttk.Frame(self)
        settings.grid(row=3,column=0,sticky='w',pady=6)
        self.configure_button = ttk.Button(settings,text='Настроить цикл 60/10',command=self.configure_cycle)
        self.configure_button.pack(side='left',padx=(0,8))
        self.disable_button = ttk.Button(settings,text='Отключить цикл',command=self.disable_cycle,state='disabled')
        self.disable_button.pack(side='left')
        ttk.Label(self,textvariable=self.notice,wraplength=760,justify='left').grid(row=4,column=0,sticky='ew')
        ttk.Label(self,text=(
            'Десять минут сами не открывают игру: сначала полезное дело и проверка мамой. '
            'Подтверждение требует родительского PIN; общий PIN не устанавливает, кто именно из родителей его ввёл.'),
            wraplength=760,justify='left').grid(row=5,column=0,sticky='ew',pady=(5,0))
        self.bind('<Destroy>',self._on_destroy,add=True)
        self.refresh()
        self._schedule()

    def _read(self):
        return validate_status(self.service.status())

    def _same(self, original):
        current = self._read()
        if stable_identity(current) != stable_identity(original):
            raise CyclePanelError('Цикл или дело изменились во время подтверждения. Проверьте новый список.')
        return current

    def _parent(self, title):
        if self.authorize_parent is None or self.authorize_parent(title) is not True:
            self.notice.set('Родительское подтверждение не получено. Ничего не изменено.')
            return False
        return not self.closed

    @staticmethod
    def _ready(value, flag):
        expected_phase = 'waiting' if flag == 'can_request_done' else 'submitted'
        return (value['enabled'] and value['break_id'] is not None
                and value['remaining_break_seconds'] == 0 and value[flag]
                and value['phase'] == expected_phase)

    def _error(self, error):
        message = str(error) if isinstance(error, CyclePanelError) else 'Не удалось подтвердить изменение цикла. Проверьте состояние; автоматического повтора нет.'
        self.notice.set(message)

    def refresh(self):
        """Only reads; elapsed time and rendering never call an approval method."""
        if self.closed:
            return False
        try:
            value = self._read()
            self._current = value
            phase = value['phase']
            remaining = value['remaining_break_seconds']
            if not value['enabled']:
                text = 'Цикл выключен. Его включение само не выдаёт игровой час.'
            elif phase == 'waiting' and remaining:
                text = f'Полезное дело: осталось не меньше {remaining // 60:02d}:{remaining % 60:02d}. Потом нужна проверка мамы.'
            elif phase == 'waiting':
                text = 'Десять минут прошли. Закончи полезное дело и покажи результат маме.'
            elif phase == 'submitted':
                text = 'Результат ждёт проверки мамой. Игра пока не разрешена.'
            elif phase == 'approved':
                text = 'Полезное дело одобрено. Контроллер проверяет выдачу следующего часа.'
            elif phase in ('consumed','playing'):
                text = 'Следующий час выдан. После него снова будет полезное дело и проверка.'
            elif phase == 'blocked':
                text = 'Цикл требует проверки родителя. Срок или состояние не подтверждены.'
            elif phase == 'cancelled':
                text = 'Этот цикл отменён. Он сам не возобновится.'
            else:
                text = 'Цикл 60/10 включён для следующих игровых сеансов.'
            self.status.set(text)
            self.task.set('Полезное дело: '+(value['task'] or DEFAULT_TASK))
            self.done_button.configure(state='normal' if self._ready(value,'can_request_done') else 'disabled')
            self.approve_button.configure(state='normal' if self._ready(value,'can_approve') else 'disabled')
            self.disable_button.configure(state='normal' if value['enabled'] else 'disabled')
            return True
        except Exception as error:
            self._current = None
            self.status.set('Состояние цикла не подтверждено. Не считайте игру разрешённой.')
            self.done_button.configure(state='disabled')
            self.approve_button.configure(state='disabled')
            self.disable_button.configure(state='disabled')
            self._error(error)
            return False

    def request_done(self):
        if self.closed:
            return False
        try:
            value = self._read()
            if not self._ready(value,'can_request_done'):
                raise CyclePanelError('Сначала заверши десять минут полезного дела. Галочка не открывает игру.')
            self.service.request_done(break_id=value['break_id'],expected_revision=value['revision'])
            self.refresh()
            self.notice.set('Готовность передана на проверку маме. Игровой час этим действием не выдан.')
            return True
        except Exception as error:
            self._error(error)
            self.refresh()
            return False

    def approve_break(self):
        if self.closed:
            return False
        try:
            original = self._read()
            if not self._ready(original,'can_approve'):
                raise CyclePanelError('Следующий час возможен после десяти минут и проверки результата.')
            if not self._parent('Мама проверила полезное дело — разрешить следующий час'):
                return False
            value = self._same(original)
            if not self._ready(value,'can_approve'):
                raise CyclePanelError('Перерыв ещё не готов к подтверждению.')
            self.service.approve_break(break_id=value['break_id'],expected_revision=value['revision'])
            self.refresh()
            self.notice.set('Родительское подтверждение передано контроллеру. Фактическое состояние следующего часа смотрите выше.')
            return True
        except Exception as error:
            self._error(error)
            self.refresh()
            return False

    def configure_cycle(self):
        if self.closed:
            return False
        try:
            original = self._read()
            if not self._parent('Настроить цикл: 60 минут игры и 10 минут полезного дела'):
                return False
            self._same(original)
            task = simpledialog.askstring('Полезное дело для дома',
                'Какое небольшое дело действительно нужно сделать? Мама проверит результат.\n'
                'Цикл: час игры, затем не меньше десяти минут полезного дела.',
                initialvalue=original['task'] or DEFAULT_TASK,parent=self.winfo_toplevel())
            if task is None or self.closed:
                return False
            task = task.strip()
            if not task or len(task) > MAX_TASK_CHARS or not task.isprintable():
                raise CyclePanelError('Укажите полезное дело в одну строку, до 240 символов.')
            value = self._same(original)
            self.service.configure(enabled=True,task=task,expected_revision=value['revision'])
            self.refresh()
            self.notice.set('Цикл настроен. Само включение не начинает игру и не снимает ограничения.')
            return True
        except Exception as error:
            self._error(error)
            self.refresh()
            return False

    def disable_cycle(self):
        if self.closed:
            return False
        try:
            original = self._read()
            if not original['enabled']:
                self.notice.set('Цикл уже выключен.')
                return False
            if not self._parent('Отключить повторяемый цикл 60/10'):
                return False
            value = self._same(original)
            self.service.configure(enabled=False,task=value['task'] or DEFAULT_TASK,
                                   expected_revision=value['revision'])
            self.refresh()
            self.notice.set('Цикл отключён. Состояние действующих уроков проверяйте отдельно.')
            return True
        except Exception as error:
            self._error(error)
            self.refresh()
            return False

    def _schedule(self):
        if not self.closed:
            self._timer = self.after(POLL_MS,self._poll)

    def _poll(self):
        self._timer = None
        if not self.closed:
            self.refresh()
            self._schedule()

    def _on_destroy(self,event):
        if event.widget is self:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            try:self.after_cancel(self._timer)
            except tk.TclError:pass
            self._timer = None
