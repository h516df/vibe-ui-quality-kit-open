import copy
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/resume_status.py'
CLI = SCRIPT.with_name('workflow.py')
spec = importlib.util.spec_from_file_location('resume_status', SCRIPT)
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)
NOW = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)
HEAD = 'a' * 40


def snapshot():
    return dict(repository='fixture/product', observed_at=NOW.isoformat(), complete=True,
                pull_requests=[dict(number=2, title='Save and reopen', url='https://github.com/fixture/product/pull/2',
                                    headRefName='codex/save', headRefOid=HEAD, baseRefName='main',
                                    isDraft=True, statusCheckRollup=[])])


class ResumeTests(unittest.TestCase):
    def summary(self, data=None, **kwargs):
        return resume.summarize_snapshot(data or snapshot(), 'fixture/product',
                                         kwargs.get('branch', 'codex/save'), kwargs.get('head', HEAD),
                                         kwargs.get('dirty', False), now=NOW)

    def test_current_task_hint_does_not_assert_ownership_or_acceptance(self):
        result = self.summary()
        self.assertEqual(result['next_action'], 'inspect_required_checks')
        self.assertIsNone(result['automatically_selected_pr'])
        self.assertEqual(result['pull_requests'][0]['checks']['status'], 'not_run')

    def test_current_check_problem_is_not_a_continue_hint(self):
        for checks, action in (([{'conclusion': 'FAILURE'}], 'diagnose_current_checks'),
                               ([{'status': 'IN_PROGRESS'}], 'inspect_pending_checks'),
                               ([{'conclusion': 'SKIPPED'}], 'inspect_required_checks')):
            data = snapshot()
            data['pull_requests'][0]['statusCheckRollup'] = checks
            self.assertEqual(self.summary(data)['next_action'], action)

    def test_unrelated_checks_do_not_choose_or_block_current_work(self):
        data = snapshot()
        row = copy.deepcopy(data['pull_requests'][0])
        row.update(number=3, url='https://github.com/fixture/product/pull/3',
                   headRefName='codex/other', statusCheckRollup=[{'conclusion': 'FAILURE'}])
        data['pull_requests'].append(row)
        data['pull_requests'][0]['statusCheckRollup'] = [{'conclusion': 'SUCCESS'}]
        self.assertEqual(self.summary(data)['next_action'], 'continue_current_task')

    def test_ambiguous_matching_prs_do_not_select_a_check_result(self):
        data = snapshot()
        row = copy.deepcopy(data['pull_requests'][0])
        row.update(number=3, url='https://github.com/fixture/product/pull/3', baseRefName='release')
        data['pull_requests'].append(row)
        self.assertEqual(self.summary(data)['next_action'], 'review_related_work')

    def report(self):
        return dict(branch='codex/save', dirty=False, github=self.summary(),
                    delivery=dict(status='not_read', next_action='read_latest_release'),
                    product_acceptance='not_verified', writer_ownership='not_verified')

    def test_brief_preserves_changes_before_remote_diagnosis(self):
        result = self.report()
        result.update(dirty=True, github=dict(status='unavailable',
                      next_action='read_related_pull_requests_with_existing_connector'))
        brief = resume.recovery_brief(result)
        self.assertEqual(brief['next_action'], 'inspect_local_changes')
        self.assertTrue(brief['remote_read_incomplete'])
        self.assertEqual(brief['responsibility'], 'agent')
        self.assertFalse(brief['automatic_execution'])

    def test_brief_does_not_invent_ownership_acceptance_or_user_obligation(self):
        result = self.report()
        result['branch'] = None
        brief = resume.recovery_brief(result)
        self.assertEqual(brief['next_action'], 'identify_source_and_writer')
        self.assertEqual(brief['product_acceptance'], 'not_verified')
        self.assertEqual(brief['writer_ownership'], 'not_verified')
        self.assertEqual(brief['user_action'], 'not_determined_by_readonly_probe')

    def test_stale_or_partial_pr_only_snapshot_returns_incomplete(self):
        for change in ({'status': 'stale'}, {'complete': False}):
            result = self.report()
            result['github'].update(change, next_action='refresh_github_snapshot')
            with patch.object(resume, 'inspect', return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
                code = resume.resume(SimpleNamespace(target='fixture', github_json=None, github=False))
            self.assertEqual(code, 2)
            self.assertTrue(json.loads(output.getvalue())['brief']['remote_read_incomplete'])

    def test_local_only_is_explicitly_unread_without_becoming_failure(self):
        result = self.report()
        result['github'] = dict(status='not_read', next_action='read_related_pull_requests')
        with patch.object(resume, 'inspect', return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
            code = resume.resume(SimpleNamespace(target='fixture', github_json=None, github=False))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['brief']['remote_read_incomplete'])
        self.assertEqual(json.loads(output.getvalue())['github']['status'], 'not_read')

    def test_local_divergence_and_dirty_changes_take_priority(self):
        self.assertEqual(self.summary(head='b' * 40)['next_action'], 'compare_current_source')
        self.assertEqual(self.summary(dirty=True)['next_action'], 'inspect_local_changes')

    def test_never_picks_latest_pr_on_main(self):
        result = self.summary(branch='main')
        self.assertEqual(result['next_action'], 'review_related_work')
        self.assertIsNone(result['automatically_selected_pr'])

    def test_stale_and_truncated_snapshots_require_refresh(self):
        data = snapshot()
        data['observed_at'] = '2026-10-20T12:00:00+00:00'
        self.assertEqual(self.summary(data)['status'], 'stale')
        self.assertEqual(self.summary(data)['next_action'], 'refresh_github_snapshot')
        data = snapshot()
        data['complete'] = False
        self.assertEqual(self.summary(data)['next_action'], 'refresh_github_snapshot')

    def test_repository_source_and_pr_identity_are_checked(self):
        for mutate in (lambda d: d.update(repository='other/product'),
                       lambda d: d['pull_requests'][0].update(headRefOid=5),
                       lambda d: d['pull_requests'][0].update(url='https://example.invalid/instruction'),
                       lambda d: d['pull_requests'].append(copy.deepcopy(d['pull_requests'][0]))):
            data = snapshot()
            mutate(data)
            with self.assertRaises(ValueError):
                self.summary(data)

    def test_failed_pending_skipped_unknown_are_not_all_green(self):
        cases = [([], 'not_run'), ([{'conclusion': 'SUCCESS'}], 'passed'),
                 ([{'conclusion': 'SUCCESS'}, {'conclusion': 'FAILURE'}], 'failed'),
                 ([{'status': 'IN_PROGRESS'}], 'pending'),
                 ([{'conclusion': 'SKIPPED'}], 'inconclusive'), ([{}], 'inconclusive')]
        for checks, expected in cases:
            self.assertEqual(resume.check_summary(checks)['status'], expected)

    def test_credentials_never_accepted_as_repository_identity(self):
        for url in ('https://token@github.com/fixture/product.git', 'https://elsewhere.invalid/fixture/product.git'):
            with self.assertRaises(ValueError):
                resume.repository_name(url)
        self.assertEqual(resume.repository_name('git@github.com:fixture/product.git'), 'fixture/product')

    def test_duplicate_json_and_invalid_clock_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'snapshot.json'
            path.write_text('{"repository":1,"repository":2}')
            with self.assertRaises(ValueError):
                resume.read_snapshot(path)
        for clock in ('2026-10-23T12:00:00', '2026-10-24T12:00:00Z', None):
            data = snapshot()
            data['observed_at'] = clock
            with self.assertRaises(ValueError):
                self.summary(data)

    def test_real_git_recovery_preserves_source_index_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(['git', '-C', tmp, *args], capture_output=True, check=True, text=True).stdout
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Recovery fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('remote', 'add', 'origin', 'https://github.com/fixture/product.git')
            (root / 'HANDOFF.md').write_text('Existing product work\n')
            git('add', '.')
            git('commit', '-qm', 'fixture')
            (root / 'private-fixture.txt').write_text('synthetic sensitive fixture')
            before = (root / '.git/index').read_bytes()
            head = git('rev-parse', 'HEAD')
            result = subprocess.run([sys.executable, str(CLI), 'resume', '--target', tmp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['github']['status'], 'not_read')
            self.assertEqual(report['product_acceptance'], 'not_verified')
            self.assertTrue(report['dirty'])
            self.assertNotIn('private-fixture', result.stdout)
            self.assertEqual((root / '.git/index').read_bytes(), before)
            self.assertEqual(git('rev-parse', 'HEAD'), head)
            self.assertEqual(git('branch', '--show-current').strip(), 'main')
            self.assertEqual((root / 'HANDOFF.md').read_text(), 'Existing product work\n')
            real_run = resume.run
            def disconnected(target, argv, timeout=20):
                if argv[0] == 'gh':
                    raise subprocess.TimeoutExpired('gh', 20)
                return real_run(target, argv, timeout)
            with patch.object(resume, 'run', side_effect=disconnected):
                partial = resume.inspect(root, github=True)
            self.assertEqual(partial['head'], head.strip())
            self.assertEqual(partial['github']['status'], 'unavailable')
            self.assertEqual(partial['github']['reason'], 'timeout')
            self.assertEqual((root / '.git/index').read_bytes(), before)

    def test_gh_failure_does_not_leak_diagnostics(self):
        failure = subprocess.CompletedProcess(['gh'], 1, '', 'synthetic-token-must-not-print')
        with patch.object(resume.subprocess, 'run', return_value=failure):
            with self.assertRaises(ValueError) as caught:
                resume.run('.', ['gh', 'pr', 'list'])
        self.assertNotIn('synthetic-token', str(caught.exception))


def release():
    return dict(tag_name='v1.0', name='Synthetic release', draft=False, prerelease=False,
                html_url='https://github.com/fixture/product/releases/tag/v1.0',
                assets=[dict(name='app.apk', size=123, state='uploaded', digest='sha256:' + 'b' * 64,
                             browser_download_url='https://github.com/fixture/product/releases/download/v1.0/app.apk')])


class ReleaseSummaryTests(unittest.TestCase):
    def repository(self, root):
        def git(*args):
            return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                  check=True, text=True).stdout
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'Recovery fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/fixture/product.git')
        (root / 'README.md').write_text('Synthetic project')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        return git

    def test_native_attachment_is_a_candidate_not_verified_delivery(self):
        result = resume.summarize_release(release(), 'fixture/product')
        self.assertEqual(result['status'], 'metadata_observed')
        self.assertEqual(result['assets'][0]['name'], 'app.apk')
        self.assertEqual(result['assets'][0]['sha256_metadata'], 'b' * 64)
        self.assertEqual(result['download_verification'], 'not_verified')
        self.assertEqual(result['device_acceptance'], 'not_verified')

    def test_no_assets_does_not_turn_source_zip_into_installable_download(self):
        data = release()
        data.update(assets=[], zipball_url='https://api.github.com/source.zip')
        result = resume.summarize_release(data, 'fixture/product')
        self.assertEqual(result['status'], 'no_assets')
        self.assertEqual(result['assets'], [])
        self.assertNotIn('source.zip', json.dumps(result))

    def test_draft_and_prerelease_not_reported_as_formal_candidate(self):
        for flag in ('draft', 'prerelease'):
            data = release()
            data[flag] = True
            result = resume.summarize_release(data, 'fixture/product')
            self.assertEqual(result['status'], 'not_formal')
            self.assertEqual(result['assets'], [])
            self.assertTrue(result[flag])
            self.assertFalse(result['prerelease' if flag == 'draft' else 'draft'])

    def test_identity_and_sensitive_download_urls_rejected(self):
        for value in ('https://github.com/other/product/releases/download/v1.0/app.apk',
                      'https://token@github.com/fixture/product/releases/download/v1.0/app.apk',
                      'https://github.com/fixture/product/releases/download/v1.0/app.apk?token=synthetic',
                      'https://github.com/fixture/product/releases/download/v1.0/../../app.apk'):
            data = release()
            data['assets'][0]['browser_download_url'] = value
            with self.assertRaises(ValueError):
                resume.summarize_release(data, 'fixture/product')

    def test_body_author_and_other_raw_metadata_are_not_returned(self):
        data = release()
        data.update(body='synthetic-secret-not-for-output', author={'email': 'private@example.invalid'})
        result = json.dumps(resume.summarize_release(data, 'fixture/product'))
        self.assertNotIn('synthetic-secret', result)
        self.assertNotIn('private@example.invalid', result)

    def test_backslash_tag_cannot_escape_repository_in_a_browser(self):
        data = release()
        data['tag_name'] = r'v1\..\..\..\..\..\other\product\releases\tag\v1'
        data['html_url'] = 'https://github.com/fixture/product/releases/tag/' + data['tag_name']
        data['assets'] = []
        with self.assertRaises(ValueError):
            resume.summarize_release(data, 'fixture/product')

    def test_invalid_sizes_and_digests_are_not_accepted(self):
        for field, value in (('size', True), ('size', -1), ('digest', 'sha256:broken')):
            data = release()
            data['assets'][0][field] = value
            with self.assertRaises(ValueError):
                resume.summarize_release(data, 'fixture/product')

    def test_pending_attachment_is_not_a_download_candidate(self):
        data = release()
        data['assets'][0]['state'] = 'new'
        result = resume.summarize_release(data, 'fixture/product')
        self.assertEqual(result['status'], 'assets_pending')
        self.assertEqual(result['pending_assets'], 1)
        self.assertEqual(result['assets'], [])

    def test_missing_metadata_digest_is_not_invented(self):
        data = release()
        del data['assets'][0]['digest']
        result = resume.summarize_release(data, 'fixture/product')
        self.assertIsNone(result['assets'][0]['sha256_metadata'])
        self.assertEqual(result['download_verification'], 'not_verified')

    def test_live_release_read_and_partial_failure_preserve_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = self.repository(root)
            before = (git('rev-parse', 'HEAD'), (root / '.git/index').read_bytes())
            real_run = resume.run
            for failed in (False, True):
                def query(target, argv):
                    if argv[:2] == ['gh', 'pr']:
                        return json.dumps(snapshot()['pull_requests'])
                    if argv[:2] == ['gh', 'api']:
                        if failed:
                            raise ValueError('query failed')
                        self.assertEqual(argv[2], 'repos/fixture/product/releases/latest')
                        return json.dumps(release())
                    return real_run(target, argv)
                with patch.object(resume, 'run', side_effect=query):
                    result = resume.inspect(root, github=True)
                self.assertEqual(result['github']['status'], 'observed')
                self.assertEqual(result['delivery']['status'], 'unavailable' if failed else 'metadata_observed')
                self.assertEqual((git('rev-parse', 'HEAD'), (root / '.git/index').read_bytes()), before)

    def test_old_snapshot_compatible_and_stale_release_not_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repository(root)
            path = root / 'context.json'
            data = snapshot()
            path.write_text(json.dumps(data))
            self.assertEqual(resume.inspect(root, path)['delivery']['status'], 'not_read')
            data.update(observed_at='2000-01-01T00:00:00Z', latest_release=release())
            path.write_text(json.dumps(data))
            self.assertEqual(resume.inspect(root, path)['delivery']['status'], 'stale')


if __name__ == '__main__':
    unittest.main()
