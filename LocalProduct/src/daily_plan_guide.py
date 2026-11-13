"""In-memory, interactive walkthrough of the daily plan."""

import tkinter as tk

from daily_plan import DAILY_POINTS_CAP, REBIRTH_POINTS
from daily_plan_animation import DailyPlanAnimation
from daily_plan_guide_art import CreeperPortrait


BACKGROUND = '#e8f1e5'
PANEL = '#f6faf4'
TEXT = '#243a2f'
ACCENT = '#306346'
MUTED = '#4b6254'
BORDER = '#b8d0b9'

EXAMPLE_TASKS = (
    ('Прочитать две страницы', 10),
    ('Решить три примера', 20),
)

STEPS = (
    ('Привет, Паша!',
     'Я Крипер. Покажу, как работает «План на сегодня». '
     'Мама записывает дела, а ты сам выбираешь, с какого начать. '
     'Здесь можно нажимать кнопки и пробовать. Твой настоящий план не изменится.'),
    ('Поставь галочку',
     'Выбери любое дело ниже. Стив взмахнёт киркой и отправит алмаз в шкаф. '
     'Чем больше вес дела, тем шире его секция. '
     'Можно убрать галочку и посмотреть, что будет.'),
    ('Позови на помощь',
     'Застрял? Кнопка «Нужна помощь» отметит дело в списке. '
     'Когда все дела готовы, нажми «Готово, проверьте». '
     'Мама сможет посмотреть план и подтвердить его с родительским PIN.'),
    ('Собери экипировку',
     f'За весь список можно набрать {DAILY_POINTS_CAP} баллов в день. '
     f'Они копятся, и у Стива появляются новые комплекты и кирки. '
     f'На {REBIRTH_POINTS} баллах откроется незеритовый комплект. '
     'Тогда можно нажать «Перерождение» и начать путь заново.'),
    ('Хорошей игры!',
     'После маминой проверки в плане появится «Хорошей игры!» и кнопка Living Society. '
     'Она откроет папку сборки мода для Minecraft. '
     'А сейчас можешь попробовать сам.'),
)


class DailyPlanGuide(tk.Toplevel):
    """Show an isolated example without touching app settings or family data."""

    def __init__(self, parent, on_open_plan=None, *, open_plan_label='Открыть мой план'):
        super().__init__(parent)
        self.title('Крипер · План на сегодня')
        self.geometry('760x540')
        self.minsize(620, 515)
        self.configure(background=BACKGROUND)
        self.bind('<Escape>', lambda _event: self.destroy())
        self.bind('<Destroy>', self._on_destroy, add='+')
        self.on_open_plan = on_open_plan
        self.open_plan_label = open_plan_label
        self.step = 0
        self.example_done = [False] * len(EXAMPLE_TASKS)
        self.example_help = False
        self.example_review = 'open'
        self.example_reborn = False
        self.example_buttons = {}
        self._speech_timer = None

        outer = tk.Frame(self, bg=BACKGROUND, padx=22, pady=17)
        outer.pack(fill='both', expand=True)
        header = tk.Frame(outer, bg=BACKGROUND)
        header.pack(fill='x', pady=(0, 10))
        self._label(header, 'План на сегодня', size=16, bold=True, bg=BACKGROUND).pack(side='left')
        self.counter = self._label(header, '', size=10, fg=MUTED, bg=BACKGROUND)
        self.counter.pack(side='right')

        dialogue = tk.Frame(outer, bg=BACKGROUND)
        dialogue.pack(fill='x')
        self.portrait = CreeperPortrait(dialogue)
        self.portrait.pack(side='left', anchor='n', padx=(0, 12))
        bubble = tk.Frame(dialogue, bg=PANEL, padx=14, pady=10, highlightthickness=1,
                          highlightbackground=BORDER)
        bubble.pack(side='left', fill='both', expand=True)
        self._label(bubble, 'КРИПЕР', size=9, fg=ACCENT, bold=True).pack(anchor='w')
        self.headline = self._label(bubble, '', size=17, bold=True)
        self.headline.pack(anchor='w', pady=(2, 4))
        self.speech = self._label(bubble, '', size=11, wraplength=440)
        self.speech.pack(fill='x')
        bubble.bind('<Configure>', self._fit_speech)

        nav = tk.Frame(outer, bg=BACKGROUND)
        nav.pack(side='bottom', fill='x', pady=(10, 0))
        self.back_button = self._button(nav, 'Назад', self._back)
        self.back_button.pack(side='left')
        self.next_button = self._button(nav, 'Дальше', self._next, primary=True)
        self.next_button.pack(side='right')
        self.example = tk.Frame(outer, bg=PANEL, padx=12, pady=9, highlightthickness=1,
                                highlightbackground=BORDER)
        self.example.pack(fill='both', expand=True, pady=(12, 0))
        self._render()

    @staticmethod
    def _label(parent, text, *, size=10, fg=TEXT, bg=PANEL, bold=False, **options):
        return tk.Label(parent, text=text, bg=bg, fg=fg, anchor='w', justify='left',
                        font=('Segoe UI', size, 'bold' if bold else 'normal'), **options)

    @staticmethod
    def _button(parent, text, command, *, primary=False):
        return tk.Button(parent, text=text, command=command, padx=10, pady=4, relief='flat',
                         bd=0, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, bg=ACCENT if primary else '#e4efe1',
                         fg='white' if primary else TEXT, activebackground='#d1e8d0',
                         activeforeground=TEXT, disabledforeground='#75837a',
                         font=('Segoe UI', 10, 'bold' if primary else 'normal'))

    def _fit_speech(self, event):
        self.speech.configure(wraplength=max(220, event.width - 30))

    def _on_destroy(self, event):
        if event.widget is self and self._speech_timer is not None:
            self.after_cancel(self._speech_timer)
            self._speech_timer = None

    def _stop_speaking(self):
        self._speech_timer = None
        self.portrait.set_speaking(False)

    def _render(self):
        title, speech = STEPS[self.step]
        self.counter.configure(text=f'{self.step + 1} / {len(STEPS)}')
        self.headline.configure(text=title)
        self.speech.configure(text=speech)
        self.back_button.configure(state='normal' if self.step else 'disabled')
        is_last = self.step == len(STEPS) - 1
        finish_text = self.open_plan_label if self.on_open_plan else 'Готово'
        self.next_button.configure(text=finish_text if is_last else 'Дальше')

        for widget in self.example.winfo_children():
            widget.destroy()
        self.example_buttons.clear()
        pages = (self._welcome, self._checklist, self._review, self._equipment, self._reward)
        pages[self.step]()
        if self._speech_timer is not None:
            self.after_cancel(self._speech_timer)
        self.portrait.set_speaking(True)
        self._speech_timer = self.after(1800, self._stop_speaking)

    def _back(self):
        if self.step:
            self.step -= 1
            self._render()

    def _next(self):
        if self.step < len(STEPS) - 1:
            self.step += 1
            self._render()
            return
        on_open_plan = self.on_open_plan
        self.destroy()
        if on_open_plan is not None:
            on_open_plan()

    def _welcome(self):
        self._label(self.example, 'Твой маршрут', size=12, bold=True).pack(anchor='w', pady=(4, 8))
        for line in ('1. Выполни дела и собери алмазы.',
                     '2. Отправь список маме на проверку.',
                     '3. Улучшай экипировку Стива.'):
            self._label(self.example, line, size=11).pack(anchor='w', pady=3)

    def _sample_tasks(self):
        return [(index, weight, self.example_done[index])
                for index, (_, weight) in enumerate(EXAMPLE_TASKS)]

    def _sample_points(self):
        completed = sum(weight for _, weight, done in self._sample_tasks() if done)
        return DAILY_POINTS_CAP * completed // sum(weight for _, weight in EXAMPLE_TASKS)

    def _checklist(self):
        tasks = tk.Frame(self.example, bg=PANEL)
        tasks.pack(side='left', fill='y', padx=(0, 10))
        for index, (title, weight) in enumerate(EXAMPLE_TASKS):
            variable = tk.BooleanVar(self, value=self.example_done[index])
            button = tk.Checkbutton(
                tasks, text=f'{title}\nВес {weight}', variable=variable, bg=PANEL,
                activebackground=PANEL, fg=TEXT, justify='left', anchor='w',
                font=('Segoe UI', 10), padx=1,
                command=lambda i=index, checked=variable: self._toggle_task(i, checked.get()),
            )
            button.variable = variable
            button.pack(fill='x', pady=(0, 6))
            self.example_buttons[f'task_{index}'] = button
        self.points_label = self._label(tasks, '', size=10, fg=MUTED)
        self.points_label.pack(anchor='w', pady=(3, 0))
        self.scene = DailyPlanAnimation(self.example)
        self.scene.pack(side='left', fill='x', expand=True, anchor='n')
        self._update_checklist()

    def _update_checklist(self, completed_task=None):
        points = self._sample_points()
        self.points_label.configure(text=f'Сегодня: {points} из {DAILY_POINTS_CAP}')
        tasks = self._sample_tasks()
        if completed_task is None:
            self.scene.sync(tasks, points)
        else:
            self.scene.celebrate(tasks, points, completed_task)

    def _toggle_task(self, index, done):
        self.example_done[index] = done
        self.example_review = 'open'
        self._update_checklist(index if done else None)

    def _review(self):
        self.review_status = self._label(self.example, '', size=11, wraplength=490)
        self.review_status.pack(anchor='w', pady=(3, 9))
        buttons = tk.Frame(self.example, bg=PANEL)
        buttons.pack(fill='x')
        for name, label, action in (
            ('help', 'Нужна помощь', self._toggle_help),
            ('submit', 'Готово, проверьте', self._submit_example),
            ('approve', 'Мама подтвердила (пример)', self._approve_example),
        ):
            button = self._button(buttons, label, action)
            button.pack(side='left', padx=(0, 7))
            self.example_buttons[name] = button
        self._update_review()

    def _update_review(self):
        if self.example_review == 'approved':
            message = 'Пример: мама подтвердила план. Хорошей игры!'
        elif self.example_review == 'submitted':
            message = 'Пример: список ждёт маминой проверки.'
        elif self.example_help:
            message = 'Пример: рядом с делом появилась отметка «Нужна помощь».'
        elif not all(self.example_done):
            message = 'Для проверки отметь оба дела на прошлом шаге.'
        else:
            message = 'Все дела готовы. Попробуй отправить их на проверку.'
        self.review_status.configure(text=message)
        is_open = self.example_review == 'open'
        self.example_buttons['help'].configure(
            text='Отменить отметку' if self.example_help else 'Нужна помощь',
            state='normal' if is_open else 'disabled',
        )
        self.example_buttons['submit'].configure(state='normal' if is_open and all(self.example_done) else 'disabled')
        self.example_buttons['approve'].configure(state='normal' if self.example_review == 'submitted' else 'disabled')

    def _toggle_help(self):
        self.example_help = not self.example_help
        self._update_review()

    def _submit_example(self):
        self.example_review = 'submitted'
        self._update_review()

    def _approve_example(self):
        self.example_review = 'approved'
        self._update_review()

    def _equipment(self):
        points = 0 if self.example_reborn else REBIRTH_POINTS
        caption = ('Пример: новый путь · 0 баллов'
                   if self.example_reborn else f'Пример: {points} баллов · полный комплект')
        toolbar = tk.Frame(self.example, bg=PANEL)
        toolbar.pack(fill='x')
        self._label(toolbar, caption, size=10, bold=True).pack(side='left')
        button = self._button(toolbar, 'Перерождение (пример)', self._rebirth_example)
        button.configure(state='disabled' if self.example_reborn else 'normal')
        button.pack(side='right')
        self.example_buttons['rebirth'] = button
        scene = DailyPlanAnimation(self.example)
        scene.pack(fill='x', pady=(4, 0))
        scene.sync(self._sample_tasks(), points)

    def _rebirth_example(self):
        self.example_reborn = True
        self._render()

    def _reward(self):
        self._label(self.example, 'После подтверждения мамой', size=12, bold=True).pack(anchor='w', pady=(4, 8))
        self._label(
            self.example, 'Хорошей игры! · Открыть сборку Living Society',
            size=11, fg=ACCENT, bold=True, wraplength=490,
        ).pack(anchor='w')


def open_daily_plan_guide(parent, on_open_plan=None, *, open_plan_label='Открыть мой план'):
    """Open one modeless walkthrough per application window."""
    top = parent.winfo_toplevel()
    guide = getattr(top, '_daily_plan_guide', None)
    if guide is None or not guide.winfo_exists():
        guide = DailyPlanGuide(top, on_open_plan, open_plan_label=open_plan_label)
        top._daily_plan_guide = guide
    guide.deiconify()
    guide.lift()
    return guide
