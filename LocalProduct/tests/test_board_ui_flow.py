"""Real board/PIN/Study UI on a nonce-owned NON-input desktop, fake desktop only.

No ordinary discovery may map these windows to the shared desktop. The guard
runs before Tk construction; every store is a fresh temporary fixture, and real
worker launch, FamilyBridge construction and Python registry calls are forbidden.
"""
from copy import deepcopy
from datetime import date
import gc
import itertools
import math
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


class FakeDesktop:
    """No native enumeration, close or minimize operation exists in this fixture."""
    def __init__(self):self.enumerations=0;self.closed=[]
    def list_windows(self):self.enumerations+=1;return []
    def close_window(self,window):
        self.closed.append(window)
        raise AssertionError('No fixture window may be closed')


class BoardUiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name!='nt':raise unittest.SkipTest('Real Windows required')
        thread,incoming=desktop_names()
        if not re.fullmatch(r'CodexQA_[0-9a-f]{32}',thread) or thread==incoming:
            raise unittest.SkipTest('Nonce-owned non-input QA desktop required')
        cls.desktop_before=thread,incoming

    def setUp(self):
        # All application/Tk imports occur after the class desktop gate.
        import tkinter as tk
        from tkinter import ttk
        import tkinter.simpledialog as dialogs
        import winreg
        from board_approval import BoardApproval
        from daily_plan import DailyPlan
        from daily_plan_ui import DailyPlanPanel
        from family_rewards import RewardBook
        from play_session import PlaySession
        from study_core import StudySession, StudySettings
        from study_guard import StudyGuard
        from study_resume import StudyResume
        from study_runtime import PersistentStudyPanel

        self.stack=[]
        self.root=None
        self.board=None
        self.study_panel=None
        self.errors=[]
        self.addCleanup(self._cleanup)
        directory=Path(__file__).resolve().parent/'tests-work'
        directory.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='board-ui-owned-',dir=directory)
        self.data=Path(self.temporary.name)
        self.clock,self.registry=Clock(),Registry()
        self.day=date(2026,11,26)
        self.desktop=FakeDesktop()
        self.ensure=mock.Mock()
        self.bridge=mock.Mock(side_effect=AssertionError('FamilyBridge forbidden in isolated UI test'))
        self._patch(mock.patch.dict(sys.modules,{'family_bridge':SimpleNamespace(FamilyBridge=self.bridge)}))
        self.popen=self._patch(mock.patch('subprocess.Popen',side_effect=AssertionError('Real worker launch forbidden')))
        for method in ('OpenKey','CreateKey','SetValueEx','DeleteValue','QueryValueEx'):
            self._patch(mock.patch.object(winreg,method,side_effect=AssertionError('Real registry call forbidden')))
        self.dialog=self._patch(mock.patch.object(dialogs,'askstring',return_value='123456'))
        self.number=self._patch(mock.patch.object(dialogs,'askinteger',side_effect=AssertionError('Unexpected numeric dialog')))
        self.plans=DailyPlan(self.data/'daily-plan',today=lambda:self.day)
        self.guard=StudyGuard(self.data/'study',clock=lambda:self.clock.wall)
        self.guard.set_pin('123456')
        self.settings=StudySettings(self.data/'study')
        self.settings.save(duration_minutes=0,delay_seconds=10,allowed_paths=[])
        self.resume=StudyResume(self.data/'study',clock=lambda:self.clock.wall,registry=self.registry)
        self.resume._startup_command=lambda:'"fixture-owned-runtime" --resume'
        counter=itertools.count(1)
        self.play=PlaySession(self.data/'play-time',clock=lambda:self.clock.wall,
            monotonic=lambda:self.clock.mono,today=lambda:self.day,
            nonce=lambda:format(next(counter),'032x'))
        self.rewards=RewardBook(self.data/'family-rewards',clock=lambda:self.clock.wall)
        self.coordinator=BoardApproval(plans=self.plans,study=self.resume,play=self.play,
            rewards=self.rewards,root=self.data/'approval',ensure_running=self.ensure)
        # The default launch_resume_worker argument is bound at class definition;
        # replacing the factories injects the actual no-launch coordinator.
        self.runtime_factory=self._patch(mock.patch('study_runtime.BoardApproval',return_value=self.coordinator))
        self.board_factory=self._patch(mock.patch('daily_plan_ui.BoardApproval',return_value=self.coordinator))
        self.root=tk.Tk()
        self.root.geometry('1280x940+10+10')
        self.root.title('Owned QA board/Study integration')
        self.root.report_callback_exception=lambda *error:self.errors.append(error)
        tabs=ttk.Notebook(self.root);tabs.pack(fill='both',expand=True)
        study_tab,board_tab=ttk.Frame(tabs),ttk.Frame(tabs)
        tabs.add(study_tab,text='Fixture Study');tabs.add(board_tab,text='Fixture board')
        self.session=StudySession(self.desktop,clock=lambda:self.clock.mono)
        self.study_panel=PersistentStudyPanel(study_tab,settings=self.settings,session=self.session,
            guard=self.guard,resume=self.resume)
        self.study_panel.play_store=self.play
        self.study_panel._board_approval=self.coordinator
        self.study_panel.pack(fill='both',expand=True)
        self.board=DailyPlanPanel(board_tab,store=self.plans,guard=self.guard,study=self.study_panel)
        self.board._family_notifications=False
        self.board.rewardspanel.book=self.rewards
        self.board.pack(fill='both',expand=True)
        tabs.select(board_tab)
        self.update()

    def _patch(self,patcher):
        self.stack.append(patcher)
        return patcher.start()

    def update(self):
        self.root.update_idletasks();self.root.update()
        self.assertEqual(self.errors,[],f'Tk callback errors: {self.errors}')
        self.assertEqual(desktop_names(),self.desktop_before)

    def _cleanup(self):
        # Finalize callbacks/variables on the same thread that created Tk.
        try:
            if self.board is not None:self.board.destroy()
            if self.study_panel is not None:self.study_panel.close()
            if self.root is not None:self.root.destroy()
        finally:
            for patcher in reversed(self.stack):patcher.stop()
            self.board=self.study_panel=self.root=None
            gc.collect()
            temporary=getattr(self,'temporary',None)
            if temporary is not None:temporary.cleanup()
            gc.collect()

    def tearDown(self):
        self.popen.assert_not_called()
        self.bridge.assert_not_called()
        self.assertEqual(self.desktop.closed,[])
        self.assertEqual(desktop_names(),self.desktop_before)

    def publish(self):
        value=self.plans.publish(['Первое тестовое дело','Второе тестовое дело'],60,
            expected_revision=None,task_points=[20,10],repeat_daily=False)
        self.board.refresh();self.update()
        return value

    def click_task(self,index):
        rows=[row for row in self.board.rows.winfo_children() if row.winfo_class()=='TFrame']
        check=next(widget for widget in rows[index].winfo_children() if widget.winfo_class()=='TCheckbutton')
        check.invoke();self.update()

    @staticmethod
    def button(parent,text):
        for widget in parent.winfo_children():
            if widget.winfo_class()=='TButton' and widget.cget('text')==text:return widget
            found=BoardUiFlowTests.button(widget,text)
            if found is not None:return found
        return None

    def test_checkbox_submit_pin_approval_hour_and_expiry_return_to_study(self):
        original=self.resume.activate(0)
        self.study_panel._sync_lease();self.study_panel._refresh();self.update()
        self.assertTrue(self.session.active)
        self.publish()
        self.assertTrue(self.board.submit_button.instate(['disabled']))
        self.click_task(0);self.click_task(1)
        self.assertTrue(self.board.submit_button.instate(['!disabled']))
        self.board.submit_button.invoke();self.update()
        submitted=self.plans.read()
        self.assertEqual(submitted['status'],'submitted')
        self.assertEqual(submitted['study_session_id'],original['session_id'])

        self.dialog.return_value='000000'
        self.board.approve_button.invoke();self.update()
        self.assertEqual(self.plans.read()['status'],'submitted')
        self.assertEqual(self.resume.read(),original)
        self.assertIsNone(self.play.status()['current'])

        self.dialog.return_value='123456'
        self.board.approve_button.invoke();self.update()
        self.assertEqual(self.plans.read()['status'],'approved')
        self.assertIsNone(self.resume.read())
        self.assertFalse(self.session.active)
        allowance=self.play.status()['current']
        self.assertEqual((allowance['phase'],allowance['duration_seconds']),('playing',3600))
        self.assertEqual(allowance['expires_utc'],4600)
        self.assertEqual(self.plans.progress()['total_points'],30)
        self.ensure.assert_called_once_with(self.resume)
        self.number.assert_not_called()

        self.clock.advance(3601)
        self.study_panel._sync_lease();self.study_panel._refresh();self.board.refresh();self.update()
        returned=self.resume.read()
        self.assertIsNone(returned['expires_utc'])
        self.assertTrue(self.session.active)
        self.assertFalse(self.session.preview)
        self.assertEqual(self.session.remaining_seconds,math.inf)
        self.assertEqual(self.play.status()['current']['phase'],'completed')
        self.assertEqual(self.play.status()['current']['return_study_id'],returned['session_id'])
        self.assertTrue(self.resume.startup_enabled())
        self.assertGreater(self.desktop.enumerations,0)

    def test_goals_toggle_child_proposal_parent_confirmation_and_prework_agreement(self):
        published=self.publish()
        self.board.goals_button.invoke();self.update()
        panel=self.board.rewardspanel
        self.assertEqual(panel.winfo_manager(),'pack')
        self.assertEqual(self.board.task_list.winfo_manager(),'')
        self.dialog.return_value='Синтетическая цель'
        self.number.side_effect=None;self.number.return_value=100
        self.button(panel,'Моя большая цель').invoke();self.update()
        goal=self.rewards.snapshot()['goals'][0]
        self.assertEqual(goal['status'],'proposed')
        panel.goals.selection_set(goal['goal_id'])

        self.dialog.return_value='000000'
        self.button(panel,'Подтвердить цель').invoke();self.update()
        self.assertEqual(self.rewards.snapshot()['goals'][0]['status'],'proposed')
        self.dialog.return_value='123456'
        self.button(panel,'Подтвердить цель').invoke();self.update()
        self.assertEqual(self.rewards.snapshot()['goals'][0]['status'],'confirmed')

        self.dialog.side_effect=['123456','Оба тестовых дела выполнены по согласованному списку.']
        self.number.return_value=20
        self.button(panel,'Договориться о наградах').invoke();self.update()
        contract=self.rewards.snapshot()['agreements'][0]
        self.assertEqual(contract['plan_revision'],published['contract_revision'])
        self.assertEqual(contract['tasks'][0]['rewards'][0]['amount'],60)
        self.assertEqual(contract['tasks'][0]['rewards'][1]['goal_id'],goal['goal_id'])
        self.assertEqual(contract['ratings'],{})
        self.assertEqual(self.plans.read(),published)
        self.assertIsNone(self.play.status()['current'])
        self.assertFalse(self.session.active)
        self.ensure.assert_not_called()
        self.board.goals_button.invoke();self.update()
        self.assertEqual(panel.winfo_manager(),'')
        self.assertEqual(self.board.task_list.winfo_manager(),'pack')


if __name__=='__main__':unittest.main()
