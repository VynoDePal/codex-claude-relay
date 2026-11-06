"""Codex-facing adapter for the bounded Ultra N documentary research engine."""
import asyncio
import contextlib
import copy
import json
import time
import uuid
from pathlib import Path

from aiohttp import ClientTimeout, web
from gpu_vision import response_events

ALIAS = 'deepseek-v4.1-ultra-n'
RESEARCH_ALIASES = {ALIAS:'deepseek-v4.1', 'glm-5.3-flash-ultra-n':'glm-5.3-flash'}
MODEL_LABELS = {'deepseek-v4.1':'DeepSeek', 'glm-5.3-flash':'GLM 5.3 Flash'}


def config_path(root,model='deepseek-v4.1'):
 if model not in MODEL_LABELS:raise ValueError('Unsupported Ultra N model')
 return Path(root)/'ultra-n'/('config.json' if model=='deepseek-v4.1' else model+'.json')


def load_config(root,model='deepseek-v4.1'):
 from ultra_n_engine import ResearchConfig
 path=config_path(root,model)
 if not path.exists():return None
 try:
  raw=json.loads(path.read_text())
  if not isinstance(raw,dict):raise ValueError('Expected a configuration object')
  config=ResearchConfig.from_dict(raw)
  if config.model!=model:raise ValueError('Configuration model does not match the selected model')
  return config
 except (ValueError,TypeError) as exc:
  raise ValueError('Invalid Ultra N configuration: '+str(exc)) from None


def research_metadata(base,config):
 result=copy.deepcopy(base)
 alias=next(alias for alias,model in RESEARCH_ALIASES.items() if model==config.model)
 result.update(slug=alias,display_name='GPU Gateway — '+MODEL_LABELS[config.model]+' Ultra N',
  description=f'Recherche documentaire ; 0 à {config.max_agents} sous-agents, {config.concurrency} simultanés, tous en xhigh.',
  default_reasoning_level='xhigh',supported_reasoning_levels=[{'effort':'xhigh','description':'Raisonnement xhigh pour le superviseur et chaque sous-agent.'}],
  input_modalities=['text'],priority=101,visibility='hide',
  base_instructions='This model runs a bounded documentary research service. The user submits a self-contained research question. The service searches public publications, reads sources, delegates to researchers as useful, and returns a cited report with limitations. It does not execute Codex client tools or modify external systems.')
 return result


def with_research_model(models,root):
 models=[m for m in models if m.get('slug') not in RESEARCH_ALIASES]
 for model in RESEARCH_ALIASES.values():
  try:config=load_config(root,model)
  except (ValueError,OSError):continue
  if config is None:continue
  base=next((m for m in models if m.get('slug')==model),None)
  if base:models.append(research_metadata(base,config))
 return models


def query_from_payload(payload):
 if payload.get('previous_response_id') or payload.get('conversation') or payload.get('background'):
  raise ValueError('Ultra N needs a foreground request with a self-contained research question')
 items=payload.get('input')
 if isinstance(items,str):query=items
 elif isinstance(items,list):
  message=next((m for m in reversed(items) if isinstance(m,dict) and m.get('role')=='user'),None)
  if not message:raise ValueError('A user research question is required')
  content=message.get('content','')
  if isinstance(content,str):query=content
  elif isinstance(content,list):
   if any(not isinstance(p,dict) or p.get('type') not in ('input_text','text') for p in content):
    raise ValueError('Ultra N accepts text research questions; use the regular GPU model for images')
   if any(not isinstance(p.get('text'),str) for p in content):
    raise ValueError('Each text part must contain a text string')
   query='\n'.join(p['text'] for p in content)
  else:raise ValueError('Invalid research question content')
 else:raise ValueError('A user research question is required')
 query=query.strip()
 if not query or len(query)>16000:raise ValueError('Research question must contain 1 to 16000 characters')
 return query


class Gateway:
 def __init__(self,client,gpu,headers,model,timeout_seconds=120):
  self.client=client;self.url=gpu.rstrip('/')+'/v1/chat/completions'
  self.headers={k:v for k,v in headers.items() if k.lower() in ('authorization','content-type')}
  self.headers['Accept-Encoding']='identity';self.model=model
  self.timeout_seconds=timeout_seconds
 async def complete(self,messages,tools,max_tokens):
  payload={'model':self.model,'messages':messages,'reasoning_effort':'xhigh','max_tokens':max_tokens,'stream':False}
  if tools:payload.update(tools=tools,tool_choice='auto')
  else:payload['tool_choice']='none'
  async with self.client.post(self.url,headers=self.headers,json=payload,
    timeout=ClientTimeout(total=self.timeout_seconds+30,sock_connect=10,sock_read=self.timeout_seconds),allow_redirects=False) as response:
   if response.status!=200:raise RuntimeError('GPU gateway returned HTTP '+str(response.status))
   body=bytearray()
   async for chunk in response.content.iter_any():
    body.extend(chunk)
    if len(body)>16*1024*1024:raise RuntimeError('GPU response exceeded the size limit')
   try:result=json.loads(body)
   except (ValueError,UnicodeError):raise RuntimeError('GPU gateway returned invalid JSON') from None
   if not isinstance(result,dict) or not result.get('choices'):raise RuntimeError('GPU gateway returned no completion')
   return result


async def relay_research(request,client,gpu,headers,payload,root,run_lock):
 from research_sources import PublicSources
 from ultra_n_engine import run_research
 try:
  alias=payload.get('model')
  model=RESEARCH_ALIASES.get(alias)
  if model is None:raise ValueError('Unknown Ultra N model')
  config=load_config(root,model)
  if config is None:raise ValueError('Ultra N is not configured')
  query=query_from_payload(payload)
 except (ValueError,OSError) as exc:raise web.HTTPBadRequest(text=str(exc)) from None
 if run_lock.locked():raise web.HTTPConflict(text='An Ultra N research run is already active. Wait or cancel it first.')
 # asyncio.Lock.acquire() does not yield on its unlocked fast path.
 await run_lock.acquire()
 streaming=payload.get('stream') is True;out=None;task=None;write_lock=asyncio.Lock()
 run_id=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
 run_dir=Path(root)/'ultra-n/runs'/run_id
 async def emit(data):
  async with write_lock:await out.write(data)
 async def progress(event):
  if streaming:await emit(b': ultra-n '+json.dumps(event,ensure_ascii=False).encode()+b'\n\n')
 async def pipeline():
  async with PublicSources() as sources:
   return await run_research(query,config,Gateway(client,gpu,headers,config.model,timeout_seconds=config.model_timeout_seconds),sources,run_dir,progress)
 try:
  if streaming:
   out=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-store','X-Accel-Buffering':'no'})
   await out.prepare(request);await emit(b': ultra-n connected\n\n')
  task=asyncio.create_task(pipeline());heartbeat=time.monotonic()
  while not task.done():
   await asyncio.wait({task},timeout=0.5)
   if request.transport is None or request.transport.is_closing():raise ConnectionResetError()
   if streaming and time.monotonic()-heartbeat>=10:
    await emit(b': ultra-n working\n\n');heartbeat=time.monotonic()
  result=await task
  metrics=result.get('metrics',{});report=result['report']
  summary=(f"\n\n---\n**Ultra N** — {metrics.get('agents_created',0)}/{config.max_agents} sous-agents créés ; "
   f"{len(result.get('sources',[]))} sources consultées ; statut : `{result['status']}`.\n"
   f"[Rapport et sources locaux]({run_dir/'report.md'})")
  status=result['status']
  if status!='completed':report='> Recherche partielle ou interrompue. Les limites ci-dessous font partie du résultat.\n\n'+report
  response={'id':'resp_'+uuid.uuid4().hex,'object':'response','created_at':int(time.time()),'model':alias,'status':'completed',
   'output':[{'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','phase':'final_answer','status':'completed',
    'content':[{'type':'output_text','text':report+summary,'annotations':[]}]}],
   'usage':{'input_tokens':metrics.get('input_tokens',0),'output_tokens':metrics.get('output_tokens',0),'total_tokens':metrics.get('total_tokens',0)},
   'reasoning':{'effort':'xhigh'},'metadata':{'research_run_id':run_id,'research_status':status,'max_agents':str(config.max_agents),'usage_complete':str(metrics.get('usage_complete',False)).lower()},
   'error':None,'incomplete_details':None,'store':False}
  if streaming:
   for event in response_events(response):await emit(event)
   await out.write_eof();return out
  return web.json_response(response)
 except ConnectionError:return out if out is not None else web.Response(status=499)
 except Exception as exc:
  error={'type':'error','code':'ultra_n_failed','message':'Ultra N could not complete its research ('+type(exc).__name__+').','param':None}
  if streaming:
   with contextlib.suppress(ConnectionError):await emit(b'data: '+json.dumps(error).encode()+b'\n\n');await out.write_eof()
   return out
  return web.json_response({'error':error},status=502)
 finally:
  if task:
   task.cancel()
   with contextlib.suppress(asyncio.CancelledError,Exception):await task
  run_lock.release()
