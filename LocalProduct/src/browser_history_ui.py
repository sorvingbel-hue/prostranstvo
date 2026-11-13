"""Visible archive, automatic schedule controls and optional on-demand source read."""
import queue,threading,tkinter as tk,os,webbrowser,datetime
from tkinter import ttk
from browser_history import read_history,export_html,profile_label,safe_url,WORKSPACE
from history_schedule import HistoryArchive,task_status
def clock(value):
 if not value:return "—"
 try:return datetime.datetime.fromisoformat(value.replace("Z","+00:00")).astimezone().strftime("%d.%m %H:%M")
 except (ValueError,TypeError):return str(value)[:30]
class BrowserHistoryPanel(ttk.Frame):
 def __init__(self,parent):
  super().__init__(parent,padding=14);self.closed=False;self.busy=False;self.status_busy=False;self.messages=queue.Queue();self.rows={};self.report_path=WORKSPACE/"history-latest.html";self.timer=None;self.status_timer=None;self.archive=HistoryArchive();self.last_success=None
  self.bind('<Destroy>',self.destroyed,add='+');self.columnconfigure(0,weight=1);self.rowconfigure(4,weight=1)
  ttk.Label(self,text='Пространство · собранная история сайтов и YouTube',font=('Segoe UI',16,'bold')).grid(row=0,column=0,sticky='w')
  bar=ttk.Frame(self);bar.grid(row=1,column=0,sticky='ew',pady=9);ttk.Label(bar,text='Последние дни:').pack(side='left')
  self.days=tk.StringVar(value='7');ttk.Combobox(bar,textvariable=self.days,values=('1','7','30','90'),state='readonly',width=5).pack(side='left',padx=8)
  ttk.Button(bar,text='Показать собранное',command=self.cached).pack(side='left');ttk.Button(bar,text='Собрать сейчас',command=self.collect).pack(side='left',padx=6);ttk.Button(bar,text='История браузера сейчас',command=self.direct).pack(side='left');ttk.Button(bar,text='Отчёт',command=self.open_report).pack(side='left',padx=6)
  schedule=ttk.Frame(self);schedule.grid(row=2,column=0,sticky='ew');self.auto=tk.BooleanVar(value=False);self.interval=tk.StringVar(value='4')
  ttk.Checkbutton(schedule,text='Автоматический сбор',variable=self.auto,command=self.configure_schedule).pack(side='left');ttk.Label(schedule,text='каждые').pack(side='left',padx=(12,3));combo=ttk.Combobox(schedule,textvariable=self.interval,values=('1','4','6','12','24'),state='readonly',width=4);combo.pack(side='left');combo.bind('<<ComboboxSelected>>',lambda event:self.configure_schedule());ttk.Label(schedule,text='ч · архив 14 дней · работает при закрытом окне').pack(side='left',padx=5)
  self.schedule_text=tk.StringVar(value='Проверяю расписание…');ttk.Label(schedule,textvariable=self.schedule_text,wraplength=1070).pack(side='bottom',anchor='w',pady=4)
  self.notice=tk.StringVar(value='Общий профиль компьютера: открытия страниц не доказывают личность и досмотр роликов.');ttk.Label(self,textvariable=self.notice,wraplength=1080,justify='left').grid(row=3,column=0,sticky='ew',pady=(3,7))
  tabs=ttk.Notebook(self);tabs.grid(row=4,column=0,sticky='nsew');self.trees={}
  for key,title in (('youtube','Открытые ролики YouTube'),('sites','Сайты и страницы')):
   frame=ttk.Frame(tabs);frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1);tabs.add(frame,text=title);tree=ttk.Treeview(frame,columns=('time','collected','browser','title','site'),show='headings',height=13)
   for col,label,width in (('time','Открыто',110),('collected','Собрано',110),('browser','Браузер и профиль',160),('title','Название',340),('site','Сайт',130)):tree.heading(col,text=label);tree.column(col,width=width)
   tree.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(frame,command=tree.yview);scroll.grid(row=0,column=1,sticky='ns');tree.configure(yscrollcommand=scroll.set);tree.bind('<Double-1>',lambda event,t=tree:self.open_selected(t));self.trees[key]=tree
  ttk.Label(self,text='Локальная история найденных профилей текущей учётной записи Windows. Расписание работает отдельно от скриншотов. Частные окна и записи, удалённые до сбора, могут отсутствовать.',wraplength=1080).grid(row=5,column=0,sticky='ew',pady=8)
  self.timer=self.after(100,self.pump);self.poll();self.cached()
 def destroyed(self,event):
  if event.widget is self:
   self.closed=True
   for timer in (self.timer,self.status_timer):
    if timer:
     try:self.after_cancel(timer)
     except tk.TclError:pass
 def work(self,operation):
  if self.busy:return
  self.busy=True
  messages=self.messages
  def worker():
   try:messages.put(('report',operation()))
   except Exception as error:messages.put(('error',str(error)))
  threading.Thread(target=worker,daemon=True).start()
 def cached(self):
  days=int(self.days.get());archive=self.archive;report_path=self.report_path
  self.work(lambda:(archive.report(days),report_path,'Архив Пространства'))
 def collect(self):
  self.notice.set('Собираю новые записи в архив…');days=int(self.days.get());archive=self.archive
  def run():archive.collect(force=True);return archive.report(days),archive.base/'history-latest.html','Архив Пространства'
  self.work(run)
 def direct(self):
  days=int(self.days.get());self.notice.set('Читаю текущую историю браузера…')
  def run():
   result=read_history(days);return result,export_html(result),'История браузера сейчас'
  self.work(run)
 def configure_schedule(self):
  enabled=bool(self.auto.get());hours=int(self.interval.get());days=int(self.days.get());archive=self.archive;report_path=self.report_path
  def run():
   archive.configure_task(enabled,hours);return archive.report(days),report_path,'Настройка расписания сохранена'
  self.work(run)
 def poll(self):
  if self.closed:return
  if not self.status_busy:
   self.status_busy=True
   archive,messages=self.archive,self.messages
   def read():
    try:messages.put(('status',(archive.config(),archive.state(),task_status())))
    except Exception as error:messages.put(('status_error',str(error)))
   threading.Thread(target=read,daemon=True).start()
  self.status_timer=self.after(5000,self.poll)
 def pump(self):
  if self.closed:return
  try:
   while True:
    kind,data=self.messages.get_nowait()
    if kind in ('status','status_error'):
     self.status_busy=False
     if kind=='status_error':self.schedule_text.set('Не удалось прочитать расписание: '+data);continue
     config,state,task=data
     if not self.busy:self.auto.set(config['enabled']);self.interval.set(str(config['interval_hours']))
     self.schedule_text.set(('Автосбор включён' if config['enabled'] else 'Автосбор выключен')+' · последний: '+clock(state.get('last_success'))+' · следующий: '+(clock(task.get('next_run')) if config['enabled'] else '—')+(' · '+state['error'] if state.get('error') else '')+(' · задача не найдена' if config['enabled'] and not task.get('installed') else ''))
     if state.get('last_success')!=self.last_success:
      self.last_success=state.get('last_success')
      if not self.busy:self.cached()
     continue
    self.busy=False
    if kind=='error':self.notice.set('Операция не выполнена: '+data);continue
    report,self.report_path,origin=data;self.rows.clear()
    for key,tree in self.trees.items():
     tree.delete(*tree.get_children());items=report['youtube'] if key=='youtube' else report['rows']
     for i,row in enumerate(items):
      ident=key+str(i);self.rows[ident]=row;tree.insert('','end',iid=ident,values=(clock(row['time']),clock(row.get('first_collected') or report.get('generated')),profile_label(row['profile']),row['title'],row['site']))
    self.notice.set(origin+' · '+str(report['returned'])+' записей; открытий роликов: '+str(len(report['youtube']))+'. '+('Выборка ограничена. ' if report['truncated'] else '')+'Общий профиль, досмотр не подтверждается.')
  except queue.Empty:pass
  self.timer=self.after(150,self.pump)
 def open_selected(self,tree):
  selected=tree.selection()
  if selected and selected[0] in self.rows:
   address=safe_url(self.rows[selected[0]]['url'])
   if address:webbrowser.open(address['url'])
   else:self.notice.set('В записи нет подходящей ссылки на сайт.')
 def open_report(self):
  if self.report_path and self.report_path.is_file():os.startfile(str(self.report_path))
  else:self.notice.set('Отчёт появится после первого успешного сбора.')
