import hashlib
import importlib.util
import io
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError


MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_temporary_download.py'
SPEC = importlib.util.spec_from_file_location('verify_temporary_download', MODULE)
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)

DATA = b'APK canary fixture, never a real application'
CONTRACT = {'url': 'https://downloads.example.net/once/token-sensitive-value',
            'size': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest()}


class FakeResponse(io.BytesIO):
    def getcode(self):
        return 200


class FakeOpener:
    def __init__(self, status=200, body=DATA):
        self.status, self.body = status, body

    def open(self, request, timeout):
        if self.status != 200:
            raise HTTPError(request.full_url, self.status, 'rejected', {}, None)
        return FakeResponse(self.body)


class TemporaryDownloadTests(unittest.TestCase):
    def test_one_success_and_two_rejections(self):
        lock = threading.Lock()
        seen = []

        def fetch(url, size):
            with lock:
                result = (dict(kind='downloaded', size=len(DATA), sha256=CONTRACT['sha256'])
                          if not seen else dict(kind='rejected', http=410))
                seen.append(result)
                return result

        result = v.probe(CONTRACT, fetch=fetch)
        self.assertEqual(result['status'], 'canary_verified')
        self.assertEqual(len(seen), 3)
        self.assertEqual(result['expiry_60_minutes'], 'not_verified')
        self.assertEqual(result['recipient_link'], 'not_tested')

    def test_parallel_double_success_fails(self):
        count = 0
        lock = threading.Lock()

        def fetch(url, size):
            nonlocal count
            with lock:
                count += 1
                n = count
            return (dict(kind='downloaded', size=len(DATA), sha256=CONTRACT['sha256'])
                    if n <= 2 else dict(kind='rejected', http=410))

        self.assertEqual(v.probe(CONTRACT, fetch=fetch)['status'], 'failed')

    def test_wrong_content_and_error_fail(self):
        sequences = [
            [dict(kind='downloaded', size=len(DATA), sha256='0' * 64),
             dict(kind='rejected', http=410), dict(kind='rejected', http=410)],
            [dict(kind='downloaded', size=len(DATA), sha256=CONTRACT['sha256']),
             dict(kind='rejected', http=403), dict(kind='rejected', http=410)],
            [dict(kind='downloaded', size=len(DATA), sha256=CONTRACT['sha256']),
             dict(kind='rejected', http=410), dict(kind='downloaded', size=len(DATA), sha256=CONTRACT['sha256'])],
        ]
        for seq in sequences:
            with self.subTest(seq=seq):
                current = iter(seq)
                self.assertEqual(v.probe(CONTRACT, fetch=lambda *_: next(current))['status'], 'failed')

    def test_no_redirect_and_real_response_integrity(self):
        original = v.request_once(CONTRACT['url'], len(DATA), lambda: FakeOpener())
        self.assertEqual(original['sha256'], CONTRACT['sha256'])
        self.assertEqual(v.request_once(CONTRACT['url'], len(DATA), lambda: FakeOpener(302)),
                         {'kind': 'unexpected_status', 'http': 302})
        self.assertEqual(v.request_once(CONTRACT['url'], len(DATA), lambda: FakeOpener(410)),
                         {'kind': 'rejected', 'http': 410})
        self.assertEqual(v.request_once(CONTRACT['url'], len(DATA) - 1, lambda: FakeOpener())['kind'], 'oversize')

    def test_github_local_and_untrusted_url_rejected_before_request(self):
        urls = ['https://github.com/h516df/release.apk', 'https://raw.githubusercontent.com/a/b',
                'https://repo.github.io/example', 'http://example.net/a', 'https://localhost/a',
                'https://127.0.0.1/a', 'https://user:pass@example.net/a',
                'https://example.net/a#fragment', 'https://example.net:8443/a']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                v.validate_contract(dict(CONTRACT, url=url))

    def test_bad_contract_rejected(self):
        for update in ({'size': True}, {'size': 0}, {'sha256': 'bad'}, {'extra': 'oops'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                v.validate_contract(dict(CONTRACT, **update))


if __name__ == '__main__':
    unittest.main()
