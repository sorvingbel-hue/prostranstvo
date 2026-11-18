"""Local browser inventory and inert, separate-profile access plans.

Import, inventory and planning never start a process, create a directory, read a
browser profile, change MCP settings or contact a page. Only the explicitly
authorized ``launch_visible_browser`` entry point may start a process. There is
deliberately no headless/MCP start function here.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import stat
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

import paths

DATA_ROOT = getattr(paths, 'DATA_ROOT', paths.DEPLOY_ROOT / 'data')


OFFICIAL_README = "https://github.com/microsoft/playwright-mcp#configuration"
OFFICIAL_PACKAGE = "https://github.com/microsoft/playwright-mcp/blob/main/package.json"
_LABELS = {"edge": "Microsoft Edge", "chrome": "Google Chrome",
           "yandex": "Яндекс Браузер", "notion": "Notion Desktop"}
_CHANNELS = {"edge": "msedge", "chrome": "chrome"}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


def known_application_paths():
    """Fixed relative candidates under standard Windows installation roots.

    Environment roots belong to trusted host configuration. No executable path
    or command-line flags are accepted from a browser request.
    """
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return {
        "edge": tuple(root / "Microsoft/Edge/Application/msedge.exe"
                      for root in (program_files, program_files_x86, local)),
        "chrome": tuple(root / "Google/Chrome/Application/chrome.exe"
                        for root in (program_files, program_files_x86, local)),
        "yandex": tuple(root / "Yandex/YandexBrowser/Application/browser.exe"
                        for root in (program_files, program_files_x86, local)),
        "notion": (local / "Programs/Notion/Notion.exe", local / "Notion/Notion.exe"),
    }


def _path_is_file(path):
    return path.is_file()


def inventory_browsers(*, is_file=None):
    """Check known paths only; ``is_file`` is a filesystem test double hook.

    Missing candidates mean 'not found in known paths', not 'not installed'.
    This does not inspect running processes, tabs, cookies or login state.
    """
    probe = _path_is_file if is_file is None else is_file
    rows = []
    for browser_id, candidates in known_application_paths().items():
        found, unreadable = [], False
        for path in dict.fromkeys(candidates):
            try:
                if probe(path):
                    found.append(str(path))
            except OSError:
                unreadable = True
        rows.append({
            "browser_id": browser_id, "name": _LABELS[browser_id],
            "status": "found" if found else ("unreadable" if unreadable else "not_found_in_known_paths"),
            "executable_path": found[0] if found else None,
            "found_paths": found,
            "headless_support": "official_playwright_channel" if browser_id in _CHANNELS else "unsupported",
            "existing_session_access": False,
            "login_state": "not_inspected",
        })
    return {"runtime_status": "diagnostic_only", "process_started": False,
            "scope": "known_paths_only", "applications": rows}


def _has_control_characters(value):
    return any(unicodedata.category(character).startswith("C") for character in value)


def validate_https_url(value):
    """Return a normalized HTTPS URL or raise a non-secret-bearing ValueError."""
    if not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise ValueError("invalid_https_url")
    if (_has_control_characters(value) or any(character.isspace() for character in value)
            or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value)):
        raise ValueError("invalid_https_url")
    try:
        decoded = unquote(value, errors="strict")
        if _has_control_characters(decoded) or "\\" in decoded:
            raise ValueError("invalid_https_url")
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or "@" in parts.netloc or port == 0):
            raise ValueError("invalid_https_url")
        bracketed = parts.netloc.startswith("[")
        authority_pattern = r"\[[^\[\]]+\](?::[0-9]+)?" if bracketed else r"[^:\[\]]+(?::[0-9]+)?"
        if not re.fullmatch(authority_pattern, parts.netloc) or "%" in host:
            raise ValueError("invalid_https_url")
        try:
            address = ipaddress.ip_address(host)
            if bracketed and address.version != 6:
                raise ValueError("invalid_https_url")
            authority = "[" + str(address) + "]" if address.version == 6 else str(address)
        except ValueError:
            if bracketed:
                raise ValueError("invalid_https_url")
            authority = host.encode("idna").decode("ascii").lower()
            labels = authority.rstrip(".").split(".")
            if len(authority) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
                raise ValueError("invalid_https_url")
        if port is not None:
            authority += ":" + str(port)
        return urlunsplit(("https", authority, parts.path or "/", parts.query, parts.fragment))
    except (ValueError, UnicodeError):
        raise ValueError("invalid_https_url") from None


def _isolated_profile_path(browser_id):
    # DATA_ROOT is an application-owned constant, never a request argument.
    root = Path(DATA_ROOT)
    if not root.is_absolute() or str(root.drive).startswith("\\\\"):
        raise ValueError("unsafe_application_data_root")
    profile = root / "browser-access" / "profiles" / browser_id
    if not profile.is_relative_to(root):
        raise ValueError("unsafe_browser_profile")
    # Fail closed for existing symlink/junction ancestry, without creating files.
    for candidate in (profile, *profile.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("unsafe_browser_profile_link")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("unsafe_browser_profile_not_directory")
    return profile


def build_browser_access_plan(browser_id, url, *, mode="headless", foreground_authorized=False):
    """Prepare a JSON-compatible plan; no process or connection is started."""
    if not isinstance(browser_id, str) or browser_id not in _LABELS:
        raise ValueError("unknown_browser")
    if mode not in ("headless", "visible"):
        raise ValueError("unknown_browser_mode")
    target = validate_https_url(url)
    row = next(item for item in inventory_browsers()["applications"]
               if item["browser_id"] == browser_id)
    plan = {
        "browser_id": browser_id, "name": row["name"], "mode": mode,
        "url": target, "status": "planned", "runtime_status": "planned_not_started",
        "process_started": False, "controllable": False,
        "browser_control": "not_connected_until_MCP_started" if mode == "headless" else "launch_only_no_controller",
        "session_access": "separate_profile_login_required", "existing_session_access": False,
        "foreground_authorized": foreground_authorized is True,
        "may_change_focus_or_open_window": mode == "visible",
        "executable_path": row["executable_path"], "profile_path": None,
        "mcp_config": None, "launch": None,
    }
    # This guard is evaluated on each request, before creating a launch command.
    if mode == "visible" and foreground_authorized is not True:
        plan["status"] = "blocked_focus_window"
        return plan
    if browser_id == "notion":
        plan.update(status="unsupported_application", browser_control="not_supported")
        return plan
    if mode == "headless" and browser_id not in _CHANNELS:
        plan.update(status="unsupported_headless_browser", browser_control="unsupported",
                    support_note="Yandex headless control is unverified/experimental; no official Playwright channel is claimed.")
        return plan
    if row["executable_path"] is None:
        plan["status"] = "blocked_browser_not_found_in_known_paths"
        return plan
    try:
        profile = _isolated_profile_path(browser_id)
    except (OSError, ValueError):
        plan["status"] = "blocked_unsafe_profile_path"
        return plan
    plan["profile_path"] = str(profile)
    if mode == "headless":
        plan["mcp_config"] = {"mcpServers": {"prostranstvo_browser": {
            "type": "stdio", "command": "playwright-mcp",
            "args": ["--headless", "--browser", _CHANNELS[browser_id],
                     "--user-data-dir", str(profile), "--image-responses", "omit"],
        }}}
        plan["prerequisites"] = ["official_@playwright/mcp_already_installed",
                                  "compatible_MCP_client_connection_required"]
    else:
        executable = Path(row["executable_path"])
        plan["launch"] = {"args": [str(executable), "--user-data-dir=" + str(profile), target],
                          "cwd": str(executable.parent), "shell": False}
    return plan


def launch_visible_browser(browser_id, url, foreground_authorized=False, spawn=None):
    """Request a visible, isolated launch only with explicit per-call permission.

    ``spawn`` follows subprocess.Popen's arguments and exists for mock-only
    tests. Success confirms process creation, never navigation, login or control.
    The ordinary default is subprocess.Popen; it is reached only after the guard.
    """
    plan = build_browser_access_plan(browser_id, url, mode="visible",
                                     foreground_authorized=foreground_authorized)
    if plan["status"] != "planned":
        return plan
    launcher = subprocess.Popen if spawn is None else spawn
    launch = plan["launch"]
    try:
        process = launcher(list(launch["args"]), cwd=launch["cwd"], shell=False)
    except OSError:
        plan.update(status="launch_failed", runtime_status="not_started", error="process_creation_failed")
        return plan
    plan.update(status="launch_requested", runtime_status="launch_requested_not_verified", process_started=True)
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        plan["pid"] = pid
    return plan
