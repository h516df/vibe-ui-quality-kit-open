"""Regression checks for premature or misleading release delivery receipts."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/verify_release.py'
SPEC = importlib.util.spec_from_file_location('release_verifier', SCRIPT)
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)
SHA = 'a' * 40
TAG_SHA = 'b' * 40


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'app.apk').write_bytes(b'synthetic APK fixture')
        digest = hashlib.sha256((self.root / 'app.apk').read_bytes()).hexdigest()
        self.contract = {'schema_version': 1, 'repository': 'owner/project', 'tag': 'v1.0.0',
                         'kind': 'release', 'require_latest': True, 'target_commit': SHA,
                         'assets': [{'name': 'app.apk', 'path': 'app.apk',
                                     'size': (self.root / 'app.apk').stat().st_size, 'sha256': digest}]}
        self.release = {'id': 12, 'tag_name': 'v1.0.0', 'draft': False, 'prerelease': False,
                        'assets': [{'name': 'app.apk', 'state': 'uploaded',
                                    'size': self.contract['assets'][0]['size'], 'digest': 'sha256:' + digest,
                                    'browser_download_url': 'https://github.com/owner/project/releases/download/v1.0.0/app.apk'}]}
        self.responses = {'/repos/owner/project/releases/tags/v1.0.0': self.release,
                          '/repos/owner/project/releases/latest': {'id': 12, 'tag_name': 'v1.0.0'},
                          '/repos/owner/project/git/ref/tags/v1.0.0': {'object': {'type': 'commit', 'sha': SHA}}}
        self.calls = []

    def api(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.responses[path])

    def check(self):
        v.validate(self.contract, self.root)
        return v.verify(self.contract, self.api)

    def test_real_native_asset_formal_state_and_pinned_source(self):
        result = self.check()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['target_commit'], SHA)
        self.assertTrue(result['latest_checked'])
        self.assertEqual(result['assets'][0]['sha256'], self.contract['assets'][0]['sha256'])
        self.assertIn('not tested', result['verification_scope'])
        self.assertFalse(result['public_repository_checked'])
        self.assertNotIn('/repos/owner/project', self.calls)

    def test_public_delivery_fails_before_release_checks_for_private_or_unknown_repository(self):
        self.contract['require_public'] = True
        for metadata in ({'private': True, 'visibility': 'private'}, {}, None,
                         {'private': False, 'visibility': 'internal'}):
            with self.subTest(metadata=metadata):
                self.calls.clear()
                self.responses['/repos/owner/project'] = metadata
                with self.assertRaisesRegex(v.Failure, 'Public delivery requires a public repository'):
                    self.check()
                self.assertEqual(self.calls, ['/repos/owner/project'])

    def test_public_repository_is_checked_without_claiming_anonymous_download(self):
        self.contract['require_public'] = True
        self.responses['/repos/owner/project'] = {'private': False, 'visibility': 'public'}
        result = self.check()
        self.assertTrue(result['public_repository_checked'])
        self.assertEqual(result['anonymous_download_verification'], 'not_verified')
        self.assertEqual(self.calls.count('/repos/owner/project'), 1)

    def test_public_requirement_is_explicit_boolean_and_false_preserves_private_delivery(self):
        for value in ('true', 1, None):
            self.contract['require_public'] = value
            with self.assertRaisesRegex(v.Failure, 'require_public must be a boolean'):
                self.check()
        self.assertEqual(self.calls, [])
        self.contract['require_public'] = False
        self.assertEqual(self.check()['status'], 'verified')
        self.assertNotIn('/repos/owner/project', self.calls)

    def test_draft_or_prerelease_never_passes_formal_delivery(self):
        for key in ('draft', 'prerelease'):
            with self.subTest(key=key):
                self.release[key] = True
                self.release['name'] = 'Official stable release, all done'
                with self.assertRaises(v.Failure):
                    self.check()
                self.release[key] = False

    def test_body_link_and_source_zip_do_not_replace_installable_attachment(self):
        self.release['body'] = '[Download APK](https://github.com/owner/project/blob/main/app.apk?raw=true)'
        self.release['zipball_url'] = 'https://api.github.com/repos/owner/project/zipball/v1.0.0'
        self.release['assets'] = []
        with self.assertRaisesRegex(v.Failure, 'native Release asset'):
            self.check()

    def test_corrupt_partial_or_unverifiable_attachment_fails(self):
        original = copy.deepcopy(self.release['assets'][0])
        for key, value in [('size', original['size'] + 1), ('size', True),
                           ('digest', 'sha256:' + '0' * 64), ('digest', None), ('state', 'starter')]:
            with self.subTest(key=key, value=value):
                self.release['assets'][0] = dict(original, **{key: value})
                with self.assertRaises(v.Failure):
                    self.check()
        self.release['assets'][0] = original

    def test_download_link_must_address_the_same_repository_tag_and_file(self):
        for url in ['https://example.org/app.apk',
                    'http://github.com/owner/project/releases/download/v1.0.0/app.apk',
                    'https://github.com/owner/other/releases/download/v1.0.0/app.apk',
                    'https://github.com/owner/project/releases/download/v0.9/app.apk',
                    'https://github.com/owner/project/releases/download/v1.0.0/source.zip',
                    'https://[malformed',
                    'https://token@github.com/owner/project/releases/download/v1.0.0/app.apk']:
            with self.subTest(url=url):
                self.release['assets'][0]['browser_download_url'] = url
                with self.assertRaises(v.Failure):
                    self.check()

    def test_latest_mismatch_and_wrong_release_id_fail(self):
        for latest in [{'id': 13, 'tag_name': 'v1.0.0'}, {'id': 12, 'tag_name': 'v0.9'}]:
            self.responses['/repos/owner/project/releases/latest'] = latest
            with self.assertRaisesRegex(v.Failure, 'latest'):
                self.check()

    def test_full_target_sha_and_annotated_tag_are_verified(self):
        self.responses['/repos/owner/project/git/ref/tags/v1.0.0']['object'] = {'type': 'tag', 'sha': TAG_SHA}
        self.responses['/repos/owner/project/git/tags/' + TAG_SHA] = {'object': {'type': 'commit', 'sha': SHA}}
        self.assertEqual(self.check()['target_commit'], SHA)
        self.responses['/repos/owner/project/git/tags/' + TAG_SHA]['object']['sha'] = 'c' * 40
        with self.assertRaisesRegex(v.Failure, 'pinned commit'):
            self.check()
        self.responses['/repos/owner/project/git/tags/' + TAG_SHA]['object'] = {'type': 'tag', 'sha': TAG_SHA}
        with self.assertRaisesRegex(v.Failure, 'does not resolve'):
            self.check()

    def test_explicit_prerelease_is_allowed_without_imposing_formal_release(self):
        self.contract.update(kind='prerelease', require_latest=False)
        self.release['prerelease'] = True
        result = self.check()
        self.assertTrue(result['prerelease'])
        self.assertFalse(result['latest_checked'])
        self.assertNotIn('/repos/owner/project/releases/latest', self.calls)

    def test_invalid_contract_and_local_file_fail_before_network(self):
        original = copy.deepcopy(self.contract)
        for key, value in [('schema_version', True), ('require_latest', 'true'),
                           ('target_commit', SHA[:7]), ('assets', [])]:
            self.contract = dict(original, **{key: value})
            with self.assertRaises(v.Failure):
                self.check()
        self.contract = copy.deepcopy(original)
        self.contract['assets'][0]['path'] = '../outside.apk'
        with self.assertRaises(v.Failure):
            self.check()
        self.contract = copy.deepcopy(original)
        (self.root / 'app.apk').write_bytes(b'changed fixture')
        with self.assertRaisesRegex(v.Failure, 'Local asset'):
            self.check()
        self.assertEqual(self.calls, [])

    def test_pinned_metadata_can_be_used_without_copying_binary_into_rules_branch(self):
        del self.contract['assets'][0]['path']
        (self.root / 'app.apk').unlink()
        self.assertEqual(self.check()['status'], 'verified')

    def test_failure_replaces_stale_success_receipt_and_exits_nonzero(self):
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps(self.contract))
        receipt = self.root / 'receipt.json'
        receipt.write_text('{"status":"verified","old":true}')
        with patch.object(v, 'verify', side_effect=v.Failure('Release is still a draft.')):
            with redirect_stdout(io.StringIO()):
                status = v.main(['--contract', str(contract), '--root', str(self.root), '--receipt', str(receipt)])
        self.assertEqual(status, 1)
        result = json.loads(receipt.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('old', result)
        self.assertIn('checked_at', result)

    def test_cli_permission_errors_do_not_leak_signed_urls_or_credentials(self):
        error = subprocess.CompletedProcess([], 1, '', 'HTTP 403 https://example.org/?sig=secret-token')
        with patch.object(v.subprocess, 'run', return_value=error):
            with self.assertRaises(v.Failure) as caught:
                v.github('/repos/owner/project/releases/latest')
        self.assertIn('HTTP 403', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))
        self.assertNotIn('https://', str(caught.exception))

    def test_receipt_cannot_overwrite_contract_or_installable_artifact(self):
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps(self.contract))
        for destination in (contract, self.root / 'app.apk'):
            before = destination.read_bytes()
            with self.subTest(path=destination.name):
                with redirect_stdout(io.StringIO()), patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        v.main(['--contract', str(contract), '--root', str(self.root), '--receipt', str(destination)])
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(destination.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
