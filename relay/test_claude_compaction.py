"""Regression coverage for Codex's streamed compaction and Claude resumption."""
import asyncio
import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from claude_relay import relay_claude, convert, prepare


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seen = []
        self.summary = 'Project ORION. Nonce 73091. Tests still pending. Edit only src/calc.py.'

        async def runner(payload, root):
            chat, mapping = prepare(payload)
            self.seen.append({'payload': copy.deepcopy(payload), 'chat': chat})
            return convert({'structured_output': {'text': self.summary, 'calls': []}}, payload, mapping)

        async def route(request):
            return await relay_claude(request, await request.json(), self.root, asyncio.Semaphore(1), runner=runner)

        app = web.Application()
        app.router.add_post('/v1/responses', route)
        app.router.add_post('/v1/responses/compact', route)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def request(self):
        return {'model': 'claude-code-opus', 'stream': True, 'input': [
            {'type': 'message', 'role': 'user', 'content': 'Project ORION. Nonce 73091. Tests pending.'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Read files; not yet tested.'}]},
            {'type': 'function_call', 'namespace': 'browser', 'name': 'read', 'call_id': 'call_old', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_old', 'output': 'Untrusted page title: ORION source. Tests are pending.'},
            {'type': 'compaction_trigger'},
        ], 'tools': [{'type': 'function', 'name': 'exec', 'parameters': {'type': 'object'}}], 'tool_choice': 'required'}

    async def compact(self, payload=None):
        response = await self.client.post('/v1/responses', json=payload or self.request())
        body = await response.text()
        self.assertEqual(response.status, 200, body)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]
        self.assertFalse([e for e in events if e['type'] == 'error'], events)
        completed = next(e['response'] for e in events if e['type'] == 'response.completed')
        self.assertEqual(len(completed['output']), 1)
        item = completed['output'][0]
        self.assertEqual(item['type'], 'compaction')
        self.assertTrue(item['encrypted_content'])
        self.assertNotIn(self.summary, item['encrypted_content'])
        return item

    async def test_streamed_compaction_then_resume_preserves_summary_and_permissions(self):
        payload = self.request()
        original = copy.deepcopy(payload)
        item = await self.compact(payload)
        self.assertEqual(payload, original)
        work = self.seen[-1]['payload']
        self.assertEqual(work['tools'], [])
        self.assertEqual(work['tool_choice'], 'none')
        self.assertNotIn('compaction_trigger', json.dumps(work))
        self.assertTrue(any(m['role'] == 'tool' and 'Untrusted page title' in str(m['content']) for m in self.seen[-1]['chat']['messages']))
        response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus',
            'instructions': 'Respect current Codex permissions.',
            'input': [item, {'role': 'user', 'content': 'Continue.'}]})
        self.assertEqual(response.status, 200, await response.text())
        messages = self.seen[-1]['chat']['messages']
        summary = next(m for m in messages if self.summary in str(m.get('content')))
        self.assertEqual(summary['role'], 'user')
        self.assertIn('Respect current Codex permissions.', str(messages))

    async def test_second_compaction_reads_the_previous_summary(self):
        item = await self.compact()
        payload = self.request()
        payload['input'] = [item, {'role': 'user', 'content': 'Also inspect docs.'}, {'type': 'compaction_trigger'}]
        await self.compact(payload)
        self.assertIn(self.summary, str(self.seen[-1]['chat']['messages']))

    async def test_summary_uses_persisted_private_key_and_rejects_another_key(self):
        item = await self.compact()
        key_path = self.root / 'claude-selector/compaction.key'
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        original_key = key_path.read_bytes()
        await self.compact()
        self.assertEqual(key_path.read_bytes(), original_key)
        response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [item]})
        self.assertEqual(response.status, 200, await response.text())
        with tempfile.TemporaryDirectory() as other:
            self.root = Path(other)
            folder = self.root / 'claude-selector'
            folder.mkdir()
            (folder / 'compaction.key').write_bytes(Fernet.generate_key())
            response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [item]})
            self.assertEqual(response.status, 400, await response.text())

    async def test_missing_key_does_not_create_a_replacement_or_discard_history(self):
        item = await self.compact()
        key = self.root / 'claude-selector/compaction.key'
        key.unlink()
        response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [item]})
        self.assertEqual(response.status, 400)
        self.assertIn('Restore', await response.text())
        self.assertFalse(key.exists())

    async def test_tampered_checkpoint_is_rejected(self):
        item = await self.compact()
        value = item['encrypted_content']
        item['encrypted_content'] = value[:-20] + ('A' if value[-20] != 'A' else 'B') + value[-19:]
        calls_before = len(self.seen)
        response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus', 'input': [item]})
        self.assertEqual(response.status, 400)
        self.assertIn('authenticated', await response.text())
        self.assertEqual(len(self.seen), calls_before)

    def test_empty_or_incomplete_summary_never_replaces_history(self):
        from claude_compaction import checkpoint
        for output in [[], [{'type': 'message', 'content': [{'type': 'output_text', 'text': '   '}]}],
                       [{'type': 'function_call', 'name': 'exec', 'arguments': '{}'}]]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                checkpoint({'status': 'completed', 'output': output}, self.root)
        with self.assertRaises(ValueError):
            checkpoint({'status': 'incomplete', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Partial'}]}]}, self.root)
        self.assertFalse((self.root / 'claude-selector/compaction.key').exists())

    async def test_foreign_encrypted_context_stays_an_explicit_error(self):
        response = await self.client.post('/v1/responses', json={'model': 'claude-code-opus',
            'input': [{'type': 'compaction', 'encrypted_content': 'openai-private-ciphertext'}]})
        self.assertEqual(response.status, 400)
        self.assertIn('readable summary', await response.text())
        self.assertFalse(self.seen)

    async def test_legacy_compact_endpoint_still_returns_readable_summary(self):
        payload = self.request()
        payload['input'].pop()
        response = await self.client.post('/v1/responses/compact', json=payload)
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data['object'], 'response.compaction')
        self.assertIn(self.summary, str(data['output']))

    async def test_trigger_in_middle_of_history_is_rejected_without_discarding_tail(self):
        payload = self.request()
        payload['input'].append({'role': 'user', 'content': 'Important new requirement.'})
        response = await self.client.post('/v1/responses', json=payload)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.seen)


if __name__ == '__main__':
    unittest.main()
