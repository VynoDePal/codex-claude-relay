import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_relay import relay_claude, prepare, convert

THREAD = '10000000-0000-4000-8000-000000000001'
PARENT = '10000000-0000-4000-8000-000000000002'
TOKEN = 'opaque-openai-checkpoint-do-not-decrypt'


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'sessions/2026/10/13'
        self.folder.mkdir(parents=True)
        self.seen = []
        self.last_prepared = None
        self.after_summary = False
        self.db = sqlite3.connect(self.root / 'state_5.sqlite')
        self.db.execute('create table threads (id text, rollout_path text)')
        self.addCleanup(self.db.close)

        async def runner(payload, root):
            self.last_prepared = copy.deepcopy(payload)
            chat, mapping = prepare(payload)
            self.seen.append(chat)
            return convert({'structured_output': {'text': 'Recovered conversation accepted.', 'calls': []}}, payload, mapping)

        async def route(request):
            return await relay_claude(request, await request.json(), self.root, asyncio.Semaphore(1), runner=runner)
        app = web.Application()
        app.router.add_post('/v1/responses', route)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def save(self, tid, records, base=None):
        p = self.folder / ('rollout-test-' + tid + '.jsonl')
        meta = {'id': tid, 'history_mode': 'paginated'}
        if base: meta['history_base'] = base
        rows = [{'type': 'session_meta', 'ordinal': 0 if not base else base['end_ordinal_exclusive'], 'payload': meta}, *records]
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.db.execute('insert or replace into threads values (?,?)', (tid, str(p)))
        self.db.commit()
        return p

    def record(self, ordinal, item):
        return {'ordinal': ordinal, 'type': 'response_item', 'payload': item}

    def message(self, role, text):
        return {'type': 'message', 'role': role, 'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}

    def compaction(self, ordinal=5):
        return {'ordinal': ordinal, 'type': 'compacted', 'payload': {'message': '', 'replacement_history': [
            {'type': 'compaction', 'encrypted_content': TOKEN}]}}

    def history(self):
        return [self.record(1, self.message('developer', 'OLD_SYSTEM_INSTRUCTIONS')),
                self.record(2, self.message('user', 'Build project ORION.')),
                self.record(3, self.message('assistant', 'The recorded code is BRIDGE-93185; tests are pending.')),
                self.record(4, {'type': 'reasoning', 'encrypted_content': 'PRIVATE_REASONING_NEVER_INCLUDE'}),
                self.compaction(), self.record(6, self.message('assistant', 'AFTER_CHECKPOINT_NOT_PART_OF_SUMMARY'))]

    def request(self):
        return {'model': 'claude-code-opus', 'instructions': 'CURRENT_SYSTEM_INSTRUCTIONS', 'input': [
            {'type': 'compaction', 'encrypted_content': TOKEN}, self.message('user', 'Continue this task.')],
            'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}]}

    async def send(self, payload=None, headers=None):
        return await self.client.post('/v1/responses', json=payload or self.request(), headers=headers or {'thread-id': THREAD})

    async def test_switch_restores_visible_history_only_up_to_exact_checkpoint(self):
        original = self.save(THREAD, self.history()).read_bytes()
        response = await self.send()
        self.assertEqual(response.status, 200, await response.text())
        rendered = json.dumps(self.seen[-1])
        self.assertIn('BRIDGE-93185', rendered)
        self.assertIn('CURRENT_SYSTEM_INSTRUCTIONS', rendered)
        for excluded in ('OLD_SYSTEM_INSTRUCTIONS', 'PRIVATE_REASONING_NEVER_INCLUDE', 'AFTER_CHECKPOINT_NOT_PART_OF_SUMMARY', TOKEN):
            self.assertNotIn(excluded, rendered)
        self.assertEqual(self.last_prepared['tools'], self.request()['tools'])
        self.assertEqual((self.folder / ('rollout-test-' + THREAD + '.jsonl')).read_bytes(), original)

    async def test_fork_follows_physical_history_page_and_honors_cutoff(self):
        parent = self.save(PARENT, self.history())
        rows = parent.read_bytes().splitlines(keepends=True)
        cutoff = sum(map(len, rows[:6]))  # Includes matching checkpoint, not parent's next response.
        self.save(THREAD, [], {'thread_id': PARENT, 'end_ordinal_exclusive': 6, 'end_byte_offset': cutoff})
        response = await self.send()
        self.assertEqual(response.status, 200, await response.text())
        self.assertIn('BRIDGE-93185', json.dumps(self.seen[-1]))
        self.assertNotIn('AFTER_CHECKPOINT', json.dumps(self.seen[-1]))

    async def test_does_not_find_checkpoint_after_fork_boundary(self):
        parent = self.save(PARENT, self.history())
        cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:5]))
        self.save(THREAD, [], {'thread_id': PARENT, 'end_ordinal_exclusive': 5, 'end_byte_offset': cutoff})
        response = await self.send()
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)

    async def test_does_not_recover_from_an_unrelated_chat(self):
        self.save(PARENT, self.history())
        self.save(THREAD, [self.record(1, self.message('user', 'Unrelated task.'))])
        response = await self.send()
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)

    async def test_missing_thread_identity_never_scans_other_chats(self):
        self.save(THREAD, self.history())
        response = await self.client.post('/v1/responses', json=self.request())
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)

    async def test_archive_preserves_tool_evidence_and_bounds_large_inline_context(self):
        rows = self.history()
        rows.insert(3, self.record(3, {'type': 'function_call_output', 'call_id': 'test', 'output': 'EVIDENCE-FIRST\n' + 'large diagnostic output\n' * 30000 + 'EVIDENCE-LAST'}))
        self.save(THREAD, rows)
        response = await self.send()
        self.assertEqual(response.status, 200, await response.text())
        self.assertLess(len(json.dumps(self.seen[-1])), 600000)
        archives = list((self.root / 'claude-selector/history').glob('*.jsonl'))
        self.assertEqual(len(archives), 1)
        self.assertIn('EVIDENCE-FIRST', archives[0].read_text())
        self.assertIn('EVIDENCE-LAST', archives[0].read_text())
        self.assertIn(str(archives[0]), json.dumps(self.seen[-1]))
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)

    async def test_configuration_update_does_not_break_a_model_switch(self):
        self.save(THREAD, self.history())
        payload = self.request()
        payload['reasoning'] = {'effort': 'high'}
        payload['input'].append({'type': 'configuration_update', 'reasoning': {'effort': 'low'}})
        response = await self.send(payload)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.seen[-1]['reasoning_effort'], 'high')

    async def test_existing_claude_compaction_can_follow_a_recovered_openai_checkpoint(self):
        self.save(THREAD, self.history())
        payload = self.request()
        payload['input'].append({'type': 'compaction_trigger'})
        response = await self.send(payload)
        self.assertEqual(response.status, 200, await response.text())
        item = (await response.json())['output'][0]
        self.assertEqual(item['type'], 'compaction')
        self.assertTrue(item['encrypted_content'].startswith('claude-code-compaction-v1:'))

    async def test_torn_history_line_is_skipped_and_reported(self):
        path = self.save(THREAD, self.history())
        lines = path.read_text().splitlines(keepends=True)
        lines.insert(3, '{"timestamp":"2026-10-02T22:56:35.669Z","ordinal":3,"type":"response_item","pay\n')
        path.write_text(''.join(lines))
        for _ in range(2):  # Second request reuses the cached archive.
            response = await self.send()
            self.assertEqual(response.status, 200, await response.text())
            rendered = json.dumps(self.seen[-1])
            self.assertIn('BRIDGE-93185', rendered)
            self.assertIn('Skipped 1 unreadable line', rendered)

    async def test_unreadable_session_header_still_fails(self):
        path = self.save(THREAD, self.history())
        lines = path.read_text().splitlines(keepends=True)
        lines[0] = lines[0][:20] + '\n'
        path.write_text(''.join(lines))
        response = await self.send()
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)

    def compress(self, path):
        from compression import zstd
        target = path.with_name(path.name + '.zst')
        target.write_bytes(zstd.compress(path.read_bytes()))
        path.unlink()
        return target

    async def test_fork_reads_compressed_parent_page_with_uncompressed_cutoff(self):
        parent = self.save(PARENT, self.history())
        cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:6]))
        self.compress(parent)
        self.save(THREAD, [], {'thread_id': PARENT, 'end_ordinal_exclusive': 6, 'end_byte_offset': cutoff})
        response = await self.send()
        self.assertEqual(response.status, 200, await response.text())
        rendered = json.dumps(self.seen[-1])
        self.assertIn('BRIDGE-93185', rendered)
        self.assertNotIn('AFTER_CHECKPOINT', rendered)

    async def test_compressed_parent_still_respects_fork_boundary(self):
        parent = self.save(PARENT, self.history())
        cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:5]))
        self.compress(parent)
        self.save(THREAD, [], {'thread_id': PARENT, 'end_ordinal_exclusive': 5, 'end_byte_offset': cutoff})
        response = await self.send()
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)

    async def test_page_present_plain_and_compressed_is_not_ambiguous(self):
        parent = self.save(PARENT, self.history())
        cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:6]))
        plain = parent.read_bytes()
        self.compress(parent)
        parent.write_bytes(plain)
        self.save(THREAD, [], {'thread_id': PARENT, 'end_ordinal_exclusive': 6, 'end_byte_offset': cutoff})
        response = await self.send()
        self.assertEqual(response.status, 200, await response.text())
        self.assertIn('BRIDGE-93185', json.dumps(self.seen[-1]))


if __name__ == '__main__':
    unittest.main()
