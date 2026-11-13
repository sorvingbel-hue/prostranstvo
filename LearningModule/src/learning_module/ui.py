"""Standalone, explicit local learning window. Importing this file is inert.

The window talks only to the injected LearningService contract. It neither
controls Windows nor contacts an AI service, microphone or browser.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
import uuid


COLORS = {
    'paper': '#f5f7f3', 'card': '#ffffff', 'ink': '#263c3a',
    'muted': '#657975', 'teal': '#176b61', 'light': '#e8f3ee',
    'border': '#d8e4dc', 'peach': '#f7dfbf', 'blue': '#a8d4e0',
}

COPY = {
    'ru': {
        'title': 'Учебная мастерская', 'subtitle': 'Дроби · понятные шаги · спокойный темп',
        'badge': 'ЛОКАЛЬНЫЙ ПРОТОТИП', 'language': 'Язык / Language',
        'scope': 'Сейчас поддерживается сложение двух правильных дробей. Произвольные школьные задания пока не проверяются.',
        'new': 'Выбрать задание', 'preset': 'Готовый пример', 'start_preset': 'Начать пример',
        'custom': 'Или составить свой', 'left': 'Первая дробь', 'right': 'Вторая дробь',
        'simplest': 'Ответ в сокращённом виде', 'start_custom': 'Начать своё задание',
        'lesson': 'Давай разберёмся', 'empty': 'Выбери пример слева. Можно сначала попробовать самому или попросить подсказку.',
        'answer': 'Твой ответ', 'answer_hint': 'Например: 3/4 или 1', 'check': 'Проверить',
        'hint': 'Подсказка', 'practice': 'Похожий пример', 'stop': 'Закончить занятие',
        'visual_empty': 'Для наглядного объяснения нажми «Подсказка».', 'visual_caption': 'Модель к подсказке',
        'history': 'Мои занятия на этом компьютере', 'state_heading': 'Состояние', 'refresh': 'Обновить', 'resume': 'Открыть выбранное',
        'export': 'Сохранить сводку занятия', 'privacy': 'Попытки и подсказки сохраняются локально. Родитель может их посмотреть. Общая учётная запись Windows не разделяет доступ людей к этим данным.',
        'status_idle': 'Готово к занятию', 'status_active': 'Занятие идёт', 'status_stopped': 'Занятие завершено',
        'attempts': 'Попыток', 'hints': 'Подсказок', 'independent': 'Верно самостоятельно', 'assisted': 'Верно после помощи',
        'yes': 'да', 'no': 'пока нет', 'history_empty': 'Занятий пока нет.', 'select_history': 'Выбери занятие в списке.',
        'saved': 'Сводка сохранена локально:', 'export_no_path': 'Запрос обработан. Путь к файлу не подтверждён.',
        'error': 'Не получилось выполнить действие. Данные не отправлялись внешнему сервису.',
        'unsupported': 'Этот тип задания пока не поддерживается. Выбери один из примеров.',
        'clarify': 'Проверь запись дроби и условие. Пока я не могу надёжно оценить ответ.',
        'conflict': 'Занятие изменилось. Нажми «Обновить», затем открой его из списка.',
        'uncertain': 'Результат предыдущего действия пока неизвестен. Повтори то же действие с теми же данными.',
        'stopped': 'Занятие завершено. Можно открыть его историю или выбрать новый пример.',
        'left_bar': 'Первая дробь', 'right_bar': 'Вторая дробь', 'sum_bar': 'Вместе',
        'module_unavailable': 'Учебный модуль сейчас недоступен.',
    },
    'en': {
        'title': 'Learning workshop', 'subtitle': 'Fractions · clear steps · your own pace',
        'badge': 'LOCAL PROTOTYPE', 'language': 'Language / Язык',
        'scope': 'This version supports adding two proper fractions. Other school assignments cannot be checked yet.',
        'new': 'Choose a task', 'preset': 'Ready-made example', 'start_preset': 'Start example',
        'custom': 'Or make your own', 'left': 'First fraction', 'right': 'Second fraction',
        'simplest': 'Answer in simplest form', 'start_custom': 'Start my task',
        'lesson': 'Let’s work it out', 'empty': 'Choose an example on the left. Try it yourself or ask for a hint.',
        'answer': 'Your answer', 'answer_hint': 'For example: 3/4 or 1', 'check': 'Check',
        'hint': 'Hint', 'practice': 'Similar example', 'stop': 'Finish session',
        'visual_empty': 'Select “Hint” for a visual explanation.', 'visual_caption': 'Model for this hint',
        'history': 'My sessions on this computer', 'state_heading': 'State', 'refresh': 'Refresh', 'resume': 'Open selected',
        'export': 'Save session summary', 'privacy': 'Attempts and hints are stored locally. A parent can review them. A shared Windows account does not separate people’s access to this data.',
        'status_idle': 'Ready to learn', 'status_active': 'Session in progress', 'status_stopped': 'Session finished',
        'attempts': 'Attempts', 'hints': 'Hints', 'independent': 'Correct independently', 'assisted': 'Correct after help',
        'yes': 'yes', 'no': 'not yet', 'history_empty': 'No sessions yet.', 'select_history': 'Choose a session from the list.',
        'saved': 'Summary saved locally:', 'export_no_path': 'Request processed. A saved file path was not confirmed.',
        'error': 'The action could not be completed. No data was sent to an external service.',
        'unsupported': 'This task type is not supported yet. Choose one of the examples.',
        'clarify': 'Check the fraction and task. I cannot reliably assess this answer yet.',
        'conflict': 'The session changed. Select “Refresh”, then open it from the list.',
        'uncertain': 'The previous action’s result is unknown. Repeat that same action with the same input.',
        'stopped': 'Session finished. Review its history or choose a new example.',
        'left_bar': 'First fraction', 'right_bar': 'Second fraction', 'sum_bar': 'Together',
        'module_unavailable': 'The learning module is unavailable right now.',
    },
}


def pick_text(value, language='ru', fallback=''):
    """Choose a bounded display string; unexpected payloads are not rendered."""
    if not isinstance(value, dict):
        return fallback
    for key in ('text_' + language, 'text_ru', 'text_en'):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text[:12000]
    return fallback


def visual_rows(visual):
    """Validate a small fraction model before handing its numbers to Canvas."""
    if not isinstance(visual, dict):
        return []
    keys = ('denominator', 'left_parts', 'right_parts', 'total_parts')
    if any(type(visual.get(key)) is not int for key in keys):
        return []
    denominator = visual['denominator']
    if not 1 <= denominator <= 24:
        return []
    parts = [visual[key] for key in keys[1:]]
    if any(part < 0 or part > 2 * denominator for part in parts):
        return []
    if parts[0] + parts[1] != parts[2]:
        return []
    return [(key, part, denominator) for key, part in zip(('left_bar', 'right_bar', 'sum_bar'), parts)]


def session_summary(session, language='ru'):
    if not isinstance(session, dict):
        return COPY[language]['status_idle']
    labels = COPY[language]
    state = labels['status_active'] if session.get('state') == 'active' else labels['status_stopped']
    attempts = session.get('attempt_count', 0)
    hints = session.get('hint_level', 0)
    if type(attempts) is not int or not 0 <= attempts <= 100000:
        attempts = '?'
    if type(hints) is not int or not 0 <= hints <= 100:
        hints = '?'
    independent = labels['yes'] if session.get('independent_correct') is True else labels['no']
    assisted = labels['yes'] if session.get('assisted_correct') is True else labels['no']
    return (f"{state} · {labels['attempts']}: {attempts} · {labels['hints']}: {hints}\n"
            f"{labels['independent']}: {independent} · {labels['assisted']}: {assisted}")


class RequestController:
    """Synchronous local contract client with explicit retry identity.

    A transport exception cannot silently create a second attempt. Until the
    same mutation is resolved, a different mutation is refused locally.
    """
    MUTATIONS = frozenset(('task.create', 'session.start', 'turn.submit', 'session.stop', 'report.export'))

    def __init__(self, service, id_factory=None):
        self.service = service
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.pending = None

    def call(self, operation, params=None, expected_revision=None):
        request = {'schema_version': '1.0', 'operation': operation, 'params': params or {}}
        if expected_revision is not None:
            request['expected_revision'] = expected_revision
        signature = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        mutation = operation in self.MUTATIONS
        if mutation and self.pending is not None:
            if self.pending[0] != signature:
                return {'status': 'unavailable', 'result': {}, 'ui_error': 'uncertain'}
            request = dict(self.pending[1])
        else:
            request['request_id'] = self.id_factory()
        if mutation:
            self.pending = (signature, request)
        try:
            response = self.service.handle(request)
        except Exception:
            return {'status': 'unavailable', 'result': {}, 'ui_error': 'uncertain' if mutation else 'error'}
        if not isinstance(response, dict) or not isinstance(response.get('status'), str) or not isinstance(response.get('result', {}), dict):
            return {'status': 'unavailable', 'result': {}, 'ui_error': 'uncertain' if mutation else 'error'}
        if mutation and response['status'] not in ('unavailable', 'failed'):
            self.pending = None
        return response


class LearningWindow(tk.Tk):
    """Independent local client. Construct explicitly, then call mainloop()."""
    def __init__(self, service, language='ru'):
        super().__init__()
        self.client = RequestController(service)
        self.language = language if language in COPY else 'ru'
        self.session = None
        self.task = None
        self.last_result = {}
        self.visual = None
        self.history_rows = []
        self._labels = []
        self._closed = False
        self._compact = None
        self._pending_start = None
        self.title(COPY[self.language]['title'])
        self.geometry('1040x820')
        self.minsize(760, 650)
        self.configure(background=COLORS['paper'])
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._configure_style()
        self._build()
        self._load_presets()
        self._translate()
        self.refresh_history()

    def _configure_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('Workshop.TFrame', background=COLORS['paper'])
        style.configure('Card.TFrame', background=COLORS['card'])
        style.configure('Workshop.TLabel', background=COLORS['paper'], foreground=COLORS['ink'], font=('Segoe UI', 11))
        style.configure('Card.TLabel', background=COLORS['card'], foreground=COLORS['ink'], font=('Segoe UI', 11))
        style.configure('Muted.TLabel', background=COLORS['card'], foreground=COLORS['muted'], font=('Segoe UI', 10))
        style.configure('Title.TLabel', background=COLORS['paper'], foreground=COLORS['ink'], font=('Segoe UI', 26, 'bold'))
        style.configure('Section.TLabel', background=COLORS['card'], foreground=COLORS['ink'], font=('Segoe UI', 15, 'bold'))
        style.configure('Workshop.TButton', font=('Segoe UI', 11), padding=(12, 9))
        style.configure('Accent.TButton', font=('Segoe UI', 11, 'bold'), padding=(12, 9), background=COLORS['teal'], foreground='white')
        style.map('Accent.TButton', background=[('active', '#11574f'), ('disabled', '#c5d5ce')])
        style.configure('Card.TCheckbutton', background=COLORS['card'], foreground=COLORS['ink'], font=('Segoe UI', 10))
        style.configure('Workshop.Treeview', rowheight=29, font=('Segoe UI', 10))

    def _label(self, parent, key, **options):
        widget = ttk.Label(parent, style=options.pop('style', 'Card.TLabel'), **options)
        self._labels.append((widget, key))
        return widget

    def _button(self, parent, key, command, primary=False):
        widget = ttk.Button(parent, command=command, style='Accent.TButton' if primary else 'Workshop.TButton')
        self._labels.append((widget, key))
        return widget

    def _build(self):
        header = ttk.Frame(self, style='Workshop.TFrame', padding=(28, 20, 28, 10))
        header.pack(fill='x')
        header.columnconfigure(0, weight=1)
        self._label(header, 'title', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        self._label(header, 'subtitle', style='Workshop.TLabel').grid(row=1, column=0, sticky='w', pady=(3, 0))
        self._label(header, 'language', style='Workshop.TLabel').grid(row=0, column=1, sticky='e', padx=10)
        self.language_var = tk.StringVar(value='Русский' if self.language == 'ru' else 'English')
        languages = ttk.Combobox(header, textvariable=self.language_var, values=('Русский', 'English'), state='readonly', width=11)
        languages.grid(row=0, column=2)
        languages.bind('<<ComboboxSelected>>', self.change_language)
        self.badge = self._label(header, 'badge', style='Workshop.TLabel')
        self.badge.grid(row=1, column=1, columnspan=2, sticky='e')

        shell = ttk.Frame(self, style='Workshop.TFrame')
        shell.pack(fill='both', expand=True)
        self.scroll = tk.Canvas(shell, background=COLORS['paper'], borderwidth=0, highlightthickness=0)
        scroll_y = ttk.Scrollbar(shell, orient='vertical', command=self.scroll.yview)
        self.scroll.configure(yscrollcommand=scroll_y.set)
        self.scroll.pack(side='left', fill='both', expand=True)
        scroll_y.pack(side='right', fill='y')
        self.page = ttk.Frame(self.scroll, style='Workshop.TFrame', padding=(28, 8, 18, 20))
        self.page_window = self.scroll.create_window((0, 0), window=self.page, anchor='nw')
        self.page.bind('<Configure>', lambda event: self.scroll.configure(scrollregion=self.scroll.bbox('all')))
        self.scroll.bind('<Configure>', lambda event: self.scroll.itemconfigure(self.page_window, width=event.width))
        self.page.columnconfigure(0, weight=0, minsize=285)
        self.page.columnconfigure(1, weight=1)
        self.bind('<MouseWheel>', self._mousewheel)

        scope = self._label(self.page, 'scope', style='Workshop.TLabel', wraplength=900)
        scope.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 14))
        self.page.bind('<Configure>', self._page_resized, add='+')
        self.scope_label = scope

        left = ttk.Frame(self.page, style='Card.TFrame', padding=18)
        self.left_card = left
        left.grid(row=1, column=0, sticky='new', padx=(0, 14))
        left.columnconfigure(0, weight=1)
        self._label(left, 'new', style='Section.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 16))
        self._label(left, 'preset').grid(row=1, column=0, sticky='w')
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(left, textvariable=self.preset_var, state='readonly', width=25)
        self.preset_box.grid(row=2, column=0, sticky='ew', pady=(6, 8))
        self.start_preset_button = self._button(left, 'start_preset', self.start_preset, primary=True)
        self.start_preset_button.grid(row=3, column=0, sticky='ew')
        ttk.Separator(left).grid(row=4, column=0, sticky='ew', pady=18)
        self._label(left, 'custom', style='Section.TLabel').grid(row=5, column=0, sticky='w', pady=(0, 12))
        self._label(left, 'left').grid(row=6, column=0, sticky='w')
        self.left_var, self.right_var = tk.StringVar(value='1/4'), tk.StringVar(value='2/4')
        self.left_entry = ttk.Entry(left, textvariable=self.left_var, width=20, font=('Segoe UI', 15))
        self.left_entry.grid(row=7, column=0, sticky='ew', pady=(5, 10))
        self._label(left, 'right').grid(row=8, column=0, sticky='w')
        self.right_entry = ttk.Entry(left, textvariable=self.right_var, width=20, font=('Segoe UI', 15))
        self.right_entry.grid(row=9, column=0, sticky='ew', pady=(5, 10))
        self.simplest_var = tk.BooleanVar(value=True)
        simplest = ttk.Checkbutton(left, variable=self.simplest_var, style='Card.TCheckbutton')
        self._labels.append((simplest, 'simplest'))
        simplest.grid(row=10, column=0, sticky='w', pady=(2, 12))
        self._button(left, 'start_custom', self.start_custom).grid(row=11, column=0, sticky='ew')

        lesson = ttk.Frame(self.page, style='Card.TFrame', padding=20)
        self.lesson_card = lesson
        lesson.grid(row=1, column=1, sticky='nsew')
        lesson.columnconfigure(0, weight=1)
        self._label(lesson, 'lesson', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        self.task_label = ttk.Label(lesson, text='', style='Card.TLabel', font=('Segoe UI', 16), wraplength=570, justify='left')
        self.task_label.grid(row=1, column=0, sticky='ew', pady=(15, 10))
        self.fraction_canvas = tk.Canvas(lesson, height=182, background=COLORS['light'], highlightthickness=0)
        self.fraction_canvas.grid(row=2, column=0, sticky='ew', pady=(0, 13))
        self.fraction_canvas.bind('<Configure>', lambda event: self._draw_visual())
        self._label(lesson, 'answer').grid(row=3, column=0, sticky='w')
        answer_row = ttk.Frame(lesson, style='Card.TFrame')
        answer_row.grid(row=4, column=0, sticky='ew', pady=(5, 4))
        answer_row.columnconfigure(0, weight=1)
        self.answer_var = tk.StringVar()
        self.answer_entry = ttk.Entry(answer_row, textvariable=self.answer_var, font=('Segoe UI', 19), width=12)
        self.answer_entry.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.answer_entry.bind('<Return>', lambda event: self.submit_answer())
        self.check_button = self._button(answer_row, 'check', self.submit_answer, primary=True)
        self.check_button.grid(row=0, column=1)
        self._label(lesson, 'answer_hint', style='Muted.TLabel').grid(row=5, column=0, sticky='w')
        buttons = ttk.Frame(lesson, style='Card.TFrame')
        buttons.grid(row=6, column=0, sticky='ew', pady=(12, 10))
        self.hint_button = self._button(buttons, 'hint', lambda: self.turn('hint'))
        self.hint_button.pack(side='left', padx=(0, 7))
        self.practice_button = self._button(buttons, 'practice', lambda: self.turn('practice'))
        self.practice_button.pack(side='left')
        self.feedback_label = ttk.Label(lesson, text='', style='Card.TLabel', wraplength=570, justify='left', font=('Segoe UI', 12))
        self.feedback_label.grid(row=7, column=0, sticky='ew', pady=(5, 15))
        self.state_label = ttk.Label(lesson, text='', style='Muted.TLabel', wraplength=570)
        self.state_label.grid(row=8, column=0, sticky='w')
        self.stop_button = self._button(lesson, 'stop', self.stop_session)
        self.stop_button.grid(row=9, column=0, sticky='w', pady=(13, 0))

        history = ttk.Frame(self.page, style='Card.TFrame', padding=18)
        self.history_card = history
        history.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(16, 0))
        history.columnconfigure(0, weight=1)
        self._label(history, 'history', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        self._button(history, 'refresh', self.refresh_history).grid(row=0, column=1, padx=(10, 0))
        self.history_tree = ttk.Treeview(history, columns=('task', 'state'), show='headings', height=4, style='Workshop.Treeview', selectmode='browse')
        self.history_tree.column('task', width=620, stretch=True)
        self.history_tree.column('state', width=180, stretch=False)
        self.history_tree.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(12, 10))
        history_scroll = ttk.Scrollbar(history, orient='vertical', command=self.history_tree.yview)
        history_scroll.grid(row=1, column=2, sticky='ns', pady=(12, 10))
        self.history_tree.configure(yscrollcommand=history_scroll.set)
        self.history_tree.bind('<Double-1>', lambda event: self.resume_selected())
        row = ttk.Frame(history, style='Card.TFrame')
        row.grid(row=2, column=0, columnspan=2, sticky='w')
        self._button(row, 'resume', self.resume_selected).pack(side='left', padx=(0, 8))
        self._button(row, 'export', self.export_session).pack(side='left')
        self.privacy_label = self._label(self.page, 'privacy', style='Workshop.TLabel', wraplength=900, justify='left')
        self.privacy_label.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(15, 0))

    def _page_resized(self, event):
        compact = event.width < 875
        if compact != self._compact:
            self._compact = compact
            if compact:
                self.page.columnconfigure(0, weight=1, minsize=0)
                self.page.columnconfigure(1, weight=0, minsize=0)
                self.left_card.grid_configure(row=1, column=0, columnspan=2, padx=0)
                self.lesson_card.grid_configure(row=2, column=0, columnspan=2, pady=(14, 0))
                self.history_card.grid_configure(row=3)
                self.privacy_label.grid_configure(row=4)
            else:
                self.page.columnconfigure(0, weight=0, minsize=285)
                self.page.columnconfigure(1, weight=1, minsize=0)
                self.left_card.grid_configure(row=1, column=0, columnspan=1, padx=(0, 14))
                self.lesson_card.grid_configure(row=1, column=1, columnspan=1, pady=0)
                self.history_card.grid_configure(row=2)
                self.privacy_label.grid_configure(row=3)
        wrap = max(300, event.width - (60 if compact else 380))
        self.task_label.configure(wraplength=wrap)
        self.feedback_label.configure(wraplength=wrap)
        self.state_label.configure(wraplength=wrap)
        self.scope_label.configure(wraplength=max(650, event.width - 30))
        self.privacy_label.configure(wraplength=max(650, event.width - 30))

    def _mousewheel(self, event):
        if getattr(event, 'widget', None) is self.history_tree:
            return
        if event.delta:
            self.scroll.yview_scroll(-int(event.delta / 120), 'units')

    def _load_presets(self):
        from .content import list_presets
        self.presets = list_presets()
        if not isinstance(self.presets, list):
            self.presets = []
        self.presets = [p for p in self.presets if isinstance(p, dict) and isinstance(p.get('id'), str)]
        self._preset_labels()

    def _preset_labels(self):
        index = self.preset_box.current()
        labels = [str(item.get('label_' + self.language, item['id']))[:150] for item in self.presets]
        self.preset_box.configure(values=labels)
        if labels:
            self.preset_box.current(index if 0 <= index < len(labels) else 0)
        self.start_preset_button.configure(state='normal' if labels else 'disabled')

    def _translate(self):
        labels = COPY[self.language]
        self.title(labels['title'])
        for widget, key in self._labels:
            widget.configure(text=labels[key])
        self.history_tree.heading('task', text=labels['lesson'])
        self.history_tree.heading('state', text=labels['state_heading'])
        self._preset_labels()
        self._render_lesson()
        self._render_history()

    def change_language(self, event=None):
        self.language = 'en' if self.language_var.get() == 'English' else 'ru'
        self._translate()

    def _show_error(self, response):
        status = response.get('status')
        key = response.get('ui_error') or {'unsupported': 'unsupported', 'needs_clarification': 'clarify', 'conflict': 'conflict', 'unavailable': 'module_unavailable'}.get(status, 'error')
        result = response.get('result', {})
        self.last_result = dict(result)
        self.last_result.setdefault('text_' + self.language, COPY[self.language].get(key, COPY[self.language]['error']))
        self.feedback_label.configure(text=pick_text(self.last_result, self.language))

    def _apply(self, response, refresh=True):
        if response.get('status') not in ('completed', 'needs_clarification', 'unsupported'):
            self._show_error(response)
            return False
        result = response.get('result', {})
        if isinstance(result.get('session'), dict):
            self.session = dict(result['session'])
        if isinstance(result.get('task'), dict):
            changed = self.task is None or self.task.get('task_id') != result['task'].get('task_id')
            self.task = dict(result['task'])
            if changed:
                self.visual = None
                self.answer_var.set('')
        if 'visual' in result:
            self.visual = result.get('visual')
        self.last_result = dict(result)
        if response['status'] != 'completed' and not pick_text(result, self.language):
            self.last_result['text_' + self.language] = COPY[self.language]['clarify' if response['status'] == 'needs_clarification' else 'unsupported']
        self._render_lesson()
        if refresh:
            self.refresh_history(show_error=False)
        return response['status'] == 'completed'

    def _render_lesson(self):
        labels = COPY[self.language]
        prompt = self.task.get('prompt_' + self.language) if isinstance(self.task, dict) else None
        self.task_label.configure(text=prompt if isinstance(prompt, str) else labels['empty'])
        self.feedback_label.configure(text=pick_text(self.last_result, self.language))
        self.state_label.configure(text=session_summary(self.session, self.language))
        active = isinstance(self.session, dict) and self.session.get('state') == 'active'
        for widget in (self.answer_entry, self.check_button, self.hint_button, self.practice_button, self.stop_button):
            widget.configure(state='normal' if active else 'disabled')
        self._draw_visual()

    def _draw_visual(self):
        if not hasattr(self, 'fraction_canvas'):
            return
        canvas = self.fraction_canvas
        canvas.delete('all')
        width = max(280, canvas.winfo_width())
        rows = visual_rows(self.visual)
        if not rows:
            canvas.create_text(width / 2, 80, text=COPY[self.language]['visual_empty'], fill=COLORS['muted'], width=max(200, width - 32), font=('Segoe UI', 11), justify='center')
            return
        canvas.create_text(10, 13, text=COPY[self.language]['visual_caption'], anchor='w', fill=COLORS['muted'], font=('Segoe UI', 9, 'bold'))
        x0, gap = 125, 10
        available = max(100, width - x0 - 15)
        max_units = max(1, max((part + denom - 1) // denom for _, part, denom in rows))
        unit_width = (available - gap * (max_units - 1)) / max_units
        for row, (key, parts, denom) in enumerate(rows):
            y0 = 34 + row * 46
            canvas.create_text(10, y0 + 12, text=COPY[self.language][key], anchor='w', fill=COLORS['ink'], font=('Segoe UI', 9))
            for unit in range(max_units):
                start = x0 + unit * (unit_width + gap)
                for cell in range(denom):
                    left = start + cell * unit_width / denom
                    right = start + (cell + 1) * unit_width / denom
                    filled = unit * denom + cell < parts
                    canvas.create_rectangle(left, y0, right, y0 + 24, fill=(COLORS['teal'] if row == 2 else COLORS['blue']) if filled else '#ffffff', outline=COLORS['border'])

    def _start(self, params):
        signature = json.dumps(params, sort_keys=True, ensure_ascii=False)
        if self._pending_start is not None:
            if self._pending_start[0] != signature:
                self._show_error({'status': 'unavailable', 'result': {}, 'ui_error': 'uncertain'})
                return
            task = self._pending_start[1]
        else:
            response = self.client.call('task.create', params)
            if response.get('status') != 'completed':
                self._show_error(response)
                return
            task = response.get('result', {}).get('task')
            if not isinstance(task, dict) or not isinstance(task.get('task_id'), str):
                self._show_error({'status': 'failed', 'result': {}})
                return
            self._pending_start = (signature, task)
        response = self.client.call('session.start', {'task_id': task['task_id'], 'learner_id': 'local-learner'})
        applied = self._apply(response)
        if self.client.pending is None:
            self._pending_start = None
        if applied:
            self.answer_entry.focus_set()

    def start_preset(self):
        index = self.preset_box.current()
        if 0 <= index < len(self.presets):
            self._start({'preset_id': self.presets[index]['id']})

    def start_custom(self):
        self._start({'left': self.left_var.get(), 'right': self.right_var.get(), 'require_simplest': bool(self.simplest_var.get())})

    def submit_answer(self):
        self.turn('answer', self.answer_var.get())

    def turn(self, action, text=None):
        if not isinstance(self.session, dict) or self.session.get('state') != 'active':
            return
        params = {'session_id': self.session['session_id'], 'action': action}
        if text is not None:
            params['text'] = text
        response = self.client.call('turn.submit', params, self.session.get('revision'))
        self._apply(response)

    def stop_session(self):
        if not isinstance(self.session, dict) or self.session.get('state') != 'active':
            return
        self._apply(self.client.call('session.stop', {'session_id': self.session['session_id']}, self.session.get('revision')))

    def refresh_history(self, show_error=True):
        response = self.client.call('history.list', {})
        if response.get('status') != 'completed':
            if show_error:
                self._show_error(response)
            return
        history = response.get('result', {}).get('history')
        if isinstance(history, list):
            self.history_rows = [row for row in history[:200] if isinstance(row, dict) and isinstance(row.get('session_id'), str)]
            self._render_history()

    def _render_history(self):
        selected = self.history_tree.selection()
        old_id = selected[0] if selected else None
        self.history_tree.delete(*self.history_tree.get_children())
        for index, row in enumerate(self.history_rows):
            label = row.get('prompt_' + self.language) or row.get('task_id') or row['session_id']
            state = COPY[self.language]['status_active' if row.get('state') == 'active' else 'status_stopped']
            self.history_tree.insert('', 'end', iid=str(index), values=(str(label)[:180], state))
        if old_id in self.history_tree.get_children():
            self.history_tree.selection_set(old_id)

    def _selected_id(self):
        selected = self.history_tree.selection()
        if not selected:
            return None
        try:
            return self.history_rows[int(selected[0])]['session_id']
        except (ValueError, IndexError, KeyError):
            return None

    def resume_selected(self):
        session_id = self._selected_id()
        if session_id is None:
            self.feedback_label.configure(text=COPY[self.language]['select_history'])
            return
        self._apply(self.client.call('session.snapshot', {'session_id': session_id}), refresh=False)

    def export_session(self):
        session_id = self._selected_id() or (self.session or {}).get('session_id')
        if not session_id:
            self.feedback_label.configure(text=COPY[self.language]['select_history'])
            return
        response = self.client.call('report.export', {'session_id': session_id})
        if response.get('status') != 'completed':
            self._show_error(response)
            return
        result = response.get('result', {})
        export = result.get('export', {})
        path = export.get('path') if isinstance(export, dict) else None
        path = path or result.get('path')
        text = COPY[self.language]['saved'] + '\n' + path[:1500] if isinstance(path, str) and path else COPY[self.language]['export_no_path']
        self.last_result = {'text_' + self.language: text}
        self.feedback_label.configure(text=text)

    def close(self):
        self._closed = True
        self.destroy()


def create_window(service, language='ru'):
    return LearningWindow(service, language=language)
