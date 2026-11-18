"""Owned-profile handoff with temporary files and mocked launches/Study only."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import browser_session as session


class BrowserSessionMockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-session-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.local = self.fixture / "Local"
        self.root = self.local / "ProstranstvoAgentBrowser"
        self.marker = self.root / "manual-login.json"
        self.edge = self.fixture / "ProgramFiles/Microsoft/Edge/Application/msedge.exe"
        self.edge.parent.mkdir(parents=True)
        self.edge.write_text("Mock exe, never executed", encoding="utf-8")
        self.study = Mock()
        self.study.root = self.fixture / "Study"
        self.study.read.return_value = None
        self.study.remaining_seconds.return_value = 0
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}).start()
        patch.object(session, "known_application_paths", return_value={"edge": (self.edge,)}).start()
        patch.object(session, "StudyResume", return_value=self.study).start()
        self.actual_spawn = patch.object(session.subprocess, "Popen",
                                         side_effect=AssertionError("Real processes forbidden in these tests")).start()

    def launch_mock(self, spawn=None):
        spawn = Mock(return_value=SimpleNamespace(pid=12345)) if spawn is None else spawn
        result = session.launch_manual_login(foreground_authorized=True, spawn=spawn)
        return result, spawn

    def finish(self, result):
        return session.finish_manual_login(user_done=True, manual_session_id=result["manual_session_id"])

    def test_prepare_and_pending_are_read_only_and_use_exact_owned_profile(self):
        expected = self.root / "profiles/edge"
        self.assertEqual(session.owned_profile_path(), expected)
        self.assertFalse(session.manual_login_pending())
        plan = session.prepare_manual_login()
        self.assertEqual(plan["status"], "prepared_not_started")
        self.assertEqual(plan["argv"], [str(self.edge), "--user-data-dir=" + str(expected),
                                        "https://www.facebook.com/"])
        self.assertEqual(plan["cwd"], str(self.edge.parent))
        self.assertFalse(plan["shell"])
        self.assertFalse(plan["process_started"])
        self.assertFalse(plan["login_verified"])
        self.assertIsNone(plan["manual_session_id"])
        self.assertFalse(self.local.exists())
        self.assertFalse(self.study.root.exists())
        self.actual_spawn.assert_not_called()

    def test_every_launch_requires_literal_true_before_any_file_writes(self):
        spawn = Mock()
        for value in (False, None, 1, "true", {}):
            result = session.launch_manual_login(foreground_authorized=value, spawn=spawn)
            self.assertEqual(result["status"], "blocked_focus_window")
        self.assertFalse(self.local.exists())
        self.assertFalse(self.study.root.exists())
        self.study.read.assert_not_called()
        spawn.assert_not_called()

    def test_active_or_unreadable_study_blocks_before_lock_or_marker_writes(self):
        self.study.read.return_value = {"mock": "active lease"}
        self.study.remaining_seconds.return_value = 60
        spawn = Mock()
        result = session.launch_manual_login(foreground_authorized=True, spawn=spawn)
        self.assertEqual(result["status"], "blocked_study_active")
        self.assertFalse(self.local.exists())
        self.study.remaining_seconds.return_value = float("inf")
        self.assertEqual(session.launch_manual_login(True, spawn)["status"], "blocked_study_active")
        self.study.read.side_effect = ValueError("mock corrupt Study lease")
        with self.assertRaises(ValueError):
            session.launch_manual_login(True, spawn)
        self.assertFalse(self.local.exists())
        self.assertFalse(self.study.root.exists())
        spawn.assert_not_called()

    def test_study_is_checked_again_under_profile_lease_before_marker_write(self):
        spawn = Mock()
        with patch.object(session, "_study_active", side_effect=[False, True]), \
             patch.object(session, "_write_marker") as write:
            result = session.launch_manual_login(True, spawn)
        self.assertEqual(result["status"], "blocked_study_active")
        write.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_missing_edge_is_fail_closed_and_does_not_create_profile_state(self):
        self.edge.unlink()
        self.assertEqual(session.prepare_manual_login()["status"], "blocked_edge_not_found")
        spawn = Mock()
        self.assertEqual(session.launch_manual_login(True, spawn)["status"], "blocked_edge_not_found")
        spawn.assert_not_called()
        self.assertFalse(self.local.exists())

    def test_mock_launch_persists_marker_before_spawn_uses_safe_argv_and_releases_lease(self):
        def launch(argv, **kwargs):
            self.assertTrue(session.manual_login_pending())
            self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "manual_login")
            with self.assertRaises(RuntimeError):
                with session.profile_lease(headless=False):
                    self.fail("Concurrent profile acquisition must fail")
            with self.assertRaises((RuntimeError, PermissionError)):
                with session.exclusive_lock(self.study.root / "session.lock", blocking=False):
                    self.fail("Study activation must be locked through process creation")
            return SimpleNamespace(pid=12345)
        spawn = Mock(side_effect=launch)
        result, _ = self.launch_mock(spawn)
        expected = self.root / "profiles/edge"
        spawn.assert_called_once_with([str(self.edge), "--user-data-dir=" + str(expected),
                                       "https://www.facebook.com/"], cwd=str(self.edge.parent), shell=False)
        self.assertEqual(result["status"], "manual_login_started")
        self.assertTrue(result["process_started"])
        self.assertFalse(result["controllable"])
        self.assertFalse(result["login_verified"])
        self.assertEqual(result["runtime_status"], "launch_requested_not_verified")
        self.assertFalse(expected.exists(), "The mock launcher must not create a browser profile")
        with session.profile_lease(headless=False) as path:
            self.assertEqual(path, expected)
        with self.assertRaisesRegex(RuntimeError, "manual_login_pending"):
            with session.profile_lease():
                self.fail("Headless must not acquire a manual-login profile")
        self.assertFalse(self.marker.is_relative_to(expected))
        self.actual_spawn.assert_not_called()
        with session.exclusive_lock(self.study.root / "session.lock", blocking=False):
            pass

    def test_held_study_session_lock_blocks_launch_before_marker_or_spawn(self):
        self.study.root.mkdir()
        spawn = Mock()
        with session.exclusive_lock(self.study.root / "session.lock", blocking=False):
            with self.assertRaises(RuntimeError):
                session.launch_manual_login(foreground_authorized=True, spawn=spawn)
        self.assertFalse(self.marker.exists())
        spawn.assert_not_called()

    def test_second_launch_keeps_existing_marker_and_returns_same_ticket(self):
        first, _ = self.launch_mock()
        original = self.marker.read_bytes()
        spawn = Mock()
        second = session.launch_manual_login(True, spawn)
        self.assertEqual(second["status"], "blocked_manual_login_pending")
        self.assertEqual(second["manual_session_id"], first["manual_session_id"])
        self.assertEqual(session.prepare_manual_login()["manual_session_id"], first["manual_session_id"])
        self.assertEqual(self.marker.read_bytes(), original)
        spawn.assert_not_called()

    def test_default_popen_path_is_mocked_and_reached_only_after_guards(self):
        session.launch_manual_login()
        self.actual_spawn.assert_not_called()
        self.actual_spawn.side_effect = None
        self.actual_spawn.return_value = SimpleNamespace(pid=456)
        result = session.launch_manual_login(foreground_authorized=True)
        self.assertEqual(result["status"], "manual_login_started")
        self.actual_spawn.assert_called_once_with(
            [str(self.edge), "--user-data-dir=" + str(session.owned_profile_path()),
             "https://www.facebook.com/"], cwd=str(self.edge.parent), shell=False)

    def test_spawn_error_leaves_failed_marker_until_explicit_matching_finish(self):
        result, _ = self.launch_mock(Mock(side_effect=OSError("mock spawn failure")))
        self.assertEqual(result["status"], "launch_failed")
        self.assertFalse(result["process_started"])
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "failed")
        self.assertTrue(session.manual_login_pending())
        with self.assertRaises(RuntimeError):
            with session.profile_lease():
                pass
        finished = self.finish(result)
        self.assertEqual(finished["status"], "manual_login_finished")
        self.assertFalse(finished["login_verified"])
        self.assertFalse(session.manual_login_pending())

    def test_failed_marker_update_retains_initial_durable_manual_state(self):
        replace = session.os.replace
        calls = []
        def interrupted(source, destination):
            calls.append(destination)
            if len(calls) > 1:
                raise OSError("mock disk full after initial marker")
            return replace(source, destination)
        with patch.object(session.os, "replace", side_effect=interrupted):
            result, _ = self.launch_mock(Mock(side_effect=OSError("mock spawn failure")))
        self.assertEqual(result["status"], "launch_failed")
        self.assertTrue(session.manual_login_pending())
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "manual_login")
        self.assertEqual(list(self.root.glob(".manual-login-*.tmp")), [])

    def test_initial_marker_write_failure_never_spawns(self):
        spawn = Mock()
        with patch.object(session.os, "replace", side_effect=OSError("mock interruption")):
            with self.assertRaises(OSError):
                session.launch_manual_login(True, spawn)
        spawn.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(self.root.glob(".manual-login-*.tmp")), [])

    def test_finish_requires_confirmation_and_matching_ticket_preserving_profile_files(self):
        result, _ = self.launch_mock()
        profile = session.owned_profile_path()
        profile.mkdir(parents=True)
        sentinel = profile / "SingletonLock"
        sentinel.write_bytes(b"synthetic test sentinel; not a Chromium lock")
        for confirmation in (False, None, 1, "yes"):
            self.assertEqual(session.finish_manual_login(user_done=confirmation,
                            manual_session_id=result["manual_session_id"])["status"],
                             "blocked_user_confirmation_required")
        for ticket in (None, 1, "0" * 32):
            blocked = session.finish_manual_login(user_done=True, manual_session_id=ticket)
            self.assertEqual(blocked["status"], "blocked_manual_session_changed")
            self.assertTrue(blocked["manual_login_pending"])
        finished = self.finish(result)
        self.assertFalse(finished["login_verified"])
        self.assertEqual(finished["profile_availability"], "not_verified")
        self.assertTrue(finished["headless_verification_required"])
        self.assertEqual(sentinel.read_bytes(), b"synthetic test sentinel; not a Chromium lock")
        self.assertFalse(self.marker.exists())
        with session.profile_lease():
            pass

    def test_stale_finish_cannot_delete_new_manual_session(self):
        first, _ = self.launch_mock()
        self.finish(first)
        second, _ = self.launch_mock()
        self.assertNotEqual(first["manual_session_id"], second["manual_session_id"])
        blocked = self.finish(first)
        self.assertEqual(blocked["status"], "blocked_manual_session_changed")
        self.assertEqual(session.prepare_manual_login()["manual_session_id"], second["manual_session_id"])
        self.finish(second)
        # Finishing an already absent marker remains unverified and harmless.
        self.assertEqual(self.finish(first)["status"], "manual_login_finished")

    def test_corrupt_marker_fails_closed_for_status_headless_and_explicit_finish(self):
        self.root.mkdir(parents=True)
        valid = {"version": 1, "state": "manual_login", "session_id": "a" * 32, "created_utc": 1}
        invalid = [[], {**valid, "version": True}, {**valid, "version": 2},
                   {**valid, "state": "finished"}, {**valid, "session_id": "other"},
                   {**valid, "created_utc": True}, {**valid, "created_utc": 0},
                   {**valid, "created_utc": 10 ** 1000},
                   {**valid, "extra": 1}]
        raw = [json.dumps(value).encode("utf-8") for value in invalid]
        raw += [b"{broken", b" " * 2049, b"\xff", json.dumps({**valid, "created_utc": float("nan")}).encode(),
                b'{"version":1,"version":1,"state":"manual_login","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_utc":1}']
        for payload in raw:
            self.marker.write_bytes(payload)
            with self.subTest(payload=payload[:60]):
                with self.assertRaises(ValueError):
                    session.manual_login_pending()
                with self.assertRaises(ValueError):
                    session.prepare_manual_login()
                with self.assertRaises(ValueError):
                    with session.profile_lease():
                        pass
                with self.assertRaises(ValueError):
                    session.finish_manual_login(user_done=True, manual_session_id="a" * 32)
                self.assertEqual(self.marker.read_bytes(), payload)
        self.actual_spawn.assert_not_called()

    def test_link_checks_fail_closed_without_rewriting_marker(self):
        result, _ = self.launch_mock()
        original = self.marker.read_bytes()
        check = session.no_links
        def reject_marker(path, *args):
            if Path(path) == self.marker:
                raise ValueError("mock linked marker")
            return check(path, *args)
        with patch.object(session, "no_links", side_effect=reject_marker):
            with self.assertRaises(ValueError):
                session.manual_login_pending()
            with self.assertRaises(ValueError):
                self.finish(result)
        self.assertEqual(self.marker.read_bytes(), original)

    def test_profile_lease_is_nonblocking_exclusive_and_released_on_exception(self):
        with self.assertRaisesRegex(RuntimeError, "mock body failed"):
            with session.profile_lease():
                with self.assertRaises(RuntimeError):
                    with session.profile_lease(headless=False):
                        pass
                raise RuntimeError("mock body failed")
        with session.profile_lease():
            pass
        self.assertFalse(session.owned_profile_path().exists())
        with self.assertRaises(ValueError):
            with session.profile_lease(headless=1):
                pass

    def test_relative_local_root_and_linked_edge_are_not_accepted(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "relative"}):
            with self.assertRaises(ValueError):
                session.prepare_manual_login()
        check = session.no_links
        def reject_edge(path, *args):
            if Path(path) == self.edge:
                raise ValueError("mock linked executable")
            return check(path, *args)
        with patch.object(session, "no_links", side_effect=reject_edge):
            self.assertEqual(session.prepare_manual_login()["status"], "blocked_edge_not_found")
        self.assertFalse(self.local.exists())


if __name__ == "__main__":
    unittest.main()
