import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Study scheduling and settings regressions using a synthetic desktop only."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from study_core import StudySession, StudySettings


@dataclass(frozen=True)
class Window:
    handle: int = 10
    pid: int = 100
    process_started: int = 1234
    executable: str = r'C:\Apps\Game.exe'
    class_name: str = 'FixtureWindow'
    title: str = 'Synthetic fixture'

    @property
    def identity(self):
        return self.handle, self.pid, self.process_started, self.class_name


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Desktop:
    def __init__(self):
        self.windows = [Window()]
        self.closed = []
        self.enumerations = 0
        self.on_list = None
        self.on_close = None
        self.outcome = 'requested'

    def list_windows(self):
        self.enumerations += 1
        if self.on_list is not None:
            self.on_list()
        return list(self.windows)

    def close_window(self, window):
        self.closed.append(window)
        if self.on_close is not None:
            self.on_close()
        return self.outcome


class StudySessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.desktop = Desktop()
        self.session = StudySession(self.desktop, self.clock)

    def start(self, **changes):
        options = dict(duration_minutes=1, delay_seconds=10, preview=False, allowed_paths=[])
        options.update(changes)
        self.session.start(**options)

    def set_time(self, target):
        """Keep synthetic UI polling alive, leaving the final tick to the test."""
        while self.clock.now + 1 < target:
            self.clock.now += 1
            self.session.tick()
        self.clock.now = target

    def test_new_session_is_inactive_and_does_not_enumerate(self):
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.remaining_seconds, 0)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.enumerations, 0)

    def test_both_delays_close_at_deadline_and_only_once(self):
        for delay in (5, 10):
            with self.subTest(delay=delay):
                self.clock.now = 0
                self.desktop.closed.clear()
                self.start(delay_seconds=delay)
                self.assertEqual(self.session.tick(), [])
                self.assertEqual(self.session.pending[0]['remaining_seconds'], delay)
                self.set_time(delay - 0.01)
                self.assertEqual(self.session.tick(), [])
                self.assertEqual(self.desktop.closed, [])
                self.set_time(delay)
                self.assertEqual(self.session.tick(), [{'action': 'requested', 'application': 'Game.exe'}])
                self.assertTrue(self.session.pending[0]['requested'])
                self.set_time(self.clock.now + 20)
                self.assertEqual(self.session.tick(), [])
                self.assertEqual(self.desktop.closed, [Window()])

    def test_roblox_retries_ignored_close_until_window_disappears(self):
        for delay in (5, 10):
            with self.subTest(delay=delay):
                self.clock.now = 0
                self.desktop.closed.clear()
                game = Window(executable=r'C:\Games\Versions\version-123\ROBLOXPLAYERBETA.EXE')
                self.desktop.windows = [game]

                def close_on_second_request():
                    if len(self.desktop.closed) == 2:
                        self.desktop.windows = []

                self.desktop.on_close = close_on_second_request
                self.start(delay_seconds=delay)
                self.session.tick()
                self.set_time(delay)
                self.session.tick()
                self.assertEqual(self.desktop.closed, [game])
                self.set_time(2 * delay - .01)
                self.session.tick()
                self.assertEqual(self.desktop.closed, [game])
                self.set_time(2 * delay)
                self.session.tick()
                self.assertEqual(self.desktop.closed, [game, game])
                self.set_time(3 * delay)
                self.session.tick()
                self.assertEqual(self.desktop.closed, [game, game])
                self.assertEqual(self.session.pending, [])

    def test_roblox_unavailable_close_is_retried_after_full_delay(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.desktop.outcome = 'unavailable'
        self.start(delay_seconds=5)
        self.session.tick()
        self.set_time(5)
        self.assertEqual(self.session.tick()[0]['action'], 'unavailable')
        self.assertTrue(self.session.pending[0]['retrying'])
        self.assertEqual(self.session.pending[0]['remaining_seconds'], 5)
        self.desktop.outcome = 'requested'
        self.set_time(9.99)
        self.assertEqual(self.session.tick(), [])
        self.set_time(10)
        self.assertEqual(self.session.tick()[0]['action'], 'requested')
        self.assertEqual(self.desktop.closed, [game, game])

    def test_roblox_same_identity_reopened_between_polls_is_closed_again(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.start(delay_seconds=5)
        self.session.tick()
        self.set_time(5)
        self.session.tick()
        reopened = Window(executable=game.executable, title='Reopened game')
        self.assertEqual(reopened.identity, game.identity)
        self.desktop.windows = [reopened]
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [game, reopened])

    def test_roblox_resume_gap_restarts_retry_grace(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.start(duration_minutes=2, delay_seconds=5)
        self.session.tick()
        self.set_time(5)
        self.session.tick()
        self.clock.now = 30
        self.assertEqual(self.session.tick(), [])
        self.set_time(34.99)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [game])
        self.set_time(35)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [game, game])

    def test_roblox_retry_never_runs_at_or_after_expiry(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.start(delay_seconds=10)
        self.session.tick()
        self.set_time(50)
        self.session.tick()
        self.assertEqual(len(self.desktop.closed), 5)
        self.set_time(60)
        self.assertEqual(self.session.tick(), [{'action': 'expired'}])
        self.set_time(70)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(len(self.desktop.closed), 5)

    def test_roblox_allowlist_and_preview_keep_existing_behavior(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.start(allowed_paths=['c:/games/ROBLOXPLAYERBETA.exe'])
        self.session.tick()
        self.set_time(20)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])
        self.assertEqual(self.session.pending, [])

        self.clock.now = 0
        self.start(preview=True)
        self.session.tick()
        self.set_time(10)
        self.assertEqual(self.session.tick(), [{'action': 'preview', 'application': 'RobloxPlayerBeta.exe'}])
        self.set_time(30)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])


    def test_roblox_escalates_only_after_normal_close_and_full_second_grace(self):
        game = Window(executable=r'C:\Users\test\AppData\Local\Roblox\Versions\version-1234567890abcdef\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.desktop.terminate_roblox = mock.Mock(return_value='terminated')
        self.start(delay_seconds=5)
        self.session.tick()
        self.set_time(5)
        self.assertEqual(self.session.tick()[0]['action'], 'requested')
        self.desktop.terminate_roblox.assert_not_called()
        self.set_time(9.99)
        self.assertEqual(self.session.tick(), [])
        self.desktop.terminate_roblox.assert_not_called()
        self.set_time(10)
        self.assertEqual(self.session.tick()[0]['action'], 'terminated')
        self.desktop.terminate_roblox.assert_called_once_with(game)
        self.assertEqual(self.desktop.closed, [game])

    def test_denied_roblox_escalation_retries_ordinary_close(self):
        game = Window(executable=r'C:\Games\RobloxPlayerBeta.exe')
        self.desktop.windows = [game]
        self.desktop.terminate_roblox = mock.Mock(return_value='unavailable')
        self.start(delay_seconds=5)
        self.session.tick()
        self.set_time(5)
        self.session.tick()
        self.set_time(10)
        self.assertEqual(self.session.tick()[0]['action'], 'requested')
        self.assertEqual(self.desktop.closed, [game, game])
        self.desktop.terminate_roblox.assert_called_once_with(game)

    def test_document_and_preview_never_escalate(self):
        self.desktop.terminate_roblox = mock.Mock()
        for preview in (False, True):
            with self.subTest(preview=preview):
                self.clock.now = 0
                self.desktop.windows = [Window()]
                self.start(delay_seconds=5, preview=preview)
                self.session.tick()
                self.set_time(20)
                self.session.tick()
        self.desktop.terminate_roblox.assert_not_called()

    def test_new_window_during_session_has_full_grace(self):
        self.desktop.windows = []
        self.start()
        self.session.tick()
        self.set_time(20)
        self.desktop.windows = [Window()]
        self.session.tick()
        self.set_time(29.99)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [])
        self.set_time(30)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [Window()])

    def test_disappeared_and_reopened_window_has_new_deadline(self):
        self.start()
        self.session.tick()
        self.set_time(5)
        self.desktop.windows = []
        self.session.tick()
        self.assertEqual(self.session.pending, [])
        self.set_time(6)
        self.desktop.windows = [Window()]
        self.session.tick()
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [])
        self.set_time(16)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [Window()])

    def test_reused_handle_from_new_process_gets_full_grace(self):
        self.start()
        self.session.tick()
        self.set_time(9)
        replacement = Window(process_started=9999)
        self.desktop.windows = [replacement]
        self.session.tick()
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [])
        self.set_time(19)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [replacement])

    def test_title_change_does_not_extend_deadline(self):
        self.start()
        self.session.tick()
        changed = Window(title='Another page title')
        self.desktop.windows = [changed]
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [changed])

    def test_allowlist_normalizes_full_path_and_does_not_allow_same_basename_elsewhere(self):
        other = Window(handle=11, executable=r'D:\Other\Game.exe')
        self.desktop.windows.append(other)
        self.start(allowed_paths=['c:/apps/GAME.exe'])
        self.session.tick()
        self.assertEqual(len(self.session.pending), 1)
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [other])

    def test_stop_cancels_all_pending_actions(self):
        self.start()
        self.session.tick()
        self.session.stop()
        self.clock.now = 10
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.session.pending, [])
        self.assertEqual(self.desktop.closed, [])

    def test_extension_preserves_existing_window_grace_and_attempts(self):
        self.start()
        self.session.tick()
        self.set_time(5)
        self.session.set_remaining(3600)
        self.assertEqual(self.session.remaining_seconds, 3600)
        self.assertEqual(self.session.pending[0]['remaining_seconds'], 5)
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [Window()])
        self.session.set_remaining(1800)
        self.assertTrue(self.session.pending[0]['requested'])
        self.set_time(20)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [Window()])

    def test_shortened_remaining_time_expires_before_pending_window_close(self):
        self.start()
        self.session.tick()
        self.set_time(5)
        self.session.set_remaining(2)
        self.set_time(7)
        self.assertEqual(self.session.tick(), [{'action': 'expired'}])
        self.assertFalse(self.session.active)
        self.assertEqual(self.desktop.closed, [])

    def test_zero_remaining_cancels_pending_windows(self):
        self.start()
        self.session.tick()
        self.session.set_remaining(0.0)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.pending, [])
        self.clock.now = 20
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])

    def test_invalid_remaining_time_preserves_current_session(self):
        self.start()
        self.session.tick()
        for seconds in (-1, 14401, float('nan'), float('inf'), True, '60'):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.session.set_remaining(seconds)
                self.assertEqual(self.session.remaining_seconds, 60)
                self.assertEqual(self.session.pending[0]['remaining_seconds'], 10)
        self.session.stop()
        with self.assertRaises(ValueError):
            self.session.set_remaining(60)
        self.assertFalse(self.session.active)

    def test_restarting_preview_as_active_starts_new_grace(self):
        self.start(preview=True)
        self.session.tick()
        self.set_time(9)
        self.start()
        self.session.tick()
        self.set_time(10)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [])
        self.set_time(19)
        self.session.tick()
        self.assertEqual(self.desktop.closed, [Window()])

    def test_expiry_cancels_pending_and_prevents_later_action(self):
        self.start()
        self.clock.now = 55
        self.session.tick()
        self.clock.now = 60
        self.assertEqual(self.session.tick(), [{'action': 'expired'}])
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.pending, [])
        self.clock.now = 100
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])

    def test_slow_enumeration_crossing_expiry_never_closes(self):
        self.desktop.windows = []
        self.start()
        self.session.tick()
        self.set_time(55)
        self.desktop.windows = [Window()]
        self.session.tick()
        self.set_time(59)
        self.desktop.on_list = lambda: setattr(self.clock, 'now', 60)
        self.assertEqual(self.session.tick(), [{'action': 'expired'}])
        self.assertFalse(self.session.active)
        self.assertEqual(self.desktop.closed, [])

    def test_expiry_between_window_actions_stops_remaining_actions(self):
        self.desktop.windows = []
        self.start()
        self.session.tick()
        self.set_time(49)
        self.desktop.windows = [Window(), Window(handle=11)]
        self.session.tick()
        self.set_time(59)
        self.desktop.on_close = lambda: setattr(self.clock, 'now', 60)
        events = self.session.tick()
        self.assertEqual([item['action'] for item in events], ['requested', 'expired'])
        self.assertEqual(self.desktop.closed, [Window()])
        self.assertFalse(self.session.active)

    def test_preview_reports_deadline_without_closing(self):
        self.start(preview=True)
        self.session.tick()
        self.set_time(10)
        self.assertEqual(self.session.tick(), [{'action': 'preview', 'application': 'Game.exe'}])
        self.set_time(20)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])

    def test_backend_identity_result_is_preserved_without_retry(self):
        for result in ('gone', 'changed', 'unavailable'):
            with self.subTest(result=result):
                self.clock.now = 0
                self.desktop.closed.clear()
                self.desktop.outcome = result
                self.start()
                self.session.tick()
                self.set_time(10)
                self.assertEqual(self.session.tick()[0]['action'], result)
                self.session.tick()
                self.assertEqual(len(self.desktop.closed), 1)

    def test_enumeration_failure_stops_session_and_clears_pending(self):
        self.start()
        self.session.tick()
        self.desktop.on_list = mock.Mock(side_effect=OSError('synthetic'))
        with self.assertRaises(OSError):
            self.session.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.pending, [])

    def test_close_failure_stops_session_before_other_window(self):
        self.desktop.windows.append(Window(handle=11))
        self.start()
        self.session.tick()
        self.set_time(10)
        self.desktop.on_close = mock.Mock(side_effect=OSError('synthetic'))
        with self.assertRaises(OSError):
            self.session.tick()
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.pending, [])
        self.assertEqual(len(self.desktop.closed), 1)

    def test_remaining_seconds_rounds_up(self):
        self.start()
        self.clock.now = 0.1
        self.assertEqual(self.session.remaining_seconds, 60)
        self.clock.now = 59.5
        self.assertEqual(self.session.remaining_seconds, 1)

    def test_resume_gap_restarts_pending_countdown(self):
        self.start(duration_minutes=2)
        self.session.tick()
        self.clock.now = 30
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [])
        self.assertEqual(self.session.pending[0]['remaining_seconds'], 10)
        self.set_time(39.99)
        self.assertEqual(self.session.tick(), [])
        self.set_time(40)
        self.assertEqual(self.session.tick()[0]['action'], 'requested')

    def test_resume_gap_does_not_repeat_already_requested_close(self):
        self.start(duration_minutes=2)
        self.session.tick()
        self.set_time(10)
        self.session.tick()
        self.clock.now = 40
        self.assertEqual(self.session.tick(), [])
        self.assertTrue(self.session.pending[0]['requested'])
        self.set_time(50)
        self.assertEqual(self.session.tick(), [])
        self.assertEqual(self.desktop.closed, [Window()])

    def test_invalid_options_and_preview_are_rejected(self):
        invalid = [dict(duration_minutes=True), dict(duration_minutes=-1), dict(duration_minutes=241),
                   dict(delay_seconds=True), dict(delay_seconds=7), dict(allowed_paths='C:/Game.exe'),
                   dict(allowed_paths=['Game.exe']), dict(allowed_paths=['C:/game.txt']),
                   dict(allowed_paths=['C:/bad\x00.exe']), dict(allowed_paths=['C:/game.exe'] * 101)]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.start(**changes)
        with self.assertRaises(ValueError):
            self.start(preview=1)
        self.assertFalse(self.session.active)


class StudySettingsTests(unittest.TestCase):
    def setUp(self):
        fixture_base = Path(__file__).resolve().parent / 'tests-work'
        fixture_base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='study-settings-test-', dir=fixture_base)
        assert Path(self.temporary.name).resolve().is_relative_to(fixture_base.resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'uncreated-study'
        self.settings = StudySettings(self.root)

    def test_missing_settings_use_defaults_without_creating_data(self):
        value = self.settings.load()
        self.assertEqual(value, {'duration_minutes': 30, 'delay_seconds': 10, 'allowed_paths': []})
        value['allowed_paths'].append('C:/Synthetic.exe')
        self.assertEqual(self.settings.load()['allowed_paths'], [])
        self.assertFalse(self.root.exists())

    def test_saved_preferences_round_trip_without_persisting_activation(self):
        expected = self.settings.save(duration_minutes=45, delay_seconds=5,
                                      allowed_paths=['C:/Apps/School.exe', r'c:\apps\SCHOOL.exe'])
        self.assertEqual(expected, self.settings.load())
        self.assertEqual(expected['allowed_paths'], [r'c:\apps\school.exe'])
        raw = json.loads(self.settings.path.read_text(encoding='utf-8'))
        self.assertEqual(set(raw), {'duration_minutes', 'delay_seconds', 'allowed_paths'})
        self.assertEqual(list(self.root.glob('.study-*.tmp')), [])

    def test_invalid_settings_do_not_overwrite_previous_preferences(self):
        previous = self.settings.save(duration_minutes=30, delay_seconds=10, allowed_paths=[])
        with self.assertRaises(ValueError):
            self.settings.save(duration_minutes=-1, delay_seconds=10, allowed_paths=[])
        self.assertEqual(self.settings.load(), previous)

    def test_interrupted_atomic_replace_preserves_previous_settings(self):
        previous = self.settings.save(duration_minutes=30, delay_seconds=10, allowed_paths=[])
        with mock.patch('study_core.os.replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaises(OSError):
                self.settings.save(duration_minutes=60, delay_seconds=5, allowed_paths=[])
        self.assertEqual(self.settings.load(), previous)
        self.assertEqual(list(self.root.glob('.study-*.tmp')), [])

    def test_unknown_activation_or_old_invalid_schema_requires_repair(self):
        self.root.mkdir()
        for value in ({'active': True}, {'duration_minutes': 30, 'delay_seconds': 10,
                                        'allowed_paths': [], 'enabled': True}, [], None):
            with self.subTest(value=value):
                self.settings.path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.settings.load()


if __name__ == '__main__':
    unittest.main()
