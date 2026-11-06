"""GLM catalog and research must keep their own model and budgets end to end."""
import asyncio, contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from combined_proxy import create_app


class GLMParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'native','account_id':'account'}}))
        (self.root/'gateway.key').write_text('gpu')
        (self.root/'ultra-n').mkdir()
        (self.root/'ultra-n/config.json').write_text(json.dumps({'max_agents':10,'concurrency':2}))
        (self.root/'ultra-n/glm-5.3-flash.json').write_text(json.dumps({'model':'glm-5.3-flash','max_agents':3,'concurrency':1}))
        self.seen = []
        async def upstream(request):
            if request.path == '/models': return web.json_response({'models':[{'slug':'gpt-test','input_modalities':['text','image']}]})
            if request.path == '/v1/models': return web.json_response({'data':[{'id':'deepseek-v4.1'},{'id':'glm-5.3-flash'}]})
            self.seen.append((dict(request.headers),await request.json()))
            return web.json_response({'output':[]})
        app = web.Application()
        app.router.add_route('*','/{tail:.*}',upstream)
        self.up = TestServer(app)
        await self.up.start_server()
        url = str(self.up.make_url('')).rstrip('/')
        self.client = TestClient(TestServer(create_app(self.root,url,url)))
        await self.client.start_server()
        self.headers = {'Authorization':'Bearer native'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.up.close()
        self.tmp.cleanup()

    async def test_catalog_enables_glm_images_and_keeps_both_research_modes(self):
        r = await self.client.get('/v1/models',headers=self.headers)
        models = {m['slug']:m for m in (await r.json())['models']}
        self.assertEqual(models['glm-5.3-flash']['input_modalities'],['text','image'])
        self.assertEqual(models['gpt-test']['input_modalities'],['text','image'])
        self.assertIn('deepseek-v4.1-ultra-n',models)
        self.assertIn('glm-5.3-flash-ultra-n',models)
        self.assertIn('0 à 3',models['glm-5.3-flash-ultra-n']['description'])
        self.assertIn('0 à 10',models['deepseek-v4.1-ultra-n']['description'])
        self.assertEqual(models['glm-5.3-flash-ultra-n']['input_modalities'],['text'])

    async def test_glm_research_uses_glm_config_and_reports_glm_alias(self):
        received = {}
        async def run(query,config,gateway,sources,directory,progress):
            received.update(model=gateway.model,config_model=config.model,n=config.max_agents,c=config.concurrency)
            return {'report':'Test report','status':'completed','sources':[],'metrics':{}}
        with patch('ultra_n_engine.run_research',run):
            r = await self.client.post('/v1/responses',headers=self.headers,json={'model':'glm-5.3-flash-ultra-n','input':'A research question'})
            self.assertEqual(r.status,200,await r.text())
            data = await r.json()
        self.assertEqual(received,{'model':'glm-5.3-flash','config_model':'glm-5.3-flash','n':3,'c':1})
        self.assertEqual(data['model'],'glm-5.3-flash-ultra-n')
        self.assertEqual(data['metadata']['max_agents'],'3')
        self.assertEqual(data['reasoning']['effort'],'xhigh')

    async def test_glm_compaction_never_switches_to_deepseek(self):
        r = await self.client.post('/v1/responses/compact',headers=self.headers,json={'model':'glm-5.3-flash-ultra-n','input':'Summarize','reasoning':{'effort':'ultra'}})
        self.assertEqual(r.status,200,await r.text())
        headers,payload = self.seen[-1]
        self.assertEqual(payload['model'],'glm-5.3-flash')
        self.assertEqual(payload['reasoning']['effort'],'xhigh')
        self.assertEqual(headers['Authorization'],'Bearer gpu')
        self.assertNotIn('ChatGPT-Account-Id',headers)

    async def test_mismatched_glm_config_does_not_invoke_another_model(self):
        (self.root/'ultra-n/glm-5.3-flash.json').write_text(json.dumps({'model':'deepseek-v4.1'}))
        r = await self.client.post('/v1/responses',headers=self.headers,json={'model':'glm-5.3-flash-ultra-n','input':'Research'})
        self.assertEqual(r.status,400)
        self.assertIn('configuration', (await r.text()).lower())
        self.assertEqual(self.seen,[])

    async def test_one_research_lock_is_shared_across_models(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def run(*args):
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch('ultra_n_engine.run_research',run):
            r = await self.client.post('/v1/responses',headers=self.headers,json={'model':'glm-5.3-flash-ultra-n','input':'Research','stream':True})
            self.assertEqual(r.status,200,await r.text() if r.status!=200 else '')
            await asyncio.wait_for(started.wait(),2)
            busy = await self.client.post('/v1/responses',headers=self.headers,json={'model':'deepseek-v4.1-ultra-n','input':'Another research'})
            self.assertEqual(busy.status,409)
            r.close()
            await asyncio.wait_for(cancelled.wait(),2)


class GLMSettingsTests(unittest.TestCase):
    def test_updating_glm_n_does_not_change_deepseek_or_duplicate_catalog(self):
        from ultra_n_cli import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'ultra-n').mkdir()
            deepseek = json.dumps({'model':'deepseek-v4.1','max_agents':10,'concurrency':2})
            (root/'ultra-n/config.json').write_text(deepseek)
            native = {'slug':'gpt-test','input_modalities':['text','image']}
            (root/'models_cache.json').write_text(json.dumps({'models':[native,{'slug':'deepseek-v4.1'},{'slug':'glm-5.3-flash'}]}))
            with contextlib.redirect_stdout(io.StringIO()):
                main(['config','--model','glm-5.3-flash','--max-agents','1'],root)
                main(['config','--model','glm-5.3-flash','--max-agents','3'],root)
            self.assertEqual((root/'ultra-n/config.json').read_text(),deepseek)
            glm = json.loads((root/'ultra-n/glm-5.3-flash.json').read_text())
            self.assertEqual(glm['model'],'glm-5.3-flash')
            self.assertEqual(glm['max_agents'],3)
            self.assertEqual(glm['concurrency'],1)
            models = json.loads((root/'models_cache.json').read_text())['models']
            self.assertEqual(models[0],native)
            self.assertEqual([m['slug'] for m in models].count('glm-5.3-flash-ultra-n'),1)
            self.assertEqual([m['slug'] for m in models].count('deepseek-v4.1-ultra-n'),1)


if __name__ == '__main__': unittest.main()
