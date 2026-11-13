"""Read ordinary browser history in standard folders of the current Windows user."""
import contextlib,datetime,hashlib,html,json,os,re,shutil,sqlite3,tempfile,time,urllib.parse
from pathlib import Path
from activity_core import no_links
from paths import DATA_ROOT
LOCAL_ROOT=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')
ROAMING_ROOT=Path(os.environ.get('APPDATA') or Path.home()/'AppData'/'Roaming')
PROFILE_ROOT=LOCAL_ROOT/'Yandex'/'YandexBrowser'/'User Data'
WORKSPACE=DATA_ROOT/'browser-history'
EPOCH=datetime.datetime(1601,1,1,tzinfo=datetime.timezone.utc)
UNIX_OFFSET_US=11644473600000000
CHROMIUM_PROFILE=re.compile(r"Default|Profile \d+")
CHROMIUM_ROOTS=(
 ('Яндекс', 'Yandex/YandexBrowser/User Data'),
 ('Chrome', 'Google/Chrome/User Data'),
 ('Chrome Beta', 'Google/Chrome Beta/User Data'),
 ('Chrome Dev', 'Google/Chrome Dev/User Data'),
 ('Chrome Canary', 'Google/Chrome SxS/User Data'),
 ('Edge', 'Microsoft/Edge/User Data'),
 ('Edge Beta', 'Microsoft/Edge Beta/User Data'),
 ('Edge Dev', 'Microsoft/Edge Dev/User Data'),
 ('Edge Canary', 'Microsoft/Edge SxS/User Data'),
 ('Brave', 'BraveSoftware/Brave-Browser/User Data'),
 ('Vivaldi', 'Vivaldi/User Data'),
 ('Chromium', 'Chromium/User Data'),
)
HISTORY_NOTE=("Обычная история найденных профилей текущей учётной записи Windows. "
              "Записи означают открытия страниц. Личность и просмотр ролика не подтверждены. "
              "Инкогнито, удалённая история и профили вне стандартных папок могут отсутствовать.")
YOUTUBE={"youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtu.be","www.youtu.be"}
VIDEO=re.compile(r"[A-Za-z0-9_-]{11}")
AUTH=re.compile(r"(?i)(oauth|callback|password|reset|signin|log-?in|access.?token|verify|activation)")
def stamp(value):
 try:return (EPOCH+datetime.timedelta(microseconds=int(value))).astimezone().isoformat()
 except (TypeError,ValueError,OverflowError):return ""
def safe_url(value):
 try:
  p=urllib.parse.urlsplit(value);host=(p.hostname or "").lower()
  if p.scheme not in ("http","https") or not host:return None
  parts=p.path.strip("/").split("/");video=""
  if host in YOUTUBE:
   if p.path=="/watch":video=urllib.parse.parse_qs(p.query).get("v",[""])[0]
   elif len(parts)>=2 and parts[0] in ("shorts","embed","live"):video=parts[1]
   elif host in ("youtu.be","www.youtu.be") and parts:video=parts[0]
  if video and VIDEO.fullmatch(video):
   return {"url":"https://www.youtube.com/watch?v="+video,"site":host,"video_id":video,"sensitive":False}
  sensitive=bool(AUTH.search(p.path))
  segments=[]
  for part in p.path.split("/"):
   if len(part)>64 or re.fullmatch(r"[A-Fa-f0-9]{32,}",part):segments.append("[hidden]")
   else:segments.append(part)
  path="/" if sensitive else "/".join(segments)
  port=p.port
  hostpart=("["+host+"]") if ":" in host else host
  if port and port not in (80,443):hostpart+=":"+str(port)
  return {"url":urllib.parse.urlunsplit((p.scheme,hostpart,path,"","")),"site":host,"video_id":"","sensitive":sensitive}
 except (ValueError,TypeError):return None
def digest(path):
 h=hashlib.sha256()
 with open(path,"rb") as f:
  for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
 return h.hexdigest()
def history_files(database):
 if database not in ('History','places.sqlite'):raise ValueError('Неизвестная база истории')
 return tuple(database+suffix for suffix in ('','-journal','-wal','-shm'))

def group(profile,database='History'):
 result={}
 for name in history_files(database):
  p=profile/name
  if p.exists():
   no_links(p,True);st=p.stat()
   if st.st_size>128*1024**2:raise ValueError("База истории слишком большая для текущего режима")
   result[name]=(st.st_size,st.st_mtime_ns,st.st_ino,digest(p))
 return result
@contextlib.contextmanager
def snapshot(profile,workspace,database='History'):
 """Stable copied transaction group, rollback recovery only in a private temporary directory."""
 files=history_files(database)
 no_links(profile);no_links(workspace);workspace.mkdir(parents=True,exist_ok=True);temporary_root=workspace/"_tmp";temporary_root.mkdir(exist_ok=True);no_links(temporary_root)
 last_error=None
 for attempt in range(3):
  temp=Path(tempfile.mkdtemp(prefix="history-",dir=temporary_root))
  connection=None;yielded=False
  try:
   if temp.resolve().parent!=temporary_root.resolve():raise ValueError("Invalid temporary path")
   before=group(profile,database)
   if database not in before:raise FileNotFoundError("История браузера не найдена")
   if sum(x[0] for x in before.values())>192*1024**2:raise ValueError("Слишком большой снимок истории")
   for name in before:shutil.copyfile(profile/name,temp/name)
   after=group(profile,database)
   if before!=after:raise RuntimeError("История изменилась во время чтения")
   if any(digest(temp/name)!=meta[3] for name,meta in before.items()):raise RuntimeError("Не совпала контрольная сумма снимка")
   journal=temp/(database+"-journal")
   if journal.exists():
    raw=journal.read_bytes()
    if len(raw)>=16 and raw[-8:]==bytes.fromhex("d9d505f920a163d7"):
     length=int.from_bytes(raw[-16:-12],"big")
     if 0<length<=len(raw)-16:raise RuntimeError("Complex multi-database journal; close browser before reading this profile")
   connection=sqlite3.connect(temp/database,timeout=3)
   connection.execute("PRAGMA trusted_schema=OFF")
   if connection.execute("PRAGMA quick_check").fetchone()[0]!="ok":raise RuntimeError("Снимок истории не прошёл проверку")
   connection.execute("PRAGMA query_only=ON")
   yielded=True
   yield connection
   return
  except (OSError,RuntimeError,sqlite3.Error,ValueError) as error:
   if yielded:raise
   last_error=error
   if attempt==2:raise RuntimeError("Не удалось прочитать согласованный снимок истории: "+str(error)) from error
   time.sleep(.08)
  finally:
   if connection:connection.close()
   if temp.resolve().parent==temporary_root.resolve():
    for name in files:
     p=temp/name
     if p.exists():no_links(p,True);p.unlink()
    temp.rmdir()
 raise RuntimeError(str(last_error))
def _discover_profiles(profile_root,local_root,roaming_root):
 if profile_root is not None:
  roots=[('Яндекс',Path(profile_root),'History',False)]
 else:
  roots=[(browser,local_root/relative,'History',False) for browser,relative in CHROMIUM_ROOTS]
  roots.extend((browser,roaming_root/'Opera Software'/directory,'History',True)
               for browser,directory in (('Opera','Opera Stable'),('Opera GX','Opera GX Stable')))
  roots.append(('Firefox',roaming_root/'Mozilla'/'Firefox'/'Profiles','places.sqlite',False))
 profiles=[]
 errors=[]
 for browser,root,database,allow_flat in roots:
  try:
   no_links(root)
   if not root.is_dir():continue
   children=[path for path in root.iterdir() if database=='places.sqlite' or CHROMIUM_PROFILE.fullmatch(path.name)]
  except (OSError,ValueError) as error:
   errors.append(browser+': '+str(error));continue
  candidates=[]
  if allow_flat:candidates.append((root,'Основной'))
  candidates.extend((path,path.name) for path in sorted(children))
  found=[]
  for path,name in candidates:
   try:
    no_links(path)
    if not path.is_dir():continue
    history=path/database
    no_links(history)
    if not history.is_file():continue
    # Keep Yandex identifiers already used by the archive's deduplication key.
    identifier=name if browser=='Яндекс' else f'{browser} / {name}'
    found.append((browser,identifier,path,database))
   except (OSError,ValueError) as error:
    errors.append(f'{browser} / {name}: {error}')
  if len(found)>10:errors.append(f'{browser}: достигнут лимит в 10 профилей')
  profiles.extend(found[:10])
 return profiles,errors

def _profile_visits(connection,database,cutoff,limit):
 if database=='places.sqlite':
  places,visits,visit_url,visit_time,offset='moz_places','moz_historyvisits','place_id','visit_date',UNIX_OFFSET_US
 else:
  places,visits,visit_url,visit_time,offset='urls','visits','url','visit_time',0
 required={places:{'id','url','title'},visits:{'id',visit_url,visit_time}}
 for table,columns in required.items():
  actual={row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
  if not columns<=actual:raise ValueError('Формат истории изменился: '+table)
 source_cutoff=cutoff-offset
 total=connection.execute(f'SELECT COUNT(*) FROM {visits} WHERE {visit_time}>=?',(source_cutoff,)).fetchone()[0]
 records=connection.execute(f'SELECT v.id,v.{visit_time},u.url,u.title FROM {visits} v '
                            f'JOIN {places} u ON u.id=v.{visit_url} WHERE v.{visit_time}>=? '
                            f'ORDER BY v.{visit_time} DESC LIMIT ?',(source_cutoff,limit+1))
 return total,offset,records

def read_history(days=7,limit=5000,profile_root=None,workspace=WORKSPACE,*,local_root=None,roaming_root=None):
 if days not in (1,7,30,90):raise ValueError("Выберите 1, 7, 30 или 90 дней")
 if not 1<=limit<=20000:raise ValueError("Неверный лимит")
 rows=[];total=0;truncated=False;sources=[]
 profiles,errors=_discover_profiles(profile_root,Path(local_root) if local_root is not None else LOCAL_ROOT,
                                    Path(roaming_root) if roaming_root is not None else ROAMING_ROOT)
 if not profiles:raise RuntimeError('; '.join(errors) if errors else 'Профили с историей браузера не найдены')
 cutoff=int((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=days)-EPOCH).total_seconds()*1000000)
 for browser,identifier,profile,database in profiles:
  try:
   with snapshot(profile,workspace,database) as connection:
    profile_total,offset,records=_profile_visits(connection,database,cutoff,limit)
    total+=profile_total
    for ident,at,url,title in records:
     if type(at) is not int or not isinstance(url,str):continue
     at+=offset
     clean=safe_url(url)
     if not clean:continue
     label="".join(x for x in title if x.isprintable())[:240] if isinstance(title,str) else ""
     if clean["sensitive"]:label="Страница входа — подробности скрыты"
     rows.append({**clean,"visit_id":str(ident),"profile":identifier,"time":stamp(at),"title":label or clean["site"],"raw_time":at})
   sources.append({'browser':browser,'profile':identifier,'visits':profile_total})
   rows.sort(key=lambda row:row['raw_time'],reverse=True)
   if len(rows)>limit:
    del rows[limit:]
    truncated=True
  except (RuntimeError,OSError,ValueError,sqlite3.Error) as error:errors.append(identifier+": "+str(error))
 if not sources and errors:raise RuntimeError("; ".join(errors))
 sites={}
 for row in rows:sites[row["site"]]=sites.get(row["site"],0)+1
 return {"generated":datetime.datetime.now().astimezone().isoformat(),"days":days,"rows":rows,"youtube":[r for r in rows if r["video_id"]],"sites":sorted(sites.items(),key=lambda x:x[1],reverse=True),"total_visits_in_period":total,"returned":len(rows),"truncated":truncated or total>limit,"errors":errors,"sources":sources,"note":HISTORY_NOTE}

def profile_label(profile):
 return 'Яндекс / '+profile if CHROMIUM_PROFILE.fullmatch(profile) else profile

def export_html(report,workspace=WORKSPACE):
 no_links(workspace);workspace.mkdir(parents=True,exist_ok=True);target=workspace/"history-latest.html";no_links(target,target.exists())
 def section(rows):
  rendered=[]
  for row in rows:
   title=html.escape(row['title'])
   address=safe_url(row['url'])
   if address:
    url=html.escape(address['url'],quote=True)
    title=f'<a rel="noreferrer noopener" href="{url}">{title}</a><br><small>{url}</small>'
   opened=html.escape(row['time'].replace('T',' '))
   profile=html.escape(profile_label(row['profile']))
   rendered.append(f'<tr><td>{opened}</td><td>{profile}</td><td>{title}</td></tr>')
  return ''.join(rendered)
 doc="<!doctype html><html lang=ru><meta charset=utf-8><meta name=referrer content=no-referrer><title>История браузеров</title><style>body{font:16px system-ui;background:#edf2f7;color:#182b3e;margin:30px}table{width:100%;border-collapse:collapse;background:white}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}small{overflow-wrap:anywhere;color:#567}h2{margin-top:30px}a{color:#235fc0}.note{background:#fff5cf;padding:16px}</style><h1>Сайты и открытые ролики YouTube</h1><p class=note>"+html.escape(report["note"])+"</p><p>Период: последние "+str(report["days"])+" дней. Показано "+str(report["returned"])+" записей; записей в базе за период: "+str(report["total_visits_in_period"])+". Обновлено "+html.escape(report["generated"])+". Параметры ссылок, данные входа и фрагменты URL скрыты.</p>"
 if report["truncated"]:doc+="<p><b>Выборка ограничена последними записями. Уменьшите период для подробностей.</b></p>"
 if report["errors"]:doc+="<p>"+html.escape("; ".join(report["errors"]))+"</p>"
 doc+="<h2>Открытые ролики YouTube — "+str(len(report["youtube"]))+"</h2><table><tr><th>Время</th><th>Профиль</th><th>Название и ссылка</th></tr>"+section(report["youtube"])+"</table><h2>Посещённые страницы</h2><table><tr><th>Время</th><th>Профиль</th><th>Страница</th></tr>"+section(report["rows"])+"</table></html>"
 fd,name=tempfile.mkstemp(prefix=".history-",suffix=".tmp",dir=workspace)
 temporary=Path(name)
 try:
  with os.fdopen(fd,"w",encoding="utf-8") as f:f.write(doc);f.flush();os.fsync(f.fileno())
  no_links(target,target.exists());os.replace(temporary,target);return target
 finally:
  if temporary.exists():no_links(temporary,True);temporary.unlink()
if __name__=="__main__":
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument("--days",type=int,default=7);a=parser.parse_args()
 report=read_history(a.days);path=export_html(report)
 print(json.dumps({"report":str(path),"days":report["days"],"returned":report["returned"],"total_visits_in_period":report["total_visits_in_period"],"youtube_openings":len(report["youtube"]),"truncated":report["truncated"],"top_sites":report["sites"][:12],"recent_youtube":report["youtube"][:12],"errors":report["errors"]},ensure_ascii=False))
