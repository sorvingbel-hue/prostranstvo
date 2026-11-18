"""Agent tool routing checks with no GUI or real launches."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import unittest
from unittest import mock
import agent_apps


class AgentAppTests(unittest.TestCase):
    def test_status_is_explicitly_inventory_not_existing_session_control(self):
        with mock.patch.object(agent_apps, 'inventory_browsers', return_value={}), \
                mock.patch.object(agent_apps, 'ApplicationLauncher') as launcher:
            launcher.return_value.catalog.return_value = {}
            result = agent_apps.call('agent_apps_status', {})
        self.assertTrue(result['this_call_starts_nothing'])
        self.assertFalse(result['existing_session_control_verified'])
        launcher.return_value.launch.assert_not_called()

    def test_default_desktop_launch_passes_false_screen_permission(self):
        with mock.patch.object(agent_apps, 'ApplicationLauncher') as launcher:
            launcher.return_value.launch.return_value = {'status': 'blocked'}
            result = agent_apps.call('launch_agent_app', {'app': 'notepad'})
        launcher.return_value.launch.assert_called_once_with('notepad', foreground_authorized=False)
        self.assertEqual(result['status'], 'blocked')

    def test_default_browser_launch_is_not_promoted_to_authorized(self):
        with mock.patch.object(agent_apps, 'launch_visible_browser') as launch:
            agent_apps.call('launch_agent_app', {'app': 'edge', 'url': 'https://example.test/'})
        launch.assert_called_once_with('edge', 'https://example.test/', foreground_authorized=False)

    def test_background_request_only_builds_a_plan(self):
        with mock.patch.object(agent_apps, 'build_browser_access_plan') as plan, \
                mock.patch.object(agent_apps, 'launch_visible_browser') as launch:
            agent_apps.call('prepare_browser_background', {'browser': 'edge', 'url': 'https://example.test/'})
        plan.assert_called_once_with('edge', 'https://example.test/')
        launch.assert_not_called()

    def test_no_shell_paths_free_arguments_or_fake_boolean_permissions(self):
        invalid = [
            ('agent_apps_status', {'command': 'cmd'}),
            ('launch_agent_app', {'app': 'cmd'}),
            ('launch_agent_app', {'app': 'notepad', 'args': ['/x']}),
            ('launch_agent_app', {'app': 'notepad', 'url': 'https://example.test/'}),
            ('launch_agent_app', {'app': 'edge', 'foreground_authorized': True}),
            ('launch_agent_app', {'app': 'notepad', 'foreground_authorized': 'true'}),
            ('prepare_browser_background', {'browser': 'notion', 'url': 'https://example.test/'}),
        ]
        with mock.patch.object(agent_apps, 'launch_visible_browser') as browser, \
                mock.patch.object(agent_apps, 'ApplicationLauncher') as desktop:
            for name, arguments in invalid:
                with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                    agent_apps.call(name, arguments)
        browser.assert_not_called()
        desktop.return_value.launch.assert_not_called()

    def test_browser_plan_never_fabricates_screen_consent(self):
        with mock.patch.object(agent_apps, 'build_browser_access_plan') as plan:
            agent_apps.call('prepare_app_launch', {'app': 'edge', 'url': 'https://example.test/'})
        plan.assert_called_once_with('edge', 'https://example.test/', mode='visible')


if __name__ == '__main__':
    unittest.main()
