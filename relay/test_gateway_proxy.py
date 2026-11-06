import copy
import unittest
from gateway_proxy import normalize_request

class NormalizationTests(unittest.TestCase):
    def test_deepseek_text_input_and_assistant_history(self):
        source={'model':'actif','input':[
            {'role':'user','content':[{'type':'input_text','text':'Bonjour'},{'type':'input_text','text':' !'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Salut','annotations':[]}]},
            {'type':'function_call','call_id':'abc','name':'shell','arguments':'{}'},
            {'type':'function_call_output','call_id':'abc','output':'result'}], 'stream':True}
        original=copy.deepcopy(source)
        out=normalize_request(source)
        self.assertEqual(out['input'][0]['content'],'Bonjour !')
        self.assertEqual(out['input'][1]['content'],'Salut')
        self.assertEqual(out['input'][2:],original['input'][2:])
        self.assertTrue(out['stream'])
        self.assertEqual(source,original)

    def test_mixed_images_are_not_dropped(self):
        source={'input':[{'role':'user','content':[{'type':'input_text','text':'Describe'},{'type':'input_image','image_url':'data:image/png;base64,AA=='}]}]}
        self.assertEqual(normalize_request(source),source)

    def test_strings_and_reasoning_are_unchanged(self):
        for source in [{'input':'Bonjour'}, {'input':[{'type':'reasoning','summary':[{'type':'summary_text','text':'reason'}]},{'role':'user','content':'Bonjour'}]}]:
            self.assertEqual(normalize_request(source),source)

if __name__=='__main__': unittest.main()

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway_proxy import create_app
import tempfile
import pathlib

class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            (root/'key').write_text('upstream-test-key')
            (root/'token').write_text('local-test-token')
            seen=[]
            async def backend(request):
                data=await request.json()
                seen.append(data)
                self.assertEqual(request.headers['Authorization'],'Bearer upstream-test-key')
                if isinstance(data['input'][0]['content'],list):
                    return web.json_response({'error':'got input_text'},status=400)
                response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
                await response.prepare(request)
                await response.write(b'data: {"type":"response.output_text.delta","delta":"Bonjour"}\n\n')
                await response.write(b'data: {"type":"response.completed"}\n\n')
                await response.write_eof()
                return response
            upstream_app=web.Application()
            upstream_app.router.add_post('/v1/responses',backend)
            async with TestServer(upstream_app) as server:
                async with TestClient(TestServer(create_app(str(server.make_url('')).rstrip('/'),root/'key',root/'token'))) as client:
                    catalog=await client.get('/v1/models',headers={'Authorization':'Bearer local-test-token'})
                    self.assertEqual(catalog.status,200)
                    self.assertEqual((await catalog.json())['models'][0]['slug'],'actif')
                    payload={'stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':'Bonjour'}]}]}
                    denied=await client.post('/v1/responses',json=payload)
                    self.assertEqual(denied.status,401)
                    self.assertEqual(seen,[])
                    response=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer local-test-token'})
                    self.assertEqual(response.status,200)
                    self.assertIn('Bonjour',await response.text())
                    self.assertEqual(seen[0]['input'][0]['content'],'Bonjour')
