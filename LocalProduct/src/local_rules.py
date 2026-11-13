"""Explicit, local file rules. Nothing runs on save or at application startup.

A preview is a short-lived snapshot, not permission to execute a future search.
Copy plans use the ordinary Executor approval queue. Inventory plans refer to
the exact previewed files. This module never executes a queued job itself.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from activity_core import no_links
from engine import fingerprint
from paths import DEPLOY_ROOT

DEFAULT_ROOT = DEPLOY_ROOT / "data" / "rules"
MAX_RULES = 100
MAX_PREVIEWS = 20
SCAN_LIMIT = 10_000
MAX_BYTES = 10 * 1024**3
PREVIEW_SECONDS = 15 * 60
MAX_STATE_BYTES = 8 * 1024**2
RULE_FIELDS = {
    "id", "version", "name", "source", "destination", "action", "name_glob",
    "name_contains", "extensions", "max_files", "max_total_bytes", "enabled",
}
DEFAULTS = {
    "destination": "", "name_glob": "*", "name_contains": "", "extensions": [],
    "max_files": 100, "max_total_bytes": 2 * 1024**3, "enabled": True,
}


class RuleError(ValueError):
    """The rule, snapshot, or requested selection is not safe to queue."""


def _text(value, field, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise RuleError("Неверное текстовое поле: " + field)
    value = value.strip()
    if not empty and not value:
        raise RuleError("Заполните поле: " + field)
    return value


def _integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuleError("Значение вне диапазона: " + field)
    return value


class RuleStore:
    """Atomic, locked registry with optimistic rule versions and single-use plans.

    ``save_rule`` accepts only the documented fields, supplying safe defaults.
    To edit, pass both the saved ``id`` and its current ``version``. The returned
    dictionary contains the full normalized rule and its incremented version.
    ``enqueue(id, token, version=None)`` accepts a token returned by ``preview``;
    retries return the already-created job rather than creating a second plan.
    """

    def __init__(self, engine, root=None):
        self.engine = engine
        self.root = Path(root if root is not None else DEFAULT_ROOT).absolute()
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        self.file = self.root / "rules.json"
        self.lock_file = self.root / "rules.lock"

    @contextmanager
    def _lock(self):
        # Acquire before reading/writing the byte. Windows denies reads of a
        # byte locked by another handle, even within the same process.
        no_links(self.lock_file)
        handle = open(self.lock_file, "a+b")
        locked = False
        try:
            no_links(self.lock_file, True)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 5
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuleError("Хранилище правил занято; повторите запрос") from None
                        time.sleep(.025)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
                locked = True
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def _atomic(self, state):
        no_links(self.root)
        no_links(self.file, self.file.exists())
        encoded = json.dumps(state, ensure_ascii=False, indent=2)
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise RuleError("Хранилище правил переполнено")
        fd, name = tempfile.mkstemp(prefix=".rules-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            for attempt in range(8):
                no_links(self.file, self.file.exists())
                try:
                    os.replace(name, self.file)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(.025)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _read(self):
        try:
            no_links(self.file, True)
            if self.file.stat().st_size > MAX_STATE_BYTES:
                raise RuleError("Хранилище правил слишком велико")
            state = json.loads(self.file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"format": 1, "rules": [], "previews": {}}
        if (not isinstance(state, dict) or set(state) != {"format", "rules", "previews"}
                or type(state["format"]) is not int or state["format"] != 1 or not isinstance(state["rules"], list)
                or len(state["rules"]) > MAX_RULES or not isinstance(state["previews"], dict)
                or len(state["previews"]) > MAX_PREVIEWS):
            raise RuleError("Повреждено хранилище правил")
        seen_ids, seen_names = set(), set()
        for rule in state["rules"]:
            value = self._validate(rule, paths=False, persisted=True)
            if value != rule or rule["id"] in seen_ids or rule["name"].casefold() in seen_names:
                raise RuleError("Повреждены или дублируются правила")
            seen_ids.add(rule["id"])
            seen_names.add(rule["name"].casefold())
        for token, preview in state["previews"].items():
            self._validate_preview(token, preview)
        return state

    @staticmethod
    def _validate_preview(token, value):
        base = {"token", "rule_id", "rule_version", "action", "source", "destination",
                "created_at", "expires_at", "status", "candidates", "count", "total_bytes",
                "scanned_files", "skipped"}
        if not isinstance(value, dict):
            raise RuleError("Повреждён предварительный просмотр")
        status = value.get("status")
        if status not in ("ready", "preparing", "queued", "failed"):
            raise RuleError("Неверное состояние предварительного просмотра")
        extra = {"job_id"} if status == "queued" else {"error"} if status == "failed" else set()
        if set(value) != base | extra or value["token"] != token:
            raise RuleError("Неполный предварительный просмотр")
        identifiers = [("token", token), ("rule_id", value["rule_id"])]
        if status == "queued":
            identifiers.append(("job_id", value["job_id"]))
        for field, ident in identifiers:
            if not isinstance(ident, str) or not re.fullmatch("[0-9a-f]{32}", ident):
                raise RuleError("Неверный идентификатор: " + field)
        _integer(value["rule_version"], "rule_version", 1, 2**31 - 1)
        if value["action"] not in ("copy", "scan"):
            raise RuleError("Неверное действие в предварительном просмотре")
        for field in ("source", "destination"):
            path = _text(value[field], field, 4096, empty=field == "destination")
            if (path and not Path(path).is_absolute()) or (field == "source" and not path):
                raise RuleError("Неверная папка в предварительном просмотре")
        for field in ("created_at", "expires_at"):
            if type(value[field]) not in (int, float) or not math.isfinite(value[field]):
                raise RuleError("Неверное время предварительного просмотра")
        if value["expires_at"] <= value["created_at"]:
            raise RuleError("Неверный срок предварительного просмотра")
        for field in ("count", "total_bytes", "scanned_files", "skipped"):
            _integer(value[field], field, 0, 2**63 - 1)
        rows = value["candidates"]
        if not isinstance(rows, list) or len(rows) > 100 or value["count"] != len(rows):
            raise RuleError("Неверный список выбранных файлов")
        for row in rows:
            fields = {"path", "size", "mtime_ns", "ino", "reason"}
            if value["action"] == "copy":
                fields.add("destination")
            if not isinstance(row, dict) or set(row) != fields:
                raise RuleError("Неверная запись выбранного файла")
            for field in ("path", "destination") if value["action"] == "copy" else ("path",):
                if not Path(_text(row[field], field, 4096)).is_absolute():
                    raise RuleError("Неверный путь выбранного файла")
            _text(row["reason"], "reason", 1000)
            for field in ("size", "mtime_ns", "ino"):
                _integer(row[field], field, 0, 2**128 - 1)
        if value["total_bytes"] != sum(row["size"] for row in rows) or value["scanned_files"] < len(rows):
            raise RuleError("Неверные итоги предварительного просмотра")
        if status == "failed":
            _text(value["error"], "error", 500, empty=True)

    def _validate(self, value, *, paths=True, persisted=False):
        if not isinstance(value, dict) or set(value) - RULE_FIELDS:
            raise RuleError("Неизвестное поле правила")
        if persisted and set(value) != RULE_FIELDS:
            raise RuleError("Неполное сохранённое правило")
        rule = {**DEFAULTS, **value}
        if not {"name", "source", "action"} <= set(rule):
            raise RuleError("Нужны имя, исходная папка и действие")
        rule["name"] = _text(rule["name"], "name", 100)
        if rule["action"] not in ("copy", "scan"):
            raise RuleError("Действие: только copy или scan")
        if type(rule["enabled"]) is not bool:
            raise RuleError("enabled должен быть true или false")
        for field in ("name_glob", "name_contains"):
            rule[field] = _text(rule[field], field, 120, empty=True)
            if any(c in rule[field] for c in ("/", "\\")):
                raise RuleError("Условие имени относится только к имени файла")
        rule["name_glob"] = rule["name_glob"] or "*"
        extensions = rule["extensions"]
        if not isinstance(extensions, list) or len(extensions) > 20:
            raise RuleError("Нужен список не более 20 расширений")
        normalized = set()
        for extension in extensions:
            if not isinstance(extension, str) or not re.fullmatch(r"\.[a-zA-Z0-9][a-zA-Z0-9_+-]{0,15}", extension):
                raise RuleError("Расширение должно выглядеть как .pdf")
            normalized.add(extension.casefold())
        rule["extensions"] = sorted(normalized)
        _integer(rule["max_files"], "max_files", 1, 100)
        _integer(rule["max_total_bytes"], "max_total_bytes", 1, MAX_BYTES)
        if "id" in rule:
            if not isinstance(rule["id"], str) or not re.fullmatch("[0-9a-f]{32}", rule["id"]):
                raise RuleError("Неверный идентификатор правила")
            _integer(rule.get("version"), "version", 1, 2**31 - 1)
        elif "version" in rule:
            raise RuleError("Версия без идентификатора недопустима")
        for field in ("source", "destination"):
            rule[field] = _text(rule[field], field, 4096, empty=field == "destination")
            if not rule[field]:
                if field == "destination" and rule["action"] == "scan":
                    continue
                raise RuleError("Выберите папку назначения")
            p = Path(rule[field])
            if not p.is_absolute():
                raise RuleError("Укажите абсолютный путь: " + field)
            if paths:
                p = self.engine.checked(p, folder=True)
                if not p.is_dir():
                    raise RuleError("Папка должна существовать: " + str(p))
            rule[field] = str(p)
        if rule["action"] == "scan" and rule["destination"]:
            raise RuleError("Для обзора папка назначения не используется")
        if rule["action"] == "copy" and rule["source"].casefold() == rule["destination"].casefold():
            raise RuleError("Исходная и целевая папки должны отличаться")
        return rule

    @staticmethod
    def _find(state, rule_id):
        for rule in state["rules"]:
            if rule["id"] == rule_id:
                return rule
        raise RuleError("Правило не найдено")

    def list_rules(self):
        with self._lock():
            return sorted(self._read()["rules"], key=lambda r: r["name"].casefold())

    def save_rule(self, value):
        with self._lock():
            rule = self._validate(value)
            state = self._read()
            if "id" in rule:
                old = self._find(state, rule["id"])
                if old["version"] != rule["version"]:
                    raise RuleError("Правило уже изменено; обновите список перед сохранением")
                rule["version"] += 1
            else:
                if len(state["rules"]) >= MAX_RULES:
                    raise RuleError("Не более 100 локальных правил")
                rule.update(id=uuid.uuid4().hex, version=1)
            if any(r["name"].casefold() == rule["name"].casefold() and r["id"] != rule["id"]
                   for r in state["rules"]):
                raise RuleError("Правило с таким именем уже существует")
            state["rules"] = [r for r in state["rules"] if r["id"] != rule["id"]] + [rule]
            self._atomic(state)
            return dict(rule)

    def _selection(self, rule):
        rule = self._validate(rule)
        if not rule["enabled"]:
            raise RuleError("Правило отключено")
        rows, skipped = self.engine.scan(rule["source"], max_files=SCAN_LIMIT)
        candidates, total, targets = [], 0, set()
        for row in sorted(rows, key=lambda item: item["path"].casefold()):
            p = self.engine.checked(row["path"])
            name = p.name.casefold()
            if not fnmatch.fnmatchcase(name, rule["name_glob"].casefold()):
                continue
            if rule["name_contains"].casefold() not in name:
                continue
            if rule["extensions"] and p.suffix.casefold() not in rule["extensions"]:
                continue
            stamp = {key: row[key] for key in ("size", "mtime_ns", "ino")}
            if not p.is_file() or fingerprint(p) != stamp:
                raise RuleError("Файл меняется во время просмотра; повторите просмотр")
            total += stamp["size"]
            if len(candidates) >= rule["max_files"] or total > rule["max_total_bytes"]:
                raise RuleError("Подборка превышает лимит правила; сузьте условия")
            reason = "имя: " + rule["name_glob"]
            if rule["name_contains"]:
                reason += "; содержит: " + rule["name_contains"]
            if rule["extensions"]:
                reason += "; расширение: " + p.suffix.casefold()
            candidate = dict(path=str(p), **stamp, reason=reason)
            if rule["action"] == "copy":
                target = self.engine.checked(Path(rule["destination"]) / p.name)
                if os.path.lexists(target) or str(target).casefold() in targets:
                    raise RuleError("Коллизия имени; существующие файлы не перезаписываются: " + str(target))
                targets.add(str(target).casefold())
                candidate["destination"] = str(target)
            candidates.append(candidate)
        return dict(candidates=candidates, count=len(candidates), total_bytes=total,
                    scanned_files=len(rows), skipped=skipped)

    @staticmethod
    def _prune(state):
        stamp = time.time()
        previews = {key: value for key, value in state["previews"].items()
                    if value["expires_at"] > stamp}
        while len(previews) >= MAX_PREVIEWS:
            del previews[min(previews, key=lambda k: previews[k]["created_at"])]
        state["previews"] = previews

    def preview(self, rule_id):
        with self._lock(), self.engine.lock():
            state = self._read()
            rule = self._find(state, rule_id)
            selection = self._selection(rule)
            self._prune(state)
            token, created = uuid.uuid4().hex, time.time()
            preview = dict(token=token, rule_id=rule_id, rule_version=rule["version"],
                           action=rule["action"], source=rule["source"], destination=rule["destination"],
                           created_at=created, expires_at=created + PREVIEW_SECONDS,
                           status="ready", **selection)
            state["previews"][token] = preview
            self._atomic(state)
            return dict(preview)

    def enqueue(self, rule_id, preview_token, version=None):
        with self._lock(), self.engine.lock():
            state = self._read()
            rule = self._find(state, rule_id)
            preview = state["previews"].get(preview_token)
            if not preview or preview["rule_id"] != rule_id:
                raise RuleError("Сначала создайте новый предварительный просмотр")
            if (rule["version"] != preview["rule_version"]
                    or (version is not None and version != rule["version"])):
                raise RuleError("Версия правила изменилась; повторите просмотр")
            if not rule["enabled"]:
                raise RuleError("Правило отключено")
            if preview["status"] == "queued":
                return preview["job_id"]
            if preview["expires_at"] <= time.time():
                raise RuleError("Просмотр устарел; повторите его")
            if preview["status"] != "ready":
                raise RuleError("Постановка этого плана уже началась или прервалась; проверьте очередь и сделайте новый просмотр")
            fresh = self._selection(rule)
            if fresh["candidates"] != preview["candidates"]:
                raise RuleError("Подборка изменилась; повторите просмотр")
            if not fresh["count"]:
                raise RuleError("Подходящих файлов нет")
            # Persist before creating a job: an interrupted retry must never
            # create a duplicate plan after a process crash.
            preview["status"] = "preparing"
            self._atomic(state)
            try:
                if rule["action"] == "copy":
                    job = self.engine.plan_transfer([r["path"] for r in fresh["candidates"]],
                                                    rule["destination"], mode="copy")
                    # plan_transfer hashes each source and creates a normal
                    # approval-gated plan. Reject a file changed between our
                    # snapshot check and that hashing operation.
                    planned = self.engine.get(job)["payload"]
                    expected = [(row["path"], row["destination"],
                                 {k: row[k] for k in ("size", "mtime_ns", "ino")})
                                for row in fresh["candidates"]]
                    actual = [(row["src"], row["dst"], row["stamp"])
                              for row in planned["records"]]
                    if planned.get("mode") != "copy" or actual != expected:
                        self.engine.cancel(job)
                        raise RuleError("Источник изменился при подготовке плана; план отменён, повторите просмотр")
                else:
                    records = [{key: row[key] for key in ("path", "size", "mtime_ns", "ino")}
                               for row in fresh["candidates"]]
                    job = self.engine.create("inventory", {"root": rule["source"], "rule_records": records})
            except Exception as exc:
                message = "".join(char if ord(char) >= 32 else " " for char in str(exc))[:500]
                preview.update(status="failed", error=message)
                self._atomic(state)
                raise
            preview.update(status="queued", job_id=job)
            self._atomic(state)
            return job
