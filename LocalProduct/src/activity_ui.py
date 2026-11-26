"""Visible controls for local, manually enabled Pasha monitoring."""
from datetime import datetime,timezone
from pathlib import Path
import math,os,queue,threading,tkinter as tk
from tkinter import ttk
def _time(value):
 if isinstance(value,dict):value=value.get('time')
 if not value:return '—'
 try:stamp=datetime.fromtimestamp(float(value)).astimezone()
 except (ValueError,TypeError,OverflowError,OSError):
  try:
   stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
   if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
   stamp=stamp.astimezone()
  except (ValueError,TypeError):return str(value)[:35]
 return stamp.strftime('%d.%m %H:%M:%S')
def _duration(value):
 try:seconds=max(0,int(float(value)))
 except (ValueError,TypeError,OverflowError):return '—'
 hours,remainder=divmod(seconds,3600);minutes,seconds=divmod(remainder,60)
 return f'{hours} ч {minutes:02} мин' if hours else f'{minutes} мин {seconds:02} с' if minutes else f'{seconds} с'
class ActivityPanel(ttk.Frame):
 def __init__(self,parent,store=None):
  super().__init__(parent,padding=12)
  if store is None:
   from activity_core import ActivityStore
   store=ActivityStore()
  self.store=store;self.closed=False;self.loading=False;self.changing=False;self.messages=queue.Queue();self.config_value={};self.state_value={};self.capture_rows={};self.capture_key=None;self.photo=None;self.timers=set()
  self.bind('<Destroy>',self._destroy,add='+');self.columnconfigure(0,weight=1);self.rowconfigure(5,weight=1)
  self.heading=tk.StringVar(value='Проверяю состояние локального агента…')
  ttk.Label(self,textvariable=self.heading,font=('Segoe UI',14,'bold'),wraplength=1010).grid(row=0,column=0,sticky='w')
  bar=ttk.Frame(self);bar.grid(row=1,column=0,sticky='ew',pady=8)
  self.toggle_button=ttk.Button(bar,text='Включить режим Паши',command=self.toggle);self.toggle_button.pack(side='left')
  self.capture_button=ttk.Button(bar,text='Снимок сейчас',command=self.capture_now,state='disabled');self.capture_button.pack(side='left',padx=7)
  ttk.Button(bar,text='Папка снимков',command=self.open_folder).pack(side='left')
  ttk.Label(bar,text='Хранить:').pack(side='left',padx=(14,4));self.retention=tk.StringVar(value='14')
  retention_box=ttk.Combobox(bar,textvariable=self.retention,width=4,values=('7','14','30'),state='readonly');retention_box.pack(side='left');retention_box.bind('<<ComboboxSelected>>',self.change_retention);ttk.Label(bar,text='дней').pack(side='left',padx=4)
  ttk.Label(self,text='Общая учётная запись: режим включается вручную и не доказывает, кто сидит за ПК. Снимки раз в 10 минут на открытом рабочем столе. Запись видна в отдельном индикаторе и доступна для паузы.',wraplength=1010,justify='left').grid(row=2,column=0,sticky='ew')
  options=ttk.Frame(self);options.grid(row=3,column=0,sticky='ew',pady=(6,5))
  self.skip_idle=tk.BooleanVar(value=False);self.titles=tk.BooleanVar(value=False);self.startup=tk.BooleanVar(value=False)
  ttk.Checkbutton(options,text='Пропускать снимки после 5 минут без ввода',variable=self.skip_idle,command=self.change_options).grid(row=0,column=0,sticky='w')
  ttk.Checkbutton(options,text='Сохранять названия окон программ',variable=self.titles,command=self.change_options).grid(row=0,column=1,sticky='w',padx=12)
  ttk.Checkbutton(options,text='Запускать агент при входе в Windows',variable=self.startup,command=self.change_startup).grid(row=1,column=0,sticky='w')
  quota_bar=ttk.Frame(options);quota_bar.grid(row=1,column=1,sticky='w',padx=12)
  ttk.Label(quota_bar,text='Лимит снимков, ГиБ:').pack(side='left');self.quota=tk.StringVar(value='5')
  quota_box=ttk.Combobox(quota_bar,textvariable=self.quota,width=4,values=('1','2','5','10','20'),state='readonly');quota_box.pack(side='left',padx=5);quota_box.bind('<<ComboboxSelected>>',self.change_quota)
  self.metrics=tk.StringVar(value='Локально · без запросов к ИИ')
  ttk.Label(self,textvariable=self.metrics,wraplength=1010).grid(row=4,column=0,sticky='ew',pady=(0,6))
  pages=ttk.Notebook(self);pages.grid(row=5,column=0,sticky='nsew')
  shots_page=ttk.Frame(pages,padding=7);programs_page=ttk.Frame(pages,padding=7);pages.add(shots_page,text='Снимки экрана');pages.add(programs_page,text='Программы и сеансы')
  shots_page.columnconfigure(0,weight=1);shots_page.columnconfigure(1,weight=1);shots_page.rowconfigure(0,weight=1)
  left=ttk.Frame(shots_page);left.grid(row=0,column=0,sticky='nsew',padx=(0,8));left.columnconfigure(0,weight=1);left.rowconfigure(0,weight=1)
  self.shots=ttk.Treeview(left,columns=('time','size'),show='headings',height=7,selectmode='browse')
  for column,title,width in (('time','Время снимка',170),('size','Размер',95)):self.shots.heading(column,text=title);self.shots.column(column,width=width)
  self.shots.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(left,orient='vertical',command=self.shots.yview);scroll.grid(row=0,column=1,sticky='ns');self.shots.configure(yscrollcommand=scroll.set)
  self.shots.bind('<<TreeviewSelect>>',self.preview_selected);self.shots.bind('<Double-1>',self.open_selected)
  ttk.Button(left,text='Открыть выбранный снимок',command=self.open_selected).grid(row=1,column=0,sticky='w',pady=5)
  self.preview=ttk.Label(shots_page,text='Выберите снимок для просмотра',anchor='center',wraplength=430);self.preview.grid(row=0,column=1,sticky='nsew')
  programs_page.columnconfigure(0,weight=1);programs_page.rowconfigure(1,weight=1)
  ttk.Label(programs_page,text='Передний план за последние 24 часа — не доказательство присутствия человека. История сайтов доступна отдельно во вкладке «Сайты и YouTube». Названия окон включаются отдельно; веб-контроль Family Safety настраивается отдельно.',wraplength=1000,justify='left').grid(row=0,column=0,sticky='ew',pady=(0,6))
  panes=ttk.Panedwindow(programs_page,orient='horizontal');panes.grid(row=1,column=0,sticky='nsew');apps_frame=ttk.Frame(panes);events_frame=ttk.Frame(panes);panes.add(apps_frame,weight=1);panes.add(events_frame,weight=2)
  for frame in (apps_frame,events_frame):frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1)
  self.apps=ttk.Treeview(apps_frame,columns=('app','duration'),show='headings',height=7)
  for column,title,width in (('app','Программа',205),('duration','Время окна',135)):self.apps.heading(column,text=title);self.apps.column(column,width=width)
  self.apps.grid(row=0,column=0,sticky='nsew',padx=(0,8))
  self.events=ttk.Treeview(events_frame,columns=('time','event'),show='headings',height=7);self.events.heading('time',text='Время');self.events.heading('event',text='Событие');self.events.column('time',width=145,stretch=False);self.events.column('event',width=330);self.events.grid(row=0,column=0,sticky='nsew')
  titles_page=ttk.Frame(pages,padding=7);pages.add(titles_page,text='Названия окон');titles_page.columnconfigure(0,weight=1);titles_page.rowconfigure(1,weight=1)
  ttk.Label(titles_page,text='Сохраняются только при включённой настройке. Заголовок окна не является точным адресом сайта.',wraplength=1000).grid(row=0,column=0,sticky='w',pady=(0,8))
  self.window_titles=ttk.Treeview(titles_page,columns=('time','window'),show='headings');self.window_titles.heading('time',text='Время');self.window_titles.heading('window',text='Программа и заголовок');self.window_titles.column('time',width=160,stretch=False);self.window_titles.column('window',width=720);self.window_titles.grid(row=1,column=0,sticky='nsew')
  self.notice=tk.StringVar(value='Режим по умолчанию выключен. Файлы остаются на ПК.');ttk.Label(self,textvariable=self.notice,wraplength=1010,justify='left').grid(row=6,column=0,sticky='ew',pady=(6,0))
  self._schedule(100,self._pump);self._poll()
 def _schedule(self,delay,callback):
  if self.closed:return
  holder={}
  def invoke():
   self.timers.discard(holder.get('id'))
   if not self.closed:callback()
  holder['id']=self.after(delay,invoke);self.timers.add(holder['id'])
 def _destroy(self,event):
  if event.widget is not self:return
  self.closed=True
  for timer in self.timers:
   try:self.after_cancel(timer)
   except tk.TclError:pass
  self.timers.clear()
 def _poll(self):
  if self.closed:return
  if not self.loading:
   self.loading=True
   def read():
    try:
     try:
      from activity_startup import startup_enabled
      startup_value=bool(startup_enabled())
     except (ImportError,OSError):startup_value=None
     self.messages.put(('snapshot',(self.store.config(),self.store.status(),self.store.recent_captures(limit=30),self.store.summary(days=1),startup_value)))
    except Exception as error:self.messages.put(('error',str(error)))
    finally:self.messages.put(('loaded',None))
   threading.Thread(target=read,daemon=True).start()
  self._schedule(3000,self._poll)
 def _pump(self):
  if self.closed:return
  try:
   while True:
    kind,value=self.messages.get_nowait()
    if kind=='snapshot':
     try:self._apply(*value)
     except Exception as error:self.notice.set('Не удалось показать журнал: '+str(error)[:250])
    elif kind=='loaded':self.loading=False
    elif kind=='changed':self.changing=False;self.notice.set(value)
    elif kind=='change_error':self.changing=False;self.notice.set('Операция не выполнена: '+value[:300])
    elif kind=='error':self.notice.set('Не удалось прочитать журнал: '+value[:300])
  except queue.Empty:pass
  self._schedule(150,self._pump)
 def _change(self,operation,notice):
  if self.changing:return
  self.changing=True
  def write():
   try:operation();self.messages.put(('changed',notice))
   except Exception as error:self.messages.put(('change_error',str(error)))
  threading.Thread(target=write,daemon=True).start()
 def toggle(self):
  enabled=not bool(self.config_value.get('enabled'))
  def apply():
   from activity_control import set_mode
   set_mode(self.store,enabled)
  self._change(apply,'Режим включён; агент применит настройку.' if enabled else 'Запрошена пауза; агент применит настройку.')
 def capture_now(self):
  if not self.config_value.get('enabled'):self.notice.set('На паузе снимки не делаются. Сначала включите режим Паши.');return
  if not self.state_value.get('running'):self.notice.set('Агент не отвечает; создание снимка не подтверждено.');return
  self._change(self.store.request_capture,'Снимок запрошен. При блокировке он будет пропущен.')
 def change_options(self):
  values=dict(skip_idle=bool(self.skip_idle.get()),record_titles=bool(self.titles.get()));self._change(lambda:self.store.set_config(**values),'Настройки записи сохранены.')
 def change_retention(self,event=None):
  try:
   days=int(self.retention.get())
   if days not in (7,14,30):raise ValueError('Выберите 7, 14 или 30 дней')
  except ValueError as error:self.notice.set(str(error));return
  self._change(lambda:self.store.set_config(retention_days=days),f'Срок хранения снимков: {days} дней.')
 def change_quota(self,event=None):
  try:
   gib=int(self.quota.get())
   if gib not in (1,2,5,10,20):raise ValueError('Выберите лимит из списка')
  except ValueError as error:self.notice.set(str(error));return
  def apply():self.store.set_config(max_storage_bytes=gib*1024**3);self.store.prune()
  self._change(apply,'Лимит собственных снимков приложения сохранён.')
 def change_startup(self):
  enabled=bool(self.startup.get())
  def apply():
   from activity_startup import set_startup_enabled
   set_startup_enabled(enabled)
  self._change(apply,'Настройка автозапуска агента сохранена.')
 def _apply(self,config,state,captures,summary,startup_value):
  self.config_value=dict(config or {});self.state_value=dict(state or {});enabled=bool(config.get('enabled'));running=bool(state.get('running'))
  if not enabled:title='Режим Паши: пауза'
  elif not running:title='Режим включён, но агент не отвечает — запись не подтверждена'
  elif not state.get('recording'):title='Ожидаю применения режима локальным агентом'
  elif state.get('locked'):title='Режим включён · рабочий стол заблокирован'
  elif config.get('skip_idle') and float(state.get('idle_seconds') or 0)>=300:title='Режим включён · снимки пропускаются из-за простоя'
  else:title='Режим Паши включён · локальный агент работает'
  self.heading.set(title);self.toggle_button.configure(text='Пауза' if enabled else 'Включить режим Паши');self.capture_button.configure(state='normal' if enabled and running else 'disabled')
  self.skip_idle.set(bool(config.get('skip_idle',False)));self.titles.set(bool(config.get('record_titles',False)));self.retention.set(str(config.get('retention_days',14)))
  if startup_value is not None:self.startup.set(startup_value)
  limit=float(config.get('max_storage_bytes',5*1024**3))/1024**3
  self.quota.set(f'{limit:g}')
  self.metrics.set('Без ИИ и расхода токенов · последний: '+_time(state.get('last_capture'))+' · следующий: '+_time(state.get('next_capture'))+f' · лимит {limit:g} ГиБ')
  if state.get('error'):self.notice.set('Агент сообщает: '+str(state['error'])[:300])
  captures=captures or [];key=tuple((str(row.get('path')),str(row.get('time')),row.get('size',0)) for row in captures)
  if key!=self.capture_key:
   previous=self._selected();previous_path=previous.get('path') if previous else None;self.capture_key=key;self.shots.delete(*self.shots.get_children());self.capture_rows.clear()
   for number,row in enumerate(captures):
    iid='shot-'+str(number);self.capture_rows[iid]=row;size=max(0,int(row.get('size') or 0))/1024**2;self.shots.insert('','end',iid=iid,values=(_time(row.get('time')),f'{size:.2f} МиБ'))
    if row.get('path')==previous_path:self.shots.selection_set(iid)
   if not captures:self.photo=None;self.preview.configure(image='',text='Снимков пока нет.')
  self.apps.delete(*self.apps.get_children())
  for row in (summary or {}).get('apps',[])[:100]:self.apps.insert('','end',values=(str(row.get('app','Неизвестно'))[:110],_duration(row.get('seconds'))))
  labels={'agent_start':'Агент запущен','agent_stop':'Агент остановлен','pasha_mode_on':'Режим включён','pasha_mode_off':'Пауза','desktop_locked':'Экран заблокирован','desktop_unlocked':'Рабочий стол открыт','resume_or_gap':'Пауза / выход из сна','recording_error':'Ошибка записи'}
  self.events.delete(*self.events.get_children())
  for row in (summary or {}).get('events',[])[:100]:
   label=labels.get(str(row.get('kind','')),'Событие сеанса');detail=str(row.get('detail') or '')
   if detail:label+=' · '+detail[:220]
   self.events.insert('','end',values=(_time(row.get('time')),label))
  self.window_titles.delete(*self.window_titles.get_children())
  for row in (summary or {}).get('titles',[])[:100]:self.window_titles.insert('','end',values=(_time(row.get('time')),str(row.get('app',''))+' — '+str(row.get('title',''))))
 def _selected(self):
  selection=self.shots.selection();return self.capture_rows.get(selection[0]) if selection else None
 def _safe_capture(self):
  row=self._selected()
  if not row:raise ValueError('Выберите снимок в списке')
  path=Path(self.store.capture_path(row['path']))
  if path.suffix.lower()!='.png' or not path.is_file():raise ValueError('Доступен только сохранённый PNG-снимок приложения')
  if path.stat().st_size>64*1024**2:raise ValueError('Снимок слишком большой')
  return path
 def preview_selected(self,event=None):
  if not self._selected():return
  try:
   path=self._safe_capture();image=tk.PhotoImage(master=self,file=str(path));width=max(260,min(650,self.preview.winfo_width()-8));height=max(140,min(360,self.preview.winfo_height()-8));factor=max(1,math.ceil(image.width()/width),math.ceil(image.height()/height));self.photo=image.subsample(factor,factor);self.preview.configure(image=self.photo,text='')
  except (OSError,ValueError,tk.TclError) as error:self.photo=None;self.preview.configure(image='',text='Не удалось открыть снимок:\n'+str(error)[:200])
 def open_selected(self,event=None):
  try:os.startfile(str(self._safe_capture()))
  except (OSError,ValueError) as error:self.notice.set('Не удалось открыть снимок: '+str(error)[:250])
 def open_folder(self):
  try:
   folder=Path(self.store.screenshots)
   if not folder.is_dir() or folder.is_symlink():raise ValueError('Папка снимков недоступна')
   os.startfile(str(folder))
  except (OSError,ValueError) as error:self.notice.set('Не удалось открыть папку: '+str(error)[:250])
