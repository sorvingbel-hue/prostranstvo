"""Cache-only fixtures; never inspect or delete the user's real cache folders."""
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import cache_cleanup as cc


class CacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.base=Path(__file__).resolve().parents[1]/'build'/'cache-cleanup-tests'
        self.base.mkdir(parents=True,exist_ok=True)
        self.owned=self.base/('owned-'+uuid.uuid4().hex)
        self.owned.mkdir()
        self.cache=self.owned/'shader-cache';self.cache.mkdir()
        self.now=time.time()
        self.calls=[]
        self.manager=cc.CacheCleanup(root=self.owned/'ledger',scopes=[dict(id='fixture',title='Owned fixture cache',root=str(self.cache))],clock=lambda:self.now,deleter=self.fake_delete)

    def tearDown(self):
        # Only a directory created by this test, under this exact fixed fixture parent.
        self.assertEqual(self.owned.resolve().parent,self.base.resolve())
        self.assertTrue(self.owned.name.startswith('owned-'))
        for path in self.owned.rglob('*'):
            if path.is_file() and not path.is_symlink():
                try:path.chmod(0o600)
                except OSError:pass
        shutil.rmtree(self.owned)

    def file(self,name='old.cache',age=8,payload=b'owned shader cache'):
        path=self.cache/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
        stamp=self.now-age*86400;os.utime(path,(stamp,stamp))
        return path

    def fake_delete(self,entry):
        self.calls.append(entry['path']);Path(entry['path']).unlink();return {'state':'deleted'}

    def prepare(self):return self.manager.prepare(['fixture'])

    def rewrite(self,plan):
        value=dict(plan);value.pop('plan_hash',None);plan['plan_hash']=cc._digest(value)
        self.manager._path(plan['plan_id']).write_text(json.dumps(plan),encoding='utf-8')

    def test_constructor_and_catalog_do_not_write_or_expose_mutable_scope(self):
        self.assertFalse(self.manager.root.exists())
        catalog=self.manager.scope_catalog();catalog[0]['root']='changed'
        self.assertEqual(self.manager.scope_catalog()[0]['root'],str(self.cache))
        with mock.patch.object(cc,'_known_local_appdata',return_value=self.owned/'local-app-data'):
            default=cc.CacheCleanup(root=self.owned/'other-ledger')
        self.assertEqual([s['id'] for s in default.scope_catalog()],['nvidia_dxcache','nvidia_glcache','windows_d3dscache'])
        self.assertFalse(default.root.exists())

    def test_prepare_selects_old_regular_files_without_deletion(self):
        old=self.file();self.file('recent.cache',age=1);self.file('personal.pdf');self.file('nested/old.bin')
        for name in ('script.ps1','script.py','script.bat','personal.sqlite','personal.db'):
            self.file(name)
        plan=self.prepare()
        self.assertEqual(plan['totals']['files'],2)
        self.assertEqual({Path(e['path']).name for e in plan['entries']},{'old.cache','old.bin'})
        self.assertTrue(old.exists());self.assertEqual(self.calls,[])
        self.assertEqual(cc._epoch(plan['expires_utc'])-cc._epoch(plan['created_utc']),1800)

    def test_minimum_age_and_scope_fail_closed(self):
        for age in (0,-1,True,0.5):
            with self.assertRaises(cc.CleanupError):self.manager.prepare(['fixture'],min_age_days=age)
        for scopes in ([],['fixture','fixture'],['anything'],r'C:\Windows'):
            with self.assertRaises(cc.CleanupError):self.manager.prepare(scopes)
        self.assertFalse(self.manager.root.exists())

    def test_approval_is_required_and_expired_plan_is_rejected(self):
        path=self.file();plan=self.prepare()
        for approved in (False,1,'yes',None):
            with self.assertRaises(cc.CleanupError):self.manager.execute(plan['plan_id'],approved=approved)
        self.now+=1801
        with self.assertRaises(cc.CleanupError):self.manager.execute(plan['plan_id'],approved=True)
        self.assertTrue(path.exists());self.assertFalse(self.calls)

    def test_tamper_schema_outside_scope_and_long_ttl_are_rejected(self):
        self.file();original=self.prepare()
        variants=[]
        plan=json.loads(json.dumps(original));plan['entries'][0]['size']+=1;variants.append(plan)
        plan=json.loads(json.dumps(original));plan['unknown']=True;variants.append(plan)
        for plan in variants:
            self.rewrite(plan)
            with self.assertRaises(cc.CleanupError):self.manager.execute(plan['plan_id'],approved=True)
        plan=json.loads(json.dumps(original));plan['entries'][0]['path']=str(self.owned/'outside.cache');self.rewrite(plan)
        with self.assertRaises(cc.CleanupError):self.manager.execute(plan['plan_id'],approved=True)
        plan=json.loads(json.dumps(original));plan['expires_utc']=cc._utc(self.now+1801);self.rewrite(plan)
        with self.assertRaises(cc.CleanupError):self.manager.execute(plan['plan_id'],approved=True)
        self.assertFalse(self.calls)

    def test_changed_file_is_not_deleted(self):
        path=self.file();plan=self.prepare();path.write_bytes(b'changed after approval preview')
        result=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(result['deleted_files'],0);self.assertEqual(result['status'],'completed_with_skips')
        self.assertTrue(path.exists());self.assertFalse(self.calls)

    def test_duplicate_or_nonfinite_json_plan_fails_before_action(self):
        path=self.file();plan=self.prepare();ledger=self.manager._path(plan['plan_id'])
        for raw in ('{"plan_id":"x","plan_id":"y"}','{"bad":NaN}','not json'):
            ledger.write_text(raw,encoding='utf-8')
            with self.assertRaises(ValueError):self.manager.execute(plan['plan_id'],approved=True)
        self.assertFalse(self.calls);self.assertTrue(path.exists())

    def test_identity_replacement_even_same_bytes_and_mtime_is_skipped(self):
        path=self.file();plan=self.prepare();old=plan['entries'][0]
        moved=self.cache/'preserve-original.cache';path.rename(moved)
        path.write_bytes(moved.read_bytes());os.utime(path,ns=(old['mtime_ns'],old['mtime_ns']))
        result=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(result['deleted_files'],0);self.assertFalse(self.calls)
        self.assertTrue(path.exists());self.assertTrue(moved.exists())

    def test_offline_and_reparse_flags_never_enter_plan(self):
        path=self.file();snapshot=cc._snapshot
        for flag in (0x400,0x1000,0x40000,0x400000):
            def flagged(candidate):
                info=snapshot(candidate)
                if Path(candidate)==path:info['attributes']|=flag
                return info
            with mock.patch.object(cc,'_snapshot',side_effect=flagged):plan=self.prepare()
            self.assertEqual(plan['totals']['files'],0)
        self.assertTrue(path.exists())

    def test_default_scope_format_allowlists_are_distinct(self):
        info={'attributes':0,'nlink':1}
        self.assertIsNone(cc._file_reason('shader.bin',info,'nvidia_dxcache'))
        self.assertIsNone(cc._file_reason('shader.toc',info,'nvidia_glcache'))
        self.assertIsNone(cc._file_reason('shader.dxcache',info,'windows_d3dscache'))
        self.assertEqual(cc._file_reason('personal.bin',info,'windows_d3dscache'),'unknown_cache_filename')
        self.assertEqual(cc._file_reason('personal.db',info,'nvidia_dxcache'),'unknown_cache_filename')

    def test_receipt_reuse_never_deletes_replacement(self):
        path=self.file();plan=self.prepare();first=self.manager.execute(plan['plan_id'],approved=True)
        path.write_bytes(b'new replacement must survive');self.now+=3600
        second=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(first['deleted_files'],1);self.assertEqual(first['deleted_bytes'],len(b'owned shader cache'))
        self.assertEqual(second['deleted_files'],1);self.assertEqual(len(self.calls),1)
        self.assertEqual(path.read_bytes(),b'new replacement must survive')

    def test_uncertain_action_is_not_counted_or_retried(self):
        path=self.file();plan=self.prepare()
        def uncertain(entry):self.calls.append(entry['path']);Path(entry['path']).unlink();raise RuntimeError('interrupted after syscall')
        self.manager.deleter=uncertain
        first=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(first['status'],'uncertain');self.assertEqual(first['deleted_files'],0)
        self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(len(self.calls),1);self.assertFalse(path.exists())

    def test_write_ahead_failure_prevents_action_and_commit_failure_stays_uncertain(self):
        path=self.file();plan=self.prepare();write=self.manager._write
        def fail_before(*args,**kwargs):raise OSError('no ledger storage')
        with mock.patch.object(self.manager,'_write',side_effect=fail_before):
            with self.assertRaises(OSError):self.manager.execute(plan['plan_id'],approved=True)
        self.assertTrue(path.exists());self.assertFalse(self.calls)
        counter=[0]
        def fail_after(*args,**kwargs):
            counter[0]+=1
            if counter[0]==3:raise OSError('commit unavailable')
            return write(*args,**kwargs)
        with mock.patch.object(self.manager,'_write',side_effect=fail_after):
            result=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(result['status'],'uncertain');self.assertEqual(result['deleted_files'],0)
        resumed=self.manager.execute(plan['plan_id'],approved=True)
        self.assertEqual(resumed['status'],'uncertain');self.assertEqual(resumed['deleted_files'],0)
        self.assertEqual(len(self.calls),1)

    def test_read_only_hardlinks_and_alternate_streams_are_skipped(self):
        readonly=self.file('readonly.cache');readonly.chmod(0o400)
        hard=self.file('hard.cache');os.link(hard,self.cache/'hard-other.cache')
        if os.name=='nt':
            alternate=self.file('streams.cache');Path(str(alternate)+':private').write_bytes(b'not cache')
        plan=self.prepare()
        self.assertEqual(plan['totals']['files'],0)
        self.assertFalse(self.calls)

    def test_reparse_ancestor_is_rejected_when_available(self):
        target=self.owned/'outside';target.mkdir();(target/'outside.cache').write_bytes(b'preserve')
        try:(self.cache/'link').symlink_to(target,target_is_directory=True)
        except (OSError,NotImplementedError):self.skipTest('directory symlink privilege unavailable')
        plan=self.prepare();self.assertEqual(plan['totals']['files'],0)
        self.assertTrue((target/'outside.cache').exists())

    def test_bounds_mark_partial_without_exceeding_byte_or_file_limit(self):
        self.file('one.cache');self.file('two.cache')
        with mock.patch.object(cc,'MAX_FILES',1):plan=self.prepare()
        self.assertLessEqual(plan['totals']['files'],1);self.assertTrue(plan['partial'])
        with mock.patch.object(cc,'MAX_BYTES',1):plan=self.prepare()
        self.assertEqual(plan['totals']['files'],0);self.assertTrue(plan['partial'])

    @unittest.skipUnless(os.name=='nt','owned NTFS handle deletion requires Windows')
    def test_actual_native_delete_and_busy_handle_skip_only_owned_fixtures(self):
        old=self.file();busy=self.file('busy.cache');plan=self.prepare()
        native=cc._native();handle=native.k.CreateFileW(str(busy),0x80000000,1,None,3,0x00200000,None)
        self.assertNotEqual(handle,cc.ctypes.c_void_p(-1).value)
        self.manager.deleter=cc._native_delete
        try:result=self.manager.execute(plan['plan_id'],approved=True)
        finally:native.close(handle)
        self.assertFalse(old.exists());self.assertTrue(busy.exists())
        self.assertEqual(result['deleted_files'],1);self.assertEqual(len(result['skipped']),1)
        self.assertEqual(result['status'],'completed_with_skips')

    @unittest.skipUnless(os.name=='nt','owned NTFS delete-pending requires Windows')
    def test_actual_delete_pending_is_uncertain_until_other_reader_closes(self):
        path=self.file();plan=self.prepare();native=cc._native()
        handle=native.k.CreateFileW(str(path),0x80000000,7,None,3,0x00200000,None)
        self.assertNotEqual(handle,cc.ctypes.c_void_p(-1).value)
        self.manager.deleter=cc._native_delete
        try:
            result=self.manager.execute(plan['plan_id'],approved=True)
            self.assertEqual(result['status'],'uncertain');self.assertEqual(result['deleted_files'],0)
        finally:native.close(handle)
        self.assertFalse(path.exists())
        self.assertEqual(self.manager.execute(plan['plan_id'],approved=True)['deleted_files'],0)


if __name__=='__main__':unittest.main()
