"""Compact optional rewards panel. No DailyPlan writes, automatic reviews or game starts."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tkinter as tk
from tkinter import simpledialog, ttk

from family_rewards import RewardBook, RewardConflict, RewardError, plan_key


class RewardsPanel(ttk.LabelFrame):
    def __init__(self, parent, get_plan, book=None, authorize_parent=None, *, today=date.today):
        super().__init__(parent, text='Договорённости и большие цели', padding=8)
        if book is None:
            import paths
            book = RewardBook(Path(paths.DEPLOY_ROOT) / 'data' / 'family-rewards')
        self.book, self.get_plan = book, get_plan
        self.authorize_parent, self.today = authorize_parent, today
        self.closed = False
        self.columnconfigure(0, weight=1)
        self.status, self.notice = tk.StringVar(self), tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=950, justify='left').grid(row=0,column=0,sticky='ew')
        actions = ttk.Frame(self)
        actions.grid(row=1,column=0,sticky='ew',pady=5)
        for text, command in [('Моя большая цель', self.propose_goal),
                              ('Подтвердить цель', self.confirm_goal),
                              ('Договориться о наградах', self.configure_agreement),
                              ('Попросить пересмотр', self.request_revisit),
                              ('Отложить цель', self.retire_goal)]:
            ttk.Button(actions,text=text,command=command).pack(side='left',padx=(0,5))
        self.goals = ttk.Treeview(self, columns=('progress','state'), show='tree headings', height=2)
        self.goals.heading('#0',text='Цель ребёнка')
        self.goals.heading('progress',text='Накоплено')
        self.goals.heading('state',text='Договорённость')
        self.goals.column('#0',width=300)
        self.goals.column('progress',width=250)
        self.goals.column('state',width=200)
        self.goals.grid(row=2,column=0,sticky='ew')
        ttk.Label(self,textvariable=self.notice,wraplength=950,justify='left').grid(row=3,column=0,sticky='ew',pady=(4,0))
        ttk.Label(self,text=(
            'Выбирает ребёнок, родитель подтверждает срок и возможность награды. Не больше двух больших целей сразу. '
            'Любовь, еда, отдых и другие базовые потребности не нужно зарабатывать. Критерии согласуйте до начала дел.'),
            wraplength=950,justify='left').grid(row=4,column=0,sticky='ew',pady=(4,0))
        self.bind('<Destroy>',self._on_destroy,add=True)
        self.refresh()

    def _error(self, error):
        text = str(error) if isinstance(error, (RewardError, RewardConflict)) else 'Не удалось безопасно прочитать или сохранить договорённости.'
        self.notice.set(text)

    def _plan(self):
        value = self.get_plan()
        if value is None:
            return None
        if type(value) is not dict:
            raise RewardError('План недоступен. Обновите доску.')
        value = deepcopy(value)
        plan_key(value.get('date'), value.get('contract_revision'))
        if value['date'] != self.today().isoformat():
            raise RewardError('Здесь доступны договорённости сегодняшнего плана. Откройте текущий день.')
        return value

    @staticmethod
    def _identity(plan):
        return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def _same_plan(self, plan):
        current = self._plan()
        if current is None or self._identity(current) != self._identity(plan):
            raise RewardConflict('План изменился во время подтверждения. Обновите доску.')

    def _parent(self, title):
        if self.authorize_parent is None or not self.authorize_parent(title):
            self.notice.set('Родительское подтверждение не получено. Ничего не изменено.')
            return False
        if self.closed:
            return False
        return True

    def _same_book(self, revision):
        if self.book.snapshot()['revision'] != revision:
            raise RewardConflict('Договорённости изменились во время диалога. Обновите доску.')

    def _selected_goal(self, state):
        selected = self.goals.selection()
        ident = selected[0] if selected else None
        return next((goal for goal in state['goals'] if goal['goal_id'] == ident), None)

    def refresh(self):
        """Read only. In particular, never prepares a contract on first open."""
        if self.closed:
            return
        try:
            state = self.book.snapshot()
            selected = self.goals.selection()
            self.goals.delete(*self.goals.get_children())
            for goal in state['goals']:
                percent = min(100, goal['balance'] * 100 // goal['target'])
                phase = ('Отложена; накопленное сохранено' if goal['status'] == 'retired' else
                         'Нужен родитель' if goal['status'] == 'proposed' else
                         'Цель достигнута' if goal['balance'] >= goal['target'] else 'Подтверждена')
                self.goals.insert('', 'end', iid=goal['goal_id'], text=goal['title'],
                    values=(f'{goal["balance"]} / {goal["target"]} {goal["unit"]} · {percent}%',phase))
            if selected and self.goals.exists(selected[0]):
                self.goals.selection_set(selected[0])
            plan = self._plan()
            agreement = next((item for item in state['agreements'] if plan is not None and
                             item['key'] == plan_key(plan['date'],plan['contract_revision'])),None)
            if agreement is None:
                self.status.set('Критерии и дополнительные награды ещё не согласованы. Сначала договоритесь до начала дел.')
            else:
                task = next((task for task in agreement['tasks'] if task['task_id']=='plan'),None)
                criteria = task['criteria'] if task else 'Есть согласованные критерии отдельных дел.'
                quality = agreement['ratings'].get('plan')
                suffix = '' if quality is None else f' · Последняя оценка: {quality}%.'
                self.status.set('Договорённость: '+criteria+suffix)
        except Exception as error:
            self.status.set('Состояние договорённостей не подтверждено.')
            self._error(error)

    def propose_goal(self):
        if self.closed:
            return False
        try:
            state = self.book.snapshot()
            title = simpledialog.askstring('Моя большая цель','Что ты сам хотел бы получить или сделать?',parent=self.winfo_toplevel())
            if title is None or self.closed:
                return False
            target = simpledialog.askinteger('Копилка цели','Сколько баллов нужно накопить? Родитель подтвердит договорённость.',
                minvalue=1,maxvalue=1_000_000,initialvalue=100,parent=self.winfo_toplevel())
            if target is None or self.closed:
                return False
            self.book.propose_goal(title.strip(),target,'баллы',expected_revision=state['revision'])
            self.refresh()
            self.notice.set('Цель предложена. Родитель подтвердит её и договорится с тобой о сроке и награде.')
            return True
        except Exception as error:
            self._error(error)
            return False

    def _change_goal(self, retire):
        if self.closed:
            return False
        try:
            state = self.book.snapshot()
            goal = self._selected_goal(state)
            if goal is None:
                raise RewardError('Сначала выбери цель в списке.')
            title = ('Отложить цель: ' if retire else 'Подтвердить цель: ')+goal['title']
            if not self._parent(title):
                return False
            self._same_book(state['revision'])
            current = self._selected_goal(state)
            if current is None or current['goal_id'] != goal['goal_id']:
                raise RewardConflict('Выбранная цель изменилась. Подтвердите её отдельно.')
            handler = self.book.retire_goal if retire else self.book.confirm_goal
            handler(goal['goal_id'],expected_revision=state['revision'])
            self.refresh()
            self.notice.set('Цель отложена; накопленное сохранено в истории.' if retire else
                            'Цель подтверждена. Баллы можно согласовать в наградах до начала дел.')
            return True
        except Exception as error:
            self._error(error)
            return False

    def confirm_goal(self):
        return self._change_goal(False)

    def retire_goal(self):
        return self._change_goal(True)

    def configure_agreement(self):
        if self.closed:
            return False
        try:
            plan = self._plan()
            if (plan is None or plan.get('status')!='open' or not plan.get('tasks') or
                    any(task.get('status')!='pending' for task in plan['tasks'])):
                raise RewardError('Критерии согласуются только до начала дел: все пункты нового плана должны быть не отмечены.')
            state = self.book.snapshot()
            key = plan_key(plan['date'],plan['contract_revision'])
            if any(item['key']==key for item in state['agreements']):
                raise RewardError('Критерии уже согласованы. Задним числом они не меняются; можно попросить пересмотр оценки.')
            goal = self._selected_goal(state)
            if goal is not None and (goal['status']!='confirmed' or goal['balance']>=goal['target']):
                raise RewardError('Для копилки выберите подтверждённую незавершённую цель или снимите выделение.')
            if not self._parent('Согласовать критерии и награды до начала дел'):
                return False
            self._same_plan(plan); self._same_book(state['revision'])
            criteria = simpledialog.askstring('Критерии до начала дел',
                'По каким понятным признакам вы вместе определите, что дела сделаны хорошо?\n'
                'Введите договорённость своими словами; она будет видна ребёнку.',parent=self.winfo_toplevel())
            if criteria is None or self.closed:
                return False
            reward_items = [{'reward_id':'play','kind':'game_minutes','amount':plan['game_minutes'],'label':'Минуты игры'}]
            if goal is not None:
                amount = simpledialog.askinteger('Баллы на большую цель',
                    f'Сколько баллов за весь план на 100% направить на «{goal["title"]}»?',
                    minvalue=1,maxvalue=1_000_000,initialvalue=20,parent=self.winfo_toplevel())
                if amount is None or self.closed:
                    return False
                reward_items.append({'reward_id':'goal','kind':'goal','amount':amount,
                    'label':goal['title'],'goal_id':goal['goal_id'],'unit':goal['unit']})
            self._same_plan(plan); self._same_book(state['revision'])
            self.book.prepare_plan(plan['date'],plan['contract_revision'],
                [{'task_id':'plan','criteria':criteria.strip(),'rewards':reward_items}],
                expected_revision=state['revision'])
            self.refresh()
            self.notice.set('Критерии и награды сохранены до начала дел. Начисление произойдёт только после родительской оценки.')
            return True
        except Exception as error:
            self._error(error)
            return False

    def request_revisit(self):
        if self.closed:
            return False
        try:
            plan, state = self._plan(), self.book.snapshot()
            if plan is None:
                raise RewardError('Сначала открой сегодняшний план.')
            key = plan_key(plan['date'],plan['contract_revision'])
            agreement = next((item for item in state['agreements'] if item['key']==key),None)
            if agreement is None or 'plan' not in agreement['ratings']:
                raise RewardError('Пока нет оценки для пересмотра. Можно сначала обсудить критерии с родителем.')
            note = simpledialog.askstring('Можно пересмотреть?',
                'Расскажи, что ты исправил или почему хочешь обсудить оценку. Родитель сможет посмотреть ещё раз.',
                parent=self.winfo_toplevel())
            if note is None or self.closed:
                return False
            self._same_plan(plan); self._same_book(state['revision'])
            self.book.request_revisit(plan['date'],plan['contract_revision'],'plan',note.strip(),expected_revision=state['revision'])
            self.refresh()
            self.notice.set('Запрос сохранён на этом компьютере. Уведомление на телефон этим действием не отправлялось.')
            return True
        except Exception as error:
            self._error(error)
            return False

    def _on_destroy(self,event):
        if event.widget is self:
            self.close()

    def close(self):
        self.closed = True
