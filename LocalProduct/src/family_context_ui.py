"""Parent-editable local facts and explicit chat-history export."""
import os
import tkinter as tk
from tkinter import ttk, messagebox

from family_context import FamilyContext, ChatLog, empty_profile
from family_messages_ui import AsyncFrame
from activity_core import no_links


class FamilyProfileWindow(tk.Toplevel):
    def __init__(self, master, context=None, log=None):
        super().__init__(master)
        self.title('Семья и память Мурчика')
        self.geometry('760x730')
        self.minsize(650, 650)
        self.context = context or FamilyContext()
        self.log = log or ChatLog()
        self.body = AsyncFrame(self, padding=16)
        self.body.pack(fill='both', expand=True)
        ttk.Label(self.body, text='Семейная память', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(self.body, text='Заполняйте только то, что котику можно рассказывать ребёнку. Пустое поле означает «не знаю».\n'
                  'Эти сведения и история остаются на данном ПК; в установщик для других семей они не входят.',
                  wraplength=700, justify='left').pack(anchor='w', pady=(5, 9))
        self.fields = {}
        for person, title, items in (
            ('child', 'Ребёнок', [('name', 'Имя'), ('aliases', 'Как ещё обращаться (через запятую)'), ('age', 'Возраст'), ('grade', 'Класс'), ('school', 'Школа')]),
            ('mother', 'Мама', [('name', 'Имя'), ('age', 'Возраст'), ('occupation', 'Занятия / работа')]),
            ('father', 'Папа', [('name', 'Имя'), ('age', 'Возраст'), ('occupation', 'Занятия / работа')])):
            group = ttk.LabelFrame(self.body, text=title, padding=7)
            group.pack(fill='x', pady=(0, 7))
            group.columnconfigure(1, weight=1)
            for row, (field, label) in enumerate(items):
                ttk.Label(group, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=2)
                variable = tk.StringVar()
                ttk.Entry(group, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=2)
                self.fields[(person, field)] = variable
        self.status = tk.StringVar(value='Сведения указываются родителем; бот не угадывает неизвестное.')
        ttk.Label(self.body, textvariable=self.status, wraplength=700).pack(anchor='w', pady=(2, 6))
        controls = ttk.Frame(self.body)
        controls.pack(fill='x')
        ttk.Button(controls, text='Сохранить сведения', command=self.save).pack(side='left')
        ttk.Button(controls, text='Открыть логи', command=self.open_logs).pack(side='left', padx=7)
        ttk.Button(controls, text='Выгрузить историю JSONL', command=self.export).pack(side='left')
        ttk.Label(self.body, text='История бесед с котиком сохраняется отдельно. Автоматического анализа эмоций, '
                  'оценок личности и отправки логов разработчику нет. Общая учётная запись Windows не отделяет '
                  'доступ ребёнка от родительского доступа к её файлам.', wraplength=700, justify='left').pack(anchor='w', pady=8)
        try:
            self.fill(self.context.profile())
        except (OSError, ValueError):
            self.status.set('Семейную память прочитать не удалось. Существующие данные не изменены.')

    def fill(self, profile):
        for (person, field), variable in self.fields.items():
            value = profile[person][field]
            variable.set(', '.join(value) if isinstance(value, list) else '' if value is None else str(value))

    def form(self):
        value = empty_profile()
        for (person, field), variable in self.fields.items():
            text = variable.get().strip()
            if field == 'age':
                try:
                    item = int(text) if text else None
                except ValueError:
                    raise ValueError('Возраст указывается целым числом.') from None
            elif field == 'aliases':
                item = [x.strip() for x in text.split(',') if x.strip()]
            else:
                item = text or None
            value[person][field] = item
        return self.context.validate(value)

    def save(self):
        if self.body._busy:
            return
        try:
            value = self.form()
        except ValueError:
            self.status.set('Проверьте возраст и длину полей. Неизвестные сведения можно оставить пустыми.')
            return
        context = self.context
        def done(saved):
            self.fill(saved)
            self.status.set('Семейные сведения сохранены на этом компьютере.')
        self.body.background(lambda: context.save(value), done,
                             lambda error: self.status.set('Сохранить не удалось. Проверьте доступ к локальной папке данных.'))

    def open_logs(self):
        try:
            no_links(self.log.root)
            self.log.root.mkdir(parents=True, exist_ok=True)
            no_links(self.log.root)
            os.startfile(str(self.log.root))
        except (OSError, ValueError):
            self.status.set('Не удалось открыть папку локальной истории.')

    def export(self):
        if self.body._busy:
            return
        log = self.log
        def done(path):
            self.status.set('История выгружена: ' + str(path.name) + '. Файл находится в папке логов.')
        self.body.background(log.export_jsonl, done,
                             lambda error: self.status.set('Историю выгрузить не удалось. Исходная база не удалялась.'))
