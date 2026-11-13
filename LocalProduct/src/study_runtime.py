"""Resume a finite, visible study session after Windows sign-in.

The owned Run entry can remain idle after a session finishes. Its next launch
exits when there is no future lease. This avoids clearing a newer session's
startup entry during an overlapping parent command.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import math
import os
import tkinter as tk
from tkinter import ttk

from study_guard import StudyGuard
from study_resume import StudyResume
from study_ui import StudyPanel


class PersistentStudyPanel(StudyPanel):
    """Share one absolute end time between the app and the startup runtime."""

    def __init__(self, parent, settings=None, session=None, desktop=None, guard=None,
                 resume=None):
        self.resume_store = resume if resume is not None else StudyResume()
        self._lease = None
        super().__init__(parent, settings=settings, session=session, desktop=desktop, guard=guard)
        try:
            self._sync_lease()
            if self.session.active:
                super()._refresh()
                self._clear_finished_lease()
        except Exception as error:
            self._runtime_error(error)

    def _runtime_error(self, error):
        self.session.stop()
        self._hide_indicator()
        self._set_controls()
        self.notice.set('Не удалось восстановить уроки. ' + str(error)[:180])

    def _adopt(self, lease, remaining):
        if not self.guard.configured():
            raise ValueError('Для восстановления уроков требуется настроенный PIN родителя')
        value = self.settings.load()
        self._apply_settings(value)
        self._lease = lease
        self.session.start(
            duration_minutes=max(1, min(240, math.ceil(remaining / 60))),
            delay_seconds=value['delay_seconds'], preview=False,
            allowed_paths=value['allowed_paths'],
        )
        self.session.set_remaining(remaining)
        self._event_lines.clear()
        self._set_controls()
        self._show_indicator()
        self.notice.set('Режим уроков восстановлен до сохранённого времени окончания.')

    def _sync_lease(self):
        lease = self.resume_store.read()
        if lease is None:
            if self._lease is not None:
                self._lease = None
                super().force_stop()
            return
        remaining = self.resume_store.remaining_seconds(lease)
        if remaining <= 0:
            self.resume_store.clear(lease['session_id'])
            if self._lease is not None:
                self._lease = None
                super().force_stop()
                self.notice.set('Время режима истекло.')
            return
        if (self._lease is None or self._lease['session_id'] != lease['session_id']
                or not self.session.active or self.session.preview):
            self._adopt(lease, remaining)
        # The same lease keeps its existing monotonic deadline and each window's
        # grace period. Moving the wall clock backwards cannot extend this run.

    def _start(self, preview, *, duration_minutes=None):
        if preview:
            return super()._start(preview=True, duration_minutes=duration_minutes)
        if self.closed or self.session.active:
            return False
        try:
            # Another controller may have started lessons while the PIN dialog was open.
            self._sync_lease()
            if self.session.active:
                super()._refresh()
                self.notice.set('Режим уже включён.')
                return False
            value = self._save_settings(duration_minutes=duration_minutes)
            self.resume_store.enable_startup()
            lease = self.resume_store.activate(value['duration_minutes'])
            self._adopt(lease, self.resume_store.remaining_seconds(lease))
            super()._refresh()
        except Exception as error:
            # Keep a persisted lease on failure so a future startup can recover it.
            self._runtime_error(error)
            return False
        return self.session.active

    def active_study_id(self):
        lease = self.resume_store.read()
        if lease is not None and self.resume_store.remaining_seconds(lease) > 0:
            return lease['session_id']
        return None

    def complete_plan_study(self, session_id):
        """End the submitted plan's session after the caller verifies the parent PIN."""
        stopped = self.resume_store.clear(session_id)
        self._sync_lease()
        return stopped

    def force_stop(self):
        if self._lease is not None:
            # A stale controller cannot remove a parent's newer replacement lease.
            self.resume_store.clear(self._lease['session_id'])
            self._lease = None
        super().force_stop()

    def _clear_finished_lease(self):
        if self._lease is not None and not self.session.active:
            # A monotonic expiry is final even if the Windows clock moved back.
            self.resume_store.clear(self._lease['session_id'])
            self._lease = None

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        try:
            # The persisted deadline is checked before another window-close request.
            # This also adopts a parent extension when this panel is currently idle.
            self._sync_lease()
            if self.session.active:
                super()._refresh()
                self._clear_finished_lease()
        except Exception as error:
            self._runtime_error(error)
        self._schedule()


class RuntimeLock:
    """Only the dedicated startup runtime owns this current-session name."""

    def __init__(self, root):
        self.root = root
        self.handle = None
        self.existed = False

    def __enter__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        key = hashlib.sha256(os.path.normcase(str(self.root.absolute())).encode('utf-8')).hexdigest()
        self.kernel = kernel
        self.handle = kernel.CreateMutexW(None, False, 'Local\\ProstranstvoStudyRuntime-' + key)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.existed = ctypes.get_last_error() == 183
        return self

    def __exit__(self, *args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Пространство · Уроки')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--start-minutes', type=int)
    args = parser.parse_args(argv)
    store = StudyResume()
    if args.start_minutes is not None:
        if not 1 <= args.start_minutes <= 240:
            raise ValueError('Продолжительность должна быть от 1 до 240 минут')
        if not StudyGuard().configured():
            raise ValueError('Сначала задайте PIN родителя в приложении')
        store.enable_startup()
        store.activate(args.start_minutes)
    lease = store.read()
    if lease is None:
        return 0
    if store.remaining_seconds(lease) <= 0:
        store.clear(lease['session_id'])
        return 0
    # An explicit extension was persisted above. An existing runtime reads it
    # on its next Tk tick, including when its previous timer already expired.
    with RuntimeLock(store.root) as lock:
        if lock.existed:
            return 0
        root = tk.Tk()
        root.title('Пространство · Уроки')
        root.geometry('1180x820')
        root.minsize(960, 700)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TLabel', font=('Segoe UI', 11))
        style.configure('TButton', font=('Segoe UI', 11), padding=9)
        panel = PersistentStudyPanel(root, resume=store)
        panel.pack(fill='both', expand=True)
        shell = None

        def close():
            if panel.request_stop():
                root.destroy()

        root.protocol('WM_DELETE_WINDOW', close)
        try:
            try:
                from study_shell import StudyShell
                shell = StudyShell(root, panel)
                shell.install()
            except Exception as error:
                if shell is not None:
                    shell.close()
                    shell = None
                panel.notice.set('Панель остаётся открытой. Не удалось настроить значок и клавиши. '
                                 + str(error)[:180])
            if args.resume and shell is not None:
                shell.hide()
            root.mainloop()
        finally:
            if shell is not None:
                shell.close()
            # Base close cancels only this process's callbacks. An OS shutdown
            # or crash must preserve the lease for the next Windows sign-in.
            panel.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
