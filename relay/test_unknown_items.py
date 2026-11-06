"""Unknown Codex history item types must not fail Claude/Gemini turns."""
import json
import unittest

import claude_relay
import gemini_relay
from claude_tools import MAX_UNKNOWN_CHARS, normalize

FUTURE = {'type': 'future_item_kind', 'id': 'fi_1', 'note': 'x' * (MAX_UNKNOWN_CHARS * 2), 'encrypted_content': 'secret'}


def payload(model):
    return {'model': model, 'reasoning': {'effort': 'high'}, 'tools': [], 'input': [
        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'bonjour'}]},
        FUTURE,
        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'continue'}]},
    ]}


class UnknownItemsTest(unittest.TestCase):
    def test_unknown_item_becomes_bounded_text(self):
        work, _ = normalize(payload('claude-code-opus'))
        rendered = json.dumps(work['input'][1])
        self.assertEqual(work['input'][1]['role'], 'user')
        self.assertIn('future_item_kind', rendered)
        self.assertNotIn('secret', rendered)
        self.assertLess(len(rendered), MAX_UNKNOWN_CHARS + 600)

    def test_claude_and_gemini_prepare_do_not_raise(self):
        for module, model in ((claude_relay, 'claude-code-opus'), (gemini_relay, 'gemini-4-argon')):
            chat, _ = module.prepare(payload(model))
            self.assertIn('future_item_kind', json.dumps(chat['messages']))


if __name__ == '__main__':
    unittest.main()

