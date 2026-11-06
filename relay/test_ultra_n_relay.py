import asyncio, contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from combined_proxy import create_app

class ResearchDispatchTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  (self.root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'native','account_id':'account'}}))
  (self.root/'gateway.key').write_text('gpu')
  (self.root/'ultra-n').mkdir();(self.root/'ultra-n/config.json').write_text(json.dumps({'max_agents':2,'concurrency':1}))
  self.seen=[]
  async def upstream(r):
   if r.path=='/models':return web.json_response({'models':[{'slug':'gpt-test','input_modalities':['text','image']}]})
   if r.path=='/v1/models':return web.json_response({'data':[{'id':'deepseek-v4.1'}]})
   self.seen.append(await r.json())
   if r.path=='/v1/chat/completions':
    self.gateway_headers=dict(r.headers)
    return web.json_response({'choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]})
   return web.json_response({'output':[]})
  app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
  self.up=TestServer(app);await self.up.start_server();url=str(self.up.make_url('')).rstrip('/')
  self.client=TestClient(TestServer(create_app(self.root,url,url)));await self.client.start_server()
  self.headers={'Authorization':'Bearer native'}
 async def asyncTearDown(self):
  await self.client.close();await self.up.close();self.tmp.cleanup()
 async def test_catalog_adds_research_mode_and_preserves_native(self):
  r=await self.client.get('/v1/models',headers=self.headers)
  models={m['slug']:m for m in (await r.json())['models']}
  self.assertIn('deepseek-v4.1-ultra-n',models)
  self.assertEqual(models['gpt-test']['input_modalities'],['text','image'])
  self.assertEqual(models['deepseek-v4.1-ultra-n']['default_reasoning_level'],'xhigh')
 async def test_compaction_uses_base_model_not_research(self):
  p={'model':'deepseek-v4.1-ultra-n','input':'A short summary','reasoning':{'effort':'xhigh'}}
  r=await self.client.post('/v1/responses/compact',headers=self.headers,json=p)
  self.assertEqual(r.status,200);self.assertEqual(self.seen[-1]['model'],'deepseek-v4.1')
 async def test_real_gateway_adapter_uses_xhigh_and_only_gpu_key(self):
  from ultra_n_relay import Gateway
  async with ClientSession() as c:
   gateway=Gateway(c,str(self.up.make_url('')).rstrip('/'),{'Authorization':'Bearer gpu','ChatGPT-Account-Id':'native-account'},'deepseek-v4.1')
   result=await gateway.complete([{'role':'user','content':'Hello'}],[],512)
  self.assertEqual(result['choices'][0]['message']['content'],'OK')
  self.assertEqual(self.seen[-1]['reasoning_effort'],'xhigh')
  self.assertEqual(self.gateway_headers['Authorization'],'Bearer gpu')
  self.assertNotIn('ChatGPT-Account-Id',self.gateway_headers)
 async def test_research_report_returns_as_codex_sse(self):
  seen={}
  async def engine(query,config,gateway,sources,artifact_dir,progress):
   seen.update(query=query,n=config.max_agents,model=gateway.model)
   await progress({'event':'worker_started','worker':1})
   return {'report':'Finding supported by [S1].','status':'completed','sources':[{'id':'S1'}],
    'metrics':{'agents_created':1,'input_tokens':10,'output_tokens':3,'total_tokens':13,'usage_complete':True}}
  with patch('ultra_n_engine.run_research',engine):
   r=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':'Research question'}]}]})
   self.assertEqual(r.status,200)
   events=[json.loads(line[6:]) for line in (await r.text()).splitlines() if line.startswith('data: ')]
  result=events[-1]['response'];self.assertEqual(events[-1]['type'],'response.completed')
  self.assertEqual(seen,{'query':'Research question','n':2,'model':'deepseek-v4.1'})
  self.assertEqual(result['metadata']['research_status'],'completed');self.assertEqual(result['usage']['total_tokens'],13)
  self.assertIn('Finding supported',result['output'][0]['content'][0]['text'])
 async def test_cancellation_releases_run_lock(self):
  entered=asyncio.Event();cancelled=asyncio.Event()
  async def engine(*args):
   entered.set()
   try:await asyncio.Event().wait()
   finally:cancelled.set()
  with patch('ultra_n_engine.run_research',engine):
   r=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','stream':True,'input':'Question'})
   self.assertEqual(r.status,200,await r.text() if r.status!=200 else '')
   await asyncio.wait_for(entered.wait(),10)
   busy=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','input':'Other'})
   self.assertEqual(busy.status,409)
   r.close();await asyncio.wait_for(cancelled.wait(),2)
  async def completed(*args):
   return {'report':'Done','status':'completed','sources':[],'metrics':{}}
  async def retry():
   with patch('ultra_n_engine.run_research',completed):
    while True:
     response=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','input':'Next research'})
     await response.read()
     if response.status!=409:return response.status
     await asyncio.sleep(0)
  self.assertEqual(await asyncio.wait_for(retry(),2),200)
 async def test_image_rejected_before_starting_research(self):
  r=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','input':[{'role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,AA=='}]}]})
  self.assertEqual(r.status,400);self.assertEqual(self.seen,[])
 async def test_malformed_text_rejected_before_engine(self):
  for part in [{'type':'input_text','text':None},{'type':'input_text','text':42},{'type':'input_text'}]:
   r=await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','input':[{'role':'user','content':[part]}]})
   self.assertEqual(r.status,400)

class SettingsTests(unittest.TestCase):
 def test_config_change_preserves_native_catalog_and_invalid_change_is_atomic(self):
  from ultra_n_cli import main
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   native={'slug':'gpt-test','input_modalities':['text','image']}
   base={'slug':'deepseek-v4.1'}
   (root/'models_cache.json').write_text(json.dumps({'models':[native,base]}))
   with contextlib.redirect_stdout(io.StringIO()):main(['config','--max-agents','8','--concurrency','2'],root)
   data=json.loads((root/'ultra-n/config.json').read_text());self.assertEqual(data['max_agents'],8)
   cached=json.loads((root/'models_cache.json').read_text())['models'];self.assertEqual(cached[0],native)
   self.assertEqual(len([m for m in cached if m['slug']=='deepseek-v4.1-ultra-n']),1)
   before=(root/'ultra-n/config.json').read_bytes()
   with self.assertRaises(SystemExit),contextlib.redirect_stderr(io.StringIO()):main(['config','--max-agents','-1'],root)
   self.assertEqual((root/'ultra-n/config.json').read_bytes(),before)
