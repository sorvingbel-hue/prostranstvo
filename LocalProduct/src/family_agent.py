"""Normal-user Telegram message worker. No monitoring or arbitrary commands."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from family_messages import MessageStore
from family_telegram import TelegramSettings
from paths import APP_ROOT

HERE = APP_ROOT
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def desktop_available():
    """Do not show family text on a locked or secure desktop."""
    if os.name != "nt":
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    user.OpenInputDesktop.restype = ctypes.c_void_p
    user.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    user.GetUserObjectInformationW.restype = ctypes.c_int
    user.CloseDesktop.argtypes = [ctypes.c_void_p]
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        return False
    try:
        buffer = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_uint32()
        return bool(user.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed))) and buffer.value.casefold() == "default"
    finally:
        user.CloseDesktop(desktop)


def write_state(store, **values):
    target = store.safe_path("agent-state.json")
    temporary = store.safe_path("agent-state-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump({"heartbeat": utc_now(), "pid": os.getpid(), **values}, output)
            output.flush()
            os.fsync(output.fileno())
        store.safe_path(target.name)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def worker_status(store):
    try:
        data = json.loads(store.safe_path("agent-state.json").read_text(encoding="utf-8"))
        stamp = dt.datetime.fromisoformat(data["heartbeat"])
        age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        return {**data, "running": data.get("running") is True and -5 <= age < 35}
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": False, "phase": "stopped"}


class WorkerLock:
    """Held by an OS lock, not by a stale PID file."""
    def __init__(self, store):
        self.path = store.safe_path("agent.lock")
        self.file = None

    def __enter__(self):
        import msvcrt
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("family_worker_already_running") from None
        return self

    def __exit__(self, *args):
        if self.file:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


class MessagePresenter:
    def __init__(self, store, executable=None):
        self.store = store
        self.executable = executable or HERE / "PashaMessage.exe"
        self.process = None
        self.current_id = None
        self.next_attempt = 0

    def tick(self):
        if self.process:
            exit_code = self.process.poll()
            if exit_code is None:
                return
            self.process = None
            self.current_id = None
            if exit_code:
                raise RuntimeError("message_window_failed")
        if time.monotonic() < self.next_attempt or not desktop_available():
            return
        incoming = self.store.pending_incoming(limit=1)
        if not incoming:
            return
        if not self.executable.is_file() or self.executable.is_symlink():
            raise RuntimeError("message_window_missing")
        message = incoming[0]
        ident = message["id"]
        if not ID_PATTERN.fullmatch(ident):
            raise RuntimeError("invalid_message_id")
        self.next_attempt = time.monotonic() + 15
        process = None
        try:
            process = subprocess.Popen(
                [str(self.executable), "--stdin", "--message-id", ident],
                cwd=self.executable.parent, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
            )
            process.stdin.write(message["text"])
            process.stdin.close()
        except Exception:
            if process:
                try:
                    if process.poll() is None:
                        process.kill()
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
            raise RuntimeError("message_window_start_failed") from None
        self.process, self.current_id = process, ident
        threading.Thread(target=self._receipts, args=(process, ident), daemon=True).start()

    def _receipts(self, process, ident):
        # The native application emits only a fixed marker and opaque message ID.
        try:
            while True:
                line = process.stdout.readline(200)
                if not line:
                    break
                if line.strip() == "FAMILY_SHOWN " + ident:
                    self.store.mark_incoming_shown(ident)
        except Exception:
            pass
        finally:
            if process.stdout:
                process.stdout.close()

    def close(self):
        # No plaintext spool file exists. Visible messages stay user-controlled.
        pass


def run(store=None, settings=None, stop=None, presenter=None):
    store = store or MessageStore()
    settings = settings or TelegramSettings(store.root)
    stop = stop or threading.Event()
    presenter = presenter or MessagePresenter(store)
    with WorkerLock(store):
        store.recover_sending()
        from board_remote import BoardRemote
        board = None
        try:
            board = BoardRemote(store, settings)
            board.recover()
        except Exception:
            board = None
        write_state(store, running=True, phase="starting", errors=[])
        try:
            while not stop.is_set():
                status = settings.status()
                if not status.get("enabled"):
                    break
                errors = []
                try:
                    settings.process_outbox(store)
                except Exception:
                    errors.append("outgoing_unavailable")
                try:
                    settings.poll_incoming(store, command_handler=board)
                except Exception:
                    errors.append("incoming_unavailable")
                try:
                    if board is None:
                        errors.append('board_unavailable')
                    else:
                        board.tick()
                except Exception:
                    errors.append("board_unavailable")
                try:
                    presenter.tick()
                except Exception:
                    errors.append("window_unavailable")
                write_state(store, running=True, phase="attention" if errors else "ready", errors=errors)
                stop.wait(3)
        finally:
            presenter.close()
            write_state(store, running=False, phase="stopped", errors=[])


def ensure_running(store, timeout=12):
    if worker_status(store).get("running"):
        return
    target = HERE / "FamilyMessageAgent.exe"
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("Модуль общения ещё не установлен.")
    subprocess.Popen([str(target)], cwd=HERE, creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker_status(store).get("running"):
            return
        time.sleep(.15)
    raise RuntimeError("Модуль общения не ответил. Проверь подключение Telegram.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    parser.parse_args()
    try:
        run()
    except RuntimeError:
        sys.exit(1)
