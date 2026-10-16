"""Offline checks for llms.txt and the per-page Markdown copies."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
import paths

# Tests also work in a fresh clone without a personal site.yaml.
with patch.object(paths, 'SITE_CONFIG_PATH', paths.SITE_CONFIG_EXAMPLE_PATH):
    import build_blog

SITE = build_blog.SITE_URL


class MarkdownCopies(unittest.TestCase):
    def test_relative_targets_become_absolute(self):
        text = ('[old](older.html) [deep](older.html#part) [gone](missing/x)\n'
                '![pic](assets/a.png "title") [ep](audio/e.mp3)\n'
                '[root](/about.html) [out](https://example.org/) [top](#here)\n'
                '<img src="assets/b.png" srcset="assets/b.png 1x, assets/c.png 2x">\n'
                '[ref]: assets/d.png\n'
                '[^1]: A footnote, not a link definition.\n')
        out = build_blog._absolutize_markdown(text, {'older.html'})
        self.assertIn(f'[old]({SITE}/posts/older.html)', out)
        self.assertIn(f'[deep]({SITE}/posts/older.html#part)', out)
        self.assertIn(f'[gone]({SITE}/missing/x)', out)
        self.assertIn(f'![pic]({SITE}/assets/a.png "title")', out)
        self.assertIn(f'[ep]({SITE}/audio/e.mp3)', out)
        self.assertIn(f'[root]({SITE}/about.html)', out)
        self.assertIn('[out](https://example.org/)', out)
        self.assertIn('[top](#here)', out)
        self.assertIn(f'src="{SITE}/assets/b.png"', out)
        self.assertIn(f'srcset="{SITE}/assets/b.png 1x, {SITE}/assets/c.png 2x"', out)
        self.assertIn(f'[ref]: {SITE}/assets/d.png', out)
        self.assertIn('[^1]: A footnote, not a link definition.', out)

    def test_page_description_does_not_swallow_the_body(self):
        out = build_blog._page_markdown('About', 'Who writes here.', 'First line.')
        self.assertEqual(out, '# About\n\n> Who writes here.\n\nFirst line.\n')

    def test_page_with_its_own_heading_keeps_it(self):
        out = build_blog._page_markdown('About', 'Ignored.', '# Mine\n\nBody')
        self.assertEqual(out, '# Mine\n\nBody\n')

    def test_post_copy_carries_metadata_then_body(self):
        post = {'title': 'A [Bracketed]\nTitle', 'slug': 'a.html', 'date': '2026-01-02',
                'tags': ['One', 'Two'], 'summary': 'Short.', 'markdown_body': '\nBody\n'}
        out = build_blog._post_markdown(post, set())
        self.assertTrue(out.startswith('# A [Bracketed] Title\n\n> Short.\n\n'))
        self.assertIn('- Tags: One, Two\n', out)
        self.assertIn(f'- Web page: {SITE}/posts/a.html\n', out)
        self.assertTrue(out.endswith('---\n\nBody\n'))


class LlmsTxt(unittest.TestCase):
    def test_structure_follows_the_spec(self):
        posts = [{'title': 'Brackets [here]', 'slug': 'b c.html', 'date': '2026-01-02',
                  'summary': 'Line one\nline two.', 'episode': None}]
        pages = [{'title': 'About', 'href': '/about.html', 'description': ''},
                 {'title': 'Home', 'href': '/index.html', 'description': ''}]
        out = build_blog.llms_txt(posts, pages)
        lines = out.splitlines()
        self.assertEqual(lines[0], f'# {build_blog.SITE_NAME}')
        self.assertTrue(lines[2].startswith('> '))
        self.assertIn(f'- [Brackets \\[here\\]]({SITE}/posts/b%20c.html.md): '
                      '2026-01-02 - Line one line two.', lines)
        self.assertIn(f'- [About]({SITE}/about.html.md)', lines)
        self.assertLess(lines.index(f'- [Home]({SITE}/index.html.md)'),
                        lines.index(f'- [About]({SITE}/about.html.md)'))
        headings = [line for line in lines if line.startswith('## ')]
        self.assertEqual(headings, ['## Posts', '## Pages', '## Optional'])


if __name__ == '__main__':
    unittest.main()
