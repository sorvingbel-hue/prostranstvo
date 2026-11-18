"""Power UI logic without Tk roots, windows, real timers or power commands."""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from power_ui import PowerPanel


def snapshot(state='queued', request_id='request-1', action='restart', remaining=60):
    return {'state': state, 'request_id': request_id, 'action': action,
            'remaining_seconds': remaining, 'can_cancel': state == 'queued'}


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class PowerPanelTests(unittest.TestCase):
    def setUp(self):
        # Exercise the actual handlers while bypassing all Tcl/Tk construction.
        self.panel = PowerPanel.__new__(PowerPanel)
        self.control = Mock()
        self.control.status.return_value = snapshot('idle', request_id=None, remaining=0)
        self.authorize = Mock(return_value=True)
        self.panel.control = self.control
        self.panel.authorize_parent = self.authorize
        self.panel.closed = False
        self.panel._timer = None
        self.panel._current = {}
        self.panel.delay = Value('60')
        self.panel.status = Value()
        self.panel.notice = Value()
        for name in ('restart_button', 'shutdown_button', 'cancel_button', 'delay_box'):
            setattr(self.panel, name, Mock())
        self.panel.after = Mock(return_value='poll-token')
        self.panel.after_cancel = Mock()
        self.panel.winfo_toplevel = Mock(return_value=None)
        self.panel.refresh()
        self.panel._schedule_poll()
        self.addCleanup(self.panel.close)
        self.confirm = patch('power_ui.messagebox.askyesno', return_value=True).start()
        self.addCleanup(patch.stopall)

    def state(self, widget):
        return widget.configure.call_args.kwargs['state']

    def test_refused_confirmation_or_pin_cannot_schedule_or_start_worker(self):
        self.confirm.return_value = False
        self.assertFalse(self.panel.schedule('restart'))
        self.authorize.assert_not_called()
        self.confirm.return_value = True
        self.authorize.return_value = False
        self.assertFalse(self.panel.schedule('shutdown'))
        self.authorize.assert_called_once_with('Выключение компьютера')
        self.control.schedule.assert_not_called()
        self.control.start_worker.assert_not_called()
        self.control.cancel.assert_not_called()

    def test_authorized_requests_schedule_selected_action_then_explicit_worker(self):
        for action in ('restart', 'shutdown'):
            with self.subTest(action=action):
                self.control.reset_mock()
                self.panel.delay.set('120')
                queued = snapshot(action=action, remaining=120)
                events = []
                self.confirm.side_effect = lambda *a, **kw: events.append('warning') or True
                self.authorize.side_effect = lambda title: events.append('pin') or True
                self.control.schedule.side_effect = lambda *a, **kw: events.append('schedule') or queued
                self.control.start_worker.side_effect = lambda rid: events.append('worker') or queued
                self.assertTrue(self.panel.schedule(action))
                self.assertEqual(events, ['warning', 'pin', 'schedule', 'worker'])
                self.control.schedule.assert_called_once_with(action, delay_seconds=120)
                self.control.start_worker.assert_called_once_with('request-1')
                self.assertIn('02:00', self.panel.status.get())
                self.assertEqual(self.state(self.panel.restart_button), 'disabled')
                self.assertEqual(self.state(self.panel.cancel_button), 'normal')
                self.assertIn('Сохраните', self.confirm.call_args.args[1])

    def test_cancellation_requires_pin_and_keeps_original_request_id(self):
        self.control.status.return_value = snapshot(request_id='original')
        self.panel.refresh()
        self.authorize.return_value = False
        self.assertFalse(self.panel.cancel())
        self.control.cancel.assert_not_called()
        def authorize(title):
            self.panel._render(snapshot(request_id='newer'))
            return True
        self.authorize.side_effect = authorize
        self.control.cancel.return_value = snapshot('cancelled', request_id='original')
        self.assertTrue(self.panel.cancel())
        self.control.cancel.assert_called_once_with('original')
        self.assertEqual(self.state(self.panel.cancel_button), 'disabled')
        self.confirm.assert_not_called()
        self.control.start_worker.assert_not_called()

    def test_poll_updates_countdown_and_never_claims_dispatch_completed(self):
        self.panel.after.reset_mock()
        for state, expected in (('queued', '00:31'), ('executing', 'Отправляется'),
                                ('dispatched', 'не подтверждено'), ('uncertain', 'не подтверждён')):
            self.control.status.return_value = snapshot(state, remaining=31)
            self.panel._poll()
            self.panel.after.assert_called_with(1000, self.panel._poll)
            self.assertIn(expected, self.panel.status.get())
            self.assertNotIn('Готово', self.panel.status.get())
        self.control.schedule.assert_not_called()
        self.control.start_worker.assert_not_called()
        self.control.cancel.assert_not_called()
        self.authorize.assert_not_called()
        self.panel.close()
        self.panel.after_cancel.assert_called_once_with('poll-token')
        self.panel.after.reset_mock()
        self.panel._poll()
        self.panel.after.assert_not_called()

    def test_failed_or_uncertain_worker_is_not_reported_as_success_or_retried(self):
        queued = snapshot()
        self.control.schedule.return_value = queued
        self.control.status.return_value = queued
        for state in ('failed', 'uncertain'):
            with self.subTest(state=state):
                self.control.start_worker.return_value = snapshot(state)
                self.assertFalse(self.panel.schedule('restart'))
                self.assertEqual(self.panel._current['state'], state)
                self.assertNotIn('Запрос запланирован', self.panel.notice.get())
        self.control.start_worker.side_effect = OSError('fixture')
        self.assertFalse(self.panel.schedule('restart'))
        self.assertIn('запуск не подтверждён', self.panel.notice.get())
        self.assertEqual(self.state(self.panel.cancel_button), 'normal')
        calls = self.control.start_worker.call_count
        self.panel._poll()
        self.assertEqual(self.control.start_worker.call_count, calls)

    def test_persisted_terminal_result_allows_a_new_request_without_automatic_retry(self):
        for state in ('dispatched', 'uncertain'):
            with self.subTest(state=state):
                self.control.status.return_value = snapshot(state, action='shutdown')
                self.panel.refresh()
                self.assertEqual(self.state(self.panel.restart_button), 'normal')
                self.assertEqual(self.state(self.panel.shutdown_button), 'normal')
                self.assertEqual(self.state(self.panel.delay_box), 'readonly')
                self.assertEqual(self.state(self.panel.cancel_button), 'disabled')
                self.panel._poll()
        self.control.schedule.assert_not_called()
        self.control.start_worker.assert_not_called()
        self.authorize.assert_not_called()

    def test_repeat_risk_is_confirmed_before_pin_for_each_terminal_result(self):
        queued = snapshot()
        for state, explanation in (('dispatched', 'уже отправлен Windows'),
                                   ('uncertain', 'Windows могла уже получить команду')):
            with self.subTest(state=state):
                self.panel._render(snapshot(state, action='shutdown'))
                events = []
                def confirm(title, text, **kwargs):
                    self.assertIn('Предыдущий запрос' if state == 'dispatched' else 'предыдущего запроса', text)
                    self.assertIn('Выключение', text)
                    self.assertIn(explanation, text)
                    self.assertIn('может повторить действие', text)
                    events.append('warning')
                    return True
                self.confirm.side_effect = confirm
                self.authorize.side_effect = lambda title: events.append('pin') or True
                self.control.schedule.side_effect = lambda *a, **kw: events.append('schedule') or queued
                self.control.start_worker.side_effect = lambda request_id: events.append('worker') or queued
                self.assertTrue(self.panel.schedule('restart'))
                self.assertEqual(events, ['warning', 'pin', 'schedule', 'worker'])

    def test_refused_repeat_warning_does_not_request_pin_or_create_a_request(self):
        for state in ('dispatched', 'uncertain'):
            with self.subTest(state=state):
                self.panel._render(snapshot(state))
                self.confirm.return_value = False
                self.assertFalse(self.panel.schedule('shutdown'))
                self.assertIn('может повторить действие', self.confirm.call_args.args[1])
                self.assertEqual(self.panel._current['state'], state)
        self.authorize.assert_not_called()
        self.control.schedule.assert_not_called()
        self.control.start_worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
