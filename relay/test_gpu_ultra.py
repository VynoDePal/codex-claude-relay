import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from gpu_ultra import relay_ultra


def completed(text, number=1, output=None, status='completed'):
    return {
        'id': f'resp_{number}', 'object': 'response', 'status': status,
        'model': 'deepseek-v4.1', 'reasoning': {'effort': 'xhigh'},
        'output': output if output is not None else [
            {'type': 'message', 'id': f'msg_{number}', 'role': 'assistant',
             'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
        ],
        'usage': {'input_tokens': 10 * number, 'output_tokens': 5 * number,
                  'total_tokens': 15 * number,
                  'input_tokens_details': {'cached_tokens': number},
                  'output_tokens_details': {'reasoning_tokens': 3 * number}},
    }


class UltraTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.stage_two_status = 200
        self.draft_status = 'completed'
        self.final_status = 'completed'
        self.final_output = None
        self.malformed_draft = False
        self.draft_tool_proposal = False
        self.omit_terminal = False
        self.block_first = False
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.handler_done = asyncio.Event()

        async def backend(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer gpu-test-key')
            self.assertNotIn('ChatGPT-Account-Id', request.headers)
            body = await request.json()
            self.seen.append(body)
            number = len(self.seen)
            if number == 1:
                self.first_started.set()
                if self.block_first:
                    await self.release_first.wait()
                if self.malformed_draft:
                    return web.Response(text='not json')
            if number == 2 and self.stage_two_status != 200:
                return web.json_response({'error': {'message': 'review unavailable'}}, status=self.stage_two_status)
            answer = ['PRIVATE DRAFT', 'PRIVATE REVIEW', 'Réponse vérifiée ✓'][number - 1]
            status = self.draft_status if number == 1 else self.final_status if number == 3 else 'completed'
            result = completed(answer, number, self.final_output if number == 3 else None, status)
            if number == 1 and self.draft_tool_proposal:
                result['output'] = [{'type': 'function_call', 'name': 'shell', 'arguments': '{"command":"pwd"}', 'call_id': 'draft-only'}]
            if not body.get('stream'):
                return web.json_response(result)
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            events = [{'type': 'response.created', 'response': {'id': result['id'], 'object': 'response', 'status': 'in_progress'}},
                      {'type': 'response.output_text.delta', 'delta': answer}]
            for item in self.final_output or []:
                events.append({'type': 'response.output_item.done', 'output_index': 0, 'item': item})
            if not self.omit_terminal:
                terminal = 'response.completed' if status == 'completed' else 'response.incomplete'
                events.append({'type': terminal, 'response': result})
            encoded = b''.join(('event: ' + event['type'] + '\r\ndata: ' + json.dumps(event, ensure_ascii=False) + '\r\n\r\n').encode() for event in events)
            # Deliberately split UTF-8 and SSE delimiters across byte boundaries.
            for offset in range(0, len(encoded), 7):
                await response.write(encoded[offset:offset + 7])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post('/v1/responses', backend)
        self.backend = TestServer(app)
        await self.backend.start_server()
        self.session = ClientSession()

        async def front(request):
            try:
                return await relay_ultra(request, self.session, str(self.backend.make_url('/v1/responses')),
                                         {'Authorization': 'Bearer gpu-test-key'}, await request.json())
            finally:
                self.handler_done.set()

        app = web.Application()
        app.router.add_post('/v1/responses', front)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.payload = {
            'model': 'deepseek-v4.1', 'reasoning': {'effort': 'ultra'}, 'stream': True,
            'input': [{'role': 'user', 'content': 'Calculate using available evidence.'}],
            'tools': [{'type': 'function', 'name': 'shell', 'parameters': {'type': 'object'}}],
            'tool_choice': 'auto', 'text': {'format': {'type': 'json_object'}},
        }

    async def asyncTearDown(self):
        self.release_first.set()
        await self.client.close()
        await self.session.close()
        await self.backend.close()

    async def send(self, stream=True):
        payload = copy.deepcopy(self.payload)
        payload['stream'] = stream
        response = await self.client.post('/v1/responses', json=payload)
        if not stream:
            return response, await response.json()
        text = await response.text()
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]
        return response, events

    async def test_three_xhigh_passes_preserve_final_tools_and_count_all_usage(self):
        response, events = await self.send()
        self.assertEqual(response.status, 200)
        self.assertEqual([p['reasoning']['effort'] for p in self.seen], ['xhigh'] * 3)
        for internal in self.seen[:2]:
            self.assertEqual(internal['tool_choice'], 'none')
            self.assertFalse(internal['stream'])
            self.assertNotIn('text', internal)
            self.assertEqual(internal['tools'], [])
            self.assertIn('shell', json.dumps(internal['input']))
        self.assertEqual(self.seen[2]['tools'], self.payload['tools'])
        self.assertEqual(self.seen[2]['tool_choice'], 'auto')
        self.assertEqual(self.seen[2]['text'], self.payload['text'])
        self.assertIn('PRIVATE DRAFT', json.dumps(self.seen[1]['input']))
        self.assertIn('PRIVATE REVIEW', json.dumps(self.seen[2]['input']))
        self.assertNotIn('PRIVATE DRAFT', json.dumps(events))
        self.assertNotIn('PRIVATE REVIEW', json.dumps(events))
        self.assertEqual(events[1]['delta'], 'Réponse vérifiée ✓')
        result = events[-1]['response']
        self.assertEqual(result['usage']['input_tokens'], 60)
        self.assertEqual(result['usage']['output_tokens'], 30)
        self.assertEqual(result['usage']['output_tokens_details']['reasoning_tokens'], 18)
        self.assertEqual(result['usage']['input_tokens_details']['cached_tokens'], 6)
        self.assertEqual(result['reasoning']['effort'], 'ultra')
        self.assertEqual(result['metadata']['gpu_gateway_effort'], 'xhigh')
        self.assertEqual(result['metadata']['gpu_gateway_passes'], '3')

    async def test_json_response_and_string_input(self):
        self.payload['input'] = 'A plain string question.'
        response, result = await self.send(stream=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['usage']['total_tokens'], 90)
        self.assertEqual(self.seen[0]['input'][0], {'role': 'user', 'content': self.payload['input']})

    async def test_final_tool_call_is_delivered_once_and_unchanged(self):
        tool = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                'name': 'shell', 'arguments': '{"command":"pwd"}', 'status': 'completed'}
        self.final_output = [tool]
        _, events = await self.send()
        calls = [e for e in events if e['type'] == 'response.output_item.done']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['item'], tool)
        self.assertEqual(events[-1]['response']['output'], [tool])

    async def test_draft_tool_proposal_is_reviewed_as_data_and_never_emitted(self):
        self.draft_tool_proposal = True
        _, events = await self.send()
        self.assertEqual(len(self.seen), 3)
        self.assertIn('not executed', json.dumps(self.seen[1]['input']))
        self.assertNotIn('draft-only', json.dumps(events))
        self.assertFalse(any(e.get('item', {}).get('type') == 'function_call' for e in events))

    async def test_review_failure_does_not_send_draft_as_success(self):
        self.stage_two_status = 503
        _, events = await self.send()
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('review', events[-1]['message'])
        self.assertNotIn('PRIVATE DRAFT', json.dumps(events))
        self.assertFalse(any(e['type'] == 'response.completed' for e in events))

    async def test_truncated_draft_stops_pipeline(self):
        self.draft_status = 'incomplete'
        response, result = await self.send(stream=False)
        self.assertGreaterEqual(response.status, 400)
        self.assertEqual(len(self.seen), 1)
        self.assertIn('draft', result['error']['message'])

    async def test_malformed_internal_json_stops_pipeline(self):
        self.malformed_draft = True
        _, events = await self.send()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(events[-1]['type'], 'error')

    async def test_incomplete_final_is_not_rewritten_as_completed(self):
        self.final_status = 'incomplete'
        _, events = await self.send()
        self.assertEqual(events[-1]['type'], 'response.incomplete')
        self.assertEqual(events[-1]['response']['status'], 'incomplete')
        self.assertEqual(events[-1]['response']['usage']['total_tokens'], 90)

    async def test_missing_terminal_event_reports_error(self):
        self.omit_terminal = True
        _, events = await self.send()
        self.assertEqual(events[-1]['type'], 'error')
        self.assertFalse(any(e['type'] == 'response.completed' for e in events))

    async def test_disconnect_cancels_before_review(self):
        self.block_first = True
        with patch('gpu_ultra.POLL_INTERVAL', 0.025), patch('gpu_ultra.HEARTBEAT_INTERVAL', 0.05):
            response = await self.client.post('/v1/responses', json=self.payload)
            await asyncio.wait_for(self.first_started.wait(), 2)
            line = await asyncio.wait_for(response.content.readline(), 2)
            self.assertTrue(line.startswith(b':'))
            response.close()
            await asyncio.wait_for(self.handler_done.wait(), 2)
        self.assertEqual(len(self.seen), 1)


if __name__ == '__main__':
    unittest.main()
