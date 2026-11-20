"""Local checklist and parent review for today's family plan."""

from functools import partial
from datetime import date
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from activity_core import no_links
from daily_plan import (
    DEFAULT_TASK_POINTS, MAX_TASK_LENGTH, MAX_TASK_MINUTES, MAX_TASKS, REBIRTH_POINTS, TASK_POINTS,
    DailyPlan, DailyPlanConflict,
)
from daily_plan_animation import DailyPlanAnimation
from daily_plan_guide import open_daily_plan_guide
from study_guard import StudyGuard


STUDY_DURATIONS = {'1 час': 60, '2 часа': 120, 'Без таймера': 0}
DATE_POLL_MS = 30_000
ARMOR_NAMES = {
    'none': 'без брони', 'leather': 'кожаная броня', 'copper': 'медная броня',
    'iron': 'железная броня', 'gold': 'золотая броня', 'diamond': 'алмазная броня',
    'netherite': 'незеритовая броня',
}
PICKAXE_NAMES = {'iron': 'железная кирка', 'diamond': 'алмазная кирка', 'netherite': 'незеритовая кирка'}


def equipment_text(tier):
    pickaxe = PICKAXE_NAMES[tier['pickaxe']]
    if tier['enchanted']:
        pickaxe = f'зачарованная {pickaxe}'
    return f"{ARMOR_NAMES[tier['armor']]}, {pickaxe}"


class TaskList(ttk.Frame):
    def __init__(self, parent, *, width=1, height=1):
        super().__init__(parent)
        background = ttk.Style(self).lookup('TFrame', 'background') or self.winfo_toplevel().cget('background')
        self.canvas = tk.Canvas(self, background=background, width=width, height=height, highlightthickness=0,
                                yscrollincrement=20, takefocus=False)
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas)
        content_window = self.canvas.create_window(0, 0, window=self.content, anchor='nw')
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(content_window, width=event.width))
        self.content.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox(content_window)))
        self.track(self.canvas)
        self.track(self.content)

    def track(self, widget):
        widget.bind('<MouseWheel>', self._scroll, add='+')
        widget.bind('<FocusIn>', self._show_focus, add='+')

    def _scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), 'units')
        return 'break'

    def _show_focus(self, event):
        if event.widget in (self.canvas, self.content):
            return
        top = event.widget.winfo_rooty() - self.canvas.winfo_rooty()
        height = event.widget.winfo_height()
        viewport = self.canvas.winfo_height()
        if top < 0:
            target = self.canvas.canvasy(top)
        elif top + height > viewport:
            target = self.canvas.canvasy(top + height - viewport + 20)
        else:
            return
        self.canvas.yview_moveto(target / self.content.winfo_height())


class PlanEditor(simpledialog.Dialog):
    def __init__(self, parent, plan, *, repeat_daily=None):
        self.plan = plan
        self._repeat_daily = bool(plan.get('repeat_daily', False)) if repeat_daily is None and plan else (
            False if repeat_daily is None else bool(repeat_daily))
        super().__init__(parent, 'Дела на сегодня')

    def body(self, parent):
        current = self.plan['tasks'] if self.plan else []
        ttk.Label(
            parent,
            text=f'Запишите до {MAX_TASKS} дел. XP за день начисляется после проверки родителем.',
        ).grid(row=0, sticky='w', pady=(0, 8))
        self.task_list = TaskList(parent, width=645, height=260)
        self.task_list.grid(row=1, sticky='ew')
        fields = self.task_list.content
        fields.columnconfigure(1, weight=1)
        minutes_label = ttk.Label(fields, text='Минуты (если известны)')
        minutes_label.grid(row=0, column=2, sticky='w', padx=(12, 0))
        points_label = ttk.Label(fields, text='Вес дела')
        points_label.grid(row=0, column=3, sticky='w', padx=(12, 0))
        self.task_list.track(minutes_label)
        self.task_list.track(points_label)
        self.entries = []
        self.estimates = []
        self.weights = []
        for index in range(MAX_TASKS):
            row = index + 1
            task_label = ttk.Label(fields, text=f'Дело {index + 1}')
            task_label.grid(row=row, column=0, sticky='w', padx=(0, 12))
            entry = ttk.Entry(fields, width=46)
            entry.grid(row=row, column=1, sticky='ew', pady=3)
            estimate = ttk.Entry(fields, width=8)
            estimate.grid(row=row, column=2, sticky='w', padx=(12, 0))
            weight = ttk.Combobox(fields, values=TASK_POINTS, width=5, state='readonly')
            weight.grid(row=row, column=3, sticky='w', padx=(12, 0))
            weight.set(str(DEFAULT_TASK_POINTS))
            for widget in (task_label, entry, estimate, weight):
                self.task_list.track(widget)
            if index < len(current):
                task = current[index]
                entry.insert(0, task['text'])
                if 'estimated_minutes' in task:
                    estimate.insert(0, str(task['estimated_minutes']))
                weight.set(str(task.get('points', DEFAULT_TASK_POINTS)))
            self.entries.append(entry)
            self.estimates.append(estimate)
            self.weights.append(weight)
        game = ttk.Frame(parent)
        game.grid(row=2, sticky='w', pady=(8, 0))
        ttk.Label(game, text='Минут на игры').pack(side='left', padx=(0, 8))
        self.minutes = ttk.Spinbox(game, from_=5, to=120, increment=5, width=6)
        self.minutes.set(str(self.plan['game_minutes'] if self.plan else 45))
        self.minutes.pack(side='left')
        self.repeat_daily_var = tk.BooleanVar(self, value=self._repeat_daily)
        self.repeat_daily_check = ttk.Checkbutton(
            parent, text='Повторять эти дела каждый день',
            variable=self.repeat_daily_var,
        )
        self.repeat_daily_check.grid(row=3, sticky='w', pady=(8, 0))
        return self.entries[0]

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(12, 5, 12, 12))
        ttk.Button(buttons, text='Сохранить план', command=self.ok).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Отмена', command=self.cancel).pack(side='left')
        buttons.pack()
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def validate(self):
        tasks = []
        task_minutes = []
        task_points = []
        for entry, estimate, weight in zip(self.entries, self.estimates, self.weights):
            task = entry.get().strip()
            time_text = estimate.get().strip()
            if not task:
                if time_text:
                    messagebox.showwarning('Дела на сегодня', 'Запишите дело, для которого указали время.', parent=self)
                    return False
                continue
            if time_text and (
                not time_text.isascii() or not time_text.isdecimal() or len(time_text) > 3
                or not 1 <= int(time_text) <= MAX_TASK_MINUTES
            ):
                messagebox.showwarning(
                    'Дела на сегодня',
                    f'Укажите от 1 до {MAX_TASK_MINUTES} минут на дело или оставьте поле пустым.',
                    parent=self,
                )
                return False
            tasks.append(task)
            task_minutes.append(int(time_text) if time_text else None)
            task_points.append(int(weight.get()))
        minutes = self.minutes.get().strip()
        if not tasks or any(len(task) > MAX_TASK_LENGTH or not task.isprintable() for task in tasks):
            messagebox.showwarning(
                'Дела на сегодня',
                f'Введите от 1 до {MAX_TASKS} дел, каждое до {MAX_TASK_LENGTH} символов.',
                parent=self,
            )
            return False
        if not minutes.isascii() or not minutes.isdecimal() or len(minutes) > 3 or not 5 <= int(minutes) <= 120:
            messagebox.showwarning('Дела на сегодня', 'Укажите от 5 до 120 минут.', parent=self)
            return False
        self.result = (tasks, int(minutes), task_minutes, task_points, bool(self.repeat_daily_var.get()))
        return True


class DailyPlanPanel(ttk.Frame):
    def __init__(self, parent, *, store=None, guard=None, study=None, living_society_target=None):
        super().__init__(parent, padding=(16, 12))
        self.store = DailyPlan() if store is None else store
        self.guard = StudyGuard() if guard is None else guard
        self.study = study
        self.living_society_target = Path(living_society_target) if living_society_target else None
        self.plan = None
        self.steve_progress = None
        self._checks = []
        self._closed = False
        self._selected_date = None
        self._history_plans = []
        self._history_choices = {}
        self._history_open = False
        self._seen_day = None
        self._date_after = None
        self.bind('<Destroy>', self._on_destroy, add='+')

        self.heading = ttk.Label(self, text='План на сегодня', font=('Segoe UI', 24, 'bold'), foreground='#193653')
        self.heading.pack(anchor='w')
        self.status = ttk.Label(self, foreground='#385a72', font=('Segoe UI', 12))
        self.status.pack(anchor='w', pady=(6, 4))
        history_row = ttk.Frame(self)
        history_row.pack(fill='x', pady=(0, 5))
        self.history_button = ttk.Button(history_row, text='Прошлые дни', command=self._open_history)
        self.history_button.pack(side='left')
        self.history_date = ttk.Combobox(history_row, width=31, state='readonly')
        self.history_date.bind('<<ComboboxSelected>>', self._select_history, add='+')
        self.today_button = ttk.Button(history_row, text='Сегодня', command=self._show_today)
        self.pending_reviews_label = ttk.Label(history_row)
        self.pending_reviews_label.pack(side='right', padx=(8, 0))
        self.progress = ttk.Progressbar(self, mode='determinate', length=200)
        self.progress.pack(anchor='w', pady=(0, 8))
        self._build_steve_scene()
        self.task_list = TaskList(self)
        self.task_list.pack(fill='both', expand=True)
        self.rows = self.task_list.content
        self.feedback = ttk.Label(self, wraplength=640, justify='left', foreground='#385a72')
        self.feedback.pack(anchor='w', pady=(4, 8))

        actions = ttk.Frame(self)
        actions.pack(fill='x', pady=(4, 6))
        self.submit_button = ttk.Button(actions, text='Готово, проверьте', command=self._submit)
        self.submit_button.pack(side='left')
        ttk.Separator(actions, orient='vertical').pack(side='left', fill='y', padx=12)
        self.edit_button = ttk.Button(actions, text='Добавить дела', command=self._edit)
        self.edit_button.pack(side='left', padx=(0, 8))
        self.approve_button = ttk.Button(actions, text='Подтвердить план', command=self._approve)
        self.approve_button.pack(side='left')
        self.guide_button = ttk.Button(actions, text='Обучалка с Крипером', command=self._open_guide)
        self.guide_button.pack(side='right')
        self.play_button = ttk.Button(
            actions, text='Открыть сборку Living Society', command=self._open_living_society,
        )
        if self.study is not None:
            self._build_study_controls()
        self.notice = ttk.Label(self, wraplength=640, justify='left')
        self.notice.pack(anchor='w', pady=(4, 0))
        self.bind('<Map>', lambda event: self.refresh() if event.widget is self else None)
        self.refresh()
        self._date_after = self.after(DATE_POLL_MS, self._poll_date)

    def _today_key(self):
        supplier = getattr(self.store, 'today', date.today)
        day = supplier() if callable(supplier) else supplier
        key = day.isoformat()
        if not isinstance(key, str) or len(key) != 10:
            raise ValueError('Не удалось определить дату доски.')
        return key

    def _scope(self):
        return {} if self._selected_date is None else {'plan_date': self._selected_date}

    def _visible_matches(self, plan, selected_date):
        if self._closed or self._selected_date != selected_date:
            return False
        if plan is None or self.plan is None:
            return plan is self.plan
        return self.plan['date'] == plan['date'] and self.plan['revision'] == plan['revision']

    def _on_destroy(self, event):
        if event.widget is not self or self._closed:
            return
        self._closed = True
        if self._date_after is not None:
            try:
                self.after_cancel(self._date_after)
            except tk.TclError:
                pass
            self._date_after = None
        self._checks.clear()

    def _poll_date(self):
        self._date_after = None
        if self._closed:
            return
        try:
            changed = self._today_key() != self._seen_day
        except (OSError, ValueError, TypeError, AttributeError):
            changed = False
        if changed:
            self.refresh()
        if not self._closed:
            self._date_after = self.after(DATE_POLL_MS, self._poll_date)

    def _open_history(self):
        if self._closed or not self._history_plans or not self._check_pin():
            return
        if self._closed:
            return
        self._history_open = True
        self.history_date.pack(side='left', padx=(8, 0))
        self.today_button.pack(side='left', padx=(8, 0))
        if self._selected_date is None:
            self._selected_date = self._history_plans[0]['date']
        self.refresh()

    def _select_history(self, _event=None):
        if self._closed:
            return
        selected = self._history_choices.get(self.history_date.get())
        if selected is not None:
            self._selected_date = selected
            self.refresh()

    def _show_today(self):
        if self._closed:
            return
        self._selected_date = None
        self._history_open = False
        self.history_date.pack_forget()
        self.today_button.pack_forget()
        self.refresh()

    def _render_history(self, unavailable):
        labels = {'open': 'в работе', 'submitted': 'ждёт проверки', 'approved': 'подтверждён'}
        self._history_choices = {
            f"{plan['date']} · {labels[plan['status']]}": plan['date']
            for plan in self._history_plans
        }
        self.history_date.configure(values=tuple(self._history_choices))
        selected = next((label for label, day in self._history_choices.items()
                         if day == self._selected_date), '')
        self.history_date.set(selected)
        self.heading.configure(text='План на сегодня' if self._selected_date is None else f'План от {self._selected_date}')
        self.history_button.configure(state='normal' if self._history_plans and not unavailable else 'disabled')
        pending = self.steve_progress.get('pending_reviews', []) if self.steve_progress else []
        self.pending_reviews_label.configure(text=f'Ждут проверки: {len(pending)}' if pending else '')

    def _open_guide(self):
        open_daily_plan_guide(self, on_open_plan=self.focus_set)

    def _open_living_society(self):
        if self._closed or self._selected_date is not None or not self.plan or self.plan['status'] != 'approved':
            return
        try:
            current = self.store.read()
            if self.study is not None and (self.study.session.active or self.study.active_study_id()):
                self.notice.configure(text='Папка игры доступна после завершения уроков.')
                return
        except (OSError, ValueError) as error:
            self.notice.configure(text=f'Не удалось проверить доступ к игре. {error}')
            return
        if current is None or current['date'] != self.plan['date'] or current['status'] != 'approved':
            self.refresh()
            return

        folder = self.living_society_target
        try:
            if folder is None or not folder.is_absolute():
                raise ValueError('Путь к локальной сборке не настроен.')
            folder = no_links(folder)
            if not folder.is_dir() or not no_links(folder / 'README.md', regular=True).is_file():
                raise FileNotFoundError('Папка сборки или инструкция не найдена.')
            if not any(no_links(jar, regular=True).is_file() for jar in folder.glob('living-society-[0-9]*.jar')):
                raise FileNotFoundError('В папке нет JAR-файла выпуска.')
            os.startfile(str(folder))
        except (OSError, ValueError) as error:
            self.notice.configure(text=f'Не удалось открыть Living Society. {error}')
        else:
            self.notice.configure(text='')

    def _build_steve_scene(self):
        scene_row = ttk.Frame(self)
        scene_row.pack(fill='x', pady=(0, 10))
        self.scene = DailyPlanAnimation(scene_row)
        details = ttk.Frame(scene_row, padding=(12, 0, 0, 0))
        details.pack(side='right', fill='both')
        self.steve_score = ttk.Label(details, font=('Segoe UI', 11, 'bold'))
        self.steve_score.pack(anchor='w', pady=(2, 4))
        self.steve_today = ttk.Label(details)
        self.steve_today.pack(anchor='w', pady=(0, 4))
        self.steve_equipment = ttk.Label(details, wraplength=260, justify='left')
        self.steve_equipment.pack(anchor='w')
        self.rebirth_button = ttk.Button(details, text='Перерождение', command=self._rebirth)
        self.scene.pack(side='left', fill='x', expand=True)

    def _scene_tasks(self):
        tasks = self.plan['tasks'] if self.plan else ()
        return [(index, task.get('points', DEFAULT_TASK_POINTS), task['status'] == 'done')
                for index, task in enumerate(tasks)]

    def _render_steve_scene(self):
        progress = self.steve_progress
        if progress is None:
            self.scene.sync((), 0)
            self.steve_score.configure(text='Прогресс Стива недоступен.')
            self.steve_today.configure(text='')
            self.steve_equipment.configure(text='')
            self.rebirth_button.pack_forget()
            return
        total = progress['total_points']
        self.scene.sync(self._scene_tasks(), total)
        cycle = progress['rebirth_count'] + 1
        self.steve_score.configure(text=f'Путь Стива {cycle} · подтверждено {total} / {REBIRTH_POINTS} XP')
        checked = progress['today_points']
        approved = progress['approved_today_points']
        day_label = self._selected_date or 'Сегодня'
        if self.plan and self.plan['status'] == 'approved':
            today_text = f'{day_label} · подтверждено за день: {approved} / {progress["daily_cap"]} XP'
        else:
            today_text = f'{day_label} · сделано {checked} / {progress["daily_cap"]}; ждёт проверки: {progress["provisional_points"]}'
        self.steve_today.configure(text=today_text)
        detail = equipment_text(progress['current_tier']).capitalize()
        tier = progress['next_tier']
        if tier:
            detail += f"\nНа {tier['threshold']} баллах — {equipment_text(tier)}."
        self.steve_equipment.configure(text=detail)
        if progress['can_rebirth'] and self._selected_date is None:
            self.rebirth_button.pack(anchor='w', pady=(5, 0))
        else:
            self.rebirth_button.pack_forget()

    def _rebirth(self):
        progress = self.steve_progress
        if self._closed or self._selected_date is not None or progress is None or not progress['can_rebirth']:
            return
        revision = self.plan['revision'] if self.plan else None
        cycle = progress['rebirth_count']
        total_points = progress['total_points']
        day = self._today_key()
        if not messagebox.askokcancel(
            'Перерождение',
            'Начать новый путь Стива? Подтверждённый XP пути и экипировка сбросятся. '
            'Сегодняшний список с отметками сохранится, счётчик перерождений увеличится. '
            'Новый XP начисляется после подтверждения ещё не проверенных планов.',
            parent=self.winfo_toplevel(),
        ):
            return
        if self._closed or self._selected_date is not None:
            return
        if self._today_key() != day:
            self.refresh()
            self.notice.configure(text='Наступил новый день. Проверьте доску ещё раз.')
            return
        self._commit(
            partial(self.store.rebirth, expected_revision=revision, expected_cycle=cycle,
                    expected_total_points=total_points),
            'Стив начал новый путь.',
        )

    def _build_study_controls(self):
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(8, 6))
        ttk.Label(controls, text='Уроки').pack(side='left', padx=(0, 8))
        self.study_duration = ttk.Combobox(
            controls, width=12, state='readonly', values=tuple(STUDY_DURATIONS),
        )
        self.study_duration.current(0)
        self.study_duration.pack(side='left', padx=(0, 8))
        self.study_start = ttk.Button(controls, text='Начать уроки', command=self._start_study)
        self.study_start.pack(side='left', padx=(0, 8))
        self.study_stop = ttk.Button(controls, text='Завершить уроки', command=self._stop_study)
        self.study_stop.pack(side='left', padx=(0, 12))
        ttk.Label(controls, textvariable=self.study.status).pack(side='left')
        self._study_trace = self.study.status.trace_add('write', self._update_study_controls)
        self.bind('<Destroy>', self._remove_study_trace, add='+')
        self._update_study_controls()

    def _update_study_controls(self, *_):
        active = self.study.session.active
        self.study_duration.configure(state='disabled' if active else 'readonly')
        self.study_start.configure(state='disabled' if active else 'normal')
        self.study_stop.configure(state='normal' if active else 'disabled')
        self._update_game_access()

    def _remove_study_trace(self, event):
        if event.widget is self:
            try:
                self.study.status.trace_remove('write', self._study_trace)
            except tk.TclError:
                pass

    def _start_study(self):
        started = self.study.start_lessons(duration_minutes=STUDY_DURATIONS[self.study_duration.get()])
        self.notice.configure(text='' if started else self.study.notice.get())

    def _stop_study(self):
        stopped = self.study.request_stop()
        self.notice.configure(text='' if stopped else self.study.notice.get())

    def refresh(self):
        if self._closed:
            return False
        try:
            day = self._today_key()
            self.store.ensure_today()
            self.plan, self.steve_progress = self.store.snapshot(**self._scope())
            self._history_plans = self.store.past_days()
            self._seen_day = day
        except (OSError, RuntimeError, ValueError) as error:
            self.plan = None
            self.steve_progress = None
            self._history_plans = []
            self._seen_day = locals().get('day', self._seen_day)
            self._render(unavailable=True)
            self.notice.configure(text=f'Не удалось прочитать план. {error}')
            return False
        self._render()
        return True

    def _render(self, *, unavailable=False):
        self._render_history(unavailable)
        for child in self.rows.winfo_children():
            child.destroy()
        self._checks.clear()
        plan = self.plan
        phase = plan['status'] if plan else ''
        tasks = plan['tasks'] if plan else []
        done = sum(task['status'] == 'done' for task in tasks)
        self.progress.configure(maximum=max(1, len(tasks)), value=done)
        self._render_steve_scene()
        if unavailable:
            summary = 'План сейчас недоступен.'
        elif not plan:
            summary = 'Здесь появятся дела, которые вы с родителями запланируете.'
        elif phase == 'submitted':
            summary = 'Список отправлен на проверку. Можно сделать перерыв.'
        elif phase == 'approved':
            summary = 'Отличная работа! Родитель подтвердил план.'
        elif done == len(tasks):
            summary = 'Все дела готовы. Молодец! Теперь можно отправить их на проверку.'
        else:
            summary = f'Готово {done} из {len(tasks)}. Выбирай, с какого дела начать.'
        self.status.configure(text=summary)
        feedback = plan['feedback'] if plan else ''
        if plan:
            minutes = f"Игровое время по договорённости — {plan['game_minutes']} мин."
            feedback = f'{feedback}\n{minutes}' if feedback else minutes
        self.feedback.configure(text=feedback)
        self.submit_button.configure(state='normal' if phase == 'open' and done == len(tasks) else 'disabled')
        self.edit_button.configure(text='Изменить дела' if plan else 'Добавить дела',
                                   state='disabled' if unavailable or self._selected_date is not None or phase in ('submitted', 'approved') else 'normal')
        self.approve_button.configure(state='normal' if phase == 'submitted' else 'disabled')
        self._update_game_access()
        for index, task in enumerate(tasks):
            self._render_task(index, task, plan['revision'], phase)

    def _update_game_access(self):
        if self._selected_date is None and self.plan and self.plan['status'] == 'approved':
            try:
                active = self.study is not None and (self.study.session.active or self.study.active_study_id())
            except (OSError, ValueError):
                self.status.configure(text='План подтверждён. Проверьте таймер «Уроки».')
            else:
                if not active:
                    self.status.configure(text='Отличная работа! Родитель подтвердил план. Хорошей игры!')
                    self.play_button.pack(side='left', padx=(8, 0))
                    return
                self.status.configure(text='План подтверждён. Режим «Уроки» ещё включён.')
        self.play_button.pack_forget()
    def _render_task(self, index, task, revision, phase):
        row = ttk.Frame(self.rows, padding=(4, 6))
        row.pack(fill='x')
        checked = tk.BooleanVar(row, value=task['status'] == 'done')
        self._checks.append(checked)
        box = ttk.Checkbutton(row, text='Готово', variable=checked,
                              command=lambda: self._task(index, 'done' if checked.get() else 'pending', revision))
        box.pack(side='left', anchor='n', padx=(0, 6))
        points = task.get('points', DEFAULT_TASK_POINTS)
        text = f"{task['text']} · вес {points}"
        if 'estimated_minutes' in task:
            text += f" · около {task['estimated_minutes']} мин."
        label = ttk.Label(row, text=text, justify='left', wraplength=450)
        label.pack(side='left', fill='x', expand=True)
        if phase != 'open':
            box.state(['disabled'])
        self.task_list.track(row)
        self.task_list.track(box)
        self.task_list.track(label)
        if phase == 'open' and task['status'] != 'done':
            asked = task['status'] == 'help'
            text = 'Помощь запрошена · отменить' if asked else 'Нужна помощь'
            action = partial(self._task, index, 'pending' if asked else 'help', revision)
            button = ttk.Button(row, text=text, command=action)
            button.pack(side='right', padx=(8, 0))
            self.task_list.track(button)
        elif phase == 'submitted':
            button = ttk.Button(row, text='Вернуть на доработку', command=partial(self._return_task, index, revision))
            button.pack(side='right', padx=(8, 0))
            self.task_list.track(button)
        separator = ttk.Separator(self.rows)
        separator.pack(fill='x')
        self.task_list.track(separator)

    def _commit(self, operation, success):
        if self._closed:
            return False
        try:
            saved = operation()
        except DailyPlanConflict as error:
            self.refresh()
            self.notice.configure(text=str(error))
        except (OSError, RuntimeError, ValueError) as error:
            self.refresh()
            self.notice.configure(text=f'Действие не выполнено. {error}')
        else:
            if self.refresh():
                self.notice.configure(text=success)
                return (self.plan['revision'] if self.plan else None) == (saved['revision'] if saved else None)
        return False

    def _task(self, index, status, revision):
        if self._closed or not self.plan:
            return
        newly_done = (status == 'done' and self.plan and self.plan['revision'] == revision
                      and self.plan['tasks'][index]['status'] != 'done')
        notice = 'Запрос о помощи виден в списке.' if status == 'help' else ''
        saved = self._commit(partial(self.store.set_task, index, status,
                                     expected_revision=revision, **self._scope()), notice)
        if saved and newly_done:
            self.scene.celebrate(self._scene_tasks(), self.steve_progress['total_points'], index)

    def _submit(self):
        if self._closed or not self.plan:
            return
        revision = self.plan['revision']
        scope = self._scope()

        def submit():
            session_id = self.study.active_study_id() if self.study is not None and not scope else None
            return self.store.submit(expected_revision=revision, study_session_id=session_id, **scope)

        self._commit(submit, 'Родитель может проверить список на этом компьютере.')

    def _check_pin(self, *, allow_setup=False):
        if self._closed:
            return False
        if self.study is not None:
            authorized = self.study.authorize_parent('Для родителей', allow_setup=allow_setup)
            if self._closed:
                return False
            self.notice.configure(text='' if authorized else self.study.notice.get())
            return authorized
        try:
            if not self.guard.configured():
                self.notice.configure(text='Сначала настройте родительский PIN на вкладке «Уроки».')
                return False
            pin = simpledialog.askstring('Для родителей', 'Введите родительский PIN.', show='•', parent=self.winfo_toplevel())
            if self._closed or pin is None:
                return False
            if self.guard.verify(pin):
                return True
            self.notice.configure(text='PIN не подошёл.')
        except (OSError, ValueError) as error:
            self.notice.configure(text=f'Не удалось проверить PIN. {error}')
        return False

    def _edit(self):
        if self._closed or self._selected_date is not None:
            return
        plan = self.plan
        day = self._today_key()
        if not self._check_pin(allow_setup=plan is None):
            return
        if not self._visible_matches(plan, None) or self._today_key() != day:
            return
        parent = self.winfo_toplevel()
        editor_kwargs = {'repeat_daily': self.steve_progress['repeat_daily']} if plan and self.steve_progress else {}
        result = PlanEditor(parent, plan, **editor_kwargs).result
        if result is None or not self._visible_matches(plan, None) or self._today_key() != day:
            return
        if plan and not messagebox.askokcancel('Изменить дела', 'Сохранить новый список? Галочки в старом списке сбросятся.', parent=parent):
            return
        if not self._visible_matches(plan, None) or self._today_key() != day:
            return
        if len(result) == 4:
            tasks, minutes, task_minutes, task_points = result
            repeat = {}
        elif len(result) == 5 and type(result[4]) is bool:
            tasks, minutes, task_minutes, task_points, repeat_daily = result
            repeat = {'repeat_daily': repeat_daily}
        else:
            self.notice.configure(text='Не удалось прочитать настройки ежедневной доски.')
            return
        revision = plan['revision'] if plan else None
        self._commit(
            partial(self.store.publish, tasks, minutes, expected_revision=revision,
                    task_minutes=task_minutes, task_points=task_points, **repeat),
            'План сохранён.',
        )

    def _approve(self):
        if self._closed or not self.plan:
            return
        plan = self.plan
        selected_date = self._selected_date
        scope = self._scope()
        if not self._check_pin():
            return
        if not self._visible_matches(plan, selected_date):
            if not self._closed:
                self.notice.configure(text='Доска изменилась. Проверьте выбранный день ещё раз.')
            return
        saved = self._commit(
            partial(self.store.review, approved=True, feedback='', expected_revision=plan['revision'], **scope),
            'План подтверждён.',
        )
        if (not saved or self.study is None or selected_date is not None or
                plan['date'] != self._today_key()):
            return
        session_id = plan.get('study_session_id')
        try:
            stopped = session_id is not None and self.study.complete_plan_study(session_id)
            active = self.study.active_study_id()
        except (OSError, RuntimeError, ValueError) as error:
            self.notice.configure(text=f'План подтверждён. Проверьте таймер и при необходимости нажмите «Завершить уроки». {error}')
            return
        if active:
            self.notice.configure(text='План подтверждён. Действующий сеанс «Уроки» продолжается.')
        elif stopped:
            self.notice.configure(text='План подтверждён. Режим «Уроки» завершён.')

    def _return_task(self, index, revision):
        if self._closed or not self.plan:
            return
        plan, selected_date, scope = self.plan, self._selected_date, self._scope()
        if plan['revision'] != revision:
            return
        if not self._check_pin():
            return
        if not self._visible_matches(plan, selected_date):
            return
        comment = simpledialog.askstring('Что поправить?', 'Коротко напишите, что осталось сделать.', parent=self.winfo_toplevel())
        if comment is None or not self._visible_matches(plan, selected_date):
            return
        self._commit(partial(self.store.review, approved=False, feedback=comment, task_index=index,
                             expected_revision=revision, **scope), 'Пункт возвращён с пояснением.')
