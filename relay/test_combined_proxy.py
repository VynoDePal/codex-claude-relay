import unittest, tempfile,json
from compression import zstd
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from combined_proxy import create_app

class CombinedTests(unittest.IsolatedAsyncioTestCase):
 async def test_catalog_and_provider_isolation(self):
  seen=[]
  async def native(request):
   self.assertEqual(request.headers['Authorization'],'Bearer native-secret')
   if request.path=='/models':return web.json_response({'models':[{'slug':'gpt-test','display_name':'GPT Test','base_instructions':'original instructions'}]})
   data=await request.json();seen.append(('openai',data));return web.json_response({'model':data['model'],'output':[]})
  async def gpu(request):
   self.assertEqual(request.headers['Authorization'],'Bearer gpu-secret')
   self.assertNotIn('ChatGPT-Account-Id',request.headers)
   if request.path=='/v1/models':return web.json_response({'data':[{'id':x} for x in ['actif','glm-5.3-flash','deepseek-v4.1']]})
   data=await request.json();seen.append(('gpu',data))
   result={'id':'test-response','object':'response','status':'completed','model':data['model'],
           'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'OK'}]}],
           'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}
   if not data.get('stream'):return web.json_response(result)
   return web.Response(body=('data: '+json.dumps({'type':'response.completed','response':result})+'\n\n').encode(),content_type='text/event-stream')
  a=web.Application();a.router.add_route('*','/{tail:.*}',native)
  b=web.Application();b.router.add_route('*','/{tail:.*}',gpu)
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'native-secret','account_id':'test-account'}}));(root/'gateway.key').write_text('gpu-secret')
   async with TestServer(a) as sa,TestServer(b) as sb:
    app=create_app(root,str(sa.make_url('')).rstrip('/'),str(sb.make_url('')).rstrip('/'))
    async with TestClient(TestServer(app)) as c:
     headers={'Authorization':'Bearer native-secret','ChatGPT-Account-Id':'test-account'}
     # The built-in OpenAI provider probes WebSocket even for a local base URL.
     # Codex interprets HTTP 426 as an immediate switch to HTTP streaming.
     res=await c.get('/v1/responses',headers={**headers,'Upgrade':'websocket'})
     self.assertEqual(res.status,426);self.assertEqual(seen,[])
     res=await c.get('/v1/responses');self.assertEqual(res.status,401)
     res=await c.get('/v1/models',headers=headers);self.assertEqual(res.status,200)
     models=(await res.json())['models'];self.assertEqual([m['slug'] for m in models],['gpt-test','actif','glm-5.3-flash','deepseek-v4.1','claude-code-opus','claude-code-fable','gemini-4-argon']);self.assertEqual(models[0]['base_instructions'],'original instructions')
     message=[{'role':'user','content':[{'type':'input_text','text':'Hi'}]}]
     p={'model':'gpt-test','input':message,'service_tier':'priority'}
     res=await c.post('/v1/responses',headers=headers,json=p);self.assertEqual(res.status,200);self.assertEqual(seen[-1],('openai',p))
     # Desktop's built-in provider compresses request bodies with zstd.
     # Forwarding must preserve the decoded JSON and correct HTTP encoding.
     res=await c.post('/v1/responses',headers={**headers,'Content-Type':'application/json','Content-Encoding':'zstd'},data=zstd.compress(json.dumps(p).encode()))
     self.assertEqual(res.status,200);self.assertEqual(seen[-1],('openai',p))
     res=await c.post('/v1/responses',headers=headers,json={'model':'actif','input':message,'stream':True,'service_tier':'priority','reasoning':{'effort':'medium'}})
     self.assertEqual(res.status,200);self.assertIn('response.completed',await res.text());self.assertEqual(seen[-1][0],'gpu');self.assertEqual(seen[-1][1]['input'][0]['content'],'Hi');self.assertNotIn('service_tier',seen[-1][1]);self.assertEqual(seen[-1][1]['reasoning']['effort'],'high')
     count=len(seen);res=await c.post('/v1/responses',json={'model':'actif'});self.assertEqual(res.status,401);self.assertEqual(len(seen),count)
     res=await c.post('/v1/responses',headers=headers,json={'model':'unknown'});self.assertEqual(res.status,400);self.assertEqual(len(seen),count)

     for model in models[1:4]:self.assertIn('ultra',[r['effort'] for r in model['supported_reasoning_levels']])
     p={'model':'gpt-test','input':message,'reasoning':{'effort':'ultra'}}
     count=len(seen);res=await c.post('/v1/responses',headers=headers,json=p)
     self.assertEqual(res.status,200);self.assertEqual(seen[count:],[('openai',p)])

     count=len(seen)
     res=await c.post('/v1/responses',headers=headers,json={'model':'deepseek-v4.1','input':message,'stream':True,'reasoning':{'effort':'ultra'}})
     self.assertEqual(res.status,200)
     events=[json.loads(line[6:]) for line in (await res.text()).splitlines() if line.startswith('data: ')]
     self.assertEqual(len(seen)-count,3)
     self.assertEqual([p['reasoning']['effort'] for _,p in seen[count:]],['xhigh']*3)
     self.assertEqual(events[-1]['response']['metadata']['gpu_gateway_passes'],'3')

     count=len(seen)
     res=await c.post('/v1/responses/compact',headers=headers,json={'model':'actif','input':message,'reasoning':{'effort':'ultra'}})
     self.assertEqual(res.status,200);self.assertEqual(len(seen)-count,1)
     self.assertEqual(seen[-1][1]['reasoning']['effort'],'xhigh')
