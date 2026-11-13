"""Show existing Windows file access events for the app's permitted folders."""
from datetime import datetime
from tkinter import ttk

from family_messages_ui import AsyncFrame
from file_audit import read_file_audit


RIGHTS = {
    'read_data_or_list_directory': 'чтение или список папки',
    'write_data_or_add_file': 'запись или добавление файла',
    'append_data_or_add_subdirectory': 'добавление данных или папки',
    'execute_or_traverse': 'запуск или обход папки',
    'delete_child': 'доступ к удалению дочернего объекта',
    'delete_right_used': 'использовано право удаления',
}


class FileAuditPanel(AsyncFrame):
    def __init__(self, parent, engine):
        super().__init__(parent, padding=12)
        self.engine = engine
        ttk.Label(self, text='Аудит доступа к файлам', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        ttk.Label(self, text='События Windows Security за сутки по папкам из «Папки и доступ». '
                  'Запись показывает учётную запись, процесс и использованное право доступа.',
                  wraplength=780).pack(anchor='w', pady=(4, 9))
        self.refresh_button = ttk.Button(self, text='Проверить журнал Windows', command=self.refresh)
        self.refresh_button.pack(anchor='w')
        self.status = ttk.Label(self, text='Готово к проверке журнала Windows.', wraplength=780)
        self.status.pack(anchor='w', pady=8)
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = ('time', 'account', 'path', 'access', 'process')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', height=10)
        for column, title, width in zip(columns, ('Время', 'Учётная запись', 'Путь', 'Доступ', 'Процесс'),
                                        (125, 130, 270, 190, 120)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=60)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(frame, command=self.tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=self.tree.xview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

    def refresh(self):
        engine = self.engine
        def read():
            roots = engine.allowed_roots()
            return read_file_audit(roots) if roots else {'status': 'no_roots', 'events': []}
        if self.background(read, self.show_report, self.failed):
            self.refresh_button.configure(state='disabled')
            self.status.configure(text='Читаю события Security…')

    def failed(self, _error):
        self.refresh_button.configure(state='normal')
        self.status.configure(text='Проверка недоступна. Проверьте разрешённые папки и доступ к журналу Windows.')

    def show_report(self, report):
        self.refresh_button.configure(state='normal')
        self.tree.delete(*self.tree.get_children())
        for event in report['events']:
            stamp = datetime.fromisoformat(event['time_utc']).astimezone().strftime('%d.%m %H:%M:%S')
            account = event['account_name'] or event['subject_sid'] or 'Не указана'
            access = ', '.join(RIGHTS.get(right, right) for right in event['rights'])
            self.tree.insert('', 'end', values=(stamp, account, event['path'], access, event['process']))
        message = {
            'no_roots': 'Выберите папки во вкладке «Папки и доступ».',
            'access_denied': 'Windows отклонила чтение Security. Нужен доступ к журналу безопасности.',
            'unavailable': 'Журнал Windows Security сейчас недоступен.',
            'no_events': 'В выборке событий по выбранным папкам нет. Наличие настроенного аудита нужно проверить в Windows.',
            'partial': 'Доступна часть событий Windows. Выборка ограничена.',
            'ok': f"Событий в выборке: {len(report['events'])}.",
        }.get(report['status'], 'Не удалось проверить журнал Windows.')
        self.status.configure(text=message)
