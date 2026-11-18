"""No native calls, browser processes or real power execution."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent_control


class AgentControlTests(unittest.TestCase):
    def test_tool_catalog_is_unique_and_has_no_power_execution(self):
        self.assertEqual(len(agent_control.TOOLS), len(agent_control.TOOL_NAMES))
        self.assertNotIn('restart_computer', agent_control.TOOL_NAMES)
        self.assertNotIn('shutdown_computer', agent_control.TOOL_NAMES)

    def test_power_proposal_is_inert(self):
        with patch('power_control.PowerControl') as control:
            value = agent_control.call('prepare_power_action', {'action': 'restart'})
        control.assert_not_called()
        self.assertFalse(value['scheduled'])
        self.assertFalse(value['force'])
        self.assertEqual(value['status'], 'proposal_only')

    def test_empty_argument_tools_reject_all_extra_parameters(self):
        for name in ('lock_computer', 'power_status', 'agent_browser_status'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                agent_control.call(name, {'command': 'arbitrary.exe'})

    def test_browser_delegates_exact_arguments(self):
        with patch('headless_browser.call', return_value={'fixture': True}) as handler:
            self.assertEqual(agent_control.call('agent_browser_open', {'url': 'https://example.com/'}),
                             {'fixture': True})
        handler.assert_called_once_with('agent_browser_open', {'url': 'https://example.com/'})

    def test_lock_uses_only_fixed_backend(self):
        with patch('session_control.lock_session', return_value={'fixture': True}) as lock:
            self.assertEqual(agent_control.call('lock_computer', {}), {'fixture': True})
        lock.assert_called_once_with()

    def test_power_status_has_no_worker_start(self):
        with patch('power_control.PowerControl') as control:
            control.return_value.status.return_value = {'state': 'idle'}
            self.assertEqual(agent_control.call('power_status', {}), {'state': 'idle'})
        control.return_value.schedule.assert_not_called()
        control.return_value.start_worker.assert_not_called()
