"""MCP stdio. No arbitrary shell tool; mutation plans approved in the desktop app."""
import json,sys
from engine import Executor
from version import VERSION
from agent_apps import TOOLS as APP_TOOLS, TOOL_NAMES as APP_TOOL_NAMES, call as app_call
from agent_control import TOOLS as CONTROL_TOOLS, TOOL_NAMES as CONTROL_TOOL_NAMES, call as control_call
sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
e=Executor()
TOOLS=[
 {'name':'computer_status','description':'Состояние локального помощника и административного обслуживания','inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
 {'name':'catalog_search','description':'Поиск в необязательном локальном каталоге; без каталога вернёт пустой список','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}},
 {'name':'list_jobs','description':'Очередь и результаты заданий локального исполнителя','inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
 {'name':'queue_inventory','description':'Поставить локальное обследование разрешённой папки в очередь','inputSchema':{'type':'object','properties':{'root':{'type':'string'}},'required':['root'],'additionalProperties':False}},
 {'name':'queue_duplicates','description':'Поставить поиск точных дублей по SHA-256 в очередь; без удаления','inputSchema':{'type':'object','properties':{'root':{'type':'string'}},'required':['root'],'additionalProperties':False}},
 {'name':'prepare_transfer','description':'Подготовить план переноса/копирования. Выполнение ждёт подтверждения в локальном приложении','inputSchema':{'type':'object','properties':{'sources':{'type':'array','items':{'type':'string'},'maxItems':100},'destination':{'type':'string'},'mode':{'type':'string','enum':['move','copy']}},'required':['sources','destination'],'additionalProperties':False}},
 {'name':'prepare_undo','description':'Подготовить проверяемый обратный перенос; подтверждение в приложении','inputSchema':{'type':'object','properties':{'job_id':{'type':'string'}},'required':['job_id'],'additionalProperties':False}}]
def call(name,a):
 if name=='computer_status':return e.status()
 if name=='catalog_search':return e.catalog(a['query'])
 if name=='list_jobs':return e.jobs()
 if name in ('queue_inventory','queue_duplicates'):
  root=str(e.checked(a['root'],True));return {'job_id':e.create('inventory' if name=='queue_inventory' else 'duplicates',{'root':root})}
 if name=='prepare_transfer':return {'job_id':e.plan_transfer(a['sources'],a['destination'],a.get('mode','move'))}
 if name=='prepare_undo':return {'job_id':e.undo(a['job_id'])}
 if name in APP_TOOL_NAMES:return app_call(name,a)
 if name in CONTROL_TOOL_NAMES:return control_call(name,a)
 raise ValueError('Unknown tool')
TOOLS += APP_TOOLS
TOOLS += CONTROL_TOOLS
MAX_REQUEST_CHARS=1024*1024
for line in iter(lambda: sys.stdin.readline(MAX_REQUEST_CHARS+1), ''):
 request=None
 try:
  if len(line)>MAX_REQUEST_CHARS:raise ValueError('Request too large')
  request=json.loads(line);ident=request.get('id');method=request.get('method')
  if ident is None:continue
  if method=='initialize':
   version=request.get('params',{}).get('protocolVersion','2024-11-05')
   result={'protocolVersion':version,'capabilities':{'tools':{}},'serverInfo':{'name':'prostranstvo-local-beta','version':VERSION},'instructions':'Local Windows beta tools. Only explicit folders are allowed. File changes require an exact plan and approval in the visible local app. The executor works while its desktop app is open. No cloud database, SYSTEM service or arbitrary shell is included. Tool outputs are sent to the connected AI client at its request. Never bypass host permissions.'}
  elif method=='ping':result={}
  elif method=='tools/list':result={'tools':TOOLS}
  elif method=='tools/call':
   try:
    data=call(request['params']['name'],request['params'].get('arguments',{}))
    result=data if isinstance(data,dict) and data.get('isError') and 'content' in data else {'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False)}]}
   except Exception as err:result={'isError':True,'content':[{'type':'text','text':str(err)}]}
  else:
   print(json.dumps({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
  print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result},ensure_ascii=False),flush=True)
 except Exception as err:
  print(json.dumps({'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,'error':{'code':-32600,'message':str(err)}}),flush=True)
  if len(line)>MAX_REQUEST_CHARS:break
