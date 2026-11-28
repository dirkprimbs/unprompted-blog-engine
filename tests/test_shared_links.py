"""Offline checks for the shared links file, footer.links and the current-site
marker in a menu."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
import paths

# Tests also work in a fresh clone without a personal site.yaml.
with patch.object(paths, 'SITE_CONFIG_PATH', paths.SITE_CONFIG_EXAMPLE_PATH):
    import build_blog
    import config

SITE = build_blog.SITE_URL


def load_network(text, language='en'):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'network.yaml'
        path.write_text(text, encoding='utf-8')
        return config._load_network(str(path), language)


NETWORK = '''
blog:
  label: "Blog"
  href: "https://blog.example.org"
talks:
  label: { en: "Talks", de: "Vorträge" }
  href: "https://talks.example.org"
shows:
  label: "Podcasts"
  items:
    - label: "First"
      href: "https://first.example.org"
    - label: { en: "All shows", de: "Alle Sendungen" }
      href: "https://example.org/shows.html"
'''


class SharedLinks(unittest.TestCase):
    def test_labels_follow_the_site_language(self):
        self.assertEqual(load_network(NETWORK, 'en')['talks']['label'], 'Talks')
        self.assertEqual(load_network(NETWORK, 'de-DE')['talks']['label'], 'Vorträge')
        self.assertEqual(load_network(NETWORK, 'de')['shows']['items'][1]['label'],
                         'Alle Sendungen')

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(load_network(NETWORK, 'fr')['talks']['label'], 'Talks')

    def test_json_is_accepted(self):
        net = load_network('{"blog": {"label": "Blog", "href": "https://b.example"}}')
        self.assertEqual(net['blog'], {'label': 'Blog', 'href': 'https://b.example',
                                       'items': []})

    def test_reference_resolves_and_can_be_renamed(self):
        net = load_network(NETWORK)
        plain = config._link_entry({'network': 'blog'}, 'nav[0]', True, net)
        self.assertEqual((plain['label'], plain['href']),
                         ('Blog', 'https://blog.example.org'))
        renamed = config._link_entry({'network': 'blog', 'label': 'Journal'},
                                     'nav[0]', True, net)
        self.assertEqual((renamed['label'], renamed['href']),
                         ('Journal', 'https://blog.example.org'))

    def test_reference_brings_its_submenu(self):
        net = load_network(NETWORK)
        entry = config._link_entry({'network': 'shows'}, 'nav[0]', True, net)
        self.assertEqual([c['label'] for c in entry['items']], ['First', 'All shows'])

    def test_bad_references_stop_the_build(self):
        net = load_network(NETWORK)
        for item, allow_children, network in (
            ({'network': 'nope'}, True, net),                   # undefined key
            ({'network': 'blog'}, True, {}),                    # no file configured
            ({'network': 'shows'}, False, net),                 # submenu in a submenu
            ({'network': 'blog', 'href': '/x'}, True, net),     # target overridden
        ):
            with self.subTest(item=item), patch('builtins.print'):
                with self.assertRaises(SystemExit):
                    config._link_entry(item, 'nav[0]', allow_children, network)

    def test_shared_file_cannot_refer_to_itself(self):
        with patch('builtins.print'), self.assertRaises(SystemExit):
            load_network('a:\n  network: b\n')


class MenuMarkup(unittest.TestCase):
    def test_own_front_page_is_marked_current(self):
        html = build_blog._nav_link_html({'label': 'Here', 'href': SITE, 'items': []})
        self.assertIn('aria-current="true"', html)
        self.assertNotIn('target="_blank"', html)

    def test_relative_home_and_other_pages_are_not(self):
        for href in ('/', f'{SITE}/about.html', 'https://elsewhere.example/'):
            html = build_blog._nav_link_html({'label': 'x', 'href': href, 'items': []})
            self.assertNotIn('aria-current', html, href)

    def test_footer_row_is_empty_without_links(self):
        with patch.object(build_blog, 'FOOTER_LINKS', []):
            self.assertEqual(build_blog._footer_links_html(), '')

    def test_footer_row_renders_links(self):
        links = [{'label': 'Contact', 'href': 'mailto:a@example.org', 'items': []},
                 {'label': 'Imprint & more', 'href': '/imprint.html', 'items': []}]
        with patch.object(build_blog, 'FOOTER_LINKS', links):
            html = build_blog._footer_links_html()
        self.assertTrue(html.startswith('<nav class="footer-links"'))
        self.assertIn('<a href="mailto:a@example.org">Contact</a>', html)
        self.assertIn('<a href="/imprint.html">Imprint &amp; more</a>', html)


if __name__ == '__main__':
    unittest.main()
