"""No real workstation lock is performed by these tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import unittest
from unittest import mock
from session_control import lock_session


class SessionLockTests(unittest.TestCase):
    def test_success_only_claims_request_and_never_claims_completed_lock(self):
        native = mock.Mock(return_value=1)
        result = lock_session(backend=native)
        native.assert_called_once_with()
        self.assertEqual(result['status'], 'lock_requested')
        self.assertFalse(result['lock_completion_verified'])
        self.assertFalse(result['processes_terminated'])

    def test_failure_is_not_reported_as_locked(self):
        with self.assertRaises(OSError):
            lock_session(backend=mock.Mock(return_value=0))
