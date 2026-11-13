"""Reviewed archive copies, local index search and selected-file restoration."""
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from indexed_archives import ArchiveStore


def _archive_work(inbox, task_id, operation, arguments):
    """Return plain results only; UI callbacks stay with the Tk thread."""
    try:
        result = operation(*arguments)
    except Exception as error:
        inbox.put((task_id, None, str(error)))
    else:
        inbox.put((task_id, result, None))


def _size(value):
    if value >= 1024 ** 3:
        return f'{value / 1024 ** 3:.2f} ГиБ'
    if value >= 1024 ** 2:
        return f'{value / 1024 ** 2:.2f} МиБ'
    return f'{value / 1024:.1f} КиБ'


class ArchivePanel(ttk.Frame):
    def __init__(self, parent, engine, store_root=None):
        super().__init__(parent, padding=10)
        self.engine = engine
        self.store = None
        self.init_error = ''
        self.plan = None
        self.busy = False
        self.results = queue.Queue()
        self._callbacks = {}
        self._next_task = 0
        self._closed = False
        self.buttons = []
        self.timer = None
        self.bind('<Destroy>', self._destroy, add='+')
        ttk.Label(self, text='Архив с индексом', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        try:
            self.store = ArchiveStore(engine, root=store_root)
        except Exception as error:
            # An optional archive index must not prevent Study mode or other tabs
            # from opening. Preserve the database for a separate recovery plan.
            self.init_error = str(error)
            self.notice = tk.StringVar(value='Архивы сейчас недоступны: ' + self.init_error)
            ttk.Label(self, textvariable=self.notice, wraplength=850, justify='left').pack(anchor='w', pady=(12, 8))
            ttk.Label(self, text='Индекс не был удалён или заменён. Существующие архивы и исходные файлы сохранены. '
                      'Остальные разделы приложения доступны. Для повторной проверки перезапустите приложение; '
                      'восстановление повреждённой базы требует отдельного плана.',
                      wraplength=850, justify='left').pack(anchor='w')
            return
        ttk.Label(self, text='Локальные ZIP-копии с проверкой SHA-256, поиском и восстановлением. '
                  'До 100 файлов и 2 ГиБ за один архив. Исходники сохраняются: создание архива занимает дополнительное место.',
                  wraplength=850, justify='left').pack(anchor='w', pady=(5, 8))
        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True)
        proposal = ttk.Frame(tabs, padding=8)
        catalog = ttk.Frame(tabs, padding=8)
        tabs.add(proposal, text='Подготовить архив')
        tabs.add(catalog, text='Поиск и восстановление')

        controls = ttk.Frame(proposal)
        controls.pack(fill='x')
        self._button(controls, 'Проверить папку…', self.choose_folder).pack(side='left')
        self._button(controls, 'Выбрать файлы…', self.choose_files).pack(side='left', padx=6)
        self._button(controls, 'Создать архив выбранных', self.create_selected).pack(side='left')
        ttk.Label(proposal, text='Папка: текстовые документы, таблицы CSV, журналы, код и субтитры '
                  '(TXT, MD, JSON, CSV, LOG и другие текстовые форматы). Для других разрешённых типов выберите отдельные файлы. '
                  'Оценка сжатия приблизительная, по образцу до 64 КиБ каждого файла.',
                  wraplength=840, justify='left').pack(anchor='w', pady=8)
        self.candidates = self._table(proposal, ('name', 'size', 'source'),
                                      ('Файл', 'Размер', 'Исходный путь'), (220, 90, 470))
        self.summary = tk.StringVar(value='Подготовьте план и выберите строки. Автоматическая архивация выключена.')
        ttk.Label(proposal, textvariable=self.summary, wraplength=840).pack(anchor='w', pady=(8, 0))

        searchbar = ttk.Frame(catalog)
        searchbar.pack(fill='x')
        self.query = tk.StringVar()
        entry = ttk.Entry(searchbar, textvariable=self.query, width=40)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda event: self.search())
        self._button(searchbar, 'Найти', self.search).pack(side='left', padx=6)
        self._button(searchbar, 'Восстановить выбранные…', self.restore_selected).pack(side='left')
        self.catalog = self._table(catalog, ('name', 'size', 'source', 'archive'),
                                  ('Файл', 'Размер', 'Исходный путь', 'Архив'), (200, 90, 400, 150))
        footer = ttk.Frame(catalog)
        footer.pack(fill='x', pady=(8, 0))
        self._button(footer, 'Проверить ZIP и восстановить индекс', self.recover).pack(side='left')
        ttk.Label(footer, text='Поиск по имени и исходному пути. Восстановление без перезаписи файлов.',
                  wraplength=470).pack(side='left', padx=8)
        self.notice = tk.StringVar(value=f'Каталог хранится на компьютере: {self.store.root}')
        ttk.Label(self, textvariable=self.notice, wraplength=850).pack(anchor='w', pady=(8, 0))
        self.timer = self.after(100, self._pump)

    def _button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        self.buttons.append(button)
        return button

    def is_busy(self):
        return self.busy

    @staticmethod
    def _table(parent, columns, headings, widths):
        box = ttk.Frame(parent)
        box.pack(fill='both', expand=True, pady=(8, 0))
        tree = ttk.Treeview(box, columns=columns, show='headings', selectmode='extended', height=9)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=70)
        vertical = ttk.Scrollbar(box, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(box, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        return tree

    def _task(self, work, done, text, *arguments):
        if self._closed:
            return
        if self.busy:
            self.notice.set('Дождитесь завершения текущей проверки.')
            return
        self.busy = True
        self.notice.set(text)
        for button in self.buttons:
            button.state(['disabled'])

        self._next_task += 1
        task_id = self._next_task
        self._callbacks[task_id] = done
        thread = threading.Thread(target=_archive_work,
                                  args=(self.results, task_id, work, arguments),
                                  daemon=True, name='indexed-archive')
        thread.start()
        return thread

    def _pump(self):
        self.timer = None
        if self._closed:
            return
        try:
            task_id, result, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            done = self._callbacks.pop(task_id, None)
            self.busy = False
            for button in self.buttons:
                button.state(['!disabled'])
            if error:
                self.notice.set('Операция остановлена: ' + error)
                messagebox.showerror('Архив с индексом', error, parent=self.winfo_toplevel())
            elif done is not None:
                done(result)
        if not self._closed:
            self.timer = self.after(100, self._pump)

    def _destroy(self, event):
        if event.widget is self:
            self._closed = True
            self._callbacks.clear()
            if self.timer is not None:
                self.after_cancel(self.timer)
                self.timer = None

    def choose_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.winfo_toplevel(), mustexist=True,
                                         title='Папка с текстовыми файлами (до 100 файлов)')
        if folder:
            self._task(self.store.propose, self._show_plan, 'Проверяю источники и оцениваю сжатие…', folder)

    def choose_files(self):
        if self.busy:
            return
        files = filedialog.askopenfilenames(parent=self.winfo_toplevel(), title='До 100 разрешённых файлов')
        if files:
            self._task(self.store.propose, self._show_plan, 'Проверяю источники и оцениваю сжатие…', list(files))

    def _show_plan(self, plan):
        self.plan = plan
        self.candidates.delete(*self.candidates.get_children())
        for index, row in enumerate(plan['files']):
            self.candidates.insert('', 'end', iid=str(index), values=(row['name'], _size(row['size']), row['source']))
        self.candidates.selection_set(self.candidates.get_children())
        estimate = plan['estimated_archive_bytes']
        saving = plan['total_bytes'] - estimate
        self.summary.set(f"Файлов: {len(plan['files'])}. Исходники: {_size(plan['total_bytes'])}. "
                         f'ZIP примерно {_size(estimate)}; разница размеров: {_size(saving)}. '
                         f"Свободно на диске архива: {_size(plan['free_bytes'])}. Пропущено: {plan['skipped']}.")
        self.notice.set('План готов. Проверьте строки; исходники останутся на месте.')

    def create_selected(self):
        indexes = [int(value) for value in self.candidates.selection()]
        if self.plan is None or not indexes:
            self.notice.set('Сначала подготовьте план и выберите файлы.')
            return
        try:
            plan = self.store.select(self.plan, indexes)
        except ValueError as error:
            self.notice.set(str(error))
            return

        def done(result):
            self.notice.set(f"Архив создан и проверен: {result['path']} ({_size(result['bytes'])}). "
                            'Исходники сохранены; место на диске не освобождалось.')

        self._task(self.store.create, done, 'Создаю ZIP и проверяю каждый файл…', plan)

    def search(self):
        query = self.query.get()

        def done(rows):
            self.catalog.delete(*self.catalog.get_children())
            for row in rows:
                self.catalog.insert('', 'end', iid=row['id'], values=(row['name'], _size(row['bytes']),
                                                                     row['source'], row['archive_id'][:12]))
            self.notice.set(f'Найдено записей: {len(rows)} (показывается до 100).')

        self._task(self.store.search, done, 'Ищу в локальном индексе…', query)

    def restore_selected(self):
        if self.busy:
            return
        entries = list(self.catalog.selection())
        if not entries:
            self.notice.set('Выберите записи в результатах поиска.')
            return
        folder = filedialog.askdirectory(parent=self.winfo_toplevel(), mustexist=True,
                                         title='Разрешённая папка для восстановления без перезаписи')
        if not folder:
            return
        self._task(self.store.restore,
                   lambda paths: self.notice.set(f'Восстановлено и проверено файлов: {len(paths)}. Папка: {folder}'),
                   'Проверяю архивы и восстанавливаю выбранные файлы…', entries, folder)

    def recover(self):
        def done(result):
            self.notice.set(f"Проверено и проиндексировано архивов: {result['indexed_archives']}. "
                            f"Ошибок: {len(result['errors'])}.")
            if result['errors']:
                messagebox.showwarning('Проверка архивов', '\n'.join(
                    row['archive'] + ': ' + row['error'] for row in result['errors'][:8]), parent=self.winfo_toplevel())

        self._task(self.store.recover, done, 'Проверяю управляемые ZIP и восстанавливаю индекс…')
