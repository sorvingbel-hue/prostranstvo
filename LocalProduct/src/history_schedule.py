"""Local browser-history archive and scheduled collection. No model/network calls."""
import argparse,datetime,json,os,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from paths import APP_ROOT
from activity_core import Connection,exclusive_lock,no_links
from browser_history import WORKSPACE,EPOCH,HISTORY_NOTE,read_history,export_html
DEFAULTS={"enabled":False,"interval_hours":4,"retention_days":14}
HELPER=APP_ROOT/'HistorySchedule.exe'
_task_cache=(0.,{})
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def epoch_cutoff(days):return int((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=days)-EPOCH).total_seconds()*1000000)
def atomic_json(path,value):
 no_links(path,path.exists());fd,name=tempfile.mkstemp(prefix=".schedule-",suffix=".tmp",dir=path.parent)
 try:
  with os.fdopen(fd,"w",encoding="utf-8") as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
  no_links(path,path.exists());os.replace(name,path)
 finally:
  if os.path.exists(name):os.unlink(name)
def helper(*args):
 p=subprocess.run([str(HELPER),*args],capture_output=True,text=True,encoding="utf-8-sig",timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
 if p.returncode:raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "Планировщик Windows отклонил операцию")
 return json.loads(p.stdout)
def task_status(refresh=False):
 global _task_cache
 if refresh or time.monotonic()-_task_cache[0]>60:
  try:value=helper("status")
  except Exception as error:value={"installed":False,"error":str(error)}
  _task_cache=(time.monotonic(),value)
 return _task_cache[1]
class HistoryArchive:
 def __init__(self,base=WORKSPACE):
  self.base=Path(base).absolute();no_links(self.base);self.base.mkdir(parents=True,exist_ok=True)
  self.config_path=self.base/"schedule-config.json";self.state_path=self.base/"schedule-state.json";self.db=self.base/"history-archive.sqlite"
  with self.connect() as c:c.execute("""CREATE TABLE IF NOT EXISTS visits(profile TEXT NOT NULL,visit_id TEXT NOT NULL,raw_time INTEGER NOT NULL,time TEXT NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,site TEXT NOT NULL,video_id TEXT NOT NULL,first_collected TEXT NOT NULL,last_collected TEXT NOT NULL,PRIMARY KEY(profile,visit_id,raw_time))""");c.execute("CREATE INDEX IF NOT EXISTS visit_time_index ON visits(raw_time)")
 def connect(self):
  for p in (self.db,Path(str(self.db)+"-journal"),Path(str(self.db)+"-wal"),Path(str(self.db)+"-shm")):no_links(p,p.exists())
  return sqlite3.connect(self.db,timeout=5,factory=Connection)
 def _read(self,path,default):
  try:no_links(path,True);return json.loads(path.read_text(encoding="utf-8-sig"))
  except FileNotFoundError:return dict(default)
 def config(self):
  c={**DEFAULTS,**self._read(self.config_path,{})}
  if set(c)!=set(DEFAULTS) or type(c["enabled"]) is not bool or type(c["interval_hours"]) is not int or c["interval_hours"] not in (1,4,6,12,24) or type(c["retention_days"]) is not int or not 1<=c["retention_days"]<=365:raise ValueError("Неверные настройки расписания")
  return c
 def set_config(self,**changes):
  with exclusive_lock(self.base/"schedule-config.lock"):
   value=self.config()
   if set(changes)-set(DEFAULTS):raise ValueError("Неизвестная настройка")
   value.update(changes)
   if type(value["enabled"]) is not bool or type(value["interval_hours"]) is not int or value["interval_hours"] not in (1,4,6,12,24) or type(value["retention_days"]) is not int or not 1<=value["retention_days"]<=365:raise ValueError("Неверные настройки расписания")
   atomic_json(self.config_path,value)
  return value
 def state(self):return self._read(self.state_path,{})
 def configure_task(self,enabled,hours=4):
  old=self.config();self.set_config(enabled=enabled,interval_hours=hours)
  if enabled:
   try:helper("enable",str(hours))
   except Exception:
    self.set_config(**old);raise
   helper("run")
  else:helper("disable")
  task_status(True)
 def ingest(self,rows,at=None):
  at=at or now();added=0;cutoff=epoch_cutoff(self.config()["retention_days"])
  with self.connect() as c:
   for r in rows:
    if r["raw_time"]<cutoff:continue
    key=(r["profile"],str(r["visit_id"]),int(r["raw_time"]))
    cur=c.execute("INSERT OR IGNORE INTO visits VALUES(?,?,?,?,?,?,?,?,?,?)",(*key,r["time"],r["url"],r["title"],r["site"],r["video_id"],at,at));added+=cur.rowcount
    c.execute("UPDATE visits SET url=?,title=?,site=?,video_id=?,last_collected=? WHERE profile=? AND visit_id=? AND raw_time=?",(r["url"],r["title"],r["site"],r["video_id"],at,*key))
   c.execute("DELETE FROM visits WHERE raw_time<?",(cutoff,))
   total=c.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
  return added,total
 def report(self,days=7,limit=5000):
  if days not in (1,7,30,90):raise ValueError("Неверный период")
  cutoff=epoch_cutoff(days)
  with self.connect() as c:
   c.row_factory=sqlite3.Row;rows=[dict(r) for r in c.execute("SELECT * FROM visits WHERE raw_time>=? ORDER BY raw_time DESC LIMIT ?",(cutoff,limit))]
   total=c.execute("SELECT COUNT(*) FROM visits WHERE raw_time>=?",(cutoff,)).fetchone()[0]
   sites=[list(r) for r in c.execute("SELECT site,COUNT(*) AS n FROM visits WHERE raw_time>=? GROUP BY site ORDER BY n DESC",(cutoff,))]
  return {"generated":datetime.datetime.now().astimezone().isoformat(),"days":days,"rows":rows,"youtube":[r for r in rows if r["video_id"]],"sites":sites,"total_visits_in_period":total,"returned":len(rows),"truncated":total>len(rows),"errors":[],"note":HISTORY_NOTE+" Архив хранится "+str(self.config()["retention_days"])+" дней. Автосбор каждые "+str(self.config()["interval_hours"])+" ч, пока пользователь вошёл в Windows."}
 def collect(self,force=False,reader=None):
  if not force and not self.config()["enabled"]:return {"skipped":"disabled"}
  try:
   with exclusive_lock(self.base/"collection.lock",False):
    previous=self.state()
    if not force and previous.get("phase") in ("completed","partial","running") and previous.get("last_started"):
     try:
      age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(previous["last_started"])).total_seconds()
      if 0<=age<120:return {"skipped":"recent"}
     except (ValueError,TypeError):pass
    state={**previous,"phase":"running","last_started":now(),"error":"","pid":os.getpid()};atomic_json(self.state_path,state)
    try:
     source=(reader or read_history)(days=7,limit=20000)
     if not force and not self.config()["enabled"]:
      state.update(phase="cancelled",finished=now());atomic_json(self.state_path,state);return {"skipped":"disabled_during_collection"}
     errors=list(source.get("errors",[]));truncated=bool(source.get("truncated"))
     if truncated:errors.append("Достигнут лимит записей за один сбор")
     added,total=self.ingest(source["rows"]);report=self.report(days=7)
     report['errors']=errors;report['truncated']=report['truncated'] or truncated
     path=export_html(report,self.base)
     state.update(phase="partial" if errors else "completed",finished=now(),new_records=added,total_records=total,source_records=len(source["rows"]),source_truncated=truncated,source_browsers=sorted({entry['browser'] for entry in source.get('sources', [])}),report=str(path),error="; ".join(errors))
     if not errors:state["last_success"]=state["finished"]
     atomic_json(self.state_path,state);return state
    except Exception as error:
     state.update(phase="failed",finished=now(),error=str(error)[:1000]);atomic_json(self.state_path,state);raise
  except RuntimeError as error:
   if "уже запущен или занят" in str(error):return {"skipped":"busy"}
   raise
if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("--collect",action="store_true");parser.add_argument("--status",action="store_true");args=parser.parse_args()
 try:
  archive=HistoryArchive();value=archive.collect() if args.collect else {"config":archive.config(),"state":archive.state(),"task":task_status(True)}
  print(json.dumps(value));sys.exit(3 if value.get("phase")=="partial" else 0)
 except Exception as error:print(str(error),file=sys.stderr);sys.exit(1)
