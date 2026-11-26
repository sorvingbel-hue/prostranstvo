"""Bounded parent-only board-command ledger; no network, GUI, PIN or Study API.

The transport MUST authenticate the pinned private chat/user before accept_message.
Only a sole family worker calls recover_inflight/process_pending outside its
Telegram settings lock. Replies go through the existing idempotent local outbox.
Import and construction are inert; same-Windows-user code is not isolated here.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from activity_core import exclusive_lock, no_links

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_COMMANDS, MAX_TOKENS, MAX_REPLIES = 2048, 512, 4096
MAX_PLAN_BYTES = 24 * 1024
HASH = re.compile(r'[a-f0-9]{64}\Z')
IDENT = re.compile(r'[a-f0-9]{32}\Z')
CODE = re.compile(r'[A-Za-z0-9_-]{32,64}\Z', re.ASCII)
APPROVE = re.compile(r'/approve[ \t]+([A-Za-z0-9_-]{32,64})(?:[ \t]+([^\r\n]+))?\Z', re.ASCII|re.IGNORECASE)
RESERVED = re.compile(r'^/(?:plan|approve)(?=$|[\s@])', re.ASCII|re.IGNORECASE)
STATES = {'issued', 'queued', 'applying', 'accepted', 'approved', 'rejected',
          'uncertain', 'expired', 'invalidated'}
RESULTS = {None, 'accepted', 'approved', 'rejected', 'stale_plan', 'expired',
           'binding_inactive', 'plan_unavailable', 'outcome_unknown', 'comment_required'}


class BoardTelegramError(ValueError):
    """Fixed technical codes only; never format callback exceptions or tokens."""


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _id(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _integer(value, lower=0, upper=2**63-1):
    return type(value) is int and lower <= value <= upper


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _date(value):
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BoardTelegramError('ledger_invalid')
        result[key] = value
    return result


def _bounded(value, depth=0, budget=None):
    budget = [1000] if budget is None else budget
    budget[0] -= 1
    if depth > 10 or budget[0] < 0:
        raise BoardTelegramError('plan_invalid')
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is str:
        if len(value) > 4000 or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise BoardTelegramError('plan_invalid')
        value.encode('utf-8', 'strict')
        return
    if type(value) is list:
        for entry in value:
            _bounded(entry, depth+1, budget)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for key, entry in value.items():
            _bounded(key, depth+1, budget)
            _bounded(entry, depth+1, budget)
        return
    raise BoardTelegramError('plan_invalid')


def _plan(value):
    if type(value) is not dict:
        raise BoardTelegramError('plan_unavailable')
    _bounded(value)
    if len(_json(value).encode('utf-8')) > MAX_PLAN_BYTES:
        raise BoardTelegramError('plan_invalid')
    if (not _date(value.get('date')) or not _integer(value.get('revision'), 1, 2**128-1)
            or value.get('status') not in ('open', 'submitted', 'approved')
            or not _integer(value.get('game_minutes'), 5, 120)):
        raise BoardTelegramError('plan_invalid')
    tasks = value.get('tasks')
    if type(tasks) is not list or not 1 <= len(tasks) <= 10:
        raise BoardTelegramError('plan_invalid')
    for task in tasks:
        if (type(task) is not dict or type(task.get('text')) is not str
                or not 1 <= len(task['text'].strip()) <= 120 or not task['text'].isprintable()
                or task.get('status') not in ('pending', 'help', 'done')):
            raise BoardTelegramError('plan_invalid')
    if value.get('status') in ('submitted', 'approved') and any(t['status'] != 'done' for t in tasks):
        raise BoardTelegramError('plan_invalid')
    linked = value.get('study_session_id')
    if linked is not None and not _id(linked, IDENT):
        raise BoardTelegramError('plan_invalid')
    return deepcopy(value)


def _command_fingerprint(fingerprint,quality,comment):
    return _hash(_json({'plan':fingerprint,'quality_percent':quality,'comment':comment}))


def _approval_tail(tail):
    """Optional percentage then one-line explanation; never treat 101 as 100%."""
    tail='' if tail is None else tail.strip()
    if not tail:
        return 100,''
    parts=re.split(r'[ \t]+',tail,maxsplit=1)
    first=parts[0]
    numeric=re.match(r'[+\-−]?(?:\d|\.\d)',first)
    if numeric:
        if not re.fullmatch(r'0|[1-9][0-9]?|100',first,re.ASCII):
            return None
        quality=int(first)
        comment=parts[1].strip() if len(parts)>1 else ''
    else:
        quality,comment=100,tail
    if len(comment)>240 or (comment and not comment.isprintable()):
        return None
    return quality,comment


def binding_authorized(settings,store,expected_binding):
    """Fresh local boolean gate, no network/queue writes or credential output.

    Call outside the board ledger lock. The passed MessageStore must already
    exist; this helper never constructs a store or binds an unbound queue.
    """
    if not _id(expected_binding,HASH):
        return False
    try:
        with settings._lock:
            data=settings._load()
            if data.get('enabled') is not True:
                return False
            pinned,paired,bot=data.get('pinned'),data.get('paired'),data.get('bot')
            if (type(pinned) is not dict or set(pinned)!={'bot_id','chat_id','user_id'}
                    or any(not _integer(v,1) for v in pinned.values())
                    or type(paired) is not dict or type(bot) is not dict
                    or pinned['chat_id']!=pinned['user_id']
                    or not _integer(paired.get('chat_id'),1)
                    or not _integer(paired.get('user_id'),1)
                    or not _integer(bot.get('id'),1)
                    or paired.get('chat_id')!=pinned['chat_id']
                    or paired.get('user_id')!=pinned['user_id']
                    or bot.get('id')!=pinned['bot_id']):
                return False
            settings._configured(data)  # Validate only; never return or log token.
            actual=_hash(_json(pinned))
            stored=store.get_binding()
            return (_id(stored,HASH) and hmac.compare_digest(expected_binding,actual)
                    and hmac.compare_digest(expected_binding,stored))
    except Exception:
        return False


def _validate(state):
    if type(state) is not dict or set(state) != {'version','tokens','commands','replies'} or type(state['version']) is not int or state['version'] != 1:
        raise BoardTelegramError('ledger_invalid')
    for key, maximum in (('tokens',MAX_TOKENS),('commands',MAX_COMMANDS),('replies',MAX_REPLIES)):
        if type(state[key]) is not list or len(state[key]) > maximum:
            raise BoardTelegramError('ledger_invalid')
    ids, hashes, command_ids, reply_ids = set(), set(), set(), set()
    for token in state['tokens']:
        # Read old v1 tokens without writing on status. They never contained
        # a comment; low-quality legacy requests fail closed before dispatch.
        if type(token) is dict and 'comment' not in token:
            legacy_keys={'request_id','token_hash','binding','date','revision','study_id',
                'fingerprint','issued_utc','expires_utc','state','quality_percent',
                'command_fingerprint','result_code'}
            if set(token)==legacy_keys:
                quality=token['quality_percent']
                if quality is not None:
                    if token['command_fingerprint']!=_hash(str(token['fingerprint'])+':'+str(quality)):
                        raise BoardTelegramError('ledger_invalid')
                    token['command_fingerprint']=_command_fingerprint(token['fingerprint'],quality,'')
                token['comment']=''
        if (type(token) is not dict or set(token) != {'request_id','token_hash','binding','date','revision','study_id',
                'fingerprint','issued_utc','expires_utc','state','quality_percent','comment','command_fingerprint','result_code'}
                or not _id(token['request_id'],IDENT) or token['request_id'] in ids
                or not _id(token['token_hash'],HASH) or token['token_hash'] in hashes
                or not _id(token['binding'],HASH) or not _date(token['date'])
                or not _integer(token['revision'],1,2**128-1)
                or (token['study_id'] is not None and not _id(token['study_id'],IDENT))
                or not _id(token['fingerprint'],HASH)
                or not _number(token['issued_utc']) or token['issued_utc'] <= 0
                or not _number(token['expires_utc']) or not 1 <= token['expires_utc']-token['issued_utc'] <= 3600
                or token['state'] not in STATES or token['result_code'] not in RESULTS
                or (token['quality_percent'] is not None and not _integer(token['quality_percent'],0,100))
                or type(token['comment']) is not str or len(token['comment'])>240
                or token['comment']!=token['comment'].strip()
                or (token['comment'] and not token['comment'].isprintable())
                or (token['command_fingerprint'] is not None and not _id(token['command_fingerprint'],HASH))):
            raise BoardTelegramError('ledger_invalid')
        quality = token['quality_percent']
        if ((quality is None) != (token['command_fingerprint'] is None)
                or (quality is not None and token['command_fingerprint'] != _command_fingerprint(token['fingerprint'],quality,token['comment']))
                or (token['state'] in ('queued','applying','accepted','approved','uncertain') and quality is None)):
            raise BoardTelegramError('ledger_invalid')
        ids.add(token['request_id']); hashes.add(token['token_hash'])
    for reply in state['replies']:
        if (type(reply) is not dict or set(reply) != {'id','binding','client_id','text','state'}
                or not _id(reply['id'],IDENT) or reply['id'] in reply_ids
                or reply['client_id'] != 'board:'+reply['id'] or not _id(reply['binding'],HASH)
                or type(reply['text']) is not str or not 1 <= len(reply['text']) <= 4000
                or any(ord(c) < 32 and c not in '\n\t' for c in reply['text'])
                or len(reply['text'].encode('utf-8','strict')) > 16000
                or reply['state'] not in ('pending','queued')):
            raise BoardTelegramError('ledger_invalid')
        reply_ids.add(reply['id'])
    for command in state['commands']:
        if (type(command) is not dict or set(command) != {'binding','update_id','message_id','command_hash','reply_id'}
                or not _id(command['binding'],HASH) or not _integer(command['update_id'])
                or not _integer(command['message_id'],1) or not _id(command['command_hash'],HASH)
                or command['reply_id'] not in reply_ids):
            raise BoardTelegramError('ledger_invalid')
        key = command['binding'],command['update_id']
        if key in command_ids:
            raise BoardTelegramError('ledger_invalid')
        command_ids.add(key)
    return state


class BoardTelegram:
    def __init__(self, root, get_plan, *, clock=time.time, today=date.today, token_ttl=900, nonce=None):
        if not callable(get_plan) or type(token_ttl) is not int or not 1 <= token_ttl <= 3600:
            raise ValueError('invalid_options')
        self.root = Path(root).absolute()
        self.path = self.root/'telegram-board.json'
        self.get_plan, self.clock, self.today, self.token_ttl = get_plan, clock, today, token_ttl
        self.nonce = nonce or (lambda: secrets.token_urlsafe(24))

    def _now(self):
        value = self.clock()
        if not _number(value) or value <= 0:
            raise BoardTelegramError('clock_unavailable')
        return value

    def _read(self):
        try:
            with no_links(self.path,True).open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES+1)
        except FileNotFoundError:
            return {'version':1,'tokens':[],'commands':[],'replies':[]}
        if len(raw) > MAX_FILE_BYTES:
            raise BoardTelegramError('ledger_too_large')
        try:
            state = json.loads(raw.decode('utf-8','strict'),object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(BoardTelegramError('ledger_invalid')))
            return _validate(state)
        except (ValueError,UnicodeError,RecursionError,TypeError,KeyError):
            raise BoardTelegramError('ledger_invalid') from None

    def _write(self,state):
        _validate(state)
        data = _json(state).encode('utf-8')
        if len(data) > MAX_FILE_BYTES:
            raise BoardTelegramError('ledger_too_large')
        descriptor,name = tempfile.mkstemp(prefix='.telegram-board-',suffix='.tmp',dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            no_links(self.path,self.path.exists())
            os.replace(temporary,self.path)
        finally:
            if temporary.exists():
                no_links(temporary,True).unlink()

    @contextmanager
    def _change(self):
        no_links(self.root).mkdir(parents=True,exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root/'telegram-board.lock'):
            state = self._read(); before=deepcopy(state)
            yield state
            if state != before:
                self._write(state)

    @staticmethod
    def recognizes(text):
        return type(text) is str and RESERVED.match(text.lstrip()) is not None

    @staticmethod
    def _reply(state,binding,text):
        if len(state['replies']) >= MAX_REPLIES:
            raise BoardTelegramError('ledger_full')
        ident=secrets.token_hex(16)
        if any(r['id']==ident for r in state['replies']):
            raise BoardTelegramError('identifier_collision')
        state['replies'].append({'id':ident,'binding':binding,'client_id':'board:'+ident,
                                 'text':text,'state':'pending'})
        return ident

    @staticmethod
    def _valid_token_time(token,now):
        return token['issued_utc'] <= now < token['expires_utc']

    def _current(self):
        value = _plan(self.get_plan())
        if value['date'] != self.today().isoformat():
            raise BoardTelegramError('plan_unavailable')
        return value

    def _issue(self,state,binding,now):
        try:
            plan = self._current()
        except Exception:
            return 'Не удалось получить сегодняшний план. Проверьте доску на компьютере.'
        lines=['План на '+plan['date']+'.']
        marks={'pending':'[ ]','help':'[?]','done':'[+]'}
        lines += [str(index+1)+'. '+marks[task['status']]+' '+task['text'] for index,task in enumerate(plan['tasks'])]
        lines += ['Игровое время по плану: '+str(plan['game_minutes'])+' мин.']
        if plan['status'] != 'submitted':
            if plan['status']=='approved':
                return '\n'.join(lines+['План уже одобрен. Новый игровой час этой командой не выдаётся.'])
            return '\n'.join(lines+['План ещё не отправлен на проверку. Сначала нужно отметить дела и отправить список.'])
        fingerprint=_hash(_json(plan))
        for token in state['tokens']:
            if token['binding']==binding and token['fingerprint']==fingerprint:
                if token['state'] in ('queued','applying','accepted','approved'):
                    return 'Одобрение этого плана уже принято. Дождитесь результата; новый игровой час не создаётся.'
                if token['state']=='uncertain':
                    return 'Результат предыдущего одобрения неизвестен. Нужна проверка на компьютере; автоматического повтора нет.'
                if token['state']=='issued':
                    token['state']='invalidated'
        if len(state['tokens'])>=MAX_TOKENS:
            raise BoardTelegramError('ledger_full')
        code=self.nonce()
        if not _id(code,CODE) or any(t['token_hash']==_hash(code) for t in state['tokens']):
            raise BoardTelegramError('identifier_collision')
        request_id=secrets.token_hex(16)
        if any(t['request_id']==request_id for t in state['tokens']):
            raise BoardTelegramError('identifier_collision')
        state['tokens'].append({'request_id':request_id,'token_hash':_hash(code),'binding':binding,
            'date':plan['date'],'revision':plan['revision'],'study_id':plan.get('study_session_id'),
            'fingerprint':fingerprint,'issued_utc':now,'expires_utc':now+self.token_ttl,'state':'issued',
            'quality_percent':None,'comment':'','command_fingerprint':None,'result_code':None})
        lines += ['Список отправлен на проверку.',
                  'Одобрить: /approve '+code,
                  'При согласованных критериях: /approve КОД 85 Что осталось доделать.',
                  'Для оценки ниже 100 обязательно пояснение; максимум 240 символов.',
                  'Код одноразовый, действует '+str(self.token_ttl//60)+' мин. Только для этого плана.']
        return '\n'.join(lines)

    def _approve_request(self,state,binding,match,now):
        parsed=_approval_tail(match.group(2))
        if parsed is None:
            return 'Неверная оценка или пояснение. Используйте /approve КОД [0–100] [пояснение до 240 символов]. Команда не выполнена.'
        code,(quality,comment)=match.group(1),parsed
        if quality<100 and not comment:
            return 'Для оценки ниже 100 напишите, что осталось доделать: /approve КОД 85 пояснение. Команда не принята.'
        wanted=_hash(code)
        token=next((t for t in state['tokens'] if hmac.compare_digest(t['token_hash'],wanted)),None)
        if token is None or token['binding']!=binding:
            return 'Код не найден или относится к другому подключению. Запросите /plan.'
        if token['state']!='issued':
            return ('Результат одобрения неизвестен; проверьте приложение. Повтор автоматически не выполняется.'
                    if token['state']=='uncertain' else 'Этот код уже использован или отменён. Новый игровой час не создаётся.')
        if not self._valid_token_time(token,now):
            token.update(state='expired',result_code='expired')
            return 'Срок кода истёк или часы изменились. Запросите свежий /plan.'
        try:
            current=self._current()
        except Exception:
            return 'Не удалось проверить план. Команда не принята; проверьте приложение.'
        if current['status']!='submitted' or _hash(_json(current))!=token['fingerprint']:
            token.update(state='rejected',result_code='stale_plan')
            return 'План изменился или уже проверен. Старый код не действует; запросите /plan.'
        token.update(state='queued',quality_percent=quality,comment=comment,
                     command_fingerprint=_command_fingerprint(token['fingerprint'],quality,comment))
        # No callback or Study action runs inside the authenticated poll lock.
        return 'Запрос одобрения принят в локальную очередь. Результат и начало игрового времени ещё не подтверждены.'

    def accept_message(self,text,update_id,message_id,binding_id,forwarded=False):
        """Durable-only. Caller already verified the exact pinned private sender."""
        if not self.recognizes(text):
            return {'handled':False}
        if (not _id(binding_id,HASH) or not _integer(update_id) or not _integer(message_id,1)
                or type(forwarded) is not bool or len(text)>4000
                or any(ord(c)<32 and c not in '\n\t' for c in text)):
            raise BoardTelegramError('invalid_command_envelope')
        command_hash=_hash(_json({'text':text,'forwarded':forwarded}))
        with self._change() as state:
            old=next((c for c in state['commands'] if c['binding']==binding_id and c['update_id']==update_id),None)
            if old is not None:
                if old['command_hash']!=command_hash or old['message_id']!=message_id:
                    raise BoardTelegramError('update_identity_changed')
                return {'handled':True,'duplicate':True,'reply_id':old['reply_id']}
            if len(state['commands'])>=MAX_COMMANDS:
                raise BoardTelegramError('ledger_full')
            now=self._now()
            for token in state['tokens']:
                if token['binding']!=binding_id and token['state'] in ('issued','queued'):
                    token.update(state='invalidated',result_code='binding_inactive')
            stripped=text.strip()
            match=APPROVE.fullmatch(stripped)
            if forwarded:
                reply='Пересланные команды не выполняются. Напишите /plan самостоятельно в этом личном чате.'
            elif stripped.lower()=='/plan':
                reply=self._issue(state,binding_id,now)
            elif match:
                reply=self._approve_request(state,binding_id,match,now)
            else:
                reply='Используйте /plan или /approve КОД [0–100] [пояснение до 240 символов]. Для оценки ниже 100 пояснение обязательно. Команда не выполнена.'
            ident=self._reply(state,binding_id,reply)
            state['commands'].append({'binding':binding_id,'update_id':update_id,'message_id':message_id,
                                       'command_hash':command_hash,'reply_id':ident})
        return {'handled':True,'duplicate':False,'reply_id':ident}

    def recover_inflight(self):
        """Call once ONLY while holding the sole family-worker OS lock."""
        count=0
        with self._change() as state:
            for token in state['tokens']:
                if token['state']=='applying':
                    token.update(state='uncertain',result_code='outcome_unknown')
                    self._reply(state,token['binding'],'Результат прерванного одобрения неизвестен. Проверьте приложение; автоматического повтора нет.')
                    count+=1
        return count

    def _finish(self,request_id,state_name,code):
        messages={
            'accepted':'Родительское одобрение сохранено. Применение ограничений и начало игрового времени ещё не подтверждены.',
            'approved':'План одобрен. Состояние игрового таймера и ограничений проверьте в приложении.',
            'rejected':'Одобрение не применено: план, критерии или подключение изменились. Проверьте приложение.',
            'expired':'Срок одобрения истёк или часы изменились. Запросите /plan.',
            'uncertain':'Результат одобрения неизвестен. Проверьте приложение; автоматического повтора нет.',
        }
        with self._change() as state:
            token=next((t for t in state['tokens'] if t['request_id']==request_id),None)
            if token is None or token['state']!='applying':
                raise BoardTelegramError('dispatch_identity_changed')
            token.update(state=state_name,result_code=code)
            self._reply(state,token['binding'],messages[state_name])
        return {'processed':True,'request_id':request_id,'state':state_name,'result_code':code}

    def process_pending(self,approve,is_authorized=None):
        """At most one callback, outside file/settings locks; no retry of unknowns."""
        if not callable(approve) or (is_authorized is not None and not callable(is_authorized)):
            raise ValueError('invalid_dispatch_callback')
        with self._change() as state:
            token=next((t for t in state['tokens'] if t['state']=='queued'),None)
            if token is None:
                return {'processed':False}
            token['state']='applying'
            item=deepcopy(token)
        request=item['request_id']
        if not self._valid_token_time(item,self._now()):
            return self._finish(request,'expired','expired')
        if item['quality_percent']<100 and not item['comment']:
            return self._finish(request,'rejected','comment_required')
        try:
            authorized=is_authorized(item['binding']) if is_authorized is not None else False
        except Exception:
            authorized=False
        if authorized is not True:
            return self._finish(request,'rejected','binding_inactive')
        try:
            current=self._current()
        except Exception:
            return self._finish(request,'rejected','plan_unavailable')
        if current['status']!='submitted' or _hash(_json(current))!=item['fingerprint']:
            return self._finish(request,'rejected','stale_plan')
        # A slow authorization or board read must not stretch the token TTL.
        if not self._valid_token_time(item,self._now()):
            return self._finish(request,'expired','expired')
        try:
            result=approve(date=item['date'],revision=item['revision'],study_id=item['study_id'],
                           request_id=request,quality_percent=item['quality_percent'],comment=item['comment'])
            status=result.get('state',result.get('status')) if type(result) is dict else None
        except Exception:
            return self._finish(request,'uncertain','outcome_unknown')
        if status in ('approved','playing'):
            return self._finish(request,'approved','approved')
        if status in ('accepted','queued','pending'):
            return self._finish(request,'accepted','accepted')
        if status in ('rejected','conflict','stale_plan','denied'):
            return self._finish(request,'rejected','rejected')
        return self._finish(request,'uncertain','outcome_unknown')

    def flush_replies(self,enqueue_reply,binding_id,limit=10):
        """Queue replies with stable IDs, never hold ledger lock over the callback.

        Callback must use TelegramSettings.queue_reply on the exact current
        pinned channel. Retrying this LOCAL enqueue is safe by client_id;
        Telegram delivery/uncertain retry stays entirely in the old outbox.
        """
        if not callable(enqueue_reply) or not _id(binding_id,HASH) or not _integer(limit,1,50):
            raise ValueError('invalid_reply_callback')
        pending=[r for r in self._read()['replies'] if r['state']=='pending' and r['binding']==binding_id][:limit]
        queued=0
        for row in pending:
            try:
                receipt=enqueue_reply(row['text'],client_id=row['client_id'])
            except Exception:
                break
            if type(receipt) is not dict or receipt.get('status') not in ('queued','sending','sent','uncertain','failed'):
                break
            with self._change() as state:
                current=next((r for r in state['replies'] if r['id']==row['id']),None)
                if current is None or current['binding']!=binding_id or current['text']!=row['text']:
                    raise BoardTelegramError('reply_identity_changed')
                current['state']='queued'
            queued+=1
        return {'queued_locally':queued,'delivery_confirmed':False,'read_confirmed':False}

    def status(self):
        state=self._read()
        return {'version':1,'commands':len(state['commands']),'tokens':len(state['tokens']),
                'pending_approvals':sum(t['state']=='queued' for t in state['tokens']),
                'inflight':sum(t['state']=='applying' for t in state['tokens']),
                'uncertain':sum(t['state']=='uncertain' for t in state['tokens']),
                'pending_replies':sum(r['state']=='pending' for r in state['replies']),
                'direct_network_or_study_api':False}
