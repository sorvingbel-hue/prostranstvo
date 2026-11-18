"""Explicit parent-app policy for ordinary repeated minimization during Study.

An empty/missing policy is inert. The caller must authenticate the parent before
``configure`` and must call ``reconcile`` while Study is active. This module does
not start a watcher, hide windows/processes, close programs or copy browser data.
Minimization is not an OS access boundary: a restored window may appear until the
next reconciliation. A separate headless browser profile remains preferable.
"""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import tempfile

from activity_core import exclusive_lock, no_links
from application_launcher import _candidates as desktop_application_paths
from browser_access import known_application_paths
from codex_pause import CodexPause
from paths import DEPLOY_ROOT


APP_IDS = ("edge", "chrome", "yandex", "vscode", "notion")
_SETTINGS_ERROR = "Повреждены настройки родительских приложений"


def _validate_ids(value):
    if (type(value) is not list or len(value) > len(APP_IDS)
            or any(type(app_id) is not str or app_id not in APP_IDS for app_id in value)
            or len(set(value)) != len(value)):
        raise ValueError("Выберите приложения из родительского каталога")
    return list(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_SETTINGS_ERROR)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(_SETTINGS_ERROR)


def _executable_key(value):
    if (not isinstance(value, str) or not ntpath.isabs(value)
            or any(ord(character) < 32 for character in value)):
        return None
    return ntpath.normcase(ntpath.normpath(value))


class _ParentWindowMinimizer(CodexPause):
    """Reuse the existing per-session/double-identity Win32 implementation."""

    def __init__(self, root, *, desktop=None):
        super().__init__(root, desktop=desktop)
        self.approved_paths = frozenset()

    def enabled(self):
        return bool(self.approved_paths)

    def _codex_executable(self, executable):
        key = _executable_key(executable)
        return key is not None and key in self.approved_paths

    def _window(self, handle):
        window = super()._window(handle)
        if window is None or window.class_name != "Chrome_WidgetWin_1":
            return None
        return window


class ParentApps:
    def __init__(self, root=None, *, desktop=None):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / "data" / "study"
        self.path = self.root / "parent-apps.json"
        self._minimizer = _ParentWindowMinimizer(self.root, desktop=desktop)

    def read(self):
        """Read version 1 policy; corruption raises rather than enabling apps."""
        try:
            no_links(self.path, True)
            if self.path.stat().st_size > 4096:
                raise ValueError(_SETTINGS_ERROR)
            value = json.loads(self.path.read_text(encoding="utf-8-sig"),
                               object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except FileNotFoundError:
            return {"version": 1, "app_ids": []}
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(_SETTINGS_ERROR) from error
        if (type(value) is not dict or set(value) != {"version", "app_ids"}
                or type(value["version"]) is not int or value["version"] != 1):
            raise ValueError(_SETTINGS_ERROR)
        try:
            app_ids = _validate_ids(value["app_ids"])
        except ValueError as error:
            raise ValueError(_SETTINGS_ERROR) from error
        return {"version": 1, "app_ids": app_ids}

    def configure(self, app_ids):
        """Persist an explicit list. Parent PIN authorization belongs to caller."""
        value = {"version": 1, "app_ids": _validate_ids(app_ids)}
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / "parent-apps.lock"):
            no_links(self.path, self.path.exists())
            descriptor, temporary = tempfile.mkstemp(prefix=".parent-apps-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                no_links(self.path, self.path.exists())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return value

    def app_ids(self):
        return self.read()["app_ids"]

    @staticmethod
    def _approved_for_ids(app_ids):
        if not app_ids:
            return ()
        candidates = {app_id: tuple(paths) for app_id, paths in known_application_paths().items()}
        desktop_candidates = desktop_application_paths(os.environ)
        # Use the exact same fixed installation candidates as both launchers.
        for app_id in ("vscode", "notion"):
            candidates[app_id] = tuple(candidates.get(app_id, ())) + tuple(desktop_candidates[app_id][1])
        approved = set()
        for app_id in app_ids:
            for candidate in candidates.get(app_id, ()):
                try:
                    path = no_links(Path(candidate))
                    key = _executable_key(str(path))
                    if key is not None and path.is_file():
                        approved.add(key)
                except (OSError, ValueError):
                    continue
        return tuple(sorted(approved))

    def approved_executables(self):
        """Normalized full paths only; no basename or caller-path exceptions."""
        return self._approved_for_ids(self.app_ids())

    def snapshot(self):
        """One validated policy read for both filtering and minimization."""
        app_ids = self.app_ids()
        return {"app_ids": app_ids, "executables": self._approved_for_ids(app_ids)}

    def protects(self, executable):
        key = _executable_key(executable)
        return key is not None and key in self.approved_executables()

    def reconcile(self, active):
        if type(active) is not bool:
            raise ValueError("Выберите состояние режима уроков")
        app_ids = self.app_ids()
        approved = self._approved_for_ids(app_ids) if active and app_ids else ()
        return self._reconcile_approved(active, app_ids, approved)

    def _reconcile_approved(self, active, app_ids, approved):
        result = {"enabled": bool(app_ids), "minimized": 0, "errors": 0}
        if not active or not app_ids:
            return result
        self._minimizer.approved_paths = frozenset(approved)
        if not self._minimizer.approved_paths:
            return result
        return self._minimizer.reconcile(True)


class ParentAppsDesktop:
    """Filter exact selected executables only during active Study.

    A failed policy reload retains the last validated snapshot so an interrupted
    settings write cannot turn a preserved parent task into a close target.
    Actual desktop errors are deliberately delegated to StudySession unchanged.
    """

    def __init__(self, desktop, policy, *, active):
        if not callable(active):
            raise TypeError("active must be callable")
        self._desktop = desktop
        self._policy = policy
        self._active = active
        self._app_ids = ()
        self._approved = frozenset()

    def __getattr__(self, name):
        return getattr(self._desktop, name)

    @property
    def selected_count(self):
        return len(self._app_ids)

    def refresh_policy(self):
        snapshot = self._policy.snapshot()
        # Only ParentApps supplies this snapshot, after its strict policy and
        # fixed installation-path validation; never accept tool/request paths.
        self._app_ids = tuple(snapshot["app_ids"])
        self._approved = frozenset(snapshot["executables"])

    def protects(self, executable):
        key = _executable_key(executable)
        return self._active() is True and key is not None and key in self._approved

    def list_windows(self):
        windows = self._desktop.list_windows()
        return [window for window in windows if not self.protects(window.executable)]

    def close_window(self, window):
        if self.protects(window.executable):
            return "parent_app_preserved"
        return self._desktop.close_window(window)

    def terminate_roblox(self, window):
        if self.protects(window.executable):
            return "parent_app_preserved"
        terminate = getattr(self._desktop, "terminate_roblox", None)
        return terminate(window) if callable(terminate) else "unavailable"

    def reconcile(self, active):
        return self._policy._reconcile_approved(active, self._app_ids, self._approved)
