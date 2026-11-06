"""Claude browser support: guidance when the cua tool exists, bounded and downscaled screenshots."""
import base64
import io
import unittest

from PIL import Image

import claude_relay
from claude_relay import MAX_IMAGE_SIDE, MAX_INLINE_IMAGES, cli_request, fit_image

CUA = {'type': 'namespace', 'name': 'mcp__cua_repl', 'description': 'UI automation', 'tools': [
    {'type': 'function', 'name': 'js', 'description': 'Run cua JavaScript',
     'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']}}]}


def png(w, h):
    out = io.BytesIO()
    Image.new('RGB', (w, h), (40, 40, 40)).save(out, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def payload(tools, images=0):
    items = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'ouvre la page'}]}]
    for i in range(images):
        items.append({'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': f'capture {i}'}, {'type': 'input_image', 'image_url': png(8, 8)}]})
    return {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'tools': tools, 'input': items}


class ClaudeBrowserTest(unittest.TestCase):
    def test_guidance_only_when_browser_tool_supplied(self):
        system, _, mapping = cli_request(payload([CUA]))
        alias = next(a for a, i in mapping.items() if i['name'] == 'js')
        self.assertIn('Browser and computer use', system)
        self.assertIn(alias, system)
        self.assertIn('cua.createBrowserTab("chrome"', system)
        system, _, _ = cli_request(payload([]))
        self.assertNotIn('Browser and computer use', system)

    def test_old_images_dropped_in_steps(self):
        _, blocks, _ = cli_request(payload([CUA], images=MAX_INLINE_IMAGES + 3))
        attached = [b for b in blocks if b['type'] == 'image']
        self.assertLessEqual(len(attached), MAX_INLINE_IMAGES)
        self.assertGreater(len(attached), 0)
        text = ' '.join(b['text'] for b in blocks if b['type'] == 'text')
        self.assertIn('Earlier image omitted', text)
        self.assertIn('capture %d' % (MAX_INLINE_IMAGES + 2), text)

    def test_drop_count_stable_between_steps(self):
        # One more screenshot must not change which earlier images are kept (prompt cache).
        _, a, _ = cli_request(payload([CUA], images=MAX_INLINE_IMAGES + 2))
        _, b, _ = cli_request(payload([CUA], images=MAX_INLINE_IMAGES + 3))
        self.assertEqual([x for x in a if x['type'] == 'text'][:10], [x for x in b if x['type'] == 'text'][:10])

    def test_large_screenshot_downscaled(self):
        url = fit_image(png(3840, 2160))
        data = base64.b64decode(url.split(',', 1)[1])
        with Image.open(io.BytesIO(data)) as im:
            self.assertLessEqual(max(im.size), MAX_IMAGE_SIDE)
        small = png(800, 600)
        self.assertEqual(fit_image(small), small)


if __name__ == '__main__':
    unittest.main()

