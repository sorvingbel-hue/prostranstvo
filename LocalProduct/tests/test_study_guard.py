"""Synthetic local PIN fixtures. No personal PIN or settings are read."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import study_guard
from study_guard import ITERATIONS, StudyGuard

PIN = '246810'
OTHER_PIN = '135791'


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class StudyGuardTests(unittest.TestCase):
    def setUp(self):
        fixture_base = Path(__file__).resolve().parent / 'tests-work'
        fixture_base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='study-guard-test-', dir=fixture_base)
        assert Path(self.temporary.name).resolve().is_relative_to(fixture_base.resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'uncreated-study'
        self.clock = Clock()
        self.guard = StudyGuard(self.root, self.clock)

    def configure(self):
        self.guard.set_pin(PIN)

    def test_read_only_checks_do_not_create_data(self):
        self.assertFalse(self.guard.configured())
        self.assertFalse(self.guard.verify(PIN))
        self.assertFalse(self.root.exists())

    def test_salted_pin_round_trip_survives_new_instance(self):
        self.configure()
        self.assertTrue(self.guard.configured())
        self.assertTrue(self.guard.verify(PIN))
        self.assertFalse(self.guard.verify(OTHER_PIN))
        self.assertTrue(StudyGuard(self.root).verify(PIN))
        value = json.loads(self.guard.path.read_text(encoding='utf-8'))
        self.assertEqual(value['version'], 1)
        self.assertGreaterEqual(value['iterations'], 310_000)
        self.assertEqual(len(bytes.fromhex(value['salt'])), 16)
        expected = hashlib.pbkdf2_hmac('sha256', PIN.encode('ascii'), bytes.fromhex(value['salt']), value['iterations'])
        self.assertEqual(bytes.fromhex(value['pin_hash']), expected)
        self.assertNotIn(PIN, self.guard.path.read_text(encoding='utf-8'))

    def test_random_salt_differs_for_same_pin(self):
        self.configure()
        other = StudyGuard(Path(self.temporary.name) / 'second-study')
        other.set_pin(PIN)
        first_value = json.loads(self.guard.path.read_text(encoding='utf-8'))
        second_value = json.loads(other.path.read_text(encoding='utf-8'))
        self.assertNotEqual(first_value['salt'], second_value['salt'])
        self.assertNotEqual(first_value['pin_hash'], second_value['pin_hash'])

    def test_existing_pin_cannot_be_overwritten(self):
        self.configure()
        previous = self.guard.path.read_bytes()
        with self.assertRaises(ValueError):
            self.guard.set_pin(OTHER_PIN)
        self.assertEqual(self.guard.path.read_bytes(), previous)
        self.assertTrue(self.guard.verify(PIN))

    def test_set_pin_rejects_bad_lengths_unicode_and_nondigits(self):
        for value in ('12345', '1' * 13, '１２３４５６', '12345a', '123456 ', '', None, 123456):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.guard.set_pin(value)
        self.assertFalse(self.root.exists())

    def test_six_and_twelve_digits_are_accepted(self):
        self.configure()
        other = StudyGuard(Path(self.temporary.name) / 'second-study')
        other.set_pin('001122334455')
        self.assertTrue(other.verify('001122334455'))

    def test_five_failures_throttle_until_boundary(self):
        self.configure()
        for _ in range(5):
            self.assertFalse(self.guard.verify(OTHER_PIN))
        with mock.patch('study_guard.hashlib.pbkdf2_hmac') as derive:
            with self.assertRaises(ValueError):
                self.guard.verify(PIN)
            self.assertFalse(derive.called)
        self.clock.now = 29.99
        with self.assertRaises(ValueError):
            self.guard.verify(PIN)
        self.clock.now = 30
        self.assertTrue(self.guard.verify(PIN))

    def test_malformed_attempts_count_toward_throttle(self):
        self.configure()
        for _ in range(5):
            self.assertFalse(self.guard.verify('bad'))
        with self.assertRaises(ValueError):
            self.guard.verify(PIN)

    def test_success_resets_failure_count(self):
        self.configure()
        for _ in range(4):
            self.assertFalse(self.guard.verify(OTHER_PIN))
        self.assertTrue(self.guard.verify(PIN))
        for _ in range(4):
            self.assertFalse(self.guard.verify(OTHER_PIN))
        self.assertTrue(self.guard.verify(PIN))

    def test_cooldown_restarts_failure_budget_after_elapsed(self):
        self.configure()
        for _ in range(5):
            self.assertFalse(self.guard.verify('bad'))
        self.clock.now = 30
        self.assertFalse(self.guard.verify('bad'))
        self.assertTrue(self.guard.verify(PIN))

    def test_attempt_budget_is_shared_by_concurrent_instances(self):
        self.configure()
        other = StudyGuard(self.root, self.clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.guard.verify, OTHER_PIN)
            second = pool.submit(other.verify, OTHER_PIN)
            self.assertFalse(first.result(timeout=5))
            self.assertFalse(second.result(timeout=5))
        for _ in range(3):
            self.assertFalse(other.verify('bad'))
        with self.assertRaisesRegex(ValueError, 'через 30 с'):
            self.guard.verify(PIN)

    def test_lockout_persists_in_a_new_process(self):
        self.configure()
        for _ in range(5):
            self.assertFalse(self.guard.verify('bad'))
        script = """\
import sys
from study_guard import StudyGuard
for now in (29.99, 30):
    guard = StudyGuard(sys.argv[1], lambda: now)
    try:
        print(guard.verify(sys.argv[2]))
    except ValueError:
        print('blocked')
"""
        child = subprocess.run(
            [sys.executable, '-B', '-c', script, str(self.root), PIN],
            cwd=Path(study_guard.__file__).resolve().parent,
            capture_output=True, text=True, timeout=10, check=True,
        )
        self.assertEqual(child.stdout.splitlines(), ['blocked', 'True'])
        self.clock.now = 30
        self.assertTrue(self.guard.verify(PIN))

    def test_corrupt_attempt_state_fails_closed_before_hashing(self):
        self.configure()
        valid = {'version': 1, 'failures': 1, 'blocked_until': 0}
        invalid = [
            {'version': True}, {'failures': True}, {'failures': -1}, {'failures': 6},
            {'blocked_until': True}, {'blocked_until': float('nan')},
            {'blocked_until': -1}, {'blocked_until': 30}, {'failures': 5},
            {'extra': 0},
        ]
        bad_states = [json.dumps({**valid, **patch}) for patch in invalid]
        bad_states += ['null', '{', ' ' * 2049]
        with mock.patch('study_guard.hashlib.pbkdf2_hmac') as derive:
            for value in bad_states:
                with self.subTest(value=value):
                    self.guard.attempts_path.write_text(value, encoding='utf-8')
                    with self.assertRaises(ValueError):
                        self.guard.verify(PIN)
            derive.assert_not_called()

    def test_attempt_write_failure_keeps_previous_budget(self):
        self.configure()
        for _ in range(4):
            self.assertFalse(self.guard.verify('bad'))
        previous = self.guard.attempts_path.read_bytes()
        with mock.patch('study_guard.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.guard.verify('bad')
        self.assertEqual(self.guard.attempts_path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob('.parent-pin-*.tmp')), [])
        other = StudyGuard(self.root, self.clock)
        self.assertFalse(other.verify('bad'))
        with self.assertRaises(ValueError):
            other.verify(PIN)

    def test_corrupt_schema_or_excessive_work_factor_fails_closed(self):
        self.configure()
        good = json.loads(self.guard.path.read_text(encoding='utf-8'))
        mutations = [{'version': True}, {'version': 2}, {'iterations': ITERATIONS - 1},
                     {'iterations': 2_000_001}, {'iterations': True}, {'salt': 'zz' * 16},
                     {'pin_hash': 'abc'}, {'plaintext': PIN}]
        for change in mutations:
            with self.subTest(change=change):
                self.guard.path.write_text(json.dumps({**good, **change}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.guard.configured()
                with self.assertRaises(ValueError):
                    self.guard.verify(PIN)
                with self.assertRaises(ValueError):
                    self.guard.set_pin(OTHER_PIN)

    def test_oversized_pin_file_fails_closed(self):
        self.configure()
        self.guard.path.write_text(' ' * 2049, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.guard.configured()

    def test_atomic_write_failure_does_not_leave_config_or_temporary(self):
        with mock.patch('study_guard.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.configure()
        self.assertFalse(self.guard.configured())
        self.assertEqual(list(self.root.glob('.parent-pin-*.tmp')), [])
        self.configure()
        self.assertTrue(self.guard.verify(PIN))


if __name__ == '__main__':
    unittest.main()
