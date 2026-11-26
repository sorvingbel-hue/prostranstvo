"""Fake widgets, callbacks and coordinator only. No Tk/OS/data/network calls."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]/'LocalProduct/src'))
sys.path.insert(0,str(HERE))
import hourly_break_ui as ui


class Value:
    def __init__(self):self.value=''
    def set(self,value):self.value=value
    def get(self):return self.value


class Widget:
    def __init__(self):self.options={}
    def configure(self,**values):self.options.update(values)


def state(**changes):
    value={'enabled':True,'phase':'waiting','task':'Убрать ненужные вещи со стола.',
           'remaining_break_seconds':600,'break_id':'a'*32,'revision':7,
           'can_request_done':False,'can_approve':False}
    value.update(changes)
    return value


class Service:
    def __init__(self):
        self.value=state();self.calls=[];self.reads=0;self.grants=0
    def status(self):
        self.reads+=1
        return deepcopy(self.value)
    def _revision(self,expected):
        if expected != self.value['revision']:raise RuntimeError('stale service revision')
    def configure(self,*,enabled,task,expected_revision):
        self._revision(expected_revision)
        self.calls.append(('configure',enabled,task,expected_revision))
        self.value.update(enabled=enabled,task=task,revision=expected_revision+1)
        if not enabled:self.value.update(phase='cancelled',can_request_done=False,can_approve=False)
    def request_done(self,*,break_id,expected_revision):
        self._revision(expected_revision)
        if (not self.value['enabled'] or self.value['remaining_break_seconds'] or
                not self.value['can_request_done'] or break_id!=self.value['break_id']):
            raise RuntimeError('not ready')
        self.calls.append(('request_done',break_id,expected_revision))
        self.value.update(phase='submitted',revision=expected_revision+1,
                          can_request_done=False,can_approve=True)
    def approve_break(self,*,break_id,expected_revision):
        self._revision(expected_revision)
        if (not self.value['enabled'] or self.value['remaining_break_seconds'] or
                not self.value['can_approve'] or break_id!=self.value['break_id']):
            raise RuntimeError('not ready')
        self.calls.append(('approve_break',break_id,expected_revision));self.grants+=1
        self.value.update(phase='approved',revision=expected_revision+1,
                          can_request_done=False,can_approve=False)


class HourlyBreakUiTests(unittest.TestCase):
    def setUp(self):
        self.service=Service()
        self.panel=object.__new__(ui.HourlyBreakPanel)
        self.panel.service=self.service
        self.panel.authorize_parent=mock.Mock(return_value=True)
        self.panel.closed=False;self.panel._timer=None;self.panel._current=None
        self.panel.status,self.panel.task,self.panel.notice=Value(),Value(),Value()
        self.panel.done_button,self.panel.approve_button=Widget(),Widget()
        self.panel.configure_button,self.panel.disable_button=Widget(),Widget()
        self.panel.winfo_toplevel=lambda:None
        self.ask=mock.patch.object(ui.simpledialog,'askstring',return_value='Протереть пыль там, где попросит мама.').start()
        self.addCleanup(mock.patch.stopall)

    def ready_child(self):
        self.service.value.update(remaining_break_seconds=0,can_request_done=True)

    def ready_parent(self):
        self.service.value.update(remaining_break_seconds=0,phase='submitted',can_approve=True)

    def test_refresh_is_pure_even_when_countdown_reaches_zero(self):
        self.panel.refresh();self.ready_child();self.panel.refresh()
        self.assertEqual(self.service.calls,[]);self.assertEqual(self.service.grants,0)
        self.panel.authorize_parent.assert_not_called();self.ask.assert_not_called()
        self.assertEqual(self.panel.done_button.options['state'],'normal')
        self.assertEqual(self.panel.approve_button.options['state'],'disabled')

    def test_600_seconds_message_and_buttons_do_not_offer_early_unlock(self):
        self.panel.refresh()
        self.assertIn('10:00',self.panel.status.get())
        self.assertEqual(self.panel.done_button.options['state'],'disabled')
        self.assertEqual(self.panel.approve_button.options['state'],'disabled')
        self.assertFalse(self.panel.request_done());self.assertFalse(self.panel.approve_break())
        self.assertEqual(self.service.calls,[]);self.panel.authorize_parent.assert_not_called()

    def test_child_done_is_only_request_not_approval_or_grant(self):
        self.ready_child()
        self.assertTrue(self.panel.request_done())
        self.assertEqual(self.service.calls,[('request_done','a'*32,7)])
        self.assertEqual(self.service.grants,0)
        self.assertIn('не выдан',self.panel.notice.get())
        self.panel.authorize_parent.assert_not_called()

    def test_parent_approval_only_after_request_and_zero_remaining(self):
        self.ready_parent()
        self.assertTrue(self.panel.approve_break())
        self.panel.authorize_parent.assert_called_once()
        self.assertEqual(self.service.calls,[('approve_break','a'*32,7)])
        self.assertEqual(self.service.grants,1)
        self.assertIn('контроллер',self.panel.notice.get())
        self.assertNotIn('Игра разрешена',self.panel.notice.get())

    def test_repeated_parent_action_cannot_redeem_same_break_twice(self):
        self.ready_parent();self.assertTrue(self.panel.approve_break())
        self.assertFalse(self.panel.approve_break())
        self.assertEqual(self.service.grants,1)

    def test_early_parent_flag_cannot_override_remaining_time(self):
        self.service.value.update(phase='submitted',can_approve=True,remaining_break_seconds=1)
        self.assertFalse(self.panel.approve_break())
        self.panel.authorize_parent.assert_not_called();self.assertEqual(self.service.calls,[])

    def test_early_child_flag_cannot_override_remaining_time(self):
        self.service.value.update(can_request_done=True,remaining_break_seconds=1)
        self.assertFalse(self.panel.request_done());self.assertEqual(self.service.calls,[])

    def test_denied_or_missing_parent_never_dispatches(self):
        self.ready_parent()
        for callback in (None,mock.Mock(return_value=False),mock.Mock(return_value='yes'),mock.Mock(return_value=1)):
            with self.subTest(callback=callback):
                self.panel.authorize_parent=callback
                self.assertFalse(self.panel.approve_break())
                self.assertFalse(self.panel.configure_cycle())
                self.assertFalse(self.panel.disable_cycle())
        self.assertEqual(self.service.calls,[]);self.ask.assert_not_called()

    def test_new_break_during_parent_pin_does_not_get_old_approval(self):
        self.ready_parent()
        def authorize(_):
            self.service.value.update(break_id='b'*32,revision=8)
            return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.approve_break())
        self.assertEqual(self.service.calls,[])

    def test_task_changed_during_pin_even_without_revision_is_rejected(self):
        self.ready_parent()
        def authorize(_):self.service.value['task']='Другое полезное дело.';return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.approve_break());self.assertEqual(self.service.calls,[])

    def test_mom_cannot_approve_if_remaining_time_reappears_after_pin(self):
        self.ready_parent()
        def authorize(_):self.service.value['remaining_break_seconds']=30;return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.approve_break());self.assertEqual(self.service.calls,[])

    def test_countdown_only_change_does_not_invalidate_config_dialog(self):
        def authorize(_):self.service.value['remaining_break_seconds']=595;return True
        self.panel.authorize_parent=authorize
        self.assertTrue(self.panel.configure_cycle())
        self.assertEqual(self.service.calls[0][0],'configure')

    def test_config_only_records_future_rule_without_grant(self):
        self.service.value.update(enabled=False,phase='disabled',break_id=None,task='')
        self.assertTrue(self.panel.configure_cycle())
        self.assertEqual(self.service.calls,[('configure',True,'Протереть пыль там, где попросит мама.',7)])
        self.assertEqual(self.service.grants,0)
        self.assertIn('не начинает игру',self.panel.notice.get())
        self.panel.authorize_parent.assert_called_once()

    def test_config_suggests_useful_task_without_saving_until_parent_accepts(self):
        self.service.value['task']=''
        self.ask.return_value=None
        self.assertFalse(self.panel.configure_cycle())
        self.assertEqual(self.ask.call_args.kwargs['initialvalue'],ui.DEFAULT_TASK)
        self.assertEqual(self.service.calls,[])

    def test_config_rejects_invalid_tasks(self):
        for task in ('   ','x'*241,'Дело\nДругое'):
            with self.subTest(task=task):
                self.ask.return_value=task
                self.assertFalse(self.panel.configure_cycle())
        self.assertEqual(self.service.calls,[])

    def test_config_checks_state_after_task_dialog(self):
        def dialog(*args,**kwargs):self.service.value['revision']+=1;return 'Полезное дело.'
        self.ask.side_effect=dialog
        self.assertFalse(self.panel.configure_cycle());self.assertEqual(self.service.calls,[])

    def test_disable_requires_parent_and_preserves_separate_study_notice(self):
        self.assertTrue(self.panel.disable_cycle())
        self.panel.authorize_parent.assert_called_once()
        self.assertEqual(self.service.calls,[('configure',False,'Убрать ненужные вещи со стола.',7)])
        self.assertEqual(self.service.grants,0)
        self.assertIn('уроков проверяйте отдельно',self.panel.notice.get())

    def test_disable_does_nothing_when_already_disabled(self):
        self.service.value['enabled']=False
        self.assertFalse(self.panel.disable_cycle())
        self.panel.authorize_parent.assert_not_called();self.assertEqual(self.service.calls,[])

    def test_disable_rejects_cycle_replaced_during_pin(self):
        def authorize(_):self.service.value['revision']=8;return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.disable_cycle());self.assertEqual(self.service.calls,[])

    def test_disabled_rule_cannot_approve_even_with_false_ready_flags(self):
        self.ready_parent();self.service.value['enabled']=False
        self.assertFalse(self.panel.approve_break());self.assertEqual(self.service.calls,[])

    def test_missing_break_identity_cannot_approve(self):
        self.ready_parent();self.service.value['break_id']=None
        self.assertFalse(self.panel.approve_break());self.assertEqual(self.service.calls,[])

    def test_contradictory_phase_and_flags_fail_closed(self):
        for phase in ('blocked','approved','consumed','cancelled','disabled','unknown'):
            with self.subTest(phase=phase):
                self.service.value.update(phase=phase,remaining_break_seconds=0,
                    can_request_done=True,can_approve=True)
                self.assertFalse(self.panel.request_done())
                self.assertFalse(self.panel.approve_break())
        self.panel.authorize_parent.assert_not_called()
        self.assertEqual(self.service.calls,[])

    def test_corrupt_status_disables_controls_and_never_dispatches(self):
        for change in ({'revision':True},{'revision':-1},{'remaining_break_seconds':-1},
                       {'remaining_break_seconds':601},{'can_approve':1},{'break_id':'not-an-id'},
                       {'task':'secret\ncontent'},{'phase':None}):
            before=deepcopy(self.service.value)
            with self.subTest(change=change):
                self.service.value.update(change)
                self.assertFalse(self.panel.refresh())
                self.assertEqual(self.panel.done_button.options['state'],'disabled')
                self.assertEqual(self.panel.approve_button.options['state'],'disabled')
                self.assertFalse(self.panel.approve_break())
            self.service.value=before
        self.assertEqual(self.service.calls,[])

    def test_service_status_exception_is_generic_and_no_mutation(self):
        self.service.status=mock.Mock(side_effect=RuntimeError('PRIVATE PATH OR TOKEN'))
        self.assertFalse(self.panel.refresh());self.assertFalse(self.panel.request_done())
        self.assertNotIn('PRIVATE',self.panel.notice.get())
        self.assertEqual(self.service.calls,[])

    def test_dispatch_failure_is_not_retried_or_claimed_successful(self):
        self.ready_parent()
        self.service.approve_break=mock.Mock(side_effect=RuntimeError('PRIVATE'))
        self.assertFalse(self.panel.approve_break())
        self.service.approve_break.assert_called_once()
        self.assertEqual(self.service.grants,0)
        self.assertIn('повтора нет',self.panel.notice.get())

    def test_closed_handlers_never_read_or_mutate_service(self):
        self.panel.close();reads=self.service.reads
        self.assertFalse(self.panel.refresh());self.assertFalse(self.panel.request_done())
        self.assertFalse(self.panel.approve_break());self.assertFalse(self.panel.configure_cycle())
        self.assertFalse(self.panel.disable_cycle())
        self.assertEqual(self.service.reads,reads);self.assertEqual(self.service.calls,[])

    def test_close_cancels_owned_poll_once_and_does_not_disable_service(self):
        self.panel._timer='owned-timer';self.panel.after_cancel=mock.Mock()
        self.panel.close();self.panel.close()
        self.panel.after_cancel.assert_called_once_with('owned-timer')
        self.assertIsNone(self.panel._timer);self.assertEqual(self.service.calls,[])

    def test_poll_refresh_is_read_only_and_schedules_next_second(self):
        self.panel.after=mock.Mock(return_value='next-timer')
        self.panel._poll()
        self.panel.after.assert_called_once_with(1000,self.panel._poll)
        self.assertEqual(self.panel._timer,'next-timer')
        self.assertEqual(self.service.calls,[])


if __name__=='__main__':unittest.main()
