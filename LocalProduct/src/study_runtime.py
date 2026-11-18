"""Resume a visible study session after Windows sign-in.

The owned Run entry can remain idle after a session finishes. Its next launch
exits when there is no future lease. This avoids clearing a newer session's
startup entry during an overlapping parent command.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import tempfile
import time
import tkinter as tk
from tkinter import ttk

from activity_core import no_links
from codex_pause import CodexPause
from parent_apps import ParentApps, ParentAppsDesktop
from study_core import validate_options
from study_guard import StudyGuard
from study_resume import StudyResume
from study_ui import StudyPanel
from study_windows import PLAN_WINDOW_TITLE


class PersistentStudyPanel(StudyPanel):
    """Share a saved session between the app and the startup runtime."""

    def __init__(self, parent, settings=None, session=None, desktop=None, guard=None,
                 resume=None):
        self.resume_store = resume if resume is not None else StudyResume()
        self._lease = None
        self._plan_popup = None
        self._health_path = None
        self._next_health_write = 0
        self._codex_minimized = 0
        self._codex_pause_enabled = False
        self._parent_apps_count = 0
        self._parent_apps_minimized = 0
        self._parent_apps_error = False
        self._parent_apps_desktop = None
        self._runtime_state = 'inactive'
        self._runtime_error_type = None
        self._settings_source = 'primary'
        self._last_valid_settings = None
        super().__init__(parent, settings=settings, session=session, desktop=desktop, guard=guard)
        self.runtime_status = tk.StringVar()
        ttk.Label(self, textvariable=self.runtime_status, wraplength=1040,
                  justify='left').grid(row=10, column=0, sticky='ew', pady=(8, 0))
        self._update_parent_status()
        self.bind_codex_pause(CodexPause(self.resume_store.root))
        self.bind_parent_apps(ParentApps(self.resume_store.root, desktop=self.session.desktop))
        try:
            self._sync_lease()
            if self.session.active:
                self._refresh()
                self._clear_finished_lease()
        except Exception as error:
            self._runtime_error(error)

    def _runtime_error(self, error):
        self.session.stop()
        self._hide_indicator()
        self._set_controls()
        if self._lease is not None:
            self.stop_button.configure(state='normal')
        self._runtime_state = 'recovery_failed'
        self._runtime_error_type = type(error).__name__
        self._update_parent_status()
        self.notice.set('Не удалось восстановить уроки. ' + str(error)[:180])

    def _apply_settings(self, value):
        if not isinstance(value, dict) or set(value) != {'duration_minutes', 'delay_seconds', 'allowed_paths'}:
            raise ValueError('Неверные настройки режима уроков')
        value = validate_options(**value)
        super()._apply_settings(value)
        self._last_valid_settings = {**value, 'allowed_paths': list(value['allowed_paths'])}

    def _recovery_settings(self):
        try:
            value = self.settings.load()
            self._apply_settings(value)
            self._settings_source = 'primary'
        except (OSError, ValueError):
            if self._last_valid_settings is not None:
                value = {**self._last_valid_settings,
                         'allowed_paths': list(self._last_valid_settings['allowed_paths'])}
                self._settings_source = 'last_valid'
            else:
                value = validate_options(30, 10, [])
                self._settings_source = 'defaults'
            # Recovery must never overwrite damaged preferences or a valid lease.
            # The lease, rather than these UI defaults, supplies the deadline.
            super()._apply_settings(value)
        return value

    def _update_parent_status(self):
        messages = {
            'inactive': 'Режим не включён. Ограничения не действуют.',
            'active': 'Активен · ограничения работают.',
            'restored': 'Восстановлен · ограничения работают. Сохранённый срок не изменён.',
            'completed': 'Завершён · ограничения отключены.',
            'preview': 'Пробная проверка · окна не закрываются.',
            'recovery_failed': ('Не восстановился · ограничения не работают. '
                                'Сеанс сохранён; повторная проверка выполняется автоматически.'),
        }
        text = messages[self._runtime_state]
        if self.session.active and not self.session.preview:
            if self._settings_source == 'defaults':
                text += (' Настройки недоступны: используются запасные параметры — '
                         'задержка 10 секунд, без добавленных исключений.')
            elif self._settings_source == 'last_valid':
                text += ' Настройки недоступны: используются последние проверенные параметры.'
        self.runtime_status.set(text)
        if self._runtime_state == 'recovery_failed':
            self.status.set('Не восстановился')
        elif self._runtime_state == 'completed':
            self.status.set('Завершён')

    def _refresh(self):
        # The snapshot must filter and minimize parent apps before tick() can
        # issue any ordinary window-close request, including the first restore.
        self._reconcile_parent_apps()
        super()._refresh()
        if self.session.active and self.session.preview:
            self._runtime_state = 'preview'
        elif not self.session.active and self._runtime_state in ('active', 'restored', 'preview'):
            self._runtime_state = 'completed'
        self._update_parent_status()

    def bind_parent_apps(self, policy):
        super().bind_parent_apps(policy)
        previous = self._parent_apps_desktop
        desktop = previous._desktop if previous is not None else self.session.desktop
        while isinstance(desktop, ParentAppsDesktop):
            desktop = desktop._desktop
        self._parent_apps_desktop = ParentAppsDesktop(desktop, policy, active=self._parent_apps_active)
        self.session.desktop = self._parent_apps_desktop

    def _parent_apps_active(self):
        return bool(self.session.active and not self.session.preview and self.session.remaining_seconds > 0)

    def _reconcile_parent_apps(self):
        proxy = self._parent_apps_desktop
        if proxy is None:
            return
        self._parent_apps_error = False
        try:
            proxy.refresh_policy()
        except (OSError, RuntimeError, ValueError) as error:
            self._parent_apps_error = True
            self.notice.set('Не удалось перечитать приложения родителя. '
                            'Сохранён последний проверенный список. ' + str(error)[:140])
        self._parent_apps_count = proxy.selected_count
        try:
            result = proxy.reconcile(active=self._parent_apps_active())
            self._parent_apps_minimized += result['minimized']
            self._parent_apps_error = self._parent_apps_error or bool(result['errors'])
        except (OSError, RuntimeError, ValueError) as error:
            self._parent_apps_error = True
            self.notice.set('Не удалось свернуть приложения родителя. ' + str(error)[:180])

    def _adopt(self, lease, remaining):
        self._lease = lease
        if not self.guard.configured():
            raise ValueError('Для восстановления уроков требуется настроенный PIN родителя')
        value = self._recovery_settings()
        untimed = remaining == math.inf
        self.session.start(
            duration_minutes=0 if untimed else max(1, min(240, math.ceil(remaining / 60))),
            delay_seconds=value['delay_seconds'], preview=False,
            allowed_paths=value['allowed_paths'],
        )
        if not untimed:
            self.session.set_remaining(remaining)
        self._event_lines.clear()
        self._set_controls()
        self._show_indicator()
        self._runtime_state = 'restored'
        self._runtime_error_type = None
        self._update_parent_status()
        self.notice.set('Уроки включены без таймера.' if untimed else
                        'Режим уроков восстановлен до сохранённого времени окончания.')

    def _sync_lease(self):
        lease = self.resume_store.read()
        if lease is None:
            if self._lease is not None:
                self._lease = None
                super().force_stop()
                self._runtime_state = 'completed'
            elif not self.session.active and self._runtime_state == 'recovery_failed':
                self._runtime_state = 'inactive'
            self._update_parent_status()
            return
        remaining = self.resume_store.remaining_seconds(lease)
        if remaining <= 0:
            self.resume_store.clear(lease['session_id'])
            if self._lease is not None:
                self._lease = None
                super().force_stop()
            self._runtime_state = 'completed'
            self._update_parent_status()
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
                self._refresh()
                self.notice.set('Режим уже включён.')
                return False
            value = self._save_settings(duration_minutes=duration_minutes)
            self.resume_store.enable_startup()
            lease = self.resume_store.activate(value['duration_minutes'])
            self._adopt(lease, self.resume_store.remaining_seconds(lease))
            self._refresh()
            if self.session.active:
                self._runtime_state = 'active'
                self._update_parent_status()
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

    def enable_plan_popup(self, *, store=None, living_society_target=None):
        from daily_plan import DailyPlan
        self._plan_popup = StudyPlanPopup(
            self, DailyPlan() if store is None else store, living_society_target,
        )

    def enable_health_status(self):
        self._health_path = self.resume_store.root / 'runtime-status.json'

    def _write_health_status(self, error):
        if self._health_path is None:
            return
        now = time.monotonic()
        if error is None and now < self._next_health_write:
            return
        pending = self.session.pending
        value = {
            'version': 1, 'pid': os.getpid(), 'heartbeat_utc': time.time(),
            'active': self.session.active, 'preview': self.session.preview,
            'session_id': self._lease['session_id'] if self._lease else None,
            'expires_utc': self._lease['expires_utc'] if self._lease else None,
            'observed_windows': len(pending),
            'close_requests': sum(item['result'] == 'requested' for item in pending),
            'error_type': (type(error).__name__ if error else
                           self._runtime_error_type if self._runtime_state == 'recovery_failed' else None),
            'codex_pause_enabled': self._codex_pause_enabled,
            'codex_windows_minimized': self._codex_minimized,
            'parent_apps_selected_count': self._parent_apps_count,
            'parent_apps_windows_minimized': self._parent_apps_minimized,
            'parent_apps_error': self._parent_apps_error,
            'state': self._runtime_state,
            'settings_source': self._settings_source,
            'settings_fallback': self._settings_source != 'primary',
            'enforcement_active': (self.session.active and not self.session.preview
                                   and self._runtime_state in ('active', 'restored')),
            'valid_for_seconds': 5,
        }
        no_links(self._health_path, self._health_path.exists())
        descriptor, temporary = tempfile.mkstemp(
            prefix='.study-health-', suffix='.tmp', dir=self._health_path.parent,
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(self._health_path, self._health_path.exists())
            os.replace(temporary, self._health_path)
            self._next_health_write = now + 2
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self):
        if self._plan_popup is not None:
            self._plan_popup.close()
        super().close()

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
        self._runtime_state = 'completed'
        self._update_parent_status()

    def request_stop(self):
        if self._lease is not None and not self.session.active:
            if not self.authorize_parent('Завершить сохранённые уроки'):
                return False
        return super().request_stop()

    def _clear_finished_lease(self):
        if self._lease is not None and not self.session.active:
            # A monotonic expiry is final even if the Windows clock moved back.
            self.resume_store.clear(self._lease['session_id'])
            self._lease = None
            self._runtime_state = 'completed'
            self._update_parent_status()

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        error = None
        try:
            # The persisted deadline is checked before another window-close request.
            # This also adopts a parent extension when this panel is currently idle.
            self._sync_lease()
            if self.session.active:
                self._refresh()
                self._clear_finished_lease()
            try:
                paused = self._codex_pause_policy.reconcile(
                    active=self.session.active and not self.session.preview,
                )
                self._codex_pause_enabled = paused['enabled']
                self._codex_minimized += paused['minimized']
            except (OSError, RuntimeError, ValueError) as caught:
                self.notice.set('Не удалось свернуть Codex. ' + str(caught)[:180])
            if self._plan_popup is not None and self.session.active and not self.session.preview and self._lease:
                self._plan_popup.show_for(self._lease['session_id'])
        except Exception as caught:
            error = caught
            self._runtime_error(caught)
        finally:
            try:
                self._write_health_status(error)
            except (OSError, ValueError):
                pass
            finally:
                self._schedule()


class RuntimeLock:
    """One controller can own a named function per Windows sign-in and data root."""

    def __init__(self, root, name='StudyRuntime'):
        self.root = root
        self.name = name
        self.handle = None
        self.existed = False

    def __enter__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        key = hashlib.sha256(os.path.normcase(str(self.root.absolute())).encode('utf-8')).hexdigest()
        self.kernel = kernel
        ctypes.set_last_error(0)
        self.handle = kernel.CreateMutexW(None, False, 'Local\\Prostranstvo' + self.name + '-' + key)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.existed = ctypes.get_last_error() == 183
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class StudyPlanPopup:
    """Show today's saved plan once per study session across desktop controllers."""

    def __init__(self, study, store, living_society_target):
        self.study = study
        self.store = store
        self.living_society_target = living_society_target
        self.last_session = None
        self.lock = None
        self.window = None
        self.raise_timer = None

    def show_for(self, session_id):
        if session_id == self.last_session:
            return
        try:
            if self.store.read() is None:
                self.last_session = session_id
                return
            if self.lock is None:
                lock = RuntimeLock(self.study.resume_store.root, 'StudyPlanWindow')
                lock.__enter__()
                if lock.existed:
                    lock.close()
                    self.last_session = session_id
                    return
                self.lock = lock
            self.last_session = session_id
            self._dismiss()
            self._open()
        except (OSError, RuntimeError, ValueError, tk.TclError) as error:
            self.last_session = session_id
            self.close()
            self.study.notice.set('Не удалось открыть план на сегодня. ' + str(error)[:180])

    def _open(self):
        from daily_plan_ui import DailyPlanPanel
        root = self.study.winfo_toplevel()
        window = self.window = tk.Toplevel(root)
        window.withdraw()
        window.title(PLAN_WINDOW_TITLE)
        width = min(1100, window.winfo_screenwidth())
        height = min(760, window.winfo_screenheight())
        window.geometry(f'{width}x{height}')
        window.protocol('WM_DELETE_WINDOW', self._dismiss)
        plan = DailyPlanPanel(
            window, store=self.store, guard=self.study.guard, study=self.study,
            living_society_target=self.living_society_target,
        )
        plan.pack(fill='both', expand=True)
        window.attributes('-topmost', True)
        window.deiconify()
        window.lift()
        self.raise_timer = window.after(1500, self._restore_z_order)

    def _restore_z_order(self):
        self.raise_timer = None
        self.window.attributes('-topmost', False)

    def _dismiss(self):
        if self.window is not None:
            window = self.window
            self.window = None
            if window.winfo_exists():
                if self.raise_timer is not None:
                    window.after_cancel(self.raise_timer)
                window.destroy()
            self.raise_timer = None

    def close(self):
        try:
            self._dismiss()
        finally:
            if self.lock is not None:
                self.lock.close()
                self.lock = None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Пространство · Уроки')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--start-minutes', type=int)
    mode.add_argument('--start-indefinitely', action='store_true')
    args = parser.parse_args(argv)
    store = StudyResume()
    if not args.resume:
        minutes = 0 if args.start_indefinitely else args.start_minutes
        if not args.start_indefinitely and not 1 <= minutes <= 240:
            raise ValueError('Продолжительность должна быть от 1 до 240 минут')
        if not StudyGuard().configured():
            raise ValueError('Сначала задайте PIN родителя в приложении')
        store.enable_startup()
        store.activate(minutes)
    try:
        lease = store.read()
        if lease is None:
            return 0
        if store.remaining_seconds(lease) <= 0:
            store.clear(lease['session_id'])
            return 0
    except (OSError, RuntimeError, ValueError):
        # Open a visible recovery-failure panel instead of silently exiting
        # pythonw on an unreadable lease. The panel retries without deleting it.
        pass
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
        panel.enable_health_status()
        panel.enable_plan_popup()
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
            if (args.resume and shell is not None and panel.session.active
                    and panel._runtime_state in ('active', 'restored')
                    and panel._settings_source == 'primary'):
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
