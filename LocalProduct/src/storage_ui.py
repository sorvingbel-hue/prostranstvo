"""Explicit, asynchronous metadata-audit panel. Closing never stops a worker."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk


_STATES = {"idle": "Проверка ещё не запускалась", "queued": "Запуск запрошен; обход ещё не подтверждён",
           "running": "Идёт обход метаданных", "completed": "Обход завершён", "partial": "Обход завершён с пропусками",
           "interrupted": "Обход прерван", "failed": "Проверка не завершена", "busy": "Другой обход уже выполняется",
           "uncertain": "Запуск не подтверждён; автоматического повтора нет"}


def _gib(value):
    return f"{value / 1024 ** 3:.2f} ГиБ" if type(value) is int and value >= 0 else "нет данных"


def format_snapshot(value):
    """Bounded local presentation, with no progress percentage or savings claim."""
    state = value.get("state", "failed")
    lines = [_STATES.get(state, "Состояние неизвестно")]
    if value.get("task_id"):
        lines.extend((f"Задание: {value['task_id']}", "Корни: " + ", ".join(value.get("roots", []))))
    if value.get("error_code"):
        lines.append("Причина: " + str(value["error_code"]))
    progress = value.get("progress") or {}
    report = value.get("report") or {}
    counters = report.get("counters") or progress.get("counters") or {}
    if counters:
        lines.extend(("", f"Обнаружено файлов: {counters.get('files', 0):,}; каталогов: {counters.get('directories', 0):,}.",
                      "Сумма логических размеров по путям: " + _gib(counters.get("logical_bytes_raw")),
                      "Известные уникальные логические байты: " + _gib(counters.get("logical_bytes_unique_known")),
                      f"Пропущено известных записей: {counters.get('skipped_entries', 0):,}; отказов доступа: {counters.get('permission_denied', 0):,}; ошибок: {counters.get('errors', 0):,}."))
    if progress.get("updated_utc"):
        lines.append("Последние метаданные: " + str(progress["updated_utc"]))
    if progress.get("stale") and state == "running":
        lines.append("Обновление задерживается. Файловая операция может ожидать ответа; завершение не подтверждено.")
    if value.get("progress_error"):
        lines.append("Сохранённый прогресс недоступен; это не подтверждает завершение обхода.")
    if report:
        lines.append("Отчёт сохранён: " + str(value.get("report_path") or "—"))
        lines.append("Время завершения отчёта: " + str(report.get("finished_utc", "—")))
        if report.get("output_limits", {}).get("budget_truncated"):
            lines.append("Список подробностей сокращён из-за ограничения размера отчёта.")
        for key, title, size_key in (("folder_aggregates", "Крупные папки (уровни пересекаются)", "logical_bytes_raw"),
                                     ("largest_files", "Крупные файлы по логическому размеру", "logical_bytes")):
            lines.extend(("", title + ":"))
            rows = report.get(key, [])
            if isinstance(rows, list):
                for row in rows[:15]:
                    if isinstance(row, dict):
                        lines.append(_gib(row.get(size_key)) + "  " + str(row.get("path", "—"))[:4096])
    lines.extend(("", "Полный объём записей неизвестен. В пропущенных каталогах могут быть дополнительные файлы.",
                  "Размеры логические: они не показывают физически занятое или освобождаемое место.",
                  "Проверка читает метаданные. Содержимое файлов не читается; ничего не удаляется и не перемещается.",
                  "Облачные, offline и связанные пути пропускаются по правилам сканера. Отчёт остаётся на этом компьютере."))
    return "\n".join(lines)


class StoragePanel(ttk.Frame):
    def __init__(self, parent, tasks=None):
        super().__init__(parent, padding=12)
        if tasks is None:
            from storage_tasks import StorageTasks
            tasks = StorageTasks()
        self.tasks = tasks
        self.closed = False
        self._io_busy = False
        self._timer = None
        self._results = queue.Queue()
        self._task_id = None
        self._active = False
        self._display_key = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.bind("<Destroy>", self._on_destroy, add=True)
        ttk.Label(self, text="Место на дисках", font=("Segoe UI", 17, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, text=("Выберите диски и явно запустите проверку. Обход выполняется в отдельном процессе; "
                              "окно можно закрыть и позже прочитать результат."),
                  wraplength=950, justify="left").grid(row=1, column=0, sticky="ew", pady=(8, 12))
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.c_drive = tk.BooleanVar(self, value=True)
        self.d_drive = tk.BooleanVar(self, value=False)
        ttk.Checkbutton(actions, text="C:", variable=self.c_drive).pack(side="left")
        ttk.Checkbutton(actions, text="D:", variable=self.d_drive).pack(side="left", padx=8)
        self.start_button = ttk.Button(actions, text="Проверить выбранные диски", command=self.start_audit)
        self.start_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Обновить состояние", command=self.refresh).pack(side="left")
        body = ttk.Frame(self)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.output = tk.Text(body, wrap="word", font=("Segoe UI", 11), state="disabled", height=18)
        self.output.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.output.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)
        self.status = tk.StringVar(self, value="Проверка запускается только кнопкой. Сохранённое состояние ещё не прочитано.")
        self.notice = tk.StringVar(self, value="")
        ttk.Label(self, textvariable=self.status, wraplength=950).grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(self, textvariable=self.notice, wraplength=950).grid(row=5, column=0, sticky="ew", pady=(4, 0))
        # No disk read, worker or timer until the containing app asks to poll.

    def _update_button(self):
        self.start_button.configure(state="disabled" if self._io_busy or self._active else "normal")

    def _request(self, operation, roots=None):
        if self.closed or self._io_busy:
            return False
        self._io_busy = True
        self._update_button()
        try:
            threading.Thread(target=self._io_worker, args=(operation, roots), daemon=True,
                             name="storage-status").start()
        except Exception:
            self._io_busy = False
            self._update_button()
            self.notice.set("Не удалось запустить чтение состояния.")
            return False
        self._schedule(100)
        return True

    def _io_worker(self, operation, roots):
        # The worker communicates only through Queue, never calls Tk APIs.
        if self.closed:
            return
        try:
            value = self.tasks.start(roots) if operation == "start" else self.tasks.status()
            self._results.put((True, value))
        except Exception as error:
            self._results.put((False, str(error)[:240]))

    def start_audit(self):
        if self.closed or self._io_busy or self._active:
            return False
        roots = (["C:\\"] if self.c_drive.get() else []) + (["D:\\"] if self.d_drive.get() else [])
        if not roots:
            self.notice.set("Выберите хотя бы один диск.")
            return False
        self.notice.set("")
        self.status.set("Сохраняю запрос на проверку…")
        return self._request("start", roots)

    def refresh(self):
        if self.closed:
            return False
        return self._request("status")

    def _render(self, value):
        progress = value.get("progress") or {}
        key = (value.get("task_id"), value.get("state"), value.get("updated_utc"),
               progress.get("updated_utc"), progress.get("stale"), value.get("error_code"),
               value.get("progress_error"), value.get("report_error"))
        presentation = format_snapshot(value) if key != self._display_key else None
        self._task_id = value.get("task_id")
        self._active = value.get("state") in {"queued", "running"}
        self.status.set(_STATES.get(value.get("state"), "Состояние неизвестно"))
        self._update_button()
        if key == self._display_key:
            return
        self._display_key = key
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", presentation)
        self.output.configure(state="disabled")

    def _schedule(self, milliseconds):
        if self.closed:
            return
        if self._timer is not None:
            self.after_cancel(self._timer)
        self._timer = self.after(milliseconds, self._poll)

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        received = False
        while True:
            try:
                success, payload = self._results.get_nowait()
            except queue.Empty:
                break
            received = True
            self._io_busy = False
            if success:
                try:
                    self._render(payload)
                except Exception:
                    self._display_key = None
                    self.notice.set("Сохранённые данные не удалось отобразить. Следующее обновление выполнится автоматически.")
                    self.status.set("Результат операции не подтверждён.")
                    self._update_button()
                else:
                    self.notice.set("")
            else:
                self.notice.set("Не удалось прочитать или запустить проверку: " + payload)
                self.status.set("Результат операции не подтверждён.")
                self._update_button()
        if not self._io_busy and not received:
            self._request("status")
        self._schedule(100 if self._io_busy else 1500)

    def start_polling(self):
        if not self.closed and self._timer is None:
            self.refresh()

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

    def _on_destroy(self, event):
        if event.widget is self:
            self.close()
