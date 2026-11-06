import asyncio
import json
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from combined_proxy import create_app, STATE
import gemini_relay as gemini


class GeminiCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_failure_is_a_terminal_responses_event(self):
        async def runner(payload, root):
            raise ValueError('Antigravity temporarily unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-secret'}}))
            async with TestClient(TestServer(create_app(root, gemini_runner=runner))) as client:
                response = await client.post('/v1/responses', headers={'Authorization': 'Bearer test-secret'}, json={'model': 'gemini-4-argon', 'input': 'Hi', 'stream': True})
                events = [json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
                self.assertEqual(events[-1]['type'], 'response.failed')
                self.assertEqual(events[-1]['response']['error']['message'], 'Antigravity temporarily unavailable')
                self.assertEqual(events[-1]['response']['status'], 'failed')

    async def test_only_argon_is_added_without_hiding_existing_models(self):
        async def native(request):
            return web.json_response({'models': [{'slug': 'gpt-test', 'display_name': 'GPT Test'}]})
        upstream = web.Application()
        upstream.router.add_get('/models', native)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-secret'}}))
            async with TestServer(upstream) as server:
                app = create_app(root, str(server.make_url('')).rstrip('/'))
                app[STATE]['gpu'] = [{'slug': 'glm-5.3-flash'}, {'slug': 'deepseek-v4.1'}]
                async with TestClient(TestServer(app)) as client:
                    response = await client.get('/v1/models?client_version=0.162.0', headers={'Authorization': 'Bearer test-secret'})
                    self.assertEqual(response.status, 200)
                    models = (await response.json())['models']
                    gemini = [m for m in models if m['slug'].startswith('gemini-')]
                    self.assertEqual(len(gemini), 1, 'Gemini 4 Argon must be selectable exactly once')
                    self.assertEqual(gemini[0]['display_name'], 'Gemini 4 Argon')
                    self.assertEqual(gemini[0]['slug'], 'gemini-4-argon')
                    self.assertEqual([e['effort'] for e in gemini[0]['supported_reasoning_levels']], ['low', 'medium', 'high'])
                    self.assertEqual({m['slug'] for m in models}, {'gpt-test', 'glm-5.3-flash', 'deepseek-v4.1', 'claude-code-opus', 'gemini-4-argon'})

    async def test_argon_routes_locally_without_waiting_for_other_providers(self):
        calls = []
        async def runner(payload, root):
            calls.append(payload)
            _, mapping = gemini.prepare(payload)
            return gemini.convert({'status': 'SUCCESS', 'structured_output': {'text': 'ARGON_LOCAL_OK', 'calls': []}}, payload, mapping)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-secret'}}))
            app = create_app(root, 'http://127.0.0.1:1', 'http://127.0.0.1:1', gemini_runner=runner)
            async with TestClient(TestServer(app)) as client:
                unauthorized = await client.post('/v1/responses', json={'model': 'gemini-4-argon', 'input': 'Hi'})
                self.assertEqual(unauthorized.status, 401)
                self.assertEqual(calls, [])
                response = await asyncio.wait_for(client.post('/v1/responses', headers={'Authorization': 'Bearer test-secret'}, json={'model': 'gemini-4-argon', 'input': 'Hi', 'stream': True}), 1)
                events = [json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
                self.assertEqual(response.status, 200)
                final = events[-1]['response']
                self.assertEqual(final['model'], 'gemini-4-argon')
                self.assertEqual(final['output'][0]['content'][0]['text'], 'ARGON_LOCAL_OK')
                self.assertEqual(len(calls), 1)
                self.assertNotIn('test-secret', json.dumps(calls))

    async def test_compaction_checkpoint_can_be_resumed_by_argon(self):
        seen = []
        async def runner(payload, root):
            seen.append(payload)
            _, mapping = gemini.prepare(payload)
            return gemini.convert({'status': 'SUCCESS', 'structured_output': {'text': 'Remember code ORANGE_731.', 'calls': []}}, payload, mapping)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-secret'}}))
            app = create_app(root, gemini_runner=runner)
            async with TestClient(TestServer(app)) as client:
                headers = {'Authorization': 'Bearer test-secret'}
                response = await client.post('/v1/responses', headers=headers, json={'model': 'gemini-4-argon', 'input': [{'role': 'user', 'content': 'Remember ORANGE_731'}, {'type': 'compaction_trigger'}]})
                self.assertEqual(response.status, 200)
                checkpoint = (await response.json())['output']
                self.assertEqual(checkpoint[0]['type'], 'compaction')
                self.assertEqual(seen[-1]['tools'], [])
                self.assertEqual(seen[-1]['tool_choice'], 'none')
                response = await client.post('/v1/responses', headers=headers, json={'model': 'gemini-4-argon', 'input': checkpoint + [{'role': 'user', 'content': 'Continue'}]})
                self.assertEqual(response.status, 200)
                self.assertIn('ORANGE_731', json.dumps(seen[-1]))
                self.assertNotIn('encrypted_content', json.dumps(seen[-1]))


class GeminiProtocolTests(unittest.TestCase):
    def payload(self):
        return {'model': 'gemini-4-argon', 'input': 'Read then edit', 'tools': [
            {'type': 'namespace', 'name': 'functions', 'description': 'Use the supplied client execution protocol.', 'tools': [
                {'type': 'function', 'name': 'exec', 'parameters': {'type': 'object'}},
                {'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "PATCH"'}},
            ]},
        ]}

    def test_tool_round_trip_retains_namespaces_grammar_and_results(self):
        payload = self.payload()
        chat, mapping = gemini.prepare(payload)
        self.assertIn('Use the supplied client execution protocol.', json.dumps(chat))
        self.assertIn('start: \\"PATCH\\"'.replace('\\"', '"'), str(chat['custom_tool_formats']))
        alias = next(k for k, v in mapping.items() if v['name'] == 'apply_patch')
        result = gemini.convert({'status': 'SUCCESS', 'structured_output': {'text': 'Editing', 'calls': [{'name': alias, 'arguments': '{"input":"PATCH"}'}]}}, payload, mapping)
        call = next(x for x in result['output'] if x['type'] == 'custom_tool_call')
        self.assertEqual((call['name'], call['namespace'], call['input']), ('apply_patch', 'functions', 'PATCH'))
        payload['input'] = [{'role': 'user', 'content': 'Edit'}] + result['output'] + [{'type': 'custom_tool_call_output', 'call_id': call['call_id'], 'output': 'PATCH_APPLIED'}]
        _, prompt, _ = gemini.cli_request(payload)
        self.assertIn('PATCH_APPLIED', prompt)

    def test_native_reasoning_variants_and_usage_are_not_misreported(self):
        for effort, expected in [('low', 'gemini-4-argon-low'), ('medium', 'gemini-4-argon-medium'), ('high', 'gemini-4-argon-high')]:
            self.assertEqual(gemini.cli_model({'model': 'gemini-4-argon', 'reasoning': {'effort': effort}}), expected)
        with self.assertRaises(ValueError):
            gemini.cli_model({'model': 'gemini-4-argon', 'reasoning': {'effort': 'ultra'}})
        payload = self.payload()
        _, mapping = gemini.prepare(payload)
        result = gemini.convert({'status': 'SUCCESS', 'structured_output': {'text': 'OK', 'calls': []}, 'usage': {'input_tokens': 100, 'cache_read_tokens': 40, 'output_tokens': 20, 'thinking_tokens': 5, 'total_tokens': 120}}, payload, mapping)
        self.assertEqual(result['usage']['input_tokens'], 100)
        self.assertEqual(result['usage']['input_tokens_details']['cached_tokens'], 40)
        self.assertEqual(result['usage']['output_tokens'], 20)
        self.assertEqual(result['usage']['output_tokens_details']['reasoning_tokens'], 5)
        self.assertNotIn('claude_models_used', result.get('metadata', {}))

    def test_malformed_and_forbidden_calls_are_rejected(self):
        payload = self.payload()
        _, mapping = gemini.prepare(payload)
        for result in [{'status': 'ERROR', 'error': 'quota reached'}, {'status': 'SUCCESS', 'response': 'no schema output'}, {'status': 'SUCCESS', 'structured_output': {'text': '', 'calls': [{'name': 'invented', 'arguments': '{}'}]}}]:
            with self.assertRaises(ValueError):
                gemini.convert(result, payload, mapping)
        payload['tool_choice'] = 'none'
        alias = next(iter(mapping))
        with self.assertRaises(ValueError):
            gemini.convert({'status': 'SUCCESS', 'structured_output': {'text': '', 'calls': [{'name': alias, 'arguments': '{}'}]}}, payload, mapping)

    def test_images_fail_explicitly_instead_of_being_discarded(self):
        payload = {'model': 'gemini-4-argon', 'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}]}]}
        with self.assertRaisesRegex(ValueError, '[Ii]mage'):
            gemini.cli_request(payload)


class GeminiCliTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, root, body):
        executable = root / '.local/bin/agy'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/usr/bin/python3\n' + body)
        executable.chmod(0o700)
        profile = root / '.gemini/config/agents/codex-gemini-bridge/agent.md'
        profile.parent.mkdir(parents=True)
        profile.write_text(gemini.AGENT_PROFILE)
        return profile

    async def test_stdin_stays_open_until_the_terminal_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, '''import json,select,sys
request=json.loads(sys.stdin.readline())
receipt=request["message"]["content"].rsplit("\\n",1)[-1]
name=sys.argv[sys.argv.index("--agent")+1]
print(json.dumps({"event":"init","init":{"model":"gemini-4-argon-medium","agent":name}}),flush=True)
ready,_,_=select.select([sys.stdin],[],[],0.1)
if ready and sys.stdin.read()=="":
    sys.exit(0)
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":"stream preserved","calls":[],"context_receipt":receipt}}}),flush=True)
sys.stdin.read()
''')
            with patch('gemini_relay.Path.home', return_value=root):
                result = await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'Hi'}, root)
            self.assertEqual(result['output'][0]['content'][0]['text'], 'stream preserved')

    async def test_long_context_separates_instructions_and_pins_argon_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, '''import sys,json
from pathlib import Path
args=sys.argv[1:]
data=json.loads(sys.stdin.readline())
name=args[args.index("--agent")+1]
profile=Path(__file__).parents[2]/".gemini/config/agents"/name/"agent.md"
Path(__file__).parents[2].joinpath("observed.json").write_text(json.dumps({"args":args,"profile":profile.read_text(),"input":data["message"]["content"]}))
print(json.dumps({"event":"init","init":{"model":args[args.index("--model")+1],"agent":args[args.index("--agent")+1]}}))
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":str(len(data["message"]["content"])),"calls":[],"context_receipt":data["message"]["content"].rsplit("\\n",1)[-1]}}}))
''')
            with patch('gemini_relay.Path.home', return_value=root):
                result = await gemini.run_cli({'model': 'gemini-4-argon', 'reasoning': {'effort': 'high'}, 'instructions': 'x' * 300000, 'input': 'ACTUAL_TASK'}, root)
            self.assertLess(int(result['output'][0]['content'][0]['text']), 1000)
            observed = json.loads((root / 'observed.json').read_text())
            args = observed['args']
            self.assertIn('x' * 300000, observed['profile'])
            self.assertNotIn('ACTUAL_TASK', observed['profile'])
            self.assertIn('ACTUAL_TASK', observed['input'])
            self.assertIn('  - finish\n', observed['profile'])
            self.assertIn('commandExecutionPolicy: "off"', observed['profile'])
            self.assertEqual(args[args.index('--model') + 1], 'gemini-4-argon-high')
            self.assertEqual(args[args.index('--effort') + 1], 'high')
            self.assertTrue(args[args.index('--agent') + 1].startswith('codex-gemini-bridge-'))
            self.assertIn('--sandbox', args)
            self.assertNotIn('--dangerously-skip-permissions', args)
            self.assertLess(max(map(len, args)), 10000)
            self.assertEqual(list((root / 'gemini-selector').iterdir()), [])

    async def test_missing_or_tampered_profile_never_starts_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.fixture(root, 'raise AssertionError("must never run")\n')
            profile.write_text('tools: [run_command]')
            with patch('gemini_relay.Path.home', return_value=root), self.assertRaisesRegex(ValueError, 'profile is missing or modified'):
                await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'Hi'}, root)
            self.assertFalse((root / 'gemini-selector').exists())

    async def test_timeout_stops_cli_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, '''import os,sys,time
from pathlib import Path
Path(__file__).parents[2].joinpath("pid").write_text(str(os.getpid()))
sys.stdin.readline()
time.sleep(30)
''')
            with patch('gemini_relay.Path.home', return_value=root), patch.object(gemini, 'CLI_TIMEOUT_SECONDS', .3):
                with self.assertRaisesRegex(ValueError, 'request was stopped'):
                    await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'Hi'}, root)
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / 'pid').read_text()), 0)

    async def test_unexpected_native_tool_is_not_reported_as_codex_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, '''import json,sys
sys.stdin.readline()
name=sys.argv[sys.argv.index("--agent")+1]
print(json.dumps({"event":"init","init":{"model":"gemini-4-argon-medium","agent":name}}))
print(json.dumps({"event":"step_update","step_update":{"tool_info":{"name":"run_command"}}}))
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":"ok","calls":[]}}}))
''')
            with patch('gemini_relay.Path.home', return_value=root), self.assertRaisesRegex(ValueError, 'Unexpected native'):
                await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'Hi'}, root)


if __name__ == '__main__':
    unittest.main()
