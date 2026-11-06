"""Prompt-cache layout, streamed progress and CLI deadline for the Claude relay."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import claude_relay
from claude_relay import cli_request, convert, prepare, relay_claude, run_cli

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='


def base():
    return {'model': 'claude-code-opus', 'instructions': 'Codex base instructions.',
            'reasoning': {'effort': 'high'}, 'tool_choice': 'auto', 'parallel_tool_calls': True,
            'tools': [{'type': 'function', 'name': 'exec', 'description': 'Run a command.',
                       'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}}}],
            'input': [
                {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Permissions: workspace-write.'}]},
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Fix the failing test.'}]},
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Running the tests.'}]},
                {'type': 'function_call', 'name': 'exec', 'call_id': 'call_1', 'arguments': '{"cmd":"pytest"}'},
            ]}


class PromptCacheLayoutTests(unittest.TestCase):
    def test_next_tool_step_only_appends_blocks(self):
        first = base()
        second = copy.deepcopy(first)
        second['input'] += [
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': '1 failed'},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Patching.'}]},
        ]
        second['reasoning'] = {'effort': 'xhigh'}
        system1, blocks1, _ = cli_request(first)
        system2, blocks2, _ = cli_request(second)
        self.assertEqual(system1, system2)
        self.assertGreater(len(blocks2), len(blocks1))
        self.assertEqual(blocks2[:len(blocks1)], blocks1)
        self.assertIn('1 failed', json.dumps(blocks2[len(blocks1):]))

    def test_developer_instructions_stay_in_system_prompt(self):
        system, blocks, _ = cli_request(base())
        self.assertIn('Permissions: workspace-write.', system)
        self.assertNotIn('Permissions: workspace-write.', json.dumps(blocks))

    def test_non_default_tool_choice_is_stated_after_history(self):
        required = base()
        required['tool_choice'] = 'required'
        _, blocks, _ = cli_request(required)
        _, plain, _ = cli_request(base())
        self.assertIn('"tool_choice": "required"', blocks[-1]['text'])
        self.assertEqual(blocks[:-1], plain)

    def test_image_follows_the_message_that_references_it(self):
        payload = base()
        payload['input'].insert(2, {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'See screenshot.'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + PNG}]})
        _, blocks, _ = cli_request(payload)
        index = next(i for i, b in enumerate(blocks) if b['type'] == 'image')
        self.assertIn('See screenshot.', blocks[index - 1]['text'])
        self.assertEqual(blocks[index]['source'], {'type': 'base64', 'media_type': 'image/png', 'data': PNG})


class StreamAndDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_turn_streams_real_progress_events(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        async def runner(payload, root):
            await asyncio.sleep(.35)
            _, mapping = prepare(payload)
            return convert({'structured_output': {'text': 'Done.', 'calls': []}}, payload, mapping)

        async def route(request):
            return await relay_claude(request, await request.json(), Path(temp.name), asyncio.Semaphore(1), runner=runner)

        app = web.Application()
        app.router.add_post('/v1/responses', route)
        async with TestClient(TestServer(app)) as client:
            with patch.object(claude_relay, 'HEARTBEAT_SECONDS', .1):
                response = await client.post('/v1/responses', json={'model': 'claude-code-opus', 'stream': True, 'input': 'Hi'})
                body = await response.text()
        lines = body.splitlines()
        events = [json.loads(line[6:]) for line in lines if line.startswith('data: ')]
        self.assertFalse([line for line in lines if line.startswith(':')])
        self.assertEqual(events[0]['type'], 'response.created')
        self.assertGreaterEqual(sum(e['type'] == 'response.in_progress' for e in events), 2)
        self.assertEqual(sum(e['type'] == 'response.created' for e in events), 1)
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(events[-1]['response']['id'], events[0]['response']['id'])
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))

    async def test_cli_deadline_reports_clear_error_and_stops_claude(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / '.local/bin/claude'
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/usr/bin/python3\nimport sys, time\nsys.stdin.readline()\ntime.sleep(30)\n')
            executable.chmod(0o700)
            started = time.monotonic()
            with patch('claude_relay.pathlib.Path.home', return_value=root), patch.object(claude_relay, 'CLI_TIMEOUT_SECONDS', 1):
                with self.assertRaisesRegex(ValueError, 'did not finish within 1 seconds'):
                    await run_cli({'model': 'claude-code-opus', 'input': 'hello'}, root)
            self.assertLess(time.monotonic() - started, 10)


class MidConversationDeveloperTests(unittest.TestCase):
    def test_late_developer_message_keeps_cached_prefix(self):
        first = base()
        second = copy.deepcopy(first)
        second['input'] += [
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'},
            {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Open page context changed.'}]},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'continue'}]},
        ]
        system1, blocks1, _ = cli_request(first)
        system2, blocks2, _ = cli_request(second)
        self.assertEqual(system1, system2)
        self.assertEqual(blocks2[:len(blocks1)], blocks1)
        added = json.dumps(blocks2[len(blocks1):])
        self.assertIn('Open page context changed.', added)
        self.assertNotIn('Open page context changed.', system2)

if __name__ == '__main__':
    unittest.main()
