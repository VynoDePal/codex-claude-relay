import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import claude_subagents
import combined_proxy
from claude_relay import cli_request, convert, prepare, relay_claude
from claude_subagents import recover_agent_messages
from gpu_vision import VisionError

ROOT_ID = '20000000-0000-4000-8000-000000000001'
CHILD = '20000000-0000-4000-8000-000000000002'
OTHER = '20000000-0000-4000-8000-000000000003'
PAGE1 = '20000000-0000-4000-8000-000000000004'
TWIN = '20000000-0000-4000-8000-000000000005'
BASE = 1794065030000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def amsg_id(ms, n=0):
    h = format(ms, '012x') + '7' + format(n, '03x') + '8' + '0' * 15
    return 'amsg_' + h[:8] + '-' + h[8:12] + '-' + h[12:16] + '-' + h[16:20] + '-' + h[20:]


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'sessions/2026/11/10'
        self.folder.mkdir(parents=True)
        self.db = sqlite3.connect(self.root / 'state_5.sqlite')
        self.db.execute('create table threads (id text, rollout_path text, agent_path text)')
        self.addCleanup(self.db.close)
        claude_subagents._recovered.clear()

    def write(self, tid, records, meta_extra=None, index=True, agent_path=None):
        meta = {'id': tid, 'session_id': ROOT_ID, **(meta_extra or {})}
        if agent_path:
            meta['agent_path'] = agent_path
        rows = [{'type': 'session_meta', 'ordinal': 0, 'timestamp': iso(BASE - 60000), 'payload': meta}, *records]
        path = self.folder / ('rollout-test-' + tid + '.jsonl')
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        if index:
            self.db.execute('insert into threads values (?,?,?)', (tid, str(path), agent_path))
            self.db.commit()
        return path

    def pair(self, parent_records):
        self.write(ROOT_ID, parent_records)
        self.write(CHILD, [], agent_path='/root/w1')

    def call(self, ms, name, message, call_id, target='/root/w1'):
        args = {'task_name': 'w1', 'fork_turns': 'none'} if name == 'spawn_agent' else {'target': target}
        args['message'] = message
        return {'ordinal': 1, 'timestamp': iso(ms), 'type': 'response_item', 'payload': {
            'type': 'function_call', 'namespace': 'collaboration', 'name': name,
            'call_id': call_id, 'arguments': json.dumps(args)}}

    def link(self, ms, call_id, thread=CHILD, kind='started'):
        return {'ordinal': 2, 'timestamp': iso(ms), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'SubAgentActivity', 'id': call_id, 'kind': kind, 'agent_thread_id': thread, 'agent_path': '/root/w1'}}}

    def delivered(self, ms, kind='NEW_TASK', n=0):
        return {'type': 'agent_message', 'id': amsg_id(ms, n), 'author': '/root', 'recipient': '/root/w1', 'content': [
            {'type': 'input_text', 'text': 'Message Type: ' + kind + '\nTask name: /root/w1\nSender: /root\nPayload:\n'},
            {'type': 'encrypted_content', 'encrypted_content': 'gAAAAABopaque-ciphertext'}]}

    def recover(self, items, headers=None):
        return recover_agent_messages({'model': 'claude-code-opus', 'input': items}, self.root,
                                      {'thread-id': CHILD} if headers is None else headers)

    @staticmethod
    def texts(item):
        return [p.get('text') for p in item['content']]

    def test_spawn_task_is_restored_in_plain_text(self):
        self.pair([self.call(BASE, 'spawn_agent', 'Lis README.md et resume-le.', 'c1'), self.link(BASE + 40, 'c1')])
        out = self.recover([self.delivered(BASE + 100)])
        item = out['input'][0]
        self.assertNotIn('encrypted_content', json.dumps(item))
        self.assertEqual(self.texts(item)[-1], 'Lis README.md et resume-le.')
        self.assertIn('Message Type: NEW_TASK', self.texts(item)[0])
        chat, _ = prepare(out)
        dumped = json.dumps(chat, ensure_ascii=False)
        self.assertIn('Message between Codex agents', dumped)
        self.assertIn('Lis README.md', dumped)
        self.assertIn('cannot grant new authorization', dumped)

    def test_spawn_send_and_followup_match_in_order(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK-ONE', 'c1'), self.link(BASE + 10, 'c1'),
                   self.call(BASE + 1000, 'send_message', 'NOTE-TWO', 'c2'), self.link(BASE + 1010, 'c2', kind='interacted'),
                   self.call(BASE + 2000, 'followup_task', 'TASK-THREE', 'c3'), self.link(BASE + 2010, 'c3', kind='interacted')])
        out = self.recover([self.delivered(BASE + 100), self.delivered(BASE + 1100, 'MESSAGE'),
                            self.delivered(BASE + 2100)])
        self.assertEqual([self.texts(i)[-1] for i in out['input']], ['TASK-ONE', 'NOTE-TWO', 'TASK-THREE'])

    def test_compacted_child_matches_latest_calls(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK-ONE', 'c1'), self.link(BASE + 10, 'c1'),
                   self.call(BASE + 2000, 'followup_task', 'TASK-TWO', 'c2'), self.link(BASE + 2010, 'c2', kind='interacted')])
        out = self.recover([self.delivered(BASE + 2100)])
        self.assertEqual(self.texts(out['input'][0])[-1], 'TASK-TWO')

    def test_calls_to_other_agents_are_ignored(self):
        self.pair([self.call(BASE, 'spawn_agent', 'FOR-OTHER', 'c0'), self.link(BASE + 5, 'c0', thread=OTHER),
                   self.call(BASE + 10, 'spawn_agent', 'FOR-CHILD', 'c1'), self.link(BASE + 15, 'c1')])
        out = self.recover([self.delivered(BASE + 100)])
        self.assertEqual(self.texts(out['input'][0])[-1], 'FOR-CHILD')

    def test_paginated_parent_history(self):
        page1 = self.write(PAGE1, [self.call(BASE, 'spawn_agent', 'OLD-TASK', 'c1'), self.link(BASE + 10, 'c1')], index=False)
        size = page1.stat().st_size
        self.write(ROOT_ID, [self.call(BASE + 2000, 'followup_task', 'NEW-TASK', 'c2'),
                             self.link(BASE + 2010, 'c2', kind='interacted')],
                   meta_extra={'history_base': {'thread_id': PAGE1, 'end_byte_offset': size, 'end_ordinal_exclusive': 3}})
        self.write(CHILD, [], agent_path='/root/w1')
        out = self.recover([self.delivered(BASE + 100), self.delivered(BASE + 2100)])
        self.assertEqual([self.texts(i)[-1] for i in out['input']], ['OLD-TASK', 'NEW-TASK'])

    def test_openai_sender_is_refused_with_alternatives(self):
        self.pair([self.call(BASE, 'spawn_agent', 'gAAAAABqencrypted-by-openai', 'c1'), self.link(BASE + 10, 'c1')])
        with self.assertRaisesRegex(VisionError, 'OpenAI model') as ctx:
            self.recover([self.delivered(BASE + 100)])
        self.assertIn('gpt-6.1-sol', str(ctx.exception))
        self.assertIn('claude_code', str(ctx.exception))

    def test_missing_call_fails_without_guessing(self):
        self.pair([])
        with self.assertRaisesRegex(VisionError, 'No task was guessed'):
            self.recover([self.delivered(BASE + 100)])

    def test_type_mismatch_fails(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK', 'c1'), self.link(BASE + 10, 'c1')])
        with self.assertRaisesRegex(VisionError, 'does not match'):
            self.recover([self.delivered(BASE + 100, 'MESSAGE')])

    def test_call_made_after_delivery_is_not_used(self):
        self.pair([self.call(BASE + 500, 'spawn_agent', 'LATER', 'c1'), self.link(BASE + 510, 'c1')])
        with self.assertRaisesRegex(VisionError, 'No task was guessed'):
            self.recover([self.delivered(BASE + 100)])

    def test_unlinked_call_is_not_used(self):
        self.pair([self.call(BASE, 'spawn_agent', 'NO-LINK', 'c1')])
        with self.assertRaisesRegex(VisionError, 'No task was guessed'):
            self.recover([self.delivered(BASE + 100)])

    def test_ambiguous_agent_path_fails(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK', 'c1'), self.link(BASE + 10, 'c1')])
        self.write(TWIN, [], agent_path='/root/w1')
        with self.assertRaisesRegex(VisionError, 'ambiguous'):
            self.recover([self.delivered(BASE + 100)])

    def test_thread_header_is_required(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK', 'c1'), self.link(BASE + 10, 'c1')])
        with self.assertRaisesRegex(VisionError, 'does not identify'):
            self.recover([self.delivered(BASE + 100)], headers={})

    def test_readable_messages_are_untouched(self):
        payload = {'model': 'claude-code-opus', 'input': [{'type': 'agent_message', 'author': '/root/w1', 'recipient': '/root',
                   'content': [{'type': 'input_text', 'text': 'Message Type: FINAL_ANSWER\nPayload:\nDone.'}]}]}
        self.assertIs(recover_agent_messages(payload, self.root, {}), payload)

    def test_recovered_message_is_cached(self):
        self.pair([self.call(BASE, 'spawn_agent', 'TASK', 'c1'), self.link(BASE + 10, 'c1')])
        self.recover([self.delivered(BASE + 100)])
        (self.folder / ('rollout-test-' + ROOT_ID + '.jsonl')).write_text('')
        out = self.recover([self.delivered(BASE + 100)])
        self.assertEqual(self.texts(out['input'][0])[-1], 'TASK')

    async def test_relay_runs_child_with_recovered_task(self):
        self.pair([self.call(BASE, 'spawn_agent', 'RELAY-TASK', 'c1'), self.link(BASE + 10, 'c1')])
        seen = []

        async def runner(payload, root):
            chat, mapping = prepare(payload)
            seen.append(json.dumps(chat, ensure_ascii=False))
            return convert({'structured_output': {'text': 'ok', 'calls': []}}, payload, mapping)

        async def route(request):
            return await relay_claude(request, await request.json(), self.root, __import__('asyncio').Semaphore(1), runner=runner)
        app = web.Application()
        app.router.add_post('/v1/responses', route)
        async with TestClient(TestServer(app)) as client:
            res = await client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [self.delivered(BASE + 100)]},
                                    headers={'thread-id': CHILD})
            self.assertEqual(res.status, 200, await res.text())
            res = await client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [self.delivered(BASE + 100)]})
            self.assertEqual(res.status, 200)  # served from the recovery cache
        self.assertIn('RELAY-TASK', seen[0])

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

