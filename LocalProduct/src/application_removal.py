"""Bounded requests to registered uninstallers; never a file-removal service.

The constructor is inert. Providers/openers are trusted test injection points,
not UI/MCP options. A provider is callable (or has snapshot()) and returns
{items, complete, warnings, steam_client, system32}. Registered item dictionaries
use the fields emitted by _WindowsProvider below.

Holds, plans and write-ahead receipts share a per-user ledger. Its authentication
detects damaged/edited records; it is not an OS security boundary against an
owner who can replace the parent anchor, entire ledger and its key. There is no
hold-clear API. The adjacent anchor detects accidental whole-ledger loss across
manager instances.
"""
from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import hashlib
import hmac
import json
import math
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from activity_core import exclusive_lock, no_links

TTL = 300
MAX_JSON = 256 * 1024
MAX_ITEMS = 2000
MAX_HOLDS = 128
MAX_BINARY = 64 * 1024**2
UNSAFE = 0x400 | 0x1000 | 0x40000 | 0x400000
SETTINGS = "ms-settings:appsfeatures"
ACTIONS = {"steam_uninstall", "msi_uninstall", "windows_settings"}
GUID = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
ID = re.compile(r"(?:steam:[1-9][0-9]{0,11}|msi:\{[0-9A-Fa-f-]{36}\}|registry:[a-f0-9]{64}|portable:[a-z0-9][a-z0-9._-]{0,95})")
NONCE = re.compile(r"[a-f0-9]{32}")
PLAN_KEYS = {"version", "kind", "plan_id", "id", "name", "action", "target",
             "created_utc", "expires_utc", "reported_bytes", "install_path",
             "identity_digest", "argv", "binary_stamp", "signature"}
RECEIPT_KEYS = {"version", "kind", "plan_id", "id", "state", "detail",
                "before_free", "after_free", "checked_utc", "attempted",
                "registration_present", "directory_remnants", "signature"}
STATES = {"not_approved", "expired", "stale", "protected", "blocked_by_policy",
          "unsupported", "dispatching", "requested", "failed", "uncertain",
          "not_requested", "verification_incomplete", "registration_absent",
          "registration_absent_remnants", "registration_absent_unverified_path"}


class RemovalError(ValueError):
    pass


def _text(value, limit=1024, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise RemovalError("Invalid bounded text")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RemovalError("Control characters are not permitted")
    return value


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise RemovalError("Invalid application identity")
    if value.startswith("msi:") and not GUID.fullmatch(value[4:]):
        raise RemovalError("Invalid MSI identity")
    return value


def _utc(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def _epoch(value):
    if not isinstance(value, str):
        raise RemovalError("Invalid timestamp")
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise RemovalError("Timezone is required")
    return stamp.timestamp()


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise RemovalError("Duplicate JSON key")
        value[key] = item
    return value


def _bad_constant(value):
    raise RemovalError("Non-finite JSON value")


def _guard(path, regular=False):
    path = Path(path)
    try:
        no_links(path, regular)
    except ValueError:
        raise RemovalError("Unsafe path or file links") from None
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & UNSAFE:
            raise RemovalError("Reparse/offline paths are not permitted")
    if os.name == "nt" and (str(path).startswith("\\\\") or ":" in str(path)[2:]):
        raise RemovalError("Only local paths without streams are permitted")
    return path


def _read(path, limit=MAX_JSON):
    path = _guard(path, True)
    before = path.stat()
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RemovalError("Ordinary one-link file required")
        raw = stream.read(limit + 1)
    after = path.stat()
    stamp = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns)
    _guard(path, True)
    if len(raw) > limit or stamp(before) != stamp(info) or stamp(info) != stamp(after):
        raise RemovalError("Bounded record changed during reading")
    return raw


def _json_read(path):
    return json.loads(_read(path).decode("utf-8-sig"), object_pairs_hook=_pairs,
                      parse_constant=_bad_constant)


def _atomic(path, value):
    path = Path(path)
    raw = _canonical_json(value)
    if len(raw) > MAX_JSON:
        raise RemovalError("Record is too large")
    _guard(path.parent)
    _guard(path, path.exists())
    fd, name = tempfile.mkstemp(prefix=".application-removal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _guard(path, path.exists())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)  # Only this newly-created ledger temporary file.


def _local_appdata():
    if os.name != "nt":
        raise RemovalError("Windows LocalAppData is required")
    pointer = ctypes.c_void_p()
    identifier = (ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    shell = ctypes.WinDLL("shell32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    if shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0x4000, None,
                                 ctypes.byref(pointer)) != 0:
        raise RemovalError("Known LocalAppData is unavailable")
    try:
        return Path(ctypes.wstring_at(pointer))
    finally:
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoTaskMemFree(pointer)


def _system32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise RemovalError("Native Windows system directory is unavailable")
    return str(Path(buffer.value))


def _norm(path):
    return ntpath.normcase(ntpath.normpath(path)) if path else ""


def _name(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold()
                   if c.isalnum())


def _self_install_path(path):
    """Match this application directory, never a generic ancestor name."""
    candidate = _norm(path)
    roots = [Path(__file__).absolute().parent]
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).absolute().parent)
    for root in roots:
        known = _norm(str(root))
        if candidate and (candidate == known or candidate.startswith(known + "\\") or
                          known.startswith(candidate + "\\")):
            return True
    return False


def _protected(item):
    if item["system_component"] or item["no_remove"] or item["release_type"]:
        return "Системный, служебный или запрещённый к удалению компонент."
    text = unicodedata.normalize("NFKC", " ".join(
        (item["name"], item["publisher"], item["install_path"]))).casefold()
    minecraft = ("minecraft", "mojang", "tlauncher", "prism", "multimc", "polymc",
                 "curseforge", "modrinth", "optifine", "modpack", "neoforge",
                 ".minecraft")
    if any(token in text for token in minecraft) or re.search(
            r"(?<![a-z])(fabric|forge|ftb|worlds|saves|mods)(?![a-z])", text):
        return "Minecraft, миры, моды и связанные инструменты сохраняются."
    identity_text = unicodedata.normalize(
        "NFKC", item["name"] + " " + item["publisher"]).casefold()
    if (any(token in identity_text for token in ("prostranstvo", "пространство", "codex")) or
            _self_install_path(item["install_path"])):
        return "Приложение и инструменты агента сохраняются."
    if any(token in text for token in (
            "antivirus", "defender",
            "security", "firewall", "driver", "runtime", "redistributable",
            "visual c++", ".net", "framework", "gaming services", "windows update",
            "microsoft windows", "python", "powershell", "winpcap", "npcap",
            "java", "jre", "jdk", "openjdk", "temurin", "zulu", "adoptium",
            "azul", "oracle", "graalvm", "corretto", "semeru")):
        return "Защищённый инструмент, драйвер, runtime или компонент безопасности."
    if item["name"].strip().casefold() in ("steam", "steam client"):
        return "Клиент Steam и общие данные библиотек сохраняются."
    return ""


def _split_command(raw):
    """Narrow uninstaller grammar, not a general command-line interpreter."""
    raw = _text(raw, 4096, False)
    match = re.fullmatch(r'\s*(?:"([^"]+)"|([^\s"]+))((?:\s+[^\s"]+)*)\s*', raw)
    if not match:
        raise RemovalError("Ambiguous uninstall command")
    return [match[1] or match[2], *match[3].split()]


def _binary_stamp(path, expected_name, *, native_system_binary=False):
    path = Path(path)
    if not path.is_absolute() or path.name.casefold() != expected_name:
        raise RemovalError("Expected absolute trusted executable")
    _guard(path, not native_system_binary)
    if os.path.normcase(str(path.absolute())) != os.path.normcase(str(path.resolve())):
        raise RemovalError("Executable canonical path changed")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or (not native_system_binary and before.st_nlink != 1):
        raise RemovalError("Ordinary executable required")
    if before.st_size > MAX_BINARY:
        raise RemovalError("Executable exceeds bounded verification size")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        total = 0
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_BINARY:
                raise RemovalError("Executable grew during verification")
            digest.update(block)
    _guard(path, not native_system_binary)
    after = path.stat()
    stamp = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns)
    if stamp(before) != stamp(opened) or stamp(opened) != stamp(after):
        raise RemovalError("Executable changed")
    return dict(path=str(path), dev=after.st_dev, ino=after.st_ino,
                size=after.st_size, mtime_ns=after.st_mtime_ns, nlink=after.st_nlink,
                sha256=digest.hexdigest())


class _WindowsProvider:
    """Read-only native Uninstall keys and machine-registered Steam location."""
    def snapshot(self):
        if os.name != "nt":
            return dict(items=[], warnings=["Каталог доступен только в Windows."],
                        complete=False, steam_client=None, system32=None)
        import winreg
        items, warnings = [], []
        complete = True
        roots = (("HKLM64", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                 ("HKLM32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                 ("HKCU", winreg.HKEY_CURRENT_USER, 0))
        base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

        def get(key, name, default=None):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                return default

        for context, hive, view in roots:
            try:
                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            subkey = winreg.EnumKey(root, index)
                            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
                                name = get(key, "DisplayName", "")
                                if not name:
                                    continue
                                steam = re.fullmatch(r"Steam App ([1-9][0-9]{0,11})", subkey)
                                msi = get(key, "WindowsInstaller", 0) == 1 and GUID.fullmatch(subkey)
                                identity = ("steam:" + steam[1] if steam else
                                            "msi:" + subkey.upper() if msi else
                                            "registry:" + _digest([context, subkey]))
                                estimate = get(key, "EstimatedSize")
                                items.append(dict(id=identity, name=name,
                                    publisher=get(key, "Publisher", ""),
                                    install_path=get(key, "InstallLocation", ""),
                                    source="registry", registry_context=context,
                                    registry_key=subkey, registered=True,
                                    reported_bytes=estimate * 1024 if type(estimate) is int and estimate >= 0 else None,
                                    uninstall_string=get(key, "UninstallString", ""),
                                    system_component=get(key, "SystemComponent", 0) != 0,
                                    no_remove=get(key, "NoRemove", 0) != 0,
                                    release_type=get(key, "ReleaseType", ""),
                                    windows_installer=bool(msi)))
                        except OSError:
                            complete = False
            except FileNotFoundError:
                pass
            except OSError:
                complete = False
        clients = set()
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam",
                                    0, winreg.KEY_READ | view) as key:
                    location = get(key, "InstallPath", "")
                    if isinstance(location, str) and location:
                        clients.add(str(Path(location) / "steam.exe"))
            except FileNotFoundError:
                pass
            except OSError:
                complete = False
        # Machine installation identity is required; HKCU SteamPath alone is
        # not executable authority. This does not authenticate against an admin.
        steam_client = next(iter(clients)) if len(clients) == 1 else None
        if not steam_client:
            warnings.append("Надёжная машинная регистрация клиента Steam не определена.")
        if not complete:
            warnings.append("Каталог прочитан не полностью; запросы отключены.")
        return dict(items=items, complete=complete, warnings=warnings,
                    steam_client=steam_client, system32=_system32())


def _default_opener(argv):
    if argv == [SETTINGS]:
        os.startfile(SETTINGS)
        return None
    return subprocess.Popen(argv, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _free_snapshot(install_path):
    if (not install_path or str(install_path).startswith(("\\\\", "//")) or
            not Path(install_path).is_absolute()):
        return {}
    drive = Path(install_path).anchor
    if not drive:
        return {}
    try:
        return {drive: shutil.disk_usage(drive).free}
    except OSError:
        return {}


class ApplicationRemovalManager:
    def __init__(self, root=None, provider=None, opener=None, clock=time.time):
        if root is not None:
            candidate = Path(root)
            if (not candidate.is_absolute() or ".." in candidate.parts or
                    str(candidate).startswith(("\\\\", "//")) or candidate == Path(candidate.anchor)):
                raise RemovalError("An explicit local ledger directory is required")
        self._root = Path(root).absolute() if root is not None else None
        self.provider = provider
        self.opener = opener
        self.clock = clock
        self._known = False

    @property
    def root(self):
        if self._root is None:
            self._root = _local_appdata() / "ProstranstvoLocalBeta/data/application-removal"
        return self._root

    @property
    def anchor_path(self):
        token = _digest(_norm(str(self.root.absolute())))[:24]
        return self.root.parent / (".application-removal-" + token + ".anchor.json")

    def close(self):
        pass

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RemovalError("Invalid clock")
        return float(value)

    def _key(self):
        value = _read(self.root / "key.bin", 32)
        if len(value) != 32:
            raise RemovalError("Invalid ledger key")
        return value

    def _seal(self, value):
        value = dict(value)
        value.pop("signature", None)
        return {**value, "signature": hmac.new(self._key(), _canonical_json(value), hashlib.sha256).hexdigest()}

    def _unseal(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("signature"), str):
            raise RemovalError("Unsigned ledger record")
        expected = self._seal(value)["signature"]
        if not hmac.compare_digest(expected, value["signature"]):
            raise RemovalError("Ledger record authentication failed")
        return value

    def _state(self):
        if not self.root.exists():
            if self._known or self.anchor_path.exists():
                raise RemovalError("Previously known policy ledger is missing")
            return []
        _guard(self.root)
        marker = _json_read(self.root / "marker.json")
        anchor = _json_read(self.anchor_path)
        holds = self._unseal(_json_read(self.root / "holds.json"))
        if not isinstance(marker, dict) or not isinstance(anchor, dict):
            raise RemovalError("Invalid initialization marker")
        if (set(marker) != {"version", "ledger_id"} or marker["version"] != 1 or
                anchor != marker or set(anchor) != {"version", "ledger_id"} or
                not isinstance(marker["ledger_id"], str) or not NONCE.fullmatch(marker["ledger_id"]) or
                set(holds) != {"version", "ledger_id", "holds", "signature"} or
                holds["version"] != 1 or holds["ledger_id"] != marker["ledger_id"] or
                not isinstance(holds["holds"], list) or len(holds["holds"]) > MAX_HOLDS):
            raise RemovalError("Invalid policy hold ledger")
        for row in holds["holds"]:
            if not isinstance(row, dict) or set(row) != {"descriptor", "reason", "source", "created_utc"}:
                raise RemovalError("Invalid hold")
            self._descriptor(row["descriptor"])
            _text(row["reason"], 1024, False)
            _text(row["source"], 256, False)
            _epoch(row["created_utc"])
        self._known = True
        return holds["holds"]

    def _initialize(self):
        if not self.root.exists() and self.anchor_path.exists():
            raise RemovalError("Previously initialized ledger is missing")
        _guard(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _guard(self.root)
        with exclusive_lock(self.root / "manager.lock"):
            if (self.root / "marker.json").exists():
                self._state()
                return
            if self._known or self.anchor_path.exists() or any(p.name != "manager.lock" for p in self.root.iterdir()):
                raise RemovalError("Policy ledger is missing or incomplete")
            # A new empty explicit ledger can be initialized by prepare/hold
            # seeding. A partial prior ledger cannot be silently reset.
            key = self.root / "key.bin"
            with key.open("xb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            marker = dict(version=1, ledger_id=uuid.uuid4().hex)
            _atomic(self.anchor_path, marker)
            _atomic(self.root / "marker.json", marker)
            _atomic(self.root / "holds.json", self._seal({**marker, "holds": []}))
            self._known = True

    @staticmethod
    def _descriptor(value):
        if not isinstance(value, dict) or set(value) - {"id", "name", "install_path", "action"} or "id" not in value:
            raise RemovalError("Invalid hold descriptor")
        _id(value["id"])
        for key in ("name", "install_path"):
            if key in value:
                _text(value[key], 1024)
        if "action" in value and value["action"] not in ACTIONS:
            raise RemovalError("Invalid held action")
        return dict(value)

    def add_policy_hold(self, descriptor, reason, source):
        descriptor = self._descriptor(descriptor)
        _text(reason, 1024, False)
        _text(source, 256, False)
        self._initialize()
        with exclusive_lock(self.root / "manager.lock"):
            holds = self._state()
            if any(x["descriptor"] == descriptor for x in holds):
                return next(dict(x) for x in holds if x["descriptor"] == descriptor)
            if len(holds) >= MAX_HOLDS:
                raise RemovalError("Policy hold capacity reached")
            row = dict(descriptor=descriptor, reason=reason, source=source,
                       created_utc=_utc(self._now()))
            holds.append(row)
            marker = _json_read(self.root / "marker.json")
            _atomic(self.root / "holds.json", self._seal({**marker, "holds": holds}))
            return dict(row)

    @staticmethod
    def _held(item, holds):
        for hold in holds:
            descriptor = hold["descriptor"]
            left, right = _norm(item["install_path"]), _norm(descriptor.get("install_path", ""))
            a, b = _name(item["name"]), _name(descriptor.get("name", ""))
            if (item["id"] == descriptor["id"] or
                    (left and right and (left == right or left.startswith(right + "\\") or right.startswith(left + "\\"))) or
                    (a and b and (a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))))):
                return hold["reason"]
        return ""

    def _snapshot(self):
        provider = self.provider or _WindowsProvider()
        raw = provider() if callable(provider) else provider.snapshot()
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list) or len(raw["items"]) > MAX_ITEMS:
            raise RemovalError("Invalid or oversized application catalog")
        if type(raw.get("complete", True)) is not bool:
            raise RemovalError("Invalid catalog completeness")
        warnings = raw.get("warnings", [])
        if not isinstance(warnings, list) or len(warnings) > 50:
            raise RemovalError("Invalid catalog warnings")
        warnings = [_text(x, 1024) for x in warnings]
        rows, ids = [], set()
        for source in raw["items"]:
            if not isinstance(source, dict):
                raise RemovalError("Invalid catalog row")
            identity = _id(source.get("id"))
            if identity in ids:
                raise RemovalError("Duplicate application identity; inventory ambiguous")
            ids.add(identity)
            row = dict(source)
            for field in ("name", "publisher", "install_path", "source", "uninstall_string", "release_type"):
                row[field] = _text(row.get(field) or "", 4096 if field == "uninstall_string" else 1024)
            if not row["name"]:
                raise RemovalError("Application name required")
            for field in ("system_component", "no_remove", "windows_installer"):
                if row.get(field, False) not in (False, True) or type(row.get(field, False)) not in (bool, int):
                    raise RemovalError("Invalid registration flag")
                row[field] = bool(row.get(field, False))
            amount = row.get("reported_bytes")
            if amount is not None and (type(amount) is not int or amount < 0 or amount > 2**63 - 1):
                raise RemovalError("Invalid reported size")
            rows.append(row)
        return dict(raw, items=rows, warnings=warnings, complete=raw.get("complete", True), _stamps={})

    def _route(self, row, snapshot):
        action, target, argv, stamp = "windows_settings", SETTINGS, [SETTINGS], None
        identity = row["id"]
        if identity.startswith("portable:") or row["source"] != "registry" or row.get("registered") is not True:
            raise RemovalError("No registered application identity")
        if identity.startswith("steam:"):
            appid = identity[6:]
            client = snapshot.get("steam_client")
            if not client:
                raise RemovalError("Trusted Steam client unavailable")
            arguments = _split_command(row["uninstall_string"])
            expected = "steam://uninstall/" + appid
            if (len(arguments) != 2 or _norm(arguments[0]) != _norm(client) or
                    arguments[1] != expected or row.get("registry_key") != "Steam App " + appid):
                raise RemovalError("Registered Steam command does not match identity")
            action, target, argv = "steam_uninstall", expected, [str(client), expected]
            binary, filename = str(client), "steam.exe"
        elif identity.startswith("msi:"):
            product = identity[4:].upper()
            if not row["windows_installer"] or not GUID.fullmatch(product) or row.get("registry_key", "").upper() != product:
                raise RemovalError("MSI registration identity required")
            system = snapshot.get("system32")
            if not system:
                raise RemovalError("Native Windows system directory unavailable")
            binary = str(Path(system) / "msiexec.exe")
            arguments = _split_command(row["uninstall_string"])
            if (_norm(arguments[0]) != _norm(binary) and arguments[0].casefold() != "msiexec.exe"):
                raise RemovalError("Untrusted registered MSI executable")
            options = arguments[1:]
            if options and options[-1].casefold() == "/norestart":
                options = options[:-1]
            joined = "".join(options)
            if not re.fullmatch(r"/[iIxX]" + re.escape(product), joined, re.IGNORECASE):
                raise RemovalError("Unsupported MSI parameters")
            action, target, argv, filename = "msi_uninstall", product, [binary, "/x", product, "/norestart"], "msiexec.exe"
        else:
            return action, target, argv, stamp
        if binary not in snapshot["_stamps"]:
            snapshot["_stamps"][binary] = _binary_stamp(
                binary, filename, native_system_binary=action == "msi_uninstall")
        return action, target, argv, snapshot["_stamps"][binary]

    def _classified(self, row, snapshot, holds, ledger_error=""):
        public = {k: row.get(k, "") for k in ("id", "name", "publisher", "install_path", "source")}
        public.update(reported_bytes=row.get("reported_bytes"), action="windows_settings",
                      status="available", reason="Ручная страница Windows: удаление выбирается самостоятельно.")
        reason = ledger_error or self._held(row, holds)
        if reason:
            public.update(status="blocked_by_policy", reason=reason)
        elif _protected(row):
            public.update(status="protected", reason=_protected(row))
        elif not snapshot["complete"]:
            public.update(status="unsupported", reason="Неполный каталог; запрос отключён.")
        else:
            try:
                action, target, argv, stamp = self._route(row, snapshot)
                public.update(action=action, reason=(
                    "Запрос штатного удаления Steam; результат проверяется отдельно." if action == "steam_uninstall" else
                    "Штатный MSI UI без quiet/elevation/reboot; результат проверяется отдельно." if action == "msi_uninstall" else public["reason"]))
                public["_route"] = (target, argv, stamp)
            except (RemovalError, OSError):
                public.update(status="unsupported", reason="Безопасный штатный маршрут не подтверждён.")
        public["_identity"] = _digest(row)
        return public

    def catalog(self):
        try:
            snapshot = self._snapshot()
        except Exception:
            return dict(items=[], warnings=["Каталог недоступен или неоднозначен; запросы отключены."], collected_utc=_utc(self._now()))
        ledger_error, holds = "", []
        try:
            holds = self._state()
        except (ValueError, OSError):
            ledger_error = "Policy holds повреждены или отсутствуют; запросы заблокированы."
        items = [self._classified(row, snapshot, holds, ledger_error) for row in snapshot["items"]]
        known = {x["id"] for x in items}
        for hold in holds:
            descriptor = hold["descriptor"]
            if descriptor["id"] not in known:
                items.append(dict(id=descriptor["id"], name=descriptor.get("name") or descriptor["id"],
                    publisher="", install_path=descriptor.get("install_path", ""), source="policy_hold",
                    action=descriptor.get("action", "windows_settings"), reported_bytes=None,
                    status="blocked_by_policy", reason=hold["reason"]))
                known.add(descriptor["id"])
        clean = [{k: v for k, v in x.items() if not k.startswith("_")} for x in items]
        return dict(items=clean, warnings=snapshot["warnings"] + ([ledger_error] if ledger_error else []),
                    collected_utc=_utc(self._now()))

    def _plan_path(self, plan_id):
        if not isinstance(plan_id, str) or not NONCE.fullmatch(plan_id):
            raise RemovalError("Invalid plan identity")
        return self.root / ("plan-" + plan_id + ".json")

    def _receipt_path(self, plan_id):
        self._plan_path(plan_id)
        return self.root / ("receipt-" + plan_id + ".json")

    def _load_plan(self, plan_id):
        plan = self._unseal(_json_read(self._plan_path(plan_id)))
        if (set(plan) != PLAN_KEYS or plan["version"] != 1 or plan["kind"] != "application-removal-plan" or
                plan["plan_id"] != plan_id or plan["action"] not in ACTIONS or
                not 0 < _epoch(plan["expires_utc"]) - _epoch(plan["created_utc"]) <= TTL):
            raise RemovalError("Invalid saved plan")
        _id(plan["id"])
        for field in ("name", "target", "install_path", "identity_digest"):
            _text(plan[field], 4096)
        if (not isinstance(plan["argv"], list) or not 1 <= len(plan["argv"]) <= 4 or
                any(not isinstance(x, str) for x in plan["argv"])):
            raise RemovalError("Invalid saved route")
        return plan

    def prepare(self, identity):
        _id(identity)
        self._initialize()
        with exclusive_lock(self.root / "manager.lock"):
            holds, snapshot = self._state(), self._snapshot()
            row = next((x for x in snapshot["items"] if x["id"] == identity), None)
            if row is None:
                raise RemovalError("Registered application not found")
            item = self._classified(row, snapshot, holds)
            if item["status"] != "available":
                raise RemovalError(item["reason"])
            target, argv, stamp = item["_route"]
            now = self._now()
            plan = self._seal(dict(version=1, kind="application-removal-plan",
                plan_id=uuid.uuid4().hex, id=identity, name=item["name"],
                action=item["action"], target=target, argv=argv, binary_stamp=stamp,
                created_utc=_utc(now), expires_utc=_utc(now + TTL),
                reported_bytes=item["reported_bytes"], install_path=item["install_path"],
                identity_digest=item["_identity"]))
            _atomic(self._plan_path(plan["plan_id"]), plan)
            self._save_receipt(self._receipt(
                plan, "not_requested", "План подготовлен; внешний запрос ещё не выполнялся."))
            return dict(plan)

    def _receipt(self, plan, state, detail, *, attempted=False, before=None, after=None,
                 present=None, remnants="unverified"):
        return dict(version=1, kind="application-removal-receipt", plan_id=plan["plan_id"],
                    id=plan["id"], state=state, detail=detail, before_free=before or {},
                    after_free=after or {}, checked_utc=_utc(self._now()), attempted=attempted,
                    registration_present=present, directory_remnants=remnants)

    def _save_receipt(self, value):
        value = self._seal(value)
        _atomic(self._receipt_path(value["plan_id"]), value)
        return dict(value)

    def _load_receipt(self, plan):
        path = self._receipt_path(plan["plan_id"])
        if not path.exists():
            raise RemovalError("Required request receipt is missing")
        value = self._unseal(_json_read(path))
        if (set(value) != RECEIPT_KEYS or value["version"] != 1 or
                value["kind"] != "application-removal-receipt" or value["state"] not in STATES or
                value["id"] != plan["id"] or value["plan_id"] != plan["plan_id"] or
                type(value["attempted"]) is not bool):
            raise RemovalError("Invalid saved receipt")
        return value

    def request(self, plan_id, approved=False):
        with exclusive_lock(_guard(self.root / "manager.lock")):
            plan = self._load_plan(plan_id)
            if approved is not True:
                return self._receipt(plan, "not_approved", "Требуется подтверждение в локальном UI.")
            previous = self._load_receipt(plan)
            if previous is not None:
                if previous["state"] == "dispatching":
                    previous.update(state="uncertain", detail="Предыдущий запрос мог начаться; автоматического повтора нет.")
                    return self._save_receipt(previous)
                if previous["state"] != "not_requested" or previous["attempted"]:
                    return dict(previous)
            consumed = self.root / ("attempt-" + plan_id + ".json")
            if consumed.exists():
                _guard(consumed, True)
                previous.update(state="uncertain", attempted=True,
                                detail="Запрос уже был начат; автоматического повтора нет.")
                return self._save_receipt(previous)
            try:
                holds, snapshot = self._state(), self._snapshot()
            except (ValueError, OSError):
                return self._save_receipt(self._receipt(plan, "blocked_by_policy", "Policy/catalog недоступны; запрос не выполнялся."))
            held = self._held(dict(id=plan["id"], name=plan["name"], install_path=plan["install_path"]), holds)
            if held:
                return self._save_receipt(self._receipt(plan, "blocked_by_policy", held))
            now = self._now()
            if now < _epoch(plan["created_utc"]) - 5 or now > _epoch(plan["expires_utc"]):
                return self._save_receipt(self._receipt(plan, "expired", "Срок плана истёк; запрос не выполнялся."))
            row = next((x for x in snapshot["items"] if x["id"] == plan["id"]), None)
            if row is None:
                return self._save_receipt(self._receipt(plan, "stale", "Регистрация изменилась; запрос не выполнялся."))
            item = self._classified(row, snapshot, holds)
            if item["status"] != "available":
                return self._save_receipt(self._receipt(plan, item["status"], item["reason"]))
            target, argv, binary = item["_route"]
            if (item["_identity"] != plan["identity_digest"] or target != plan["target"] or
                    argv != plan["argv"] or binary != plan["binary_stamp"]):
                return self._save_receipt(self._receipt(plan, "stale", "Программа или штатный маршрут изменились; запрос не выполнялся."))
            before = _free_snapshot(plan["install_path"])
            receipt = self._receipt(plan, "dispatching", "Запрос подготовлен к открытию; удаление не подтверждено.",
                                    attempted=True, before=before, after=before, present=True)
            self._save_receipt(receipt)  # Durable consume BEFORE any external effect.
            # A separate create-once record also blocks replay of an older
            # authentic not_requested receipt. Neither record has a clear API.
            consume_record = self._seal(dict(plan_id=plan_id, id=plan["id"],
                                              created_utc=_utc(self._now())))
            _guard(consumed)
            with consumed.open("xb") as stream:
                stream.write(_canonical_json(consume_record))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                opened = (self.opener or _default_opener)(list(argv))
                receipt.update(state="failed" if opened is False else "requested",
                               detail="Штатный UI запрошен; это не подтверждение удаления." if opened is not False else "Открытие не подтверждено; автоматического повтора нет.")
            except Exception as error:
                receipt.update(state="failed" if isinstance(error, OSError) else "uncertain",
                               detail="Исход запроса не подтверждён; автоматического повтора нет.")
            receipt["after_free"] = _free_snapshot(plan["install_path"])
            return self._save_receipt(receipt)

    @staticmethod
    def _remnants(path):
        if not path or not Path(path).is_absolute() or str(path).startswith("\\\\"):
            return "unverified"
        try:
            _guard(path)
            Path(path).lstat()
            return "present"
        except FileNotFoundError:
            return "absent"
        except (ValueError, OSError):
            return "unverified"

    def verify(self, plan_id):
        with exclusive_lock(_guard(self.root / "manager.lock")):
            plan = self._load_plan(plan_id)
            previous = self._load_receipt(plan)
            receipt = previous or self._receipt(plan, "not_requested", "Запрос не выполнялся.")
            try:
                self._state()
                snapshot = self._snapshot()
                if not snapshot["complete"]:
                    raise RemovalError("Incomplete inventory")
            except (ValueError, OSError):
                receipt.update(state="verification_incomplete", detail="Каталог неполон; отсутствие программы не установлено.")
                return self._save_receipt(receipt)
            present = any(x["id"] == plan["id"] for x in snapshot["items"])
            remnants = self._remnants(plan["install_path"])
            receipt.update(registration_present=present, directory_remnants=remnants,
                           after_free=_free_snapshot(plan["install_path"]), checked_utc=_utc(self._now()))
            if not present:
                state = ("registration_absent_remnants" if remnants == "present" else
                         "registration_absent" if remnants == "absent" else "registration_absent_unverified_path")
                receipt.update(state=state, detail="Регистрация не найдена; это наблюдение, не доказательство удаления всех файлов.")
            elif receipt["state"] == "dispatching":
                receipt.update(state="uncertain", detail="Регистрация ещё есть; исход предыдущего открытия неизвестен.")
            else:
                if receipt["state"].startswith("registration_absent") or receipt["state"] == "verification_incomplete":
                    receipt["state"] = "uncertain" if receipt["attempted"] else "not_requested"
                receipt["detail"] = "Регистрация ещё есть; удаление не подтверждено."
            return self._save_receipt(receipt)
