import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ultra_n_engine import ResearchConfig, run_research


def answer(text='Evidence [S1]', calls=None, usage=True):
    result = {'choices': [{'message': {'role': 'assistant', 'content': text, 'tool_calls': calls or []}}]}
    if usage:
        result['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
    return result


def call(name, **args):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Sources:
    async def search(self, query, limit=5):
        return [{'title': 'Paper', 'url': 'https://example.org/paper', 'snippet': 'Discovery only', 'provider': 'fake'}]

    async def fetch(self, url):
        if 'fail' in url:
            raise RuntimeError('SECRET credential')
        return {'url': url, 'title': 'Paper', 'text': 'Observed evidence with important uncertainty.', 'content_type': 'text/plain'}


class Gateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    async def complete(self, messages, tools, max_tokens):
        self.inputs.append((messages, tools, max_tokens))
        return next(self.responses)


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        ResearchConfig().validate()
        for values in ({'bogus': 1}, {'max_agents': True}, {'max_seconds': 1}, {'max_agents': 0}, {'model': 'other'}, {'max_total_tokens': 9999}):
            with self.assertRaises(ValueError):
                ResearchConfig.from_dict(values)
        ResearchConfig.from_dict({'max_agents': 0, 'concurrency': 1})

    def test_worker_requires_research_and_summary_turns(self):
        with self.assertRaises(ValueError):
            ResearchConfig.from_dict({'max_worker_turns': 1})
        ResearchConfig.from_dict({'max_worker_turns': 2}).validate()
        self.assertEqual(ResearchConfig().max_worker_turns, 6)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'research'

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_engine(self, responses, **config):
        self.gateway = Gateway(responses)
        return await run_research('Compare evidence', ResearchConfig.from_dict(config), self.gateway, Sources(), self.path)

    async def test_zero_workers_private_files_and_dedup(self):
        result = await self.run_engine([
            answer(None, [call('search_publications', query='evidence')]),
            answer(None, [call('read_source', url='https://example.org/paper#one'), call('read_source', url='https://example.org/paper#two')]),
            answer(),
        ], max_agents=0, concurrency=1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['metrics']['agents_created'], 0)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        for name in ('report.md', 'sources.json', 'state.json', 'events.jsonl'):
            self.assertEqual((self.path / name).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('reasoning_content', (self.path / 'state.json').read_text())

    async def test_two_workers_then_rejected_wave_c1(self):
        tasks = [{'objective': 'Read evidence', 'role': 'reviewer'}] * 2
        result = await self.run_engine([
            answer(None, [call('delegate', tasks=tasks)]),
            answer(None, [call('read_source', url='https://example.org/a')]), answer(),
            answer(None, [call('read_source', url='https://example.org/b')]), answer('Second [S2]'),
            answer(None, [call('delegate', tasks=tasks[:1])]), answer('Both [S1] [S2]'),
        ], max_agents=2, concurrency=1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['metrics']['agents_created'], 2)
        self.assertEqual(result['metrics']['peak_workers'], 1)
        self.assertIn('remaining', json.dumps(self.gateway.inputs[-1]))

    async def test_source_error_and_empty_sources(self):
        result = await self.run_engine([answer(None, [call('read_source', url='https://example.org/fail')]), answer('All established.')])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('SECRET', ''.join(p.read_text() for p in self.path.rglob('*') if p.is_file()))

    async def test_invalid_citation_repair(self):
        result = await self.run_engine([answer(None, [call('read_source', url='https://example.org/a')]), answer('Bad [S999]'), answer('Fixed [S1]')])
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('[S999]', result['report'])

    async def test_invalid_citation_partial(self):
        result = await self.run_engine([answer(None, [call('read_source', url='https://example.org/a')]), answer('Bad [S999]'), answer('Still [S999]')])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('[S999]', result['report'])

    async def test_concurrent_caps(self):
        tasks = [{'objective': 'Read', 'role': 'reviewer'}] * 2
        result = await self.run_engine([answer(None, [call('delegate', tasks=tasks)]), answer(None, [call('read_source', url='https://example.org/a')]), answer(None, [call('read_source', url='https://example.org/b')]), answer()], max_agents=2, concurrency=2, max_model_calls=4, max_tool_calls=2)
        self.assertLessEqual(result['metrics']['model_calls'], 4)
        self.assertLessEqual(result['metrics']['tool_calls'], 2)
        self.assertLessEqual(result['metrics']['total_tokens'], 160000)

    async def test_missing_usage_and_token_reservation(self):
        result = await self.run_engine([answer('No sources', usage=False)], max_total_tokens=10000)
        self.assertFalse(result['metrics']['usage_complete'])
        self.assertLessEqual(result['metrics']['total_tokens'], 10000)
        self.assertGreater(result['metrics']['total_tokens'], 4096)

    async def test_actual_token_overrun(self):
        response = answer()
        response['usage']['total_tokens'] = 200000
        result = await self.run_engine([response])
        self.assertGreater(result['metrics']['token_overrun'], 0)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['metrics']['model_calls'], 1)

    async def test_wall_timeout(self):
        class Slow:
            async def complete(self, **kwargs):
                await asyncio.sleep(2)
        with patch('ultra_n_engine._deadline_seconds', return_value=0.02):
            result = await run_research('query', ResearchConfig(), Slow(), Sources(), self.path)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['metrics']['stop_reason'], 'wall_timeout')

    async def test_disconnect_cancellation(self):
        started = asyncio.Event()
        class Slow:
            async def complete(self, **kwargs):
                started.set()
                await asyncio.sleep(10)
        task = asyncio.create_task(run_research('query', ResearchConfig(), Slow(), Sources(), self.path))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = json.loads((self.path / 'state.json').read_text())
        self.assertEqual(state['status'], 'cancelled')
        self.assertGreater(state['metrics']['total_tokens'], 0)

    async def test_query_validation(self):
        for query in ('', '   ', 'x' * 16001):
            with self.assertRaises(ValueError):
                await run_research(query, ResearchConfig(), Gateway([]), Sources(), self.path)

    async def test_grouped_invalid_citation(self):
        result = await self.run_engine([answer(None, [call('read_source', url='https://example.org/a')]), answer('Bad [S1, S999]'), answer('Still [S999]')])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('S999', result['report'])

    async def test_workers_cannot_expand_agents(self):
        task = {'objective': 'Read', 'role': 'reviewer'}
        result = await self.run_engine([answer(None, [call('delegate', tasks=[task])]), answer(None, [call('delegate', tasks=[task])]), answer('No evidence.'), answer('No evidence.')], max_agents=2, concurrency=1)
        self.assertEqual(result['metrics']['agents_created'], 1)
        self.assertIn('forbidden', json.dumps(self.gateway.inputs[2]))

    async def test_real_concurrent_worker_reservations(self):
        class ConcurrentGateway:
            async def complete(self, messages, tools, max_tokens):
                if 'You supervise.' in messages[0]['content']:
                    return answer(None, [call('delegate', tasks=[{'objective': 'Read', 'role': 'reviewer'}] * 2)])
                await asyncio.sleep(0.01)
                return answer('Unverified', usage=False)
        result = await run_research('question', ResearchConfig(max_agents=2, concurrency=2, max_total_tokens=18000), ConcurrentGateway(), Sources(), self.path)
        self.assertLessEqual(result['metrics']['total_tokens'], 18000)
        self.assertEqual(result['metrics']['token_overrun'], 0)
        self.assertEqual(result['metrics']['agents_created'], 2)
        self.assertLessEqual(result['metrics']['peak_workers'], 2)

    async def test_cancel_all_inflight_workers(self):
        both_started = asyncio.Event()
        active = 0
        cancelled = 0
        class ConcurrentGateway:
            async def complete(self, messages, tools, max_tokens):
                nonlocal active, cancelled
                if 'You supervise.' in messages[0]['content']:
                    return answer(None, [call('delegate', tasks=[{'objective': 'Read', 'role': 'reviewer'}] * 2)])
                active += 1
                if active == 2:
                    both_started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled += 1
                    raise
        task = asyncio.create_task(run_research('query', ResearchConfig(max_agents=2, concurrency=2), ConcurrentGateway(), Sources(), self.path))
        await asyncio.wait_for(both_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancelled, 2)
        state = json.loads((self.path / 'state.json').read_text())
        self.assertEqual(state['metrics']['peak_workers'], 2)
        self.assertFalse(state['metrics']['usage_complete'])

    async def test_live_running_state(self):
        seen = []
        async def progress(event):
            state = json.loads((self.path / 'state.json').read_text())
            seen.append(state)
        await run_research('question', ResearchConfig(), Gateway([answer('No source')]), Sources(), self.path, progress)
        self.assertTrue(all(s['status'] == 'running' for s in seen))
        self.assertEqual(seen[1]['metrics']['model_calls'], 1)
        self.assertEqual(json.loads((self.path / 'state.json').read_text())['status'], 'failed')

    async def test_bare_source_token_is_not_a_citation(self):
        result = await self.run_engine([
            answer(None, [call('read_source', url='https://example.org/a')]),
            answer('The evidence in scenario S1 supports the claim.'),
            answer('Still only scenario S1.'),
        ])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('supports the claim', result['report'])

    async def test_valid_grouped_citations(self):
        result = await self.run_engine([
            answer(None, [call('read_source', url='https://example.org/a'), call('read_source', url='https://example.org/b')]),
            answer('Evidence [S1, S2].'),
        ])
        self.assertEqual(result['status'], 'completed')

    async def test_worst_case_synthesis_serialization_reserve(self):
        from ultra_n_engine import _Run, FINAL_EVIDENCE_LIMIT, FINAL_INSTRUCTION_LIMIT, SYSTEM
        run = _Run('A unicode question 🌍', ResearchConfig(), Gateway([]), Sources(), self.path, None)
        reserved = run.synthesis_reserve()
        # Evidence is JSON-escaped first; UTF-8 truncation is applied before the
        # request serializer escapes it again. Exercise both escape-heavy ASCII
        # and multibyte data at the full byte allowance.
        for value in ('🌍', '\\', '"', '\x00', '\n'):
            run.worker_results = [{'agent_id': 1, 'summary': value * 20000}]
            run.sources = {str(i): {'id': f'S{i}', 'title': 'Paper', 'text': value * 20000} for i in range(1, 8)}
            messages = run.final_messages(value * FINAL_INSTRUCTION_LIMIT)
            packed = messages[2]['content'].split('\n', 1)[1]
            self.assertLessEqual(len(packed.encode('utf-8')), FINAL_EVIDENCE_LIMIT)
            self.assertNotIn('\x00', packed)
            self.assertNotIn('\n', packed)
            self.assertLessEqual(run.estimate(messages, []), reserved)
        self.assertLess(reserved, 45000)

    async def test_concurrent_evidence_growth_preserves_synthesis(self):
        started = 0
        workers_started = asyncio.Event()
        worker_followups = 0
        final_calls = 0
        class GrowingSources(Sources):
            async def fetch(self, url):
                return {'url': url, 'title': 'Multibyte paper', 'text': '🌍' * 30000, 'content_type': 'text/plain'}
        class GrowingGateway:
            async def complete(self, messages, tools, max_tokens):
                nonlocal started, worker_followups, final_calls
                if not tools:
                    final_calls += 1
                    return answer('Partial supported evidence [S1, S2].')
                if 'You supervise.' in messages[0]['content']:
                    return answer(None, [call('delegate', tasks=[{'objective': 'Read a', 'role': 'reviewer'}, {'objective': 'Read b', 'role': 'reviewer'}])])
                if len(messages) == 2:
                    started += 1
                    number = started
                    if started == 2:
                        workers_started.set()
                    await workers_started.wait()
                    return answer(None, [call('read_source', url=f'https://example.org/{number}')])
                worker_followups += 1
                return answer('Evidence [S1].')
        result = await run_research('Research unicode documents', ResearchConfig(max_total_tokens=70000, max_model_calls=10, max_supervisor_turns=1, max_agents=2, concurrency=2), GrowingGateway(), GrowingSources(), self.path)
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(worker_followups, 0)
        # Two compact worker summaries plus the reserved supervisor synthesis.
        self.assertEqual(final_calls, 3)
        self.assertLessEqual(result['metrics']['total_tokens'], 70000)
        self.assertEqual(result['metrics']['token_overrun'], 0)
        self.assertIn('Partial supported evidence', result['report'])

    async def test_last_worker_turn_returns_summary_without_tools(self):
        result = await self.run_engine([
            answer(None, [call('delegate', tasks=[{'objective': 'Read evidence', 'role': 'reviewer'}])]),
            answer(None, [call('read_source', url='https://example.org/a')]),
            answer('Observed evidence with uncertainty [S1].'),
            answer('Synthesis [S1].'),
        ], max_agents=1, concurrency=1, max_worker_turns=2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.gateway.inputs[2][1], [])
        self.assertIn('last worker turn', self.gateway.inputs[2][0][-1]['content'])
        state = json.loads((self.path / 'state.json').read_text())
        self.assertIn('Observed evidence', state['worker_results'][0]['summary'])

    async def test_final_evidence_prioritizes_findings_and_deeper_excerpts(self):
        from ultra_n_engine import _Run, FINAL_EVIDENCE_LIMIT
        run = _Run('Question', ResearchConfig(), Gateway([]), Sources(), self.path, None)
        run.worker_results = [{'agent_id': 1, 'summary': 'Useful finding [S1]. ' * 1000}]
        for number in range(1, 3):
            run.sources[str(number)] = {'id': f'S{number}', 'title': 'Paper', 'text': 'Title and boilerplate. ' * 40 + 'IMPORTANT RESULT ' * 200}
        packed = run.final_messages()[2]['content'].split('\n', 1)[1]
        payload = json.loads(packed)
        self.assertEqual(next(iter(payload)), 'worker_summaries')
        self.assertIn('Useful finding', payload['worker_summaries'][0]['summary'])
        self.assertIn('IMPORTANT RESULT', payload['sources'][0]['excerpt'])
        self.assertLessEqual(len(packed.encode('utf-8')), FINAL_EVIDENCE_LIMIT)
