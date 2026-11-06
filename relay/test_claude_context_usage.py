"""Claude Code sums usage over internal calls; Codex must see the last call's context size."""
import unittest

import claude_relay
from claude_relay import last_call_usage


def call(inp, read, create, out):
    return {'type': 'assistant', 'message': {'id': 'm', 'usage': {'input_tokens': inp, 'cache_read_input_tokens': read,
                                                              'cache_creation_input_tokens': create, 'output_tokens': out}}}


class ContextUsageTest(unittest.TestCase):
    def test_last_call_usage_picks_final_assistant_event(self):
        events = [{'type': 'system'}, call(2, 0, 450000, 10), call(2, 450000, 30000, 20), {'type': 'result'}]
        self.assertEqual(last_call_usage(events)['cache_read_input_tokens'], 450000)
        self.assertEqual(last_call_usage(events)['cache_creation_input_tokens'], 30000)
        self.assertIsNone(last_call_usage([{'type': 'result'}]))

    def test_convert_reports_context_not_cumulative_sum(self):
        payload = {'model': 'claude-code-opus', 'input': [], 'tools': []}
        result = {'type': 'result', 'structured_output': {'text': 'ok', 'calls': []},
                  'usage': {'input_tokens': 4, 'cache_read_input_tokens': 450000, 'cache_creation_input_tokens': 480000, 'output_tokens': 30},
                  'context_usage': {'input_tokens': 2, 'cache_read_input_tokens': 450000, 'cache_creation_input_tokens': 30000, 'output_tokens': 20}}
        response = claude_relay.convert(result, payload, {})
        usage = response['usage']
        self.assertEqual(usage['input_tokens'], 480002)
        self.assertEqual(usage['output_tokens'], 30)

    def test_convert_without_context_usage_keeps_old_behaviour(self):
        payload = {'model': 'claude-code-opus', 'input': [], 'tools': []}
        result = {'type': 'result', 'structured_output': {'text': 'ok', 'calls': []},
                  'usage': {'input_tokens': 5, 'cache_read_input_tokens': 10, 'cache_creation_input_tokens': 0, 'output_tokens': 3}}
        self.assertEqual(claude_relay.convert(result, payload, {})['usage']['input_tokens'], 15)


if __name__ == '__main__':
    unittest.main()

