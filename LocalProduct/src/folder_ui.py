"""Explicit local folder permissions for the file executor."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class FolderPanel(ttk.Frame):
    def __init__(self, parent, engine):
        super().__init__(parent, padding=12)
        self.engine = engine
        ttk.Label(self, text='Разрешённые папки', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(self, text='Выберите отдельные папки на C: или D: для инвентаризации, копирования и архивов.\n'
                  'Каждый перенос сначала создаёт план. Системные файлы, облака, сохранения и данные приложения защищены.',
                  wraplength=820, justify='left').pack(anchor='w', pady=(6, 12))
        box = ttk.Frame(self)
        box.pack(fill='both', expand=True)
        self.folders = tk.Listbox(box, selectmode='browse', exportselection=False, height=12)
        scrollbar = ttk.Scrollbar(box, orient='vertical', command=self.folders.yview)
        self.folders.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.folders.pack(side='left', fill='both', expand=True)
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(10, 4))
        ttk.Button(controls, text='Добавить папку…', command=self.add).pack(side='left')
        ttk.Button(controls, text='Убрать разрешение', command=self.remove).pack(side='left', padx=8)
        ttk.Button(controls, text='Обновить', command=self.refresh).pack(side='left')
        self.notice = tk.StringVar()
        ttk.Label(self, textvariable=self.notice, wraplength=820).pack(anchor='w', pady=(6, 0))
        self.refresh()

    def refresh(self):
        try:
            roots = self.engine.allowed_roots()
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        self.folders.delete(0, 'end')
        for root in roots:
            self.folders.insert('end', root)
        self.notice.set('Изменения действуют для новых проверок и ожидающих заданий. Удаление разрешения не удаляет файлы.')

    def _save(self, roots):
        try:
            self.engine.set_allowed_roots(roots)
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror('Разрешённые папки', str(error), parent=self.winfo_toplevel())
            return
        self.refresh()

    def add(self):
        folder = filedialog.askdirectory(parent=self.winfo_toplevel(), title='Разрешить отдельную папку на C: или D:', mustexist=True)
        if not folder:
            return
        try:
            roots = self.engine.allowed_roots()
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        self._save([*roots, folder])

    def remove(self):
        selected = self.folders.curselection()
        if not selected:
            self.notice.set('Выберите папку в списке, чтобы убрать её разрешение.')
            return
        # Re-read before saving so another process's additions are preserved.
        selected_path = self.folders.get(selected[0])
        try:
            roots = self.engine.allowed_roots()
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        self._save([root for root in roots if root != selected_path])
