"""Local file executor. stdlib only; an optional local catalog stays authoritative."""
import concurrent.futures, contextlib, ctypes, datetime, hashlib, json, os, shutil, sqlite3, stat, threading, uuid
from pathlib import Path
from paths import APP_ROOT,CATALOG_PATH,DEFAULT_ALLOWED_ROOTS,DEFAULT_BLOCKED_ROOTS,EXECUTOR_DATA,MAINTENANCE_ROOT
from folder_policy import FolderPolicy,PROTECTED_PARTS,no_links
BASE=APP_ROOT
INDEX=CATALOG_PATH
ROOTS=DEFAULT_ALLOWED_ROOTS
BLOCKED=DEFAULT_BLOCKED_ROOTS
DENY_PARTS={'.git','.codex','.aws','.ssh','node_modules','appdata','onedrive','icloudphotos','iclouddrive','.minecraft','saves','backups','windows','program files','program files (x86)'}
DENY_PARTS|=PROTECTED_PARTS
DENY_EXT={'.exe','.dll','.msi','.sys','.lnk','.url','.key','.pem','.pfx','.kdbx','.sqlite','.db','.dat','.torrent','.crdownload'}
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def drive_usage():
 """Report mounted logical drives; inaccessible/removable volumes are optional."""
 if hasattr(os,'listdrives'):
  try:drives=os.listdrives()
  except OSError:drives=[]
 elif os.name=='nt':
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.GetLogicalDrives.restype=ctypes.c_ulong
  mask=kernel.GetLogicalDrives()
  drives=[chr(65+i)+':\\' for i in range(26) if mask&(1<<i)]
 else:drives=[os.path.abspath(os.sep)]
 result=[]
 for drive in drives:
  try:usage=shutil.disk_usage(drive)
  except OSError:continue
  result.append(dict(drive=str(drive),free=usage.free,total=usage.total))
 return result
def inside(p,r):
 try:p.relative_to(r);return True
 except ValueError:return False
def digest(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def fingerprint(p):
 s=p.stat();return dict(size=s.st_size,mtime_ns=s.st_mtime_ns,ino=s.st_ino)
def native_kernel():
 from ctypes import wintypes
 k=ctypes.WinDLL('kernel32',use_last_error=True)
 k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];k.CreateFileW.restype=wintypes.HANDLE
 k.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];k.SetFileInformationByHandle.restype=wintypes.BOOL
 k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
 return k
def locked_file(p,delete=False):
 if os.name!='nt':return open(p,'rb')
 import msvcrt
 k=native_kernel();h=k.CreateFileW(str(p),0x80000000|(0x10000 if delete else 0),1,None,3,0x08000000,None)
 if h==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
 try:fd=msvcrt.open_osfhandle(h,os.O_RDONLY|os.O_BINARY)
 except Exception:k.CloseHandle(h);raise
 return os.fdopen(fd,'rb')
def mark_delete(f,value):
 import msvcrt
 flag=ctypes.c_ubyte(bool(value));k=native_kernel()
 if not k.SetFileInformationByHandle(msvcrt.get_osfhandle(f.fileno()),4,ctypes.byref(flag),ctypes.sizeof(flag)):raise ctypes.WinError(ctypes.get_last_error())
def no_alternate_streams(p):
 if os.name!='nt':return
 from ctypes import wintypes
 class Stream(ctypes.Structure):_fields_=[('size',ctypes.c_longlong),('name',wintypes.WCHAR*296)]
 k=native_kernel();k.FindFirstStreamW.argtypes=[wintypes.LPCWSTR,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];k.FindFirstStreamW.restype=wintypes.HANDLE
 k.FindNextStreamW.argtypes=[wintypes.HANDLE,ctypes.c_void_p];k.FindNextStreamW.restype=wintypes.BOOL;k.FindClose.argtypes=[wintypes.HANDLE]
 data=Stream();h=k.FindFirstStreamW(str(p),0,ctypes.byref(data),0)
 if h==ctypes.c_void_p(-1).value:
  error=ctypes.get_last_error()
  if error==38:return
  raise ctypes.WinError(error)
 try:
  while True:
   if data.name!='::$DATA':raise ValueError('У файла есть дополнительные потоки NTFS; нужен отдельный план сохранения: '+str(p))
   if not k.FindNextStreamW(h,ctypes.byref(data)):
    if ctypes.get_last_error()!=38:raise ctypes.WinError(ctypes.get_last_error())
    break
 finally:k.FindClose(h)
class ClosingConnection(sqlite3.Connection):
 def __exit__(self,*args):
  try:return super().__exit__(*args)
  finally:self.close()
class Busy(RuntimeError):pass
class ScanLimitExceeded(ValueError):pass
class Executor:
 def __init__(self,data=None,roots=None,index=None,blocked=None):
  self.data=Path(data or EXECUTOR_DATA);no_links(self.data);self.data.mkdir(parents=True,exist_ok=True)
  self.roots=[Path(x).absolute() for x in (ROOTS if roots is None else roots)]
  self.blocked=[Path(x).absolute() for x in (BLOCKED if blocked is None else blocked)]
  self.index=Path(INDEX if index is None else index);self.db=self.data/'queue.sqlite'
  self._folder_policy=None if roots is not None else FolderPolicy(self.data/'allowed-folders.json',self.roots,[*self.blocked,BASE,self.data,self.index],DENY_PARTS)
  self._reload_roots()
  with self.connect() as c:
   c.executescript('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,created TEXT,updated TEXT,payload TEXT,result TEXT,error TEXT,cancel INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS receipts(job TEXT,n INTEGER,src TEXT,dst TEXT,sha TEXT,bytes INTEGER,index_id INTEGER,state TEXT,PRIMARY KEY(job,n));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT,job TEXT,message TEXT);''')
 def connect(self):
  c=sqlite3.connect(self.db,timeout=10,factory=ClosingConnection);c.row_factory=sqlite3.Row;return c
 @contextlib.contextmanager
 def lock(self):
  no_links(self.data/'executor.lock',(self.data/'executor.lock').exists())
  f=open(self.data/'executor.lock','a+b');f.seek(0);f.write(b'0');f.flush();f.seek(0)
  try:
   if os.name=='nt':
    import msvcrt
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise Busy('Другой исполнитель уже работает')
   else:
    import fcntl
    try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:raise Busy('Executor busy')
   yield
  finally:f.close()
 def _reload_roots(self):
  if self._folder_policy is not None:self.roots=self._folder_policy.load()
 def allowed_roots(self):
  self._reload_roots();return [str(p) for p in self.roots]
 def set_allowed_roots(self,roots):
  if self._folder_policy is None:raise ValueError('Явные папки этого исполнителя задаются при его создании')
  with self.lock():self.roots=self._folder_policy.save(roots)
  return [str(p) for p in self.roots]
 def checked(self,value,folder=False):
  self._reload_roots()
  p=Path(value).absolute()
  if not any(inside(p,r) for r in self.roots):raise ValueError('Путь вне разрешённых папок')
  if any(inside(p,r) for r in self.blocked):raise ValueError('Рабочая/служебная папка защищена')
  if any(s.lower() in DENY_PARTS or s.lower().startswith(('onedrive - ','onedrive – ')) for s in p.parts):raise ValueError('Папка приложения, облака или сохранений защищена')
  if not folder and (p.suffix.lower() in DENY_EXT or p.name.lower().startswith(('.env','credentials','token','secret'))):raise ValueError('Служебный или исполняемый файл исключён')
  for part in [p,*p.parents]:
   if os.path.lexists(part):
    s=part.lstat()
    if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:raise ValueError('Ссылки, junction и облачные placeholders исключены')
  resolved=p.resolve()
  if resolved!=p:raise ValueError('Неоднозначный путь')
  return p
 def event(self,j,message):
  with self.connect() as c:c.execute('INSERT INTO events(at,job,message) VALUES(?,?,?)',(now(),j,message))
 def create(self,kind,payload,approval=False):
  ident=uuid.uuid4().hex
  with self.connect() as c:c.execute('INSERT INTO jobs(id,kind,status,created,updated,payload) VALUES(?,?,?,?,?,?)',(ident,kind,'needs_approval' if approval else 'queued',now(),now(),json.dumps(payload,ensure_ascii=False)))
  self.event(ident,'Задание создано');return ident
 def jobs(self):
  with self.connect() as c:
   rows=c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 200').fetchall()
  return [dict(r, payload=json.loads(r['payload']),result=json.loads(r['result']) if r['result'] else None) for r in rows]
 def get(self,j):
  with self.connect() as c:r=c.execute('SELECT * FROM jobs WHERE id=?',(j,)).fetchone()
  if not r:raise ValueError('Задание не найдено')
  return dict(r,payload=json.loads(r['payload']),result=json.loads(r['result']) if r['result'] else None)
 def approve(self,j):
  with self.connect() as c:
   cur=c.execute("UPDATE jobs SET status='queued',updated=? WHERE id=? AND status='needs_approval'",(now(),j))
   if cur.rowcount!=1:raise ValueError('Задание не ожидает подтверждения')
  self.event(j,'Конкретный план подтверждён пользователем')
 def cancel(self,j):
  with self.connect() as c:c.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status IN ('queued','needs_approval') THEN 'cancelled' ELSE status END,updated=? WHERE id=?",(now(),j))
 def cancelled(self,j):
  with self.connect() as c:return bool(c.execute('SELECT cancel FROM jobs WHERE id=?',(j,)).fetchone()[0])
 def index_connection(self,write=False):
  if not self.index.exists():return None
  if write:return sqlite3.connect(self.index,timeout=10)
  return sqlite3.connect(self.index.as_uri()+'?mode=ro',uri=True,timeout=10)
 def catalog(self,q=''):
  c=self.index_connection()
  if c is None:return []
  try:
   c.row_factory=sqlite3.Row
   return [dict(r) for r in c.execute('SELECT id,path,name,size,category FROM files WHERE name LIKE ? OR path LIKE ? LIMIT 100',('%'+q+'%','%'+q+'%'))]
  finally:c.close()
 def scan(self,root,j=None,max_files=None):
  if max_files is not None and (type(max_files) is not int or not 1<=max_files<=100000):raise ValueError('Лимит сканирования: 1–100000 файлов')
  root=self.checked(root,True);stack=[root];rows=[];skipped=0
  if not root.is_dir():raise ValueError('Нужна существующая папка')
  while stack:
   if j is not None and self.cancelled(j):raise InterruptedError('Отменено пользователем')
   try:
    with os.scandir(stack.pop()) as it:
     for e in it:
      try:
       p=self.checked(e.path,e.is_dir(follow_symlinks=False))
       if e.is_dir(follow_symlinks=False):stack.append(p)
       elif e.is_file(follow_symlinks=False):rows.append(dict(path=str(p),**fingerprint(p)))
      except (OSError,ValueError):skipped+=1
      if max_files is not None and len(rows)>max_files:raise ScanLimitExceeded('В папке больше '+str(max_files)+' файлов; выберите меньшую папку')
   except OSError:skipped+=1
  return rows,skipped
 def inventory_records(self,root,records,j=None):
  root=self.checked(root,True)
  if not root.is_dir():raise ValueError('Нужна существующая папка')
  if not isinstance(records,list) or not 1<=len(records)<=100:raise ValueError('Нужны 1–100 конкретных файлов инвентаризации')
  rows=[];seen=set()
  for record in records:
   if j is not None and self.cancelled(j):raise InterruptedError('Отменено пользователем')
   if not isinstance(record,dict) or set(record)!={'path','size','mtime_ns','ino'}:raise ValueError('Неверная запись инвентаризации')
   if not isinstance(record['path'],str) or any(type(record[k]) is not int or record[k]<0 for k in ('size','mtime_ns','ino')):raise ValueError('Неверный отпечаток файла')
   p=self.checked(record['path']);key=os.path.normcase(str(p))
   if not inside(p,root) or key in seen or not p.is_file():raise ValueError('Файл инвентаризации вне папки или повторён')
   stamp=fingerprint(p)
   if stamp!={k:record[k] for k in ('size','mtime_ns','ino')}:raise ValueError('Файл изменён после просмотра правила')
   seen.add(key);rows.append(dict(path=str(p),**stamp))
  return rows,0
 def plan_transfer(self,sources,destination,mode='move'):
  if mode not in ('move','copy'):raise ValueError('Режим: move или copy')
  if not isinstance(sources,list) or not 1<=len(sources)<=100:raise ValueError('Нужны 1–100 явных файлов')
  dest=self.checked(destination,True)
  if not dest.is_dir():raise ValueError('Целевая папка должна существовать')
  records=[];seen=set();bytes_=0
  for s in sources:
   p=self.checked(s)
   if not p.is_file():raise ValueError('Выберите отдельные файлы')
   no_alternate_streams(p)
   target=self.checked(dest/p.name)
   if os.path.lexists(target) or str(target).casefold() in seen:raise ValueError('Коллизия имени: '+str(target))
   seen.add(str(target).casefold());before=fingerprint(p);sha=digest(p)
   if fingerprint(p)!=before:raise ValueError('Источник меняется')
   idx=None;c=self.index_connection()
   if c:
    try:
     r=c.execute('SELECT id FROM files WHERE path=?',(str(p),)).fetchone();idx=r[0] if r else None
    finally:c.close()
   records.append(dict(src=str(p),dst=str(target),sha=sha,stamp=before,index_id=idx));bytes_+=before['size']
  if shutil.disk_usage(dest).free<bytes_+10*1024**2:raise ValueError('Недостаточно свободного места')
  return self.create('transfer',dict(mode=mode,records=records,total_bytes=bytes_),True)
 def update_index(self,c,record):
  if c is None or record['index_id'] is None:return
  ident=record['index_id'];src=record['src'];dst=record['dst']
  row=c.execute('SELECT id,name,path,extension,category,source_root FROM files WHERE id=? AND path=?',(ident,src)).fetchone()
  if row is None:raise ValueError('Каталог изменён другим агентом: перенос остановлен')
  old=list(row[1:]);new=list(old);new[1]=dst;new[0]=Path(dst).name;new[2]=Path(dst).suffix.lower()
  c.execute("INSERT INTO files_fts(files_fts,rowid,name,path,extension,category,source_root) VALUES('delete',?,?,?,?,?,?)",(ident,*old))
  c.execute('UPDATE files SET name=?,path=?,extension=? WHERE id=?',(new[0],dst,new[2],ident))
  c.execute('INSERT INTO files_fts(rowid,name,path,extension,category,source_root) VALUES(?,?,?,?,?,?)',(ident,*new))
 def transfer_one(self,j,n,r,mode):
  src=self.checked(r['src']);dst=self.checked(r['dst']);self.checked(dst.parent,True)
  no_alternate_streams(src)
  if os.path.lexists(dst):raise ValueError('Цель уже существует: '+str(dst))
  tmp=dst.parent/('.companion-'+j+'-'+str(n)+'.partial');created=False;c=None;source=None;marked=False;removed=False;committed=False;receipt=False
  try:
   source=locked_file(src,mode=='move')
   st=os.fstat(source.fileno())
   if dict(size=st.st_size,mtime_ns=st.st_mtime_ns,ino=st.st_ino)!=r['stamp']:raise ValueError('Источник изменён после планирования')
   with open(tmp,'xb') as target:
    h=hashlib.sha256()
    for block in iter(lambda:source.read(1024*1024),b''):
     if self.cancelled(j):raise InterruptedError('Отменено')
     target.write(block);h.update(block)
    target.flush();os.fsync(target.fileno())
   if h.hexdigest()!=r['sha']:raise ValueError('Источник изменился после планирования')
   shutil.copystat(src,tmp)
   if os.name=='nt':os.rename(tmp,dst)
   else:os.link(tmp,dst);tmp.unlink()
   created=True
   if digest(dst)!=r['sha']:raise ValueError('Проверка копии не пройдена')
   with self.connect() as own:own.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',(j,n,str(src),str(dst),r['sha'],r['stamp']['size'],r['index_id'],'copied'))
   receipt=True
   if mode=='move':
    c=self.index_connection(True)
    if c:c.execute('BEGIN IMMEDIATE')
    self.update_index(c,r)
    if os.name=='nt':mark_delete(source,True);marked=True
    else:src.unlink();removed=True
    if c:c.commit()
    committed=True
    source.close();source=None;removed=True
   with self.connect() as own:own.execute("UPDATE receipts SET state='complete' WHERE job=? AND n=?",(j,n))
   self.event(j,'Проверено: '+str(src)+' → '+str(dst))
  except Exception:
   if marked and source is not None and not committed:
    mark_delete(source,False);marked=False
   if c and not committed:c.rollback()
   if source is not None:source.close();source=None
   if removed and not committed and not src.exists() and dst.exists() and digest(dst)==r['sha']:
    with open(dst,'rb') as inp,open(src,'xb') as out:shutil.copyfileobj(inp,out)
    shutil.copystat(dst,src)
    if digest(src)==r['sha']:removed=False
   if created and not removed and not committed and dst.exists() and digest(dst)==r['sha']:dst.unlink()
   if receipt and not committed:
    # Record only a proven rollback; unknown outcomes still require recovery review.
    try:
     if src.is_file() and not os.path.lexists(dst) and digest(src)==r['sha']:
      with self.connect() as own:own.execute("UPDATE receipts SET state='rolled_back' WHERE job=? AND n=? AND state='copied'",(j,n))
    except (OSError,sqlite3.Error):pass
   self.event(j,'Операция прервана; квитанция сохранена')
   raise
  finally:
   if source is not None:source.close()
   if c:c.close()
   if tmp.exists():tmp.unlink()
 def undo(self,j):
  original=self.get(j)
  if original['kind']!='transfer' or original['payload']['mode']!='move':raise ValueError('Обратный перенос доступен для перемещений')
  with self.connect() as c:rows=c.execute("SELECT * FROM receipts WHERE job=? AND state='complete' ORDER BY n DESC",(j,)).fetchall()
  if not rows:raise ValueError('Нет завершённых перемещений')
  records=[]
  for r in rows:
   src=self.checked(r['dst']);dst=self.checked(r['src'])
   if dst.exists() or digest(src)!=r['sha']:raise ValueError('Файлы изменены; автоматический возврат остановлен')
   records.append(dict(src=str(src),dst=str(dst),sha=r['sha'],stamp=fingerprint(src),index_id=r['index_id']))
  return self.create('transfer',dict(mode='move',records=records,undo_of=j,total_bytes=sum(r['stamp']['size'] for r in records)),True)
 def execute(self,j):
  with self.lock():
   with self.connect() as c:
    c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM jobs WHERE id=?',(j,)).fetchone()
    if not r or r['status']!='queued':raise ValueError('Задание не готово или уже выполняется')
    c.execute("UPDATE jobs SET status='running',updated=? WHERE id=?",(now(),j))
   try:
    payload=json.loads(r['payload'])
    if r['kind'] in ('inventory','duplicates'):
     if r['kind']=='inventory' and 'rule_records' in payload:rows,skipped=self.inventory_records(payload['root'],payload['rule_records'],j)
     else:rows,skipped=self.scan(payload['root'],j,max_files=payload.get('max_files'))
     result=dict(files=len(rows),bytes=sum(x['size'] for x in rows),skipped=skipped)
     if r['kind']=='inventory':result['largest']=sorted(rows,key=lambda x:x['size'],reverse=True)[:100]
     else:
      groups={}
      for x in rows:groups.setdefault(x['size'],[]).append(x)
      candidates=[x for xs in groups.values() if len(xs)>1 and xs[0]['size']>0 for x in xs]
      def hashed(x):
       if self.cancelled(j):raise InterruptedError('Отменено')
       p=Path(x['path']);h=digest(p)
       if fingerprint(p)!={k:x[k] for k in ('size','mtime_ns','ino')}:raise ValueError('Файл меняется')
       return h,x
      matches={}
      with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
       for h,x in pool.map(hashed,candidates):matches.setdefault(h,[]).append(x)
      result['groups']=[dict(sha256=h,files=xs) for h,xs in matches.items() if len(xs)>1]
     report=self.data/(j+'.json');report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
     result['report']=str(report)
    elif r['kind']=='transfer':
     for n,record in enumerate(payload['records']):self.transfer_one(j,n,record,payload['mode'])
     result=dict(verified_files=len(payload['records']),mode=payload['mode'])
    else:raise ValueError('Неизвестный вид задания')
    with self.connect() as c:c.execute("UPDATE jobs SET status='completed',result=?,updated=? WHERE id=?",(json.dumps(result,ensure_ascii=False),now(),j))
   except Exception as e:
    with self.connect() as c:c.execute('UPDATE jobs SET status=?,error=?,updated=? WHERE id=?',('cancelled' if isinstance(e,InterruptedError) else 'failed',str(e),now(),j))
    self.event(j,str(e));raise
  return self.get(j)
 def _recover_move_receipt(self,job,payload,receipt):
  """Confirm a finished move from evidence; never change user files or the index."""
  n=receipt['n'];records=payload['records']
  if type(n) is not int or not 0<=n<len(records):raise ValueError('Квитанция не соответствует плану')
  record=records[n]
  if any(receipt[key]!=record[key] for key in ('src','dst','sha','index_id')) or receipt['bytes']!=record['stamp']['size']:
   raise ValueError('Квитанция не соответствует плану')
  src=self.checked(record['src']);dst=self.checked(record['dst']);self.checked(dst.parent,True)
  if os.path.lexists(src):raise ValueError('Исходный путь существует; нужен ручной разбор')
  if not dst.is_file():raise ValueError('Проверенная копия не найдена')
  no_alternate_streams(dst)
  with locked_file(dst) as target:
   before=os.fstat(target.fileno())
   if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size!=receipt['bytes']:
    raise ValueError('Копия не соответствует ожидаемому обычному файлу')
   h=hashlib.sha256();count=0
   for block in iter(lambda:target.read(1024*1024),b''):h.update(block);count+=len(block)
   after=os.fstat(target.fileno())
   stamp=lambda s:dict(size=s.st_size,mtime_ns=s.st_mtime_ns,ino=s.st_ino)
   if count!=receipt['bytes'] or h.hexdigest()!=receipt['sha'] or stamp(before)!=stamp(after) or fingerprint(dst)!=stamp(after):
    raise ValueError('Копия изменена; автоматический возврат запрещён')
   if record['index_id'] is not None:
    c=self.index_connection()
    if c is None:raise ValueError('Каталог недоступен; согласованность переноса не подтверждена')
    try:row=c.execute('SELECT id,path FROM files WHERE id=?',(record['index_id'],)).fetchone()
    finally:c.close()
    if row is None or self.checked(row[1])!=dst:raise ValueError('Каталог не подтверждает завершённый перенос')
   if os.path.lexists(src):raise ValueError('Исходный путь появился; нужен ручной разбор')
   with self.connect() as own:
    updated=own.execute("UPDATE receipts SET state='complete' WHERE job=? AND n=? AND state='copied'",(job,n)).rowcount
  return updated==1
 def recover(self):
  summary={'verified_moves':0,'blocked':[]}
  with self.lock():
   with self.connect() as c:
    c.execute("UPDATE jobs SET status='interrupted',error='Исполнитель был прерван; квитанции сохранены, автоматический повтор запрещён' WHERE status='running'")
    jobs=c.execute("SELECT id,status,error,payload FROM jobs WHERE status IN ('interrupted','failed') AND kind='transfer'").fetchall()
   for job in jobs:
    try:payload=json.loads(job['payload'])
    except (TypeError,ValueError):
     summary['blocked'].append(dict(job_id=job['id'],receipt=None,reason='План не читается'));continue
    if not isinstance(payload,dict):
     summary['blocked'].append(dict(job_id=job['id'],receipt=None,reason='План не читается'));continue
    if payload.get('mode')!='move':continue
    with self.connect() as c:receipts=c.execute("SELECT * FROM receipts WHERE job=? AND state='copied' ORDER BY n",(job['id'],)).fetchall()
    for receipt in receipts:
     try:
      if self._recover_move_receipt(job['id'],payload,receipt):
       summary['verified_moves']+=1;self.event(job['id'],'После прерывания подтверждено завершённое перемещение; доступен проверяемый обратный план')
     except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as error:
      summary['blocked'].append(dict(job_id=job['id'],receipt=receipt['n'],reason=str(error)))
    if receipts:
     blocked=[x for x in summary['blocked'] if x['job_id']==job['id']]
     if job['status']=='failed':
      note=('Восстановление: некоторые квитанции требуют ручной проверки; автоматический повтор запрещён.' if blocked else 'Восстановление: завершённые перемещения проверены; можно подготовить обратный план.')
      original=job['error'] or ''
      message=original if note in original else original+('\n' if original else '')+note
     else:
      message='Исполнитель был прерван; автоматический повтор запрещён. '
      message+=('Некоторые квитанции требуют ручной проверки: '+'; '.join(x['reason'] for x in blocked)) if blocked else 'Завершённые перемещения проверены; можно подготовить обратный план.'
     with self.connect() as c:c.execute("UPDATE jobs SET error=? WHERE id=? AND status=?",(message,job['id'],job['status']))
  return summary
 def status(self):
  self._reload_roots()
  maintenance=MAINTENANCE_ROOT;admin=maintenance/'native-admin-status.json'
  try:a=json.loads(admin.read_text(encoding='utf-8-sig'))
  except (OSError,ValueError):a={'phase':'Ещё не запущено'}
  from system_broker import status as broker_status
  return dict(system_service=broker_status(),roots=[str(x) for x in self.roots],catalog=str(self.index),catalog_exists=self.index.exists(),admin=a,drives=drive_usage(),google_drive='Локальная beta: облачные подключения не настроены')


