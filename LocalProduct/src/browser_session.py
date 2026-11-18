"""Owned Edge profile lease and explicit, manual Facebook login handoff.

Preparation and marker reads are inert. The gateway must hold profile_lease()
until its owned browser context has closed. A manual marker survives the visible
launcher and blocks headless acquisition until the owner explicitly finishes.
No credentials, cookies, Chromium lock files or existing browser processes are
read, copied, removed or controlled here.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links
from browser_access import known_application_paths
from study_resume import StudyResume


LOGIN_URL = "https://www.facebook.com/"
_MARKER_ERROR = "invalid_manual_login_marker"


def _browser_root():
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if not local.is_absolute() or str(local.drive).startswith("\\\\"):
        raise ValueError("invalid_browser_local_data_root")
    return no_links(local / "ProstranstvoAgentBrowser")


def owned_profile_path():
    """Return the one app-owned user-data directory without creating it."""
    return no_links(_browser_root() / "profiles" / "edge")


def _marker_path():
    return _browser_root() / "manual-login.json"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_MARKER_ERROR)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(_MARKER_ERROR)


def _valid_timestamp(value):
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_marker(value):
    if (type(value) is not dict or set(value) != {"version", "state", "session_id", "created_utc"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["state"]) is not str or value["state"] not in ("manual_login", "failed")
            or type(value["session_id"]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", value["session_id"]) is None
            or not _valid_timestamp(value["created_utc"])):
        raise ValueError(_MARKER_ERROR)
    return dict(value)


def _read_marker():
    path = _marker_path()
    try:
        no_links(path, True)
        if path.stat().st_size > 2048:
            raise ValueError(_MARKER_ERROR)
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except FileNotFoundError:
        return None
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(_MARKER_ERROR) from error
    return _validate_marker(value)


def manual_login_pending():
    """Read-only; corrupt or linked state raises so callers must fail closed."""
    return _read_marker() is not None


@contextmanager
def profile_lease(headless=True):
    """Hold the same nonblocking interprocess lock for the full owned context.

    headless=False is reserved for the explicit manual launcher/finish actions.
    A persistent empty lock file is normal; its existence does not mean it is held.
    """
    if type(headless) is not bool:
        raise ValueError("invalid_profile_lease_mode")
    profile = owned_profile_path()
    root = _browser_root()
    root.mkdir(parents=True, exist_ok=True)
    no_links(root)
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(root / "edge-profile.lock", blocking=False))
        except PermissionError as error:
            # Windows can reject the locked byte read before msvcrt.locking().
            # Normalize acquisition only; errors from the body remain unchanged.
            raise RuntimeError("browser_profile_lease_unavailable") from error
        if headless and manual_login_pending():
            raise RuntimeError("manual_login_pending")
        no_links(profile)
        yield profile


def _write_marker(value):
    value = _validate_marker(value)
    path = _marker_path()
    no_links(path, path.exists())
    descriptor, temporary = tempfile.mkstemp(prefix=".manual-login-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path, path.exists())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _known_edge():
    for candidate in known_application_paths()["edge"]:
        path = Path(candidate)
        try:
            if path.is_absolute() and path.name.casefold() == "msedge.exe" and no_links(path).is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def _study_active(store=None):
    store = StudyResume() if store is None else store
    lease = store.read()
    if lease is None:
        return False
    remaining = store.remaining_seconds(lease)
    if type(remaining) not in (int, float) or math.isnan(remaining) or remaining < 0:
        raise ValueError("invalid_study_remaining_time")
    return remaining > 0


@contextmanager
def _study_launch_lock(store):
    """Serialize the launch decision with StudyResume.activate()/clear()."""
    root = no_links(Path(store.root))
    if not root.is_absolute():
        raise ValueError("invalid_study_session_root")
    root.mkdir(parents=True, exist_ok=True)
    no_links(root)
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(root / "session.lock", blocking=False))
        except PermissionError as error:
            raise RuntimeError("study_session_lock_unavailable") from error
        yield


def _result(status, **values):
    return {"status": status, "process_started": False, "login_verified": False,
            "controllable": False, "profile_availability": "not_verified",
            "manual_session_id": None, **values}


def prepare_manual_login():
    """Fixed, read-only login plan; never acquire a lease or create directories."""
    profile = owned_profile_path()
    marker = _read_marker()
    pending = marker is not None
    active = _study_active()
    edge = _known_edge()
    status = ("blocked_manual_login_pending" if pending else "blocked_study_active" if active
              else "blocked_edge_not_found" if edge is None else "prepared_not_started")
    return _result(status, profile_path=str(profile), url=LOGIN_URL,
                   argv=[str(edge), "--user-data-dir=" + str(profile), LOGIN_URL] if edge else None,
                   cwd=str(edge.parent) if edge else None, shell=False,
                   study_active=active, manual_login_pending=pending,
                   manual_session_id=marker["session_id"] if marker else None,
                   foreground_authorization_required=True)


def launch_manual_login(foreground_authorized=False, spawn=None):
    """Launch only after per-request screen consent and while Study is inactive.

    The marker persists even if spawn fails. This does not close a headless
    context: its existing profile lease must first be released by its owner.
    """
    if foreground_authorized is not True:
        return _result("blocked_focus_window")
    # This first check precedes every mkdir/lock/marker write.
    study = StudyResume()
    if _study_active(study):
        return _result("blocked_study_active", study_active=True)
    if _known_edge() is None:
        return _result("blocked_edge_not_found")
    with profile_lease(headless=False) as profile:
        previous = _read_marker()
        if previous is not None:
            return _result("blocked_manual_login_pending", manual_login_pending=True,
                           manual_session_id=previous["session_id"])
        # Lock order is profile -> Study. Study's writers never acquire profile.
        # Hold only through process creation, never through the human's login.
        with _study_launch_lock(study):
            if _study_active(study):
                return _result("blocked_study_active", study_active=True)
            edge = _known_edge()
            if edge is None:
                return _result("blocked_edge_not_found")
            marker = _validate_marker({"version": 1, "state": "manual_login",
                                       "session_id": uuid.uuid4().hex, "created_utc": time.time()})
            _write_marker(marker)
            argv = [str(edge), "--user-data-dir=" + str(profile), LOGIN_URL]
            launcher = subprocess.Popen if spawn is None else spawn
            try:
                process = launcher(argv, cwd=str(edge.parent), shell=False)
            except Exception:
                marker["state"] = "failed"
                try:
                    _write_marker(marker)
                except (OSError, ValueError):
                    # The already-durable manual_login marker still fails closed.
                    pass
                return _result("launch_failed", manual_login_pending=True, error="process_creation_failed",
                               manual_session_id=marker["session_id"])
            result = _result("manual_login_started", process_started=True, manual_login_pending=True,
                             profile_path=str(profile), runtime_status="launch_requested_not_verified",
                             manual_session_id=marker["session_id"],
                             user_must_close_login_window=True)
            pid = getattr(process, "pid", None)
            if type(pid) is int and pid > 0:
                result["pid"] = pid
            return result


def finish_manual_login(*, user_done=False, manual_session_id=None):
    """Clear only a valid manual marker after explicit owner confirmation.

    The user must first close their separate login window. We neither close it
    nor infer that its profile lock is free; the next headless open verifies that.
    Corrupt markers require separate recovery and are never silently deleted.
    """
    if user_done is not True:
        return _result("blocked_user_confirmation_required")
    with profile_lease(headless=False):
        marker = _read_marker()
        if marker is not None:
            if type(manual_session_id) is not str or manual_session_id != marker["session_id"]:
                return _result("blocked_manual_session_changed", manual_login_pending=True)
            path = _marker_path()
            no_links(path, True)
            path.unlink()
        return _result("manual_login_finished", manual_login_pending=False,
                       headless_verification_required=True, user_reported_window_closed=True)
