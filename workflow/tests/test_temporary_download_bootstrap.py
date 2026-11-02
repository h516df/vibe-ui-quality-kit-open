"""Cloudflare provisioning dry-run, idempotency, and safe boundary checks."""

import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'temporary-download-worker' / 'bootstrap.py'
spec = importlib.util.spec_from_file_location('apk_bootstrap', SOURCE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
ACCOUNT = 'ab' * 16
UUID = '12345678-1234-5678-abcd-1234567890ab'


class FakeCloudflare:
    def __init__(self, *, subdomain='my-worker-subdomain', bucket=False, database=False):
        self.subdomain = subdomain
        self.bucket = bucket
        self.database = database
        self.calls = []

    def call(self, method, path, payload=None, allow_missing=False):
        self.calls.append((method, path, payload))
        if path == '/workers/subdomain':
            return {'subdomain': self.subdomain} if self.subdomain else None
        if path.startswith('/r2/buckets/') and method == 'GET':
            return {'name': path.rsplit('/', 1)[1]} if self.bucket else None
        if path.startswith('/d1/database?'):
            return [{'name': bootstrap.resource_names(ACCOUNT)['database'], 'uuid': UUID}] if self.database else []
        if method == 'POST' and path == '/r2/buckets':
            self.bucket = True
            return {'name': payload['name']}
        if method == 'POST' and path == '/d1/database':
            self.database = True
            return {'name': payload['name'], 'uuid': UUID}
        raise AssertionError(f'Unexpected API call: {method} {path}')


class BootstrapTests(unittest.TestCase):
    def test_names_are_unique_and_restricted_to_account(self):
        names = bootstrap.resource_names(ACCOUNT)
        self.assertTrue(all(name.endswith('abababab') for name in names.values()))
        for invalid in ('', 'x' * 32, 'not-a-secret'):
            with self.subTest(invalid=invalid), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.resource_names(invalid)

    def test_plan_is_read_only(self):
        api = FakeCloudflare()
        info = bootstrap.ensure_resources(api, bootstrap.resource_names(ACCOUNT), apply=False)
        self.assertFalse(info['bucket_exists'])
        self.assertFalse(info['database_exists'])
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_apply_creates_only_missing_names_and_reuses_after_restart(self):
        api = FakeCloudflare()
        names = bootstrap.resource_names(ACCOUNT)
        first = bootstrap.ensure_resources(api, names, apply=True)
        self.assertEqual(sum(method == 'POST' for method, _, _ in api.calls), 2)
        self.assertTrue(first['bucket_created'])
        self.assertTrue(first['database_created'])
        api.calls.clear()
        second = bootstrap.ensure_resources(api, names, apply=True)
        self.assertTrue(second['bucket_exists'])
        self.assertTrue(second['database_exists'])
        self.assertFalse(second['bucket_created'])
        self.assertFalse(second['database_created'])
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))
        config, origin = bootstrap.worker_config(names, second)
        self.assertTrue(origin.endswith('.workers.dev'))
        self.assertEqual(config['d1_databases'][0]['database_id'], UUID)

    def test_no_subdomain_has_no_mutation(self):
        api = FakeCloudflare(subdomain=None)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.ensure_resources(api, bootstrap.resource_names(ACCOUNT), apply=True)
        self.assertEqual([method for method, _, _ in api.calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
