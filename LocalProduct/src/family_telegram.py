"""Explicit, private-chat Telegram transport for intentional family messages.

No connections, file creation, or credential reads occur at import. Tokens and
pairing state are protected by Windows DPAPI for the current user. A shared
Windows login is the same DPAPI identity; this is not protection from that user.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request

TOKEN_RE = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}\Z")
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
METHODS = frozenset(("getMe", "getWebhookInfo", "getUpdates", "sendMessage"))
MAX_RESPONSE = 1024 * 1024
MAX_CONFIG = 65536
PAIR_TTL = 600


class TelegramError(RuntimeError):
    """Only static error codes leave the adapter; never remote descriptions."""

    def __init__(self, code="telegram_error"):
        self.code = code if re.fullmatch(r"[a-z0-9_]{1,48}", str(code)) else "telegram_error"
        super().__init__(self.code)


class TelegramRejected(TelegramError):
    def __init__(self, error_code, retry_after=None):
        number = error_code if type(error_code) is int and 400 <= error_code <= 599 else 400
        self.error_code = number
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        super().__init__("telegram_" + str(number))


class DeliveryUncertain(TelegramError):
    """The send might have succeeded; it must not be retried automatically."""

    def __init__(self):
        super().__init__("delivery_uncertain")


class _SettingsLock:
    """Reentrant Windows mutex shared by GUI/agent instances and processes.

    The kernel object creates no files, including before initial setup. Global
    scope also serializes two logon sessions using the same configured folder.
    Default kernel-object ACLs apply; failure to acquire fails closed. There is
    deliberately no thread-only fallback on another OS.
    """

    def __init__(self, root, timeout_ms=30000):
        key = os.path.normcase(os.path.abspath(os.fspath(root)))
        self._name = "Global\\Prostranstvo-FamilyTelegram-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        self._root = Path(root)
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle = None
        self._kernel = None
        self._timeout_ms = timeout_ms

    def __enter__(self):
        if not self._thread_lock.acquire(timeout=self._timeout_ms / 1000):
            raise TelegramError("transport_busy")
        handle = None
        kernel = None
        try:
            if self._depth:
                self._depth += 1
                return self
            _checked(self._root)
            if os.name != "nt":
                raise TelegramError("windows_mutex_required")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel.ReleaseMutex.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.CreateMutexW(None, False, self._name)
            if not handle:
                raise TelegramError("settings_lock_failed")
            outcome = kernel.WaitForSingleObject(handle, self._timeout_ms)
            if outcome not in (0, 0x80):  # acquired or abandoned; _load validates persisted state
                raise TelegramError("transport_busy" if outcome == 0x102 else "settings_lock_failed")
            self._handle, self._kernel, self._depth = handle, kernel, 1
            return self
        except BaseException:
            if handle and kernel:
                kernel.CloseHandle(handle)
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback):
        release_failed = False
        try:
            self._depth -= 1
            if not self._depth:
                handle, kernel = self._handle, self._kernel
                self._handle, self._kernel = None, None
                try:
                    release_failed = not bool(kernel.ReleaseMutex(handle))
                finally:
                    kernel.CloseHandle(handle)
        finally:
            self._thread_lock.release()
        if release_failed and exc_type is None:
            raise TelegramError("settings_lock_failed")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TelegramError("redirect_refused")


class TelegramAPI:
    def __init__(self, token, *, opener=None, timeout=10):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise TelegramError("invalid_token")
        if type(timeout) not in (int, float) or not 1 <= timeout <= 15:
            raise TelegramError("invalid_timeout")
        self._token = token
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def call(self, method, payload=None):
        if method not in METHODS or (payload is not None and not isinstance(payload, dict)):
            raise TelegramError("method_refused")
        try:
            body = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > 20000:
                raise TelegramError("request_too_large")
            request = urllib.request.Request(
                "https://api.telegram.org/bot" + self._token + "/" + method,
                data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
            http_code = 200
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                http_code = exc.code
                response = exc
            with response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise TelegramError("response_too_large")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise TelegramError("invalid_response")
            if result.get("ok") is not True:
                number = result.get("error_code", http_code)
                retry_after = (result.get("parameters") or {}).get("retry_after") if isinstance(result.get("parameters", {}), dict) else None
                if type(number) is int and 400 <= number < 500:
                    raise TelegramRejected(number, retry_after)
                raise TelegramError("server_error")
            if http_code != 200 or "result" not in result:
                raise TelegramError("invalid_response")
            return result["result"]
        except TelegramRejected:
            raise
        except Exception:
            if method == "sendMessage":
                raise DeliveryUncertain() from None
            raise TelegramError("request_failed") from None


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class DPAPIProtector:
    def _convert(self, value, decrypt):
        if os.name != "nt":
            raise TelegramError("windows_dpapi_required")
        if not isinstance(value, bytes) or not value or len(value) > MAX_CONFIG:
            raise TelegramError("credential_invalid")
        buffer = ctypes.create_string_buffer(value)
        source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = _Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
        function.restype = ctypes.c_int
        try:
            # CRYPTPROTECT_UI_FORBIDDEN only; never LOCAL_MACHINE.
            if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
                raise TelegramError("credential_unavailable")
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.memset(buffer, 0, len(value))
            if target.data:
                kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def protect(self, value):
        return self._convert(value, False)

    def unprotect(self, value):
        return self._convert(value, True)


def _checked(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TelegramError("unsafe_config_path")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise TelegramError("unsafe_config_path")
    return path


def _integer(value):
    return type(value) is int and 0 < value < 2**63


def _label(value):
    return "".join(c for c in str(value or "") if c.isprintable())[:80]


def _plain_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        return False
    if any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        return False
    try:
        return len(value.encode("utf-8", "strict")) <= 16000
    except UnicodeError:
        return False


def _updates(value):
    if not isinstance(value, list) or len(value) > 100:
        raise TelegramError("invalid_updates")
    return sorted((u for u in value if isinstance(u, dict) and type(u.get("update_id")) is int
                   and 0 <= u["update_id"] < 2**63 - 1), key=lambda u: u["update_id"])


def _private_message(update):
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat"), message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if chat.get("type") != "private" or sender.get("is_bot") is not False:
        return None
    if not _integer(chat.get("id")) or not _integer(sender.get("id")) or chat["id"] != sender["id"]:
        return None
    if not _integer(message.get("message_id")) or not isinstance(message.get("text"), str):
        return None
    return message


class TelegramSettings:
    def __init__(self, root=None, *, api_factory=TelegramAPI, protector=None, clock=time.time):
        if root is None:
            from paths import DATA_ROOT
            root = DATA_ROOT / "family-messages"
        self.root = Path(root).absolute()
        self.path = self.root / "telegram-config.json"
        self._api_factory = api_factory
        self._protector = protector or DPAPIProtector()
        self._clock = clock
        self._lock = _SettingsLock(self.root)

    def _load(self):
        try:
            path = _checked(self.path)
            if not path.exists():
                return {"enabled": False, "offset": 0}
            if not path.is_file() or path.stat().st_size > MAX_CONFIG:
                raise TelegramError("config_invalid")
            outer = json.loads(path.read_text(encoding="utf-8"))
            if outer.get("schema") != 1 or set(outer) != {"schema", "protected"}:
                raise TelegramError("config_invalid")
            plain = self._protector.unprotect(base64.b64decode(outer["protected"], validate=True))
            data = json.loads(plain)
            if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
                raise TelegramError("config_invalid")
            offset = data.get("offset", 0)
            if type(offset) is not int or not 0 <= offset < 2**63 - 1:
                # Telegram interprets negative offsets as truncating old updates.
                raise TelegramError("config_invalid")
            pinned = data.get("pinned")
            if pinned is not None and (not isinstance(pinned, dict) or set(pinned) != {"bot_id", "chat_id", "user_id"}
                                       or not all(_integer(v) for v in pinned.values())):
                raise TelegramError("config_invalid")
            return data
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("credential_unavailable") from None

    def _save(self, data):
        temporary = None
        try:
            _checked(self.root).mkdir(parents=True, exist_ok=True)
            _checked(self.path)
            encrypted = self._protector.protect(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            body = json.dumps({"schema": 1, "protected": base64.b64encode(encrypted).decode("ascii")}).encode("utf-8")
            if len(body) > MAX_CONFIG:
                raise TelegramError("config_too_large")
            temporary = self.path.with_name(".telegram-" + secrets.token_hex(16) + ".tmp")
            with open(temporary, "xb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            _checked(self.path)
            os.replace(temporary, self.path)
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("config_write_failed") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _call(self, token, method, payload=None):
        try:
            return self._api_factory(token).call(method, payload or {})
        except TelegramError:
            raise
        except Exception:
            if method == "sendMessage":
                raise DeliveryUncertain() from None
            raise TelegramError("request_failed") from None

    def _configured(self, data):
        if not isinstance(data.get("token"), str) or not TOKEN_RE.fullmatch(data["token"]):
            raise TelegramError("not_configured")
        return data["token"]

    def setup(self, token):
        with self._lock:
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
                raise TelegramError("invalid_token")
            data = self._load()
            bot = self._call(token, "getMe")
            if not isinstance(bot, dict) or bot.get("is_bot") is not True or not _integer(bot.get("id")) or not USERNAME_RE.fullmatch(str(bot.get("username", ""))):
                raise TelegramError("invalid_bot")
            webhook = self._call(token, "getWebhookInfo")
            if not isinstance(webhook, dict) or webhook.get("url"):
                raise TelegramError("webhook_active")
            pinned = data.get("pinned")
            if pinned and pinned["bot_id"] != bot["id"]:
                raise TelegramError("binding_changed")
            if (data.get("bot") or {}).get("id") != bot["id"]:
                data["offset"] = 0
            data.update(token=token, bot={"id": bot["id"], "username": bot["username"]},
                        enabled=False, paired=None, pairing=None)
            self._save(data)
            return self.status()

    def begin_pairing(self):
        with self._lock:
            data = self._load()
            self._configured(data)
            nonce = secrets.token_urlsafe(24)
            expires = int(self._clock()) + PAIR_TTL
            data.update(enabled=False, pairing={"nonce": nonce, "expires_at": expires, "candidate": None})
            self._save(data)
            return {"url": "https://t.me/" + data["bot"]["username"] + "?start=" + nonce,
                    "nonce": nonce, "expires_at": expires}

    def poll_pairing(self):
        with self._lock:
            data = self._load()
            token = self._configured(data)
            pairing = data.get("pairing")
            if not pairing:
                return None
            if self._clock() >= pairing["expires_at"]:
                data["pairing"] = None
                self._save(data)
                return None
            updates = _updates(self._call(token, "getUpdates", {"offset": data.get("offset", 0), "limit": 50,
                                                                  "timeout": 0, "allowed_updates": ["message"]}))
            for update in updates:
                data["offset"] = max(data.get("offset", 0), update["update_id"] + 1)
                message = _private_message(update)
                if not message or pairing["candidate"] or message["text"].strip() != "/start " + pairing["nonce"]:
                    continue
                sender = message["from"]
                pinned = data.get("pinned")
                if pinned and (pinned["chat_id"] != message["chat"]["id"] or pinned["user_id"] != sender["id"]):
                    continue
                pairing["candidate"] = {"chat_id": message["chat"]["id"], "user_id": sender["id"],
                                        "first_name": _label(sender.get("first_name")), "username": _label(sender.get("username"))}
            self._save(data)
            candidate = pairing.get("candidate")
            return dict(candidate, nonce=pairing["nonce"]) if candidate else None

    def confirm_pairing(self, nonce):
        with self._lock:
            data = self._load()
            self._configured(data)
            pairing = data.get("pairing")
            if not pairing or self._clock() >= pairing["expires_at"] or not isinstance(nonce, str) or not hmac.compare_digest(pairing["nonce"], nonce) or not pairing.get("candidate"):
                raise TelegramError("pairing_invalid")
            recipient = pairing["candidate"]
            identity = {"bot_id": data["bot"]["id"], "chat_id": recipient["chat_id"], "user_id": recipient["user_id"]}
            if data.get("pinned") and data["pinned"] != identity:
                raise TelegramError("binding_changed")
            data.update(pinned=identity, paired=recipient, pairing=None, enabled=False)
            self._save(data)
            return self.status()

    def enable(self):
        with self._lock:
            data = self._load()
            token = self._configured(data)
            if not data.get("paired"):
                raise TelegramError("not_paired")
            webhook = self._call(token, "getWebhookInfo")
            if not isinstance(webhook, dict) or webhook.get("url"):
                raise TelegramError("webhook_active")
            data["enabled"] = True
            self._save(data)
            return self.status()

    def disable(self):
        with self._lock:
            data = self._load()
            data["enabled"] = False
            self._save(data)
            return self.status()

    def disconnect(self):
        with self._lock:
            data = self._load()
            data.update(enabled=False, token=None, paired=None, pairing=None)
            self._save(data)
            return self.status()

    def status(self):
        with self._lock:
            data = self._load()
            recipient, pairing, bot = data.get("paired") or {}, data.get("pairing") or {}, data.get("bot") or {}
            return {"configured": bool(data.get("token")), "enabled": data["enabled"], "paired": bool(recipient),
                    "bot_username": bot.get("username"), "bot_id": bot.get("id"),
                    "recipient_name": recipient.get("first_name"), "recipient_user_id": recipient.get("user_id"),
                    "pairing_pending": bool(pairing), "pairing_expires_at": pairing.get("expires_at"),
                    "has_binding": bool(data.get("pinned")), "transport": "telegram"}

    def _active(self, data, store):
        if not data.get("enabled") or not data.get("paired"):
            return False
        self._configured(data)
        identity = hashlib.sha256(json.dumps(data["pinned"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            store.bind_identity(identity)
        except Exception:
            raise TelegramError("binding_changed") from None
        return True

    def queue_reply(self, store, text, client_id, reply_to=None):
        """Queue an intentional reply for the currently enabled, pinned channel.

        The settings mutex covers channel validation, durable queue binding and
        insertion. No API call, enabling, credential change or retry occurs here.
        """
        from family_messages import _text, _opaque
        _text(text)
        _opaque(client_id, "повтор отправки")
        if reply_to is not None:
            _opaque(reply_to, "исходное сообщение")
        with self._lock:
            data = self._load()
            self._configured(data)
            if not data.get("paired") or not data.get("pinned"):
                raise TelegramError("not_paired")
            if not data.get("enabled"):
                raise TelegramError("channel_paused")
            if not self._active(data, store):
                raise TelegramError("channel_paused")
            return store.enqueue_reply(text, client_id=client_id, reply_to=reply_to)

    def process_outbox(self, store):
        with self._lock:
            data = self._load()
            if not self._active(data, store):
                return None
            message = store.claim_next()
            if message is None:
                return None
            try:
                text = message["text"]
                if not _plain_text(text):
                    raise TelegramRejected(400)
                result = self._call(data["token"], "sendMessage", {"chat_id": data["paired"]["chat_id"], "text": text,
                          "disable_notification": False, "protect_content": True, "link_preview_options": {"is_disabled": True}})
                if not isinstance(result, dict) or not _integer(result.get("message_id")) or result.get("chat", {}).get("id") != data["paired"]["chat_id"]:
                    raise DeliveryUncertain()
            except TelegramRejected as exc:
                store.mark_failed(message["id"], exc.code)
                return {"id": message["id"], "status": "failed", "error_code": exc.code, "retry_after": exc.retry_after}
            except Exception:
                store.mark_uncertain(message["id"], "delivery_uncertain")
                return {"id": message["id"], "status": "uncertain", "error_code": "delivery_uncertain"}
            try:
                store.mark_sent(message["id"], result["message_id"])
            except Exception:
                try:
                    store.mark_uncertain(message["id"], "receipt_write_failed")
                except Exception:
                    pass
                raise TelegramError("receipt_write_failed") from None
            return {"id": message["id"], "status": "sent", "meaning": "accepted_by_telegram",
                    "notification": "requested", "telegram_message_id": result["message_id"]}

    def poll_incoming(self, store):
        with self._lock:
            data = self._load()
            if not self._active(data, store):
                return []
            offset = max(data.get("offset", 0), store.get_update_offset())
            updates = _updates(self._call(data["token"], "getUpdates", {"offset": offset, "limit": 50,
                                                                      "timeout": 0, "allowed_updates": ["message"]}))
            accepted = []
            for update in updates:
                if update["update_id"] < offset:
                    continue
                message = _private_message(update)
                if (message and message["chat"]["id"] == data["paired"]["chat_id"]
                        and message["from"]["id"] == data["paired"]["user_id"]
                        and _plain_text(message["text"])
                        and not message["text"].lstrip().startswith("/start")):
                    try:
                        record = store.add_incoming(message["text"], update["update_id"], message["message_id"])
                    except Exception:
                        raise TelegramError("incoming_write_failed") from None
                    if record.get("status") == "received":
                        accepted.append(record)
                # Advance only after the intended message was durably stored.
                try:
                    store.advance_update_offset(update["update_id"] + 1)
                except Exception:
                    raise TelegramError("offset_write_failed") from None
            return accepted
