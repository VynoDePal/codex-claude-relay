"""additional_tools history items become callable definitions, never conversation items."""
import json
import tempfile
import unittest
from pathlib import Path

import claude_history
import claude_relay
import gemini_relay
from claude_tools import normalize

EXTRA = {'type': 'additional_tools', 'id': 'at_1', 'role': 'developer', 'tools': [
    {'type': 'namespace', 'name': 'clock', 'description': 'Time tools', 'tools': [
        {'type': 'function', 'name': 'sleep', 'description': 'Sleep',
         'parameters': {'type': 'object', 'properties': {'ms': {'type': 'number'}}}}]},
    {'type': 'function', 'name': 'exec', 'description': 'OLD exec',
     'parameters': {'type': 'object', 'properties': {}}},
]}


def payload(model):
    return {'model': model, 'reasoning': {'effort': 'high'}, 'input': [
        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'bonjour'}]},
        EXTRA,
        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'continue'}]},
    ], 'tools': [{'type': 'function', 'name': 'exec', 'description': 'CURRENT exec',
                  'parameters': {'type': 'object', 'properties': {}}}]}


class AdditionalToolsTest(unittest.TestCase):
    def test_normalize_merges_definitions_and_drops_item(self):
        work, _ = normalize(payload('claude-code-opus'))
        self.assertFalse(any(i.get('type') == 'additional_tools' for i in work['input']))
        self.assertEqual(len(work['input']), 2)
        names = {t['name']: t for t in work['tools']}
        self.assertEqual(names['exec']['description'], 'CURRENT exec')
        self.assertEqual(names['clock']['tools'][0]['name'], 'sleep')

    def test_claude_prepare_accepts_item(self):
        chat, mapping = claude_relay.prepare(payload('claude-code-opus'))
        self.assertNotIn('additional_tools', json.dumps(chat['messages']))
        self.assertTrue(any('sleep' in json.dumps(t) for t in chat.get('tools', [])))

    def test_gemini_prepare_accepts_item(self):
        chat, mapping = gemini_relay.prepare(payload('gemini-4-argon'))
        self.assertNotIn('additional_tools', json.dumps(chat['messages']))

    def test_cached_archive_rows_are_filtered(self):
        with tempfile.TemporaryDirectory() as root:
            thread = '30000000-0000-4000-8000-000000000001'
            token = 'gAAAA-token'
            import hashlib
            digest = hashlib.sha256(token.encode()).hexdigest()
            folder = Path(root) / 'claude-selector/history'
            folder.mkdir(parents=True)
            identity = {'archive_version': claude_history.ARCHIVE_VERSION, 'thread_id': thread, 'checkpoint_sha256': digest}
            rows = [identity,
                    {'ordinal': 1, 'item': {'type': 'message', 'role': 'user', 'content': 'hi'}},
                    {'ordinal': 2, 'item': EXTRA}]
            (folder / (thread + '-' + digest + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
            _, got, _ = claude_history.archive(root, thread, token)
            self.assertEqual([r['ordinal'] for r in got], [1])


if __name__ == '__main__':
    unittest.main()

