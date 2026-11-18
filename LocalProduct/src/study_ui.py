"""Visible, manually enabled controls for the local study session."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk


UNTIMED_LABEL = 'Без таймера'


def duration_text(seconds):
    if seconds == math.inf:
        return UNTIMED_LABEL
    seconds = max(0, math.ceil(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02}:{seconds:02}"


class StudyPanel(ttk.Frame):
    """Keep session lifetime, window observation and Tk callbacks together."""

    def __init__(self, parent, settings=None, session=None, desktop=None, guard=None):
        super().__init__(parent, padding=12)
        if settings is None:
            from study_core import StudySettings
            settings = StudySettings()
        if session is None:
            from study_core import StudySession
            if desktop is None:
                from study_windows import WindowsDesktop
                desktop = WindowsDesktop()
            session = StudySession(desktop)
        if guard is None:
            from study_guard import StudyGuard
            guard = StudyGuard()
        self.settings = settings
        self.session = session
        self.guard = guard
        self.closed = False
        self._timer = None
        self._indicator = None
        self._indicator_text = None
        self._allowed_paths = []
        self._displayed_rows = None
        self._event_lines = []
        self._codex_pause_policy = None
        self.codex_pause_enabled = tk.BooleanVar(value=False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(6, weight=1)
        self.bind("<Destroy>", self._on_destroy, add=True)

        self.heading = tk.StringVar(value="Родительский контроль · Уроки")
        ttk.Label(self, textvariable=self.heading, font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            self,
            text=("Включайте на время уроков. Открытые и новые окна получают команду "
                  "закрытия после предупреждения. Пробный режим показывает результат "
                  "проверки в таблице."),
            wraplength=1040,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(8, 12))

        options = ttk.Frame(self)
        options.grid(row=2, column=0, sticky="ew")
        ttk.Label(options, text="Продолжительность, минут").pack(side="left")
        self.duration = tk.StringVar(value="30")
        self.duration_box = ttk.Combobox(
            options, textvariable=self.duration, values=(15, 30, 45, 60, 90, 120, 240, UNTIMED_LABEL),
            state="readonly", width=12,
        )
        self.duration_box.pack(side="left", padx=(7, 20))
        ttk.Label(options, text="Задержка закрытия, секунд").pack(side="left")
        self.delay = tk.StringVar(value="10")
        self.delay_box = ttk.Combobox(
            options, textvariable=self.delay, values=(5, 10), state="readonly", width=4,
        )
        self.delay_box.pack(side="left", padx=7)

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", pady=12)
        self.preview_button = ttk.Button(actions, text="Пробный режим", command=self.start_preview)
        self.preview_button.pack(side="left")
        self.start_button = ttk.Button(actions, text="Начать уроки", command=self.start_lessons)
        self.start_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="Завершить режим", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.status = tk.StringVar(value="Выключено")
        ttk.Label(actions, textvariable=self.status, font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=16
        )
        ttk.Button(actions, text="Защищённая пауза ПК", command=self.open_parental_control).pack(side="right")

        allowed = ttk.LabelFrame(self, text="Разрешённые программы для уроков", padding=8)
        allowed.grid(row=4, column=0, sticky="ew")
        allowed.columnconfigure(0, weight=1)
        self.allowed_list = tk.Listbox(allowed, height=3, exportselection=False, font=("Segoe UI", 10))
        self.allowed_list.grid(row=0, column=0, rowspan=2, sticky="ew", padx=(0, 10))
        self.add_button = ttk.Button(allowed, text="Добавить программу…", command=self.add_allowed)
        self.add_button.grid(row=0, column=1, sticky="ew")
        self.remove_button = ttk.Button(allowed, text="Убрать из списка", command=self.remove_allowed)
        self.remove_button.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        ttk.Label(
            self,
            text=("Общая учётная запись позволяет пользователю с правами администратора "
                  "обойти ограничение. Диалог сохранения документа может оставаться открытым. "
                  "Досрочное завершение требует PIN родителя."),
            wraplength=1040,
            justify="left",
        ).grid(row=5, column=0, sticky="ew", pady=10)

        table = ttk.Frame(self)
        table.grid(row=6, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.windows = ttk.Treeview(
            table, columns=("application", "title", "deadline"), show="headings", height=8,
        )
        for key, title, width in (
            ("application", "Программа", 220),
            ("title", "Окно", 500),
            ("deadline", "Действие", 220),
        ):
            self.windows.heading(key, text=title)
            self.windows.column(key, width=width, stretch=key != "deadline")
        self.windows.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.windows.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.windows.configure(yscrollcommand=scrollbar.set)
        self.notice = tk.StringVar(value="Выберите пробный режим, чтобы проверить список окон.")
        self.guard_status = tk.StringVar(value="PIN родителя потребуется при настройке плана и управлении уроками.")
        ttk.Label(self, textvariable=self.guard_status, wraplength=1040).grid(
            row=7, column=0, sticky="ew", pady=(8, 0)
        )
        ttk.Label(self, textvariable=self.notice, wraplength=1040, justify="left").grid(
            row=8, column=0, sticky="ew", pady=(4, 0)
        )
        try:
            self._apply_settings(self.settings.load())
        except Exception as error:
            self.notice.set("Не удалось прочитать настройки. " + str(error)[:180])
        try:
            if self.guard.configured():
                self.guard_status.set("PIN родителя установлен. Он нужен для настройки плана и управления уроками.")
        except Exception as error:
            self.notice.set("Не удалось проверить PIN родителя. " + str(error)[:180])
        self.codex_pause_button = ttk.Checkbutton(
            self, text="Сворачивать Codex во время уроков", variable=self.codex_pause_enabled,
            command=self._toggle_codex_pause, state="disabled",
        )
        self.codex_pause_button.grid(row=9, column=0, sticky="w", pady=(6, 0))
        self._schedule()

    def bind_codex_pause(self, policy):
        self._codex_pause_policy = policy
        self.codex_pause_button.configure(state="normal")
        try:
            self.codex_pause_enabled.set(policy.enabled())
        except (OSError, ValueError) as error:
            self.notice.set("Не удалось прочитать настройку Codex. " + str(error)[:180])

    def _toggle_codex_pause(self):
        policy = self._codex_pause_policy
        if policy is None:
            return
        try:
            previous = policy.enabled()
            requested = self.codex_pause_enabled.get()
            if not self.authorize_parent("Доступ к Codex"):
                self.codex_pause_enabled.set(previous)
                return
            policy.set_enabled(requested)
            self.notice.set("Во время уроков окна Codex будут сворачиваться." if requested
                            else "Автоматическое сворачивание Codex выключено.")
        except (OSError, RuntimeError, ValueError) as error:
            self.notice.set("Не удалось изменить доступ к Codex. " + str(error)[:180])

    def _apply_settings(self, value):
        self.duration.set(str(value["duration_minutes"]) if value["duration_minutes"] else UNTIMED_LABEL)
        self.delay.set(str(value["delay_seconds"]))
        self._allowed_paths = list(value["allowed_paths"])
        self.allowed_list.delete(0, "end")
        for path in self._allowed_paths:
            self.allowed_list.insert("end", path)

    def _save_settings(self, *, duration_minutes=None):
        if duration_minutes is None:
            selected = self.duration.get()
            duration_minutes = 0 if selected == UNTIMED_LABEL else int(selected)
        value = self.settings.save(
            duration_minutes=duration_minutes,
            delay_seconds=int(self.delay.get()),
            allowed_paths=list(self._allowed_paths),
        )
        self._apply_settings(value)
        return value

    def add_allowed(self):
        if self.session.active:
            return
        path = filedialog.askopenfilename(
            parent=self, title="Выберите программу для уроков", filetypes=(("Программы", "*.exe"),),
        )
        if not path:
            return
        previous = list(self._allowed_paths)
        path = str(Path(path).resolve())
        if os.path.normcase(path) not in {os.path.normcase(item) for item in self._allowed_paths}:
            self._allowed_paths.append(path)
        try:
            self._save_settings()
            self.notice.set("Разрешённая программа добавлена. Настройки сохранены на ПК.")
        except Exception as error:
            self._allowed_paths = previous
            self.notice.set("Не удалось сохранить настройку. " + str(error)[:180])

    def remove_allowed(self):
        if self.session.active:
            return
        selected = self.allowed_list.curselection()
        if not selected:
            return
        previous = list(self._allowed_paths)
        del self._allowed_paths[selected[0]]
        try:
            self._save_settings()
            self.notice.set("Программа убрана из разрешённых.")
        except Exception as error:
            self._allowed_paths = previous
            self.notice.set("Не удалось сохранить настройку. " + str(error)[:180])

    def start_preview(self):
        self._start(preview=True)

    def open_parental_control(self):
        try:
            import ctypes
            from activity_core import no_links
            folder = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 38, None, 0, folder) != 0:
                raise OSError('Не удалось определить папку установленных программ')
            target = Path(folder.value) / 'ProstranstvoParental' / 'ParentalPanel.exe'
            if target.is_file():
                no_links(target, True)
                os.startfile(str(target))
            else:
                app = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
                installer = app / 'ParentalControl' / 'ParentalSetup.exe'
                no_links(installer, True)
                if not installer.is_file():
                    raise OSError('Установщик системной защиты пока не найден')
                os.startfile(str(installer))
        except Exception as error:
            self.notice.set('Не удалось открыть защищённый режим. ' + str(error)[:180])

    def start_lessons(self, *, duration_minutes=None):
        if self.closed:
            return False
        if self.session.active:
            self.notice.set("Режим уже включён.")
            return False
        if not self.authorize_parent("Начать уроки", allow_setup=True):
            return False
        return self._start(preview=False, duration_minutes=duration_minutes)

    def _start(self, preview, *, duration_minutes=None):
        if self.closed or self.session.active:
            return False
        try:
            value = self._save_settings(duration_minutes=duration_minutes)
            self.session.start(preview=preview, **value)
            self._event_lines.clear()
            self._set_controls()
            self._show_indicator()
            self.notice.set(
                "Пробная проверка. Окна с истёкшим отсчётом отмечаются в таблице."
                if preview else "Время уроков. Сохраните игру или документ до конца отсчёта."
            )
            self._refresh()
        except Exception as error:
            self.session.stop()
            self._hide_indicator()
            self._set_controls()
            self.notice.set("Не удалось включить режим. " + str(error)[:180])
            return False
        return self.session.active

    def authorize_parent(self, title, *, allow_setup=False):
        try:
            if not self.guard.configured():
                if allow_setup and not (self.session.active and not self.session.preview):
                    return self._setup_parent_pin()
                self.notice.set("Родительский PIN не настроен.")
                return False
            pin = simpledialog.askstring(
                title, "Введите PIN родителя.", show="•", parent=self.winfo_toplevel(),
            )
            if pin is None:
                self.notice.set("Подтверждение отменено.")
                return False
            if self.guard.verify(pin):
                return True
            self.notice.set("Неверный PIN.")
        except Exception as error:
            self.notice.set("Не удалось подтвердить PIN. " + str(error)[:180])
        return False

    def _setup_parent_pin(self):
        pin = simpledialog.askstring(
            "PIN родителя", "Задайте PIN родителя из 6–12 цифр для плана и режима уроков.",
            show="•", parent=self.winfo_toplevel(),
        )
        if pin is None:
            self.notice.set("Настройка PIN отменена.")
            return False
        if not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 12:
            self.notice.set("PIN должен содержать от 6 до 12 цифр.")
            return False
        confirmation = simpledialog.askstring(
            "Повторите PIN родителя", "Введите тот же PIN ещё раз.",
            show="•", parent=self.winfo_toplevel(),
        )
        if confirmation is None:
            self.notice.set("Настройка PIN отменена.")
            return False
        if confirmation != pin:
            self.notice.set("PIN не совпали. Начните настройку ещё раз.")
            return False
        self.guard.set_pin(pin)
        self.guard_status.set("PIN родителя установлен. Он нужен для настройки плана и управления уроками.")
        return True

    def stop(self):
        return self.request_stop()

    def request_stop(self):
        """Return whether a parent allowed stopping before the session expires."""
        if self.session.active and not self.session.preview:
            verified = self.authorize_parent("Завершить уроки")
            if not self.session.active:
                return True
            if not verified:
                return False
        try:
            self.force_stop()
        except (OSError, RuntimeError, ValueError) as error:
            self.notice.set("Не удалось завершить уроки. " + str(error)[:180])
            return False
        return True

    def force_stop(self):
        self.session.stop()
        self._hide_indicator()
        self._set_controls()
        self._render_rows()
        self.notice.set("Режим завершён. Отложенные команды закрытия отменены.")

    def _set_controls(self):
        running = self.session.active
        for widget in (self.preview_button, self.start_button, self.add_button, self.remove_button):
            widget.configure(state="disabled" if running else "normal")
        for widget in (self.duration_box, self.delay_box):
            widget.configure(state="disabled" if running else "readonly")
        self.stop_button.configure(state="normal" if running else "disabled")
        if not running:
            self.status.set("Выключено")

    def _show_indicator(self):
        self._hide_indicator()
        self._indicator = tk.Toplevel(self)
        self._indicator.title("Пространство · Время уроков")
        self._indicator.geometry("520x160+80+80")
        self._indicator.resizable(False, False)
        self._indicator.attributes("-topmost", True)
        self._indicator_text = tk.StringVar()
        ttk.Label(
            self._indicator, textvariable=self._indicator_text, font=("Segoe UI", 16, "bold"),
            padding=18, wraplength=480, justify="left",
        ).pack(fill="both", expand=True)
        ttk.Label(
            self._indicator, text="Управление во вкладке «Уроки» приложения «Пространство».",
            padding=(18, 0, 18, 12), wraplength=480,
        ).pack(fill="x")
        self._indicator.protocol("WM_DELETE_WINDOW", self._bring_controls)

    def _bring_controls(self):
        top = self.winfo_toplevel()
        top.deiconify()
        top.lift()
        self.focus_set()

    def _hide_indicator(self):
        if self._indicator is not None:
            try:
                self._indicator.destroy()
            except tk.TclError:
                pass
        self._indicator = None
        self._indicator_text = None

    def _render_rows(self):
        rows = []
        for item in self.session.pending:
            window = item["window"]
            if item.get("retrying"):
                action = f"Повтор закрытия через {max(0, math.ceil(item['remaining_seconds']))} с"
            elif item["requested"]:
                labels = {
                    "preview": "Пробная отметка", "requested": "Закрытие запрошено",
                    "terminated": "Roblox завершён",
                    "gone": "Окно уже закрыто", "changed": "Окно изменилось",
                    "unavailable": "Окно недоступно",
                }
                result = item.get("result") or ("preview" if self.session.preview else "requested")
                action = labels.get(result, "Проверка завершена")
            else:
                action = f"Через {max(0, math.ceil(item['remaining_seconds']))} с"
            rows.append((window.handle, window.pid, window.executable, window.title, action))
        if rows == self._displayed_rows:
            return
        self._displayed_rows = rows
        self.windows.delete(*self.windows.get_children())
        for index, (_, _, executable, title, action) in enumerate(rows):
            self.windows.insert("", "end", iid=str(index), values=(Path(executable).name, title, action))

    def _refresh(self):
        events = self.session.tick()
        for event in events:
            labels = {
                "preview": "Пробная отметка",
                "requested": "Закрытие запрошено",
                "terminated": "Roblox завершён",
                "gone": "Окно уже закрыто",
                "changed": "Окно изменилось",
                "unavailable": "Окно недоступно",
                "expired": "Время режима истекло",
            }
            action = event.get("action", "")
            label = labels.get(action, action)
            name = event.get("application", "")
            self._event_lines.append(label + (" · " + name if name else ""))
        self._event_lines = self._event_lines[-3:]
        if self._event_lines:
            self.notice.set("\n".join(self._event_lines))
        self._render_rows()
        if self.session.active:
            name = "Пробный режим" if self.session.preview else "Уроки"
            remaining = duration_text(self.session.remaining_seconds)
            self.status.set(f"{name} · {remaining}")
            waiting = [item["remaining_seconds"] for item in self.session.pending
                       if not item["requested"] or item.get("retrying")]
            detail = (
                f"Команда закрытия через {max(0, math.ceil(min(waiting)))} с. Сохраните работу."
                if waiting else "Режим действует для вновь открываемых окон."
            )
            if self.session.preview:
                detail = "Пробная проверка окон. Результаты в приложении."
            if self._indicator_text is not None:
                timing = remaining if remaining == UNTIMED_LABEL else f"осталось {remaining}"
                self._indicator_text.set(f"{name} · {timing}\n{detail}")
        else:
            self._hide_indicator()
            self._set_controls()

    def _schedule(self):
        if not self.closed:
            self._timer = self.after(400, self._poll)

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        try:
            if self.session.active:
                self._refresh()
        except Exception as error:
            self.session.stop()
            self._hide_indicator()
            self._set_controls()
            self.notice.set("Режим остановлен после ошибки. " + str(error)[:180])
        self._schedule()

    def _on_destroy(self, event):
        if event.widget is self:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        self.session.stop()
        self._hide_indicator()
