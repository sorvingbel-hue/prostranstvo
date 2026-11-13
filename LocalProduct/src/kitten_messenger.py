"""Visible child-only family messenger and a small, free local kitten helper.

Importing this module creates no storage, starts no workers and sends nothing.
Only an explicit send button passes the chosen draft to the fixed parent bridge.
The kitten defaults to local replies; an explicitly configured API is optional.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
import uuid

from cat_dialogue import CatDialogue
from family_context import ChatLog, FamilyContext
from cat_study import CatStudyMonitor


CREAM = '#fffaf0'
INK = '#40352e'
SOFT = '#f0dfce'
PEACH = '#f8cbb5'
GREEN = '#457d6a'
BLUE = '#406fa1'
QUICK_PHRASES = ('Папа, привет!', 'Я закончил уроки!', 'Позвони мне, пожалуйста.', 'Я тебя люблю!')
RECEIPTS = {
    'queued': 'В очереди на этом компьютере',
    'sending': 'Отправляется…',
    'sent': 'Принято Telegram · прочтение неизвестно',
    'uncertain': 'Результат неизвестен · не отправляй повторно',
    'failed': 'Не отправлено',
    'received': 'Получено с телефона',
    'shown': 'Получено с телефона',
}


def channel_text(channel):
    return {
        'ready': 'Telegram подключён. Можно написать папе.',
        'paused': 'Доставка сейчас на паузе. Попроси папу включить её.',
        'not_paired': 'Папе нужно закончить привязку Telegram в «Общение».',
        'not_configured': 'Папе нужно один раз подключить Telegram в «Общение».',
    }.get(channel.get('state'), 'Подключение пока недоступно. Попробуй обновить чуть позже.')


def friendly_error(error):
    """Never show arbitrary exception text, URLs, credentials or API payloads."""
    return {
        'invalid_message': 'Напиши от 1 до 4000 букв и знаков.',
        'not_configured': 'Папе нужно подключить Telegram в приложении «Пространство».',
        'not_paired': 'Папе нужно закончить привязку своего Telegram.',
        'channel_paused': 'Отправка на паузе. Попроси папу включить доставку.',
        'binding_changed': 'Настройки получателя изменились. Попроси папу проверить подключение.',
        'queue_rejected': 'Сообщение не принято. Проверь длину текста и подключение.',
    }.get(getattr(error, 'code', None), 'Не получилось проверить результат. Текст сохранён в поле; проверь переписку перед повтором.')


def kitten_reply(text):
    """Compatibility single-turn API; the visible chat keeps its own context."""
    return CatDialogue().answer(text)


def desktop_work_area(window):
    """Usable primary desktop, excluding the taskbar when Windows exposes it."""
    if os.name=='nt':
        try:
            import ctypes
            from ctypes import wintypes
            rectangle=wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48,0,ctypes.byref(rectangle),0):
                area=(rectangle.left,rectangle.top,rectangle.right,rectangle.bottom)
                if 240<=area[2]-area[0]<=32768 and 240<=area[3]-area[1]<=32768:return area
        except (OSError,AttributeError,ValueError):pass
    return (0,0,window.winfo_screenwidth(),max(240,window.winfo_screenheight()-48))


def record_for_draft(voice, transcriber, log, session_id, cancel_event, recognizing=None):
    """Pure worker: explicit finite capture, then optional offline draft text.

    Neither a recognized phrase nor a recording can reach the dialogue engine
    or family transport here. Cancellation is also passed to the native models
    so stopping before their process starts cannot activate the microphone.
    """
    if cancel_event.is_set():return {'status':'stopped'}
    receipt=dict(voice.record(seconds=8,cancel_event=cancel_event))
    result={'status':receipt.get('status'),'recording':receipt,'history_saved':True,'transcription':'unavailable'}
    if isinstance(receipt.get('path'),str) and receipt['path']:
        basename=Path(receipt['path']).name
        try:
            log.append('user','Голосовая запись сохранена локально: '+basename+'. Она не отправлена автоматически.',kind='cat',message_id='voice:'+basename,session_id=session_id)
        except Exception:result['history_saved']=False
    if cancel_event.is_set() or receipt.get('status')=='stopped':
        result['status']='stopped';return result
    if not receipt.get('path') or transcriber is None:return result
    try:
        capabilities=transcriber.status()
        if cancel_event.is_set():result['status']='stopped';return result
        if capabilities.get('available') is not True:return result
        result['transcription']='recognizing'
        if recognizing is not None:recognizing()
        answer=transcriber.transcribe(receipt['path'],cancel_event=cancel_event)
        if cancel_event.is_set():result['status']='stopped';return result
        text=answer.get('text')
        if (answer.get('status')=='transcribed' and answer.get('language')=='ru' and answer.get('local') is True
                and isinstance(text,str) and text.strip() and len(text)<=4000
                and not any(ord(char)<32 and char not in '\r\n\t' for char in text)):
            text.encode('utf-8','strict')
            result['transcription']='transcribed';result['text']=text.strip()
        elif answer.get('status') in {'no_speech','empty'} or text=='':result['transcription']='no_speech'
        else:result['transcription']='failed'
    except Exception as error:
        # A failed recognizer never hides a successful local audio receipt and
        # never exposes native paths, DLL errors or arbitrary model output.
        code=getattr(error,'code','')
        result['transcription']='no_speech' if code=='no_speech' else ('unavailable' if code in {'model_missing','worker_missing','assets_invalid'} else 'failed')
    return result


class LocalConversation:
    """Pure non-Tk model for explicit conversations and their local receipts."""
    def __init__(self, dialogue, log, session_id, study=None):
        self.dialogue, self.log, self.session_id = dialogue, log, session_id
        self.pending = None
        self.study = study
        self.pending_study = {}

    def submit(self, text, request_id):
        # A disk error must not turn a retry into a different answer or advance
        # the conversation a second time. Only this bounded pending turn is kept.
        if self.pending is None or self.pending[:2] != (request_id, text):
            self.pending = (request_id, text, self.dialogue.reply(text))
        answer = self.pending[2]
        records = []
        if text.strip():
            records.append(dict(role='user', text=text, kind='cat', message_id='local:' + request_id + ':user', session_id=self.session_id))
        records.append(dict(role='kitten', text=answer['text'], kind='cat', message_id='local:' + request_id + ':kitten', session_id=self.session_id))
        try:
            self.log.append_many(records)
        except Exception:
            if answer.get('intent') in {'self_harm','abuse','immediate_danger'}:
                return dict(answer,history_saved=False)
            raise
        self.pending = None
        return dict(answer,history_saved=True)

    def send(self, controller, text, request_id):
        saved=True
        try:self.log.append('user', text, kind='family', message_id='outgoing:' + request_id, session_id=self.session_id)
        except Exception:saved=False
        # The canonical family queue is already persistent. A broken separate
        # review log must not prevent a child from asking a parent for help.
        return dict(controller.send(text, request_id),history_saved=saved)

    def snapshot(self, controller):
        result = dict(controller.snapshot())
        records = []
        for row in result['messages']:
            incoming = row.get('direction') == 'incoming'
            source = ('incoming:' + str(row['id'])) if incoming else ('outgoing:' + str(row.get('client_id') or row['id']))
            if isinstance(row.get('text'), str) and row['text'].strip():
                records.append(dict(role='parent' if incoming else 'user', text=row['text'], kind='family', message_id=source, session_id=self.session_id))
        result['history_saved'] = True
        if records:
            try:
                self.log.append_many(records)
            except Exception:
                # The canonical family inbox already retains these messages.
                # A later refresh retries the separate log without duplicates.
                result['history_saved'] = False
        result['study'] = {'state':'idle','active':False}
        result['study_events'] = []
        if self.study is not None:
            try:
                observation=self.study.poll()
                result['study']=observation['status']
                for event in observation['events']:
                    if event.get('author')=='local_study_timer' and event.get('kind') in {'study_started','study_five_minutes','study_ended','study_cleared'}:
                        self.pending_study[event['id']]=dict(event)
                for key,event in list(self.pending_study.items())[:32]:
                    receipt=self.log.append('system',event['text'],kind='cat',message_id='study:'+key,session_id=self.session_id)
                    if receipt.get('inserted') is True:result['study_events'].append(event)
                    del self.pending_study[key]
            except Exception:
                result['study']={'state':'unavailable','active':None}
            if len(self.pending_study)>32:self.pending_study=dict(list(self.pending_study.items())[-32:])
        return result


class MessengerController:
    """Deliberate messaging over the existing fixed-recipient family transport."""
    def __init__(self, store=None, bridge=None, ensure_worker=None):
        self._store, self._bridge = store, bridge
        self._ensure_worker = ensure_worker

    def store(self):
        if self._store is None:
            from family_messages import MessageStore
            self._store = MessageStore()
        return self._store

    def bridge(self):
        if self._bridge is None:
            from family_bridge import FamilyBridge
            self._bridge = FamilyBridge(store=self.store())
        return self._bridge

    def snapshot(self):
        channel = self.bridge().channel_status()
        worker_ready = False
        if channel.get('channel_ready') is True:
            try:
                if self._ensure_worker is None:
                    from family_agent import ensure_running
                    ensure_running(self.store())
                else:
                    self._ensure_worker(self.store())
                worker_ready = True
            except Exception:
                worker_ready = False
        rows = self.store().list_messages(limit=50)
        return {'channel': channel, 'messages': list(reversed(rows)), 'worker_ready': worker_ready}

    def send(self, text, request_id):
        from family_messages import _text, _opaque
        _text(text)
        _opaque(request_id, 'отправка')
        return self.bridge().send_message(text, request_id)


class KittenCanvas(tk.Canvas):
    """Packaged kitten portrait, with an original vector fallback."""
    def __init__(self, master, **kwargs):
        super().__init__(master, bg=CREAM, highlightthickness=0, **kwargs)
        self._blink = False
        self._blink_timer = None
        self._portrait=None
        self._portrait_small=None
        self._portrait_scale=None
        self._load_portrait()
        self.bind('<Configure>', self._draw)
        self.bind('<Destroy>', self._destroy, add='+')
        self._blink_timer = self.after(3800, self._animate)

    def _load_portrait(self):
        from activity_core import no_links
        folder=Path(sys.executable).absolute().parent if getattr(sys,'frozen',False) else Path(__file__).absolute().parent
        roots=[folder]
        if folder.name=='src':roots.append(folder.parent)
        for root in roots:
            asset=root/'assets'/'kitten-portrait-v2.png'
            try:
                no_links(asset,True)
                if asset.stat().st_size>4*1024*1024:continue
                portrait=tk.PhotoImage(master=self,file=str(asset))
                if not (1<=portrait.width()<=2048 and 1<=portrait.height()<=2048):continue
                self._portrait=portrait;return
            except (OSError,ValueError,tk.TclError):continue

    def _destroy(self, event):
        if event.widget is self and self._blink_timer:
            try:
                self.after_cancel(self._blink_timer)
            except tk.TclError:
                pass
            self._blink_timer = None

    def _animate(self):
        self._blink = not self._blink
        self._draw()
        self._blink_timer = self.after(140 if self._blink else 3800, self._animate)

    def _draw(self, _event=None):
        self.delete('all')
        if self._portrait is not None:
            available=max(1,min(self.winfo_width()-4,self.winfo_height()-4))
            factor=max(1,(max(self._portrait.width(),self._portrait.height())+available-1)//available)
            if factor!=self._portrait_scale:
                self._portrait_small=self._portrait.subsample(factor,factor)
                self._portrait_scale=factor
            self.create_image(self.winfo_width()/2,self.winfo_height()/2+(1 if self._blink else 0),image=self._portrait_small)
            return
        scale = min(max(1, self.winfo_width()) / 240, max(1, self.winfo_height()) / 190)
        ox = (self.winfo_width() - 240 * scale) / 2
        oy = (self.winfo_height() - 190 * scale) / 2
        def coords(values):
            return [v * scale + (ox if i % 2 == 0 else oy) for i, v in enumerate(values)]
        def oval(*values, **style):
            return self.create_oval(*coords(values), **style)
        def polygon(*values, **style):
            return self.create_polygon(*coords(values), **style)
        def line(*values, **style):
            return self.create_line(*coords(values), width=max(1, 2 * scale), capstyle='round', **style)
        oval(47, 166, 198, 183, fill='#eadfd1', outline='')
        self.create_line(*coords((167,157,205,159,217,140,209,128,195,134)), smooth=True, fill='#e3bda0', width=16*scale, capstyle='round')
        oval(76, 100, 177, 179, fill='#f0cfae', outline='#d9ae8d', width=max(1, scale))
        oval(96, 123, 156, 175, fill='#fff2dd', outline='')
        polygon(63, 61, 66, 13, 107, 39, fill='#efcba8', outline='#d9ae8d', width=max(1, scale), smooth=True)
        polygon(137, 39, 176, 14, 184, 63, fill='#efcba8', outline='#d9ae8d', width=max(1, scale), smooth=True)
        polygon(72, 44, 72, 24, 94, 42, fill='#edb7ad', outline='', smooth=True)
        polygon(151, 43, 170, 25, 175, 48, fill='#edb7ad', outline='', smooth=True)
        oval(55, 31, 191, 135, fill='#f4d5b6', outline='#d9ae8d', width=max(1, scale))
        oval(82, 76, 163, 126, fill='#fff2dd', outline='')
        oval(65, 88, 91, 103, fill='#efc0b4', outline='')
        oval(157, 88, 181, 103, fill='#efc0b4', outline='')
        if self._blink:
            line(84, 76, 92, 81, 101, 76, fill=INK, smooth=True)
            line(144, 76, 152, 81, 161, 76, fill=INK, smooth=True)
        else:
            oval(84, 65, 104, 90, fill='#426957', outline='')
            oval(142, 65, 162, 90, fill='#426957', outline='')
            oval(90, 69, 100, 88, fill='#233c35', outline='')
            oval(148, 69, 158, 88, fill='#233c35', outline='')
            oval(87, 67, 94, 75, fill='white', outline='')
            oval(145, 67, 152, 75, fill='white', outline='')
        polygon(115, 93, 130, 93, 123, 101, fill='#b97e7b', outline='', smooth=True)
        line(123, 100, 123, 107, 115, 110, 110, 106, fill='#916c5c', smooth=True)
        line(123, 103, 128, 110, 135, 108, fill='#916c5c', smooth=True)
        for values in ((84,104,51,98),(83,111,49,115),(161,104,195,98),(162,111,197,115)):
            line(*values, fill='#bc947b')
        oval(76, 157, 119, 181, fill='#fff2dd', outline='#d9ae8d', width=max(1,scale))
        oval(133, 157, 177, 181, fill='#fff2dd', outline='#d9ae8d', width=max(1,scale))
        line(90,172,90,177,fill='#d9ae8d')
        line(102,172,102,177,fill='#d9ae8d')
        line(148,172,148,177,fill='#d9ae8d')
        line(160,172,160,177,fill='#d9ae8d')


class KittenMessenger(tk.Tk):
    def __init__(self, store=None, bridge=None, mini=False, start_background=True, controller=None, dialogue=None, chat_log=None, family_context=None, voice=None, study_monitor=None, transcriber=None, tricks_launcher=None, ai_settings=None, ai_gateway=None):
        super().__init__()
        self.title('Мурчик · Написать папе')
        self.configure(bg=CREAM)
        self.controller = controller or MessengerController(store, bridge)
        self.family_context = family_context if family_context is not None else FamilyContext()
        self.chat_log = chat_log if chat_log is not None else ChatLog()
        from cat_api import ApiSettings, ApiGateway
        from cat_brain import CatBrain
        from cat_knowledge import ApprovedKnowledge
        self._ai_settings = ai_settings if ai_settings is not None else ApiSettings()
        self._ai_gateway = ai_gateway if ai_gateway is not None else ApiGateway(self._ai_settings)
        self._ai_window = None
        self._busy_ai_status = False
        self._ai_status_after_settings = None
        self._pending_ai_mode = None
        self._intro_shown = False
        self.dialogue = dialogue if dialogue is not None else CatBrain(
            CatDialogue(family_reply=self.family_context.family_reply),
            knowledge=ApprovedKnowledge(), api=self._ai_gateway)
        self.study_monitor=study_monitor if study_monitor is not None else CatStudyMonitor()
        self.conversation = LocalConversation(self.dialogue, self.chat_log, uuid.uuid4().hex,self.study_monitor)
        self._voice = voice
        self._transcriber = transcriber
        self._transcriber_loaded = transcriber is not None
        self._tricks_launcher=tricks_launcher
        self._busy_voice = False
        self._voice_kind = None
        self._voice_superseded = False
        self._pending_auto_speak = False
        self._closing_voice = False
        self._voice_request = None
        self._voice_cancel = None
        self._voice_draft = None
        self._draft_revision = 0
        self._recognized_preview = None
        self._last_cat_text = None
        self._results = queue.Queue()
        self._closed = False
        self._busy_send = False
        self._busy_dialogue = False
        self._busy_refresh = False
        self._pump_timer = self._refresh_timer = None
        self._draft_request = None
        self._dialogue_request = None
        self._local = []
        self._rows = []
        self._channel = {}
        self._mini_position = None
        self._drag = None
        self._uncertain_text = None
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.chat = tk.Frame(self, bg=CREAM)
        self._build_chat()
        self.mini_frame = tk.Frame(self, bg=CREAM, highlightbackground=SOFT, highlightthickness=2)
        self._build_mini()
        if mini:
            self.show_mini()
        else:
            self.open_chat()
        self._pump_timer = self.after(80, self._pump)
        self.refresh_ai_status()
        if start_background:
            self.refresh()
            self._refresh_timer = self.after(4000, self._periodic)

    def _build_chat(self):
        # Only the conversation body scrolls. The editable draft and explicit
        # send/record/stop controls stay on screen even on a 720p desktop.
        self.chat.columnconfigure(0,weight=1)
        self.chat.rowconfigure(2,weight=1)
        head = self.header = tk.Frame(self.chat, bg=CREAM)
        head.grid(row=0,column=0,sticky='ew',padx=14,pady=(7,3))
        KittenCanvas(head, width=80, height=64).pack(side='left')
        caption = tk.Frame(head, bg=CREAM)
        caption.pack(side='left', fill='x', expand=True, padx=(8,0))
        tk.Label(caption, text='Мурчик', bg=CREAM, fg=INK, font=('Segoe UI',20,'bold')).pack(anchor='w')
        tk.Label(caption, text='Поговорить с котиком · написать папе', bg=CREAM, fg='#806d5e', font=('Segoe UI',9)).pack(anchor='w')
        self.channel_status = tk.StringVar(value='Проверяю подключение Telegram…')
        self.channel_label=tk.Label(self.chat, textvariable=self.channel_status, bg='#f0f2e6', fg='#53674a', justify='left', wraplength=410, padx=10, pady=5, font=('Segoe UI',9))
        self.channel_label.grid(row=1,column=0,sticky='ew',padx=14,pady=(0,5))
        self.body_frame=tk.Frame(self.chat,bg=CREAM)
        self.body_frame.grid(row=2,column=0,sticky='nsew',padx=14)
        self.body_canvas=tk.Canvas(self.body_frame,bg=CREAM,highlightthickness=0,height=180)
        body_scroll=ttk.Scrollbar(self.body_frame,command=self.body_canvas.yview)
        self.body_canvas.configure(yscrollcommand=body_scroll.set)
        body_scroll.pack(side='right',fill='y');self.body_canvas.pack(side='left',fill='both',expand=True)
        self.body=tk.Frame(self.body_canvas,bg=CREAM)
        body_window=self.body_canvas.create_window(0,0,window=self.body,anchor='nw')
        self.body.bind('<Configure>',lambda event:self.body_canvas.configure(scrollregion=self.body_canvas.bbox('all')))
        self.body_canvas.bind('<Configure>',lambda event:self.body_canvas.itemconfigure(body_window,width=event.width))
        self.body_canvas.bind('<MouseWheel>',lambda event:self.body_canvas.yview_scroll(-int(event.delta/120),'units'))
        transcript_frame = tk.Frame(self.body, bg=SOFT)
        transcript_frame.pack(fill='both', expand=True)
        self.transcript = tk.Text(transcript_frame, wrap='word', bg='white', fg=INK, font=('Segoe UI',12), relief='flat', padx=12,pady=10, height=9, state='disabled', cursor='arrow')
        scroll = ttk.Scrollbar(transcript_frame, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        self.transcript.pack(side='left',fill='both',expand=True)
        self.transcript.tag_configure('papa',foreground=BLUE,font=('Segoe UI',10,'bold'))
        self.transcript.tag_configure('self',foreground=GREEN,font=('Segoe UI',10,'bold'))
        self.transcript.tag_configure('cat',foreground='#a77456',font=('Segoe UI',10,'bold'))
        self.transcript.tag_configure('receipt',foreground='#8b8278',font=('Segoe UI',9))
        self.transcript.tag_configure('system',foreground='#677058',font=('Segoe UI',10,'bold'))
        phrases = tk.Frame(self.body,bg=CREAM)
        phrases.pack(fill='x',pady=(6,3))
        for index, phrase in enumerate(QUICK_PHRASES):
            button=tk.Button(phrases,text=phrase,command=lambda text=phrase:self.fill_phrase(text),bg='#f3e7d6',fg=INK,relief='flat',font=('Segoe UI',9),padx=5,pady=4,activebackground=PEACH)
            button.grid(row=index//2,column=index%2,sticky='ew',padx=(0,5),pady=2)
        phrases.columnconfigure(0,weight=1);phrases.columnconfigure(1,weight=1)
        self.suggestions_frame = tk.Frame(self.body, bg=CREAM)
        self.suggestions_frame.pack(fill='x',pady=(2,3))
        tk.Label(self.body,text='Беседы с Мурчиком сохраняются на этом ПК. Родители могут их посмотреть.',bg=CREAM,fg='#806d5e',wraplength=390,justify='left',font=('Segoe UI',9)).pack(fill='x',pady=(3,6))
        self.composer=tk.Frame(self.chat,bg=CREAM)
        self.composer.grid(row=3,column=0,sticky='ew',padx=14,pady=(5,0))
        self.recognized_frame=tk.Frame(self.composer,bg='#f0f2e6',padx=6,pady=4)
        tk.Label(self.recognized_frame,text='Распознано на этом ПК · проверь текст',bg='#f0f2e6',fg=INK,font=('Segoe UI',9)).pack(anchor='w')
        self.recognized_text=tk.Text(self.recognized_frame,height=2,wrap='word',font=('Segoe UI',11),relief='flat',state='disabled')
        self.recognized_text.pack(fill='x')
        preview_actions=tk.Frame(self.recognized_frame,bg='#f0f2e6');preview_actions.pack(fill='x')
        self.use_recognized_button=tk.Button(preview_actions,text='Добавить в черновик',command=self.use_recognized,bg='#f0f2e6',fg=BLUE,relief='flat',font=('Segoe UI',9))
        self.use_recognized_button.pack(side='left')
        tk.Button(preview_actions,text='Убрать',command=self.dismiss_recognized,bg='#f0f2e6',fg=BLUE,relief='flat',font=('Segoe UI',9)).pack(side='right')
        self.draft=tk.Text(self.composer,height=3,wrap='word',font=('Segoe UI',13),bg='white',fg=INK,relief='solid',borderwidth=1,padx=8,pady=5,undo=True)
        self.draft.pack(fill='x')
        self.draft.bind('<Control-Return>',self._send_key)
        self.draft.bind('<<Modified>>',self._draft_changed)
        actions=tk.Frame(self.composer,bg=CREAM)
        actions.pack(fill='x',pady=(6,3))
        self.send_button=tk.Button(actions,text='Отправить папе',command=self.send_draft,bg=GREEN,fg='white',activebackground='#356451',activeforeground='white',font=('Segoe UI',12,'bold'),relief='flat',padx=10,pady=7)
        self.send_button.pack(side='left',fill='x',expand=True)
        self.cat_button=tk.Button(actions,text='Сказать котику',command=self.talk_to_cat,bg='#f3e7d6',fg=INK,relief='flat',font=('Segoe UI',11),padx=8,pady=8)
        self.cat_button.pack(side='left',padx=(7,0))
        voicebar=tk.Frame(self.composer,bg=CREAM)
        voicebar.pack(fill='x',pady=(1,0))
        self.speak_button=tk.Button(voicebar,text='Озвучить ответ',command=self.speak_answer,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',9))
        self.speak_button.pack(side='left')
        self.microphone_button=tk.Button(voicebar,text='Микрофон · 8 с',command=self.record_voice,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',9))
        self.microphone_button.pack(side='left',padx=6)
        self.stop_voice_button=tk.Button(voicebar,text='Стоп',command=self.stop_voice,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',9))
        self.stop_voice_button.pack(side='right')
        self.auto_speak=tk.BooleanVar(value=False)
        tk.Checkbutton(self.composer,text='Озвучивать ответы котика · до закрытия',variable=self.auto_speak,bg=CREAM,fg=INK,activebackground=CREAM,font=('Segoe UI',9),anchor='w').pack(fill='x')
        self.notice=tk.StringVar(value='Папе уйдёт только текст по зелёной кнопке.')
        self.notice_label=tk.Label(self.composer,textvariable=self.notice,bg=CREAM,fg='#806d5e',wraplength=410,justify='left',font=('Segoe UI',9))
        self.notice_label.pack(fill='x',pady=(2,3))
        self.ai_status = tk.StringVar(value='Проверяю режим ответов…')
        self.ai_status_label = tk.Label(self.composer, textvariable=self.ai_status, bg='#f0f2e6', fg='#53674a',
            wraplength=390, justify='left', font=('Segoe UI',9), padx=6, pady=3)
        self.ai_status_label.pack(fill='x', pady=(0,2))
        modebar = tk.Frame(self.composer, bg=CREAM)
        modebar.pack(fill='x', pady=(0,2))
        self.ai_mode_button = tk.Button(modebar, text='ИИ', command=lambda: self.select_ai_mode(True),
            bg=CREAM, fg=INK, relief='flat', font=('Segoe UI',9))
        self.ai_mode_button.pack(side='left')
        self.offline_mode_button = tk.Button(modebar, text='Офлайн', command=lambda: self.select_ai_mode(False),
            bg=CREAM, fg=INK, relief='flat', font=('Segoe UI',9))
        self.offline_mode_button.pack(side='left', padx=5)
        self.ai_button = tk.Button(modebar, text='Настройки ИИ', command=self.open_ai,
            bg=CREAM, fg=BLUE, relief='flat', font=('Segoe UI',9))
        self.ai_button.pack(side='right')
        bottom=tk.Frame(self.composer,bg=CREAM)
        bottom.pack(fill='x',pady=(0,5))
        tk.Button(bottom,text='Котик на стол',command=self.show_mini,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',9)).pack(side='left')
        self.tricks_button=tk.Button(bottom,text='Научить трюку',command=self.open_tricks,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',9))
        self.tricks_button.pack(side='left',padx=(5,0))
        self.refresh_button=tk.Button(bottom,text='Обновить',command=self.refresh,bg=CREAM,fg=BLUE,relief='flat',font=('Segoe UI',10))
        self.refresh_button.pack(side='right')
        self.chat.bind('<Configure>',self._resize_chat)
        self._render()

    def _resize_chat(self,event):
        if event.widget is self.chat:
            width=max(220,event.width-52)
            self.channel_label.configure(wraplength=width)
            self.notice_label.configure(wraplength=width)
            self.ai_status_label.configure(wraplength=width)
            if event.height<600:self.header.grid_remove()
            else:self.header.grid()

    def _draft_changed(self,_event=None):
        if self.draft.edit_modified():
            self._draft_revision+=1
            self.draft.edit_modified(False)

    def open_tricks(self):
        if self._closed:return
        if self._busy_voice:
            self.notice.set('Сначала останови голос или дождись распознавания. Потом откроем трюки.');return
        try:
            launcher=self._tricks_launcher
            if launcher is None:
                from pet_tricks_launch import launch
                launcher=launch
            launcher()
            self.notice.set('Открываю редактор трюков в браузере на этом ПК. В режиме уроков браузер может быть закрыт.')
        except Exception:self.notice.set('Редактор трюков пока недоступен. Мурчик и переписка продолжают работать.')

    def refresh_ai_status(self):
        if self._closed or self._busy_ai_status or self._pending_ai_mode is not None:return
        self._busy_ai_status=True
        gateway=self._ai_gateway
        self._start('ai_status',gateway.status)

    def select_ai_mode(self, enabled):
        if self._closed or self._pending_ai_mode is not None:return
        if self._busy_dialogue or self._busy_send or self._busy_voice:
            self.notice.set('Дождись окончания текущего действия и выбери режим.');return
        if self._ai_window is not None:
            self.notice.set('Закрой окно настроек ИИ, чтобы выбрать режим здесь.');return
        if self._ai_status_after_settings is not None:
            self.notice.set('Дождись проверки сохранённого режима.');return
        self._pending_ai_mode = enabled
        self.ai_status.set('Сохраняю выбор режима…')
        self._enable_mode_controls(False)
        if not self._busy_ai_status:
            self._save_ai_mode()

    def _enable_mode_controls(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for button in (self.ai_mode_button, self.offline_mode_button, self.ai_button,
                       self.send_button, self.cat_button):
            button.configure(state=state)

    def _save_ai_mode(self):
        settings, enabled = self._ai_settings, self._pending_ai_mode
        self._start('ai_mode', lambda: settings.set_enabled(enabled))

    def _display_ai_status(self, result, error):
        selected = None
        if error or not isinstance(result, dict) or result.get('state') == 'settings_unavailable':
            text = 'Настройки ИИ недоступны. Проверь локальный статус.'
        elif result.get('enabled') is True:
            selected = True
            if result.get('ready') is True:
                text = 'ИИ выбран. Свободные вопросы и недавний контекст могут передаваться в OpenAI.'
            elif result.get('state') == 'key_missing':
                text = 'ИИ выбран. Ответы ждут API-ключ. Для локальной беседы выбери «Офлайн».'
            elif result.get('state') == 'privacy_required':
                text = 'ИИ выбран. Ответы ждут настройки обработки данных. Для локальной беседы выбери «Офлайн».'
            else:
                text = 'ИИ выбран. Ответы ждут подключения. Для локальной беседы выбери «Офлайн».'
        else:
            selected = False
            text = 'Офлайн выбран. Котик отвечает на этом ПК.'
        self.ai_status.set(text)
        for button, value in ((self.ai_mode_button, True), (self.offline_mode_button, False)):
            active = selected is value
            button.configure(bg=GREEN if active else CREAM, fg='white' if active else INK,
                             relief='sunken' if active else 'flat')

    def open_ai(self):
        if self._closed or self._pending_ai_mode is not None or self._ai_status_after_settings is not None:return
        if self._busy_dialogue:
            self.notice.set('Дождись текущего ответа перед изменением режима ИИ.');return
        try:
            if self._ai_window is not None and self._ai_window.winfo_exists():
                self._ai_window.lift();return
            from cat_ai_ui import open_ai_settings
            self._ai_window=open_ai_settings(self,self._ai_settings,self._ai_gateway)
            self._ai_window.bind('<Destroy>',self._ai_window_closed,add='+')
        except Exception:self.notice.set('Настройки ИИ сейчас недоступны. Для локальной беседы можно выбрать «Офлайн».')

    def _ai_window_closed(self,event):
        if event.widget is self._ai_window:
            self._ai_window=None
            if not self._closed:
                # A periodic read already in flight can predate the settings save.
                self._ai_status_after_settings = 'reread' if self._busy_ai_status else 'pending'
                self.ai_status.set('Проверяю сохранённый режим…')
                self.refresh_ai_status()

    def dismiss_recognized(self):
        self._recognized_preview=None;self.recognized_frame.pack_forget()

    def use_recognized(self):
        if self._closed or self._busy_send or self._busy_dialogue or self._voice_kind == 'record' or not self._recognized_preview:return
        current=self.draft.get('1.0','end-1c')
        addition=('\n' if current.strip() else '')+self._recognized_preview
        if len(current)+len(addition)>4000:
            self.notice.set('В черновике уже много текста. Сократи его перед добавлением записи.');return
        self.draft.insert('end-1c',addition);self.dismiss_recognized();self.draft.focus_set()
        self.notice.set('Текст добавлен в черновик. Проверь его и выбери, кому сказать. Ничего не отправлено.')

    def _build_mini(self):
        top=tk.Frame(self.mini_frame,bg=CREAM)
        top.pack(fill='x')
        tk.Label(top,text='Мурчик',bg=CREAM,fg=INK,font=('Segoe UI',10,'bold')).pack(side='left',padx=8,pady=3)
        tk.Button(top,text='×',command=self.close,bg=CREAM,fg='#8c796b',font=('Segoe UI',12),relief='flat',padx=4,pady=0).pack(side='right')
        kitten=KittenCanvas(self.mini_frame,width=164,height=124,cursor='hand2')
        kitten.pack()
        kitten.bind('<ButtonPress-1>',self._drag_start)
        kitten.bind('<B1-Motion>',self._drag_move)
        kitten.bind('<ButtonRelease-1>',self._drag_end)
        tk.Button(self.mini_frame,text='Написать папе',command=self.open_chat,bg=GREEN,fg='white',activebackground='#356451',relief='flat',font=('Segoe UI',11,'bold'),pady=6).pack(fill='x',padx=8,pady=(0,8))

    def show_mini(self):
        if self._closed:return
        # Recording must never continue behind a compact decorative pet with
        # no recording indicator or stop control.
        if self._busy_voice:
            self.stop_voice()
            self.notice.set('Останавливаю голос перед сворачиванием. Нажми ещё раз после остановки.');return
        self.chat.pack_forget()
        self.minsize(176,205)
        self.overrideredirect(True)
        self.attributes('-topmost',True)
        self.mini_frame.pack(fill='both',expand=True)
        x,y=self._mini_position or (max(0,self.winfo_screenwidth()-190),max(0,self.winfo_screenheight()-260))
        self.geometry(f'176x205+{x}+{y}')
        self.deiconify()

    def open_chat(self):
        if self._closed:return
        if self.mini_frame.winfo_ismapped():self._mini_position=(self.winfo_x(),self.winfo_y())
        self.mini_frame.pack_forget()
        self.overrideredirect(False)
        self.attributes('-topmost',False)
        self.chat.pack(fill='both',expand=True)
        left,top,right,bottom=desktop_work_area(self)
        width=min(470,max(1,right-left-24))
        # The client excludes the title bar and borders. Leave enough room for
        # those decorations and a margin inside the usable desktop rectangle.
        height=min(840,max(1,bottom-top-64))
        self.minsize(min(440,width),min(570,height))
        self.geometry(f'{width}x{height}{right-width-12:+d}{top+12:+d}')
        self.deiconify();self.lift()
        self.draft.focus_set()

    def _drag_start(self,event):
        self._drag=(event.x_root,event.y_root,self.winfo_x(),self.winfo_y(),False)

    def _drag_move(self,event):
        if self._drag:
            sx,sy,x,y,already_moved=self._drag
            dx,dy=event.x_root-sx,event.y_root-sy
            self._drag=(sx,sy,x,y,already_moved or abs(dx)+abs(dy)>5)
            self.geometry(f'+{max(0,min(self.winfo_screenwidth()-self.winfo_width(),x+dx))}+{max(0,min(self.winfo_screenheight()-self.winfo_height(),y+dy))}')

    def _drag_end(self,_event):
        moved=self._drag and self._drag[-1]
        self._drag=None
        if moved:self._mini_position=(self.winfo_x(),self.winfo_y())
        else:self.open_chat()

    def _start(self,kind,work):
        # A background thread must never keep the Tk root (or its variables)
        # alive. Otherwise its last reference can disappear on the worker and
        # Tcl will be finalized outside the creating GUI thread.
        results=self._results
        def run():
            try:results.put((kind,work(),None))
            except Exception as error:results.put((kind,None,error))
        threading.Thread(target=run,daemon=True,name='kitten-'+kind).start()

    def refresh(self):
        if self._closed or self._busy_refresh:return
        self._busy_refresh=True
        controller, conversation = self.controller, self.conversation
        self._start('snapshot',lambda:conversation.snapshot(controller))

    def _periodic(self):
        self._refresh_timer=None
        if not self._closed:
            self.refresh()
            self.refresh_ai_status()
            self._refresh_timer=self.after(4000,self._periodic)

    def fill_phrase(self,text):
        if self._busy_send or self._busy_dialogue:return
        self.draft.delete('1.0','end');self.draft.insert('1.0',text);self.draft.focus_set()
        self.notice.set('Можно изменить текст. Папе он уйдёт только по кнопке «Отправить папе».')

    def _send_key(self,_event):
        self.send_draft();return 'break'

    def send_draft(self):
        if self._closed or self._closing_voice or self._busy_send or self._busy_dialogue or self._pending_ai_mode is not None:return
        if self._voice_kind == 'record':
            self.notice.set('Сначала останови запись или дождись черновика. Потом проверь текст и отправь.');return
        text=self.draft.get('1.0','end-1c')
        try:
            from family_messages import _text
            _text(text)
        except (ValueError,TypeError):
            self.notice.set('Напиши непустое сообщение длиной до 4000 букв и знаков.');return
        if text==self._uncertain_text:
            self.notice.set('Результат этой отправки неизвестен. Сначала проверь, получил ли папа сообщение; автоматически повторять его нельзя.');return
        if self._draft_request is None or self._draft_request[0]!=text:
            self._draft_request=(text,uuid.uuid4().hex)
        request=self._draft_request[1]
        self._supersede_speech()
        self._busy_send=True
        self.send_button.configure(state='disabled');self.cat_button.configure(state='disabled')
        self.draft.configure(state='disabled')
        self.notice.set('Передаю выбранное сообщение папе…')
        controller, conversation = self.controller, self.conversation
        self._start('send',lambda:conversation.send(controller,text,request))

    def talk_to_cat(self):
        if self._closed or self._closing_voice or self._busy_send or self._busy_dialogue or self._pending_ai_mode is not None:return
        if self._ai_window is not None or self._ai_status_after_settings is not None:
            self.notice.set('Закрой настройки ИИ и дождись проверки режима перед беседой.');return
        if self._voice_kind == 'record':
            self.notice.set('Дождись распознавания или нажми «Стоп», затем проверь черновик.');return
        text=self.draft.get('1.0','end-1c')
        if len(text)>4000:self.notice.set('Для котика хватит короткой записки до 4000 знаков.');return
        if self._dialogue_request is None or self._dialogue_request[0] != text:
            self._dialogue_request = (text, uuid.uuid4().hex)
        request_id = self._dialogue_request[1]
        self._supersede_speech()
        self._busy_dialogue=True
        self.send_button.configure(state='disabled');self.cat_button.configure(state='disabled');self.draft.configure(state='disabled')
        self.notice.set('Мурчик подбирает короткий ответ…')
        conversation=self.conversation
        self._start('dialogue',lambda:conversation.submit(text,request_id))

    def _suggestions(self, rows):
        for child in self.suggestions_frame.winfo_children():child.destroy()
        for index, row in enumerate(rows[:4]):
            button=tk.Button(self.suggestions_frame,text=row['label'],command=lambda text=row['text']:self.fill_phrase(text),bg='#f3e7d6',fg=INK,relief='flat',font=('Segoe UI',9),padx=7,pady=4)
            button.grid(row=index//2,column=index%2,sticky='ew',padx=(0,4),pady=2)
        self.suggestions_frame.columnconfigure(0,weight=1);self.suggestions_frame.columnconfigure(1,weight=1)

    def _voice_model(self):
        if self._voice is None:
            from cat_voice import CatVoice
            self._voice=CatVoice()
        return self._voice

    def _speech_model(self):
        if not self._transcriber_loaded:
            self._transcriber_loaded=True
            try:
                from cat_speech import CatSpeech
                self._transcriber=CatSpeech()
            except Exception:self._transcriber=None
        return self._transcriber

    def _voice_start(self,kind):
        if self._closed or self._closing_voice or self._busy_voice or self._busy_dialogue or self._busy_send or self._pending_ai_mode is not None:return
        if kind=='record' and self._recognized_preview:
            self.notice.set('Сначала добавь предыдущий распознанный текст в черновик или нажми «Убрать».');return
        if kind=='speak' and not self._last_cat_text:
            self.notice.set('Сначала скажи что-нибудь Мурчику. Здесь можно озвучить его последний ответ.');return
        try:voice=self._voice_model()
        except Exception:
            self.notice.set('Голосовой модуль пока недоступен. Можно продолжить писать текстом.');return
        self._busy_voice=True
        self._voice_kind = kind
        self._voice_superseded = False
        request_id=uuid.uuid4().hex
        cancel_event=threading.Event()
        self._voice_request=request_id;self._voice_cancel=cancel_event
        self._draft_changed()
        self._voice_draft=(self._draft_revision,self.draft.get('1.0','end-1c'))
        self.speak_button.configure(state='disabled');self.microphone_button.configure(state='disabled')
        if kind=='speak':
            text=self._last_cat_text
            self.notice.set('Мурчик читает ответ. Кнопка «Стоп» остановит звук.')
            def speak():
                if cancel_event.is_set():return {'status':'stopped'}
                return voice.speak(text,cancel_event=cancel_event)
            self._start('voice_speak:'+request_id,speak)
        else:
            self.send_button.configure(state='disabled')
            self.cat_button.configure(state='disabled')
            self.use_recognized_button.configure(state='disabled')
            self.notice.set('Записываю микрофон до 8 секунд. Затем попробую распознать по-русски на этом ПК — только в черновик.')
            log, session_id = self.chat_log, self.conversation.session_id
            transcriber=self._speech_model()
            results=self._results
            def recognizing():results.put(('voice_stage:'+request_id,{'stage':'recognizing'},None))
            def record():
                return record_for_draft(voice,transcriber,log,session_id,cancel_event,recognizing)
            self._start('voice_record:'+request_id,record)

    def speak_answer(self):self._voice_start('speak')
    def record_voice(self):self._voice_start('record')

    def _cancel_voice(self):
        if self._voice_cancel is not None:self._voice_cancel.set()
        for model in (self._voice,self._transcriber):
            if model is not None:
                try:model.stop()
                except Exception:pass

    def _supersede_speech(self):
        self._pending_auto_speak = False
        if self._voice_kind == 'speak' and not self._voice_superseded:
            self._voice_superseded = True
            self._cancel_voice()

    def stop_voice(self):
        self._pending_auto_speak = False
        self._cancel_voice()
        if not (self._busy_send or self._busy_dialogue):
            self._voice_superseded = False
            self.notice.set('Останавливаю звук, запись или распознавание. Ничего не отправлено.')

    def _voice_finished(self,kind,result,error):
        if self._voice_cancel is not None and self._voice_cancel.is_set():
            result={'status':'stopped'};error=None
        self._busy_voice=False
        self._voice_kind = None
        self.speak_button.configure(state='normal');self.microphone_button.configure(state='normal')
        if kind == 'voice_record':
            self.send_button.configure(state='normal')
            self.cat_button.configure(state='normal')
            self.use_recognized_button.configure(state='normal')
        if self._voice_superseded:
            # The next text action owns its buttons and notice, even if the
            # old audio helper exits after that action has already finished.
            pass
        elif error:
            code=getattr(error,'code','')
            self.notice.set('Микрофон недоступен. Можно написать текстом.' if 'microphone' in code else 'Голосовая функция сейчас недоступна. Можно продолжить писать текстом.')
        elif result.get('status')=='stopped':self.notice.set('Голос остановлен. Черновик сохранён; ничего не отправлено.')
        elif kind=='voice_record':
            state=result.get('transcription')
            if state=='transcribed':
                self._draft_changed()
                current=self.draft.get('1.0','end-1c')
                revision,original=self._voice_draft
                if not original and not current and revision==self._draft_revision:
                    self.draft.insert('1.0',result['text'])
                    self.notice.set('Распознано на этом ПК и добавлено в черновик. Проверь текст. Ничего не отправлено.')
                else:
                    self._recognized_preview=result['text']
                    self.recognized_text.configure(state='normal');self.recognized_text.delete('1.0','end');self.recognized_text.insert('1.0',result['text']);self.recognized_text.configure(state='disabled')
                    self.recognized_frame.pack(fill='x',before=self.draft,pady=(0,5))
                    self.notice.set('Черновик сохранён. Распознанный текст показан отдельно — можно добавить его кнопкой.')
            elif state=='no_speech':self.notice.set('Речь не удалось разобрать. Запись осталась на этом ПК; можно написать текстом.')
            elif state=='failed':self.notice.set('Запись сохранена, но распознать её не получилось. Можно написать текстом.')
            else:self.notice.set('Запись сохранена на этом ПК. Распознавание пока недоступно: локальная модель не настроена. Можно написать текстом.')
            if result.get('history_saved') is False:self.notice.set(self.notice.get()+' Отдельную историю записи сохранить не удалось.')
        else:self.notice.set('Озвучивание завершено или остановлено.')
        self._voice_request=None;self._voice_cancel=None;self._voice_draft=None
        self._voice_superseded = False
        speak_next = self._pending_auto_speak
        self._pending_auto_speak = False
        if self._closing_voice:
            self.after(50,self.close)
        elif speak_next and self.auto_speak.get():
            self.speak_answer()

    def _pump(self):
        self._pump_timer=None
        if self._closed:return
        try:
            while True:
                kind,result,error=self._results.get_nowait()
                if kind=='snapshot':
                    self._busy_refresh=False
                    if error:self.channel_status.set('Не получилось обновить переписку. Подключение пока не подтверждено.')
                    else:
                        changed=self._rows!=result['messages']
                        self._channel=result['channel'];self._rows=result['messages']
                        self.channel_status.set(channel_text(self._channel))
                        if self._channel.get('channel_ready') and not result.get('worker_ready'):
                            self.channel_status.set('Telegram подключён. Доставка пока не запущена; сообщение может остаться в очереди.')
                        if result.get('history_saved') is False:
                            self.notice.set('Отдельную историю пока не удалось сохранить. Семейные сообщения остаются в переписке; повторю при обновлении.')
                        study=result.get('study',{})
                        if study.get('active') is True:
                            remaining=study.get('remaining_seconds',0)
                            self.channel_status.set(self.channel_status.get()+'\nУчебный таймер: ещё '+str(max(1,(int(remaining)+59)//60))+' мин. Папе можно написать.')
                        elif study.get('state')=='unavailable':
                            self.channel_status.set(self.channel_status.get()+'\nСостояние учебного таймера пока проверяется.')
                        for event in result.get('study_events',[]):
                            self._local.append(('Режим уроков',event['text']));changed=True
                        self._local=self._local[-16:]
                        if changed:self._render()
                elif kind=='ai_status':
                    self._busy_ai_status=False
                    if self._ai_status_after_settings == 'reread':
                        self._ai_status_after_settings = 'pending'
                        self.refresh_ai_status()
                    elif self._pending_ai_mode is not None:
                        self._save_ai_mode()
                    else:
                        self._ai_status_after_settings = None
                        self._display_ai_status(result,error)
                elif kind=='ai_mode':
                    self._pending_ai_mode = None
                    self._enable_mode_controls(True)
                    self._display_ai_status(result,error)
                    if error or not isinstance(result,dict):
                        self.notice.set('Не удалось сохранить выбор режима. Проверь настройки и повтори.')
                        self._ai_status_after_settings = 'pending'
                        self.refresh_ai_status()
                elif kind=='dialogue':
                    self._busy_dialogue=False
                    self.send_button.configure(state='normal');self.cat_button.configure(state='normal');self.draft.configure(state='normal')
                    if error:
                        self.notice.set('Не удалось сохранить беседу. Текст оставлен в поле — можно повторить попытку.')
                    else:
                        text=self._dialogue_request[0]
                        if text.strip():self._local.append(('Ты → Мурчику',text))
                        author = {'api_reply': 'Мурчик · внешний ИИ',
                                  'api_wait': 'Мурчик · статус ИИ',
                                  'api_fallback': 'Мурчик · статус ИИ'}.get(
                                      result.get('intent'), 'Мурчик · местный помощник')
                        self._local.append((author,result['text']))
                        self._last_cat_text=result['text']
                        self._local=self._local[-16:]
                        if result.get('history_saved') is False:
                            self.notice.set('Историю сохранить не получилось, но важный ответ показан. Текст оставлен в поле. Если нужна помощь, сразу позови безопасного взрослого рядом.')
                        else:
                            self._dialogue_request=None
                            self.draft.delete('1.0','end')
                            self.notice.set('Беседа сохранена на этом ПК. Папе ничего не отправлено автоматически.')
                        self._suggestions(result.get('suggestions',[]));self._render()
                        self.refresh_ai_status()
                        if self.auto_speak.get() and result.get('history_saved') is not False:
                            if self._voice_kind == 'speak':
                                self._pending_auto_speak = True
                            else:
                                self.speak_answer()
                elif kind.startswith('voice_'):
                    action,_,request_id=kind.partition(':')
                    if request_id!=self._voice_request:continue
                    if action=='voice_stage':
                        if self._voice_cancel is not None and not self._voice_cancel.is_set():
                            self.notice.set('Распознаю по-русски на этом ПК. Текст появится только в черновике; отправки не будет.')
                    else:self._voice_finished(action,result,error)
                else:
                    self._busy_send=False
                    self.send_button.configure(state='normal');self.cat_button.configure(state='normal');self.draft.configure(state='normal')
                    if error:self.notice.set(friendly_error(error))
                    else:
                        state=result.get('status')
                        if state=='uncertain':self._uncertain_text=self._draft_request[0]
                        self.notice.set(RECEIPTS.get(state,'Сообщение принято локально. Результат доставки пока неизвестен.'))
                        if result.get('history_saved') is False:self.notice.set(self.notice.get()+' Отдельная история временно недоступна; сообщение сохраняется в семейной очереди.')
                        self.draft.delete('1.0','end');self._draft_request=None
                        self.refresh()
        except queue.Empty:pass
        self._pump_timer=self.after(80,self._pump)

    def _render(self):
        self.transcript.configure(state='normal');self.transcript.delete('1.0','end')
        self.transcript.insert('end','Мурчик\n','cat')
        self.transcript.insert('end','Привет! Здесь можно написать папе или поболтать со мной по-русски. Я компьютерный помощник в образе котёнка. Без внешнего ИИ мои разговоры ограничены. Беседы сохраняются на этом ПК и доступны родителям. Режим ответов указан под полем ввода.\n\n')
        for row in self._rows:
            incoming=row.get('direction')=='incoming'
            self.transcript.insert('end','Папа\n' if incoming else 'Ты → папе\n','papa' if incoming else 'self')
            self.transcript.insert('end',str(row.get('text',''))+'\n')
            self.transcript.insert('end',RECEIPTS.get(row.get('status'),'Статус неизвестен')+'\n\n','receipt')
        for author,text in self._local:
            self.transcript.insert('end',author+'\n','system' if author=='Режим уроков' else 'cat')
            self.transcript.insert('end',text+'\n\n')
        self.transcript.configure(state='disabled');self.transcript.see('end')

    def show_intro(self):
        if self._closed:
            return
        self.open_chat()
        if self._intro_shown:
            return
        self._intro_shown = True
        name = ''
        try:
            name = self.family_context.profile()['child']['name'] or ''
        except (AttributeError, KeyError, OSError, ValueError):
            pass
        greeting = f'Привет, {name}!' if name else 'Привет!'
        self._local.append(('Мурчик · приветствие',
            greeting + ' Я Мурчик. Хочешь рассказать, как прошёл день, или поговорить о Roblox?'))
        self._local = self._local[-16:]
        self._render()

    def close(self):
        if self._closed:return
        if self._ai_window is not None and self._ai_window.saving:
            self.notice.set('Дождись сохранения настроек ИИ перед закрытием.');return
        if self._pending_ai_mode is not None:
            self.notice.set('Дождись сохранения режима перед закрытием.');return
        if self._busy_send or self._busy_dialogue:
            self.notice.set('Дождись результата текущей отправки перед закрытием.');return
        if self._busy_voice:
            self._closing_voice=True;self.stop_voice();return
        if self._voice is not None:
            try:self._voice.stop()
            except Exception:pass
        self._closed=True
        for timer in (self._pump_timer,self._refresh_timer):
            if timer:
                try:self.after_cancel(timer)
                except tk.TclError:pass
        self.destroy()


class _InstanceSignal:
    """One visible messenger per Windows user/session/store, with bring-to-front."""
    def __init__(self,root):
        self.root=Path(root).absolute();self.kernel=None;self.mutex=self.event=self.greet_event=None;self.owner=False

    def acquire(self, greet=False):
        if os.name!='nt':return True
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p];kernel.CreateMutexW.restype=ctypes.c_void_p
        kernel.CreateEventW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_wchar_p];kernel.CreateEventW.restype=ctypes.c_void_p
        kernel.OpenEventW.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_wchar_p];kernel.OpenEventW.restype=ctypes.c_void_p
        kernel.SetEvent.argtypes=[ctypes.c_void_p];kernel.SetEvent.restype=ctypes.c_int
        kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.ReleaseMutex.argtypes=[ctypes.c_void_p]
        key=hashlib.sha256(os.path.normcase(str(self.root)).encode('utf-8')).hexdigest()
        name='Local\\Prostranstvo-Kitten-'+key
        ctypes.set_last_error(0)
        mutex=kernel.CreateMutexW(None,True,name)
        if not mutex:raise RuntimeError('Messenger instance unavailable')
        if ctypes.get_last_error()==183:
            event=kernel.OpenEventW(2,False,name+('-greet' if greet else '-show'))
            if not event and greet:
                event=kernel.OpenEventW(2,False,name+'-show')
            if event:kernel.SetEvent(event);kernel.CloseHandle(event)
            kernel.CloseHandle(mutex);return False
        event=kernel.CreateEventW(None,False,False,name+'-show')
        greet_event=kernel.CreateEventW(None,False,False,name+'-greet')
        if not event or not greet_event:
            kernel.ReleaseMutex(mutex)
            for handle in (event,greet_event,mutex):
                if handle:kernel.CloseHandle(handle)
            raise RuntimeError('Messenger signal unavailable')
        self.kernel,self.mutex,self.event,self.greet_event,self.owner=kernel,mutex,event,greet_event,True
        return True

    def requested(self):
        return self.kernel is not None and self.kernel.WaitForSingleObject(self.event,0)==0

    def greeting_requested(self):
        return self.kernel is not None and self.kernel.WaitForSingleObject(self.greet_event,0)==0

    def close(self):
        if self.kernel:
            if self.owner:self.kernel.ReleaseMutex(self.mutex)
            for handle in (self.event,self.greet_event,self.mutex):
                if handle:self.kernel.CloseHandle(handle)
            self.kernel=None


def main(argv=None):
    parser=argparse.ArgumentParser(description='Мурчик · семейные сообщения')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--mini',action='store_true')
    mode.add_argument('--greet',action='store_true')
    args=parser.parse_args(argv)
    from family_messages import default_root
    instance=_InstanceSignal(default_root())
    try:
        if not instance.acquire(greet=args.greet):return 0
        window=KittenMessenger(mini=args.mini)
        if args.greet:
            window.show_intro()
        def signals():
            if window._closed:return
            greet=instance.greeting_requested()
            show=instance.requested()
            if greet:window.show_intro()
            elif show:window.open_chat()
            window.after(250,signals)
        window.after(250,signals)
        window.mainloop()
    finally:instance.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
