"""Gemini relay compacts oversized history itself instead of failing."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gemini_relay as gemini
import test_gemini_relay as fixtures


def block(i, size=20000):
    return 'Conversation message ' + str(i) + ':\n' + json.dumps({'role': 'user', 'content': 'MARK_' + str(i) + ' ' + 'x' * size})


class FitConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []

    async def summarize(self, previous, chunk):
        self.calls.append((previous, chunk))
        self.assertLess(len((previous + chunk).encode()), gemini.MAX_CONVERSATION_BYTES)
        marks = sorted(set(re.findall(r'(?:MARK|SUMMARY_OF)_(\d+)', previous + ' ' + chunk)), key=int)
        return 'SUMMARY_OF ' + ' '.join('SUMMARY_OF_' + m for m in marks)

    async def test_small_conversation_is_verbatim_without_summary(self):
        blocks = [block(i, 1000) for i in range(5)]
        text = await gemini.fit_conversation(blocks, None, self.root, self.summarize)
        self.assertEqual(text, '\n\n'.join(blocks))
        self.assertEqual(self.calls, [])

    async def test_large_history_is_compacted_and_latest_kept_verbatim(self):
        blocks = [block(i) for i in range(40)]
        settings = gemini.SETTINGS_PREFIX + '{"tool_choice": "none"}'
        text = await gemini.fit_conversation(blocks, settings, self.root, self.summarize)
        self.assertLessEqual(len(text.encode()), gemini.MAX_CONVERSATION_BYTES)
        self.assertTrue(text.startswith(gemini.SUMMARY_HEADER))
        self.assertIn(blocks[-1], text)
        self.assertTrue(text.endswith(settings))
        self.assertIn('SUMMARY_OF_0', text, 'The oldest history must survive in the summary')
        self.assertNotIn(blocks[0], text)
        self.assertTrue(self.calls)
        first = len(self.calls)
        again = await gemini.fit_conversation(blocks, settings, self.root, self.summarize)
        self.assertEqual(again, text)
        self.assertEqual(len(self.calls), first, 'Cached summaries must be reused')
        more = blocks + [block(40), block(41)]
        await gemini.fit_conversation(more, None, self.root, self.summarize)
        self.assertLessEqual(len(self.calls) - first, 2, 'Earlier chunks must stay cached after new turns')

    async def test_huge_single_old_block_is_split_into_chunks(self):
        blocks = [block(0, 400000), block(1, 100)]
        text = await gemini.fit_conversation(blocks, None, self.root, self.summarize)
        self.assertLessEqual(len(text.encode()), gemini.MAX_CONVERSATION_BYTES)
        self.assertIn(blocks[-1], text)
        self.assertGreaterEqual(len(self.calls), 3)
        self.assertIn('[Conversation block split by the Gemini transport: part', text)
        self.assertIn('SUMMARY_OF_0', text)

    async def test_oversized_latest_message_still_fails_without_inference(self):
        with self.assertRaisesRegex(ValueError, 'too large'):
            await gemini.fit_conversation([block(0, 100), block(1, 300000)], None, self.root, self.summarize)


class GeminiAutoCompactionCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_cli_summarizes_then_answers_with_current_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures.GeminiCliTests().fixture(root, '''import json,sys
from pathlib import Path
args=sys.argv[1:]
name=args[args.index("--agent")+1]
profile=(Path(__file__).parents[2]/".gemini/config/agents"/name/"agent.md").read_text()
request=json.loads(sys.stdin.readline())
content=request["message"]["content"]
receipt=content.rsplit("\\n",1)[-1]
log=Path(__file__).parent/"calls.jsonl"
summary="compacts part of a long Codex conversation" in profile
with log.open("a") as f: f.write(json.dumps({"summary":summary,"size":len(content.encode())})+"\\n")
text="OLD_CODE_ALPHA kept" if summary else json.dumps({"task":"CURRENT_TASK_552" in content,"summary":"OLD_CODE_ALPHA kept" in content})
print(json.dumps({"event":"init","init":{"model":args[args.index("--model")+1],"agent":name}}))
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":text,"calls":[],"context_receipt":receipt}}}))
''')
            items = [{'role': 'user', 'content': 'OLD_CODE_ALPHA ' + 'y' * 30000}]
            items += [{'role': 'assistant' if i % 2 else 'user', 'content': 'history ' + str(i) + ' ' + 'z' * 30000} for i in range(12)]
            items.append({'role': 'user', 'content': 'CURRENT_TASK_552'})
            with patch('gemini_relay.Path.home', return_value=root):
                result = await gemini.run_cli({'model': 'gemini-4-argon', 'input': items}, root)
            observed = json.loads(result['output'][0]['content'][0]['text'])
            self.assertEqual(observed, {'task': True, 'summary': True})
            calls = [json.loads(l) for l in (root / '.local/bin/calls.jsonl').read_text().splitlines()]
            self.assertTrue(any(c['summary'] for c in calls))
            self.assertFalse(calls[-1]['summary'])
            self.assertTrue(all(c['size'] < gemini.MAX_CONVERSATION_BYTES + 200 for c in calls))


if __name__ == '__main__':
    unittest.main()

