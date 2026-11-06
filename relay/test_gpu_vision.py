"""Regression: image requests must reach the vision API without losing tools."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from combined_proxy import create_app

IMAGE = 'data:image/png;base64,TEST'

class VisionTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp = tempfile.TemporaryDirectory()
  root = Path(self.temp.name)
  (root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'native'}}))
  (root/'gateway.key').write_text('gpu')
  self.seen=[]
  self.answer={'role':'assistant','content':'A red square and a blue circle.'}
  self.finish='stop'
  self.status=200
  async def backend(r):
   if r.path=='/models':return web.json_response({'models':[{'slug':'gpt-test','input_modalities':['text','image']}]})
   if r.path=='/v1/models':return web.json_response({'data':[{'id':x} for x in ['deepseek-v4.1','actif','glm-5.3-flash']]})
   p=await r.json();self.seen.append((r.path,p,dict(r.headers)))
   if r.path!='/v1/chat/completions':return web.json_response({'error':{'message':"got input_text"}},status=400)
   if self.status!=200:return web.json_response({'error':{'message':'Unavailable'}},status=self.status)
   return web.json_response({'id':'chat-test','object':'chat.completion','created':123,'model':p['model'],
    'choices':[{'index':0,'message':self.answer,'finish_reason':self.finish}],
    'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20,'completion_tokens_details':{'reasoning_tokens':3}}})
  upstream=web.Application();upstream.router.add_route('*','/{tail:.*}',backend)
  self.up=TestServer(upstream);await self.up.start_server()
  url=str(self.up.make_url('')).rstrip('/')
  self.client=TestClient(TestServer(create_app(root,url,url)));await self.client.start_server()
  self.headers={'Authorization':'Bearer native'}
  self.payload={'model':'deepseek-v4.1','stream':True,'store':False,'reasoning':{'effort':'xhigh'},
    'input':[{'role':'user','content':[{'type':'input_text','text':'Describe.'},{'type':'input_image','image_url':IMAGE,'detail':'high'}]}]}
 async def asyncTearDown(self):
  await self.client.close();await self.up.close();self.temp.cleanup()
 async def send(self,p=None):
  r=await self.client.post('/v1/responses',headers=self.headers,json=p or self.payload)
  text=await r.text()
  events=[json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ') and line[6:]!='[DONE]']
  return r,events
 async def test_images_advertised_for_verified_gpu_models(self):
  r=await self.client.get('/v1/models',headers=self.headers)
  models={m['slug']:m for m in (await r.json())['models']}
  self.assertIn('image',models['deepseek-v4.1']['input_modalities'])
  self.assertEqual(models['glm-5.3-flash']['input_modalities'],['text','image'])
  self.assertEqual(models['gpt-test']['input_modalities'],['text','image'])
 async def test_image_conversion_stream_and_key_isolation(self):
  r,events=await self.send();self.assertEqual(r.status,200)
  self.assertEqual(events[-1]['type'],'response.completed')
  result=events[-1]['response'];self.assertEqual(result['output'][0]['content'][0]['text'],'A red square and a blue circle.')
  self.assertEqual(result['usage']['input_tokens'],12)
  path,p,headers=self.seen[-1]
  self.assertEqual(path,'/v1/chat/completions');self.assertEqual(headers['Authorization'],'Bearer gpu')
  self.assertNotIn('ChatGPT-Account-Id',headers)
  self.assertEqual(p['messages'][0]['content'],[{'type':'text','text':'Describe.'},{'type':'image_url','image_url':{'url':IMAGE,'detail':'high'}}])
  self.assertEqual(p['reasoning_effort'],'xhigh')
 async def test_tool_call_roundtrip_preserves_image_reasoning_and_format(self):
  self.answer={'role':'assistant','content':None,'reasoning_content':'Check the reference.',
   'tool_calls':[{'id':'call-7','type':'function','function':{'name':'lookup','arguments':'{"id":7}'}}]};self.finish='tool_calls'
  p=copy.deepcopy(self.payload);p['tools']=[{'type':'function','name':'lookup','parameters':{'type':'object','properties':{'id':{'type':'integer'}}}}]
  p['tool_choice']={'type':'function','name':'lookup'}
  r,events=await self.send(p);self.assertEqual(r.status,200)
  calls=[e['item'] for e in events if e['type']=='response.output_item.done' and e['item']['type']=='function_call']
  self.assertEqual(len(calls),1);self.assertEqual(calls[0]['call_id'],'call-7');self.assertEqual(calls[0]['arguments'],'{"id":7}')
  p['input']+=events[-1]['response']['output']+[{'type':'function_call_output','call_id':'call-7','output':'verified'}]
  p['stream']=False;p['text']={'format':{'type':'json_schema','name':'result','strict':True,'schema':{'type':'object','properties':{}}}}
  self.answer={'role':'assistant','content':'{}'};self.finish='stop'
  r=await self.client.post('/v1/responses',headers=self.headers,json=p);self.assertEqual(r.status,200)
  data=await r.json();self.assertEqual(data['status'],'completed')
  sent=self.seen[-1][1];self.assertEqual(sent['messages'][-1],{'role':'tool','tool_call_id':'call-7','content':'verified'})
  self.assertEqual(sent['messages'][-2]['reasoning_content'],'Check the reference.')
  self.assertEqual(sent['response_format']['json_schema']['name'],'result')
 async def test_ultra_preserves_images_in_all_three_xhigh_passes(self):
  p=copy.deepcopy(self.payload);p['reasoning']['effort']='ultra'
  r,events=await self.send(p);self.assertEqual(r.status,200)
  self.assertEqual(events[-1]['type'],'response.completed')
  self.assertEqual(events[-1]['response']['metadata']['gpu_gateway_passes'],'3')
  self.assertEqual(events[-1]['response']['usage']['total_tokens'],60)
  self.assertEqual(len(self.seen),3)
  for path,sent,_ in self.seen:
   self.assertEqual(path,'/v1/chat/completions');self.assertEqual(sent['reasoning_effort'],'xhigh')
   self.assertEqual(sent['messages'][0]['content'][1]['image_url']['url'],IMAGE)
 async def test_length_limit_is_not_reported_as_success(self):
  self.finish='length';r,events=await self.send()
  self.assertTrue(events,'Expected an explicit terminal stream event')
  self.assertEqual(events[-1]['type'],'response.incomplete')
  self.assertEqual(events[-1]['response']['incomplete_details']['reason'],'max_output_tokens')
 async def test_upstream_error_is_not_a_completed_answer(self):
  self.status=503;r,events=await self.send()
  self.assertFalse(any(e['type']=='response.completed' for e in events))
  self.assertTrue(r.status==503 or any(e['type']=='error' for e in events))
 async def test_custom_tool_input_survives_conversion(self):
  p=copy.deepcopy(self.payload);p['tools']=[{'type':'custom','name':'apply_patch','description':'Apply a patch.'}]
  self.answer={'role':'assistant','content':None,'tool_calls':[{'id':'custom-1','type':'function','function':{'name':'apply_patch','arguments':'{"input":"*** Begin Patch\\n*** End Patch"}'}}]};self.finish='tool_calls'
  r,events=await self.send(p)
  self.assertEqual(r.status,200)
  call=events[-1]['response']['output'][0]
  self.assertEqual(call['type'],'custom_tool_call');self.assertEqual(call['input'],'*** Begin Patch\n*** End Patch')
 async def test_invalid_upstream_completion_fails_explicitly(self):
  self.finish=None;r,events=await self.send()
  self.assertEqual(events[-1]['type'],'error')
  self.assertFalse(any(e['type']=='response.completed' for e in events))
 async def test_namespace_tool_roundtrip_and_hosted_search_capability(self):
  p=copy.deepcopy(self.payload)
  p['tools']=[{'type':'namespace','name':'catalog','tools':[{'type':'function','name':'lookup','description':'Lookup','parameters':{'type':'object','properties':{}}}]},{'type':'web_search','external_web_access':False}]
  r,events=await self.send(p);self.assertEqual(events[-1]['type'],'response.completed')
  sent=self.seen[-1][1];self.assertEqual(len(sent['tools']),1)
  alias=sent['tools'][0]['function']['name']
  self.assertTrue(any('web_search' in str(m['content']) and 'unavailable' in str(m['content']) for m in sent['messages']))
  self.answer={'role':'assistant','content':None,'tool_calls':[{'id':'ns-1','type':'function','function':{'name':alias,'arguments':'{}'}}]};self.finish='tool_calls'
  r,events=await self.send(p);call=events[-1]['response']['output'][0]
  self.assertEqual((call['namespace'],call['name']),('catalog','lookup'))
  p['input']+=[call,{'type':'function_call_output','call_id':'ns-1','output':'found'}]
  self.answer={'role':'assistant','content':'Found'};self.finish='stop'
  await self.send(p)
  self.assertEqual(self.seen[-1][1]['messages'][-2]['tool_calls'][0]['function']['name'],alias)
