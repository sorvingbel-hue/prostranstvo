"""Existing host seam, with an isolated log and no active desktop or family data."""
from fractions import Fraction
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent / "WindowsCompanion"))
from learning_module.service import LearningService
from learning_module.client import InProcessClient
from learning_module.adapter import LearningDialogueAdapter
HOST_AVAILABLE = (ROOT.parent / "WindowsCompanion" / "kitten_messenger.py").is_file()
if HOST_AVAILABLE:
    from kitten_messenger import LocalConversation


class IsolatedLog:
    def __init__(self):
        self.records = []
        self.fail_once = False

    def append_many(self, records):
        if self.fail_once:
            self.fail_once = False
            raise OSError("isolated log failure")
        self.records.extend(records)


@unittest.skipUnless(HOST_AVAILABLE, "Optional host is absent from the standalone module")
class HostBoundaryTests(unittest.TestCase):
    def test_current_host_can_retry_its_log_without_advancing_learning_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            service = LearningService(directory)
            try:
                client = InProcessClient(service)
                task = client.request("task.create", {"preset_id": "g4_same_1"})["result"]["task"]
                session = client.request("session.start", {"task_id": task["task_id"], "learner_id": "fixture"})["result"]["session"]
                adapter = LearningDialogueAdapter(client, session["session_id"])
                log = IsolatedLog()
                conversation = LocalConversation(adapter, log, "isolated-host-session")
                log.fail_once = True
                answer = str(Fraction(task["left"]) + Fraction(task["right"]))
                with self.assertRaises(OSError):
                    conversation.submit(answer, "same-host-request")
                result = conversation.submit(answer, "same-host-request")
                self.assertEqual(result["intent"], "learning")
                self.assertTrue(result["history_saved"])
                snapshot = client.request("session.snapshot", {"session_id": session["session_id"]})
                self.assertEqual(snapshot["session_revision"], 1)
                self.assertEqual(snapshot["result"]["session"]["attempt_count"], 1)
                self.assertEqual(len(log.records), 2)
                self.assertEqual({record["role"] for record in log.records}, {"user", "kitten"})
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
