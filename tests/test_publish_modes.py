"""Exercise shell mode dispatch without models or deployment credentials."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublishModes(unittest.TestCase):
    def test_ingest_without_ftp_config_and_deploy_still_requires_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / 'publish.sh', root / 'publish.sh')
            shutil.copy(ROOT / 'site.example.yaml', root / 'site.example.yaml')
            shutil.copytree(ROOT / 'content_pipeline', root / 'content_pipeline',
                            ignore=lambda _, names: [n for n in names if n not in ('TEMPLATE.md', 'VOICE.example.md')])
            (root / '.setup_done').touch()
            result = subprocess.run(['bash', str(root / 'publish.sh'), '--ingest'],
                                    cwd='/tmp', text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('No new drafts', result.stdout)
            (root / 'engine').mkdir()
            (root / 'engine' / 'ingest.py').write_text('print("INGEST_REACHED")\nraise SystemExit(1)\n')
            draft = root / 'content_pipeline' / 'sources' / 'test.md'
            draft.write_text('Draft')
            result = subprocess.run(['bash', str(root / 'publish.sh'), '--ingest'],
                                    text=True, capture_output=True)
            self.assertIn('INGEST_REACHED', result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(draft.read_text(), 'Draft')
            result = subprocess.run(['bash', str(root / 'publish.sh'), '--deploy'],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Missing', result.stdout)
