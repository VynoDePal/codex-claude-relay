"""Protect task text from Antigravity's per-user-message truncation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gemini_relay as gemini
import test_gemini_relay as fixtures


class GeminiPromptIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_context_cannot_produce_a_successful_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures.GeminiCliTests().fixture(root, '''import json,sys
sys.stdin.readline()
name=sys.argv[sys.argv.index("--agent")+1]
print(json.dumps({"event":"init","init":{"model":"gemini-4-argon-medium","agent":name}}))
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":"Plausible but unverified","calls":[],"context_receipt":"wrong"}}}))
''')
            with patch('gemini_relay.Path.home', return_value=root):
                with self.assertRaisesRegex(ValueError, 'context integrity'):
                    await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'Read the complete task'}, root)

    async def test_large_instructions_do_not_hide_the_actual_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures.GeminiCliTests().fixture(root, '''import json,sys
from pathlib import Path
args=sys.argv[1:]
name=args[args.index("--agent")+1]
profile=Path(__file__).parents[2]/".gemini/config/agents"/name/"agent.md"
request=json.loads(sys.stdin.readline())
visible=request["message"]["content"][:200000]
intact="CURRENT_TASK_814" in visible
has_policy="POLICY_END_933" in profile.read_text()
data_isolated="CURRENT_TASK_814" not in profile.read_text()
print(json.dumps({"event":"init","init":{"model":"gemini-4-argon-medium","agent":name}}))
print(json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":{"text":json.dumps({"task_visible":intact,"policy_preserved":has_policy,"data_isolated":data_isolated}),"calls":[],"context_receipt":visible.rsplit("\\n",1)[-1]}}}))
''')
            payload = {'model': 'gemini-4-argon',
                       'instructions': 'Policy context. ' * 22000 + 'POLICY_END_933',
                       'input': 'CURRENT_TASK_814', 'tools': [], 'tool_choice': 'none'}
            with patch('gemini_relay.Path.home', return_value=root):
                result = await gemini.run_cli(payload, root)
            observed = json.loads(result['output'][0]['content'][0]['text'])
            self.assertTrue(observed['task_visible'], 'The current task was silently cut off')
            self.assertTrue(observed['policy_preserved'], 'Trusted instructions were lost')
            self.assertTrue(observed['data_isolated'], 'User content must not become native instructions')
            self.assertEqual([p.name for p in (root / '.gemini/config/agents').iterdir()],
                             [gemini.AGENT_NAME], 'Per-request profiles must be cleaned up')

    async def test_oversized_conversation_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures.GeminiCliTests().fixture(root, 'raise AssertionError("oversized input must not launch")\n')
            with patch('gemini_relay.Path.home', return_value=root):
                with self.assertRaisesRegex(ValueError, 'conversation.*too large'):
                    await gemini.run_cli({'model': 'gemini-4-argon', 'input': 'é' * 200000}, root)


if __name__ == '__main__':
    unittest.main()
