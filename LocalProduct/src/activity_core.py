"""Local explicit Pasha-mode recording. No network, model, keyboard or audio calls."""
from __future__ import annotations
import contextlib,ctypes,datetime,json,os,re,shutil,sqlite3,struct,tempfile,time,uuid,zlib
from pathlib import Path
from paths import DATA_ROOT as PRODUCT_DATA_ROOT
DATA_ROOT=PRODUCT_DATA_ROOT/'activity'
DEFAULTS={"enabled":False,"interval_seconds":600,"retention_days":14,"max_storage_bytes":5*1024**3,"record_titles":False,"skip_idle":False,"capture_request":""}
IMAGE_NAME=re.compile(r"^screen-\d{8}T\d{12}Z-[0-9a-f]{8}\.png$")
DATE_DIR=re.compile(r"^\d{4}-\d{2}-\d{2}$")
def utc_now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def unix(value):return datetime.datetime.fromisoformat(value).timestamp()
def no_links(path,regular=False):
 path=Path(path)
 for entry in (path,*path.parents):
  try:st=entry.lstat()
  except FileNotFoundError:continue
  if entry.is_symlink() or getattr(st,"st_file_attributes",0)&0x400:raise ValueError("Ссылки и junction недопустимы: "+str(entry))
  if entry==path and regular and (not entry.is_file() or st.st_nlink!=1):raise ValueError("Требуется обычный файл без hardlink: "+str(path))
 return path
class Connection(sqlite3.Connection):
 def __exit__(self,*args):
  try:return super().__exit__(*args)
  finally:self.close()
@contextlib.contextmanager
def exclusive_lock(path,blocking=True):
 no_links(path);f=open(path,"a+b")
 try:
  no_links(path,True);f.seek(0)
  if os.name=="nt":
   import msvcrt
   end=time.monotonic()+3
   while True:
    try:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);break
    except OSError:
     if not blocking or time.monotonic()>end:raise RuntimeError("Модуль уже запущен или занят")
     time.sleep(.05)
   try:yield
   finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
  else:
   import fcntl
   fcntl.flock(f,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
   try:yield
   finally:fcntl.flock(f,fcntl.LOCK_UN)
 finally:f.close()
class ActivityStore:
 def __init__(self,base=DATA_ROOT):
  self.base=Path(base).absolute();no_links(self.base);self.base.mkdir(parents=True,exist_ok=True);no_links(self.base)
  self.screenshots=self.base/"screenshots";self.screenshots.mkdir(exist_ok=True);no_links(self.screenshots)
  self.db=self.base/"activity.sqlite";self.config_file=self.base/"config.json";self.state_file=self.base/"state.json"
  with self.connect() as c:c.executescript("""
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS captures(path TEXT PRIMARY KEY,time TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,ino TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL,app TEXT NOT NULL,title TEXT NOT NULL,seconds REAL NOT NULL);
CREATE INDEX IF NOT EXISTS segments_start ON segments(start);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
""")
  if not self.config_file.exists():
   with exclusive_lock(self.base/"config.lock"):
    if not self.config_file.exists():self._atomic(self.config_file,DEFAULTS)
 def connect(self):
  for p in (self.db,Path(str(self.db)+"-journal"),Path(str(self.db)+"-wal"),Path(str(self.db)+"-shm")):no_links(p,p.exists())
  return sqlite3.connect(self.db,timeout=5,factory=Connection)
 def _atomic(self,path,value):
  no_links(path,path.exists());fd,name=tempfile.mkstemp(prefix=".activity-",suffix=".tmp",dir=self.base)
  try:
   with os.fdopen(fd,"w",encoding="utf-8") as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
   for attempt in range(8):
    no_links(path,path.exists())
    try:os.replace(name,path);break
    except PermissionError:
     if attempt==7:raise
     time.sleep(.025)
  finally:
   if os.path.exists(name):os.unlink(name)
 def _read(self,path,default):
  try:no_links(path,True);return json.loads(path.read_text(encoding="utf-8-sig"))
  except FileNotFoundError:return dict(default)
 def config(self):
  value=self._read(self.config_file,DEFAULTS)
  if not isinstance(value,dict):raise ValueError("Неверный файл настроек")
  return self._validate({**DEFAULTS,**value})
 @staticmethod
 def _validate(value):
  if set(value)!=set(DEFAULTS):raise ValueError("Неизвестная настройка")
  for key in ("enabled","record_titles","skip_idle"):
   if type(value[key]) is not bool:raise ValueError("Неверный флаг "+key)
  for key,lo,hi in (("interval_seconds",60,86400),("retention_days",1,365),("max_storage_bytes",50*1024**2,100*1024**3)):
   if type(value[key]) is not int or not lo<=value[key]<=hi:raise ValueError("Значение вне диапазона: "+key)
  request=value["capture_request"]
  if not isinstance(request,str) or (request and not re.fullmatch("[0-9a-f]{32}",request)):raise ValueError("Неверный запрос снимка")
  return value
 def set_config(self,changes=None,**kwargs):
  changes={**(changes or {}),**kwargs}
  if set(changes)-set(DEFAULTS):raise ValueError("Неизвестная настройка")
  with exclusive_lock(self.base/"config.lock"):
   value=self.config();value.update(changes);self._validate(value)
   if not value["enabled"]:value["capture_request"]=""
   self._atomic(self.config_file,value)
  return value
 def request_capture(self):
  with exclusive_lock(self.base/"config.lock"):
   value=self.config()
   if not value["enabled"]:raise ValueError("Сначала включите «Режим Паши»")
   value["capture_request"]=uuid.uuid4().hex;self._atomic(self.config_file,value);return value["capture_request"]
 def status(self):
  value=self._read(self.state_file,{});fresh=False
  try:fresh=not value.get("stopped",False) and 0<=time.time()-unix(value.get("heartbeat",""))<20
  except (ValueError,TypeError):pass
  return {**value,"running":fresh,"recording":fresh and bool(value.get("enabled")),"enabled":self.config()["enabled"],"data_root":str(self.base)}
 def write_status(self,**values):self._atomic(self.state_file,{**values,"heartbeat":utc_now()})
 def event(self,kind,detail=""):
  with self.connect() as c:c.execute("INSERT INTO events(time,kind,detail) VALUES(?,?,?)",(utc_now(),str(kind)[:60],str(detail)[:500]))
 def add_usage(self,app,title,seconds,start,end):
  if seconds<=0:return
  with self.connect() as c:
   last=c.execute("SELECT id,app,title,end FROM segments ORDER BY id DESC LIMIT 1").fetchone()
   if last and last[1]==app and last[2]==title and abs(unix(start)-unix(last[3]))<20:c.execute("UPDATE segments SET end=?,seconds=seconds+? WHERE id=?",(end,seconds,last[0]))
   else:c.execute("INSERT INTO segments(start,end,app,title,seconds) VALUES(?,?,?,?,?)",(start,end,app,title,seconds))
 def capture_path(self,path):
  path=Path(path)
  if not path.is_absolute():path=self.screenshots/path
  path=path.absolute()
  try:relative=path.relative_to(self.screenshots)
  except ValueError:raise ValueError("Снимок вне папки записи")
  if len(relative.parts)!=2 or not DATE_DIR.fullmatch(relative.parts[0]) or not IMAGE_NAME.fullmatch(relative.parts[1]):raise ValueError("Неверное имя снимка")
  no_links(path,path.exists())
  if not path.resolve().is_relative_to(self.screenshots.resolve()):raise ValueError("Снимок вне папки записи")
  return path
 def save_capture(self,png,at=None):
  if not png.startswith(b"\x89PNG\r\n\x1a\n"):raise ValueError("Ожидался PNG")
  value=self.config()
  if not value["enabled"]:raise ValueError("Режим записи выключен")
  self.prune()
  if shutil.disk_usage(self.base).free<len(png)+64*1024**2:raise OSError("Недостаточно свободного места для записи")
  if len(png)>value["max_storage_bytes"]:raise OSError("Один снимок превышает установленный лимит хранения")
  at=at or datetime.datetime.now(datetime.timezone.utc);folder=self.screenshots/at.strftime("%Y-%m-%d")
  no_links(folder);folder.mkdir(exist_ok=True);no_links(folder)
  path=self.capture_path(folder/("screen-"+at.strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8]+".png"))
  with open(path,"xb") as f:f.write(png);f.flush();os.fsync(f.fileno())
  st=path.stat()
  try:
   with self.connect() as db:db.execute("INSERT INTO captures(path,time,size,mtime_ns,ino) VALUES(?,?,?,?,?)",(str(path),at.isoformat(),st.st_size,st.st_mtime_ns,str(st.st_ino)))
  except Exception:
   if path.exists() and path.stat().st_ino==st.st_ino:path.unlink()
   raise
  self.prune();return path
 def recent_captures(self,limit=30):
  with self.connect() as c:rows=c.execute("SELECT path,time,size FROM captures WHERE deleted=0 ORDER BY time DESC LIMIT ?",(max(1,min(int(limit),500)),)).fetchall()
  result=[]
  for path,at,size in rows:
   try:
    safe=self.capture_path(path)
    if safe.exists():result.append({"path":str(safe),"time":at,"size":size})
   except (OSError,ValueError):continue
  return result
 def summary(self,days=1):
  cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=max(1,min(int(days),365)))).isoformat()
  with self.connect() as c:
   totals={}
   for app,seconds,start,end in c.execute("SELECT app,seconds,start,end FROM segments WHERE end>=?",(cutoff,)):
    span=unix(end)-unix(start)
    fraction=max(0.,min(1.,(unix(end)-max(unix(start),unix(cutoff)))/span)) if span>0 else 0.
    totals[app]=totals.get(app,0.)+seconds*fraction
   apps=[{"app":app,"seconds":round(seconds,1)} for app,seconds in sorted(totals.items(),key=lambda item:item[1],reverse=True)]
   events=[{"time":at,"kind":kind,"detail":detail} for at,kind,detail in c.execute("SELECT time,kind,detail FROM events WHERE time>=? ORDER BY id DESC LIMIT 200",(cutoff,))]
   count=c.execute("SELECT COUNT(*) FROM captures WHERE time>=? AND deleted=0",(cutoff,)).fetchone()[0]
   titles=[{"time":at,"app":app,"title":title,"seconds":round(seconds,1)} for at,app,title,seconds in c.execute("SELECT start,app,title,seconds FROM segments WHERE end>=? AND title<>? ORDER BY id DESC LIMIT 100",(cutoff,""))]
  return {"apps":apps,"events":events,"captures":count,"titles":titles,"identity_note":"Общая учётная запись: человек определяется только ручным режимом."}
 def prune(self,now=None):
  value=self.config();current=time.time() if now is None else now;cutoff=current-value["retention_days"]*86400
  with self.connect() as db:rows=db.execute("SELECT path,time,size,mtime_ns,ino FROM captures WHERE deleted=0 ORDER BY time").fetchall()
  total=sum(row[2] for row in rows);removed=[]
  for path,at,size,mtime,ino in rows:
   try:expired=unix(at)<cutoff
   except ValueError:continue
   if not expired and total<=value["max_storage_bytes"]:continue
   safe=self.capture_path(path)
   if safe.exists():
    st=safe.stat()
    if (st.st_size,st.st_mtime_ns,str(st.st_ino))!=(size,mtime,ino):raise ValueError("Снимок изменён; удаление остановлено: "+str(safe))
    no_links(safe,True);safe.unlink()
   with self.connect() as db:db.execute("UPDATE captures SET deleted=1 WHERE path=?",(path,))
   total-=size;removed.append(str(safe))
  with self.connect() as db:
   mark=datetime.datetime.fromtimestamp(cutoff,datetime.timezone.utc).isoformat();db.execute("DELETE FROM segments WHERE end<?",(mark,));db.execute("DELETE FROM events WHERE time<?",(mark,))
  return removed
class CaptureScheduler:
 def __init__(self):self.active=False;self.due=0.;self.request=""
 def ready(self,config,monotonic,unlocked,idle):
  enabled=config["enabled"]
  if enabled and not self.active:self.due=monotonic
  if not enabled:self.due=monotonic;self.request=""
  self.active=enabled;activity_allowed=not config.get("skip_idle",False) or idle<300
  return enabled and unlocked and activity_allowed and (monotonic>=self.due or bool(config["capture_request"] and config["capture_request"]!=self.request))
 def captured(self,config,monotonic):self.due=monotonic+config["interval_seconds"];self.request=config["capture_request"]
def windows_api():
 from ctypes import wintypes as w
 u=ctypes.WinDLL("user32",use_last_error=True);k=ctypes.WinDLL("kernel32",use_last_error=True);g=ctypes.WinDLL("gdi32",use_last_error=True)
 u.OpenInputDesktop.argtypes=[w.DWORD,w.BOOL,w.DWORD];u.OpenInputDesktop.restype=w.HANDLE;u.CloseDesktop.argtypes=[w.HANDLE]
 u.GetUserObjectInformationW.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]
 u.GetForegroundWindow.restype=w.HWND;u.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)];u.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
 k.GetTickCount64.restype=ctypes.c_ulonglong;k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
 k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)];k.CloseHandle.argtypes=[w.HANDLE]
 return u,k,g
def desktop_state():
 from ctypes import wintypes as w
 u,k,_=windows_api();handle=u.OpenInputDesktop(0,False,1);unlocked=False
 if handle:
  try:
   name=ctypes.create_unicode_buffer(256);needed=w.DWORD()
   unlocked=bool(u.GetUserObjectInformationW(handle,2,name,ctypes.sizeof(name),ctypes.byref(needed))) and name.value.lower()=="default"
  finally:u.CloseDesktop(handle)
 class Last(ctypes.Structure):_fields_=[("cbSize",w.UINT),("dwTime",w.DWORD)]
 last=Last(ctypes.sizeof(Last),0);idle=10**9
 if u.GetLastInputInfo(ctypes.byref(last)):idle=(((k.GetTickCount64()&0xFFFFFFFF)-last.dwTime)&0xFFFFFFFF)/1000.
 return unlocked,idle,k.GetTickCount64()/1000.
def foreground(record_titles=False):
 from ctypes import wintypes as w
 u,k,_=windows_api();hwnd=u.GetForegroundWindow();pid=w.DWORD();u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
 handle=k.OpenProcess(0x1000,False,pid.value);app="unknown";title=""
 if handle:
  try:
   path=ctypes.create_unicode_buffer(32768);length=w.DWORD(len(path))
   if k.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(length)):app=Path(path.value).name
  finally:k.CloseHandle(handle)
 if record_titles:
  text=ctypes.create_unicode_buffer(256);u.GetWindowTextW(hwnd,text,len(text));title=text.value[:200]
 return app,title
def capture_png():
 """GDI current user desktop only. No impersonation or input injection."""
 from ctypes import wintypes as w
 u,_,g=windows_api()
 if not desktop_state()[0]:raise RuntimeError("Рабочий стол заблокирован или открыт защищённый экран")
 u.GetDC.argtypes=[w.HWND];u.GetDC.restype=w.HDC;u.ReleaseDC.argtypes=[w.HWND,w.HDC]
 g.CreateCompatibleDC.argtypes=[w.HDC];g.CreateCompatibleDC.restype=w.HDC
 g.CreateCompatibleBitmap.argtypes=[w.HDC,ctypes.c_int,ctypes.c_int];g.CreateCompatibleBitmap.restype=w.HBITMAP
 g.SelectObject.argtypes=[w.HDC,w.HANDLE];g.SelectObject.restype=w.HANDLE;g.DeleteObject.argtypes=[w.HANDLE];g.DeleteDC.argtypes=[w.HDC]
 g.BitBlt.argtypes=[w.HDC,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HDC,ctypes.c_int,ctypes.c_int,w.DWORD]
 g.GetDIBits.argtypes=[w.HDC,w.HBITMAP,w.UINT,w.UINT,ctypes.c_void_p,ctypes.c_void_p,w.UINT]
 x,y,width,height=[u.GetSystemMetrics(i) for i in (76,77,78,79)]
 if width<=0 or height<=0 or width*height>64_000_000:raise RuntimeError("Недопустимый размер экрана")
 source=memory=bitmap=previous=None
 try:
  source=u.GetDC(None);memory=g.CreateCompatibleDC(source);bitmap=g.CreateCompatibleBitmap(source,width,height)
  if not source or not memory or not bitmap:raise ctypes.WinError(ctypes.get_last_error())
  previous=g.SelectObject(memory,bitmap)
  if not g.BitBlt(memory,0,0,width,height,source,x,y,0x00CC0020|0x40000000):raise ctypes.WinError(ctypes.get_last_error())
  g.SelectObject(memory,previous);previous=None
  class Header(ctypes.Structure):
   _fields_=[("size",w.DWORD),("width",w.LONG),("height",w.LONG),("planes",w.WORD),("bits",w.WORD),("compression",w.DWORD),("sizeimage",w.DWORD),("xppm",w.LONG),("yppm",w.LONG),("used",w.DWORD),("important",w.DWORD)]
  header=Header(ctypes.sizeof(Header),width,-height,1,32,0,0,0,0,0,0);buffer=ctypes.create_string_buffer(width*height*4)
  if g.GetDIBits(memory,bitmap,0,height,buffer,ctypes.byref(header),0)!=height:raise ctypes.WinError(ctypes.get_last_error())
  if not desktop_state()[0]:raise RuntimeError("Сеанс заблокирован во время снимка")
  raw=buffer.raw;encoded=bytearray((width*3+1)*height)
  for row in range(height):
   a=row*width*4;rgb=bytearray(width*3);block=raw[a:a+width*4];rgb[0::3]=block[2::4];rgb[1::3]=block[1::4];rgb[2::3]=block[0::4];b=row*(width*3+1);encoded[b]=0;encoded[b+1:b+1+width*3]=rgb
  def chunk(kind,data):return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xFFFFFFFF)
  return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(encoded,3))+chunk(b"IEND",b"")
 finally:
  if previous and memory:g.SelectObject(memory,previous)
  if bitmap:g.DeleteObject(bitmap)
  if memory:g.DeleteDC(memory)
  if source:u.ReleaseDC(None,source)
