import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from combined_proxy import create_app

class CatalogVersionTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  (self.root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'native-test','account_id':'test-account'}}))
  self.seen=[]
  async def upstream(r):
   if r.path=='/models':
    version=r.query.get('client_version');self.seen.append((version,r.headers.get('User-Agent')))
    return web.json_response({'models':[{'slug':'model-'+str(version),'display_name':'Test model'}]})
   p=await r.json();return web.json_response({'model':p['model'],'output':[]})
  app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
  self.up=TestServer(app);await self.up.start_server();self.addAsyncCleanup(self.up.close)
  self.client=TestClient(TestServer(create_app(self.root,str(self.up.make_url('')).rstrip('/'))))
  await self.client.start_server();self.addAsyncCleanup(self.client.close)
  self.headers={'Authorization':'Bearer native-test'}
 async def fetch(self,version=None,ua=None):
  headers={**self.headers,**({'User-Agent':ua} if ua else {})}
  r=await self.client.get('/v1/models',headers=headers,params={'client_version':version} if version else {})
  self.assertEqual(r.status,200);return (await r.json())['models'][0]['slug']
 async def test_client_version_forwarded_and_refreshes_without_waiting_ttl(self):
  self.assertEqual(await self.fetch('0.154.0'),'model-0.154.0')
  self.assertEqual(await self.fetch('0.159.0'),'model-0.159.0')
  self.assertEqual(self.seen,[('0.154.0','codex_cli_rs/0.154.0'),('0.159.0','codex_cli_rs/0.159.0')])
  await self.fetch('0.159.0');self.assertEqual(len(self.seen),2)
 async def test_user_agent_used_for_response_requests(self):
  headers={**self.headers,'User-Agent':'codex_cli_rs/0.160.0 (Linux; x86_64)'}
  r=await self.client.post('/v1/responses',headers=headers,json={'model':'model-0.160.0','input':'Hi'})
  self.assertEqual(r.status,200);self.assertEqual(self.seen[0][0],'0.160.0')
 async def test_installed_desktop_version_is_fallback_and_can_change(self):
  with patch('combined_proxy.installed_client_version',return_value='0.159.0'):self.assertEqual(await self.fetch(),'model-0.159.0')
  with patch('combined_proxy.installed_client_version',return_value='0.160.0'):self.assertEqual(await self.fetch(),'model-0.160.0')
 async def test_explicit_version_has_priority_over_user_agent(self):
  self.assertEqual(await self.fetch('0.159.0','codex_cli_rs/0.154.0'),'model-0.159.0')
 async def test_malformed_version_rejected_without_upstream_request(self):
  r=await self.client.get('/v1/models',headers=self.headers,params={'client_version':'0.159.0&extra=bad'})
  self.assertEqual(r.status,400);self.assertEqual(self.seen,[])
if __name__=='__main__':unittest.main()
