"""Exercise the actual Cloudflare D1 SQLite schema and atomic claim SQL."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCHEMA = (Path(__file__).resolve().parents[1] / 'temporary-download-worker'
          / 'migrations' / '0001_init.sql').read_text(encoding='utf-8')
CLAIM = '''UPDATE download_tokens SET consumed_at = ?
           WHERE token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL
           AND expires_at > ? RETURNING token_hash'''


class DownloadSqlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'worker.sqlite3')
        with sqlite3.connect(self.path) as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO apk_objects VALUES (?, ?, ?, ?, ?, ?)',
                       ('app/v1/app.apk', 5, 'a' * 64, 'etag-original', 'app.apk', 100))
            db.execute('INSERT INTO download_tokens'
                       ' (token_hash, request_id, object_key, kind, issued_at, expires_at)'
                       ' VALUES (?, ?, ?, ?, ?, ?)',
                       ('b' * 64, 'test-request-id-2026', 'app/v1/app.apk',
                        'canary', 100, 3600100))

    def claim(self, at):
        with sqlite3.connect(self.path, timeout=5) as db:
            return db.execute(CLAIM, (at, 'b' * 64, at)).fetchone()

    def test_two_concurrent_consumers_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.claim, (101, 101)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertIsNone(self.claim(101))

    def test_expiry_boundary_is_exclusive(self):
        self.assertIsNone(self.claim(3600100))

    def test_expiry_and_request_id_invariants(self):
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO download_tokens'
                           ' (token_hash, request_id, object_key, kind, issued_at, expires_at)'
                           ' VALUES (?, ?, ?, ?, ?, ?)',
                           ('c' * 64, 'test-request-id-2026', 'app/v1/app.apk',
                            'recipient', 100, 3600100))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO download_tokens'
                           ' (token_hash, request_id, object_key, kind, issued_at, expires_at)'
                           ' VALUES (?, ?, ?, ?, ?, ?)',
                           ('c' * 64, 'another-request-id-2026', 'app/v1/app.apk',
                            'recipient', 100, 3600101))


if __name__ == '__main__':
    unittest.main()
