"""Non-network safety checks for the trusted download service operator."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / 'temporary-download-worker'
          / 'operator.py')
spec = importlib.util.spec_from_file_location('download_operator', SOURCE)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


class OperatorTests(unittest.TestCase):
    def test_endpoint_safety(self):
        self.assertEqual(operator.base_url('https://example.workers.dev/'),
                         'https://example.workers.dev')
        for url in ('http://example.com', 'https://github.com',
                    'https://localhost', 'https://example.com/path',
                    'https://127.0.0.1', 'https://raw.githubusercontent.com/a',
                    'https://u:p@example.com', 'https://example.com?token=x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                operator.base_url(url)

    def test_download_url_file_is_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'issued.json'
            data = {'url': 'https://test.workers.dev/d/' + 'a' * 64, 'kind': 'canary'}
            operator.save_secret_json(str(path), data)
            self.assertEqual(operator.load_issued(path)[1], 'a' * 64)
            self.assertEqual(json.loads(path.read_text()), data)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                operator.save_secret_json(str(path), data)

    def test_digest_exact_file_and_empty_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.apk'
            path.write_bytes(b'abc')
            self.assertEqual(operator.digest(path)[0], 3)
            path.write_bytes(b'')
            with self.assertRaises(ValueError):
                operator.digest(path)


if __name__ == '__main__':
    unittest.main()
