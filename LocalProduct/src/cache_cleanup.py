"""Explicit shader-cache cleanup, never a general-purpose deletion endpoint.

Default scope is three current-user cache folders. Constructors do not create
files or delete anything. A persisted fresh plan and approved=True are required.
The scope/deleter injection points are for owned test fixtures, not MCP options.
"""
from __future__ import annotations
import contextlib
from copy import deepcopy
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from activity_core import exclusive_lock

MAX_FILES = 5000
MAX_BYTES = 4 * 1024**3
MAX_PLAN_BYTES = 8 * 1024**2
MAX_SAMPLES = 200
TTL = 30 * 60
UNSAFE = 0x400 | 0x1000 | 0x40000 | 0x400000
# Narrow shader-cache formats, not a general extension blocklist. Unknown names
# are left alone even inside an approved cache folder.
CACHE_SUFFIXES = {'.bin','.cache','.toc','.dxcache'}
SCOPE_SUFFIXES = {'nvidia_dxcache':{'.bin','.toc'},'nvidia_glcache':{'.bin','.toc'},
                  'windows_d3dscache':{'.dxcache'}}
STAMP = ('size','mtime_ns','ctime_ns','ino','dev','file_id','volume_serial','attributes','nlink')
ENTRY_KEYS = {'scope_id','path',*STAMP}
PLAN_KEYS = {'version','kind','plan_id','created_utc','expires_utc','min_age_days','scopes',
             'entries','totals','skipped','errors','skipped_count','error_count','partial','plan_hash'}
ANALYSIS_KEYS = {'partial','skipped_count','error_count','skipped','errors'}

class CleanupError(ValueError):
    pass

class CleanupUncertain(RuntimeError):
    pass

def _utc(value):
    return dt.datetime.fromtimestamp(value,dt.timezone.utc).isoformat()

def _epoch(value):
    if not isinstance(value,str):
        raise CleanupError('invalid_plan_time')
    parsed=dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset()!=dt.timedelta(0):
        raise CleanupError('invalid_plan_time')
    return parsed.timestamp()

def _key(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))

def _inside(path,root):
    try:return os.path.commonpath((_key(path),_key(root)))==_key(root)
    except ValueError:return False

def _absolute(value):
    path=Path(value)
    if not path.is_absolute() or '\0' in str(path):
        raise CleanupError('absolute_path_required')
    path=Path(os.path.abspath(path))
    if os.name=='nt':
        if path.drive.upper() not in ('C:','D:'):
            raise CleanupError('local_c_or_d_required')
        if any(p.endswith((' ','.')) or any(c in p for c in '<>:"|?*') for p in path.parts[1:]):
            raise CleanupError('ambiguous_path')
    return path

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def _json_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise CleanupError('duplicate_json_key')
        value[key]=item
    return value

def _invalid_json_constant(value):
    raise CleanupError('nonfinite_json_value')

class _Info(ctypes.Structure):
    _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),('accessed',wintypes.FILETIME),
              ('written',wintypes.FILETIME),('volume',wintypes.DWORD),('size_hi',wintypes.DWORD),
              ('size_lo',wintypes.DWORD),('links',wintypes.DWORD),('index_hi',wintypes.DWORD),
              ('index_lo',wintypes.DWORD)]

class _FileId(ctypes.Structure):
    _fields_=[('volume',ctypes.c_ulonglong),('file_id',ctypes.c_ubyte*16)]

class _Native:
    def __init__(self):
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        definitions=(
            ('CreateFileW',[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE],wintypes.HANDLE),
            ('CloseHandle',[wintypes.HANDLE],wintypes.BOOL),
            ('GetFileInformationByHandle',[wintypes.HANDLE,ctypes.POINTER(_Info)],wintypes.BOOL),
            ('GetFileInformationByHandleEx',[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
            ('GetFinalPathNameByHandleW',[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD],wintypes.DWORD),
            ('SetFileInformationByHandle',[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
            ('GetDriveTypeW',[wintypes.LPCWSTR],wintypes.UINT))
        for name,args,result in definitions:
            function=getattr(self.k,name);function.argtypes=args;function.restype=result

    def open(self,path,*,delete=False,directory=False):
        access=0x80 | (0x10000 if delete else 0)
        share=1 if delete else (3 if directory else 7)
        flags=0x00200000 | (0x02000000 if directory else 0)
        handle=self.k.CreateFileW(str(path),access,share,None,3,flags,None)
        if handle==ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def info(self,handle,path):
        info=_Info();identity=_FileId()
        if not self.k.GetFileInformationByHandle(handle,ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.k.GetFileInformationByHandleEx(handle,18,ctypes.byref(identity),ctypes.sizeof(identity)):
            raise ctypes.WinError(ctypes.get_last_error())
        buffer=ctypes.create_unicode_buffer(32768)
        length=self.k.GetFinalPathNameByHandleW(handle,buffer,len(buffer),0)
        if not length or length>=len(buffer):
            raise CleanupError('final_path_unavailable')
        final=buffer.value
        if final.startswith('\\\\?\\'):
            final=final[4:]
        if _key(final)!=_key(path):
            raise CleanupError('final_path_mismatch')
        ticks=lambda value:(value.dwHighDateTime<<32)|value.dwLowDateTime
        return dict(size=(info.size_hi<<32)|info.size_lo,
                    mtime_ns=(ticks(info.written)-116444736000000000)*100,
                    ctime_ns=(ticks(info.created)-116444736000000000)*100,
                    ino=(info.index_hi<<32)|info.index_lo,dev=int(identity.volume),
                    file_id=bytes(identity.file_id).hex(),volume_serial=int(identity.volume),
                    attributes=int(info.attributes),nlink=int(info.links))

    def close(self,handle):
        return bool(self.k.CloseHandle(handle))

_native_instance=None
def _native():
    global _native_instance
    if _native_instance is None:
        _native_instance=_Native()
    return _native_instance

@contextlib.contextmanager
def _pin_directories(path):
    path=_absolute(path);handles=[]
    try:
        for parent in reversed((path,*path.parents)):
            if os.name=='nt':
                native=_native()
                handle=native.open(parent,directory=True);handles.append(handle)
                info=native.info(handle,parent)
                if not info['attributes']&0x10 or info['attributes']&UNSAFE:
                    raise CleanupError('unsafe_ancestor')
            else:
                info=parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
                    raise CleanupError('unsafe_ancestor')
        yield
    finally:
        if os.name=='nt':
            for handle in reversed(handles):
                _native().close(handle)

def _snapshot(path):
    path=_absolute(path)
    if os.name=='nt' and _native().k.GetDriveTypeW(str(path.anchor))!=3:
        raise CleanupError('fixed_local_volume_required')
    with _pin_directories(path.parent):
        if os.name=='nt':
            handle=_native().open(path,directory=True)
            try:
                result=_native().info(handle,path)
                if not result['attributes']&(0x10|UNSAFE):
                    from engine import no_alternate_streams
                    try:no_alternate_streams(path)
                    except ValueError as error:raise CleanupError('alternate_streams') from error
                return result
            finally:_native().close(handle)
        value=path.lstat()
        if path.is_symlink():
            raise CleanupError('reparse_or_link')
        return dict(size=value.st_size,mtime_ns=value.st_mtime_ns,ctime_ns=value.st_ctime_ns,
                    ino=value.st_ino,dev=value.st_dev,file_id=f'{value.st_ino:032x}',
                    volume_serial=value.st_dev,attributes=0x10 if stat.S_ISDIR(value.st_mode) else (1 if not value.st_mode&0o222 else 0),
                    nlink=value.st_nlink)

def _file_reason(path,info,scope_id=None):
    if info['attributes']&0x10:return 'directory_not_deleted'
    if info['attributes']&UNSAFE:return 'reparse_offline_or_cloud'
    if info['attributes']&1:return 'read_only'
    if info['nlink']!=1:return 'hardlink'
    name=Path(path).name.casefold()
    if Path(name).suffix not in SCOPE_SUFFIXES.get(scope_id,CACHE_SUFFIXES):
        return 'unknown_cache_filename'
    return None

def _known_local_appdata():
    if os.name!='nt':
        raise CleanupError('default_scopes_require_windows')
    class GUID(ctypes.Structure):
        _fields_=[('a',ctypes.c_ulong),('b',ctypes.c_ushort),('c',ctypes.c_ushort),('d',ctypes.c_ubyte*8)]
    guid=GUID.from_buffer_copy(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
    shell=ctypes.WinDLL('shell32');ole=ctypes.WinDLL('ole32')
    shell.SHGetKnownFolderPath.argtypes=[ctypes.POINTER(GUID),wintypes.DWORD,wintypes.HANDLE,ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype=ctypes.c_long
    ole.CoTaskMemFree.argtypes=[ctypes.c_void_p]
    pointer=ctypes.c_void_p()
    code=shell.SHGetKnownFolderPath(ctypes.byref(guid),0,None,ctypes.byref(pointer))
    if code<0:raise CleanupError('local_appdata_unavailable')
    try:return _absolute(ctypes.wstring_at(pointer))
    finally:ole.CoTaskMemFree(pointer)

def _native_delete(entry):
    if os.name!='nt':raise CleanupError('native_delete_requires_windows')
    path=Path(entry['path'])
    with _pin_directories(path.parent):
        native=_native();handle=native.open(path,delete=True);marked=False
        try:
            fresh=native.info(handle,path)
            if _file_reason(path,fresh,entry['scope_id']) or any(fresh[k]!=entry[k] for k in STAMP):
                return {'state':'skipped','reason':'changed_or_unsafe'}
            from engine import no_alternate_streams
            try:no_alternate_streams(path)
            except ValueError:return {'state':'skipped','reason':'alternate_streams'}
            flag=ctypes.c_ubyte(1)
            if not native.k.SetFileInformationByHandle(handle,4,ctypes.byref(flag),ctypes.sizeof(flag)):
                raise ctypes.WinError(ctypes.get_last_error())
            marked=True
        finally:
            closed=native.close(handle)
            if marked and not closed:
                raise CleanupUncertain('delete_handle_close_failed')
        # lexists() hides access errors as False. Only an explicit not-found
        # result proves this name is gone; delete-pending remains uncertain.
        try:
            fresh=_snapshot(path)
        except FileNotFoundError:
            return {'state':'deleted'}
        except (OSError,CleanupError):
            return {'state':'delete_pending','reason':'deletion_requested_not_yet_observable'}
        if (fresh['file_id'],fresh['volume_serial'])!=(entry['file_id'],entry['volume_serial']):
            return {'state':'deleted','reason':'replacement_preserved'}
        return {'state':'delete_pending','reason':'original_still_pending'}

class CacheCleanup:
    def __init__(self,root=None,scopes=None,clock=time.time,deleter=None):
        local=_known_local_appdata() if scopes is None or root is None else None
        self.root=_absolute(root if root is not None else local/'ProstranstvoLocalBeta'/'data'/'cache-cleanup')
        if os.name=='nt' and self.root.drive.casefold()!='c:':
            raise CleanupError('ledger_requires_c_drive')
        if scopes is None:
            scopes=[{'id':'nvidia_dxcache','title':'NVIDIA DirectX shader cache','root':str(local/'NVIDIA'/'DXCache')},
                    {'id':'nvidia_glcache','title':'NVIDIA OpenGL shader cache','root':str(local/'NVIDIA'/'GLCache')},
                    {'id':'windows_d3dscache','title':'Windows Direct3D shader cache','root':str(local/'D3DSCache')}]
        if not isinstance(scopes,list) or not 1<=len(scopes)<=3:
            raise CleanupError('invalid_scope_catalog')
        self.scopes={}
        for scope in scopes:
            if (not isinstance(scope,dict) or set(scope)!={'id','title','root'}
                    or not isinstance(scope['id'],str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',scope['id'])
                    or not isinstance(scope['title'],str) or not 1<=len(scope['title'])<=100):
                raise CleanupError('invalid_scope_catalog')
            path=_absolute(scope['root'])
            if path==Path(path.anchor) or _inside(self.root,path) or _inside(path,self.root):
                raise CleanupError('scope_overlaps_ledger_or_volume_root')
            if scope['id'] in self.scopes:raise CleanupError('duplicate_scope')
            self.scopes[scope['id']]=dict(scope,root=str(path))
        self.clock=clock;self.deleter=_native_delete if deleter is None else deleter

    def scope_catalog(self):
        return [dict(x) for x in self.scopes.values()]

    def _ensure(self):
        # Missing ledger ancestors may be created; existing ancestors are never followed through links.
        for path in (self.root,*self.root.parents):
            if path.exists() or path.is_symlink():
                value=path.lstat()
                if path.is_symlink() or getattr(value,'st_file_attributes',0)&UNSAFE:
                    raise CleanupError('unsafe_ledger')
        self.root.mkdir(parents=True,exist_ok=True)
        with _pin_directories(self.root):pass

    def _path(self,ident,receipt=False):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):
            raise CleanupError('invalid_plan_id')
        return self.root/(ident+('.receipt.json' if receipt else '.plan.json'))

    def _write(self,path,value,new=False):
        raw=(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\n').encode('utf-8')
        if len(raw)>MAX_PLAN_BYTES:raise CleanupError('ledger_too_large')
        with _pin_directories(self.root):
            if path.exists():
                info=path.lstat()
                if path.is_symlink() or info.st_nlink!=1 or not stat.S_ISREG(info.st_mode):
                    raise CleanupError('unsafe_ledger_file')
            if new:
                with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            else:
                temporary=self.root/('.'+uuid.uuid4().hex+'.tmp')
                try:
                    with temporary.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                    os.replace(temporary,path)
                finally:
                    if temporary.exists():temporary.unlink()

    def _read(self,path):
        with _pin_directories(self.root):
            value=path.lstat()
            if path.is_symlink() or value.st_nlink!=1 or not stat.S_ISREG(value.st_mode) or value.st_size>MAX_PLAN_BYTES:
                raise CleanupError('unsafe_ledger_file')
            with path.open('r',encoding='utf-8') as stream:raw=stream.read(MAX_PLAN_BYTES+1)
        if len(raw.encode('utf-8'))>MAX_PLAN_BYTES:raise CleanupError('ledger_too_large')
        return json.loads(raw,object_pairs_hook=_json_object,parse_constant=_invalid_json_constant)

    def prepare(self,scope_ids,min_age_days=7):
        if type(min_age_days) is not int or not 1<=min_age_days<=3650:raise CleanupError('minimum_age_requires_at_least_one_day')
        if (not isinstance(scope_ids,list) or not 1<=len(scope_ids)<=3 or any(not isinstance(x,str) or x not in self.scopes for x in scope_ids)
                or len(set(scope_ids))!=len(scope_ids)):
            raise CleanupError('invalid_scope_ids')
        self._ensure()
        with exclusive_lock(self.root/'cleanup.lock',blocking=False):
            now=_epoch(_utc(self.clock()));cutoff=int((now-min_age_days*86400)*10**9)
            entries=[];skipped=[];errors=[];counts={'skipped':0,'errors':0};total=0;partial=False;visited=0
            def note(kind,path,reason):
                counts[kind]+=1
                target=skipped if kind=='skipped' else errors
                if len(target)<MAX_SAMPLES:target.append({'path':str(path),'reason':reason})
            for scope_id in scope_ids:
                scope=self.scopes[scope_id];root=Path(scope['root']);stack=[(root,0)]
                while stack:
                    directory,depth=stack.pop()
                    if depth>16:note('skipped',directory,'depth_limit');partial=True;continue
                    try:
                        if os.name=='nt' and _native().k.GetDriveTypeW(str(directory.anchor))!=3:
                            raise CleanupError('fixed_local_volume_required')
                        with _pin_directories(directory),os.scandir(directory) as files:
                            for item in files:
                                visited+=1
                                if visited>MAX_FILES:
                                    note('skipped',directory,'inspection_limit');partial=True;stack.clear();break
                                path=Path(item.path)
                                if not _inside(path,root):raise CleanupError('outside_scope')
                                try:
                                    info=_snapshot(path)
                                    if info['attributes']&0x10:
                                        stack.append((path,depth+1));continue
                                    reason=_file_reason(path,info,scope_id)
                                    if reason:note('skipped',path,reason);continue
                                    if info['mtime_ns']>cutoff:note('skipped',path,'too_recent');continue
                                    if len(entries)>=MAX_FILES or total+info['size']>MAX_BYTES:
                                        note('skipped',path,'plan_limit');partial=True;continue
                                    entries.append(dict(scope_id=scope_id,path=str(path),**info));total+=info['size']
                                except (OSError,CleanupError) as error:note('errors',path,type(error).__name__);partial=True
                    except (OSError,CleanupError) as error:note('errors',directory,type(error).__name__);partial=True
            entries.sort(key=lambda e:_key(e['path']))
            plan=dict(version=1,kind='shader_cache_cleanup',plan_id=uuid.uuid4().hex,created_utc=_utc(now),
                      expires_utc=_utc(now+TTL),min_age_days=min_age_days,scopes=[dict(self.scopes[x]) for x in scope_ids],
                      entries=entries,totals={'files':len(entries),'logical_bytes':total},skipped=skipped,errors=errors,
                      skipped_count=counts['skipped'],error_count=counts['errors'],partial=partial)
            plan['plan_hash']=_digest(plan)
            self._write(self._path(plan['plan_id']),plan,new=True)
            return plan

    def _plan(self,ident,check_time=True):
        plan=self._read(self._path(ident))
        if not isinstance(plan,dict) or set(plan)!=PLAN_KEYS:raise CleanupError('invalid_plan_schema')
        hashed=dict(plan);claimed=hashed.pop('plan_hash')
        if (not isinstance(claimed,str) or not re.fullmatch(r'[0-9a-f]{64}',claimed)
                or claimed!=_digest(hashed) or type(plan['version']) is not int or plan['version']!=1
                or plan['kind']!='shader_cache_cleanup' or plan['plan_id']!=ident):
            raise CleanupError('invalid_plan_hash_or_identity')
        created=_epoch(plan['created_utc']);expires=_epoch(plan['expires_utc']);now=_epoch(_utc(self.clock()))
        if not 0<expires-created<=TTL or (check_time and (now<created or now>=expires)):raise CleanupError('plan_expired_or_invalid_clock')
        if type(plan['min_age_days']) is not int or not 1<=plan['min_age_days']<=3650:raise CleanupError('invalid_plan_age')
        if not isinstance(plan['scopes'],list) or any(s!=self.scopes.get(s.get('id')) for s in plan['scopes'] if isinstance(s,dict)) or not all(isinstance(s,dict) for s in plan['scopes']):
            raise CleanupError('scope_catalog_changed')
        scope_ids={s['id'] for s in plan['scopes']}
        if not scope_ids or len(scope_ids)!=len(plan['scopes']):raise CleanupError('invalid_plan_scopes')
        entries=plan['entries'];seen=set();total=0
        if not isinstance(entries,list) or len(entries)>MAX_FILES:raise CleanupError('invalid_plan_entries')
        for entry in entries:
            if not isinstance(entry,dict) or set(entry)!=ENTRY_KEYS or entry['scope_id'] not in scope_ids:
                raise CleanupError('invalid_plan_entry')
            path=_absolute(entry['path']);key=_key(path)
            if key in seen or not _inside(path,self.scopes[entry['scope_id']]['root']) or path==Path(self.scopes[entry['scope_id']]['root']):
                raise CleanupError('entry_outside_scope_or_duplicate')
            if any(type(entry[k]) is not int or entry[k]<0 for k in STAMP if k!='file_id') or not isinstance(entry['file_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',entry['file_id']):
                raise CleanupError('invalid_plan_stamp')
            if _file_reason(path,entry,entry['scope_id']) or entry['mtime_ns']>int((created-plan['min_age_days']*86400)*1e9):
                raise CleanupError('unsafe_plan_entry')
            seen.add(key);total+=entry['size']
        if total>MAX_BYTES or plan['totals']!={'files':len(entries),'logical_bytes':total}:raise CleanupError('invalid_plan_totals')
        if (not isinstance(plan['totals'],dict) or any(type(v) is not int for v in plan['totals'].values())
                or type(plan['partial']) is not bool or any(type(plan[k]) is not int or plan[k]<0 for k in ('skipped_count','error_count'))):
            raise CleanupError('invalid_plan_summary')
        for field in ('skipped','errors'):
            if (not isinstance(plan[field],list) or len(plan[field])>MAX_SAMPLES
                    or len(plan[field])>plan['skipped_count' if field=='skipped' else 'error_count']
                    or any(not isinstance(v,dict) or set(v)!={'path','reason'} or not all(isinstance(x,str) for x in v.values()) for v in plan[field])):
                raise CleanupError('invalid_plan_samples')
        return plan

    def _free(self,plan):
        result={}
        for scope in plan['scopes']:
            anchor=Path(scope['root']).anchor
            if anchor not in result:
                try:result[anchor]=shutil.disk_usage(anchor).free
                except OSError:result[anchor]=None
        return result

    @staticmethod
    def _analysis(plan):
        """Copy the immutable analysis from the validated, approved plan."""
        return {key:deepcopy(plan[key]) for key in ANALYSIS_KEYS}

    @classmethod
    def _checked_analysis(cls,receipt,plan):
        expected=cls._analysis(plan)
        if 'analysis' not in receipt:
            # Old receipts inherit the approved plan's analysis in the returned
            # view only; a completed receipt is never rewritten to migrate it.
            return expected
        saved=receipt['analysis']
        if (not isinstance(saved,dict) or set(saved)!=ANALYSIS_KEYS
                or type(saved['partial']) is not bool
                or any(type(saved[key]) is not int or saved[key]<0 for key in ('skipped_count','error_count'))):
            raise CleanupError('invalid_receipt_analysis')
        for field,count in (('skipped','skipped_count'),('errors','error_count')):
            if (not isinstance(saved[field],list) or len(saved[field])>MAX_SAMPLES
                    or len(saved[field])>saved[count]
                    or any(not isinstance(row,dict) or set(row)!={'path','reason'}
                           or not all(isinstance(value,str) for value in row.values()) for row in saved[field])):
                raise CleanupError('invalid_receipt_analysis')
        if saved!=expected:
            raise CleanupError('receipt_analysis_does_not_match_plan')
        return expected

    @classmethod
    def _result(cls,receipt,path,plan):
        analysis=cls._checked_analysis(receipt,plan)
        deleted=[e for e in receipt['entries'] if e['state']=='deleted']
        skipped=[{'path':e['path'],'reason':e.get('reason','skipped')} for e in receipt['entries'] if e['state']=='skipped']
        errors=[{'path':e['path'],'reason':e.get('reason',e['state'])} for e in receipt['entries'] if e['state'] in ('error','uncertain','delete_pending')]
        status=receipt['status']
        if status=='completed' and (analysis['partial'] or analysis['error_count']):
            status='completed_with_skips'
        return dict(status=status,analysis=analysis,deleted_files=len(deleted),deleted_bytes=sum(e['size'] for e in deleted),
                    skipped=skipped,errors=errors,before_free=receipt['before_free'],after_free=receipt['after_free'],
                    receipt_path=str(path))

    def execute(self,plan_id,approved=False):
        if approved is not True:raise CleanupError('explicit_plan_approval_required')
        self._ensure()
        with exclusive_lock(self.root/'cleanup.lock',blocking=False):
            receipt_path=self._path(plan_id,receipt=True)
            if receipt_path.exists():
                receipt=self._read(receipt_path)
                original=self._plan(plan_id,check_time=False)
                if not isinstance(receipt,dict) or receipt.get('plan_id')!=plan_id or not isinstance(receipt.get('entries'),list):
                    raise CleanupError('invalid_receipt')
                if (receipt.get('plan_hash')!=original['plan_hash'] or len(receipt['entries'])!=len(original['entries'])
                        or receipt.get('status') not in ('running','uncertain','completed','completed_with_skips')):
                    raise CleanupError('invalid_receipt')
                self._checked_analysis(receipt,original)
                for row,entry in zip(receipt['entries'],original['entries']):
                    if (not isinstance(row,dict) or not {'path','size','file_id','volume_serial','state'}<=set(row)<={'path','size','file_id','volume_serial','state','reason'}
                            or any(row[k]!=entry[k] for k in ('path','size','file_id','volume_serial'))
                            or row['state'] not in ('not_started','in_flight','deleted','skipped','uncertain','delete_pending')):
                        raise CleanupError('invalid_receipt_entry')
                if receipt['status']=='running':
                    for entry in receipt['entries']:
                        if entry['state']=='in_flight':entry.update(state='uncertain',reason='previous_execution_interrupted')
                    receipt['status']='uncertain';self._write(receipt_path,receipt)
                return self._result(receipt,receipt_path,original)
            plan=self._plan(plan_id)
            receipt=dict(version=1,plan_id=plan_id,plan_hash=plan['plan_hash'],status='running',created_utc=_utc(self.clock()),
                         entries=[dict(path=e['path'],size=e['size'],file_id=e['file_id'],volume_serial=e['volume_serial'],state='not_started') for e in plan['entries']],
                         before_free=self._free(plan),after_free={},analysis=self._analysis(plan))
            self._checked_analysis(receipt,plan)
            self._write(receipt_path,receipt,new=True)
            for index,entry in enumerate(plan['entries']):
                row=receipt['entries'][index]
                try:
                    fresh=_snapshot(Path(entry['path']))
                    if _file_reason(entry['path'],fresh,entry['scope_id']) or any(fresh[k]!=entry[k] for k in STAMP):
                        row.update(state='skipped',reason='changed_or_unsafe');self._write(receipt_path,receipt);continue
                    row['state']='in_flight';self._write(receipt_path,receipt)
                    try:
                        outcome=self.deleter(dict(entry))
                        if not isinstance(outcome,dict) or outcome.get('state') not in ('deleted','skipped','delete_pending','uncertain'):
                            raise CleanupUncertain('invalid_deleter_result')
                        row.update(outcome)
                    except OSError as error:row.update(state='skipped',reason='busy_or_access_denied' if getattr(error,'winerror',None) in (5,32,33) or isinstance(error,PermissionError) else 'native_error')
                    except Exception as error:row.update(state='uncertain',reason=type(error).__name__)
                    try:self._write(receipt_path,receipt)
                    except Exception:
                        row.update(state='uncertain',reason='receipt_commit_failed_after_action')
                        receipt['status']='uncertain';receipt['after_free']=self._free(plan)
                        return self._result(receipt,receipt_path,plan)
                except FileNotFoundError:
                    row.update(state='skipped',reason='missing');self._write(receipt_path,receipt)
                except CleanupError as error:
                    row.update(state='skipped',reason=str(error));self._write(receipt_path,receipt)
                except OSError:
                    if row['state'] in ('in_flight','deleted','delete_pending'):
                        row.update(state='uncertain',reason='receipt_or_post_action_error')
                        receipt['status']='uncertain'
                        receipt['after_free']=self._free(plan)
                        return self._result(receipt,receipt_path,plan)
                    row.update(state='skipped',reason='busy_or_access_denied');self._write(receipt_path,receipt)
            states={e['state'] for e in receipt['entries']}
            receipt['status']='uncertain' if states&{'uncertain','in_flight','delete_pending'} else 'completed_with_skips' if ('skipped' in states or plan['partial'] or plan['error_count']) else 'completed'
            receipt['after_free']=self._free(plan);receipt['finished_utc']=_utc(self.clock())
            self._write(receipt_path,receipt)
            return self._result(receipt,receipt_path,plan)

