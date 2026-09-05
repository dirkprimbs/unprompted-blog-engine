"""Offline checks for durable announcement retries; no real network writes."""
import io
import json
import os
import urllib.error
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
    import announce


def http_error(code, name='InvalidRequest'):
    return urllib.error.HTTPError('https://example.test', code, name, {},
                                  io.BytesIO(json.dumps({'error': name}).encode()))


class AnnouncementSafety(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, MASTODON_TOKEN='test-token',
                              MASTODON_SERVER='example.test')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sleep = patch.object(announce.time, 'sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.saved = {}

    def attempt(self):
        state = json.loads(json.dumps(self.saved))

        def save():
            self.saved = json.loads(json.dumps(state))

        return announce._Attempt(state, save)

    def test_permanent_http_errors_are_not_retried(self):
        with patch.object(announce.urllib.request, 'urlopen', side_effect=http_error(422)) as send:
            with self.assertRaises(urllib.error.HTTPError):
                announce._request('https://example.test', data=b'create')
        self.assertEqual(send.call_count, 1)

    def test_transient_http_errors_retry_with_same_request(self):
        with patch.object(announce.urllib.request, 'urlopen', side_effect=[
                http_error(503), io.BytesIO(b'{"id":"123"}')]) as send:
            result = announce._request('https://example.test', data=b'payload',
                                       headers={'Idempotency-Key': 'stable'})
        self.assertEqual(result, {'id': '123'})
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.args[0].data, b'payload')
            self.assertEqual(call.args[0].get_header('Idempotency-key'), 'stable')

    def test_mastodon_response_loss_reuses_key_after_restart(self):
        remote = {}
        requests = []

        def send(request, **kwargs):
            if request.full_url.endswith('/bookmark'):
                return io.BytesIO(b'{}')
            key = request.get_header('Idempotency-key')
            self.assertEqual(key, self.saved['key'])  # persisted before send
            requests.append((key, request.data))
            if key not in remote:
                remote[key] = {'id': '123', 'url': 'https://example.test/@me/123'}
                raise TimeoutError('accepted, response lost')
            return io.BytesIO(json.dumps(remote[key]).encode())

        with patch.object(announce.urllib.request, 'urlopen', side_effect=send), \
                patch.object(announce, 'MAX_RETRIES', 1):
            with self.assertRaises(TimeoutError):
                announce.announce_mastodon('Title', 'Summary', 'https://site.test/post', [],
                                           attempt=self.attempt())
            coordinates, _ = announce.announce_mastodon(
                'Edited title', 'Edited summary', 'https://site.test/new-url', [],
                attempt=self.attempt())
        self.assertEqual(coordinates['mastodon_id'], '"123"')
        self.assertEqual(len(remote), 1)
        self.assertEqual(requests[0], requests[1])

    def test_mastodon_expired_uncertain_attempt_does_not_post(self):
        attempt = self.attempt()
        with patch.object(announce, '_mastodon_post', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                announce.announce_mastodon('Title', '', 'https://site.test', [], attempt=attempt)
        self.saved['started'] -= 3600
        with patch.object(announce.urllib.request, 'urlopen') as send:
            with self.assertRaisesRegex(RuntimeError, 'too old'):
                announce.announce_mastodon('Title', '', 'https://site.test', [],
                                           attempt=self.attempt())
        send.assert_not_called()

    def test_saved_response_recovers_even_after_key_expiry(self):
        with patch.object(announce, '_mastodon_post', return_value={'id': '123'}):
            announce.announce_mastodon('Title', '', 'https://site.test', [], attempt=self.attempt())
        self.saved['started'] -= 3600
        with patch.object(announce, '_mastodon_post', return_value={}) as send:
            result, _ = announce.announce_mastodon('Title', '', 'https://site.test', [],
                                                  attempt=self.attempt())
        self.assertEqual(result['mastodon_id'], '"123"')
        self.assertEqual(send.call_args.args[0], '/api/v1/statuses/123/bookmark')
        self.assertEqual(send.call_count, 1)

    def test_bluesky_response_loss_recovers_exact_record_after_restart(self):
        remote = {}
        session = {'did': 'did:plc:test', 'accessJwt': 'test'}

        def create(nsid, payload, token=None):
            if nsid.endswith('createBookmark'):
                return {}
            self.assertEqual(payload['rkey'], self.saved['key'])
            self.assertRegex(payload['rkey'], r'^[234567abcdefghij][234567abcdefghijklmnopqrstuvwxyz]{12}$')
            remote.update(uri='at://did:plc:test/app.bsky.feed.post/' + payload['rkey'],
                          cid='test-cid', value=payload['record'])
            raise TimeoutError('accepted, response lost')

        with patch.object(announce, '_bluesky_session', return_value=session), \
                patch.object(announce, '_bluesky_call', side_effect=create) as send:
            with patch.object(announce, '_request', side_effect=[http_error(400, 'RecordNotFound'),
                                                                TimeoutError('offline')]):
                with self.assertRaises(TimeoutError):
                    announce.announce_bluesky('Title', '', 'https://site.test', [],
                                              attempt=self.attempt())
            with patch.object(announce, '_request', return_value=remote):
                coordinates, _ = announce.announce_bluesky('Changed', '', 'https://site.test', [],
                                                          attempt=self.attempt())
        creates = [c for c in send.call_args_list if c.args[0].endswith('createRecord')]
        self.assertEqual(len(creates), 1)
        self.assertIn(remote['uri'], coordinates['bluesky_uri'])

    def test_bluesky_different_record_is_not_adopted_or_overwritten(self):
        with patch.object(announce, '_bluesky_session', return_value={'did': 'did:plc:test', 'accessJwt': 'test'}), \
                patch.object(announce, '_request', return_value={'value': {'text': 'Someone else'}}), \
                patch.object(announce, '_bluesky_call') as send:
            with self.assertRaisesRegex(ValueError, 'different content'):
                announce.announce_bluesky('Title', '', 'https://site.test', [], attempt=self.attempt())
        send.assert_not_called()

    def test_failed_checkpoint_never_sends(self):
        attempt = announce._Attempt({}, lambda: (_ for _ in ()).throw(OSError('disk full')))
        with patch.object(announce, '_mastodon_post') as send:
            with self.assertRaises(OSError):
                announce.announce_mastodon('Title', '', 'https://site.test', [], attempt=attempt)
        send.assert_not_called()

    def test_atomic_write_failure_preserves_article(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'post.md'
            path.write_text('Original article')
            with patch.object(announce.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    announce._atomic_write(str(path), 'Replacement')
            self.assertEqual(path.read_text(), 'Original article')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_provider_success_survives_other_provider_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'post.md'
            path.write_text('---\ntitle: Test\nslug: test\nannounce: pending\n---\nBody\n')
            sends = []

            def provider(*args, attempt):
                state = attempt.prepare('account', {'text': 'Test'}, 'stable-key')
                on_disk = path.read_text()
                self.assertIn('stable-key', on_disk)
                if 'response' not in state:
                    sends.append('sent')
                    attempt.complete({'id': '123'})
                return {'mastodon_host': 'example.test', 'mastodon_id': '"123"'}, 'link'

            def unavailable(*args, **kwargs):
                raise TimeoutError('offline')

            providers = ({'name': 'Mastodon', 'keys': ('mastodon_id',), 'enabled': lambda: True,
                          'announce': provider},
                         {'name': 'Bluesky', 'keys': ('bluesky_uri',), 'enabled': lambda: True,
                          'announce': unavailable})
            with patch.object(announce, 'CONTENT_DIR', directory), \
                    patch.object(announce, 'PROVIDERS', providers), \
                    patch.object(announce, 'ANNOUNCE_DELAY_SECONDS', 0):
                announce.announce_all()
                announce.announce_all()
            self.assertEqual(sends, ['sent'])
            self.assertIn('announce: pending', path.read_text())
            self.assertIn('mastodon_id: "123"', path.read_text())

    def test_restart_after_checkpoint_recovers_and_removes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'post.md'
            path.write_text('---\ntitle: Test\nannounce: pending\n---\nBody\n')
            sends = []

            def provider(*args, attempt):
                state = attempt.prepare('account', {'text': 'Test'}, 'stable-key')
                if 'response' not in state:
                    sends.append('sent')
                    attempt.complete({'id': '123'})
                    raise KeyboardInterrupt('process stopped before frontmatter settled')
                return {'mastodon_id': '"123"'}, 'link'

            providers = ({'name': 'Mastodon', 'keys': ('mastodon_id',),
                          'enabled': lambda: True, 'announce': provider},)
            with patch.object(announce, 'CONTENT_DIR', directory), \
                    patch.object(announce, 'PROVIDERS', providers):
                with self.assertRaises(KeyboardInterrupt):
                    announce.announce_all()
                self.assertIn('announcement_state:', path.read_text())
                self.assertEqual(announce.announce_all(), 1)
            self.assertEqual(sends, ['sent'])
            self.assertIn('mastodon_id: "123"', path.read_text())
            self.assertNotIn('announcement_state:', path.read_text())
            self.assertNotIn('announce: pending', path.read_text())

    def test_malformed_state_does_not_send_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'post.md'
            original = ('---\ntitle: Test\nannounce: pending\n'
                        'announcement_state:\n  Mastodon: {}\n---\nBody\n')
            path.write_text(original)
            with patch.object(announce, 'CONTENT_DIR', directory), \
                    patch.object(announce, '_mastodon_post') as send:
                with self.assertRaises(ValueError):
                    announce.announce_all()
            self.assertEqual(path.read_text(), original)
            send.assert_not_called()
