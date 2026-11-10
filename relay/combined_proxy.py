"""Authenticated loopback router: native OpenAI catalog plus private GPU API.
OpenAI credentials are never sent to the GPU gateway. No request-body logging.
"""
import asyncio,contextlib,copy,hmac,json,pathlib,time,os,re,subprocess,shutil
from functools import lru_cache
from urllib.parse import urlencode
from aiohttp import web,ClientSession,ClientTimeout,ClientError
from gateway_proxy import normalize_request
from gpu_ultra import relay_ultra
from gpu_vision import VisionClient, has_images, relay_vision
from ultra_n_relay import RESEARCH_ALIASES, load_config, relay_research, with_research_model
from claude_relay import MODELS as CLAUDE_MODELS, metadata as claude_metadata, relay_claude, run_cli
from gemini_relay import MODELS as GEMINI_MODELS, metadata as gemini_metadata, relay_gemini, run_cli as run_gemini_cli
from claude_compaction import PREFIX as CLAUDE_CHECKPOINT, read_summary
from gpu_vision import VisionError

def claude_concurrency():
 """Simultaneous Claude requests (CODEX_CLAUDE_CONCURRENCY, default 3, 1 to 8). Sub-agents share this limit."""
 try:value=int(os.environ.get('CODEX_CLAUDE_CONCURRENCY','3'))
 except ValueError:value=3
 return min(8,max(1,value))
STATE=web.AppKey('state',dict)
ROOT=pathlib.Path.home()/'.codex'
OPENAI='https://chatgpt.com/backend-api/codex'
GPU_URL_FILE='gpu-gateway.url'

# GPU gateway base URL: $CODEX_GPU_GATEWAY_URL or <root>/gpu-gateway.url (not versioned). Empty disables GPU models.
def configured_gpu(root):
 value=os.environ.get('CODEX_GPU_GATEWAY_URL','').strip()
 if not value:
  try:value=(pathlib.Path(root)/GPU_URL_FILE).read_text().strip()
  except OSError:value=''
 return value.rstrip('/')

VERSION_PATTERN=r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?'

@lru_cache(maxsize=8)
def binary_client_version(path,signature):
 # The stat signature invalidates this cache automatically after an app update.
 result=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=5,check=True)
 match=re.search(r'codex-cli\s+('+VERSION_PATTERN+r')',result.stdout)
 if not match:raise ValueError('Unrecognized Codex version output')
 return match.group(1)

def installed_client_version():
 # CODEX_CLIENT_VERSION lets CI and machines without Codex Desktop run the router.
 override=os.environ.get('CODEX_CLIENT_VERSION','').strip()
 if override and re.fullmatch(VERSION_PATTERN,override):return override
 for candidate in ('/usr/lib/chatgpt/resources/codex',shutil.which('codex')):
  if not candidate:continue
  try:
   st=pathlib.Path(candidate).stat()
   return binary_client_version(candidate,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size))
  except (OSError,ValueError,subprocess.SubprocessError):continue
 raise web.HTTPServiceUnavailable(text='Unable to determine the installed Codex version')

def request_client_version(request):
 version=request.query.get('client_version')
 if version is not None:
  if len(version)>80 or not re.fullmatch(VERSION_PATTERN,version):raise web.HTTPBadRequest(text='Invalid client_version')
  return version
 match=re.search(r'(?:codex_cli_rs|codex_vscode|codex_desktop)/('+VERSION_PATTERN+r')(?:[\s)]|$)',request.headers.get('User-Agent',''))
 return match.group(1) if match else installed_client_version()


def is_claude_checkpoint(item):
 return isinstance(item,dict) and item.get('type')=='compaction' and isinstance(item.get('encrypted_content'),str) and item['encrypted_content'].startswith(CLAUDE_CHECKPOINT)

def restore_claude_checkpoints(payload,root):
 """Replace Claude/Gemini relay checkpoints with their readable summary.

 OpenAI and the GPU gateway cannot decrypt checkpoints issued by this relay
 (invalid_encrypted_content after switching model). Returns (payload, changed).
 """
 items=payload.get('input')
 if not isinstance(items,list) or not any(is_claude_checkpoint(i) for i in items):return payload,False
 work=copy.deepcopy(payload)
 work['input']=[read_summary(i,root) if is_claude_checkpoint(i) else i for i in items]
 return work,True

def gpu_metadata(name):
 return {
  'slug':name,'display_name':'GPU Gateway — '+name,
  'description':'Modèle de la passerelle privée ; disponible seulement si chargé sur le serveur.',
  'base_instructions':'You are Codex, a coding assistant connected to a private GPU model. Help the user complete their requests. Use available tools when useful, respect configured permissions, and verify changes before claiming success. State clearly when an operation has not been tested.',
  'default_reasoning_level':'high',
  'supported_reasoning_levels':[{'effort':x,'description':x} for x in ['low','high','xhigh','max']]+[
   {'effort':'ultra','description':'Ultra personnalisé : résolution, vérification et réponse corrigée, trois passes en xhigh.'}],
  'shell_type':'unified_exec','visibility':'list','supported_in_api':True,'priority':100,
  'support_verbosity':False,'truncation_policy':{'mode':'tokens','limit':10000},
  'experimental_supported_tools':[],'input_modalities':['text','image'] if name in ('deepseek-v4.1','glm-5.3-flash') else ['text'],'context_window':1048576,
  'supports_search_tool':False,'default_reasoning_summary':'none','supports_reasoning_summary_parameter':False,
 }


def create_app(root=ROOT,openai=OPENAI,gpu=None,claude_runner=run_cli,gemini_runner=run_gemini_cli):
 if gpu is None:gpu=configured_gpu(root)
 app=web.Application(client_max_size=64*1024*1024)
 cache_dir=root/'gateway-compat';cache_dir.mkdir(parents=True,exist_ok=True)
 state={'openai':[],'gpu':[],'updated':0,'client_version':None,'lock':asyncio.Lock(),'research_lock':asyncio.Lock(),'claude_limit':asyncio.Semaphore(claude_concurrency()),'gemini_limit':asyncio.Semaphore(2),'catalog_refresh':None}
 for kind in (['openai','gpu'] if gpu else ['openai']):
  try:state[kind]=json.loads((cache_dir/(kind+'-models.json')).read_text())
  except (OSError,ValueError):pass
 if state['gpu']:
  state['gpu']=[gpu_metadata(m['slug']) for m in state['gpu'] if isinstance(m,dict) and isinstance(m.get('slug'),str)]
 app[STATE]=state
 async def lifecycle(app):
  async with ClientSession(timeout=ClientTimeout(total=None,sock_connect=10,sock_read=300),trust_env=False,auto_decompress=False) as client:
   state['client']=client
   try:yield
   finally:
    task=state['catalog_refresh']
    if task and not task.done():task.cancel()
    if task:
     with contextlib.suppress(asyncio.CancelledError,Exception):await task
 app.cleanup_ctx.append(lifecycle)

 def credentials(request):
  try:tokens=json.loads((root/'auth.json').read_text())['tokens']
  except (OSError,ValueError,KeyError):raise web.HTTPServiceUnavailable(text='Native Codex login unavailable')
  expected='Bearer '+tokens['access_token']
  if not hmac.compare_digest(request.headers.get('Authorization',''),expected):raise web.HTTPUnauthorized()
  return tokens

 async def refresh(tokens,version,force=False):
  async with state['lock']:
   if not force and state['client_version']==version and time.monotonic()-state['updated']<60:return
   async def fetch(kind,url,headers):
    try:
     async with state['client'].get(url,headers={**headers,'Accept-Encoding':'identity'},timeout=ClientTimeout(total=15),allow_redirects=False) as resp:
      if resp.status!=200:return
      data=await resp.json()
      if not isinstance(data,dict):return
      rows=data.get('models' if kind=='openai' else 'data')
      if not isinstance(rows,list) or not rows:return
      key='slug' if kind=='openai' else 'id'
      if any(not isinstance(m,dict) or not isinstance(m.get(key),str) or not m[key] for m in rows):return
      models=rows if kind=='openai' else [gpu_metadata(m['id']) for m in rows]
      state[kind]=models
      dest=cache_dir/(kind+'-models.json');tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps(models));tmp.chmod(0o600);os.replace(tmp,dest)
    except (ClientError,TimeoutError,OSError,ValueError,KeyError):pass
   jobs=[fetch('openai',openai+'/models?'+urlencode({'client_version':version}),{'Authorization':'Bearer '+tokens['access_token'],'ChatGPT-Account-Id':tokens.get('account_id',''),'User-Agent':'codex_cli_rs/'+version})]
   try:key=(root/'gateway.key').read_text().strip()
   except OSError:key=''
   if key and gpu:jobs.append(fetch('gpu',gpu+'/v1/models',{'Authorization':'Bearer '+key}))
   await asyncio.gather(*jobs)
   state['updated']=time.monotonic();state['client_version']=version

 async def handler(request):
  if request.path=='/health':return web.json_response({'status':'ok','openai_models':len(state['openai']),'gpu_models':len(state['gpu']),'claude_models':len(CLAUDE_MODELS),'gemini_models':len(GEMINI_MODELS)})
  if request.headers.get('Origin'):raise web.HTTPForbidden()
  if request.path=='/v1/responses' and request.method=='GET':
   credentials(request)
   # Codex's built-in provider probes WebSocket first. 426 tells it to use
   # HTTP streaming immediately, without consuming its reconnect attempts.
   return web.Response(status=426,text='Use HTTP POST with stream=true')
  if (request.path,request.method) not in [('/v1/models','GET'),('/v1/responses','POST'),('/v1/responses/compact','POST')]:raise web.HTTPNotFound()
  tokens=credentials(request)
  if request.path=='/v1/models':
   version=request_client_version(request)
   task=state['catalog_refresh']
   if task is None or task.done():
    if task:
     with contextlib.suppress(asyncio.CancelledError,Exception):task.result()
    task=asyncio.create_task(refresh(tokens,version));state['catalog_refresh']=task
   # Codex's catalog request times out before an offline GPU's 15s deadline.
   # Keep valid cached models visible while the refresh finishes in background.
   done,_=await asyncio.wait({task},timeout=2)
   if done:
    with contextlib.suppress(asyncio.CancelledError,Exception):task.result()
   if not state['openai']:raise web.HTTPServiceUnavailable(text='OpenAI catalog unavailable')
   native_ids={m['slug'] for m in state['openai']}
   models=with_research_model(state['openai']+[m for m in state['gpu'] if m['slug'] not in native_ids and m['slug'] not in CLAUDE_MODELS],root)
   models=[m for m in models if m['slug'] not in CLAUDE_MODELS and m['slug'] not in GEMINI_MODELS]+claude_metadata()+gemini_metadata()
   return web.json_response({'models':models})
  try:
   raw=await request.read();payload=json.loads(raw)
   if not isinstance(payload,dict):raise ValueError()
  except ValueError:raise web.HTTPBadRequest(text='Invalid JSON')
  if payload.get('model') in CLAUDE_MODELS:
   return await relay_claude(request,payload,root,state['claude_limit'],claude_runner)
  if payload.get('model') in GEMINI_MODELS:
   return await relay_gemini(request,payload,root,state['gemini_limit'],gemini_runner)
  await refresh(tokens,request_client_version(request))
  name=payload.get('model');native_ids={m['slug'] for m in state['openai']};gpu_ids={m['slug'] for m in state['gpu']}
  if name in RESEARCH_ALIASES and name not in native_ids:
   try:config=load_config(root,RESEARCH_ALIASES[name])
   except (ValueError,OSError) as exc:raise web.HTTPBadRequest(text=str(exc)) from None
   if config is None or config.model not in gpu_ids:raise web.HTTPBadRequest(text='Ultra N base model is unavailable')
   if request.path=='/v1/responses':
    try:key=(root/'gateway.key').read_text().strip()
    except OSError:raise web.HTTPServiceUnavailable(text='GPU key unavailable')
    return await relay_research(request,state['client'],gpu,{'Authorization':'Bearer '+key,'Content-Type':'application/json'},payload,root,state['research_lock'])
   payload['model']=config.model;name=config.model
   payload['reasoning']={**(payload.get('reasoning') or {}),'effort':'xhigh'}
  if name not in native_ids|gpu_ids:raise web.HTTPBadRequest(text='Model is not present in either provider catalog')
  try:payload,restored=restore_claude_checkpoints(payload,root)
  except VisionError as exc:raise web.HTTPBadRequest(text=str(exc)) from None
  is_gpu=name not in native_ids and name in gpu_ids
  if is_gpu:
   payload=normalize_request(payload)
   payload.pop('service_tier',None)
   if isinstance(payload.get('reasoning'),dict) and payload['reasoning'].get('effort')=='medium':payload['reasoning']['effort']='high'
   try:key=(root/'gateway.key').read_text().strip()
   except OSError:raise web.HTTPServiceUnavailable(text='GPU key unavailable')
   url=gpu+request.path
   headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept-Encoding':'identity'}
   vision=has_images(payload.get('input'))
   if isinstance(payload.get('reasoning'),dict) and payload['reasoning'].get('effort')=='ultra':
    if request.path=='/v1/responses':
     client=VisionClient(state['client']) if vision else state['client']
     return await relay_ultra(request,client,url,headers,payload)
    payload['reasoning']['effort']='xhigh'
   if vision and request.path=='/v1/responses':
    return await relay_vision(request,state['client'],url,headers,payload)
   raw=json.dumps(payload).encode()
  else:
   url=openai+request.path.removeprefix('/v1')
   # aiohttp has already decompressed the request body. Forward it as plain
   # JSON instead of incorrectly marking the decoded bytes as zstd/gzip.
   headers={k:v for k,v in request.headers.items() if k.lower() not in {'host','content-length','content-encoding','transfer-encoding','connection','accept-encoding','cookie','proxy-authorization'}}
   headers['Authorization']='Bearer '+tokens['access_token']
   headers['ChatGPT-Account-Id']=tokens.get('account_id','')
   headers['Accept-Encoding']='identity'
   if restored:raw=json.dumps(payload).encode()
  try:
   async with state['client'].post(url,data=raw,headers=headers,allow_redirects=False) as response:
    keep={'content-type','cache-control','x-request-id','retry-after','content-encoding'}
    out=web.StreamResponse(status=response.status,headers={k:v for k,v in response.headers.items() if k.lower() in keep})
    await out.prepare(request)
    async for chunk in response.content.iter_any():await out.write(chunk)
    await out.write_eof();return out
  except (ClientError,TimeoutError):
   if 'out' in locals() and out.prepared:
    if request.transport:request.transport.close()
    return out
   return web.json_response({'error':{'message':'Upstream provider unavailable or timed out'}},status=502)
 app.router.add_route('*','/{tail:.*}',handler)
 return app

if __name__=='__main__':web.run_app(create_app(),host='127.0.0.1',port=4012,access_log=None,print=None)
