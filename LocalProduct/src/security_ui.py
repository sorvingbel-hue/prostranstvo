"""Inert saved-report view; only the explicit button starts one collector."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import ttk


_GROUPS = {
    "windows": ("Windows", {"product": "Система", "release": "Версия", "build": "Сборка", "revision": "Ревизия"}),
    "updates": ("Обновления", {"latest_kb_id": "Последний наблюдаемый KB", "latest_kb_installed_date": "Дата установки этого KB"}),
    "defender": ("Microsoft Defender", {"antivirus_enabled": "Антивирус включён", "realtime_enabled": "Защита в реальном времени",
        "signatures_version": "Версия сигнатур", "signatures_updated_utc": "Обновление сигнатур", "quick_scan_end_utc": "Последняя быстрая проверка",
        "full_scan_end_utc": "Последняя полная проверка", "quick_scan_overdue": "Быстрая проверка просрочена", "full_scan_overdue": "Полная проверка просрочена"}),
    "threats": ("Наблюдение Defender", {"active_threat_count": "Число активных угроз, известных Defender"}),
    "firewall": ("Брандмауэр: активные профили", {"domain_enabled": "Доменный включён", "private_enabled": "Частный включён", "public_enabled": "Общедоступный включён"}),
    "encryption": ("Шифрование системного тома", {"system_volume_protection": "Защита BitLocker", "system_volume_encryption": "Состояние шифрования"}),
    "backup": ("Резервирование", {"legacy_windows_backup_configuration_seen": "Наблюдается конфигурация Windows Backup"}),
    "backup_assurance": ("Восстановление", {"restore_tested": "Восстановление из копии проверено"}),
    "permissions": ("Границы проверки", {"agent_permissions_coverage": "Права агентов охвачены полностью", "extension_permissions_coverage": "Права расширений охвачены полностью"}),
}
_UNKNOWN = {
    "access_denied": "нет доступа", "unavailable": "источник недоступен", "unsupported": "не поддерживается",
    "timeout": "истекло время ожидания", "output_limit": "превышен предел данных", "invalid_data": "неподтверждённые данные",
    "provider_error": "ошибка источника", "missing_data": "источник не вернул значение", "not_collected": "не собиралось",
    "not_tested": "не проверялось", "no_supported_configuration_observed": "поддерживаемая конфигурация не обнаружена; другие способы не проверены",
}


def _value(value):
    if type(value) is bool:
        return "да" if value else "нет"
    return str(value)


class SecurityPanel(ttk.Frame):
    def __init__(self, parent, reporter=None):
        super().__init__(parent, padding=12)
        if reporter is None:
            from security_report import SecurityReport
            reporter = SecurityReport()
        self.reporter = reporter
        self.closed = False
        self._busy = False
        self._timer = None
        self._results = queue.Queue()
        self._display_key = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.bind("<Destroy>", self._on_destroy, add=True)
        ttk.Label(self, text="Отчёт безопасности", font=("Segoe UI", 17, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, text=("Ограниченные локальные наблюдения: отчёт не определяет взлом и не подтверждает безопасность. "
                              "Проверка не меняет настройки и не запускает антивирусное сканирование."),
                  wraplength=950, justify="left").grid(row=1, column=0, sticky="ew", pady=(8, 12))
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.collect_button = ttk.Button(actions, text="Сформировать отчёт безопасности", command=self.collect_report)
        self.collect_button.pack(side="left")
        ttk.Button(actions, text="Обновить вид", command=self.refresh).pack(side="left", padx=8)
        body = ttk.Frame(self)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.output = tk.Text(body, wrap="word", font=("Segoe UI", 11), state="disabled", height=18)
        self.output.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.output.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.tag_configure("heading", font=("Segoe UI", 12, "bold"))
        self.output.tag_configure("unknown", foreground="#8a4b00")
        self.status = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=950).grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(self, textvariable=self.notice, wraplength=950).grid(row=5, column=0, sticky="ew", pady=(4, 0))
        # Constructor only reads existing, validated data. App opts into polling.
        self.refresh()

    def _write(self, sections):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        for text, tag in sections:
            self.output.insert("end", text, tag)
        self.output.configure(state="disabled")

    def _render(self, payload):
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if key == self._display_key:
            return
        self._display_key = key
        if payload is None:
            self._write([("Сохранённого отчёта пока нет. Нажмите «Сформировать отчёт безопасности».", "")])
            self.status.set("Отчёт ещё не сформирован.")
            return
        report, freshness = payload["report"], payload["freshness"]
        stale = freshness.get("stale") is not False
        sections = [(f"Отчёт {report['report_id']}\nСобран (UTC): {report['collected_utc']}\n", "")]
        sections.append((("УСТАРЕЛИ: возраст отчёта неизвестен или превышает 24 часа.\n" if stale
                          else "Возраст отчёта не превышает 24 часа; это не оценка состояния защиты.\n"), "unknown"))
        unknown = 0
        for group, (heading, fields) in _GROUPS.items():
            sections.append(("\n" + heading + "\n", "heading"))
            for field, label in fields.items():
                row = report["evidence"][group][field]
                missing = row["value"] is None
                unknown += int(missing)
                value = "НЕИЗВЕСТНО: " + _UNKNOWN.get(row["unknown_reason"], "нет подтверждённых данных") if missing else _value(row["value"])
                sections.append((f"{label}: {value}\n", "unknown" if missing else ""))
                sections.append((f"  Наблюдение (UTC): {row['observed_utc']}; источник: {row['source']}\n", ""))
        sections.append(("\nПоследний KB не доказывает полноту обновлений. Нулевое число угроз Defender не доказывает отсутствие заражения. "
                         "Конфигурация резервирования не доказывает успешную копию или восстановление. "
                         "Права агентов и расширений охвачены не полностью.\n", "unknown"))
        self._write(sections)
        age = "УСТАРЕЛ" if stale else "возраст не более 24 часов"
        self.status.set(f"Сохранённый отчёт: {age}; НЕПОЛНЫЙ, неизвестных значений: {unknown}. Заключения о безопасности нет.")

    def refresh(self, *, background=False):
        if self.closed:
            return
        if not background:
            self._display_key = None
            self.notice.set("")
        try:
            report = self.reporter.latest()
            payload = None if report is None else self.reporter.for_ai(report["report_id"])
            self._render(payload)
        except Exception:
            self.status.set("Сохранённый отчёт недоступен; его состояние неизвестно.")
            self.notice.set("Не удалось прочитать очищенный отчёт. Обновите вид или сформируйте новый.")

    def collect_report(self):
        if self.closed or self._busy:
            return False
        self._busy = True
        self.notice.set("")
        self.status.set("Собираю ограниченный локальный отчёт без изменения настроек…")
        self.collect_button.configure(state="disabled")
        try:
            threading.Thread(target=self._collect_worker, daemon=True, name="SecurityReport").start()
            self._schedule_poll(100)
            return True
        except Exception:
            self._busy = False
            self.collect_button.configure(state="normal")
            self.status.set("Сбор не начат.")
            self.notice.set("Не удалось запустить сбор отчёта.")
            return False

    def _collect_worker(self):
        if self.closed:
            return
        try:
            report = self.reporter.collect()
            if self.closed:
                return
            outcome = (True, self.reporter.for_ai(report["report_id"]))
        except Exception:
            outcome = (False, None)
        if not self.closed:
            self._results.put(outcome)

    def start_polling(self):
        self._schedule_poll(3000)

    def _schedule_poll(self, delay):
        if self.closed:
            return
        if self._timer is not None:
            self.after_cancel(self._timer)
        self._timer = self.after(delay, self._poll)

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        try:
            success, payload = self._results.get_nowait()
        except queue.Empty:
            if not self._busy:
                self.refresh(background=True)
        else:
            self._busy = False
            self.collect_button.configure(state="normal")
            if success:
                self._display_key = None
                self._render(payload)
                self.notice.set("Отчёт сохранён локально. Это не подтверждение передачи удалённому помощнику.")
            else:
                self.status.set("Сбор или чтение нового отчёта не завершены. Прежний вид сохранён.")
                self.notice.set("Не удалось показать новый очищенный отчёт.")
        self._schedule_poll(200 if self._busy else 3000)

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
        # A running fixed snapshot owns its timeout. Closing a view never kills
        # a process, joins a thread or sends another system request.
