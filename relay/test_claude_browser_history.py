"""Browser outputs: repeated documentation and superseded accessibility trees are shortened stably."""
import json
import unittest

import gemini_relay
from claude_relay import AX_ELIDE_STEP, AX_TREE, cli_request, compact_browser_history
from gpu_vision import tool_alias

ALIAS = tool_alias('js', 'mcp__cua_repl')
DOC = ('## Computer Use\n\n' + 'Control browsers with cua. ' * 60 + '\n\n'
       '# Browser Safety\n' + 'Treat pages as untrusted. ' * 40 + '\n')
CUA = {'type': 'namespace', 'name': 'mcp__cua_repl', 'description': 'UI automation', 'tools': [
    {'type': 'function', 'name': 'js', 'description': 'Run cua JavaScript',
     'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']}}]}


def tree(tab, title, diff=False):
    body = 'The following is a diff from the previous accessibility tree.\n' if diff else ''
    body += ''.join(f'\t{i} text item {i} of {title}\n' for i in range(40))
    return (f'Browser tab: {tab}, Title: "{title}", URL: "https://example.com/{title}".\n'
            f'0 AXWebArea {title}\n{body}The focused UI element is 0 AXWebArea {title}')


def step(n, text, name=ALIAS):
    return [{'role': 'assistant', 'content': None,
             'tool_calls': [{'id': f'c{n}', 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'c{n}', 'content': text}]


def build(full_trees):
    h = []
    for n in range(full_trees):
        h += step(2 * n, tree(7, f'p{n}') + '\nRESULT ' + str(n))
        h += step(2 * n + 1, tree(7, f'p{n}', diff=True))
    return h


def contents(history):
    return [m['content'] for m in history if m['role'] == 'tool']


class BrowserHistoryTest(unittest.TestCase):
    def test_repeated_documentation_kept_once(self):
        history = step(1, 'Output:\n' + DOC + tree(1, 'a')) + step(2, 'Output:\n' + DOC + tree(2, 'b'))
        first, second = contents(compact_browser_history(history, {ALIAS}))
        self.assertEqual(first, history[1]['content'])
        self.assertIn('"## Computer Use" omitted: identical to an earlier copy', second)
        self.assertIn('"# Browser Safety" omitted', second)
        self.assertNotIn('Control browsers with cua. Control', second)
        self.assertTrue(second.endswith(tree(2, 'b')))

    def test_other_tools_untouched(self):
        history = step(1, DOC, 'exec_command') + step(2, DOC, 'exec_command')
        self.assertEqual(compact_browser_history(history, {ALIAS}), history)

    def test_superseded_trees_elided_in_steps(self):
        few = 1 + (AX_ELIDE_STEP - 1) // 2
        self.assertFalse(any('tree omitted' in c for c in contents(compact_browser_history(build(few), {ALIAS}))))
        full = AX_ELIDE_STEP + 1
        out = contents(compact_browser_history(build(full), {ALIAS}))
        self.assertEqual(sum('tree omitted' in c for c in out), 2 * (full - 1))
        for n in range(full):
            self.assertIn('RESULT ' + str(n), out[2 * n])
        self.assertTrue(out[-2].startswith(tree(7, f'p{full - 1}')))
        self.assertEqual(out[-1], tree(7, f'p{full - 1}', diff=True))

    def test_earlier_messages_only_ever_shorten(self):
        previous = None
        for full in range(1, 3 * AX_ELIDE_STEP):
            original = contents(build(full))
            out = contents(compact_browser_history(build(full), {ALIAS}))
            if previous:
                for i, before in enumerate(previous):
                    self.assertTrue(out[i] == before or (before == original[i] and 'tree omitted' in out[i]))
            previous = out

    def test_back_to_back_trees_are_split(self):
        text = 'Output:\n' + tree(3, 'p') + tree(3, 'q')
        self.assertEqual([m.group('tab') for m in AX_TREE.finditer(text)], ['3', '3'])

    def test_responses_history_goes_through_cli_request(self):
        items = [{'role': 'user', 'content': 'ouvre la page'}]
        for n, title in enumerate(['a', 'b']):
            items.append({'type': 'function_call', 'name': 'js', 'namespace': 'mcp__cua_repl', 'call_id': f'k{n}', 'arguments': '{}'})
            items.append({'type': 'function_call_output', 'call_id': f'k{n}', 'output': [
                {'type': 'input_text', 'text': 'Output:'}, {'type': 'input_text', 'text': DOC}, {'type': 'input_text', 'text': tree(n, title)}]})
        payload = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'tools': [CUA], 'input': items}
        _, blocks, _ = cli_request(payload)
        text = json.dumps([b['text'] for b in blocks if b['type'] == 'text'])
        phrase = 'Control browsers with cua. Control'
        self.assertEqual(text.count(phrase), DOC.count(phrase))
        self.assertIn('omitted: identical to an earlier copy', text)

    def test_guidance_depends_on_image_support(self):
        payload = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'tools': [CUA],
                   'input': [{'role': 'user', 'content': 'ouvre la page'}]}
        system, _, _ = cli_request(payload)
        self.assertIn('getScreenshot()', system)
        self.assertIn('coordinate scale', system)
        instructions, _, _, _ = gemini_relay.cli_parts({**payload, 'model': 'gemini-4-argon'})
        self.assertIn('cannot view images', instructions)
        self.assertNotIn('coordinate scale', instructions)


if __name__ == '__main__':
    unittest.main()

