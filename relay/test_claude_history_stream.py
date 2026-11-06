"""Long histories: streamed replay, archive written to disk, bounded excerpt."""
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_history as h
from gpu_vision import VisionError

THREAD = '20000000-0000-4000-8000-000000000001'
TOKEN = 'opaque-openai-checkpoint-stream-test'


class StreamedArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        folder = self.root / 'sessions/2026/10/30'
        folder.mkdir(parents=True)
        rows = [{'type': 'session_meta', 'ordinal': 0, 'payload': {'id': THREAD}}]
        n = 1
        for i in range(400):
            rows.append({'ordinal': n, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant' if i % 2 else 'user',
                         'content': [{'type': 'input_text', 'text': f'MSG-{i}| ' + 'x' * 2000}]}})
            rows.append({'ordinal': n + 1, 'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': f'c{i}',
                         'output': f'OUT-{i}| ' + 'y' * 3000}})
            n += 2
        rows.append({'ordinal': n, 'type': 'compacted', 'payload': {'message': '', 'replacement_history': [
            {'type': 'compaction', 'encrypted_content': TOKEN}]}})
        self.rollout = folder / ('rollout-test-' + THREAD + '.jsonl')
        self.rollout.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        db = sqlite3.connect(self.root / 'state_5.sqlite')
        db.execute('create table threads (id text, rollout_path text)')
        db.execute('insert into threads values (?,?)', (THREAD, str(self.rollout)))
        db.commit()
        db.close()
        self.history = self.root / 'claude-selector/history'

    def test_limit_allows_long_chats(self):
        self.assertGreaterEqual(h.MAX_READ_BYTES, 4 * 1024 ** 3)
        self.assertTrue(inspect.isgeneratorfunction(h.replay))

    def test_streamed_excerpt_matches_in_memory_excerpt(self):
        with mock.patch.object(h, 'MAX_INLINE_CHARS', 50000):
            path, skipped = h.build_archive(self.root, THREAD, TOKEN)
            streamed = h.excerpt_from_archive(path, skipped)
            _, rows, _ = h.archive(self.root, THREAD, TOKEN)
            self.assertEqual(streamed, h.excerpt(path, rows, skipped))
        text = streamed['content'][0]['text']
        self.assertEqual(len(rows), 800)
        self.assertIn('MSG-399|', text)
        self.assertNotIn('MSG-0|', text)
        self.assertIn('of 800 visible records', text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.history.iterdir()), [path.name])

    def test_archive_is_reused_without_the_rollout(self):
        path, _ = h.build_archive(self.root, THREAD, TOKEN)
        self.rollout.unlink()
        self.assertEqual(h.build_archive(self.root, THREAD, TOKEN)[0], path)

    def test_read_limit_still_applies_and_leaves_no_temporary_files(self):
        with mock.patch.object(h, 'MAX_READ_BYTES', self.rollout.stat().st_size // 2):
            with self.assertRaises(VisionError) as caught:
                h.build_archive(self.root, THREAD, TOKEN)
        self.assertIn('recovery size limit', str(caught.exception))
        self.assertEqual(list(self.history.iterdir()), [])

    def test_recover_compactions_uses_the_streamed_archive(self):
        payload = {'input': [{'type': 'compaction', 'encrypted_content': TOKEN}, {'role': 'user', 'content': 'continue'}]}
        work = h.recover_compactions(payload, self.root, {'thread-id': THREAD})
        self.assertIn('MSG-399|', work['input'][0]['content'][0]['text'])
        self.assertEqual(work['input'][1], {'role': 'user', 'content': 'continue'})


if __name__ == '__main__':
    unittest.main()

