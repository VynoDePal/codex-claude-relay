"""Gemini tool-catalog compaction for Antigravity's native input budget."""
import unittest

import gemini_relay as gemini

PREAMBLE = 'Run JavaScript code.\n- Global helpers: ALL_TOOLS.\n'
CORE = '### \x60exec_command\x60\nRuns a command in a PTY. ' + 'core detail ' * 50 + '\n'


def catalog(apps=40, tools_per_app=10, body=3000):
    parts = [PREAMBLE, CORE]
    for a in range(apps):
        space = 'mcp__codex_apps__app%d' % a
        parts.append('## %s\nTools in the %s namespace.\n' % (space, space))
        for t in range(tools_per_app):
            parts.append('### \x60%s_tool%d\x60\nShared app description %d.\n\nDoes thing %d of app %d. ' % (space, t, a, t, a)
                         + 'x' * body + '\nEND_%d_%d\n' % (a, t))
    return '\n'.join(parts)


def chat(description):
    return {'tools': [{'type': 'function', 'function': {'name': 'exec', 'description': description}},
                      {'type': 'function', 'function': {'name': 'small', 'description': 'Small tool.'}}]}


class GeminiCatalogTests(unittest.TestCase):
    def test_small_catalog_is_unchanged(self):
        text = catalog(apps=2, tools_per_app=2, body=100)
        c = chat(text)
        self.assertEqual(gemini.compact_catalog(c), 0)
        self.assertEqual(c['tools'][0]['function']['description'], text)

    def test_large_catalog_fits_budget_and_keeps_every_tool(self):
        text = catalog()
        self.assertGreater(len(text), gemini.CATALOG_BUDGET_CHARS)
        c = chat(text)
        count = gemini.compact_catalog(c)
        result = c['tools'][0]['function']['description']
        self.assertGreater(count, 0)
        self.assertLessEqual(len(result) + len('Small tool.'), gemini.CATALOG_BUDGET_CHARS + 1000)
        self.assertEqual(result.count('### \x60'), text.count('### \x60'))
        self.assertIn(CORE.strip(), result)
        self.assertTrue(result.startswith(PREAMBLE))
        self.assertIn('ALL_TOOLS.find', result)
        self.assertIn('%d nested tool descriptions' % count, result)
        self.assertEqual(c['tools'][1]['function']['description'], 'Small tool.')

    def test_abbreviation_keeps_the_tool_specific_line(self):
        c = chat(catalog())
        gemini.compact_catalog(c)
        result = c['tools'][0]['function']['description']
        start = result.index('### \x60mcp__codex_apps__app0_tool3\x60')
        unit = result[start:result.index('###', start + 3)]
        if '[abbreviated]' in unit:
            self.assertIn('Does thing 3 of app 0', unit)
            self.assertNotIn('Shared app description', unit)
            self.assertNotIn('END_0_3', unit)

    def test_only_as_much_as_needed_is_abbreviated(self):
        text = catalog(apps=30, tools_per_app=10, body=1000)
        c = chat(text)
        count = gemini.compact_catalog(c)
        self.assertGreater(count, 0)
        self.assertLess(count, 300)
        self.assertGreaterEqual(c['tools'][0]['function']['description'].count('END_'), 300 - count)

    def test_uncompactable_catalog_is_refused_before_inference(self):
        c = chat('Plain description without headings. ' + 'y' * (gemini.CATALOG_HARD_LIMIT_CHARS + 10))
        with self.assertRaisesRegex(ValueError, 'too large for Antigravity'):
            gemini.compact_catalog(c)

    def test_prepare_applies_compaction_to_real_payloads(self):
        payload = {'model': 'gemini-4-argon', 'input': 'Hi', 'tools': [
            {'type': 'custom', 'name': 'exec', 'description': catalog(),
             'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: /.+/'}}]}
        chat_payload, mapping = gemini.prepare(payload)
        description = chat_payload['tools'][0]['function']['description']
        self.assertLess(len(description), gemini.CATALOG_BUDGET_CHARS + 2000)
        self.assertIn('[abbreviated]', description)
        self.assertEqual(payload['tools'][0]['description'], catalog())


if __name__ == '__main__':
    unittest.main()
