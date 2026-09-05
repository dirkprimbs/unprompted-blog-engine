"""Offline checks for article and URL collision safety."""
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
    import ingest


class IngestionSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.override = patch.object(ingest, 'CONTENT_DIR', self.tmp.name)
        self.override.start()
        self.addCleanup(self.override.stop)

    def test_existing_file_and_coordinates_survive(self):
        path = self.directory / '2026-09-05_Repeat.md'
        original = '---\nslug: repeat\nmastodon_id: "123"\n---\nOriginal'
        path.write_text(original)
        with self.assertRaises(FileExistsError):
            ingest.write_new_post(str(path), 'repeat', 'Replacement')
        self.assertEqual(path.read_text(), original)

    def test_slug_collision_across_dates_and_html_suffix(self):
        path = self.directory / 'old.md'
        path.write_text('---\nslug: "repeat.html"\n---\nOriginal')
        destination = self.directory / 'new.md'
        with self.assertRaises(FileExistsError):
            ingest.write_new_post(str(destination), 'repeat', 'Replacement')
        self.assertFalse(destination.exists())

    def test_legacy_filename_reserves_its_url(self):
        (self.directory / 'repeat.md').write_text('---\ntitle: Old\n---\nOriginal')
        with self.assertRaises(FileExistsError):
            ingest.write_new_post(str(self.directory / 'new.md'), 'repeat', 'New')

    def test_exclusive_open_race_does_not_delete_winner(self):
        destination = self.directory / 'new.md'
        real_open = open

        def racing_open(path, mode='r', **kwargs):
            if mode == 'x':
                destination.write_text('Other writer won')
            return real_open(path, mode, **kwargs)

        with patch('builtins.open', racing_open), self.assertRaises(FileExistsError):
            ingest.write_new_post(str(destination), 'new', 'New')
        self.assertEqual(destination.read_text(), 'Other writer won')

    def test_unique_article_is_created(self):
        destination = self.directory / 'new.md'
        ingest.write_new_post(str(destination), 'new', 'New article')
        self.assertEqual(destination.read_text(), 'New article')
