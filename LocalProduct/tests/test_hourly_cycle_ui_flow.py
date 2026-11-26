"""Real Tk cycle buttons on a nonce-owned NON-input desktop only.

All state, clock, registry and worker dependencies are fixtures. No game window
is inspected or closed; the test does not inject input or touch a personal PIN.
"""
from datetime import date
import gc
import itertools
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SOURCE))
from test_desktop_ui_native import desktop_names
from test_board_approval import Clock, Registry


class HourlyCycleUiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name!='nt':raise unittest.SkipTest('Real Windows required')
        thread,incoming=desktop_names()
        if not re.fullmatch(r'CodexQA_[0-9a-f]{32}',thread) or thread==incoming:
            raise unittest.SkipTest('Nonce-owned non-input QA desktop required')
        cls.desktop_before=thread,incoming

    def setUp(self):
        import tkinter as tk
        import tkinter.simpledialog as dialogs
        import winreg
        from board_approval import BoardApproval
        from daily_plan import DailyPlan
        from family_rewards import RewardBook
        from hourly_break import HourlyBreakStore
        from hourly_break_ui import HourlyBreakPanel
        from hourly_cycle import HourlyCycleControl
        from play_session import PlaySession
        from study_resume import StudyResume

        self.root=None;self.panel=None;self.patches=[];self.errors=[]
        self.addCleanup(self._cleanup)
        base=Path(__file__).resolve().parent/'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='hourly-ui-owned-',dir=base)
        self.data=Path(self.temporary.name)
        self.clock,self.registry=Clock(),Registry()
        self.day=date(2026,11,26)
        self.ensure=mock.Mock()
        self.parent=mock.Mock(return_value=True)
        self.popen=self._patch(mock.patch('subprocess.Popen',side_effect=AssertionError('Real worker launch forbidden')))
        self.socket=self._patch(mock.patch('socket.socket',side_effect=AssertionError('Network forbidden')))
        self.bridge=mock.Mock(side_effect=AssertionError('FamilyBridge forbidden'))
        self._patch(mock.patch.dict(sys.modules,{'family_bridge':SimpleNamespace(FamilyBridge=self.bridge)}))
        for name in ('OpenKey','CreateKey','SetValueEx','DeleteValue','QueryValueEx'):
            self._patch(mock.patch.object(winreg,name,side_effect=AssertionError('Real registry forbidden')))
        self.dialog=self._patch(mock.patch.object(dialogs,'askstring',return_value='Тестовое полезное дело, согласованное с мамой.'))
        self.plans=DailyPlan(self.data/'daily-plan',today=lambda:self.day)
        self.resume=StudyResume(self.data/'study',clock=lambda:self.clock.wall,registry=self.registry)
        self.resume._startup_command=lambda:'"fixture-owned-hourly-controller" --resume'
        counter=itertools.count(1)
        nonce=lambda:format(next(counter),'032x')
        self.play=PlaySession(self.data/'play-time',clock=lambda:self.clock.wall,
            monotonic=lambda:self.clock.mono,today=lambda:self.day,nonce=nonce)
        self.breaks=HourlyBreakStore(self.data/'hourly-break',clock=lambda:self.clock.wall,
            monotonic=lambda:self.clock.mono,today=lambda:self.day,nonce=nonce)
        self.approval=BoardApproval(plans=self.plans,study=self.resume,play=self.play,
            rewards=RewardBook(self.data/'family-rewards',clock=lambda:self.clock.wall),
            root=self.data/'approval',ensure_running=self.ensure,breaks=self.breaks)
        self.control=HourlyCycleControl(self.approval)
        self.root=tk.Tk();self.root.withdraw()
        self.root.geometry('820x420+10+10')
        self.root.title('Owned QA hourly-cycle UI')
        self.root.report_callback_exception=lambda *error:self.errors.append(error)
        self.panel=HourlyBreakPanel(self.root,self.control,authorize_parent=self.parent)
        self.panel.pack(fill='both',expand=True)
        self.update()

    def _patch(self,patcher):
        self.patches.append(patcher)
        return patcher.start()

    def update(self):
        self.root.update_idletasks();self.root.update()
        self.assertEqual(self.errors,[],'Tk callback raised')
        self.assertEqual(desktop_names(),self.desktop_before)

    def _cleanup(self):
        try:
            if self.panel is not None:self.panel.close()
            if self.root is not None:self.root.destroy()
        finally:
            for patcher in reversed(self.patches):patcher.stop()
            self.panel=self.root=None
            gc.collect()
            temporary=getattr(self,'temporary',None)
            if temporary is not None:temporary.cleanup()
            gc.collect()

    def tearDown(self):
        self.popen.assert_not_called();self.socket.assert_not_called();self.bridge.assert_not_called()
        self.assertEqual(desktop_names(),self.desktop_before)

    def configure(self):
        self.panel.configure_button.invoke();self.update()
        self.assertTrue(self.control.status()['enabled'])
        self.assertIsNone(self.play.status()['current'])
        self.assertEqual(self.control.status()['task'],'Тестовое полезное дело, согласованное с мамой.')
        self.ensure.assert_not_called()

    def first_hour(self):
        lease=self.resume.activate(0)
        plan=self.plans.publish(['Первое синтетическое дело'],60,expected_revision=None)
        plan=self.plans.set_task(0,'done',expected_revision=plan['revision'])
        plan=self.plans.submit(expected_revision=plan['revision'],study_session_id=lease['session_id'])
        self.approval.approve(plan_date=plan['date'],revision=plan['revision'],study_id=lease['session_id'],
            request_id='e'*32)
        self.assertIsNone(self.resume.read())
        first=self.play.status()['current']
        self.assertEqual((first['phase'],first['duration_seconds']),('playing',3600))
        return first

    def expire_hour(self):
        self.clock.advance(3601);self.approval.pump();self.panel.refresh();self.update()
        value=self.control.status()
        self.assertEqual(value['phase'],'waiting')
        self.assertEqual(value['remaining_break_seconds'],600)
        self.assertEqual(self.resume.read()['duration_seconds'],0)
        self.assertTrue(self.panel.done_button.instate(['disabled']))
        self.assertTrue(self.panel.approve_button.instate(['disabled']))
        return value

    def test_every_hour_needs_ten_minutes_real_done_request_and_parent_approval(self):
        self.configure();self.parent.reset_mock()
        first=self.first_hour();first_break=self.expire_hour()
        restrictions=self.resume.read()
        self.panel.done_button.invoke();self.panel.approve_button.invoke();self.update()
        self.assertEqual(self.play.status()['current']['play_id'],first['play_id'])
        self.assertEqual(self.resume.read(),restrictions)
        self.parent.assert_not_called()

        self.clock.advance(599);self.approval.pump();self.panel.refresh();self.update()
        self.assertEqual(self.control.status()['remaining_break_seconds'],1)
        self.assertTrue(self.panel.done_button.instate(['disabled']))
        self.panel.done_button.invoke();self.update()
        self.assertEqual(self.control.status()['phase'],'waiting')

        self.clock.advance(1);self.approval.pump();self.panel.refresh();self.update()
        self.assertEqual(self.control.status()['remaining_break_seconds'],0)
        self.assertTrue(self.panel.done_button.instate(['!disabled']))
        self.assertTrue(self.panel.approve_button.instate(['disabled']))
        self.assertEqual(self.resume.read(),restrictions)  # time alone never unlocks
        self.panel.done_button.invoke();self.update()
        self.assertEqual(self.control.status()['phase'],'submitted')
        self.assertEqual(self.play.status()['current']['play_id'],first['play_id'])
        self.assertEqual(self.resume.read(),restrictions)  # child request never unlocks

        self.parent.return_value=False
        self.panel.approve_button.invoke();self.update()
        self.assertEqual(self.control.status()['phase'],'submitted')
        self.assertEqual(self.resume.read(),restrictions)
        self.parent.return_value=True
        self.panel.approve_button.invoke();self.update()
        next_play=self.play.status()['current']
        self.assertNotEqual(next_play['play_id'],first['play_id'])
        self.assertEqual((next_play['phase'],next_play['duration_seconds']),('playing',3600))
        self.assertIsNone(self.resume.read())
        self.assertEqual(self.control.status()['phase'],'consumed')
        deadline=next_play['expires_utc']
        self.panel.approve_button.invoke();self.update()
        self.assertEqual(self.play.status()['current']['expires_utc'],deadline)

        second_break=self.expire_hour()
        self.assertNotEqual(second_break['break_id'],first_break['break_id'])
        self.assertEqual(second_break['current']['source_play_id'],next_play['play_id'])
        self.assertEqual(second_break['remaining_break_seconds'],600)

    def test_parent_disabling_cycle_does_not_clear_current_study_restrictions(self):
        self.configure();self.first_hour();self.expire_hour()
        restrictions=self.resume.read();play=self.play.status()['current']
        self.parent.return_value=False
        self.panel.disable_button.invoke();self.update()
        self.assertTrue(self.control.status()['enabled'])
        self.assertEqual(self.resume.read(),restrictions)
        self.parent.return_value=True
        self.panel.disable_button.invoke();self.update()
        self.assertFalse(self.control.status()['enabled'])
        self.assertEqual(self.resume.read(),restrictions)
        self.assertEqual(self.play.status()['current'],play)
        self.clock.advance(1000);self.approval.pump();self.panel.refresh();self.update()
        self.assertEqual(self.resume.read(),restrictions)
        self.assertTrue(self.panel.done_button.instate(['disabled']))
        self.assertTrue(self.panel.approve_button.instate(['disabled']))

    def test_hourly_panel_buttons_and_text_fit_an_840_by_440_owned_window(self):
        self.root.geometry('840x440+10+10')
        # A long useful-task label exercises actual Tk wrapping, not just the
        # short default phrase. The frame remains on this owned QA desktop.
        task=(('Сначала уточни у мамы, что полезно сделать: убери вещи на место, '
               'протри свободную поверхность и покажи результат. ') * 2)[:240].strip()
        self.control.configure(enabled=True,task=task,
            expected_revision=self.control.status()['revision'])
        self.panel.refresh()
        self.root.deiconify()
        self.update()
        self.assertTrue(self.panel.winfo_ismapped())
        left,top=self.root.winfo_rootx(),self.root.winfo_rooty()
        right,bottom=left+self.root.winfo_width(),top+self.root.winfo_height()
        controls=[self.panel.done_button,self.panel.approve_button,
                  self.panel.configure_button,self.panel.disable_button]
        def labels(parent):
            result=[]
            for child in parent.winfo_children():
                if child.winfo_class()=='TLabel':result.append(child)
                result.extend(labels(child))
            return result
        texts=labels(self.panel)
        self.assertGreaterEqual(len(texts),4)
        for widget in controls+texts:
            with self.subTest(widget=widget.winfo_class(),text=str(widget.cget('text'))[:45]):
                self.assertTrue(widget.winfo_viewable())
                self.assertGreater(widget.winfo_width(),0)
                self.assertGreater(widget.winfo_height(),0)
                self.assertGreaterEqual(widget.winfo_rootx(),left)
                self.assertGreaterEqual(widget.winfo_rooty(),top)
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),right)
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)
        self.assertIsNone(self.play.status()['current'])
        self.assertIsNone(self.resume.read())
        self.ensure.assert_not_called()

    def test_pending_hourly_break_opens_board_without_a_current_daily_plan(self):
        from study_runtime import StudyPlanPopup

        self.control.configure(enabled=True,task='Убери согласованную с мамой полку.',
            expected_revision=self.control.status()['revision'])
        initial=self.resume.activate(0)
        prepared=self.play.prepare(self.day.isoformat(),1,60,initial['session_id'],
            current_study_id=initial['session_id'])['current']
        self.resume.clear(initial['session_id'])
        self.play.confirm_release(prepared['play_id'],current_study_id=None)
        self.clock.advance(3600)
        self.approval.pump()
        current=self.resume.read()
        self.assertEqual(self.control.status()['phase'],'waiting')
        self.assertIsNone(self.plans.read())

        control=self.control
        class FakeStudy:
            supports_game_timer=True
            def hourly_cycle_control(self):return control
        study=FakeStudy()
        study.resume_store=self.resume
        study.notice=mock.Mock()
        popup=StudyPlanPopup(study,self.plans,None)
        self.addCleanup(popup.close)
        owned_lock=mock.MagicMock()
        owned_lock.existed=False
        with mock.patch('study_runtime.RuntimeLock',return_value=owned_lock) as lock_type, \
             mock.patch.object(popup,'_open') as open_board:
            popup.show_for(current['session_id'])
            popup.show_for(current['session_id'])
            open_board.assert_called_once_with()
            lock_type.assert_called_once_with(self.resume.root,'StudyPlanWindow')
            owned_lock.__enter__.assert_called_once_with()
            self.assertEqual(popup.last_session,current['session_id'])
        study.notice.set.assert_not_called()
        self.assertIsNone(self.plans.read())
        self.assertEqual(self.resume.read(),current)
        self.assertEqual(self.control.status()['phase'],'waiting')


if __name__=='__main__':unittest.main()
