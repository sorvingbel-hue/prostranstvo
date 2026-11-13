"""Explicit learning-session adapter for a host's dialogue.reply interface.

This does not activate learning from arbitrary chat or change a study timer.
The host selects a session and decides when the adapter is used.
"""
from __future__ import annotations

from .client import ClientError


class LearningDialogueAdapter:
    def __init__(self, client, session_id, language="ru"):
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 64:
            raise ValueError("explicit_session_required")
        if language not in ("ru", "en"):
            raise ValueError("unsupported_language")
        self.client = client
        self.session_id = session_id
        self.language = language

    def _answer(self, text, available=True, *, stopped=False):
        suggestions = [] if stopped else (["Подсказка", "Похожая задача", "Стоп"]
            if self.language == "ru" else ["Hint", "Practice", "Stop"])
        return dict(text=text, intent="learning", suggestions=suggestions,
                    language=self.language, local_only=True, corpus_available=available)

    def _unavailable(self):
        return self._answer(
            "Учебный модуль сейчас недоступен. Ответ пока не проверен. Можно вернуться к заданию позже или написать родителю."
            if self.language == "ru" else
            "The learning module is unavailable. Your answer has not been checked. You can return later or contact a parent.",
            available=False)

    def reply(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return self._answer("Напиши ответ или выбери подсказку." if self.language == "ru"
                                else "Enter an answer or choose a hint.")
        try:
            snapshot = self.client.request("session.snapshot", {"session_id": self.session_id})
            if snapshot.get("status") != "completed":
                return self._unavailable()
            session = snapshot.get("result", {}).get("session", {})
            if session.get("state") == "stopped":
                return self._answer("Это занятие остановлено. Новое можно открыть в учебном окне."
                    if self.language == "ru" else
                    "This session is stopped. You can start a new one in the learning window.", stopped=True)
            revision = snapshot.get("session_revision")
            if revision is None:
                revision = session.get("revision")
            if type(revision) is not int or revision < 0:
                return self._unavailable()
            command = text.strip().casefold()
            if command in ("стоп", "stop"):
                operation, params = "session.stop", {"session_id": self.session_id}
            elif command in ("подсказка", "hint"):
                operation, params = "turn.submit", {"session_id": self.session_id, "action": "hint"}
            elif command in ("похожая задача", "practice"):
                operation, params = "turn.submit", {"session_id": self.session_id, "action": "practice"}
            else:
                operation, params = "turn.submit", {"session_id": self.session_id, "action": "answer", "text": text.strip()}
            response = self.client.request(operation, params, expected_revision=revision)
            if response.get("status") == "conflict":
                return self._answer("Задание изменилось в другом окне. Ответ не отправлен повторно; проверь текущее задание."
                    if self.language == "ru" else
                    "The task changed in another window. Your answer was not retried; check the current task.")
            if response.get("status") not in ("completed", "needs_clarification", "unsupported", "cancelled"):
                return self._unavailable()
            result = response.get("result", {})
            reply_text = result.get("text_" + self.language)
            if operation == "turn.submit" and params.get("action") == "practice" and response.get("status") == "completed":
                prompt = result.get("task", {}).get("prompt_" + self.language)
                if not isinstance(prompt, str) or not prompt.strip():
                    return self._unavailable()
                reply_text = str(reply_text or "") + "\n\n" + prompt
            if not isinstance(reply_text, str) or not reply_text.strip() or len(reply_text) > 4000:
                return self._unavailable()
            return self._answer(reply_text, stopped=(operation == "session.stop" and response.get("status") == "completed"))
        except (ClientError, ValueError, TypeError, KeyError, AttributeError):
            return self._unavailable()
