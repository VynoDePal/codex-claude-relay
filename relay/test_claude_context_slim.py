"""Claude context: slimmed tool catalog, bounded accessibility trees, deduplicated instruction blocks."""
import unittest

import claude_relay
from claude_relay import MAX_TREE_CHARS, MAX_TREE_LINE_CHARS, cap_tree, cli_request, compact_browser_history, dedupe_blocks
from gpu_vision import tool_alias
from tool_catalog import slim_catalog

BT = '\x60'
DECL = 'exec tool declaration:\n' + BT * 3 + 'ts\ndeclare const tools: { NAME(args: { ' + 'field: string; ' * 120 + '}): Promise<CallToolResult>; };\n' + BT * 3 + '\n'


def tool(name, text):
    return '### ' + BT + name + BT + '\n' + text + '\n\n' + DECL.replace('NAME', name) + '\n'


PLUGIN = ' This tool is part of plugin ' + BT + 'GitHub' + BT + '.'
CATALOG = ('Run JavaScript code to orchestrate tool calls.\n\n'
           + tool('exec_command', 'Runs a command in a PTY.')
           + '## mcp__codex_app\nManage the Codex desktop app.\n\n'
           + tool('mcp__codex_app__create_thread', 'Create a separate task.')
           + '## mcp__codex_apps__github\nAccess repositories, issues, and pull requests.\n\n'
           + tool('mcp__codex_apps__github_fetch_pr', 'Fetch a pull request with its diff.' + PLUGIN)
           + tool('mcp__codex_apps__github_get_repo', 'Retrieve metadata for a GitHub repository.' + PLUGIN))


def chat():
    return {'tools': [{'type': 'function', 'function': {'name': 'exec', 'description': CATALOG}}]}


def tree(n, line_len=60):
    body = ''.join('\t\t%d link Description: %s\n' % (i, 'x' * line_len) for i in range(1, n))
    return ('Browser tab: 9, Title: "Big", URL: "https://example.com/big".\n0 AXWebArea Big\n' + body
            + '\nThe focused UI element is 0 AXWebArea Big')


SKILLS = '<skills_instructions>\n' + 'skill line\n' * 600 + '</skills_instructions>'


def dev(text):
    return {'role': 'developer', 'content': [{'type': 'text', 'text': text}]}


class SlimCatalogTest(unittest.TestCase):
    def test_connectors_abbreviated_core_and_local_servers_verbatim(self):
        c = chat()
        self.assertGreaterEqual(slim_catalog(c), 2)
        text = c['tools'][0]['function']['description']
        self.assertIn(tool('exec_command', 'Runs a command in a PTY.'), text)
        self.assertIn('declare const tools: { mcp__codex_app__create_thread', text)
        self.assertNotIn('declare const tools: { mcp__codex_apps__github_fetch_pr', text)
        self.assertIn('### ' + BT + 'mcp__codex_apps__github_fetch_pr' + BT + '\nFetch a pull request with its diff. [abbreviated]', text)
        self.assertNotIn('plugin ' + BT + 'GitHub', text)
        self.assertIn('ALL_TOOLS.find', text)
        self.assertLess(len(text), len(CATALOG) * 0.75)

    def test_slimming_is_deterministic(self):
        a, b = chat(), chat()
        slim_catalog(a)
        slim_catalog(b)
        self.assertEqual(a, b)

    def test_prepare_slims_the_exec_catalog(self):
        payload = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'input': [{'role': 'user', 'content': 'bonjour'}],
                   'tools': [{'type': 'custom', 'name': 'exec', 'description': CATALOG}]}
        prepared, _ = claude_relay.prepare(payload)
        description = prepared['tools'][0]['function']['description']
        self.assertIn('[abbreviated]', description)
        self.assertIn('declare const tools: { exec_command', description)


class TreeCapTest(unittest.TestCase):
    def test_small_tree_unchanged(self):
        t = tree(50)
        self.assertEqual(cap_tree(t), t)

    def test_long_lines_clipped_without_dropping_rows(self):
        out = cap_tree(tree(60, line_len=1000))
        self.assertLessEqual(len(out), MAX_TREE_CHARS)
        self.assertTrue(all(len(line) <= MAX_TREE_LINE_CHARS for line in out.split('\n')))
        self.assertIn('\t\t59 link', out)
        self.assertNotIn('[Relay:', out)

    def test_huge_tree_keeps_head_tail_and_explains(self):
        t = tree(3000)
        out = cap_tree(t)
        self.assertLessEqual(len(out), MAX_TREE_CHARS)
        self.assertTrue(out.startswith('Browser tab: 9, Title: "Big"'))
        self.assertTrue(out.endswith('The focused UI element is 0 AXWebArea Big'))
        self.assertIn('\t\t1 link', out)
        self.assertIn('\t\t2999 link', out)
        self.assertRegex(out, r'\[Relay: \d+ lines of this long accessibility tree omitted \(element indices \d+ to \d+\)')
        self.assertEqual(cap_tree(t), out)

    def test_latest_big_tree_is_capped_in_history(self):
        alias = tool_alias('js', 'mcp__cua_repl')
        history = [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': alias, 'arguments': '{}'}}]},
                   {'role': 'tool', 'tool_call_id': 'c1', 'content': 'Output:\n' + tree(3000)}]
        out = compact_browser_history(history, {alias})
        self.assertLess(len(out[1]['content']), MAX_TREE_CHARS + 100)
        self.assertIn('[Relay: ', out[1]['content'])


class DedupeTest(unittest.TestCase):
    def test_identical_later_copy_replaced(self):
        changed = SKILLS.replace('skill line', 'skill line v2', 1)
        history = [dev(SKILLS), {'role': 'user', 'content': [{'type': 'text', 'text': 'continue'}]}, dev(SKILLS), dev(changed)]
        out = dedupe_blocks([], history)
        self.assertEqual(out[0], history[0])
        self.assertIn('identical to the copy in conversation message 1', out[2]['content'][0]['text'])
        self.assertEqual(out[3], history[3])
        self.assertEqual(history[2]['content'][0]['text'], SKILLS)

    def test_copy_of_a_system_prompt_block(self):
        out = dedupe_blocks([dev(SKILLS)], [dev(SKILLS)])
        self.assertIn('the system prompt', out[0]['content'][0]['text'])

    def test_short_blocks_and_tool_outputs_untouched(self):
        history = [dev('short'), dev('short'), {'role': 'tool', 'tool_call_id': 'x', 'content': SKILLS},
                   {'role': 'tool', 'tool_call_id': 'y', 'content': SKILLS}]
        self.assertEqual(dedupe_blocks([], history), history)

    def test_cli_request_reference_matches_message_numbers(self):
        payload = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'tools': [], 'input': [
            {'role': 'user', 'content': 'bonjour'}, {'role': 'developer', 'content': SKILLS},
            {'role': 'user', 'content': 'suite'}, {'role': 'developer', 'content': SKILLS}]}
        _, blocks, _ = cli_request(payload)
        texts = [b['text'] for b in blocks]
        self.assertEqual(sum(t.count('skill line') for t in texts), 600)
        self.assertTrue(any('identical to the copy in conversation message 2' in t for t in texts))


if __name__ == '__main__':
    unittest.main()

