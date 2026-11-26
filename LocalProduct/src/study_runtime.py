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
from board_approval import BoardApproval, launch_resume_worker
from play_session import PlaySession


RUNTIME_CONTRACT_VERSION = 2
RUNTIME_FEATURES = ('hourly_status_v1', 'deferred_plan_dismiss_v1')


class PersistentStudyPanel(StudyPanel):
    """Share a saved session between the app and the startup runtime."""

    def __init__(self, parent, settings=None, session=None, desktop=None, guard=None,
                 resume=None, ensure_play_running=launch_resume_worker):
        self.resume_store = resume if resume is not None else StudyResume()
        self.supports_game_timer = callable(getattr(self.resume_store, 'activate_if_empty', None))
        self._ensure_play_running = ensure_play_running
        self.play_store = (PlaySession(self.resume_store.root.absolute().parent / 'play-time',
            clock=self.resume_store.clock,
            monotonic=session.clock if session is not None else time.monotonic)
            if self.supports_game_timer else None)
        self._board_approval = (BoardApproval(study=self.resume_store, play=self.play_store,
            ensure_running=ensure_play_running) if self.supports_game_timer else None)
        self._play_view = None
        self._play_error_type = None
        self._hourly_view = None
        self._hourly_error_type = None
        self._keeper_last_poll_utc = None
        self._keeper_last_poll_ok = None
        self._keeper_last_error_type = None
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
        play = self.__dict__.get('_play_view')
        if (not self.__dict__.get('_play_error_type') and self._runtime_state != 'recovery_failed'
                and not self.session.active and play and play['current']
                and play['current']['phase'] == 'playing'):
            seconds = play['current']['remaining_seconds']
            text = f'Игровое время: {seconds // 60:02d}:{seconds % 60:02d}. По окончании вернутся ограничения.'
        hourly = self.__dict__.get('_hourly_view')
        if self.__dict__.get('_hourly_error_type'):
            text += (' Часовой цикл недоступен: автоматическое продолжение игры не подтверждено. '
                     'Действующие уроки проверяются отдельно.')
        elif hourly and hourly['enabled']:
            phase = hourly['phase']
            if phase == 'waiting':
                seconds = hourly['remaining_break_seconds']
                text += (f' Полезное дело: осталось {seconds // 60:02d}:{seconds % 60:02d}. '
                         'После перерыва нужны отметка ребёнка и подтверждение родителя.'
                         if seconds else ' Перерыв завершён. Нужны отметка ребёнка и подтверждение родителя.')
            elif phase == 'submitted':
                text += ' Дело отмечено ребёнком. Следующий игровой час ожидает подтверждения родителя.'
            elif phase == 'approved':
                text += ' Дело подтверждено родителем. Передача следующего игрового часа ещё не завершена.'
            elif phase == 'blocked':
                text += ' Часовой цикл приостановлен. Новое игровое время не выдано.'
        if self.__dict__.get('_play_error_type'):
            text += ' Игровой таймер недоступен; его прежний статус не считается действующим.'
        if self.__dict__.get('_keeper_last_poll_ok') is False:
            text += ' Последняя проверка контроллера завершилась ошибкой.'
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
        approval = self.__dict__.get('_board_approval')
        if approval is not None:
            try:
                self._play_view = approval.pump()
                self._play_error_type = None
            except (OSError, RuntimeError, ValueError) as error:
                # A previously cached "playing" view must not conceal a failed
                # timer or a still-active Study lease on the parent screen.
                self._play_view = None
                self._play_error_type = type(error).__name__
                self.notice.set('Игровой таймер недоступен. Проверка действующих уроков продолжается.')
            try:
                self._hourly_view = approval.breaks.status()
                self._hourly_error_type = None
            except (OSError, RuntimeError, ValueError) as error:
                self._hourly_view = None
                self._hourly_error_type = type(error).__name__
        lease = self.resume_store.read()
        if lease is None:
            if self._lease is not None:
                self._lease = None
                super().force_stop()
                self._runtime_state = 'completed'
            elif not self.session.active and self._runtime_state == 'recovery_failed':
                self._runtime_state = 'inactive'
            self._dismiss_plan_after_idle()
            self._update_parent_status()
            return
        remaining = self.resume_store.remaining_seconds(lease)
        if remaining <= 0:
            self.resume_store.clear(lease['session_id'])
            if self._lease is not None:
                self._lease = None
                super().force_stop()
            self._runtime_state = 'completed'
            self._dismiss_plan_after_idle()
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
            if self._board_approval is not None:
                lease = self._board_approval.start_parent_study(value['duration_minutes'])
            else:
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

    def _dismiss_plan_after_idle(self):
        popup = self.__dict__.get('_plan_popup')
        if popup is not None:
            popup.dismiss_when_inactive()

    def _write_health_status(self, error):
        if self._health_path is None:
            return
        now = time.monotonic()
        if error is None and now < self._next_health_write:
            return
        pending = self.session.pending
        value = {
            'version': 1, 'pid': os.getpid(), 'heartbeat_utc': time.time(),
            # These are loaded-code markers. Hashing the current source file
            # would falsely identify an old process after an on-disk update.
            'runtime_contract_version': RUNTIME_CONTRACT_VERSION,
            'runtime_features': list(RUNTIME_FEATURES),
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
            'keeper': {'state': ('running' if self.__dict__.get('_keeper_last_poll_ok') is True
                                 else 'error' if self.__dict__.get('_keeper_last_poll_ok') is False
                                 else 'not_polled'),
                       'last_poll_utc': self.__dict__.get('_keeper_last_poll_utc'),
                       'last_error_type': self.__dict__.get('_keeper_last_error_type')},
            'enforcement_observation': 'controller_state_only',
            'game_timer_error': self.__dict__.get('_play_error_type'),
            'game_timer': ({key:self._play_view['current'][key] for key in
                ('phase', 'remaining_seconds', 'expires_utc')}
                if self.__dict__.get('_play_view') and self._play_view['current'] else None),
            'hourly_cycle_error': self.__dict__.get('_hourly_error_type'),
            'hourly_cycle': ({key:self._hourly_view[key] for key in
                ('enabled', 'phase', 'remaining_break_seconds', 'can_request_done', 'can_approve')}
                if self.__dict__.get('_hourly_view') is not None else None),
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

    def approve_board_plan(self, store, plan, *, quality_percent=100, comment='', request_id=None):
        """Caller checked the parent PIN and the still-visible board revision."""
        prior = plan.get('approval')
        self.play_store.today = store.today
        service = BoardApproval(plans=store, study=self.resume_store, play=self.play_store,
            ensure_running=self._ensure_play_running)
        value = service.approve(plan_date=plan['date'],
            revision=prior['submitted_revision'] if prior else plan['revision'],
            study_id=plan.get('study_session_id'),
            request_id=prior['request_id'] if prior else request_id,
            quality_percent=prior['quality_percent'] if prior else quality_percent,
            comment=plan['feedback'] if prior else comment)
        self._sync_lease()
        return value

    def board_play_status(self):
        return self.play_store.status() if self.play_store is not None else {'current':None,'requires_startup':False,'action':'none'}

    def hourly_cycle_control(self):
        from hourly_cycle import HourlyCycleControl
        return HourlyCycleControl(self._board_approval)

    def force_stop(self):
        approval = self.__dict__.get('_board_approval')
        if approval is not None:
            approval.cancel()
        if self._lease is not None:
            # A stale controller cannot remove a parent's newer replacement lease.
            self.resume_store.clear(self._lease['session_id'])
            self._lease = None
        super().force_stop()
        self._runtime_state = 'completed'
        self._dismiss_plan_after_idle()
        self._update_parent_status()

    def request_stop(self):
        try:
            self._sync_lease()
        except (OSError, RuntimeError, ValueError) as error:
            self._runtime_error(error)
            return False
        if self.__dict__.get('_play_error_type') is not None:
            self.notice.set('Игровой таймер повреждён или недоступен. Действующие уроки сохранены; сначала восстановите состояние таймера.')
            return False
        if self.__dict__.get('_board_approval') is not None and self.play_store.status()['requires_startup']:
            if not self.authorize_parent('Отменить игровой таймер и его возврат к урокам'):
                return False
            self.force_stop()
            return True
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
            self._dismiss_plan_after_idle()
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
            self._keeper_last_poll_utc = time.time()
            self._keeper_last_poll_ok = error is None
            self._keeper_last_error_type = type(error).__name__ if error else None
            self._update_parent_status()
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
        self.dismiss_timer = None

    def dismiss_when_inactive(self):
        """Finish the current approval callback before destroying its child panel."""
        if self.window is None or self.dismiss_timer is not None:
            return
        window = self.window

        def dismiss_if_still_inactive():
            if self.window is not window:
                return
            self.dismiss_timer = None
            try:
                # A replacement Study may have appeared since scheduling. Its
                # checklist must survive even when the old approval succeeded.
                if self.study.active_study_id() is None:
                    self._dismiss()
            except (OSError, RuntimeError, ValueError, tk.TclError):
                # The normal runtime retry reports the unreadable lease. An
                # optional window must not stop Study or delete its saved state.
                return

        try:
            self.dismiss_timer = window.after_idle(dismiss_if_still_inactive)
        except tk.TclError:
            self.dismiss_timer = None

    def show_for(self, session_id):
        if session_id == self.last_session:
            return
        try:
            # Materialize an explicitly enabled daily template before deciding
            # that a new study session has no checklist for this date.
            self.store.ensure_today()
            cycle_control = (self.study.hourly_cycle_control()
                if callable(getattr(type(self.study), 'hourly_cycle_control', None))
                and self.study.supports_game_timer is True else None)
            cycle = cycle_control.status() if cycle_control is not None else None
            pending_break = (cycle is not None and cycle['enabled']
                and cycle['phase'] in ('waiting','submitted','approved','blocked'))
            if self.store.read() is None and not pending_break:
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
        if self.window is not None:
            self.window.attributes('-topmost', False)

    def _dismiss(self):
        if self.window is not None:
            window = self.window
            self.window = None
            if window.winfo_exists():
                if self.dismiss_timer is not None:
                    window.after_cancel(self.dismiss_timer)
                if self.raise_timer is not None:
                    window.after_cancel(self.raise_timer)
                window.destroy()
            self.raise_timer = None
            self.dismiss_timer = None

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
        if callable(getattr(type(store), 'activate_if_empty', None)):
            BoardApproval(study=store).start_parent_study(minutes)
        else:
            store.enable_startup()
            store.activate(minutes)
    try:
        lease = store.read()
        if lease is None:
            if not PlaySession(store.root.parent / 'play-time').status()['requires_startup']:
                return 0
        elif store.remaining_seconds(lease) <= 0:
            store.clear(lease['session_id'])
            if not PlaySession(store.root.parent / 'play-time').status()['requires_startup']:
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
            if (args.resume and shell is not None and (
                    (panel.session.active and panel._runtime_state in ('active', 'restored')
                     and panel._settings_source == 'primary')
                    or (not panel.session.active and panel.play_store.status()['requires_startup']
                        and panel._runtime_state != 'recovery_failed'))):
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
