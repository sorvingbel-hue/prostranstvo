"""Owned cache ledgers and non-GUI display harness; no real cache deletion."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import cache_cleanup as cc
from cache_cleanup_ui import CacheCleanupPanel


class Notice:
    def __init__(self):self.value=''
    def set(self,value):self.value=value
    def get(self):return self.value


class CacheCleanupSummaryTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'build'
        self.temporary=tempfile.TemporaryDirectory(prefix='cache-summary-owned-',dir=base)
        self.owned=Path(self.temporary.name).resolve()
        self.assertEqual(self.owned.parent,base.resolve())
        self.addCleanup(self.temporary.cleanup)
        self.cache=self.owned/'cache';self.cache.mkdir()
        self.file=self.cache/'old.cache';self.file.write_bytes(b'owned fixture remains untouched')
        self.now=1_800_000_000.0
        os.utime(self.file,(self.now-10*86400,self.now-10*86400))
        self.calls=[]
        self.manager=cc.CacheCleanup(root=self.owned/'ledger',scopes=[
            dict(id='fixture',title='Owned fixture',root=str(self.cache)),
            dict(id='missing',title='Missing owned fixture',root=str(self.owned/'missing'))],
            clock=lambda:self.now,deleter=self.fake_delete)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.manager,'_free',return_value={'owned-volume':1000}).start()
        mock.patch.object(cc,'_native_delete',side_effect=AssertionError('Native delete forbidden')).start()
        mock.patch.object(cc,'_known_local_appdata',side_effect=AssertionError('Real cache lookup forbidden')).start()

    def fake_delete(self,entry):
        self.assertEqual(Path(entry['path']),self.file)
        self.calls.append(deepcopy(entry))
        return {'state':'deleted'}  # No unlink; only the saved result is exercised.

    def prepare(self,partial=True):
        return self.manager.prepare(['fixture','missing'] if partial else ['fixture'])

    def execute(self,plan):
        return self.manager.execute(plan['plan_id'],approved=True)

    def receipt(self,plan):
        path=self.manager._path(plan['plan_id'],receipt=True)
        return path,json.loads(path.read_text(encoding='utf-8'))

    @staticmethod
    def expected_analysis(plan):
        return {key:deepcopy(plan[key]) for key in cc.ANALYSIS_KEYS}

    def rewrite_plan(self,plan):
        unsigned=dict(plan);unsigned.pop('plan_hash')
        plan['plan_hash']=cc._digest(unsigned)
        self.manager._path(plan['plan_id']).write_text(json.dumps(plan),encoding='utf-8')

    def test_partial_analysis_survives_first_result_and_expired_retry(self):
        plan=self.prepare()
        self.assertTrue(plan['partial']);self.assertEqual(plan['error_count'],1)
        first=self.execute(plan)
        self.now+=cc.TTL+1
        second=self.execute(plan)
        self.assertEqual(first['status'],'completed_with_skips')
        self.assertEqual(second['status'],'completed_with_skips')
        self.assertEqual(first['analysis'],self.expected_analysis(plan))
        self.assertEqual(second['analysis'],first['analysis'])
        self.assertEqual(first['errors'],[])  # These are execution errors only.
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.file.exists())

    def test_new_write_ahead_receipt_has_checked_analysis_before_effect(self):
        plan=self.prepare()
        def inspect(entry):
            _path,receipt=self.receipt(plan)
            self.assertEqual(receipt['status'],'running')
            self.assertEqual(receipt['entries'][0]['state'],'in_flight')
            self.assertEqual(receipt['analysis'],self.expected_analysis(plan))
            return self.fake_delete(entry)
        self.manager.deleter=inspect
        self.execute(plan)
        _path,receipt=self.receipt(plan)
        self.assertEqual(receipt['analysis'],self.expected_analysis(plan))

    def test_complete_analysis_can_still_report_clean_completion(self):
        plan=self.prepare(partial=False)
        result=self.execute(plan)
        self.assertEqual(result['status'],'completed')
        self.assertFalse(result['analysis']['partial'])
        self.assertEqual(result['analysis']['error_count'],0)

    def test_nonzero_analysis_error_count_prevents_clean_completion(self):
        plan=self.prepare();plan['partial']=False
        self.rewrite_plan(plan)
        result=self.execute(plan)
        self.assertFalse(result['analysis']['partial'])
        self.assertEqual(result['analysis']['error_count'],1)
        self.assertEqual(result['status'],'completed_with_skips')

    def test_old_completed_receipt_uses_plan_summary_without_rewrite_or_retry(self):
        plan=self.prepare();self.execute(plan)
        path,receipt=self.receipt(plan)
        receipt.pop('analysis');receipt['status']='completed'
        path.write_text(json.dumps(receipt),encoding='utf-8')
        original=path.read_bytes()
        self.now+=cc.TTL+1
        with mock.patch.object(self.manager,'_write',side_effect=AssertionError('Read migration forbidden')):
            result=self.execute(plan)
        self.assertEqual(result['status'],'completed_with_skips')
        self.assertEqual(result['analysis'],self.expected_analysis(plan))
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(len(self.calls),1)

    def test_old_uncertain_receipt_keeps_unknown_outcome_and_is_not_retried(self):
        plan=self.prepare();self.execute(plan)
        path,receipt=self.receipt(plan)
        receipt.pop('analysis');receipt['status']='uncertain'
        receipt['entries'][0].update(state='uncertain',reason='owned unknown outcome')
        path.write_text(json.dumps(receipt),encoding='utf-8')
        original=path.read_bytes()
        result=self.execute(plan)
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(result['deleted_files'],0)
        self.assertEqual(result['analysis'],self.expected_analysis(plan))
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(len(self.calls),1)

    def test_changed_or_malformed_receipt_summary_fails_before_rewrite_or_effect(self):
        plan=self.prepare();self.execute(plan)
        path,original=self.receipt(plan)
        summary=original['analysis']
        variants=[None,[],{},dict(summary,extra=True),dict(summary,partial=1),
                  dict(summary,error_count=True),dict(summary,error_count=-1),
                  dict(summary,errors='invalid'),dict(summary,errors=[None]),
                  dict(summary,errors=[{'path':'owned','reason':4}]),
                  dict(summary,errors=[{'path':'owned','reason':'changed'}]),
                  dict(summary,error_count=2),dict(summary,partial=False)]
        for changed in variants:
            with self.subTest(changed=changed):
                damaged=deepcopy(original);damaged['analysis']=changed
                path.write_text(json.dumps(damaged),encoding='utf-8')
                before=path.read_bytes()
                with mock.patch.object(self.manager,'_write',side_effect=AssertionError('Corrupt receipt mutation forbidden')):
                    with self.assertRaises(cc.CleanupError):self.execute(plan)
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(len(self.calls),1)
        self.assertTrue(self.file.exists())

    def test_inconsistent_plan_sample_count_fails_before_any_effect(self):
        plan=self.prepare();plan['error_count']=0
        self.rewrite_plan(plan)
        with self.assertRaises(cc.CleanupError):self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.manager._path(plan['plan_id'],receipt=True).exists())

    def test_result_analysis_is_detached_from_saved_plan_and_receipt(self):
        plan=self.prepare();result=self.execute(plan)
        result['analysis']['errors'][0]['reason']='caller edit'
        result['analysis']['error_count']=0
        again=self.execute(plan)
        self.assertEqual(again['analysis'],self.expected_analysis(plan))
        self.assertEqual(len(self.calls),1)

    def test_uncertain_fake_delete_keeps_analysis_without_counting_or_retry(self):
        plan=self.prepare()
        def uncertain(entry):
            self.calls.append(deepcopy(entry));raise RuntimeError('Unknown owned outcome')
        self.manager.deleter=uncertain
        first=self.execute(plan);again=self.execute(plan)
        self.assertEqual(first['status'],'uncertain')
        self.assertEqual(again['status'],'uncertain')
        self.assertEqual(first['analysis'],self.expected_analysis(plan))
        self.assertEqual(first['deleted_files'],0)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.file.exists())

    def display(self,receipt):
        # The display method receives plain fake fields; no Tk root or event input.
        panel=SimpleNamespace(_plan='old plan',notice=Notice(),text='')
        panel._text=lambda value:setattr(panel,'text',value)
        CacheCleanupPanel._show_receipt(panel,receipt)
        return panel

    def test_display_separates_analysis_and_execution_errors(self):
        result=self.execute(self.prepare())
        panel=self.display(result)
        self.assertIn('completed_with_skips',panel.notice.get())
        self.assertIn('Анализ частичный',panel.notice.get())
        self.assertIn('ошибок 1',panel.notice.get())
        self.assertIn('ошибок 0 при исполнении',panel.notice.get())
        self.assertIn('Ошибки анализа',panel.text)
        self.assertIn('Исполнение',panel.text)
        self.assertNotIn('Ошибки исполнения',panel.text)
        self.assertIsNone(panel._plan)

    def test_display_never_promotes_uncertain_execution(self):
        result=self.execute(self.prepare())
        result.update(status='uncertain',deleted_files=0,deleted_bytes=0,
                      errors=[{'path':'owned cache','reason':'unknown'}])
        panel=self.display(result)
        self.assertIn('Результат uncertain.',panel.notice.get())
        self.assertIn('Ошибки анализа',panel.text)
        self.assertIn('Ошибки исполнения',panel.text)

    def test_display_demotes_clean_status_when_analysis_is_partial_or_unavailable(self):
        result=self.execute(self.prepare());result['status']='completed'
        self.assertIn('Результат completed_with_skips.',self.display(result).notice.get())
        result.pop('analysis')
        panel=self.display(result)
        self.assertIn('Результат unverified.',panel.notice.get())
        self.assertIn('Полнота анализа не подтверждена',panel.notice.get())

    def test_display_rejects_corrupt_summary_without_claiming_success(self):
        result=self.execute(self.prepare());result['analysis']['error_count']=True
        panel=self.display(result)
        self.assertIn('не прошла проверку',panel.notice.get())
        self.assertNotIn('Результат completed',panel.notice.get())


if __name__=='__main__':unittest.main(verbosity=2)
