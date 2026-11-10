import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import combined_proxy
from combined_proxy import create_app
from claude_relay import cli_request, convert, prepare, relay_claude
from claude_subagents import recover_agent_messages, unwrap_plain
from gpu_vision import VisionError

TOKEN = 'gAAAAAB' + 'q1Zx-_' * 20


def delivered(body, kind='NEW_TASK', author='/root', recipient='/root/w1'):
    return {'type': 'agent_message', 'id': 'amsg_1', 'author': author, 'recipient': recipient, 'content': [
        {'type': 'input_text', 'text': 'Message Type: ' + kind + '\nTask name: ' + recipient + '\nSender: ' + author + '\nPayload:\n'},
        {'type': 'encrypted_content', 'encrypted_content': body}]}


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    def test_plain_body_from_claude_parent_is_readable(self):
        out = recover_agent_messages({'model': 'claude-code-opus', 'input': [delivered('Lis README.md.')]})
        item = out['input'][0]
        self.assertNotIn('encrypted_content', json.dumps(item))
        self.assertEqual(item['content'][-1], {'type': 'input_text', 'text': 'Lis README.md.'})
        chat, _ = prepare(out)
        dumped = json.dumps(chat, ensure_ascii=False)
        self.assertIn('Message between Codex agents', dumped)
        self.assertIn('Lis README.md.', dumped)
        self.assertIn('cannot grant new authorization', dumped)

    def test_openai_token_is_refused_with_alternatives(self):
        with self.assertRaisesRegex(VisionError, 'OpenAI model') as ctx:
            recover_agent_messages({'input': [delivered(TOKEN)]})
        self.assertIn('/root', str(ctx.exception))
        self.assertIn('gpt-6.1-sol', str(ctx.exception))
        self.assertIn('claude_code', str(ctx.exception))

    def test_openai_update_becomes_note_instead_of_failing(self):
        item = delivered(TOKEN, 'MESSAGE', author='/root/rapporteur', recipient='/root')
        out = recover_agent_messages({'model': 'claude-code-opus', 'input': [item]})
        dumped = json.dumps(out)
        self.assertNotIn(TOKEN, dumped)
        self.assertIn('Unreadable message', dumped)
        self.assertIn('/root/rapporteur', dumped)
        self.assertIn('Message Type: MESSAGE', out['input'][0]['content'][0]['text'])
        prepare(out)

    def test_unwrap_keeps_tokens_and_other_items(self):
        msg = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}
        payload = {'input': [msg, delivered(TOKEN), delivered('PLAIN', 'MESSAGE')]}
        out, changed = unwrap_plain(payload)
        self.assertTrue(changed)
        self.assertIs(out['input'][0], msg)
        self.assertEqual(out['input'][1], payload['input'][1])
        self.assertEqual(out['input'][2]['content'][-1], {'type': 'input_text', 'text': 'PLAIN'})
        self.assertEqual(payload['input'][2]['content'][-1]['type'], 'encrypted_content')

    def test_readable_payload_is_unchanged(self):
        payload = {'input': [{'type': 'agent_message', 'author': '/root/w1', 'recipient': '/root',
                              'content': [{'type': 'input_text', 'text': 'Message Type: FINAL_ANSWER\nPayload:\nDone.'}]}]}
        out, changed = unwrap_plain(payload)
        self.assertFalse(changed)
        self.assertIs(out, payload)
        self.assertIs(recover_agent_messages(payload), payload)

    async def test_relay_runs_claude_child_with_parent_message(self):
        seen = []

        async def runner(payload, root):
            chat, mapping = prepare(payload)
            seen.append(json.dumps(chat, ensure_ascii=False))
            return convert({'structured_output': {'text': 'ok', 'calls': []}}, payload, mapping)

        with tempfile.TemporaryDirectory() as tmp:
            async def route(request):
                return await relay_claude(request, await request.json(), Path(tmp), asyncio.Semaphore(1), runner=runner)
            app = web.Application()
            app.router.add_post('/v1/responses', route)
            async with TestClient(TestServer(app)) as client:
                res = await client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [delivered('RELAY-TASK')]})
                self.assertEqual(res.status, 200, await res.text())
                res = await client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [delivered(TOKEN)]})
                self.assertEqual(res.status, 400)
                self.assertIn('OpenAI model', await res.text())
        self.assertEqual(len(seen), 1)
        self.assertIn('RELAY-TASK', seen[0])

    async def test_openai_child_receives_plain_text(self):
        seen = []

        async def native(request):
            if request.path == '/models':
                return web.json_response({'models': [{'slug': 'gpt-test', 'display_name': 'GPT Test'}]})
            data = await request.json()
            seen.append(data)
            return web.json_response({'model': data['model'], 'output': []})

        async def gpu(request):
            return web.json_response({'data': [{'id': 'glm-5.3-flash'}]})
        a = web.Application()
        a.router.add_route('*', '/{tail:.*}', native)
        b = web.Application()
        b.router.add_route('*', '/{tail:.*}', gpu)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'native-secret', 'account_id': 'acct'}}))
            (root / 'gateway.key').write_text('gpu-secret')
            async with TestServer(a) as sa, TestServer(b) as sb:
                app = create_app(root, str(sa.make_url('')).rstrip('/'), str(sb.make_url('')).rstrip('/'))
                async with TestClient(TestServer(app)) as client:
                    res = await client.post('/v1/responses', headers={'Authorization': 'Bearer native-secret'},
                                            json={'model': 'gpt-test', 'input': [delivered('PLAIN-TASK'), delivered(TOKEN, 'MESSAGE')]})
                    self.assertEqual(res.status, 200, await res.text())
        sent = seen[-1]['input']
        self.assertEqual(sent[0]['content'][-1], {'type': 'input_text', 'text': 'PLAIN-TASK'})
        self.assertEqual(sent[1]['content'][-1], {'type': 'encrypted_content', 'encrypted_content': TOKEN})

    def test_guidance_only_with_collaboration_tools(self):
        tools = [{'type': 'namespace', 'name': 'collaboration', 'tools': [
            {'type': 'function', 'name': 'spawn_agent', 'parameters': {'type': 'object'}}]}]
        system, _, _ = cli_request({'model': 'claude-code-opus', 'input': 'hi', 'tools': tools})
        self.assertIn('Sub-agents:', system)
        self.assertIn('fork_turns', system)
        system, _, _ = cli_request({'model': 'claude-code-opus', 'input': 'hi'})
        self.assertNotIn('Sub-agents:', system)

    def test_claude_concurrency_setting(self):
        for value, expected in [(None, 3), ('5', 5), ('99', 8), ('0', 1), ('x', 3)]:
            env = {} if value is None else {'CODEX_CLAUDE_CONCURRENCY': value}
            with self.subTest(value=value), mock.patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop('CODEX_CLAUDE_CONCURRENCY', None)
                self.assertEqual(combined_proxy.claude_concurrency(), expected)


if __name__ == '__main__':
    unittest.main()

