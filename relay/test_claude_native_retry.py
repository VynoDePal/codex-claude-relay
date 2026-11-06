"""A failed native tool attempt must not end a turn without Codex calls."""
import asyncio
import json
import tempfile
import unittest
from unittest import mock

import claude_relay
import gemini_relay

PAYLOAD = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'input': [{'role': 'user', 'content': 'Confie la correction a Claude Code.'}],
           'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}]}


def stdout(events):
    return ''.join(json.dumps(e) + '\n' for e in events).encode()


def native(name):
    return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': {}}]}}


def result(calls, text='ok'):
    return {'type': 'result', 'structured_output': {'text': text, 'calls': calls}}


GIVE_UP = [native('Bash'), {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True, 'content': 'No such tool available: Bash'}]}},
           result([], 'Je n ai plus acces aux outils de cette session.')]
CALL = [result([{'name': 'read', 'arguments': '{}'}], 'Je lis le fichier.')]


class FakeProc:
    def __init__(self, out, sent):
        self.out, self.sent, self.returncode, self.pid = out, sent, None, 0

    async def communicate(self, raw):
        self.sent.append(raw.decode())
        self.returncode = 0
        return self.out, b''

    async def wait(self):
        return 0


class NativeRetryTest(unittest.TestCase):
    def run_with(self, outputs):
        sent = []
        queue = list(outputs)

        async def fake_exec(*args, **kwargs):
            return FakeProc(stdout(queue.pop(0)), sent)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(claude_relay.asyncio, 'create_subprocess_exec', fake_exec):
            response = asyncio.run(claude_relay.run_cli(PAYLOAD, root))
        return response, sent

    def test_retry_after_native_attempt_without_calls(self):
        response, sent = self.run_with([GIVE_UP, CALL])
        self.assertEqual(len(sent), 2)
        self.assertIn('Transport note from the relay', sent[1])
        self.assertIn('Bash', sent[1])
        self.assertTrue(any(o['type'] == 'function_call' and o['name'] == 'read' for o in response['output']))

    def test_no_retry_when_no_native_attempt(self):
        _, sent = self.run_with([[result([], 'Termine.')]])
        self.assertEqual(len(sent), 1)

    def test_no_retry_when_calls_already_present(self):
        _, sent = self.run_with([[native('Read'), *CALL]])
        self.assertEqual(len(sent), 1)

    def test_protocol_note_is_claude_only(self):
        system, _, _ = claude_relay.cli_request(PAYLOAD)
        self.assertIn('Your only native tool is StructuredOutput', system)
        self.assertNotIn('Your only native tool is StructuredOutput', gemini_relay.PROTOCOL)


if __name__ == '__main__':
    unittest.main()

