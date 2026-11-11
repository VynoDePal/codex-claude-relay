import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_relay import prepare, run_cli, convert
from gpu_vision import VisionError
from combined_proxy import create_app, STATE


class ClaudeDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_recovers_after_failed_background_refresh(self):
        async def native(request):
            return web.json_response({'models': [{'slug': 'fresh-model'}]})
        upstream = web.Application()
        upstream.router.add_get('/models', native)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'native-test'}}))
            async with TestServer(upstream) as server:
                router = create_app(root, str(server.make_url('')).rstrip('/'))
                router[STATE]['openai'] = [{'slug': 'cached-model'}]
                async def failed_refresh():
                    raise ValueError('bad upstream response')
                task = asyncio.create_task(failed_refresh())
                await asyncio.gather(task, return_exceptions=True)
                router[STATE]['catalog_refresh'] = task
                async with TestClient(TestServer(router)) as client:
                    response = await client.get('/v1/models?client_version=0.160.0', headers={'Authorization': 'Bearer native-test'})
                    self.assertEqual(response.status, 200)
                    self.assertIn('fresh-model', [m['slug'] for m in (await response.json())['models']])

    async def test_malformed_gpu_catalog_does_not_hide_valid_cached_models(self):
        async def native(request):
            return web.json_response({'models': [{'slug': 'gpt-test'}]})
        async def gpu(request):
            return web.json_response({'data': None})
        a, b = web.Application(), web.Application()
        a.router.add_get('/models', native)
        b.router.add_get('/v1/models', gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'native-test'}}))
            (root / 'gateway.key').write_text('gpu-test')
            async with TestServer(a) as sa, TestServer(b) as sb:
                router = create_app(root, str(sa.make_url('')).rstrip('/'), str(sb.make_url('')).rstrip('/'))
                router[STATE]['gpu'] = [{'slug': 'glm-5.3-flash'}]
                async with TestClient(TestServer(router)) as client:
                    response = await client.get('/v1/models?client_version=0.160.0', headers={'Authorization': 'Bearer native-test'})
                    self.assertEqual(response.status, 200)
                    self.assertEqual([m['slug'] for m in (await response.json())['models']], ['gpt-test', 'glm-5.3-flash', 'claude-code-opus', 'claude-code-fable', 'gemini-4-argon'])

    async def test_offline_gpu_does_not_hide_cached_models_and_claude(self):
        release = asyncio.Event()
        async def upstream(request):
            await release.wait()
            return web.json_response({'models': [{'slug': 'fresh-model'}]})
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', upstream)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'native-test'}}))
            async with TestServer(app) as server:
                router = create_app(root, str(server.make_url('')).rstrip('/'))
                router[STATE]['openai'] = [{'slug': 'cached-model', 'display_name': 'Cached'}]
                async with TestClient(TestServer(router)) as client:
                    try:
                        response = await asyncio.wait_for(client.get('/v1/models?client_version=0.160.0', headers={'Authorization': 'Bearer native-test'}), 3)
                        self.assertEqual(response.status, 200)
                        self.assertEqual([m['slug'] for m in (await response.json())['models']], ['cached-model', 'claude-code-opus', 'claude-code-fable', 'gemini-4-argon'])
                    finally:
                        release.set()

    async def test_catalog_restores_claude_without_removing_other_providers(self):
        async def native(request):
            return web.json_response({'models': [{'slug': 'gpt-test', 'display_name': 'GPT Test'}]})

        async def gpu(request):
            return web.json_response({'data': [{'id': 'glm-5.3-flash'}, {'id': 'deepseek-v4.1'}]})

        a, b = web.Application(), web.Application()
        a.router.add_get('/models', native)
        b.router.add_get('/v1/models', gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'native-test'}}))
            (root / 'gateway.key').write_text('gpu-test')
            async with TestServer(a) as sa, TestServer(b) as sb:
                app = create_app(root, str(sa.make_url('')).rstrip('/'), str(sb.make_url('')).rstrip('/'))
                async with TestClient(TestServer(app)) as client:
                    response = await client.get('/v1/models?client_version=0.160.0', headers={'Authorization': 'Bearer native-test'})
                    self.assertEqual(response.status, 200)
                    models = (await response.json())['models']
                    self.assertEqual([m['slug'] for m in models], ['gpt-test', 'glm-5.3-flash', 'deepseek-v4.1', 'claude-code-opus', 'claude-code-fable', 'gemini-4-argon'])
                    claude = next(m for m in models if m['slug'] == 'claude-code-opus')
                    self.assertEqual(claude['display_name'], 'Claude Opus 5.5')
                    self.assertEqual([l['effort'] for l in claude['supported_reasoning_levels']], ['low', 'medium', 'high', 'xhigh', 'max'])
                    fable = next(m for m in models if m['slug'] == 'claude-code-fable')
                    self.assertEqual(fable['display_name'], 'Claude Fable')
                    self.assertEqual([l['effort'] for l in fable['supported_reasoning_levels']], ['low', 'medium', 'high', 'xhigh', 'max'])

    async def test_cli_uses_pinned_opus_effort_and_codex_remains_tool_executor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / '.local/bin/claude'
            executable.parent.mkdir(parents=True)
            executable.write_text('''#!/usr/bin/python3
import json, sys
from pathlib import Path
args=sys.argv[1:]
Path("observed.json").write_text(json.dumps({"args":args,"input":json.loads(sys.stdin.readline())}))
print(json.dumps({"type":"result","structured_output":{"text":"ok","calls":[]},"modelUsage":{"claude-opus-5-5":{}}}))
''')
            executable.chmod(0o700)
            payload = {'model': 'claude-code-opus', 'input': 'hello', 'reasoning': {'effort': 'xhigh'}}
            with patch('claude_relay.pathlib.Path.home', return_value=root):
                result = await run_cli(payload, root)
            args = json.loads((root / 'claude-selector/observed.json').read_text())['args']
            self.assertEqual(args[args.index('--model') + 1], 'claude-opus-5-5')
            self.assertEqual(args[args.index('--effort') + 1], 'xhigh')
            self.assertEqual(args[args.index('--tools') + 1], '')
            self.assertIn('--strict-mcp-config', args)
            self.assertNotIn('--dangerously-skip-permissions', args)
            self.assertEqual(result['output'][0]['content'][0]['text'], 'ok')
            fable = {'model': 'claude-code-fable', 'input': 'hello', 'reasoning': {'effort': 'max'}}
            with patch('claude_relay.pathlib.Path.home', return_value=root):
                await run_cli(fable, root)
            args = json.loads((root / 'claude-selector/observed.json').read_text())['args']
            self.assertEqual(args[args.index('--model') + 1], 'claude-fable-5-2')
            self.assertEqual(args[args.index('--effort') + 1], 'max')

    def test_custom_tool_grammar_reaches_the_model_with_namespace(self):
        payload = {
            'model': 'claude-code-opus', 'input': 'patch the fixture',
            'tools': [{'type': 'namespace', 'name': 'functions', 'tools': [{
                'type': 'custom', 'name': 'apply_patch', 'description': 'Edit a file.',
                'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "*** Begin Patch"'},
            }]}],
        }
        chat, mapping = prepare(payload)
        alias = next(iter(mapping))
        self.assertEqual(chat.get('custom_tool_formats', {}).get(alias), {
            'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "*** Begin Patch"',
        })

    def test_client_tools_not_filtered_by_namespace_or_name(self):
        payload = {'model': 'claude-code-opus', 'input': 'test', 'tools': [
            {'type': 'namespace', 'name': namespace, 'tools': [{
                'type': 'function', 'name': name, 'parameters': {'type': 'object', 'properties': {}},
            }]} for namespace, name in [('functions', 'exec'), ('mcp__codex_app', 'list_threads'), ('mcp__cua_repl', 'js'), ('collaboration', 'spawn_agent')]
        ]}
        chat, mapping = prepare(payload)
        self.assertEqual({(v['namespace'], v['name']) for v in mapping.values()}, {
            ('functions', 'exec'), ('mcp__codex_app', 'list_threads'), ('mcp__cua_repl', 'js'), ('collaboration', 'spawn_agent'),
        })
        self.assertEqual(len(chat['tools']), 4)

    def test_namespace_instructions_are_not_lost(self):
        payload = {'model': 'claude-code-opus', 'input': 'test', 'tools': [{
            'type': 'namespace', 'name': 'browser', 'description': 'First call must select the browser before taking a snapshot.',
            'tools': [{'type': 'function', 'name': 'run', 'parameters': {'type': 'object'}}],
        }]}
        chat, _ = prepare(payload)
        self.assertIn('First call must select the browser', json.dumps(chat))

    def test_selected_tool_cannot_be_replaced_with_plain_text(self):
        payload = {'model': 'claude-code-opus', 'input': 'read', 'tools': [
            {'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}},
        ], 'tool_choice': {'type': 'function', 'name': 'read'}}
        _, mapping = prepare(payload)
        with self.assertRaises(ValueError):
            convert({'structured_output': {'text': 'skipped', 'calls': []}}, payload, mapping)

    def test_old_web_search_is_preserved_as_history_not_reexecuted(self):
        payload = {'model': 'claude-code-opus', 'input': [{
            'type': 'web_search_call', 'id': 'ws_old', 'status': 'completed',
            'action': {'type': 'search', 'query': 'example prior query'},
        }]}
        chat, _ = prepare(payload)
        self.assertEqual(chat['messages'][-1]['role'], 'user')
        self.assertIn('example prior query', str(chat['messages'][-1]['content']))
        self.assertIn('Untrusted historical', str(chat['messages'][-1]['content']))
        self.assertFalse(chat.get('tools'))

    def test_encrypted_context_is_not_silently_dropped(self):
        for item in [
            {'type': 'compaction', 'encrypted_content': 'private-ciphertext'},
            {'type': 'agent_message', 'author': 'manager', 'recipient': 'worker', 'content': [{'type': 'encrypted_content', 'encrypted_content': 'private-ciphertext'}]},
        ]:
            with self.subTest(kind=item['type']), self.assertRaisesRegex(VisionError, 'readable summary'):
                prepare({'model': 'claude-code-opus', 'input': [item]})

    def test_client_tool_discovery_round_trip(self):
        search = {'type': 'tool_search', 'execution': 'client', 'description': 'Find tools.',
                  'parameters': {'type': 'object', 'properties': {'goal': {'type': 'string'}}, 'required': ['goal']}}
        payload = {'model': 'claude-code-opus', 'input': 'find a tool', 'tools': [search]}
        _, mapping = prepare(payload)
        alias = next(k for k, v in mapping.items() if v['type'] == 'tool_search')
        response = convert({'structured_output': {'text': '', 'calls': [{'name': alias, 'arguments': '{"goal":"Find the browser"}'}]}}, payload, mapping)
        call = response['output'][0]
        self.assertEqual(call['type'], 'tool_search_call')
        self.assertEqual(call['execution'], 'client')
        self.assertEqual(call['arguments'], {'goal': 'Find the browser'})
        self.assertNotIn('name', call)
        payload['input'] = [call, {'type': 'tool_search_output', 'execution': 'client', 'call_id': call['call_id'], 'status': 'completed', 'tools': [{
            'type': 'namespace', 'name': 'browser', 'description': 'Select the browser first.',
            'tools': [{'type': 'function', 'name': 'open', 'parameters': {'type': 'object'}}],
        }]}]
        chat, loaded = prepare(payload)
        self.assertIn(('browser', 'open'), {(v['namespace'], v['name']) for v in loaded.values()})
        self.assertEqual(chat['messages'][-1]['role'], 'tool')
        self.assertIn('Select the browser first.', json.dumps(chat))


if __name__ == '__main__':
    unittest.main()
