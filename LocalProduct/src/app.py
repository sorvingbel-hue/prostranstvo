import json,os,subprocess,sys,threading,time,tkinter as tk
from tkinter import ttk,filedialog,messagebox
from pathlib import Path
from engine import Executor,Busy
from paths import APP_ROOT,DATA_ROOT
from version import VERSION
from activity_ui import ActivityPanel
from browser_history_ui import BrowserHistoryPanel
from family_messages_ui import FamilyMessagesPanel
from study_runtime import PersistentStudyPanel as StudyPanel
from study_shell import StudyShell
from daily_plan_ui import DailyPlanPanel
from folder_ui import FolderPanel
from file_audit_ui import FileAuditPanel
from rules_ui import RulePanel
from archive_ui import ArchivePanel
from power_ui import PowerPanel
from browser_login_ui import BrowserLoginPanel
from maintenance_ui import MaintenancePanel
from security_ui import SecurityPanel
from storage_ui import StoragePanel
LABELS={'queued':'В очереди','needs_approval':'Нужна проверка плана','running':'Выполняется','completed':'Готово','failed':'Ошибка','cancelled':'Отменено','interrupted':'Прервано'}
class App(tk.Tk):
 def __init__(self):
  super().__init__();self.title('Пространство · Local Beta');self.geometry('1260x900');self.minsize(900,620)
  self.configure(bg='#edf2f7');self.engine=Executor();self.stop=threading.Event();self.current=None
  style=ttk.Style(self);style.theme_use('clam');style.configure('TFrame',background='#edf2f7');style.configure('TLabel',background='#edf2f7',font=('Segoe UI',11));style.configure('TButton',font=('Segoe UI',11),padding=9);style.configure('Treeview',rowheight=29,font=('Segoe UI',10));style.configure('Treeview.Heading',font=('Segoe UI',10,'bold'))
  head=ttk.Frame(self,padding=20);head.pack(fill='x');ttk.Label(head,text='ПРОСТРАНСТВО · LOCAL BETA '+VERSION,font=('Segoe UI',25,'bold')).pack(anchor='w');ttk.Label(head,text='Локальная очередь · проверенные переносы · данные на этом компьютере').pack(anchor='w',pady=(5,0))
  self.tabs=ttk.Notebook(self);self.tabs.pack(fill='both',expand=True,padx=20,pady=(0,16))
  self.home=ttk.Frame(self.tabs,padding=18);self.filestab=ttk.Frame(self.tabs,padding=8);self.searchtab=ttk.Frame(self.tabs,padding=18);self.settingstab=ttk.Frame(self.tabs,padding=18)
  self.filepages=ttk.Notebook(self.filestab);self.filepages.pack(fill='both',expand=True)
  self.tasks=ttk.Frame(self.filepages,padding=10);self.foldertab=ttk.Frame(self.filepages);self.rulestab=ttk.Frame(self.filepages);self.archivetab=ttk.Frame(self.filepages)
  for panel,name in [(self.tasks,'Задания'),(self.foldertab,'Папки и доступ'),(self.rulestab,'Мои правила'),(self.archivetab,'Архивы')]:self.filepages.add(panel,text=name)
  self.folderpanel=FolderPanel(self.foldertab,self.engine);self.folderpanel.pack(fill='both',expand=True)
  self.rulespanel=RulePanel(self.rulestab,self.engine);self.rulespanel.pack(fill='both',expand=True)
  self.archivepanel=ArchivePanel(self.archivetab,self.engine);self.archivepanel.pack(fill='both',expand=True)
  self.auditpanel=FileAuditPanel(self.filepages,self.engine);self.filepages.add(self.auditpanel,text='Аудит Windows')
  self.activitytab=ttk.Frame(self.tabs,padding=14)
  self.historytab=ttk.Frame(self.tabs,padding=10)
  self.messagestab=ttk.Frame(self.tabs,padding=10)
  self.studytab=ttk.Frame(self.tabs,padding=10)
  self.dailyplantab=ttk.Frame(self.tabs)
  self.powertab=ttk.Frame(self.tabs)
  for panel,name in [(self.home,'Компьютер'),(self.studytab,'Уроки'),(self.dailyplantab,'План на сегодня'),(self.powertab,'Питание'),(self.messagestab,'Общение'),(self.activitytab,'Режим Паши'),(self.historytab,'Сайты и YouTube'),(self.filestab,'Файлы'),(self.searchtab,'Общий каталог'),(self.settingstab,'Подключения')]:self.tabs.add(panel,text=name)
  self.studypanel=StudyPanel(self.studytab);self.studypanel.pack(fill='both',expand=True)
  self._build_power_panel()
  self.dailyplanpanel=DailyPlanPanel(self.dailyplantab,guard=self.studypanel.guard,study=self.studypanel,
      living_society_target=os.environ.get('PROSTRANSTVO_LIVING_SOCIETY_TARGET'))
  self.dailyplanpanel.pack(fill='both',expand=True)
  self.studypanel.enable_plan_popup(store=self.dailyplanpanel.store,
      living_society_target=self.dailyplanpanel.living_society_target)
  self.activitypanel=ActivityPanel(self.activitytab);self.activitypanel.pack(fill='both',expand=True)
  self.historypanel=BrowserHistoryPanel(self.historytab);self.historypanel.pack(fill='both',expand=True)
  ttk.Button(self.messagestab,text='Запустить котёнка на рабочем столе',command=self.open_kitten).pack(anchor='w',pady=(0,8))
  self.messagespanel=FamilyMessagesPanel(self.messagestab);self.messagespanel.pack(fill='both',expand=True)
  self._build_home_pages()
  self.homeText=tk.Text(self.homesummary,wrap='word',font=('Segoe UI',12),bg='white',relief='flat',padx=18,pady=18,height=17);self.homeText.pack(fill='both',expand=True)
  bar=ttk.Frame(self.homesummary);bar.pack(fill='x',pady=12)
  ttk.Button(bar,text='Обновить состояние',command=self.refresh_home).pack(side='left')
  ttk.Button(bar,text='Открыть локальные данные',command=lambda:os.startfile(str(DATA_ROOT))).pack(side='left',padx=8)
  bar=ttk.Frame(self.tasks);bar.pack(fill='x')
  for text,cmd in [('Обследовать папку',lambda:self.add_scan('inventory')),('Найти дубли',lambda:self.add_scan('duplicates')),('Перенести файлы',lambda:self.plan('move')),('Копировать файлы',lambda:self.plan('copy'))]:ttk.Button(bar,text=text,command=cmd).pack(side='left',padx=(0,8))
  self.tree=ttk.Treeview(self.tasks,columns=('kind','status','date'),show='headings',height=8)
  for col,name,w in [('kind','Задание',180),('status','Состояние',250),('date','Создано',230)]:self.tree.heading(col,text=name);self.tree.column(col,width=w)
  self.tree.pack(fill='x',pady=12);self.tree.bind('<<TreeviewSelect>>',self.select)
  controls=ttk.Frame(self.tasks);controls.pack(fill='x')
  ttk.Button(controls,text='Подтвердить выбранный план',command=self.approve).pack(side='left')
  ttk.Button(controls,text='Отменить задание',command=self.cancel).pack(side='left',padx=8)
  ttk.Button(controls,text='Подготовить обратный перенос',command=self.undo).pack(side='left')
  self.details=tk.Text(self.tasks,wrap='word',font=('Consolas',10),height=11,bg='white',relief='flat',padx=12,pady=12);self.details.pack(fill='both',expand=True,pady=12)
  sb=ttk.Frame(self.searchtab);sb.pack(fill='x');self.query=ttk.Entry(sb,font=('Segoe UI',12));self.query.pack(side='left',fill='x',expand=True);self.query.bind('<Return>',lambda e:self.search());ttk.Button(sb,text='Поиск в существующем индексе',command=self.search).pack(side='left',padx=8)
  self.searchresults=tk.Text(self.searchtab,wrap='word',font=('Segoe UI',11),bg='white',relief='flat');self.searchresults.pack(fill='both',expand=True,pady=12)
  connectionText=('Программа и SQLite-базы работают локально. Онлайн-база и собственный облачный сервер не нужны.\n'
   'Папка программы: '+str(APP_ROOT)+'\nПапка данных: '+str(DATA_ROOT)+'\n\n'
   'Очередь файлов выполняется, пока открыто приложение. Разрешённые папки C: и D: '
   'настраиваются в разделе Файлы → Папки и доступ. '
   'Системные папки, сохранения игр, облачные placeholders и секреты исключены.\n\n'
   'Codex подключается к ProstranstvoCore.exe mcp через локальный stdio. Инструкция CODEX-SETUP-RU.txt '
   'лежит в папке программы. Семейные команды подключаются отдельно через ProstranstvoCore.exe family-mcp; инструкция FAMILY-PET-CHAT-RU.txt. Получателю нужен собственный клиент и аккаунт Codex; запросы к ИИ требуют интернета.\n'
   'Telegram подключается отдельно своим ботом и аккаунтом, требует интернета. '
   'Снимки, история браузера и автозапуск включаются пользователем в соответствующих вкладках.\n\n'
   'Эта бета работает с обычными правами пользователя. SYSTEM-служба и административное обслуживание '
   'в пакет не входят. Установщик не включает запись, сбор истории, Telegram или автозапуск.\n'
   'Общий каталог необязателен: чужие индексы и личные данные в пакет не включаются. '
   'Без каталога доступны обследование папок, поиск дублей и проверяемые планы переноса.')
  self._build_browser_login_panel()
  ttk.Label(self.settingstab,text=connectionText,wraplength=900,justify='left').pack(anchor='w')
  ttk.Button(self.settingstab,text='Открыть папку программы',command=lambda:os.startfile(str(APP_ROOT))).pack(anchor='w',pady=16)
  ttk.Button(self.settingstab,text='Открыть локальные данные',command=lambda:os.startfile(str(DATA_ROOT))).pack(anchor='w')
  self.statusvar=tk.StringVar(value='Проверяю восстановление. Очередь пока приостановлена.');ttk.Label(self,textvariable=self.statusvar,padding=(20,8)).pack(fill='x')
  try:self.studyshell=StudyShell(self,self.studypanel);self.studyshell.install()
  except Exception:self.statusvar.set('Панель «Уроки» доступна в окне. Значок и сочетание клавиш требуют проверки.')
  self.protocol('WM_DELETE_WINDOW',self.close);
  if '--activity' in sys.argv:self.tabs.select(self.activitytab)
  if '--messages' in sys.argv:self.tabs.select(self.messagestab)
  self.refresh_home();self.refresh();threading.Thread(target=self.worker,daemon=True).start();self.after(1500,self.tick)
 def report_error(self,e):messagebox.showerror('Операция не выполнена',str(e))
 def _build_home_pages(self):
  self.homepages=ttk.Notebook(self.home);self.homepages.pack(fill='both',expand=True)
  self.homesummary=ttk.Frame(self.homepages)
  self.maintenancetab=ttk.Frame(self.homepages)
  self.homepages.add(self.homesummary,text='Обзор')
  self.homepages.add(self.maintenancetab,text='Диагностика и ИИ')
  self.maintenancepanel=MaintenancePanel(self.maintenancetab)
  self.maintenancepanel.pack(fill='both',expand=True)
  self.maintenancepanel.start_polling()
  self.securitypanel=SecurityPanel(self.homepages)
  self.homepages.add(self.securitypanel,text='Безопасность')
  self.securitypanel.start_polling()
  self.storagepanel=StoragePanel(self.homepages)
  self.homepages.add(self.storagepanel,text='Место на дисках')
  self.storagepanel.start_polling()
 def _build_power_panel(self):
  self.powerpanel=PowerPanel(self.powertab,authorize_parent=self.studypanel.authorize_parent)
  self.powerpanel.pack(fill='both',expand=True)
 def _build_browser_login_panel(self):
  self.browserloginpanel=BrowserLoginPanel(self.settingstab,authorize_parent=self.studypanel.authorize_parent)
  self.browserloginpanel.pack(fill='x',pady=(0,12))
 def destroy(self):
  for panelname in ('securitypanel','storagepanel'):
   panel=self.__dict__.get(panelname)
   if panel is not None:panel.close()
  maintenancepanel=self.__dict__.get('maintenancepanel')
  if maintenancepanel is not None:maintenancepanel.close()
  panel=self.__dict__.get('powerpanel')
  if panel is not None:panel.close()
  loginpanel=self.__dict__.get('browserloginpanel')
  if loginpanel is not None:loginpanel.close()
  super().destroy()
 def open_kitten(self):
  try:
   from desktop_pet import launch
   launch()
  except Exception as e:self.report_error(e)
 def add_scan(self,kind):
  root=filedialog.askdirectory(title='Выберите разрешённую папку')
  if not root:return
  try:self.engine.checked(root,True);self.engine.create(kind,{'root':root});self.refresh()
  except Exception as e:self.report_error(e)
 def plan(self,mode):
  files=filedialog.askopenfilenames(title='Выберите до 100 отдельных файлов')
  if not files:return
  dest=filedialog.askdirectory(title='Куда поместить файлы?')
  if not dest:return
  def background():
   try:
    self.statusvar_safe('Подготавливаю план и хеши…');j=self.engine.plan_transfer(list(files),dest,mode)
    self.after(0,lambda:self.show_job(j))
   except Exception as e:self.after(0,self.report_error,e)
  threading.Thread(target=background,daemon=True).start()
 def statusvar_safe(self,s):self.after(0,self.statusvar.set,s)
 def refresh(self):
  selected=self.tree.selection();selection=selected[0] if selected else None
  self.tree.delete(*self.tree.get_children())
  for j in self.engine.jobs():self.tree.insert('',tk.END,iid=j['id'],values=({'inventory':'Обследование','duplicates':'Поиск дублей','transfer':'Перенос / копирование'}.get(j['kind'],j['kind']),LABELS.get(j['status'],j['status']),j['created'][:19]))
  if selection and self.tree.exists(selection):self.tree.selection_set(selection)
 def select(self,e=None):
  selected=self.tree.selection()
  if selected:self.show_job(selected[0],False)
 def show_job(self,j,choose=True):
  job=self.engine.get(j);self.current=j
  lines=['Задание: '+job['id'],'Состояние: '+LABELS.get(job['status'],job['status'])]
  payload=job['payload']
  if job['kind']=='transfer':
   lines+=['Режим: '+payload['mode'],'Объём: '+str(round(payload['total_bytes']/1024**2,2))+' МиБ','']
   for r in payload['records']:lines += [r['src'],'  → '+r['dst'],'  SHA-256: '+r['sha'],'']
  else:lines+=['Папка: '+payload.get('root','')]
  result=job.get('result')
  if result:
   lines+=['','РЕЗУЛЬТАТ']
   if 'files' in result:lines+=['Файлов: '+str(result['files'])+' · '+str(round(result['bytes']/1024**3,3))+' ГиБ','Пропущено защищённых/недоступных объектов: '+str(result['skipped'])]
   for r in result.get('largest',[])[:15]:lines.append(str(round(r['size']/1024**2,1))+' МиБ · '+r['path'])
   for g in result.get('groups',[])[:15]:lines+=['Совпадение SHA-256: '+g['sha256'],*[x['path'] for x in g['files']]]
   if 'verified_files' in result:lines.append('Проверено файлов: '+str(result['verified_files']))
   if 'report' in result:lines+=['','Полный отчёт: '+result['report']]
  if job.get('error'):lines+=['','Операция остановлена: '+job['error']]
  self.details.delete('1.0','end');self.details.insert('1.0','\n'.join(lines))
  if choose:self.tabs.select(self.filestab);self.filepages.select(self.tasks);self.refresh();self.tree.selection_set(j)
 def approve(self):
  if not self.current:return
  try:
   j=self.engine.get(self.current)
   if j['status']!='needs_approval':raise ValueError('Это задание не ожидает подтверждения')
   records=j['payload']['records'];preview='\n'.join(x['src']+'\n → '+x['dst'] for x in records[:12])
   if len(records)>12:preview+='\n… полный список в выбранном плане'
   if messagebox.askyesno('Выполнить конкретный план?',str(len(records))+' файлов. Режим: '+j['payload']['mode']+'\n\n'+preview):self.engine.approve(self.current);self.refresh()
  except Exception as e:self.report_error(e)
 def cancel(self):
  if self.current:self.engine.cancel(self.current);self.refresh()
 def undo(self):
  if not self.current:return
  try:self.show_job(self.engine.undo(self.current))
  except Exception as e:self.report_error(e)
 def worker(self):
  if self.stop.is_set():return
  self.statusvar_safe('Проверяю восстановление. Очередь пока приостановлена.')
  while not self.stop.is_set():
   try:
    recovery=self.engine.recover();break
   except Busy:
    if self.stop.is_set():return
    self.statusvar_safe('Очередь ждёт завершения другого исполнителя…')
    if self.stop.wait(1):return
   except Exception as error:
    if not self.stop.is_set():self.statusvar_safe('Очередь приостановлена: проверка восстановления не завершилась ('+type(error).__name__+').')
    return
  else:return
  if self.stop.is_set():return
  blocked=len(recovery.get('blocked',[])) if isinstance(recovery,dict) else 0
  self.statusvar_safe(('Проверка завершена. Требуют ручной проверки квитанции: '+str(blocked)+'.') if blocked else 'Проверка восстановления завершена. Очередь готова.')
  while not self.stop.is_set():
   try:
    queued=[j for j in self.engine.jobs() if j['status']=='queued']
    if self.stop.is_set():return
    if queued:
     try:self.engine.execute(queued[-1]['id'])
     except Busy:self.stop.wait(1)
     except Exception as e:
      if not self.stop.is_set():self.statusvar_safe(str(e))
    else:self.stop.wait(1)
   except Exception:self.stop.wait(2)
 def refresh_home(self):
  try:
   s=self.engine.status();lines=['ХРАНИЛИЩЕ']
   for d in s['drives']:lines.append(d['drive']+' свободно '+str(round(d['free']/1024**3,1))+' ГиБ из '+str(round(d['total']/1024**3,1)))
   low=[d['drive'] for d in s['drives'] if d['free']<max(5*1024**3,d['total']*.05)]
   if low:lines+=['','МАЛО СВОБОДНОГО МЕСТА',', '.join(low),'Файлы → Архивы: выберите папку и подготовьте предложение архивации.']
   lines+=['','ЛОКАЛЬНЫЕ ДАННЫЕ',str(DATA_ROOT),'SQLite-базы и отчёты хранятся в этой учётной записи.',
    '', 'ОБЩИЙ КАТАЛОГ','Подключён локальный индекс' if s['catalog_exists'] else 'Не подключён — обследование папок и задания доступны без индекса',s['catalog'],
    '', 'ПРАВА И ПОДКЛЮЧЕНИЯ','Local Beta работает с обычными правами пользователя.',
    'Административное обслуживание и SYSTEM-служба в этот пакет не входят.',
    'Снимки экрана, сбор истории, Telegram и автозапуск включаются отдельно в интерфейсе.']
   lines+=['','РАЗРЕШЁННЫЕ ПАПКИ',*s['roots']]
   self.homeText.delete('1.0','end');self.homeText.insert('1.0','\n'.join(lines))
  except Exception as e:self.report_error(e)
 def search(self):
  try:
   rows=self.engine.catalog(self.query.get());self.searchresults.delete('1.0','end')
   for r in rows:self.searchresults.insert('end',str(r['id'])+' · '+r['name']+' · '+str(round(r['size']/1024,1))+' КиБ\n'+r['path']+'\n\n')
   if not rows:self.searchresults.insert('end','Совпадений нет.')
  except Exception as e:self.report_error(e)
 def tick(self):
  self.refresh()
  if self.current and self.tree.exists(self.current):self.show_job(self.current,False)
  self.refresh_home();self.after(3000,self.tick)
 def close(self):
  if getattr(self.rulespanel,'_busy',False) or self.archivepanel.is_busy():
   self.statusvar.set('Завершаю операцию с правилами или архивом перед закрытием.');self.after(400,self.close);return
  running=[j for j in self.engine.jobs() if j['status']=='running']
  if running:
   if not messagebox.askyesno('Выполняется задание','Запросить отмену и закрыть окно после завершения текущей операции?'):return
  if not self.studypanel.request_stop():return
  if running:
   for j in running:self.engine.cancel(j['id'])
   self.after(1000,self.close_when_idle);return
  self.stop.set();self.destroy()
 def close_when_idle(self):
  if getattr(self.rulespanel,'_busy',False) or self.archivepanel.is_busy() or any(j['status']=='running' for j in self.engine.jobs()):self.after(500,self.close_when_idle)
  else:self.stop.set();self.destroy()
if __name__=='__main__':App().mainloop()
