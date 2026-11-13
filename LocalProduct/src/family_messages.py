"""Local inbox/outbox for deliberately sent family messages.

No network, recording, transport credentials, startup changes or automatic retry.
``sent`` means a transport accepted a message, never that somebody read it.
SQLite stores message text locally without application-level encryption. No
retention/secure-erasure promise is made by this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

MAX_TEXT_CHARS = 4000
MAX_TEXT_BYTES = 16000
MAX_REQUEST_BYTES = 65536
APPLICATION_ID = 0x46504D53
SCHEMA_VERSION = 1
STATUSES = ("queued", "sending", "sent", "uncertain", "failed", "received", "shown")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z", re.ASCII)
_BINDING = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_COLUMNS = (
    ("id", "TEXT"), ("direction", "TEXT"), ("client_id", "TEXT"),
    ("text", "TEXT"), ("reply_to", "TEXT"), ("created_at", "TEXT"),
    ("updated_at", "TEXT"), ("status", "TEXT"),
    ("telegram_update_id", "INTEGER"), ("telegram_message_id", "INTEGER"),
    ("error_code", "TEXT"), ("attempts", "INTEGER"),
)


def default_root() -> Path:
    # Resolve only when requested; importing the module never creates storage.
    from paths import DATA_ROOT
    return DATA_ROOT / "family-messages"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Введите непустой текст сообщения.")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError("Сообщение слишком длинное.")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("Текст содержит недопустимые символы.") from None
    if len(encoded) > MAX_TEXT_BYTES or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        raise ValueError("Текст содержит недопустимые символы или слишком велик.")
    return value


def _opaque(value: str, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise ValueError("Некорректный идентификатор: " + label)
    return value


def _integer(value: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9223372036854775807:
        raise ValueError("Некорректный числовой идентификатор.")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 200:
        raise ValueError("Размер списка должен быть от 1 до 200.")
    return value


def _check_path(path: Path) -> None:
    # Do not resolve first: doing so would hide an existing link/junction.
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Папки и файлы сообщений не должны быть ссылками.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("Файл сообщений имеет дополнительные жёсткие ссылки.")
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Необычный объект в пути хранения сообщений.")


class MessageStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(default_root() if root is None else root).absolute()
        _check_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _check_path(self.root)
        self.db = self.root / "messages.sqlite"
        self.db_path = self.db
        with self._connect(validate=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                objects = conn.execute("SELECT name FROM sqlite_master").fetchall()
                app = conn.execute("PRAGMA application_id").fetchone()[0]
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if not objects and app == 0 and version == 0:
                    conn.execute("""CREATE TABLE messages (
                        id TEXT PRIMARY KEY,
                        direction TEXT NOT NULL CHECK(direction IN ('outgoing','incoming')),
                        client_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        reply_to TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        telegram_update_id INTEGER UNIQUE,
                        telegram_message_id INTEGER,
                        error_code TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(direction,client_id),
                        CHECK((direction='outgoing' AND status IN
                          ('queued','sending','sent','uncertain','failed')) OR
                          (direction='incoming' AND status IN ('received','shown')))
                    )""")
                    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    conn.execute("""CREATE TABLE tombstones (
                        direction TEXT NOT NULL,
                        receipt_key TEXT NOT NULL,
                        local_id TEXT NOT NULL,
                        telegram_message_id INTEGER,
                        PRIMARY KEY(direction,receipt_key)
                    )""")
                    conn.execute("CREATE INDEX messages_pending ON messages(direction,status,created_at)")
                    conn.execute("PRAGMA application_id=" + str(APPLICATION_ID))
                    conn.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
                self._validate(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def safe_path(self, filename: str) -> Path:
        """Validate one ordinary filename inside this store, e.g. worker.lock."""
        if (not isinstance(filename, str) or not _FILENAME.fullmatch(filename)
                or filename.endswith(".")
                or filename.split(".", 1)[0].upper() in
                {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                 *("LPT" + str(i) for i in range(1, 10))}):
            raise ValueError("Требуется обычное имя файла внутри папки сообщений.")
        target = self.root / filename
        _check_path(target)
        return target

    def _storage_paths(self) -> None:
        for name in ("messages.sqlite", "messages.sqlite-journal", "messages.sqlite-wal", "messages.sqlite-shm"):
            self.safe_path(name)

    @contextmanager
    def _connect(self, validate: bool = True):
        self._storage_paths()
        conn = sqlite3.connect(self.db, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            self._storage_paths()
            if validate:
                self._validate(conn)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _validate(conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Это не база семейных сообщений.")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Версия базы сообщений не поддерживается.")
        objects = conn.execute("SELECT name,type,tbl_name FROM sqlite_master").fetchall()
        tables = {"messages", "metadata", "tombstones"}
        if {r[0] for r in objects if r[1] == "table"} != tables:
            raise ValueError("Неожиданная структура базы сообщений.")
        if any(r[1] not in ("table", "index") or r[2] not in tables for r in objects):
            raise ValueError("Неожиданные объекты в базе сообщений.")
        if tuple((r[1], r[2]) for r in conn.execute("PRAGMA table_info(messages)")) != _COLUMNS:
            raise ValueError("Неожиданные поля базы сообщений.")
        if tuple((r[1], r[2]) for r in conn.execute("PRAGMA table_info(metadata)")) != (("key", "TEXT"), ("value", "TEXT")):
            raise ValueError("Неожиданные поля служебных данных.")
        if tuple((r[1], r[2]) for r in conn.execute("PRAGMA table_info(tombstones)")) != (
                ("direction", "TEXT"), ("receipt_key", "TEXT"), ("local_id", "TEXT"),
                ("telegram_message_id", "INTEGER")):
            raise ValueError("Неожиданные поля квитанций удаления.")

    @contextmanager
    def _write(self):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _row(conn: sqlite3.Connection, ident: str) -> dict:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (_opaque(ident, "сообщение"),)).fetchone()
        if row is None:
            raise KeyError("Сообщение не найдено.")
        return dict(row)

    def enqueue_reply(self, text: str, client_id: str | None = None, reply_to: str | None = None) -> dict:
        text = _text(text)
        nonce = uuid.uuid4().hex if client_id is None else _opaque(client_id, "повтор отправки")
        if reply_to is not None:
            reply_to = _opaque(reply_to, "исходное сообщение")
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM tombstones WHERE direction='outgoing' AND receipt_key=?", (nonce,)).fetchone():
                raise ValueError("Эта отправка удалена локально; её нельзя повторить тем же идентификатором.")
            existing = conn.execute("SELECT * FROM messages WHERE direction='outgoing' AND client_id=?", (nonce,)).fetchone()
            if existing is not None:
                result = dict(existing)
                if result["text"] != text or result["reply_to"] != reply_to:
                    raise ValueError("Идентификатор отправки уже относится к другому сообщению.")
                return result
            ident, stamp = uuid.uuid4().hex, _now()
            conn.execute("""INSERT INTO messages
                (id,direction,client_id,text,reply_to,created_at,updated_at,status)
                VALUES (?,'outgoing',?,?,?,?,?,'queued')""", (ident, nonce, text, reply_to, stamp, stamp))
            return self._row(conn, ident)

    def list_messages(self, limit: int = 50, direction: str | None = None) -> list[dict]:
        limit = _limit(limit)
        if direction not in (None, "outgoing", "incoming"):
            raise ValueError("Неизвестное направление сообщений.")
        with self._connect() as conn:
            where = "" if direction is None else " WHERE direction=?"
            values = (limit,) if direction is None else (direction, limit)
            rows = conn.execute("SELECT * FROM messages" + where + " ORDER BY created_at DESC,rowid DESC LIMIT ?", values)
            return [dict(row) for row in rows]

    def pending_incoming(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("""SELECT * FROM messages
                WHERE direction='incoming' AND status='received'
                ORDER BY created_at,rowid LIMIT ?""", (_limit(limit),))]

    def status(self) -> dict:
        with self._connect() as conn:
            counts = {r[0]: r[1] for r in conn.execute("SELECT status,count(*) FROM messages GROUP BY status")}
            bound = conn.execute("SELECT 1 FROM metadata WHERE key='telegram_binding'").fetchone() is not None
        return {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in STATUSES},
                "data_root": str(self.root), "binding_set": bound,
                "transport_connected": False}

    def claim_next(self) -> dict | None:
        with self._write() as conn:
            row = conn.execute("""SELECT id FROM messages
                WHERE direction='outgoing' AND status='queued' ORDER BY created_at,rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE messages SET status='sending',updated_at=?,error_code=NULL,attempts=attempts+1 WHERE id=?", (_now(), row[0]))
            return self._row(conn, row[0])

    def mark_sent(self, ident: str, telegram_message_id: int) -> dict:
        message_id = _integer(telegram_message_id, 1)
        with self._write() as conn:
            row = self._row(conn, ident)
            if row["direction"] != "outgoing":
                raise ValueError("Это не исходящее сообщение.")
            if row["status"] == "sent" and row["telegram_message_id"] == message_id:
                return row
            if row["status"] != "sending":
                raise ValueError("Нет подтверждаемой попытки отправки.")
            conn.execute("UPDATE messages SET status='sent',telegram_message_id=?,updated_at=?,error_code=NULL WHERE id=?", (message_id, _now(), ident))
            return self._row(conn, ident)

    def _mark_error(self, ident: str, state: str, error_code: str) -> dict:
        if not isinstance(error_code, str) or not _ERROR.fullmatch(error_code):
            raise ValueError("Требуется короткий технический код ошибки.")
        with self._write() as conn:
            row = self._row(conn, ident)
            if row["direction"] != "outgoing" or row["status"] != "sending":
                raise ValueError("Нет текущей попытки отправки.")
            conn.execute("UPDATE messages SET status=?,error_code=?,updated_at=? WHERE id=?", (state, error_code, _now(), ident))
            return self._row(conn, ident)

    def mark_uncertain(self, ident: str, error_code: str) -> dict:
        return self._mark_error(ident, "uncertain", error_code)

    def mark_failed(self, ident: str, error_code: str) -> dict:
        return self._mark_error(ident, "failed", error_code)

    def recover_sending(self) -> int:
        """Call only after acquiring the single-worker lock; never resend here."""
        with self._write() as conn:
            changed = conn.execute("""UPDATE messages SET status='uncertain',
                error_code='interrupted',updated_at=?
                WHERE direction='outgoing' AND status='sending'""", (_now(),))
            return changed.rowcount

    def retry_message(self, ident: str) -> dict:
        """Explicit user retry; a prior ambiguous attempt may have arrived."""
        with self._write() as conn:
            row = self._row(conn, ident)
            if row["direction"] != "outgoing" or row["status"] not in ("uncertain", "failed"):
                raise ValueError("Это сообщение нельзя повторить из текущего состояния.")
            conn.execute("UPDATE messages SET status='queued',error_code=NULL,updated_at=? WHERE id=?", (_now(), ident))
            return self._row(conn, ident)

    def add_incoming(self, text: str, telegram_update_id: int, telegram_message_id: int) -> dict:
        text, update_id, message_id = _text(text), _integer(telegram_update_id), _integer(telegram_message_id, 1)
        with self._write() as conn:
            old = conn.execute("SELECT * FROM messages WHERE telegram_update_id=?", (update_id,)).fetchone()
            if old is not None:
                result = dict(old)
                if result["direction"] != "incoming" or result["text"] != text or result["telegram_message_id"] != message_id:
                    raise ValueError("Обновление Telegram уже относится к другому сообщению.")
                return result
            removed = conn.execute("SELECT * FROM tombstones WHERE direction='incoming' AND receipt_key=?", (str(update_id),)).fetchone()
            if removed is not None:
                if removed["telegram_message_id"] != message_id:
                    raise ValueError("Обновление Telegram уже относится к другому сообщению.")
                return {"id": removed["local_id"], "direction": "incoming", "text": None,
                        "status": "deleted", "telegram_update_id": update_id,
                        "telegram_message_id": message_id}
            ident, stamp = uuid.uuid4().hex, _now()
            conn.execute("""INSERT INTO messages
                (id,direction,client_id,text,created_at,updated_at,status,telegram_update_id,telegram_message_id)
                VALUES (?,'incoming',?,?,?,?,'received',?,?)""", (ident, "telegram:" + str(update_id), text, stamp, stamp, update_id, message_id))
            return self._row(conn, ident)

    def mark_incoming_shown(self, ident: str) -> dict:
        with self._write() as conn:
            row = self._row(conn, ident)
            if row["direction"] != "incoming" or row["status"] not in ("received", "shown"):
                raise ValueError("Это не входящее сообщение для отображения.")
            if row["status"] != "shown":
                conn.execute("UPDATE messages SET status='shown',updated_at=? WHERE id=?", (_now(), ident))
            return self._row(conn, ident)

    def delete_message(self, ident: str) -> dict:
        """Delete local text only. Technical dedup receipts remain, as does Telegram."""
        with self._write() as conn:
            row = self._row(conn, ident)
            if row["status"] == "sending":
                raise ValueError("Нельзя удалять сообщение во время отправки.")
            receipt_key = row["client_id"] if row["direction"] == "outgoing" else str(row["telegram_update_id"])
            conn.execute("""INSERT INTO tombstones(direction,receipt_key,local_id,telegram_message_id)
                VALUES (?,?,?,?)""", (row["direction"], receipt_key, ident, row["telegram_message_id"]))
            conn.execute("DELETE FROM messages WHERE id=?", (ident,))
            return {"id": ident, "deleted": True, "scope": "local", "direction": row["direction"]}

    def get_meta(self, key: str, default=None):
        if key != "telegram_offset":
            raise ValueError("Неизвестное служебное поле.")
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return default if row is None else _integer(int(row[0]))

    def set_meta(self, key: str, value: int) -> int:
        if key != "telegram_offset":
            raise ValueError("Неизвестное служебное поле.")
        return self.advance_update_offset(value)

    def get_update_offset(self) -> int:
        return self.get_meta("telegram_offset", 0)

    def advance_update_offset(self, offset: int) -> int:
        offset = _integer(offset)
        with self._write() as conn:
            old = conn.execute("SELECT value FROM metadata WHERE key='telegram_offset'").fetchone()
            next_offset = max(offset, _integer(int(old[0])) if old is not None else 0)
            conn.execute("INSERT INTO metadata(key,value) VALUES ('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(next_offset),))
            return next_offset

    def get_binding(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='telegram_binding'").fetchone()
            return None if row is None else row[0]

    def bind_identity(self, identity: str) -> str:
        if not isinstance(identity, str) or not _BINDING.fullmatch(identity):
            raise ValueError("Требуется SHA-256 идентификатор канала.")
        with self._write() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='telegram_binding'").fetchone()
            if row is not None and row[0] != identity:
                raise ValueError("Очередь уже привязана к другому каналу Telegram.")
            if row is None:
                conn.execute("INSERT INTO metadata(key,value) VALUES ('telegram_binding',?)", (identity,))
            return identity


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Локальная очередь намеренно отправленных семейных сообщений.")
    parser.add_argument("--data-root", type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--enqueue-stdin", action="store_true")
    modes.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.enqueue_stdin:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Запрос слишком велик.")
            value = json.loads(raw.decode("utf-8", "strict"))
            if not isinstance(value, dict) or set(value) - {"text", "client_id", "reply_to"} or "text" not in value:
                raise ValueError("Некорректные поля запроса.")
            # Validate before even creating storage for an invalid message.
            _text(value["text"])
            row = MessageStore(args.data_root).enqueue_reply(**value)
            result = {"id": row["id"], "status": row["status"], "delivery_confirmed": False}
        else:
            result = MessageStore(args.data_root).status()
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, UnicodeError):
        # Never echo private text, credentials, paths supplied by callers, or raw
        # transport errors into stdout/stderr. The GUI can show its own message.
        print(json.dumps({"error": "local_queue_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
