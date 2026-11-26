"""Fake widgets/dialogs plus temporary RewardBook; no Tk or actual parent prompts."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]/'LocalProduct/src'))
sys.path.insert(0,str(HERE))
import family_rewards_ui as ui
from family_rewards import RewardBook


class Value:
    def __init__(self):self.value=''
    def set(self,value):self.value=value
    def get(self):return self.value


class Tree:
    def __init__(self):self.items={};self.selected=()
    def selection(self):return self.selected
    def get_children(self):return tuple(self.items)
    def delete(self,*ids):
        for ident in ids:self.items.pop(ident,None)
    def insert(self,parent,index,*,iid,text,values):self.items[iid]=(text,values)
    def exists(self,ident):return ident in self.items
    def selection_set(self,ident):self.selected=(ident,)


class RewardsUiTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='rewards-ui-fixture-',dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.book=RewardBook(Path(self.temporary.name)/'data',clock=lambda:1000)
        self.plan={'date':'2026-11-26','revision':45,'contract_revision':40,'status':'open',
                   'game_minutes':60,'tasks':[{'text':'Первое дело','status':'pending'}]}
        self.panel=object.__new__(ui.RewardsPanel)
        self.panel.book=self.book;self.panel.get_plan=lambda:deepcopy(self.plan)
        self.panel.authorize_parent=mock.Mock(return_value=True)
        self.panel.today=lambda:date(2026,11,26);self.panel.closed=False
        self.panel.status,self.panel.notice,self.panel.goals=Value(),Value(),Tree()
        self.panel.winfo_toplevel=lambda:None
        self.addCleanup(mock.patch.stopall)
        self.askstring=mock.patch.object(ui.simpledialog,'askstring').start()
        self.askinteger=mock.patch.object(ui.simpledialog,'askinteger').start()

    def revision(self):return self.book.snapshot()['revision']

    def add_goal(self,confirmed=True):
        result=self.book.propose_goal('Ласты',100,'баллы',expected_revision=self.revision())
        gid=result['goal']['goal_id']
        if confirmed:self.book.confirm_goal(gid,expected_revision=self.revision())
        self.panel.refresh();self.panel.goals.selection_set(gid)
        return gid

    def prepare(self):
        self.askstring.return_value='Выполнены все дела по нашему списку.'
        self.assertTrue(self.panel.configure_agreement())

    def test_refresh_is_pure_and_never_configures_contract(self):
        self.panel.refresh()
        self.assertFalse(self.book.root.exists())
        self.panel.authorize_parent.assert_not_called()
        self.askstring.assert_not_called();self.askinteger.assert_not_called()

    def test_child_proposal_needs_no_parent_and_remains_unconfirmed(self):
        self.askstring.return_value=' Ласты ';self.askinteger.return_value=100
        self.assertTrue(self.panel.propose_goal())
        goal=self.book.snapshot()['goals'][0]
        self.assertEqual((goal['title'],goal['status']),('Ласты','proposed'))
        self.panel.authorize_parent.assert_not_called()

    def test_cancel_proposal_does_not_create_store(self):
        self.askstring.return_value=None
        self.assertFalse(self.panel.propose_goal())
        self.assertFalse(self.book.root.exists())

    def test_goal_confirmation_requires_parent_and_retirement_preserves_balance(self):
        gid=self.add_goal(confirmed=False)
        self.panel.authorize_parent.return_value=False
        self.assertFalse(self.panel.confirm_goal())
        self.assertEqual(self.book.snapshot()['goals'][0]['status'],'proposed')
        self.panel.authorize_parent.return_value=True
        self.assertTrue(self.panel.confirm_goal())
        self.assertTrue(self.panel.retire_goal())
        self.assertEqual(self.book.snapshot()['goals'][0]['status'],'retired')
        self.assertEqual(self.book.snapshot()['goals'][0]['goal_id'],gid)

    def test_parent_goal_confirmation_cannot_act_on_changed_selection(self):
        gid=self.add_goal(False)
        def authorize(_):self.panel.goals.selected=();return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.confirm_goal())
        self.assertEqual(self.book.snapshot()['goals'][0]['status'],'proposed')

    def test_agreement_cannot_be_created_after_work_begins_or_on_past_day(self):
        self.askstring.return_value='Критерий'
        for change in [{'status':'submitted'},{'tasks':[{'text':'Дело','status':'done'}]},
                       {'date':'2026-11-25'},{'contract_revision':None}]:
            before=deepcopy(self.plan);self.plan.update(change)
            self.assertFalse(self.panel.configure_agreement())
            self.assertFalse(self.book.root.exists())
            self.plan=before
        self.panel.authorize_parent.assert_not_called()

    def test_denied_parent_cannot_prepare_contract(self):
        self.panel.authorize_parent.return_value=False
        self.assertFalse(self.panel.configure_agreement())
        self.assertFalse(self.book.root.exists())
        self.askstring.assert_not_called()

    def test_configures_whole_plan_before_work_without_editing_daily_plan(self):
        before=deepcopy(self.plan);self.prepare()
        self.assertEqual(self.plan,before)
        agreement=self.book.snapshot()['agreements'][0]
        self.assertEqual((agreement['plan_revision'],agreement['ratings']),(40,{}))
        self.assertEqual(agreement['tasks'][0]['rewards'][0]['amount'],60)
        self.assertEqual(agreement['tasks'][0]['criteria'],'Выполнены все дела по нашему списку.')
        self.assertEqual(agreement['credited'],{'plan/play':0})

    def test_explicit_confirmed_goal_allocation_and_progress(self):
        gid=self.add_goal();self.askinteger.return_value=20;self.prepare()
        agreement=self.book.snapshot()['agreements'][0]
        self.assertEqual(agreement['tasks'][0]['rewards'][1]['goal_id'],gid)
        self.book.evaluate(self.plan['date'],40,{'plan':50},review_id='review1',comment='Стол ещё не закончен.',expected_revision=self.revision())
        self.panel.refresh()
        self.assertIn('10 / 100 баллы',self.panel.goals.items[gid][1][0])
        self.assertIn('50%',self.panel.status.get())

    def test_unconfirmed_selected_goal_cannot_be_auto_confirmed_by_agreement(self):
        self.add_goal(False)
        self.assertFalse(self.panel.configure_agreement())
        self.assertEqual(self.book.snapshot()['agreements'],[])
        self.panel.authorize_parent.assert_not_called()

    def test_plan_changed_during_pin_prevents_dialog_and_write(self):
        def authorize(_):self.plan['revision']+=1;return True
        self.panel.authorize_parent=authorize
        self.assertFalse(self.panel.configure_agreement())
        self.askstring.assert_not_called()
        self.assertFalse(self.book.root.exists())

    def test_plan_changed_during_criteria_dialog_prevents_write(self):
        def criteria(*args,**kwargs):self.plan['tasks'][0]['status']='done';return 'Критерии'
        self.askstring.side_effect=criteria
        self.assertFalse(self.panel.configure_agreement())
        self.assertFalse(self.book.root.exists())

    def test_reward_book_changed_during_dialog_prevents_contract_write(self):
        def criteria(*args,**kwargs):
            self.book.propose_goal('Другая цель',100,'баллы',expected_revision=self.revision());return 'Критерий'
        self.askstring.side_effect=criteria
        self.assertFalse(self.panel.configure_agreement())
        self.assertEqual(self.book.snapshot()['agreements'],[])

    def test_existing_agreement_criteria_never_reconfigured(self):
        self.prepare();raw=self.book.path.read_bytes();self.askstring.reset_mock()
        self.assertFalse(self.panel.configure_agreement())
        self.assertEqual(self.book.path.read_bytes(),raw)
        self.askstring.assert_not_called()

    def test_cancel_or_empty_criteria_does_not_save_or_invent_default(self):
        for value in (None,'   '):
            self.askstring.return_value=value
            self.assertFalse(self.panel.configure_agreement())
            self.assertEqual(self.book.snapshot()['agreements'],[])

    def test_child_revisit_is_local_request_not_review_or_telegram_notification(self):
        self.prepare()
        self.book.evaluate(self.plan['date'],40,{'plan':50},review_id='parent1',comment='Нужно закончить стол.',expected_revision=self.revision())
        self.askstring.return_value='Я поправил.'
        self.panel.authorize_parent.reset_mock()
        self.assertTrue(self.panel.request_revisit())
        agreement=self.book.snapshot()['agreements'][0]
        self.assertEqual(agreement['credited']['plan/play'],30)
        self.assertEqual(len(agreement['reviews']),1)
        self.assertTrue(agreement['revisits']['plan']['requested'])
        self.assertIn('не отправлялось',self.panel.notice.get())
        self.panel.authorize_parent.assert_not_called()

    def test_no_revisit_prompt_without_actual_rating(self):
        self.prepare();self.askstring.reset_mock()
        self.assertFalse(self.panel.request_revisit())
        self.askstring.assert_not_called()

    def test_storage_error_not_reported_as_success_and_not_raw_exception(self):
        self.book.snapshot=mock.Mock(side_effect=OSError('fixture private detail'))
        self.panel.refresh()
        self.assertNotIn('fixture private detail',self.panel.notice.get())
        self.assertIn('не подтверждено',self.panel.status.get())

    def test_close_disables_all_mutating_handlers_without_tk(self):
        self.panel.close()
        self.assertFalse(self.panel.propose_goal())
        self.assertFalse(self.panel.confirm_goal())
        self.assertFalse(self.panel.retire_goal())
        self.assertFalse(self.panel.configure_agreement())
        self.assertFalse(self.panel.request_revisit())
        self.panel.refresh()
        self.assertFalse(self.book.root.exists())


if __name__=='__main__':unittest.main()
