"""Tk panel for deterministic local rules; file work stays off the Tk thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from decimal import Decimal, InvalidOperation
from tkinter import filedialog, ttk

from local_rules import RuleError, RuleStore

ACTION_LABELS = {"Копирование": "copy", "Обзор файлов": "scan"}


def _rule_work(inbox, kind, operation, arguments):
    """A worker owns only models/plain values, never a Tk widget or callback."""
    try:
        result = operation(*arguments)
    except Exception as error:
        inbox.put((kind, None, str(error)))
    else:
        inbox.put((kind, result, None))


def _save_and_list(store, value):
    saved = store.save_rule(value)
    return saved, store.list_rules()


class RulePanel(ttk.Frame):
    def __init__(self, parent, engine, store=None):
        super().__init__(parent, padding=10)
        self.engine = engine
        self.store = store if store is not None else RuleStore(engine)
        self._messages = queue.Queue()
        self._busy = False
        self._closed = False
        self._after_id = None
        self._rules = {}
        self._current = None
        self._preview = None
        self.name = tk.StringVar()
        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.action = tk.StringVar(value="Копирование")
        self.pattern = tk.StringVar(value="*")
        self.contains = tk.StringVar()
        self.extensions = tk.StringVar()
        self.max_files = tk.StringVar(value="100")
        self.max_mib = tk.StringVar(value="2048")
        self.enabled = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Правила запускаются только по вашему запросу.")
        self._editors = []

        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(10, weight=2)
        ttk.Label(self, text="Научи один раз", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 5))
        saved = ttk.Frame(self)
        saved.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(0, 8))
        saved.columnconfigure(0, weight=1)
        saved.rowconfigure(0, weight=1)
        self.rule_list = ttk.Treeview(saved, columns=("name", "action", "version"),
                                     show="headings", height=4, selectmode="browse")
        for key, label, width in (("name", "Правило", 350), ("action", "Действие", 130),
                                   ("version", "Версия", 65)):
            self.rule_list.heading(key, text=label)
            self.rule_list.column(key, width=width, stretch=key == "name")
        self.rule_list.grid(row=0, column=0, sticky="nsew")
        self.rule_list.bind("<<TreeviewSelect>>", self._select_rule)
        scroll = ttk.Scrollbar(saved, orient="vertical", command=self.rule_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.rule_list.configure(yscrollcommand=scroll.set)

        self._entry(2, "Имя правила", self.name)
        self._entry(3, "Исходная папка", self.source, choose=True)
        self._entry(4, "Куда копировать", self.destination, choose=True)
        ttk.Label(self, text="Действие").grid(row=5, column=0, sticky="w")
        self.action_box = ttk.Combobox(self, textvariable=self.action, values=list(ACTION_LABELS),
                                       state="readonly", width=20)
        self.action_box.grid(row=5, column=1, sticky="w", pady=2)
        self._editors.append((self.action_box, "readonly"))
        enabled = ttk.Checkbutton(self, text="Правило включено", variable=self.enabled)
        enabled.grid(row=5, column=2, columnspan=2, sticky="w")
        self._editors.append((enabled, "normal"))

        conditions = ttk.Frame(self)
        conditions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(5, 2))
        conditions.columnconfigure(1, weight=1)
        for row, label, var in ((0, "Маска имени (например *.pdf)", self.pattern),
                                (1, "Имя содержит", self.contains),
                                (2, "Расширения через запятую (.pdf, .txt)", self.extensions)):
            ttk.Label(conditions, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            entry = ttk.Entry(conditions, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            self._editors.append((entry, "normal"))
        limits = ttk.Frame(self)
        limits.grid(row=7, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Label(limits, text="Не более файлов").pack(side="left")
        for var, maximum in ((self.max_files, 100), (self.max_mib, 10240)):
            widget = ttk.Spinbox(limits, from_=1, to=maximum, textvariable=var, width=8)
            widget.pack(side="left", padx=(7, 12))
            self._editors.append((widget, "normal"))
            if var is self.max_files:
                ttk.Label(limits, text="Объём, МиБ").pack(side="left")
        actions = ttk.Frame(self)
        actions.grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 5))
        self.new_button = ttk.Button(actions, text="Новое правило", command=self.new_rule)
        self.save_button = ttk.Button(actions, text="Сохранить правило", command=self.save_rule)
        self.preview_button = ttk.Button(actions, text="Предпросмотр", command=self.preview_rule)
        self.queue_button = ttk.Button(actions, text="Поставить в очередь", command=self.queue_rule)
        self.refresh_button = ttk.Button(actions, text="Обновить", command=self.refresh)
        for button in (self.new_button, self.save_button, self.preview_button, self.queue_button,
                       self.refresh_button):
            button.pack(side="left", padx=(0, 6))
        ttk.Label(self, textvariable=self.status, wraplength=900).grid(
            row=9, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        results = ttk.Frame(self)
        results.grid(row=10, column=0, columnspan=4, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.files = ttk.Treeview(results, columns=("path", "bytes", "reason"), show="headings", height=7)
        for key, label, width in (("path", "Файл", 470), ("bytes", "Байт", 90),
                                   ("reason", "Почему выбран", 270)):
            self.files.heading(key, text=label)
            self.files.column(key, width=width)
        self.files.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(results, orient="vertical", command=self.files.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(results, orient="horizontal", command=self.files.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.files.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        ttk.Label(self, text="Без расписания и обращений к ИИ. Копирование потребует подтверждения "
                  "конкретного плана во вкладке «Задания».", wraplength=900).grid(
            row=11, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.bind("<Destroy>", self._destroyed, add="+")
        self._after_id = self.after(75, self._poll)
        self.refresh()

    def _entry(self, row, label, variable, choose=False):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(self, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        self._editors.append((entry, "normal"))
        if choose:
            button = ttk.Button(self, text="Выбрать…", command=lambda: self._choose(variable))
            button.grid(row=row, column=3, padx=(6, 0))
            self._editors.append((button, "normal"))

    def _choose(self, variable):
        path = filedialog.askdirectory(parent=self, title="Выберите папку", mustexist=True)
        if path:
            variable.set(path)

    def _form(self):
        try:
            max_files = int(self.max_files.get())
            amount = Decimal(self.max_mib.get()) * 1024**2
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise ValueError()
            max_bytes = int(amount)
        except (ValueError, InvalidOperation, OverflowError):
            raise RuleError("Укажите целое число файлов и корректный объём в МиБ") from None
        action = ACTION_LABELS.get(self.action.get())
        extensions = [x.strip() for x in self.extensions.get().split(",") if x.strip()]
        value = dict(name=self.name.get().strip(), source=self.source.get().strip(),
                     destination=self.destination.get().strip() if action == "copy" else "",
                     action=action, name_glob=self.pattern.get().strip() or "*",
                     name_contains=self.contains.get().strip(), extensions=extensions,
                     max_files=max_files, max_total_bytes=max_bytes, enabled=self.enabled.get())
        if self._current:
            value.update(id=self._current["id"], version=self._current["version"])
        return value

    def _set_busy(self, busy):
        self._busy = busy
        self.rule_list.state(["disabled"] if busy else ["!disabled"])
        for widget, state in self._editors:
            widget.configure(state="disabled" if busy else state)
        for button in (self.new_button, self.save_button, self.preview_button, self.queue_button,
                       self.refresh_button):
            button.configure(state="disabled" if busy else "normal")

    def _run(self, kind, operation, *arguments):
        if self._closed or self._busy:
            return
        self._set_busy(True)
        self.status.set("Подготавливаю локальный план…")
        thread = threading.Thread(target=_rule_work,
                                  args=(self._messages, kind, operation, arguments),
                                  name="LocalRule-" + kind, daemon=True)
        thread.start()
        return thread

    def _poll(self):
        self._after_id = None
        if self._closed:
            return
        while True:
            try:
                kind, result, error = self._messages.get_nowait()
            except queue.Empty:
                break
            self._set_busy(False)
            if error:
                self._preview = None
                self.status.set(error)
                continue
            if kind in ("list", "save"):
                if kind == "save":
                    saved, rules = result
                    self._current = saved
                    self._preview = None
                    self.status.set("Правило сохранено, версия " + str(saved["version"]) + ". Запустите предпросмотр.")
                else:
                    rules = result
                    self.status.set("Правил: " + str(len(rules)) + ". Выберите правило или создайте новое.")
                self._rules = {r["id"]: r for r in rules}
                self.rule_list.delete(*self.rule_list.get_children())
                for rule in rules:
                    action = next(k for k, v in ACTION_LABELS.items() if v == rule["action"])
                    if not rule["enabled"]:
                        action += " (выкл.)"
                    self.rule_list.insert("", "end", iid=rule["id"], values=(rule["name"], action, rule["version"]))
                if self._current and self._current["id"] in self._rules:
                    self.rule_list.selection_set(self._current["id"])
                    self._select_rule(force=kind == "save")
            elif kind == "preview":
                self._preview = result
                self.files.delete(*self.files.get_children())
                for row in result["candidates"]:
                    self.files.insert("", "end", values=(row["path"], row["size"], row["reason"]))
                self.status.set("Версия {rule_version}: выбрано {count} файлов, {total_bytes} байт; "
                                "просмотрено {scanned_files}, исключено {skipped}. План ещё не выполняется.".format(**result))
            elif kind == "queue":
                suffix = (" Подтвердите копирование во вкладке «Задания»."
                          if self._preview and self._preview["action"] == "copy"
                          else " Обзор будет выполнен очередью; результат — во вкладке «Задания».")
                self.status.set("Создано задание " + result[:8] + "." + suffix)
                self._preview = None
        if not self._closed:
            self._after_id = self.after(75, self._poll)

    def _select_rule(self, _event=None, force=False):
        if self._busy:
            return
        selected = self.rule_list.selection()
        if not selected or selected[0] not in self._rules:
            return
        rule = self._rules[selected[0]]
        if self._current and self._current == rule and not force:
            return
        self._current = dict(rule)
        self._preview = None
        for variable, field in ((self.name, "name"), (self.source, "source"),
                                (self.destination, "destination"), (self.pattern, "name_glob"),
                                (self.contains, "name_contains"), (self.max_files, "max_files"),
                                (self.enabled, "enabled")):
            variable.set(rule[field])
        self.action.set(next(k for k, v in ACTION_LABELS.items() if v == rule["action"]))
        self.extensions.set(", ".join(rule["extensions"]))
        self.max_mib.set(str(Decimal(rule["max_total_bytes"]) / 1024**2))
        self.files.delete(*self.files.get_children())
        self.status.set("Выбрано правило «" + rule["name"] + "», версия " + str(rule["version"]))

    def new_rule(self):
        if self._busy:
            return
        self._current = self._preview = None
        self.rule_list.selection_remove(*self.rule_list.selection())
        for variable in (self.name, self.source, self.destination, self.contains, self.extensions):
            variable.set("")
        self.pattern.set("*")
        self.action.set("Копирование")
        self.max_files.set("100")
        self.max_mib.set("2048")
        self.enabled.set(True)
        self.files.delete(*self.files.get_children())
        self.status.set("Выберите папки и условия, затем сохраните правило.")

    def refresh(self):
        self._run("list", self.store.list_rules)

    def save_rule(self):
        if self._busy:
            return
        try:
            value = self._form()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self._run("save", _save_and_list, self.store, value)

    def _saved_form(self):
        if not self._current:
            raise RuleError("Сначала сохраните правило")
        if self._form() != self._current:
            raise RuleError("Сначала сохраните изменения правила")
        return self._current["id"]

    def preview_rule(self):
        if self._busy:
            return
        try:
            ident = self._saved_form()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self._preview = None
        self._run("preview", self.store.preview, ident)

    def queue_rule(self):
        if self._busy:
            return
        try:
            ident = self._saved_form()
            if not self._preview or self._preview["rule_id"] != ident:
                raise RuleError("Сначала выполните предпросмотр")
            token, version = self._preview["token"], self._preview["rule_version"]
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self._run("queue", self.store.enqueue, ident, token, version)

    def _destroyed(self, event):
        if event.widget is self:
            self.close()

    def close(self):
        self._closed = True
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
