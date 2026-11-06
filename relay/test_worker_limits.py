import asyncio, contextlib, io, json, tempfile, unittest
from pathlib import Path
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from ultra_n_engine import ResearchConfig, run_research, _Run
from ultra_n_relay import Gateway, load_config
from ultra_n_cli import main
from test_ultra_n_engine import answer, call, Sources


class WorkerBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_waits_for_a_peers_actual_usage_before_giving_up(self):
        started=asyncio.Event();release=asyncio.Event()
        class Model:
            async def complete(self,messages,tools,max_tokens):
                if len(messages)==1:
                    started.set();await release.wait()
                    return answer('Peer result')
                return answer('Useful compact findings [S1].')
        with tempfile.TemporaryDirectory() as tmp:
            run=_Run('Question',ResearchConfig(max_total_tokens=100000),Model(),Sources(),Path(tmp),None)
            run.sources={'a':{'id':'S1','title':'Paper','text':'Observed evidence'}}
            messages=[{'role':'system','content':'Worker instructions'},{'role':'user','content':'Read evidence'},
                      {'role':'tool','content':json.dumps({'source_id':'S1'})}]
            peer=asyncio.create_task(run.model([{'role':'user','content':'x'*60000}],[]))
            await asyncio.wait_for(started.wait(),1)
            summary=asyncio.create_task(run.worker_budget_summary(messages))
            try:
                await asyncio.sleep(.02)
                self.assertFalse(summary.done(),'The summary must wait while its peer holds the reservation')
                release.set();await peer
                self.assertEqual(await asyncio.wait_for(summary,1),'Useful compact findings [S1].')
                self.assertIsNone(run.metrics['stop_reason'])
                self.assertEqual(run.reserved,0)
            finally:
                peer.cancel();summary.cancel()
                await asyncio.gather(peer,summary,return_exceptions=True)

    async def test_escaped_source_metadata_is_bounded_and_excludes_unread_evidence(self):
        observed=[]
        class Model:
            async def complete(self,messages,tools,max_tokens):
                observed.append(messages)
                return answer('Partial findings [S1].')
        with tempfile.TemporaryDirectory() as tmp:
            run=_Run('Question',ResearchConfig(),Model(),Sources(),Path(tmp),None)
            run.sources={str(i):{'id':f'S{i}','title':'\x00'*300,'text':'\x00'*10000} for i in range(1,21)}
            run.sources['other']={'id':'S99','title':'Other worker','text':'UNREAD_OTHER_WORKER_EVIDENCE'}
            messages=[{'role':'system','content':'Worker instructions'},{'role':'user','content':'Task'}]
            messages += [{'role':'tool','content':json.dumps({'source_id':f'S{i}'})} for i in range(1,21)]
            result=await asyncio.wait_for(run.worker_budget_summary(messages),1)
            self.assertIn('Partial findings',result)
            packed=observed[0][2]['content'].split('\n',1)[1]
            self.assertLessEqual(len(packed.encode()),10000)
            self.assertEqual(len(json.loads(packed)['sources']),20)
            self.assertNotIn('UNREAD_OTHER_WORKER_EVIDENCE',str(observed))

    async def test_large_worker_context_returns_compact_findings_for_both_models(self):
        class LargeSources(Sources):
            async def fetch(self,url):
                return {'url':url,'title':'Large paper','text':'Observed evidence. '+'🌍'*20000,'content_type':'text/plain'}
        for model in ('deepseek-v4.1','glm-5.3-flash'):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                requests=[]
                class Model:
                    async def complete(self,messages,tools,max_tokens):
                        requests.append((messages,tools))
                        if 'You supervise.' in messages[0]['content']:
                            if len(messages)==2:
                                return answer(None,[call('delegate',tasks=[{'objective':'Read the evidence','role':'researcher'}])])
                            return answer('Worker findings supported by [S1, S2, S3].')
                        if tools:
                            return answer(None,[call('read_source',url='https://example.org/'+str(i)) for i in range(3)])
                        return answer('Useful worker findings with uncertainty [S1, S2, S3].')
                directory=Path(tmp)/'run'
                result=await run_research('Read evidence',ResearchConfig(model=model,max_agents=1,concurrency=1,max_total_tokens=65000),Model(),LargeSources(),directory)
                state=json.loads((directory/'state.json').read_text())
                self.assertIn('Useful worker findings',state['worker_results'][0]['summary'])
                self.assertEqual(state['worker_results'][0]['status'],'partial')
                self.assertEqual(state['worker_results'][0]['stop_reason'],'token_limit')
                self.assertEqual(result['status'],'completed')
                self.assertIsNone(result['metrics']['stop_reason'])
                summaries=[messages for messages,tools in requests if not tools]
                self.assertEqual(len(summaries),1)
                self.assertLess(len(json.dumps(summaries[0]).encode()),30000)
                self.assertLessEqual(result['metrics']['total_tokens'],65000)
                self.assertEqual(result['metrics']['token_overrun'],0)

    async def test_compact_summary_does_not_bypass_model_call_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Model:
                async def complete(self,messages,tools,max_tokens):
                    if 'You supervise.' in messages[0]['content']:
                        return answer(None,[call('delegate',tasks=[{'objective':'Read','role':'researcher'}])])
                    return answer('Unsupported result')
            result=await run_research('Read evidence',ResearchConfig(max_agents=1,concurrency=1,max_model_calls=1),Model(),Sources(),Path(tmp)/'run')
            self.assertEqual(result['metrics']['model_calls'],1)
            self.assertEqual(result['status'],'budget_exhausted')


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_timeout_is_configurable_and_cancels_slow_http(self):
        async def upstream(request):
            await asyncio.sleep(.08)
            return web.json_response(answer('Ready'))
        app=web.Application();app.router.add_post('/v1/chat/completions',upstream)
        async with TestServer(app) as server, ClientSession() as client:
            url=str(server.make_url('')).rstrip('/')
            fast=Gateway(client,url,{},'glm-5.3-flash',timeout_seconds=.02)
            with self.assertRaises(TimeoutError):await fast.complete([{'role':'user','content':'Hi'}],[],512)
            patient=Gateway(client,url,{},'glm-5.3-flash',timeout_seconds=.5)
            result=await patient.complete([{'role':'user','content':'Hi'}],[],512)
            self.assertEqual(result['choices'][0]['message']['content'],'Ready')

    async def test_two_minutes_reaches_http_transport_by_default(self):
        observed=[]
        class ObservedClient:
            def __init__(self,client):self.client=client
            def post(self,*args,**kwargs):
                observed.append(kwargs['timeout'])
                return self.client.post(*args,**kwargs)
        async def upstream(request):return web.json_response(answer('OK'))
        app=web.Application();app.router.add_post('/v1/chat/completions',upstream)
        async with TestServer(app) as server, ClientSession() as client:
            for model in ('glm-5.3-flash','deepseek-v4.1'):
                config=ResearchConfig(model=model)
                gateway=Gateway(ObservedClient(client),str(server.make_url('')).rstrip('/'),{},model,timeout_seconds=config.model_timeout_seconds)
                result=await gateway.complete([{'role':'user','content':'Hi'}],[],512)
                self.assertEqual(result['choices'][0]['message']['content'],'OK')
                self.assertEqual(observed[-1].sock_read,120)
                self.assertGreaterEqual(observed[-1].total,120)


class SettingsTests(unittest.TestCase):
    def test_timeout_persists_independently_for_both_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                main(['config','--model','deepseek-v4.1','--model-timeout-seconds','120'],root)
                main(['config','--model','glm-5.3-flash','--model-timeout-seconds','120'],root)
            self.assertEqual(load_config(root,'glm-5.3-flash').model_timeout_seconds,120)
            self.assertEqual(load_config(root,'deepseek-v4.1').model_timeout_seconds,120)
            path=root/'ultra-n/glm-5.3-flash.json';before=path.read_bytes()
            with self.assertRaises(SystemExit),contextlib.redirect_stderr(io.StringIO()):
                main(['config','--model','glm-5.3-flash','--model-timeout-seconds','-1'],root)
            self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
