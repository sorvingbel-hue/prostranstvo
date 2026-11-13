"""Explicit family messages and private Telegram setup; no monitoring data."""
from __future__ import annotations

import argparse
import datetime as dt
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import uuid
import webbrowser

from family_messages import MessageStore
from family_telegram import TelegramSettings

LABELS = {
    "queued": "Ожидает отправки", "sending": "Отправляется",
    "sent": "Принято Telegram", "uncertain": "Результат неизвестен",
    "failed": "Ошибка отправки", "received": "Получено с телефона",
    "shown": "Окно показано на ПК",
}


def local_time(value):
    try:
        return dt.datetime.fromisoformat(value).astimezone().strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "—"


class AsyncFrame(ttk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._results = queue.Queue()
        self._callbacks = {}
        self._next_operation = 0
        self._busy = False
        self._closed = False
        self.bind("<Destroy>", self._on_destroy, add=True)
        self._drain_id = self.after(80, self._drain)

    def _on_destroy(self, event):
        if event.widget is self:
            self._closed = True
            # Tk callbacks belong to the UI thread even when a network request
            # outlives its window. Worker results carry only an opaque ID/data.
            self._callbacks.clear()
            for name in ('_drain_id', '_periodic_id', '_focus_id'):
                handle = getattr(self, name, None)
                if handle:
                    try:
                        self.after_cancel(handle)
                    except tk.TclError:
                        pass

    def background(self, action, success=None, failure=None):
        if self._closed or self._busy:
            return False
        self._busy = True
        self._next_operation += 1
        operation = self._next_operation
        self._callbacks[operation] = (success, failure)
        results = self._results
        def work(action=action, results=results, operation=operation):
            try:
                results.put((operation, action(), None))
            except Exception as error:
                # Exceptions keep tracebacks/frame references. Retain only the
                # public static code, never a worker traceback or UI callback.
                failure = RuntimeError('background_action_failed')
                code = getattr(error, 'code', None)
                if isinstance(code, str):
                    failure.code = code
                results.put((operation, None, failure))
        threading.Thread(target=work, daemon=True).start()
        return True

    def _drain(self):
        if self._closed:
            return
        try:
            while True:
                operation, result, error = self._results.get_nowait()
                success, failure = self._callbacks.pop(operation, (None, None))
                self._busy = False
                if error is not None:
                    if failure:
                        failure(error)
                    else:
                        messagebox.showerror("Общение", safe_error(error), parent=self.winfo_toplevel())
                elif success:
                    success(result)
        except queue.Empty:
            pass
        if not self._closed:
            self._drain_id = self.after(80, self._drain)


def safe_error(error):
    # Transport exceptions are already sanitized. Never print a traceback/HTTP URL.
    code = getattr(error, "code", None)
    explanations = {
        "telegram_401": "Telegram не принял токен бота. Проверь токен в BotFather.",
        "invalid_token": "Токен бота имеет неверный формат. Скопируй его из официального BotFather.",
        "telegram_409": "Этот бот уже используется другим подключением. Нужен отдельный бот.",
        "webhook_active": "У бота уже есть другое подключение. Создай отдельного бота для семейных сообщений.",
        "not_paired": "Сначала привяжи свой личный Telegram через кнопку «Настроить Telegram».",
        "not_configured": "Сначала подключи бота через кнопку «Настроить Telegram».",
        "binding_changed": "Эта очередь привязана к другому боту или получателю. Автоматическая смена заблокирована.",
        "pairing_invalid": "Срок ссылки привязки истёк или аккаунт ещё не подтверждён. Подготовь новую ссылку.",
        "credential_unavailable": "Windows не смогла открыть сохранённое подключение в этой учётной записи.",
        "request_failed": "Telegram сейчас не ответил. Проверь интернет и попробуй ещё раз.",
        "channel_paused": "Доставка Telegram приостановлена. Родитель может включить её в разделе «Общение».",
    }
    if code in explanations:
        return explanations[code]
    text = str(error)
    if "api.telegram.org" in text or "://" in text or "bot_token" in text.lower():
        return "Не удалось выполнить запрос Telegram. Проверь подключение и настройки бота."
    # Do not show arbitrary exception payloads from networking, files, or API replies.
    return "Действие не завершено. Проверь соединение и настройки Telegram."


class ReplyComposer(AsyncFrame):
    def __init__(self, master, store, settings, reply_to=None, on_saved=None):
        super().__init__(master, padding=20)
        self.store, self.settings = store, settings
        self.reply_to, self.on_saved = reply_to, on_saved
        self.client_id = uuid.uuid4().hex
        self.channel_enabled = False
        ttk.Label(self, text="ОТВЕТ ПАПЕ", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(self, text="Напиши сообщение и нажми кнопку внизу.", font=("Segoe UI", 14)).pack(anchor="w", pady=(8, 12))
        self.text = tk.Text(self, font=("Segoe UI", 22), wrap="word", height=7, padx=12, pady=12)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<Control-Return>", self._send_key)
        self.info = tk.StringVar(value="Проверяю подключение Telegram…")
        ttk.Label(self, textvariable=self.info, wraplength=740, font=("Segoe UI", 13)).pack(anchor="w", pady=12)
        self.save_button = ttk.Button(self, text="Сохранить ответ", command=self.save)
        self.save_button.pack(fill="x", ipady=9)
        self.background(self.settings.status, self._channel_state)
        self.text.focus_set()

    def _channel_state(self, state):
        self.channel_enabled = all(state.get(key) is True for key in ("configured", "paired", "has_binding", "enabled"))
        self.save_button.configure(text="Отправить папе в Telegram" if self.channel_enabled else "Сохранить ответ для отправки")
        self.info.set("Только этот текст будет отправлен в Telegram." if self.channel_enabled else
                      "Доставка Telegram не включена. Ответ сохранится на этом компьютере до подключения.")

    def _send_key(self, event):
        self.save()
        return "break"

    def save(self):
        if self._busy:
            return
        text = self.text.get("1.0", "end-1c")
        if not text.strip():
            self.info.set("Сначала напиши сообщение.")
            return
        if len(text) > 4000:
            self.info.set("Сообщение слишком длинное: максимум 4000 символов.")
            return
        self.save_button.configure(state="disabled")
        self.text.configure(state="disabled")
        store, settings = self.store, self.settings
        client_id, reply_to = self.client_id, self.reply_to
        channel_enabled = getattr(self, "channel_enabled", False)
        def save_local():
            # An active delivery is bound under the transport mutex to the
            # current fixed recipient. A pause/rebind since the UI refresh
            # must leave the draft intact rather than silently retarget it.
            if channel_enabled:
                message = settings.queue_reply(store, text, client_id, reply_to)
            else:
                message = store.enqueue_reply(text, client_id=client_id, reply_to=reply_to)
            try:
                if settings.status().get("enabled"):
                    from family_agent import ensure_running
                    ensure_running(store)
            except Exception:
                return message, False
            return message, True
        def done(result):
            message, worker_ok = result
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.client_id = uuid.uuid4().hex
            self.save_button.configure(state="normal")
            self.info.set("Ответ сохранён в очереди. Здесь ещё нет подтверждения доставки на телефон." if worker_ok else
                          "Ответ сохранён. Модуль доставки не ответил; сообщение остаётся в очереди.")
            if self.on_saved:
                self.on_saved(message)
        def failed(error):
            self.save_button.configure(state="normal")
            self.text.configure(state="normal")
            self.info.set("Ответ не сохранён. Текст оставлен в поле — попробуй ещё раз.")
        self.background(save_local, done, failed)


class TelegramSetup(tk.Toplevel):
    def __init__(self, master, settings, on_changed):
        super().__init__(master)
        self.title("Подключить Telegram · Пространство")
        self.geometry("780x700")
        self.minsize(650, 520)
        self.settings, self.on_changed, self.pairing = settings, on_changed, None
        self.link = tk.StringVar()
        canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.body = AsyncFrame(canvas, padding=20)
        body_window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda event:canvas.configure(scrollregion=canvas.bbox("all")), add=True)
        canvas.bind("<Configure>", lambda event:canvas.itemconfigure(body_window, width=event.width))
        ttk.Label(self.body, text="Один отдельный бот для семейных сообщений", font=("Segoe UI", 16, "bold"), wraplength=710).pack(anchor="w")
        instructions = ("1. В Telegram открой официального @BotFather и создай бота командой /newbot.\n"
                        "2. Вставь выданный токен только в поле ниже.\n"
                        "3. Открой ссылку своего бота, нажми Start и подтверди аккаунт.\n"
                        "4. В разделе «Общение» включи доставку — готово.\n\n"
                        "Обычная переписка через Telegram-бота бесплатна, ИИ для отправки не нужен.\n"
                        "Нужен отдельный бот: существующие webhook и чужое подключение не отключаются.")
        ttk.Label(self.body, text=instructions, wraplength=710, font=("Segoe UI", 12)).pack(anchor="w", pady=12)
        ttk.Button(self.body, text="Открыть официального BotFather", command=lambda:webbrowser.open("https://t.me/BotFather")).pack(anchor="w")
        ttk.Label(self.body, text="Токен бота (как пароль):").pack(anchor="w", pady=(14, 4))
        self.token = ttk.Entry(self.body, show="•", font=("Segoe UI", 13))
        self.token.pack(fill="x")
        ttk.Button(self.body, text="Проверить бота и подготовить привязку", command=self.prepare).pack(anchor="w", pady=10)
        self.resume_button = ttk.Button(self.body, text="Продолжить привязку сохранённого бота", command=self.resume_pairing, state="disabled")
        self.resume_button.pack(anchor="w", pady=3)
        self.note = tk.StringVar(value="Токен хранится с защитой Windows для текущей учётной записи. В общей учётке это не отделяет взрослых от детей.")
        ttk.Label(self.body, textvariable=self.note, wraplength=710, font=("Segoe UI", 12)).pack(anchor="w", pady=8)
        self.open_button = ttk.Button(self.body, text="Открыть своего бота → Start", command=self.open_bot, state="disabled")
        self.open_button.pack(anchor="w", pady=5)
        link_bar = ttk.Frame(self.body)
        link_bar.pack(fill="x", pady=5)
        ttk.Entry(link_bar, textvariable=self.link, state="readonly").pack(side="left", fill="x", expand=True)
        self.copy_button = ttk.Button(link_bar, text="Скопировать ссылку", command=self.copy_link, state="disabled")
        self.copy_button.pack(side="left", padx=(8,0))
        self.check_button = ttk.Button(self.body, text="Я нажал Start — проверить аккаунт", command=self.check_pairing, state="disabled")
        self.check_button.pack(anchor="w", pady=5)
        ttk.Label(self.body, text="Токен не отправляется в ChatGPT и не записывается в общий журнал.", wraplength=710).pack(anchor="w", pady=14)
        self.body.background(self.settings.status, self._initial_state)

    def _initial_state(self, state):
        if state.get("configured") is True:
            self.resume_button.configure(state="normal")
            self.note.set("Бот уже сохранён. Токен повторно вводить не нужно. Продолжение привязки приостанавливает доставку до подтверждения.")

    def _pairing_ready(self, pairing):
        self.pairing = pairing
        self.link.set(pairing["url"])
        self.note.set("Открой ссылку на своём телефоне или на ПК, нажми Start и проверь аккаунт кнопкой ниже. Ссылка действует 10 минут.")
        self.open_button.configure(state="normal")
        self.copy_button.configure(state="normal")
        self.check_button.configure(state="normal")
        self.on_changed()

    def _pairing_failure(self, error):
        self.pairing = None
        self.link.set("")
        for button in (self.open_button, self.copy_button, self.check_button):
            button.configure(state="disabled")
        self.note.set(safe_error(error))

    def resume_pairing(self):
        if self.body._busy:
            return
        self.note.set("Подготавливаю новую ссылку сохранённого бота…")
        self.body.background(self.settings.begin_pairing, self._pairing_ready, self._pairing_failure)

    def copy_link(self):
        if self.pairing:
            self.clipboard_clear()
            self.clipboard_append(self.pairing["url"])
            self.note.set("Ссылка скопирована. Открой её в своём Telegram и нажми Start, затем проверь аккаунт ниже.")

    def prepare(self):
        if self.body._busy:
            return
        token = self.token.get().strip()
        self.token.delete(0, "end")
        self.note.set("Проверяю бота через Telegram…")
        settings = self.settings
        def prepare():
            settings.setup(token)
            return settings.begin_pairing()
        self.body.background(prepare, self._pairing_ready, self._pairing_failure)

    def open_bot(self):
        if self.pairing:
            webbrowser.open(self.pairing["url"])

    def check_pairing(self):
        self.note.set("Проверяю нажатие Start…")
        def done(candidate):
            if not candidate:
                self.note.set("Нажатие Start пока не получено. Открой ссылку своего бота и повтори проверку.")
                return
            display = candidate.get("first_name") or candidate.get("username") or "аккаунт Telegram"
            if not messagebox.askyesno("Это твой Telegram?", "Привязать аккаунт «" + display[:100] + "»\nID: " + str(candidate["user_id"]) + "?\n\nВ этот личный чат будут приходить ответы с компьютера.", parent=self):
                self.note.set("Аккаунт не подтверждён.")
                return
            def confirmed():
                self.note.set("Аккаунт привязан. Включи доставку на вкладке «Общение».")
                self.on_changed()
                self.destroy()
            settings, nonce = self.settings, self.pairing["nonce"]
            self.body.background(lambda:settings.confirm_pairing(nonce), lambda _:confirmed())
        self.body.background(self.settings.poll_pairing, done)


class FamilyMessagesPanel(AsyncFrame):
    def __init__(self, master, store=None, settings=None):
        super().__init__(master, padding=12)
        self.store = store or MessageStore()
        self.settings = settings or TelegramSettings(self.store.root)
        self.cached = {}
        self.rows = {}
        ttk.Label(self, text="ОБЩЕНИЕ С ПАПОЙ", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        self.summary = tk.StringVar(value="Проверяю состояние…")
        ttk.Label(self, textvariable=self.summary, wraplength=1080).pack(anchor="w", pady=9)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        for text, command in [("Ответить папе", self.compose), ("Настроить Telegram", self.setup),
                              ("Включить / приостановить", self.toggle), ("Обновить", self.refresh)]:
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 8))
        self.tree = ttk.Treeview(self, columns=("direction", "time", "status", "text"), show="headings", height=8)
        for key, title, width in [("direction", "От кого", 120), ("time", "Время", 120),
                                  ("status", "Состояние", 220), ("text", "Сообщение", 480)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=80)
        self.tree.pack(fill="both", expand=True, pady=10)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)
        self.details = tk.Text(self, height=4, font=("Segoe UI", 14), wrap="word", padx=12, pady=10, state="disabled")
        self.details.pack(fill="x")
        tools = ttk.Frame(self)
        tools.pack(fill="x", pady=8)
        ttk.Button(tools, text="Повторить выбранный ответ", command=self.retry).pack(side="left")
        ttk.Button(tools, text="Удалить локальную запись", command=self.delete).pack(side="left", padx=8)
        ttk.Button(tools, text="Отключить Telegram", command=self.disconnect).pack(side="left")
        self.startup_var = tk.BooleanVar(value=False)
        ttk.Button(self, text="Семья, память и логи Мурчика…", command=self.family_profile).pack(anchor="w", pady=5)
        ttk.Checkbutton(self, text="Запускать доставку после входа в Windows", variable=self.startup_var, command=self.change_startup).pack(anchor="w", pady=5)
        ttk.Label(self, text="Только явно отправленные сообщения. Принято Telegram ≠ прочитано папой. Уведомления зависят от настроек телефона.\nТексты хранятся на этом ПК до ручного удаления; удаление здесь не удаляет переписку из Telegram.", wraplength=1080).pack(anchor="w", pady=5)
        self.refresh()
        self._periodic_id = self.after(4000, self.periodic)

    def family_profile(self):
        from family_context_ui import FamilyProfileWindow
        FamilyProfileWindow(self)

    def periodic(self):
        if not self._closed:
            self.refresh()
            self._periodic_id = self.after(4000, self.periodic)

    def refresh(self):
        if self._busy:
            return
        settings, store = self.settings, self.store
        def load():
            from family_agent import worker_status
            import family_startup
            try:
                startup = family_startup.startup_enabled()
            except OSError:
                startup = False
            return settings.status(), worker_status(store), store.list_messages(limit=100), startup
        def done(data):
            channel, worker, rows, startup = data
            self.cached = channel
            self.startup_var.set(startup)
            enabled = channel.get("enabled")
            paired = channel.get("paired") or channel.get("chat_id") or channel.get("recipient_set")
            phase = "Доставка включена" if enabled else "Доставка приостановлена" if paired else "Telegram ещё не подключён"
            if enabled:
                phase += " · модуль работает" if worker.get("running") else " · модуль не запущен"
                if worker.get("phase") == "attention":
                    phase += " · есть ошибка доставки или показа"
            self.summary.set(phase + ". С компьютера уходят только отправленные здесь сообщения.")
            selection = self.tree.selection()
            old = selection[0] if selection else None
            self.tree.delete(*self.tree.get_children())
            self.rows = {row["id"]:row for row in rows}
            for row in rows:
                self.tree.insert("", "end", iid=row["id"], values=("С компьютера" if row["direction"] == "outgoing" else "С телефона", local_time(row.get("created_at")), LABELS.get(row["status"], row["status"]), row["text"].replace("\n", " ")[:110]))
            if old in self.rows:
                self.tree.selection_set(old)
        self.background(load, done)

    def selected(self):
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def show_selected(self, event=None):
        row = self.selected()
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        if row:
            self.details.insert("1.0", row["text"])
        self.details.configure(state="disabled")

    def compose(self):
        window = tk.Toplevel(self)
        window.title("Ответ папе")
        window.geometry("840x610")
        ReplyComposer(window, self.store, self.settings, on_saved=lambda _:self.refresh()).pack(fill="both", expand=True)

    def setup(self):
        TelegramSetup(self, self.settings, self.refresh)

    def toggle(self):
        if self._busy:
            return
        if self.cached.get("enabled"):
            self.background(self.settings.disable, lambda _:self.refresh())
            return
        count = self.store.status().get("queued", 0)
        if not messagebox.askyesno("Включить Telegram?", "Отправлять новые и ожидающие ответы (" + str(count) + ") в привязанный личный чат?\nСообщения из этого чата также будут показываться на компьютере.", parent=self.winfo_toplevel()):
            return
        settings, store = self.settings, self.store
        def enable():
            from family_agent import ensure_running
            settings.enable()
            ensure_running(store)
        self.background(enable, lambda _:self.refresh())

    def retry(self):
        row = self.selected()
        if not row or row["direction"] != "outgoing" or row["status"] not in ("failed", "uncertain"):
            return
        if not messagebox.askyesno("Повторить отправку?", "Сначала проверь Telegram. Если прошлый запрос дошёл, повтор создаст второе сообщение. Повторить?", parent=self.winfo_toplevel()):
            return
        self.store.retry_message(row["id"])
        self.refresh()

    def delete(self):
        row = self.selected()
        if not row:
            return
        if messagebox.askyesno("Удалить локальную запись?", "Убрать запись из локальной переписки? Открытое окно сообщения и переписка в Telegram останутся. Это обычное удаление, не гарантированное стирание с диска.", parent=self.winfo_toplevel()):
            try:
                self.store.delete_message(row["id"])
                self.details.configure(state="normal")
                self.details.delete("1.0", "end")
                self.details.configure(state="disabled")
                self.refresh()
            except Exception as error:
                messagebox.showerror("Общение", safe_error(error), parent=self.winfo_toplevel())

    def disconnect(self):
        if messagebox.askyesno("Отключить Telegram?", "Остановить будущую передачу и удалить сохранённое подключение бота на этом компьютере?", parent=self.winfo_toplevel()):
            self.background(self.settings.disconnect, lambda _:self.refresh())

    def change_startup(self):
        import family_startup
        try:
            if self.startup_var.get() and not self.cached.get("enabled"):
                raise ValueError("Сначала подключи Telegram и включи доставку.")
            family_startup.set_startup_enabled(self.startup_var.get())
        except Exception:
            self.startup_var.set(False)
            messagebox.showerror("Автозапуск", "Сначала подключи Telegram и включи доставку. Изменить автозапуск сейчас не удалось.", parent=self.winfo_toplevel())


def compose_window(reply_to=None, ready_stdout=False, store=None, settings=None):
    root = tk.Tk()
    root.title("Ответ папе · Пространство")
    root.geometry("840x610")
    root.minsize(650, 480)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TButton", font=("Segoe UI", 16), padding=10)
    style.configure("TLabel", font=("Segoe UI", 12))
    store = store or MessageStore()
    settings = settings or TelegramSettings(store.root)
    composer = ReplyComposer(root, store, settings, reply_to=reply_to)
    composer.pack(fill="both", expand=True)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    composer.text.focus_force()
    def handoff_focus():
        if root.winfo_exists():
            root.lift()
            composer.text.focus_force()
    # One focus handoff after the preceding TopMost notification closes.
    composer._focus_id = composer.after(300, handoff_focus)
    if ready_stdout:
        print("FAMILY_REPLY_READY", flush=True)
    return root


def messages_window(open_setup=False, store=None, settings=None):
    root = tk.Tk()
    root.title("Пространство · Общение")
    root.geometry("1160x790")
    root.minsize(900, 650)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TButton", font=("Segoe UI", 11), padding=9)
    style.configure("TLabel", font=("Segoe UI", 11))
    style.configure("Treeview", font=("Segoe UI", 10), rowheight=29)
    style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
    panel = FamilyMessagesPanel(root, store=store, settings=settings)
    panel.pack(fill="both", expand=True)
    if open_setup:
        root.after(300, panel.setup)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", action="store_true")
    parser.add_argument("--ready-stdout", action="store_true")
    parser.add_argument("--reply-to")
    parser.add_argument("--panel", action="store_true")
    parser.add_argument("--setup", action="store_true")
    arguments = parser.parse_args()
    if arguments.reply_to and (len(arguments.reply_to) > 100 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in arguments.reply_to)):
        raise SystemExit(2)
    if arguments.panel:
        messages_window(arguments.setup).mainloop()
    else:
        compose_window(arguments.reply_to, arguments.ready_stdout).mainloop()
