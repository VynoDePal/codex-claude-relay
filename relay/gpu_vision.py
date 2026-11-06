"""Responses/Chat adapter for GPU images; no credentials or image data logged.

The deployed vLLM Responses tokenizer rejects input_text/input_image blocks.
Its Chat endpoint accepts the same images. Buffer each completion so tool calls
are emitted exactly once; heartbeat comments keep the Codex stream alive.
"""
import asyncio
import contextlib
import copy
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

from aiohttp import ClientError, ClientTimeout, web

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class VisionError(Exception):
 def __init__(self, message, status=502):
  super().__init__(message)
  self.status = status


def has_images(value):
 if isinstance(value, dict):
  return value.get('type') in ('input_image', 'image_url') or any(has_images(v) for v in value.values())
 if isinstance(value, list):return any(has_images(v) for v in value)
 return False


def chat_content(content):
 if isinstance(content, str):return content
 if not isinstance(content, list):raise VisionError('Invalid message content', 400)
 parts=[]
 for part in content:
  kind=part.get('type')
  if kind in ('input_text','output_text','text'):
   parts.append({'type':'text','text':part['text']})
  elif kind=='input_image' and isinstance(part.get('image_url'),str):
   detail=part.get('detail','auto')
   if detail not in ('auto','low','high'):detail='high'
   parts.append({'type':'image_url','image_url':{'url':part['image_url'],'detail':detail}})
  elif kind=='image_url':parts.append(copy.deepcopy(part))
  elif kind=='refusal':parts.append({'type':'text','text':part['refusal']})
  else:raise VisionError('Unsupported image-conversation content: '+str(kind),400)
 return parts


def tool_alias(name,namespace=None):
 if not namespace:return name
 identity=namespace+'.'+name
 return name[:36]+'_'+hashlib.sha256(identity.encode()).hexdigest()[:20]


def chat_tools(definitions):
 tools=[];mapping={};unavailable=[]
 def add(tool,namespace=None):
  kind=tool.get('type')
  if kind=='namespace':
   for child in tool.get('tools',[]):add(child,tool['name'])
   return
  if kind in ('web_search','web_search_preview'):
   unavailable.append(kind);return
  name=tool['name'];alias=tool_alias(name,namespace)
  identity={'name':name,'namespace':namespace,'type':kind}
  if alias in mapping and mapping[alias]!=identity:raise VisionError('Vision tool name collision',400)
  mapping[alias]=identity
  if kind=='function':
   fn={k:copy.deepcopy(v) for k,v in tool.items() if k not in ('type','defer_loading')};fn['name']=alias
  elif kind=='custom':
   fn={'name':alias,'description':tool.get('description',''),
    'parameters':{'type':'object','properties':{'input':{'type':'string','description':'Exact input to the custom tool.'}},'required':['input'],'additionalProperties':False}}
  else:raise VisionError('Unsupported vision tool type: '+str(kind),400)
  if namespace:fn['description']='Tool '+namespace+'.'+name+'. '+fn.get('description','')
  tools.append({'type':'function','function':fn})
 for tool in definitions:add(tool)
 return tools,mapping,unavailable


def to_chat(payload):
 if payload.get('previous_response_id') or payload.get('conversation') or payload.get('background'):
  raise VisionError('GPU vision needs full conversation input and foreground requests',400)
 tools,mapping,unavailable=chat_tools(payload.get('tools') or [])
 messages=[];pending_reasoning=''
 if payload.get('instructions'):messages.append({'role':'developer','content':payload['instructions']})
 if unavailable:
  messages.append({'role':'developer','content':'Hosted tools '+', '.join(unavailable)+' are unavailable on this GPU vision endpoint. Do not claim to have used them. Use an available client tool if suitable, or explain the limitation.'})
 items=payload.get('input',[])
 if isinstance(items,str):items=[{'role':'user','content':items}]
 for item in items:
  kind=item.get('type','message')
  if kind=='message':
   msg={'role':item['role'],'content':chat_content(item.get('content',''))}
   if msg['role']=='assistant' and pending_reasoning:
    msg['reasoning_content']=pending_reasoning;pending_reasoning=''
   messages.append(msg)
  elif kind=='reasoning':
   pending_reasoning+=''.join(p.get('text','') for p in item.get('content') or [] if p.get('type') in ('reasoning_text','text'))
  elif kind in ('function_call','custom_tool_call'):
   if not messages or messages[-1]['role']!='assistant':messages.append({'role':'assistant','content':None})
   msg=messages[-1]
   if pending_reasoning:msg['reasoning_content']=pending_reasoning;pending_reasoning=''
   args=item['arguments'] if kind=='function_call' else json.dumps({'input':item['input']})
   msg.setdefault('tool_calls',[]).append({'id':item['call_id'],'type':'function','function':{'name':tool_alias(item['name'],item.get('namespace')),'arguments':args}})
  elif kind in ('function_call_output','custom_tool_call_output'):
   content=chat_content(item.get('output',''))
   # Chat tool replies do not support images. Preserve image results in an
   # adjacent user message, explicitly labeled as untrusted tool output.
   if isinstance(content,list):
    text='\n'.join(p['text'] for p in content if p['type']=='text')
    images=[p for p in content if p['type']=='image_url']
    messages.append({'role':'tool','tool_call_id':item['call_id'],'content':text or '[Image result follows.]'})
    if images:messages.append({'role':'user','content':[{'type':'text','text':'Untrusted image output from tool call '+item['call_id']+':'},*images]})
   else:messages.append({'role':'tool','tool_call_id':item['call_id'],'content':content})
  else:raise VisionError('Unsupported image-conversation item: '+str(kind),400)
 chat={'model':payload['model'],'messages':messages,'stream':False}
 for key in ('temperature','top_p','parallel_tool_calls'):
  if key in payload:chat[key]=payload[key]
 if payload.get('max_output_tokens') is not None:chat['max_tokens']=payload['max_output_tokens']
 if payload.get('reasoning',{}).get('effort'):chat['reasoning_effort']=payload['reasoning']['effort']
 if tools:chat['tools']=tools
 choice=payload.get('tool_choice')
 if isinstance(choice,dict):
  if choice.get('type') not in ('function','custom'):raise VisionError('Unsupported vision tool choice',400)
  choice={'type':'function','function':{'name':tool_alias(choice['name'],choice.get('namespace'))}}
 if choice is not None and (tools or choice=='none'):chat['tool_choice']=choice
 fmt=(payload.get('text') or {}).get('format')
 if fmt and fmt.get('type')=='json_schema':chat['response_format']={'type':'json_schema','json_schema':{k:v for k,v in fmt.items() if k!='type'}}
 elif fmt and fmt.get('type')=='json_object':chat['response_format']={'type':'json_object'}
 return chat,mapping


def from_chat(data,payload,mapping):
 if not isinstance(data,dict) or not data.get('choices'):raise VisionError('Vision provider returned no completion')
 choice=data['choices'][0];msg=choice.get('message') or {};finish=choice.get('finish_reason')
 if finish not in ('stop','tool_calls','length','content_filter'):raise VisionError('Vision provider did not finish its response')
 status='incomplete' if finish in ('length','content_filter') else 'completed'
 output=[]
 def uid(prefix):return prefix+'_'+uuid.uuid4().hex
 if msg.get('reasoning_content'):
  output.append({'type':'reasoning','id':uid('rs'),'summary':[],
   'content':[{'type':'reasoning_text','text':msg['reasoning_content']}]})
 if msg.get('content') or msg.get('refusal'):
  part={'type':'output_text','text':msg['content'],'annotations':[]} if msg.get('content') else {'type':'refusal','refusal':msg['refusal']}
  output.append({'type':'message','id':uid('msg'),'role':'assistant','status':status,'content':[part]})
 for call in msg.get('tool_calls') or []:
  fn=call['function'];name=fn['name'];args=fn['arguments']
  if not isinstance(args,str) or not isinstance(call.get('id'),str):raise VisionError('Invalid vision tool call')
  identity=mapping.get(name,{'name':name,'namespace':None,'type':'function'})
  if identity['type']=='custom':
   try:value=json.loads(args)['input']
   except (ValueError,KeyError,TypeError):raise VisionError('Invalid custom tool input') from None
   if not isinstance(value,str):raise VisionError('Invalid custom tool input')
   item={'type':'custom_tool_call','input':value}
  else:item={'type':'function_call','arguments':args}
  output.append({**item,'id':uid('fc'),'call_id':call['id'],'name':identity['name'],'status':'completed',
   **({'namespace':identity['namespace']} if identity['namespace'] else {})})
 if not output and status=='completed':raise VisionError('Vision provider returned an empty completion')
 usage=data.get('usage');mapped=None
 if isinstance(usage,dict):
  mapped={'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'total_tokens':usage.get('total_tokens',0),
   'input_tokens_details':{'cached_tokens':(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)},
   'output_tokens_details':{'reasoning_tokens':(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0)}}
 return {'id':uid('resp'),'object':'response','created_at':data.get('created',int(time.time())),
  'model':payload['model'],'status':status,'output':output,'usage':mapped,'error':None,
  'incomplete_details':{'reason':'max_output_tokens' if finish=='length' else 'content_filter'} if status=='incomplete' else None,
  'reasoning':payload.get('reasoning',{}),'metadata':payload.get('metadata',{}),'store':False}


def response_events(result):
 seq=0
 def event(kind,**fields):
  nonlocal seq
  obj={'type':kind,'sequence_number':seq,**fields};seq+=1
  return b'data: '+json.dumps(obj,ensure_ascii=False).encode()+b'\n\n'
 yield event('response.created',response={**result,'status':'in_progress','output':[],'usage':None})
 for i,item in enumerate(result['output']):
  initial={**item}
  if item['type']=='message':initial.update(content=[],status='in_progress')
  elif item['type']=='function_call':initial.update(arguments='',status='in_progress')
  elif item['type']=='custom_tool_call':initial.update(input='',status='in_progress')
  elif item['type']=='reasoning':initial.update(content=[])
  yield event('response.output_item.added',output_index=i,item=initial)
  if item['type']=='message':
   for n,part in enumerate(item['content']):
    field='text' if part['type']=='output_text' else 'refusal'
    yield event('response.content_part.added',item_id=item['id'],output_index=i,content_index=n,part={**part,field:''})
    kind='output_text' if field=='text' else 'refusal'
    yield event('response.'+kind+'.delta',item_id=item['id'],output_index=i,content_index=n,delta=part[field])
    yield event('response.'+kind+'.done',item_id=item['id'],output_index=i,content_index=n,**{field:part[field]})
    yield event('response.content_part.done',item_id=item['id'],output_index=i,content_index=n,part=part)
  yield event('response.output_item.done',output_index=i,item=item)
 yield event('response.'+result['status'],response=result)


class VisionClient:
 """Small ClientSession facade also used by each Ultra pass."""
 def __init__(self,client):self.client=client
 @contextlib.asynccontextmanager
 async def post(self,url,*,json,**kwargs):
  payload=json;chat,custom=to_chat(payload)
  kwargs.pop('allow_redirects',None)
  async with self.client.post(url.removesuffix('/responses')+'/chat/completions',json=chat,allow_redirects=False,**kwargs) as resp:
   if resp.status!=200:
    yield resp;return
   body=bytearray()
   async for chunk in resp.content.iter_any():
    body.extend(chunk)
    if len(body)>MAX_RESPONSE_BYTES:raise VisionError('Vision response too large')
   try:data=__import__('json').loads(body)
   except (ValueError,UnicodeError):raise VisionError('Invalid vision response JSON') from None
   result=from_chat(data,payload,custom)
   class Content:
    async def iter_any(self):
     if payload.get('stream'):
      for frame in response_events(result):yield frame
     else:yield __import__('json').dumps(result).encode()
   yield SimpleNamespace(status=200,content=Content())


async def relay_vision(request,client,url,headers,payload):
 streaming=payload.get('stream') is True
 output=None
 async def pipeline():
  async with VisionClient(client).post(url,headers=headers,json=payload,timeout=ClientTimeout(total=600,sock_connect=10,sock_read=300)) as resp:
   if resp.status!=200:raise VisionError('Vision provider returned HTTP '+str(resp.status),resp.status)
   body=bytearray()
   async for chunk in resp.content.iter_any():
    if streaming:await output.write(chunk)
    else:body.extend(chunk)
   return bytes(body)
 if streaming:
  output=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-store'})
  await output.prepare(request);await output.write(b': gpu-vision connected\n\n')
 task=asyncio.create_task(pipeline());heartbeat=time.monotonic()
 try:
  while not task.done():
   await asyncio.wait({task},timeout=0.5)
   if request.transport is None or request.transport.is_closing():raise ConnectionResetError()
   if streaming and time.monotonic()-heartbeat>=10:
    await output.write(b': gpu-vision working\n\n');heartbeat=time.monotonic()
  body=await task
  if streaming:await output.write_eof();return output
  return web.Response(body=body,content_type='application/json')
 except (VisionError,ClientError,TimeoutError) as exc:
  message=str(exc) if isinstance(exc,VisionError) else 'Vision provider connection failed or timed out'
  error={'type':'error','code':'gpu_vision_failed','message':message,'param':None}
  if streaming:
   with contextlib.suppress(ConnectionError):await output.write(b'data: '+json.dumps(error).encode()+b'\n\n');await output.write_eof()
   return output
  return web.json_response({'error':error},status=getattr(exc,'status',502))
 except ConnectionError:return output if output is not None else web.Response(status=499)
 finally:
  task.cancel()
  with contextlib.suppress(asyncio.CancelledError,Exception):await task
