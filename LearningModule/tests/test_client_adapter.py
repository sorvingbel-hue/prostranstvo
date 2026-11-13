import copy
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from learning_module.adapter import LearningDialogueAdapter
from learning_module.client import ClientError, InProcessClient, MAX_MESSAGE_BYTES, StdioClient


def response(req, **changes):
    result = dict(schema_version="1.0", module_version="0.1.0", request_id=req["request_id"],
                  status="completed", session_revision=None, result={}, evidence=[], limitations=[])
    result.update(changes)
    return result


class FakeService:
    def __init__(self):
        self.calls = []
        self.revision = 0
        self.stopped = False
        self.conflict = False

    def handle(self, req):
        self.calls.append(copy.deepcopy(req))
        if req["operation"] == "session.snapshot":
            return response(req, session_revision=self.revision, result={"session": {
                "session_id": "test-session", "revision": self.revision,
                "state": "stopped" if self.stopped else "active"}})
        if self.conflict:
            return response(req, status="conflict", session_revision=self.revision)
        if req.get("expected_revision") != self.revision:
            return response(req, status="conflict", session_revision=self.revision)
        self.revision += 1
        self.stopped = req["operation"] == "session.stop"
        return response(req, session_revision=self.revision,
                        result={"text_ru": "Проверяемая реплика", "text_en": "Checked reply",
                                "task":{"prompt_ru":"Сложи 1/3 и 1/3.", "prompt_en":"Add 1/3 and 1/3."}})


FAKE_SERVER = r'''
import json,sys,time
mode=sys.argv[1]
if mode == "no_read":time.sleep(15)
for raw in sys.stdin.buffer:
    request=json.loads(raw)
    if mode == "hang":
        time.sleep(15)
    if mode == "oversized":
        sys.stdout.buffer.write(b"x" * 65538 + b"\n");sys.stdout.buffer.flush();continue
    if mode == "broken":
        sys.stdout.buffer.write(b"not-json\n");sys.stdout.buffer.flush();continue
    answer={"schema_version":"1.0","module_version":"0.1.0","request_id":request["request_id"],"status":"completed","session_revision":None,"result":{"echo":request},"evidence":[],"limitations":[]}
    if mode == "wrong_id":answer["request_id"]="wrong-id"
    if mode == "duplicate":
        line=json.dumps(answer)[:-1]+',"status":"failed"}'
    else:line=json.dumps(answer,ensure_ascii=False)
    sys.stdout.buffer.write((line+"\n").encode("utf-8"));sys.stdout.buffer.flush()
'''


class ClientTests(unittest.TestCase):
    def test_inprocess_request_snapshot_and_revision(self):
        seen = []
        class Echo:
            def handle(self, req):
                seen.append(req)
                return response(req)
        params = {"text": "дробь"}
        result = InProcessClient(Echo()).request("module.describe", params, 3, "request-1")
        params["text"] = "changed"
        self.assertEqual(result["request_id"], "request-1")
        self.assertEqual(seen[0]["params"], {"text": "дробь"})
        self.assertEqual(seen[0]["expected_revision"], 3)

    def test_bad_revision_and_oversized_request_never_reach_service(self):
        service = FakeService()
        client = InProcessClient(service)
        for revision in (-1, True, 1.5):
            with self.assertRaises(ClientError):client.request("module.describe", expected_revision=revision)
        with self.assertRaisesRegex(ClientError, "message_too_large"):
            client.request("module.describe", {"text": "x" * MAX_MESSAGE_BYTES})
        self.assertEqual(service.calls, [])

    def test_invalid_response_id_and_nonfinite_are_rejected(self):
        class Wrong:
            def handle(self, req):return response(req, request_id="other")
        with self.assertRaisesRegex(ClientError, "response_id_mismatch"):
            InProcessClient(Wrong()).request("module.describe")
        class Nonfinite:
            def handle(self, req):return response(req, result={"number":float("nan")})
        with self.assertRaisesRegex(ClientError, "invalid_response"):
            InProcessClient(Nonfinite()).request("module.describe")

    def test_service_exception_is_sanitized(self):
        class Broken:
            def handle(self, req):raise RuntimeError("private detail must not leak")
        with self.assertRaises(ClientError) as caught:
            InProcessClient(Broken()).request("module.describe")
        self.assertEqual(str(caught.exception), "service_failed")

    def client(self, mode="normal", **kwargs):
        client = StdioClient([sys.executable, "-u", "-c", FAKE_SERVER, mode], **kwargs)
        self.addCleanup(client.close)
        return client

    def test_stdio_is_lazy_and_handles_multiple_bilingual_requests(self):
        client = self.client()
        self.assertIsNone(client.process_id)
        for request_id in ("first", "second"):
            answer = client.request("task.create", {"text":"1/2 плюс 1/3"}, request_id=request_id)
            self.assertEqual(answer["result"]["echo"]["params"]["text"], "1/2 плюс 1/3")
        self.assertIsNotNone(client.process_id)

    def test_stdio_duplicate_fields_wrong_id_and_bad_json_close_client(self):
        for mode, code in (("wrong_id", "response_id_mismatch"), ("duplicate", "invalid_response"), ("broken", "invalid_response")):
            with self.subTest(mode=mode):
                client = self.client(mode)
                with self.assertRaisesRegex(ClientError, code):client.request("module.describe")
                with self.assertRaisesRegex(ClientError, "client_closed"):client.request("module.describe")
                self.assertIsNotNone(client._process.poll())

    def test_stdio_oversized_output_is_bounded(self):
        client = self.client("oversized")
        with self.assertRaisesRegex(ClientError, "response_too_large"):
            client.request("module.describe")
        self.assertIsNotNone(client._process.poll())

    def test_stdio_timeout_terminates_owned_process_without_retry(self):
        client = self.client("hang", timeout=0.1)
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "request_timeout"):
            client.request("turn.submit", {"action":"answer", "text":"1/2"})
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(client._process.poll())
        with self.assertRaisesRegex(ClientError, "client_closed"):client.request("module.describe")

    def test_stdio_timeout_includes_blocked_stdin_write(self):
        client = self.client("no_read", timeout=0.1)
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "request_timeout"):
            client.request("task.create", {"text":"x" * 30000})
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(client._process.poll())
        self.assertFalse(client._writer_thread.is_alive())

    def test_missing_revision_in_response_is_rejected(self):
        class Missing:
            def handle(self, req):
                answer = response(req)
                del answer["session_revision"]
                return answer
        with self.assertRaisesRegex(ClientError, "invalid_response"):
            InProcessClient(Missing()).request("module.describe")

    def test_invalid_timeout_argv_and_context_close(self):
        for value in (0, 31, float("nan"), True):
            with self.assertRaises(ClientError):StdioClient([sys.executable], timeout=value)
        for value in ("python", [], [""], ["python", "bad\0arg"]):
            with self.assertRaises(ClientError):StdioClient(value)
        client = self.client()
        with client:client.request("module.describe")
        self.assertIsNotNone(client._process.poll())


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.adapter = LearningDialogueAdapter(InProcessClient(self.service), "test-session")

    def test_explicit_session_and_language_are_required(self):
        with self.assertRaises(ValueError):LearningDialogueAdapter(None, "")
        with self.assertRaises(ValueError):LearningDialogueAdapter(None, "session", "fr")
        self.assertEqual(self.service.calls, [])

    def test_answer_uses_snapshot_revision_and_maps_host_contract(self):
        self.service.revision = 4
        result = self.adapter.reply(" 3/4 ")
        self.assertEqual([c["operation"] for c in self.service.calls], ["session.snapshot", "turn.submit"])
        self.assertEqual(self.service.calls[-1]["params"], {"session_id":"test-session", "action":"answer", "text":"3/4"})
        self.assertEqual(self.service.calls[-1]["expected_revision"], 4)
        self.assertEqual(result["text"], "Проверяемая реплика")
        self.assertTrue(result["local_only"])
        self.assertEqual(result["intent"], "learning")

    def test_hint_practice_and_stop_have_explicit_commands(self):
        for text, action in (("Подсказка", "hint"), ("hint", "hint"), ("Похожая задача", "practice"), ("practice", "practice")):
            self.adapter.reply(text)
            self.assertEqual(self.service.calls[-1]["params"]["action"], action)
            self.assertNotIn("text", self.service.calls[-1]["params"])
        result = self.adapter.reply("стоп")
        self.assertEqual(self.service.calls[-1]["operation"], "session.stop")
        self.assertEqual(result["suggestions"], [])

    def test_practice_displays_actual_new_task(self):
        result = self.adapter.reply("Похожая задача")
        self.assertIn("Сложи 1/3 и 1/3.", result["text"])

    def test_stopped_session_does_not_send_another_turn(self):
        self.service.stopped = True
        self.adapter.reply("1/2")
        self.assertEqual(len(self.service.calls), 1)

    def test_conflict_does_not_retry_or_claim_answer_was_checked(self):
        self.service.conflict = True
        result = self.adapter.reply("1/2")
        self.assertEqual(len(self.service.calls), 2)
        self.assertIn("не отправлен повторно", result["text"])
        self.assertNotIn("Проверяемая", result["text"])

    def test_failure_is_sanitized_and_unchecked(self):
        class Broken:
            def request(self, *args, **kwargs):raise ClientError("secret-like internal code")
        result = LearningDialogueAdapter(Broken(), "session").reply("1/2")
        self.assertFalse(result["corpus_available"])
        self.assertIn("пока не проверен", result["text"])
        self.assertNotIn("secret", result["text"])

    def test_english_and_empty_input(self):
        result = LearningDialogueAdapter(InProcessClient(self.service), "session", "en").reply("hint")
        self.assertEqual(result["language"], "en")
        self.assertEqual(result["text"], "Checked reply")
        count = len(self.service.calls)
        result = self.adapter.reply("")
        self.assertEqual(len(self.service.calls), count)
        self.assertIn("Напиши", result["text"])


class RealServiceIntegrationTests(unittest.TestCase):
    def test_inprocess_adapter_tracks_assistance_and_practice(self):
        from learning_module.service import LearningService
        with tempfile.TemporaryDirectory(prefix="learning-adapter-test-") as directory:
            service = LearningService(Path(directory) / "data")
            try:
                client = InProcessClient(service)
                created = client.request("task.create", {"preset_id":"g4_same_1"})
                session = client.request("session.start", {
                    "task_id":created["result"]["task"]["task_id"], "learner_id":"synthetic-learner"})
                session_id = session["result"]["session"]["session_id"]
                adapter = LearningDialogueAdapter(client, session_id)
                self.assertTrue(adapter.reply("подсказка")["corpus_available"])
                adapter.reply("3/4")
                snapshot = client.request("session.snapshot", {"session_id":session_id})
                self.assertTrue(snapshot["result"]["session"]["assisted_correct"])
                self.assertFalse(snapshot["result"]["session"]["independent_correct"])
                practice_reply = adapter.reply("похожая задача")
                practice = client.request("session.snapshot", {"session_id":session_id})
                task = practice["result"]["task"]
                self.assertIn(task["prompt_ru"], practice_reply["text"])
                expected = str(Fraction(task["left"]) + Fraction(task["right"]))
                adapter.reply(expected)
                snapshot = client.request("session.snapshot", {"session_id":session_id})
                self.assertTrue(snapshot["result"]["session"]["independent_correct"])
                exported = client.request("report.export", {"session_id":session_id})
                self.assertEqual(exported["status"], "completed")
                self.assertTrue(Path(exported["result"]["export"]["path"]).is_file())
                stopped = adapter.reply("стоп")
                self.assertIn("Родительский таймер не изменён", stopped["text"])
            finally:
                service.close()

    def test_source_stdio_replay_survives_process_restart(self):
        source = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory(prefix="learning-stdio-test-") as directory:
            argv = [sys.executable, "-B", "-m", "learning_module", "--stdio", "--data-dir", str(Path(directory) / "data")]
            with StdioClient(argv, cwd=source) as client:
                self.assertTrue(client.request("module.describe")["result"]["local_only"])
                task = client.request("task.create", {"preset_id":"g5_unlike_1"})["result"]["task"]
                session = client.request("session.start", {"task_id":task["task_id"], "learner_id":"synthetic-learner"})["result"]["session"]
                params = {"session_id":session["session_id"], "action":"answer", "text":"5/6"}
                answered = client.request("turn.submit", params, expected_revision=0, request_id="stable-answer-request")
                self.assertTrue(answered["result"]["assessment"]["correct"])
                self.assertEqual(answered, client.request("turn.submit", params, expected_revision=0, request_id="stable-answer-request"))
                conflict = client.request("turn.submit", dict(params,text="1/2"), expected_revision=0, request_id="stable-answer-request")
                self.assertEqual(conflict["status"], "conflict")
            with StdioClient(argv, cwd=source) as client:
                replay = client.request("turn.submit", params, expected_revision=0, request_id="stable-answer-request")
                self.assertEqual(replay, answered)
                snapshot = client.request("session.snapshot", {"session_id":session["session_id"]})
                self.assertEqual(snapshot["result"]["session"]["attempt_count"], 1)
                self.assertEqual(snapshot["session_revision"], 1)
                adapter = LearningDialogueAdapter(client, session["session_id"], language="en")
                self.assertIn("parental timer", adapter.reply("stop")["text"].lower())


if __name__ == "__main__":
    unittest.main()
