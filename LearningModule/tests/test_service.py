from __future__ import annotations

from fractions import Fraction
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from learning_module.service import LearningService
from learning_module.__main__ import serve


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = LearningService(self.directory.name)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def call(self, operation, params=None, revision=None, request_id=None):
        request = dict(schema_version="1.0", request_id=request_id or uuid.uuid4().hex,
                       operation=operation, params=params or {})
        if revision is not None:
            request["expected_revision"] = revision
        return self.service.handle(request)

    def start(self, preset="g4_same_1"):
        task = self.call("task.create", {"preset_id": preset})["result"]["task"]
        result = self.call("session.start", {"task_id": task["task_id"], "learner_id": "test-learner"})
        self.assertEqual(result["status"], "completed")
        return result["result"]["session"], task

    def test_equivalence_and_reduction_are_separate(self):
        session, _ = self.start("g4_same_2")
        response = self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": "3/6"}, 0)
        assessment = response["result"]["assessment"]
        self.assertTrue(assessment["correct_value"])
        self.assertFalse(assessment["correct_form"])
        self.assertFalse(assessment["correct"])
        self.assertFalse(response["result"]["session"]["independent_correct"])
        response = self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": "1/2"}, 1)
        self.assertTrue(response["result"]["session"]["independent_correct"])
        self.assertEqual(response["evidence"][0]["checked_claims"], ["answer_equivalence", "simplest_form"])

    def test_invalid_notation_is_not_a_mathematical_attempt(self):
        session, _ = self.start()
        response = self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": "__import__('os')"}, 0)
        self.assertEqual(response["result"]["assessment"]["state"], "invalid")
        self.assertEqual(response["result"]["session"]["attempt_count"], 0)
        self.assertEqual(response["evidence"], [])

    def test_replay_is_persistent_and_never_adds_an_attempt(self):
        session, _ = self.start()
        params = {"session_id": session["session_id"], "action": "answer", "text": "3/4"}
        original = self.call("turn.submit", params, 0, "persisted-answer")
        self.service.close()
        self.service = LearningService(self.directory.name)
        replay = self.call("turn.submit", params, 0, "persisted-answer")
        self.assertEqual(original, replay)
        self.assertEqual(len(self.service.repository.events(session["session_id"])), 1)
        changed = self.call("turn.submit", dict(params, text="1/4"), 0, "persisted-answer")
        self.assertEqual(changed["status"], "conflict")

    def test_stale_revision_and_stopped_session_do_not_mutate(self):
        session, _ = self.start()
        stopped = self.call("session.stop", {"session_id": session["session_id"]}, 0)
        self.assertEqual(stopped["result"]["session"]["state"], "stopped")
        stale = self.call("turn.submit", {"session_id": session["session_id"], "action": "hint"}, 0)
        self.assertEqual(stale["status"], "conflict")
        after = self.call("turn.submit", {"session_id": session["session_id"], "action": "hint"}, 1)
        self.assertEqual(after["status"], "unavailable")
        self.assertEqual(self.service.repository.session(session["session_id"])["revision"], 1)

    def test_receipt_failure_rolls_back_the_entire_attempt(self):
        session, _ = self.start()
        with patch.object(self.service.repository, "cache", side_effect=sqlite3.OperationalError("private disk detail")):
            response = self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": "3/4"}, 0)
        self.assertEqual(response["status"], "unavailable")
        self.assertNotIn("private disk detail", json.dumps(response))
        saved = self.service.repository.session(session["session_id"])
        self.assertEqual(saved["revision"], 0)
        self.assertEqual(saved["attempt_count"], 0)
        self.assertEqual(self.service.repository.events(session["session_id"]), [])

    def test_assisted_answer_then_new_task_reset(self):
        session, task = self.start("g5_unlike_1")
        session_id = session["session_id"]
        for revision in range(4):
            response = self.call("turn.submit", {"session_id": session_id, "action": "hint"}, revision)
        response = self.call("turn.submit", {"session_id": session_id, "action": "answer", "text": "5/6"}, 4)
        self.assertTrue(response["result"]["session"]["assisted_correct"])
        self.assertFalse(response["result"]["session"]["independent_correct"])
        next_task = self.call("turn.submit", {"session_id": session_id, "action": "practice"}, 5)
        self.assertEqual(next_task["result"]["session"]["hint_level"], 0)
        self.assertFalse(next_task["result"]["session"]["assisted_correct"])
        self.assertNotEqual(task["task_id"], next_task["result"]["task"]["task_id"])

    def test_export_recovers_after_receipt_failure_and_is_self_contained(self):
        session, old_task = self.start()
        self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": "3/4"}, 0)
        practice = self.call("turn.submit", {"session_id": session["session_id"], "action": "practice"}, 1)
        with patch.object(self.service.repository, "cache", side_effect=sqlite3.OperationalError("failure")):
            failed = self.call("report.export", {"session_id": session["session_id"]}, request_id="export-retry")
        self.assertEqual(failed["status"], "unavailable")
        successful = self.call("report.export", {"session_id": session["session_id"]}, request_id="export-retry")
        self.assertEqual(successful["status"], "completed")
        path = Path(successful["result"]["export"]["path"])
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["tasks"][old_task["task_id"]]["prompt_ru"], old_task["prompt_ru"])
        self.assertIn(practice["result"]["task"]["task_id"], report["tasks"])
        self.assertEqual(successful, self.call("report.export", {"session_id": session["session_id"]}, request_id="export-retry"))
        self.assertEqual(len(list(path.parent.glob("*.json"))), 1)

    def test_modified_export_is_not_overwritten(self):
        session, _ = self.start()
        with patch.object(self.service.repository, "cache", side_effect=sqlite3.OperationalError("failure")):
            self.call("report.export", {"session_id": session["session_id"]}, request_id="edited-export")
        path = next((Path(self.directory.name) / "exports").glob("*.json"))
        path.write_text("edited by owner", encoding="utf-8")
        response = self.call("report.export", {"session_id": session["session_id"]}, request_id="edited-export")
        self.assertEqual(response["status"], "unavailable")
        self.assertEqual(path.read_text(encoding="utf-8"), "edited by owner")

    def test_no_network_for_whole_local_workflow(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            session, task = self.start()
            answer = str(Fraction(task["left"]) + Fraction(task["right"]))
            self.assertEqual(self.call("turn.submit", {"session_id": session["session_id"], "action": "answer", "text": answer}, 0)["status"], "completed")
            self.assertEqual(self.call("report.export", {"session_id": session["session_id"]})["status"], "completed")

    def test_protocol_rejects_duplicate_fields_and_oversized_lines(self):
        destination = io.BytesIO()
        source = io.BytesIO(b'{"request_id":"one","request_id":"two"}\n')
        self.assertEqual(serve(self.service, source, destination), 0)
        response = json.loads(destination.getvalue())
        self.assertEqual(response["status"], "needs_clarification")
        destination = io.BytesIO()
        self.assertEqual(serve(self.service, io.BytesIO(b"x" * 33000 + b"\n"), destination), 2)
        self.assertEqual(destination.getvalue(), b"")

    def test_unsupported_task_and_os_operation_are_rejected(self):
        self.assertEqual(self.call("task.create", {"left": "do homework", "right": "secret"})["status"], "unsupported")
        self.assertEqual(self.call("windows.unlock", {})["status"], "unsupported")
        self.assertEqual(self.call("task.create", {"left": "1/4", "right": "2/4", "execute_command": "anything"})["status"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
