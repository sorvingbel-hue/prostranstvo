"""Versioned local application service. Does not control Windows or a pet."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid

from . import __version__, CONTRACT_VERSION
from .content import make_task, select_preset, check_answer, hint, practice_task, list_presets
from .fractions import MathInputError
from .storage import LocalRepository, encode

MAX_REQUEST_BYTES = 32768
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MUTATIONS = {"task.create", "session.start", "turn.submit", "session.stop", "report.export"}
OPERATIONS = MUTATIONS | {"module.describe", "session.snapshot", "history.list"}
LIMITATIONS = ["Only supported fraction addition, not arbitrary homework.",
               "No cloud AI, microphone, school integration or Windows control.",
               "Local Windows account access is not parent authentication.",
               "Arithmetic tests do not establish learning effectiveness."]


class RequestError(Exception):
    def __init__(self, code, status="needs_clarification"):
        super().__init__(code)
        self.code, self.status = code, status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RequestError("invalid_identifier")
    return value


def fields(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or not set(required) <= set(value):
        raise RequestError("invalid_fields")


ERROR_TEXT = {
    "invalid_fields": ("Проверь поля запроса. Этот формат пока не поддерживается.", "Check the request fields. This format is not supported."),
    "invalid_request": ("Не удалось прочитать запрос. Попробуй ещё раз.", "The request could not be read. Try again."),
    "invalid_identifier": ("Выбери задание или занятие из списка.", "Choose a task or session from the list."),
    "unsupported_operation": ("Этой возможности пока нет в учебном модуле.", "This learning feature is not available yet."),
    "unsupported_version": ("Версии учебного модуля и клиента несовместимы.", "The learning module and client versions do not match."),
    "task_not_found": ("Задание не найдено. Выбери его заново.", "Task not found. Select it again."),
    "session_not_found": ("Занятие не найдено. Начни новое.", "Session not found. Start a new one."),
    "session_stopped": ("Занятие остановлено. Можно начать новое.", "This session has stopped. You can start a new one."),
    "revision_conflict": ("Занятие уже изменилось. Обнови его перед следующей попыткой.", "This session changed. Refresh before your next attempt."),
    "request_conflict": ("Этот запрос уже использован для другого действия. Ответ не изменён.", "This request was already used for a different action. No answer was changed."),
    "storage_limit": ("Достигнут лимит локального прототипа. История сохранена.", "This local prototype reached its storage limit. Existing history is preserved."),
    "storage_unavailable": ("Не получилось сохранить занятие. Попытка не подтверждена; обнови историю.", "The session could not be saved. The attempt is not confirmed; refresh history."),
    "internal_error": ("Учебный модуль временно недоступен. Попробуй обновить занятие.", "The learning module is temporarily unavailable. Try refreshing the session."),
}


class LearningService:
    def __init__(self, data_dir):
        self.repository = LocalRepository(data_dir)

    def close(self):
        self.repository.close()

    def _response(self, request_id, status="completed", result=None, revision=None):
        result = {} if result is None else result
        assessment = result.get("assessment", {})
        evidence = []
        if assessment.get("state") == "verified":
            evidence.append({"kind": "deterministic_check", "checker": assessment["checker"],
                             "checked_claims": assessment["checked_claims"],
                             "scope": "This numeric attempt only; not mastery or teaching effectiveness."})
        return {"schema_version": CONTRACT_VERSION, "module_version": __version__,
                "request_id": request_id, "status": status, "session_revision": revision,
                "result": result, "evidence": evidence, "limitations": list(LIMITATIONS)}

    def handle(self, request):
        request_id = request.get("request_id") if type(request) is dict else None
        if not isinstance(request_id, str) or len(request_id) > 64:
            request_id = None
        try:
            fields(request, {"schema_version", "request_id", "operation", "params", "expected_revision"}, {"schema_version", "request_id", "operation", "params"})
            identifier(request["request_id"])
            if len(encode(request).encode("utf-8")) > MAX_REQUEST_BYTES:
                raise RequestError("invalid_request")
            if request["schema_version"] != CONTRACT_VERSION:
                raise RequestError("unsupported_version", "unsupported")
            operation, params = request["operation"], request["params"]
            if not isinstance(operation, str) or operation not in OPERATIONS:
                raise RequestError("unsupported_operation", "unsupported")
            if type(params) is not dict:
                raise RequestError("invalid_fields")
            if operation not in MUTATIONS:
                result, revision = self._dispatch(operation, params, request)
                return self._response(request_id, result=result, revision=revision)
            fingerprint = hashlib.sha256(encode({key: value for key, value in request.items() if key != "request_id"}).encode("utf-8")).hexdigest()
            with self.repository.transaction():
                cached = self.repository.cached(request_id)
                if cached is not None:
                    if cached[0] != fingerprint:
                        raise RequestError("request_conflict", "conflict")
                    return cached[1]
                result, revision = self._dispatch(operation, params, request)
                response = self._response(request_id, result=result, revision=revision)
                self.repository.cache(request_id, fingerprint, response)
                return response
        except RequestError as error:
            ru, en = ERROR_TEXT.get(error.code, ERROR_TEXT["invalid_request"])
            return self._response(request_id, error.status, {"code": error.code, "text_ru": ru, "text_en": en})
        except MathInputError:
            return self._response(request_id, "unsupported", {"code": "unsupported_task", "text_ru": "Пока умею складывать две неотрицательные дроби меньше единицы. Проверь запись: например, 1/4 и 2/4.", "text_en": "I can currently add two nonnegative proper fractions. Check the notation, for example 1/4 and 2/4."})
        except OverflowError:
            ru, en = ERROR_TEXT["storage_limit"]
            return self._response(request_id, "unavailable", {"code": "storage_limit", "text_ru": ru, "text_en": en})
        except (OSError, sqlite3.Error):
            ru, en = ERROR_TEXT["storage_unavailable"]
            return self._response(request_id, "unavailable", {"code": "storage_unavailable", "text_ru": ru, "text_en": en})
        except (TypeError, ValueError, UnicodeError, RecursionError):
            ru, en = ERROR_TEXT["invalid_request"]
            return self._response(request_id, "needs_clarification", {"code": "invalid_request", "text_ru": ru, "text_en": en})
        except Exception:
            ru, en = ERROR_TEXT["internal_error"]
            return self._response(request_id, "failed", {"code": "internal_error", "text_ru": ru, "text_en": en})

    def _session(self, params, request=None):
        session = self.repository.session(identifier(params.get("session_id")))
        if session is None:
            raise RequestError("session_not_found")
        if request is not None:
            revision = request.get("expected_revision")
            if type(revision) is not int or revision != session["revision"]:
                raise RequestError("revision_conflict", "conflict")
        return session

    def _snapshot(self, session, **extra):
        return dict(session=session, task=self.repository.task(session["task_id"]), **extra), session["revision"]

    def _dispatch(self, operation, params, request):
        if operation == "module.describe":
            fields(params, ())
            return {"runtime_available": True, "local_only": True, "presets": list_presets(),
                    "capabilities": ["fraction_addition", "hint_ladder", "local_history", "local_export"],
                    "text_ru": "Локальный прототип: сложение дробей, подсказки и история занятий.",
                    "text_en": "Local prototype: fraction addition, hints and session history."}, None
        if operation == "task.create":
            fields(params, {"preset_id", "left", "right", "require_simplest"})
            if "preset_id" in params:
                if set(params) != {"preset_id"}:
                    raise RequestError("invalid_fields")
                task = select_preset(identifier(params["preset_id"]))
            else:
                if not {"left", "right"} <= set(params) or type(params.get("require_simplest", True)) is not bool:
                    raise RequestError("invalid_fields")
                task = make_task(params["left"], params["right"], params.get("require_simplest", True))
            task["task_id"] = uuid.uuid4().hex
            self.repository.put_task(task)
            return {"task": task, "text_ru": task["prompt_ru"], "text_en": task["prompt_en"]}, None
        if operation == "session.start":
            fields(params, {"task_id", "learner_id"}, {"task_id", "learner_id"})
            task_id = identifier(params["task_id"])
            learner_id = identifier(params["learner_id"])
            if self.repository.task(task_id) is None:
                raise RequestError("task_not_found")
            timestamp = now()
            session = {"session_id": uuid.uuid4().hex, "task_id": task_id,
                       "learner_id": learner_id, "state": "active", "revision": 0,
                       "attempt_count": 0, "hint_level": 0, "independent_correct": False,
                       "assisted_correct": False, "started_at": timestamp, "updated_at": timestamp}
            self.repository.create_session(session)
            return self._snapshot(session, text_ru="Попробуй сам или попроси подсказку.", text_en="Try it yourself or ask for a hint.")
        if operation == "session.snapshot":
            fields(params, {"session_id"}, {"session_id"})
            return self._snapshot(self._session(params))
        if operation == "history.list":
            fields(params, ())
            return {"history": self.repository.history()}, None
        if operation == "report.export":
            fields(params, {"session_id"}, {"session_id"})
            session = self._session(params)
            export = self.repository.export_report(session, request["request_id"], now())
            return {"export": export, "text_ru": "Сводка сохранена локально. Общая учётная запись Windows не защищает её от других пользователей этой учётки.", "text_en": "Report saved locally. Other people using this Windows account may access it."}, session["revision"]
        if operation == "session.stop":
            fields(params, {"session_id"}, {"session_id"})
            session = self._session(params, request)
            if session["state"] != "stopped":
                session.update(state="stopped", revision=session["revision"] + 1, updated_at=now())
                self.repository.update_session(session)
                self.repository.add_event(session["session_id"], {"kind": "session_stopped", "at_utc": session["updated_at"], "revision": session["revision"]})
            return self._snapshot(session, text_ru="Занятие остановлено. Родительский таймер не изменён.", text_en="Session stopped. The parental timer was not changed.")
        fields(params, {"session_id", "action", "text"}, {"session_id", "action"})
        session = self._session(params, request)
        if session["state"] != "active":
            raise RequestError("session_stopped", "unavailable")
        task = self.repository.task(session["task_id"])
        action = params["action"]
        result = {}
        event = {"kind": action, "task_id": task["task_id"], "at_utc": now()}
        if action == "answer":
            text = params.get("text")
            if not isinstance(text, str) or len(text) > 80:
                raise RequestError("invalid_fields")
            assessment = check_answer(task, text)
            result = {"assessment": assessment, "text_ru": assessment["text_ru"], "text_en": assessment["text_en"]}
            event.update(answer=text, assessment=assessment, assisted=session["hint_level"] > 0)
            if assessment["state"] == "verified":
                session["attempt_count"] += 1
                if assessment["correct"]:
                    key = "assisted_correct" if session["hint_level"] > 0 else "independent_correct"
                    session[key] = True
        elif action == "hint":
            if "text" in params:
                raise RequestError("invalid_fields")
            session["hint_level"] = min(4, session["hint_level"] + 1)
            suggestion = hint(task, session["hint_level"])
            result = {"text_ru": suggestion["text_ru"], "text_en": suggestion["text_en"], "visual": suggestion.get("visual"), "hint": suggestion}
            event.update(level=session["hint_level"], hint=suggestion)
        elif action == "practice":
            if "text" in params:
                raise RequestError("invalid_fields")
            new_task = practice_task(task)
            new_task["task_id"] = uuid.uuid4().hex
            self.repository.put_task(new_task)
            session.update(task_id=new_task["task_id"], hint_level=0, independent_correct=False, assisted_correct=False)
            event.update(new_task_id=new_task["task_id"])
            result = {"text_ru": "Новый похожий пример. Попробуй без подсказок.", "text_en": "A new similar task. Try it without hints."}
        else:
            raise RequestError("unsupported_operation", "unsupported")
        session.update(revision=session["revision"] + 1, updated_at=event["at_utc"])
        event["revision"] = session["revision"]
        self.repository.add_event(session["session_id"], event)
        self.repository.update_session(session)
        return self._snapshot(session, **result)
