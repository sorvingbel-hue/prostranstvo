"""Visible local collector; each new process/logon session starts paused."""
import argparse,ctypes,datetime,subprocess,sys,time,tkinter as tk
from pathlib import Path
from paths import APP_ROOT
from activity_core import ActivityStore,CaptureScheduler,exclusive_lock,desktop_state,foreground,capture_png,utc_now
class Agent:
 def __init__(self,store,root):
  self.store=store;self.root=root;self.scheduler=CaptureScheduler();self.last_enabled=False;self.last_usage=None;self.last_tick=time.monotonic();self.last_cleanup=0.;self.error="";self.last_capture=None;self.last_lock=None;self.stopped=False
  store.set_config(enabled=False);_,_,uptime=desktop_state();self.boot_utc=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=uptime)).isoformat()
  store.event("agent_start","Общая учётная запись; ручной режим выключен. Загрузка Windows около "+self.boot_utc)
  self.build_indicator();self.tick()
 def build_indicator(self):
  root=self.root;root.title("Пространство · локальная запись");root.resizable(False,False);root.attributes("-topmost",True)
  root.geometry("350x125+%d+%d"%(max(0,root.winfo_screenwidth()-375),max(0,root.winfo_screenheight()-210)))
  self.label=tk.Label(root,text="ПАУЗА · снимки не записываются",font=("Segoe UI",11,"bold"),bg="#eef2f7",fg="#354558",padx=12,pady=12);self.label.pack(fill="x")
  bar=tk.Frame(root);bar.pack(fill="both",expand=True,padx=8,pady=5)
  self.toggle=tk.Button(bar,text="Включить режим Паши",command=self.flip);self.toggle.pack(side="left")
  tk.Button(bar,text="Программа",command=self.open_app).pack(side="left",padx=5);tk.Button(bar,text="Выход",command=self.close).pack(side="right");root.protocol("WM_DELETE_WINDOW",self.close)
 def open_app(self):
  subprocess.Popen([str(APP_ROOT/'Prostranstvo.exe'),'--activity'],cwd=APP_ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
 def flip(self):
  try:self.store.set_config(enabled=not self.store.config()["enabled"])
  except Exception as error:self.error=str(error)
 def close(self):
  if self.stopped:return
  self.stopped=True
  try:
   self.store.set_config(enabled=False);self.store.event("agent_stop","Модуль закрыт пользователем; запись выключена")
   self.store.write_status(enabled=False,running=False,last_capture=self.last_capture,next_capture=None,error=self.error,stopped=True)
  finally:self.root.destroy()
 def tick(self):
  if self.stopped:return
  current=time.monotonic();at=utc_now();elapsed=current-self.last_tick;self.last_tick=current
  try:
   config=self.store.config();unlocked,idle,_=desktop_state();enabled=config["enabled"]
   if enabled!=self.last_enabled:
    self.store.event("pasha_mode_on" if enabled else "pasha_mode_off","Режим включён вручную; личность не подтверждается" if enabled else "Режим выключен вручную")
    self.last_usage=None;self.last_enabled=enabled;self.error=""
   if unlocked!=self.last_lock:
    self.store.event("desktop_unlocked" if unlocked else "desktop_locked","Состояние интерактивного рабочего стола");self.last_lock=unlocked;self.last_usage=None
   if elapsed>30:
    self.store.event("resume_or_gap","Пауза между отметками %.0f секунд; это не время активного использования"%elapsed);self.last_usage=None
   activity_allowed=not config.get("skip_idle",False) or idle<300
   if enabled and unlocked and activity_allowed:
    app,title=foreground(config["record_titles"])
    if self.last_usage and elapsed<=15:
     previous_app,previous_title,previous_time=self.last_usage;self.store.add_usage(previous_app,previous_title,elapsed,previous_time,at)
    self.last_usage=(app,title,at)
   else:self.last_usage=None
   if current-self.last_cleanup>3600:self.store.prune();self.last_cleanup=current
   if self.scheduler.ready(config,current,unlocked,idle):
    png=capture_png()
    if self.store.config()["enabled"] and desktop_state()[0]:
     path=self.store.save_capture(png);self.last_capture={"path":str(path),"time":utc_now()};self.scheduler.captured(config,time.monotonic())
   next_capture=None
   if enabled:next_capture=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=max(0,self.scheduler.due-time.monotonic()))).isoformat()
   text="ЗАПИСЬ · РЕЖИМ ПАШИ" if enabled else "ПАУЗА · снимки не записываются"
   if enabled and (not unlocked or (config.get("skip_idle",False) and idle>=300)):text="РЕЖИМ ПАШИ · ожидание активности"
   self.label.config(text=text,bg="#8e2027" if enabled else "#eef2f7",fg="white" if enabled else "#354558");self.toggle.config(text="Пауза" if enabled else "Включить режим Паши")
   self.store.write_status(enabled=enabled,last_capture=self.last_capture,next_capture=next_capture,error=self.error,locked=not unlocked,idle_seconds=round(idle),boot_utc=self.boot_utc,mode="manual_shared_account",stopped=False)
  except Exception as error:
   self.error=str(error)[:500];self.last_usage=None
   try:
    self.store.set_config(enabled=False);self.store.event("recording_error",self.error);self.store.write_status(enabled=False,last_capture=self.last_capture,next_capture=None,error=self.error,stopped=False)
   except Exception:pass
   self.label.config(text="ОШИБКА · запись выключена",bg="#8e2027",fg="white");self.toggle.config(text="Включить режим Паши")
  self.root.after(5000,self.tick)
def main():
 parser=argparse.ArgumentParser();parser.add_argument("--background",action="store_true");parser.parse_args()
 try:
  function=ctypes.windll.user32.SetProcessDpiAwarenessContext;function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_bool;function(ctypes.c_void_p(-4))
 except Exception:
  try:ctypes.windll.user32.SetProcessDPIAware()
  except Exception:pass
 store=ActivityStore()
 try:
  with exclusive_lock(store.base/"collector.lock",False):
   root=tk.Tk();Agent(store,root);root.mainloop()
 except RuntimeError as error:
  if "уже запущен" not in str(error):raise
if __name__=="__main__":main()
