"""Read-only maintenance reports and unverified advice; no action execution."""
from __future__ import annotations

import json
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk


def _text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) if isinstance(value, (dict, list)) else str(value)


def _gib(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return 'нет данных'
    return f'{value / 1024 ** 3:.1f} ГиБ'


class MaintenancePanel(ttk.Frame):
    def __init__(self, parent, advisor=None):
        super().__init__(parent, padding=12)
        if advisor is None:
            from maintenance_advisor import MaintenanceAdvisor
            advisor = MaintenanceAdvisor()
        self.advisor = advisor
        self.closed = False
        self._busy = False
        self._timer = None
        self._results = queue.Queue()
        self._showing_payload = False
        self._display_key = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.bind('<Destroy>', self._on_destroy, add=True)
        ttk.Label(self, text='Диагностика и ИИ', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(self, text=('Отчёт читает состояние компьютера. Советы помощника требуют проверки; '
                              'эта панель ничего не исправляет и не запускает модель.'),
                  wraplength=950, justify='left').grid(row=1, column=0, sticky='ew', pady=(8, 12))
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        self.collect_button = ttk.Button(actions, text='Сформировать отчёт', command=self.collect_report)
        self.collect_button.pack(side='left')
        ttk.Button(actions, text='Обновить вид', command=self.refresh).pack(side='left', padx=8)
        ttk.Button(actions, text='Для помощника', command=self.show_ai_report).pack(side='left')
        body = ttk.Frame(self)
        body.grid(row=3, column=0, sticky='nsew')
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.output = tk.Text(body, wrap='word', font=('Segoe UI', 11), state='disabled', height=18)
        self.output.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(body, orient='vertical', command=self.output.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.tag_configure('heading', font=('Segoe UI', 12, 'bold'))
        self.output.tag_configure('unverified', foreground='#8a4b00', font=('Segoe UI', 11, 'bold'))
        self.status = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=950).grid(row=4, column=0, sticky='ew', pady=(8, 0))
        ttk.Label(self, textvariable=self.notice, wraplength=950).grid(row=5, column=0, sticky='ew', pady=(4, 0))
        # Reading a saved report is inert. Polling is enabled separately by App.
        self.refresh()

    def _write(self, sections):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        for text, tag in sections:
            self.output.insert('end', text, tag)
        self.output.configure(state='disabled')

    def _render(self, report):
        key = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
        if key == self._display_key:
            return
        self._display_key = key
        if report is None:
            self._write([('Сохранённого отчёта пока нет. Нажмите «Сформировать отчёт».', '')])
            self.status.set('Отчёт ещё не сформирован.')
            return
        facts = report.get('facts', {})
        sections = [(f"Отчёт {report.get('report_id', '—')}\nСобран: {report.get('collected_utc', '—')}\n\n", '')]
        sections.append(('Факты\n', 'heading'))
        for drive in facts.get('drives', []):
            sections.append((f"{drive.get('drive', 'Диск')}: свободно {_gib(drive.get('free'))} из {_gib(drive.get('total'))}\n", ''))
        os_info = facts.get('os', {})
        sections.append((f"Система: {os_info.get('system') or 'нет данных'}; версия: {os_info.get('release') or '—'}; сборка: {os_info.get('build') or '—'}\n", ''))
        service = facts.get('system_service', {})
        installed = {True: 'да', False: 'нет', None: 'нет данных'}.get(service.get('installed'), 'нет данных')
        sections.append((f"Системная служба: установлена — {installed}; состояние — {service.get('state', 'нет данных')}.\n", ''))
        if service.get('available'):
            stale = 'УСТАРЕЛИ' if service.get('stale') else 'сохранённые данные'
            sections.append((f"Данные службы: {stale}; время: {service.get('evidence_utc') or '—'}; возраст: {service.get('age_seconds')} с.\n"
                             f"Сохранённый вердикт: {service.get('verdict') or 'нет данных'}.\n", ''))
            if service.get('job'):
                sections.append(('Последняя проверка службы:\n' + _text(service['job']) + '\n', ''))
        else:
            sections.append(('Данные проверки службы недоступны.\n', ''))
        if facts.get('unavailable'):
            sections.append(('Нет данных от источников: ' + ', '.join(map(str, facts['unavailable'])) + '\n', ''))
        sections.append(('\nНаблюдения отчёта\n', 'heading'))
        findings = report.get('findings', [])
        for finding in findings:
            sections.append((f"• [{finding.get('severity', 'info')}] {finding.get('message', '')}\n", ''))
            if finding.get('evidence'):
                sections.append(('  Данные: ' + _text(finding['evidence']) + '\n', ''))
            if finding.get('action_ids'):
                sections.append(('  Возможные действия (не выполняются): ' + ', '.join(finding['action_ids']) + '\n', ''))
        if not findings:
            sections.append(('Наблюдений в отчёте нет. Это не подтверждает отсутствие проблем.\n', ''))
        ai = report.get('ai', {})
        sections.append(('\nСоветы ИИ — НЕ ПРОВЕРЕНЫ\n', 'unverified'))
        if ai.get('status') == 'advice_received':
            for observation in ai.get('observations', []):
                sections.append(('• ' + str(observation) + '\n', 'unverified'))
            if ai.get('action_ids'):
                sections.append(('Предложения действий (не выполняются): ' + ', '.join(ai['action_ids']) + '\n', 'unverified'))
        else:
            sections.append(('Для анализа нужен подключённый помощник. Автоматический запрос к модели не выполняется.\n', ''))
        if ai.get('note'):
            sections.append((str(ai['note']) + '\n', 'unverified'))
        self._write(sections)
        self.status.set('Показан сохранённый отчёт. ' + ('Сбор нового отчёта продолжается.' if self._busy
                        else 'Предложенные действия не выполняются.'))

    def refresh(self, *, background=False):
        if self.closed:
            return
        if not background:
            self._showing_payload = False
            self._display_key = None
            self.notice.set('')
        try:
            self._render(self.advisor.latest())
        except Exception as error:
            self.notice.set('Не удалось прочитать отчёт. ' + str(error)[:240])

    def show_ai_report(self):
        if self.closed:
            return
        try:
            payload = self.advisor.ai_report()
            self._showing_payload = True
            self._display_key = None
            self.notice.set('')
            self._write([(json.dumps(payload, ensure_ascii=False, indent=2), '')])
            self.status.set('Данные для подключённого помощника. Запрос к модели не отправлялся.')
        except Exception as error:
            self.notice.set('Не удалось подготовить данные для помощника. ' + str(error)[:240])

    def collect_report(self):
        if self.closed or self._busy:
            return False
        self._busy = True
        self._showing_payload = False
        self.notice.set('')
        self.status.set('Собираю отчёт без изменения настроек…')
        self.collect_button.configure(state='disabled')
        try:
            threading.Thread(target=self._collect_worker, daemon=True, name='MaintenanceReport').start()
            self._schedule_poll(100)
            return True
        except Exception as error:
            self._busy = False
            self.collect_button.configure(state='normal')
            self.notice.set('Не удалось начать сбор. ' + str(error)[:240])
            return False

    def _collect_worker(self):
        if self.closed:
            return
        try:
            outcome = (True, self.advisor.collect())
        except Exception as error:
            outcome = (False, str(error)[:240])
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
            success, value = self._results.get_nowait()
        except queue.Empty:
            if not self._busy and not self._showing_payload:
                self.refresh(background=True)
        else:
            self._busy = False
            self.collect_button.configure(state='normal')
            if success:
                self._display_key = None
                if self._showing_payload:
                    self.notice.set('Новый отчёт сохранён. «Для помощника» покажет его актуальную версию.')
                else:
                    self._render(value)
            else:
                self.notice.set('Не удалось собрать отчёт. ' + value)
                self.status.set('Сбор не завершён. Прежний отчёт сохранён.')
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
