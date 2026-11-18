"""Manual-login contracts with fake widgets; no Tcl, windows or processes."""
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import browser_login_ui
from browser_login_ui import BrowserLoginPanel

SESSION_A = 'a' * 32
SESSION_B = 'b' * 32


def plan(status='prepared_not_started', pending=False, session_id=None, **extra):
    return {'status': status, 'manual_login_pending': pending,
            'manual_session_id': session_id, 'login_verified': False,
            'process_started': False, **extra}


class Value:
    def __init__(self, *_args, **_kwargs):
        self.value = ''

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class BrowserLoginPanelTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.spawn = mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('No real processes')).start()
        self.run = mock.patch.object(subprocess, 'run', side_effect=AssertionError('No real commands')).start()
        self.session = mock.Mock()
        self.session.prepare_manual_login.return_value = plan()
        self.session.launch_manual_login.return_value = plan(
            'manual_login_started', True, SESSION_A, process_started=True)
        self.session.finish_manual_login.return_value = plan('manual_login_finished')
        self.authorize = mock.Mock(return_value=True)
        self.panel = BrowserLoginPanel.__new__(BrowserLoginPanel)
        self.panel.session = self.session
        self.panel.authorize_parent = self.authorize
        self.panel.closed = False
        self.panel._timer = None
        self.panel._current = {}
        self.panel._completion_reported = False
        self.panel.status = Value()
        self.panel.notice = Value()
        self.panel.start_button = mock.Mock()
        self.panel.finish_button = mock.Mock()
        self.panel.after = mock.Mock(return_value='poll-token')
        self.panel.after_cancel = mock.Mock()
        self.panel.winfo_toplevel = mock.Mock(return_value=None)
        self.confirm = mock.patch('browser_login_ui.messagebox.askyesno', return_value=True).start()
        self.addCleanup(self.panel.close)

    @staticmethod
    def state(widget):
        return widget.configure.call_args.kwargs['state']

    def test_launch_requires_pin_then_explicit_visible_window_confirmation(self):
        self.authorize.return_value = False
        self.assertFalse(self.panel.launch())
        self.confirm.assert_not_called()
        self.authorize.return_value = True
        self.confirm.return_value = False
        self.assertFalse(self.panel.launch())
        self.session.launch_manual_login.assert_not_called()
        events = []
        self.authorize.side_effect = lambda title: events.append('pin') or True
        def confirm(title, text, **kwargs):
            self.assertIn('видимое окно Edge с Facebook', text)
            self.assertIn('займёт экран', text)
            self.assertIn('не вводит пароль', text)
            events.append('window-consent')
            return True
        self.confirm.side_effect = confirm
        self.session.launch_manual_login.side_effect = lambda **kwargs: events.append('launch') or plan(
            'manual_login_started', True, SESSION_A, process_started=True)
        self.assertTrue(self.panel.launch())
        self.assertEqual(events, ['pin', 'window-consent', 'launch'])
        self.session.launch_manual_login.assert_called_once_with(foreground_authorized=True)
        self.assertIn('не проверен', self.panel.status.get())

    def test_launch_blocked_or_failed_never_claims_success(self):
        for status in ('blocked_study_active', 'blocked_edge_not_found', 'blocked_focus_window', 'launch_failed'):
            with self.subTest(status=status):
                self.session.launch_manual_login.return_value = plan(status)
                self.assertFalse(self.panel.launch())
                self.assertIn('не подтверждено', self.panel.notice.get())
        self.session.finish_manual_login.assert_not_called()

    def test_pending_and_corrupt_state_display_without_actions(self):
        self.session.prepare_manual_login.return_value = plan('blocked_manual_login_pending', True, SESSION_A)
        self.panel.refresh()
        self.assertIn('Ожидается завершение', self.panel.status.get())
        self.assertEqual(self.state(self.panel.start_button), 'disabled')
        self.assertEqual(self.state(self.panel.finish_button), 'normal')
        self.session.prepare_manual_login.side_effect = ValueError('invalid_manual_login_marker')
        self.panel.refresh()
        self.assertIn('повреждён', self.panel.status.get())
        self.assertEqual(self.state(self.panel.start_button), 'disabled')
        self.assertEqual(self.state(self.panel.finish_button), 'disabled')
        self.session.launch_manual_login.assert_not_called()
        self.session.finish_manual_login.assert_not_called()

    def test_finish_requires_pending_exact_session_pin_and_user_confirmation(self):
        self.assertFalse(self.panel.finish())
        self.authorize.assert_not_called()
        self.session.prepare_manual_login.return_value = plan('blocked_manual_login_pending', True, SESSION_A)
        self.authorize.return_value = False
        self.assertFalse(self.panel.finish())
        self.confirm.assert_not_called()
        self.authorize.return_value = True
        self.confirm.return_value = False
        self.assertFalse(self.panel.finish())
        self.session.finish_manual_login.assert_not_called()
        self.confirm.return_value = True
        self.assertTrue(self.panel.finish())
        self.session.finish_manual_login.assert_called_once_with(
            user_done=True, manual_session_id=SESSION_A)
        self.assertIn('сами вошли', self.confirm.call_args.args[1])
        self.assertIn('закрыли', self.confirm.call_args.args[1])
        self.assertIn('Вход ещё не проверен', self.panel.status.get())
        self.assertIn('при следующем подключении', self.panel.status.get())
        self.session.prepare_manual_login.return_value = plan()
        self.panel.refresh()
        self.assertIn('при следующем подключении', self.panel.status.get())

    def test_finish_binds_marker_before_dialog_and_rejects_changed_session(self):
        self.session.prepare_manual_login.return_value = plan('blocked_manual_login_pending', True, SESSION_A)
        def authorize(_title):
            self.panel._render(plan('blocked_manual_login_pending', True, SESSION_B))
            return True
        self.authorize.side_effect = authorize
        self.session.finish_manual_login.return_value = plan('blocked_manual_session_changed', True, SESSION_B)
        self.assertFalse(self.panel.finish())
        self.session.finish_manual_login.assert_called_once_with(
            user_done=True, manual_session_id=SESSION_A)
        self.assertIn('изменилась', self.panel.status.get())
        self.assertIn('не подтверждено', self.panel.notice.get())
        self.assertFalse(self.panel._completion_reported)

    def test_poll_reads_only_and_close_stops_future_polls(self):
        self.panel._schedule_poll()
        self.panel._poll()
        self.panel.after.assert_called_with(1500, self.panel._poll)
        self.session.launch_manual_login.assert_not_called()
        self.session.finish_manual_login.assert_not_called()
        self.authorize.assert_not_called()
        self.panel.close()
        self.panel.after_cancel.assert_called_once_with('poll-token')
        self.panel.after.reset_mock()
        self.panel._poll()
        self.panel.after.assert_not_called()
        self.spawn.assert_not_called()
        self.run.assert_not_called()

    def test_constructor_is_read_only_with_all_tk_primitives_replaced(self):
        frame = browser_login_ui.ttk.Frame
        with mock.patch.object(frame, '__init__', lambda self, *a, **kw: None), \
                mock.patch.object(BrowserLoginPanel, 'columnconfigure'), \
                mock.patch.object(BrowserLoginPanel, 'bind'), \
                mock.patch.object(BrowserLoginPanel, 'after', return_value='constructor-poll'), \
                mock.patch.object(BrowserLoginPanel, 'after_cancel'), \
                mock.patch.object(browser_login_ui.ttk, 'Frame', side_effect=lambda *a, **kw: mock.Mock()), \
                mock.patch.object(browser_login_ui.ttk, 'Label', side_effect=lambda *a, **kw: mock.Mock()), \
                mock.patch.object(browser_login_ui.ttk, 'Button', side_effect=lambda *a, **kw: mock.Mock()), \
                mock.patch.object(browser_login_ui.tk, 'StringVar', side_effect=Value):
            panel = BrowserLoginPanel(object(), session=self.session, authorize_parent=self.authorize)
            self.session.prepare_manual_login.assert_called_once_with()
            self.session.launch_manual_login.assert_not_called()
            self.session.finish_manual_login.assert_not_called()
            self.assertIn('Ничего не открыто', panel.status.get())
            panel.close()
        self.spawn.assert_not_called()
        self.run.assert_not_called()

    def test_app_wires_connections_authorizer_and_cleanup(self):
        app = importlib.import_module('app')
        application = object.__new__(app.App)
        application.settingstab = object()
        application.studypanel = SimpleNamespace(authorize_parent=self.authorize)
        with mock.patch.object(app, 'BrowserLoginPanel') as panel, mock.patch.object(app.tk.Tk, 'destroy'):
            application._build_browser_login_panel()
            panel.assert_called_once_with(application.settingstab, authorize_parent=self.authorize)
            panel.return_value.pack.assert_called_once_with(fill='x', pady=(0, 12))
            application.destroy()
            panel.return_value.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
