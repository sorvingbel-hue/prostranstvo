"""Mapped family board layout on an owned NON-input desktop; no native actions."""
from datetime import date
import gc
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_desktop_ui_native import desktop_names
from test_board_approval import Clock, Registry


class _EmptyDesktop:
    def list_windows(self):return []
    def close_window(self,window):raise AssertionError('Real enforcement forbidden')


class BoardLayoutNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name!='nt':raise unittest.SkipTest('Real Windows required')
        cls.desktop=desktop_names()
        if not re.fullmatch(r'CodexQA_[0-9a-f]{32}',cls.desktop[0]) or cls.desktop[0]==cls.desktop[1]:
            raise unittest.SkipTest('Nonce-owned non-input QA desktop required')

    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        import winreg
        import paths
        import daily_plan
        from daily_plan_ui import DailyPlanPanel
        from study_core import StudySession,StudySettings
        from study_guard import StudyGuard
        from study_resume import StudyResume
        from study_runtime import PersistentStudyPanel

        self.root=self.board=self.study=None
        self.patches=[];self.errors=[];self.forbidden=[]
        self.addCleanup(self.cleanup)
        self.temporary=tempfile.TemporaryDirectory(prefix='owned-board-layout-')
        self.profile=Path(self.temporary.name)
        self.clock,self.registry=Clock(),Registry()
        self.day=date(2026,11,26)
        self.patch(mock.patch.object(paths,'DEPLOY_ROOT',self.profile))
        self.patch(mock.patch.object(daily_plan,'DEPLOY_ROOT',self.profile))
        for target in ('subprocess.Popen','os.system','socket.socket','socket.create_connection',
                       'study_windows.WindowsDesktop','codex_pause.WindowsDesktop',
                       'study_runtime.RuntimeLock','tkinter.messagebox._show',
                       'tkinter.simpledialog.askstring'):
            self.forbidden.append(self.patch(mock.patch(target,side_effect=AssertionError(target))))
        self.forbidden.append(self.patch(mock.patch('os.startfile',side_effect=AssertionError('os.startfile'),create=True)))
        for name in ('OpenKey','CreateKey','SetValueEx','DeleteValue','QueryValueEx'):
            self.forbidden.append(self.patch(mock.patch.object(winreg,name,side_effect=AssertionError('Real registry forbidden'))))
        self.bridge=mock.Mock(side_effect=AssertionError('FamilyBridge forbidden'))
        self.patch(mock.patch.dict(sys.modules,{'family_bridge':SimpleNamespace(FamilyBridge=self.bridge)}))
        self.patch(mock.patch.object(PersistentStudyPanel,'_show_indicator'))
        self.patch(mock.patch.object(PersistentStudyPanel,'_schedule'))
        self.ensure=mock.Mock()
        self.root=tk.Tk();self.root.withdraw()
        self.root.report_callback_exception=lambda *error:self.errors.append(error)
        style=ttk.Style(self.root);style.theme_use('clam')
        style.configure('TLabel',font=('Segoe UI',11))
        style.configure('TButton',font=('Segoe UI',11),padding=9)
        self.guard=StudyGuard(self.profile/'study',clock=lambda:self.clock.wall)
        self.guard.set_pin('82917463')
        self.resume=StudyResume(self.profile/'study',clock=lambda:self.clock.wall,registry=self.registry)
        self.resume._startup_command=lambda:'"owned layout fixture" --resume'
        self.store=daily_plan.DailyPlan(self.profile/'daily-plan',today=lambda:self.day)
        self.store.publish(['Уроки: два коротких примера','Убрать вещи на место','Помочь маме'],60,
                           expected_revision=None)
        self.study=PersistentStudyPanel(self.root,settings=StudySettings(self.profile/'study'),
            session=StudySession(_EmptyDesktop(),clock=lambda:self.clock.mono),guard=self.guard,
            resume=self.resume,ensure_play_running=self.ensure)
        # Use the real service and owned stores, including its current-day source.
        self.study._board_approval.plans=self.store
        self.study._board_approval.breaks.today=lambda:self.day
        self.study.play_store.today=lambda:self.day
        self.board=DailyPlanPanel(self.root,store=self.store,guard=self.guard,study=self.study)
        self.board._family_notifications=False
        self.board.pack(fill='both',expand=True)

    def patch(self,patcher):
        self.patches.append(patcher)
        return patcher.start()

    def update(self):
        self.root.update_idletasks();self.root.update()
        self.assertEqual(self.errors,[],'Tk callback errors')
        self.assertEqual(desktop_names(),self.desktop)

    def show(self,width,height):
        self.root.geometry(f'{width}x{height}+10+10')
        self.root.deiconify();self.update()
        self.assertEqual((self.root.winfo_width(),self.root.winfo_height()),(width,height))

    def assert_inside(self,widgets):
        left,top=self.root.winfo_rootx(),self.root.winfo_rooty()
        right,bottom=left+self.root.winfo_width(),top+self.root.winfo_height()
        for widget in widgets:
            with self.subTest(button=str(widget.cget('text'))):
                self.assertTrue(widget.winfo_viewable(),'critical control is not visible')
                x,y=widget.winfo_rootx(),widget.winfo_rooty()
                width,height=widget.winfo_width(),widget.winfo_height()
                self.assertGreaterEqual(width,widget.winfo_reqwidth(),f'text clipped: {width} < {widget.winfo_reqwidth()}')
                self.assertGreaterEqual(height,widget.winfo_reqheight())
                self.assertGreaterEqual(x,left)
                self.assertGreaterEqual(y,top)
                self.assertLessEqual(x+width,right)
                self.assertLessEqual(y+height,bottom)

    def critical(self):
        return [self.board.submit_button,self.board.edit_button,self.board.approve_button,
                self.board.goals_button,self.board.hourly_button,self.board.guide_button,
                self.board.study_start,self.board.study_stop]

    def test_board_actions_fit_at_960_by_700_and_840_by_600(self):
        for width,height in ((960,700),(840,600)):
            with self.subTest(size=(width,height)):
                self.show(width,height)
                self.assert_inside(self.critical())

    def test_hourly_actions_and_parent_stop_fit_with_active_study_at_840_by_600(self):
        self.resume.activate(0);self.study._sync_lease()
        self.assertTrue(self.study.session.active)
        self.board.hourly_button.invoke()
        self.show(840,600)
        self.assertTrue(self.board._hourly_open)
        self.assert_inside(self.critical()+[self.board.hourlypanel.done_button,
            self.board.hourlypanel.approve_button,self.board.hourlypanel.configure_button,
            self.board.hourlypanel.disable_button])

    def test_approved_plan_retry_and_play_actions_fit_at_840_by_600(self):
        plan=self.store.read()
        for index in range(len(plan['tasks'])):
            plan=self.store.set_task(index,'done',expected_revision=plan['revision'])
        plan=self.store.submit(expected_revision=plan['revision'])
        self.store.review(approved=True,feedback='',expected_revision=plan['revision'],
                          approval_id='a'*32,quality_percent=100)
        self.board.refresh();self.show(840,600)
        self.assertEqual(self.board.approve_button.cget('text'),'Проверить выдачу игры')
        self.assert_inside(self.critical()+[self.board.play_button])

    def test_short_window_keeps_scrollable_tasks_and_restores_scene_after_growth(self):
        from daily_plan import MAX_TASKS
        self.store.publish([f'Полезное тестовое дело {index+1}' for index in range(MAX_TASKS)],60,
                           expected_revision=self.store.read()['revision'])
        self.board.refresh();self.show(840,500)
        self.assertFalse(self.board._scene_row.winfo_ismapped())
        self.assert_inside(self.critical())
        canvas=self.board.task_list.canvas
        self.assertGreater(canvas.winfo_height(),30)
        self.assertLess(canvas.yview()[1],1)
        canvas.yview_moveto(1);self.update()
        last_row=self.board.rows.winfo_children()[-2]
        self.assertLess(last_row.winfo_rooty(),canvas.winfo_rooty()+canvas.winfo_height())
        self.show(960,700)
        self.assertTrue(self.board._scene_row.winfo_ismapped())
        self.assert_inside(self.critical())

    def test_view_switch_preserves_parent_pin_guard_and_long_hourly_task(self):
        self.show(840,600)
        self.board.goals_button.invoke();self.update()
        self.assertTrue(self.board._rewards_open)
        self.assertFalse(self.board._scene_row.winfo_ismapped())
        self.board.hourly_button.invoke();self.update()
        self.assertFalse(self.board._rewards_open)
        self.assertTrue(self.board._hourly_open)
        before=self.board.hourlypanel.service.status()
        with mock.patch('study_ui.simpledialog.askstring',return_value='not-the-pin'):
            self.board.hourlypanel.configure_button.invoke()
        self.assertEqual(self.board.hourlypanel.service.status(),before)
        task=(('Уточни у мамы, какая помощь сейчас полезна: убери вещи по местам, '
               'протри свободную поверхность и покажи ей результат. ')*2)[:240].strip()
        with mock.patch('study_ui.simpledialog.askstring',side_effect=['82917463',task]):
            self.board.hourlypanel.configure_button.invoke()
        self.update()
        self.assertTrue(self.board.hourlypanel.service.status()['enabled'])
        self.assertEqual(self.board.hourlypanel.service.status()['task'],task)
        self.assert_inside(self.critical()+[self.board.hourlypanel.done_button,
            self.board.hourlypanel.approve_button,self.board.hourlypanel.configure_button,
            self.board.hourlypanel.disable_button])
        self.board.hourly_button.invoke();self.update()
        self.assertTrue(self.board.task_list.winfo_ismapped())
        self.assertTrue(self.board._scene_row.winfo_ismapped())
        self.assert_inside(self.critical())

    def test_submit_and_parent_denial_remain_usable_after_layout_change(self):
        self.show(840,600)
        for index in range(len(self.board.plan['tasks'])):
            self.board._task(index,'done',self.board.plan['revision'])
        self.board.submit_button.invoke();self.update()
        self.assertEqual(self.store.read()['status'],'submitted')
        self.assertTrue(self.board.approve_button.instate(['!disabled']))
        with mock.patch('study_ui.simpledialog.askstring',return_value='not-the-pin'):
            self.board.approve_button.invoke()
        self.update()
        self.assertEqual(self.store.read()['status'],'submitted')
        self.assertIsNone(self.study.play_store.status()['current'])
        self.assert_inside(self.critical())

    def test_destroy_board_cancels_owned_polls_and_animation(self):
        self.show(840,600)
        scene=self.board.scene;hourly=self.board.hourlypanel;rewards=self.board.rewardspanel
        scene.celebrate([(0,10,True)],0,0)
        tokens=(self.board._date_after,self.board._game_after,hourly._timer,scene._after_id)
        self.assertTrue(all(tokens))
        self.board.destroy();self.update()
        pending=set(self.root.tk.splitlist(self.root.tk.call('after','info')))
        self.assertTrue(set(tokens).isdisjoint(pending))
        self.assertTrue(self.board._closed)
        self.assertIsNone(self.board._date_after)
        self.assertIsNone(self.board._game_after)
        self.assertTrue(hourly.closed);self.assertIsNone(hourly._timer)
        self.assertTrue(rewards.closed);self.assertIsNone(scene._after_id)

    def tearDown(self):
        for forbidden in self.forbidden:forbidden.assert_not_called()
        self.bridge.assert_not_called();self.ensure.assert_not_called()
        self.assertEqual(desktop_names(),self.desktop)

    def cleanup(self):
        try:
            if self.board is not None and self.board.winfo_exists():self.board.destroy()
            if self.study is not None:self.study.close()
            if self.root is not None:self.root.destroy()
        finally:
            for patcher in reversed(self.patches):patcher.stop()
            self.board=self.study=self.root=None
            gc.collect()
            self.temporary.cleanup()


if __name__=='__main__':unittest.main()
