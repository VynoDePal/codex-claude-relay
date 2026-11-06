"""Relay checkpoints must become readable text before reaching OpenAI or the GPU gateway."""
import tempfile
import unittest
from pathlib import Path

from claude_compaction import checkpoint
from combined_proxy import restore_claude_checkpoints


class ForeignCheckpointTest(unittest.TestCase):
    def test_relay_checkpoint_is_replaced_by_summary(self):
        with tempfile.TemporaryDirectory() as root:
            done = checkpoint({'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'RESUME_42'}]}]}, root)
            item = done['output'][0]
            openai_item = {'type': 'compaction', 'encrypted_content': 'gAAAA-openai'}
            payload = {'model': 'gpt-test', 'input': [openai_item, item, {'role': 'user', 'content': 'continue'}]}
            work, changed = restore_claude_checkpoints(payload, Path(root))
            self.assertTrue(changed)
            self.assertEqual(work['input'][0], openai_item)
            self.assertEqual(work['input'][1]['role'], 'user')
            self.assertIn('RESUME_42', work['input'][1]['content'][0]['text'])
            self.assertEqual(payload['input'][1], item)

    def test_payload_without_relay_checkpoint_is_untouched(self):
        payload = {'model': 'gpt-test', 'input': [{'type': 'compaction', 'encrypted_content': 'gAAAA'}]}
        work, changed = restore_claude_checkpoints(payload, Path('/nonexistent'))
        self.assertFalse(changed)
        self.assertIs(work, payload)


if __name__ == '__main__':
    unittest.main()

