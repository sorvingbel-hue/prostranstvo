"""Bounded local records for learning; no host imports or network."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid


MAX_SESSIONS = 1000
MAX_EVENTS = 200
MAX_REQUESTS = 20000


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def no_link(path):
    path = Path(path)
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("Linked data paths are not supported")


class LocalRepository:
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        no_link(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "learning.sqlite3"
        no_link(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            no_link(str(self.path) + suffix)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA trusted_schema=OFF")
        self.connection.execute("PRAGMA max_page_count=8192")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL
                REFERENCES tasks(id), updated_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL
                REFERENCES sessions(id) ON DELETE CASCADE, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                response TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_session ON events(session_id, id);
            CREATE INDEX IF NOT EXISTS session_updated ON sessions(updated_at);
        """)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def cached(self, request_id):
        row = self.connection.execute("SELECT fingerprint,response FROM requests WHERE id=?", (request_id,)).fetchone()
        return None if row is None else (row["fingerprint"], json.loads(row["response"]))

    def cache(self, request_id, fingerprint, response):
        count = self.connection.execute("SELECT count(*) FROM requests").fetchone()[0]
        if count >= MAX_REQUESTS:
            raise OverflowError("request_limit")
        self.connection.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, fingerprint, encode(response)))

    def put_task(self, task):
        self.connection.execute("INSERT INTO tasks VALUES(?,?)", (task["task_id"], encode(task)))

    def task(self, task_id):
        row = self.connection.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return None if row is None else json.loads(row["body"])

    def create_session(self, session):
        if self.connection.execute("SELECT count(*) FROM sessions").fetchone()[0] >= MAX_SESSIONS:
            raise OverflowError("session_limit")
        self.connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (session["session_id"], session["task_id"], session["updated_at"], encode(session)))

    def session(self, session_id):
        row = self.connection.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else json.loads(row["body"])

    def update_session(self, session):
        self.connection.execute("UPDATE sessions SET task_id=?,updated_at=?,body=? WHERE id=?", (session["task_id"], session["updated_at"], encode(session), session["session_id"]))

    def add_event(self, session_id, event):
        count = self.connection.execute("SELECT count(*) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]
        if count >= MAX_EVENTS:
            raise OverflowError("event_limit")
        self.connection.execute("INSERT INTO events(session_id,body) VALUES(?,?)", (session_id, encode(event)))

    def events(self, session_id):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT body FROM events WHERE session_id=? ORDER BY id", (session_id,))]

    def history(self):
        result = []
        for row in self.connection.execute("SELECT body FROM sessions ORDER BY updated_at DESC,id DESC LIMIT 100"):
            session = json.loads(row[0])
            task = self.task(session["task_id"])
            result.append(dict(session, prompt_ru=task["prompt_ru"], prompt_en=task["prompt_en"]))
        return result

    def export_report(self, session, request_id, created_at):
        directory = self.root / "exports"
        no_link(directory)
        directory.mkdir(exist_ok=True)
        destination = directory / ("lesson-" + session["session_id"] + "-r" + str(session["revision"]) + "-" + request_id + ".json")
        no_link(destination)
        events = self.events(session["session_id"])
        task_ids = {session["task_id"]}
        for event in events:
            for key in ("task_id", "new_task_id"):
                if isinstance(event.get(key), str):
                    task_ids.add(event[key])
        payload = {"format_version": "1.0", "created_at_utc": session["updated_at"],
                   "export_request_id": request_id,
                   "source": "local_learning_module", "session": session,
                   "current_task": self.task(session["task_id"]), "events": events,
                   "tasks": {task_id: self.task(task_id) for task_id in sorted(task_ids)},
                   "limitations": ["Local profile is not proof of a person's identity.",
                                   "Checked answers are not proof of mastery or teaching effectiveness.",
                                   "Stored locally; anyone with this Windows account may have access."]}
        body = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if destination.exists():
            # A previous write may have completed before its DB receipt committed.
            # Reuse only the exact immutable snapshot; never overwrite an edited report.
            if destination.stat().st_size != len(body) or destination.read_bytes() != body:
                raise FileExistsError("Export content differs")
            return {"path": str(destination), "saved": True, "local_only": True}
        temporary = directory / (".pending-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"path": str(destination), "saved": True, "local_only": True}
