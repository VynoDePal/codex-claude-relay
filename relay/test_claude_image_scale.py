"""Downscaled screenshots must tell Claude how to convert coordinates back to page pixels."""
import base64
import io
import unittest

from PIL import Image

from claude_relay import MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE, cli_request, fit_image_info


def png(w, h):
    out = io.BytesIO()
    Image.new('RGB', (w, h), (40, 40, 40)).save(out, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


class ImageScaleTest(unittest.TestCase):
    def test_chrome_viewport_gets_scale_note(self):
        url, note = fit_image_info(png(1920, 927))
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as im:
            w, h = im.size
        self.assertLessEqual(max(w, h), MAX_IMAGE_SIDE)
        self.assertLessEqual(w * h, MAX_IMAGE_PIXELS)
        self.assertIn('downscaled from 1920x927', note)
        self.assertIn(f'{1920 / w:.4f}', note)

    def test_small_image_has_no_note(self):
        url = png(1280, 720)
        self.assertEqual(fit_image_info(url), (url, ''))

    def test_note_reaches_claude_next_to_image(self):
        payload = {'model': 'claude-code-opus', 'reasoning': {'effort': 'high'}, 'tools': [], 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': png(1920, 927)}]}]}
        _, blocks, _ = cli_request(payload)
        text = ' '.join(b['text'] for b in blocks if b['type'] == 'text')
        self.assertIn('Attached image 1 from this message; shown at', text)


if __name__ == '__main__':
    unittest.main()

