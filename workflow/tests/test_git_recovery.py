import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import git_recovery as recovery


class FakeGitHub:
    def __init__(self, fixture):
        self.fixture = fixture
        self.refs = {'main': fixture.base}
        self.mutations = 0
        self.calls = []
        self.private = False
        self.bad_tree = False
        self.merge = None

    def __call__(self, path, body=None, missing=False, method=None):
        self.calls.append((path, body is not None))
        if body is not None:
            self.mutations += 1
        if not path:
            return dict(full_name='fixture/source', private=self.private, default_branch='main')
        if path.startswith('/git/ref/heads/'):
            sha = self.refs.get(path.removeprefix('/git/ref/heads/'))
            return {'object': {'sha': sha}} if sha else None
        if path == '/git/commits/' + self.fixture.base:
            return {'tree': {'sha': self.fixture.base_tree}}
        if self.merge and path == '/git/commits/' + self.merge['sha']:
            return self.merge
        if path == '/git/trees':
            with tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'))
                self.fixture.git('read-tree', body['base_tree'], env=env)
                for entry in body['tree']:
                    if entry.get('sha', 'content') is None:
                        self.fixture.git('update-index', '--force-remove', '--', entry['path'], env=env)
                    else:
                        sha = self.fixture.git('hash-object', '-w', '--stdin', data=entry['content'].encode())
                        self.fixture.git('update-index', '--add', '--cacheinfo', entry['mode'], sha, entry['path'], env=env)
                tree = self.fixture.git('write-tree', env=env)
            return {'sha': 'f' * 40 if self.bad_tree else tree}
        if path == '/git/commits':
            env = dict(os.environ)
            for kind, values in (('AUTHOR', body['author']), ('COMMITTER', body['committer'])):
                for key in ('name', 'email', 'date'):
                    env['GIT_' + kind + '_' + key.upper()] = values[key]
            sha = self.fixture.git('commit-tree', body['tree'], '-p', body['parents'][0],
                                   data=body['message'].encode(), env=env)
            return {'sha': sha}
        if path == '/git/refs':
            branch = body['ref'].removeprefix('refs/heads/')
            if branch in self.refs:
                raise ValueError('Fixture branch conflict')
            self.refs[branch] = body['sha']
            return {'object': {'sha': body['sha']}}
        if path.startswith('/git/refs/heads/') and method == 'PATCH':
            branch=path.removeprefix('/git/refs/heads/')
            if body.get('force') is not False or self.refs.get(branch) != self.fixture.base:
                raise ValueError('Fixture non-fast-forward conflict')
            self.refs[branch]=body['sha']
            return {'object':{'sha':body['sha']}}
        raise AssertionError('Unexpected fixture endpoint: ' + path)


class GitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Recovery fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'merge.verifySignatures', 'false')
        (self.root / '.git/no-hooks').mkdir()
        self.git('config', 'core.hooksPath', str(self.root / '.git/no-hooks'))
        self.git('remote', 'add', 'origin', 'https://github.com/fixture/source.git')
        (self.root / '.gitignore').write_text('.env\n')
        (self.root / 'old.txt').write_text('old source\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.base_tree = self.git('rev-parse', 'HEAD^{tree}')
        self.git('switch', '-qc', 'agent/source')
        (self.root / 'old.txt').unlink()
        (self.root / 'source.py').write_text('print("synthetic source")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'source fixture')
        self.head = self.git('rev-parse', 'HEAD')
        (self.root / '.env').write_text('SYNTHETIC_IGNORED_VALUE')
        self.api = FakeGitHub(self)

    def git(self, *args, data=None, env=None):
        return subprocess.check_output(['git', '-C', str(self.root), *args], input=data,
                                       env=env, stderr=subprocess.PIPE).decode().strip()

    def invoke(self, operation='publish', apply=False, **changes):
        values = dict(target=self.root, expected_head=self.head, operation=operation,
                      apply=apply, remote_head=None, gh=None, network='inherit')
        values.update(changes)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(recovery.recover(SimpleNamespace(**values), api=self.api), 0)
        return json.loads(output.getvalue())

    def test_plan_is_local_and_never_prints_source_or_ignored_configuration(self):
        before = (self.root / '.git/index').read_bytes()
        report = self.invoke()
        self.assertEqual(report['status'], 'planned')
        self.assertEqual(report['changed_files'], 2)
        self.assertEqual(self.api.calls, [])
        self.assertNotIn('synthetic source', json.dumps(report))
        self.assertNotIn('SYNTHETIC_IGNORED_VALUE', json.dumps(report))
        self.assertEqual((self.root / '.git/index').read_bytes(), before)

    def test_publish_matches_actual_git_tree_and_commit_then_repeat_has_zero_mutations(self):
        before = (self.git('rev-parse', 'HEAD'), (self.root / '.git/index').read_bytes())
        report = self.invoke(apply=True)
        self.assertEqual(report['status'], 'published')
        self.assertEqual(report['mutation_requests'], 3)
        self.assertEqual(self.api.refs['agent/source'], self.head)
        self.assertEqual(before, (self.git('rev-parse', 'HEAD'), (self.root / '.git/index').read_bytes()))
        self.api.mutations = 0
        self.assertEqual(self.invoke(apply=True)['status'], 'already_present')
        self.assertEqual(self.api.mutations, 0)
        self.assertEqual((self.root / '.env').read_text(), 'SYNTHETIC_IGNORED_VALUE')

    def test_private_repo_conflicting_branch_and_moved_base_never_mutate(self):
        for change in ('private', 'branch', 'base'):
            with self.subTest(change=change):
                self.api = FakeGitHub(self)
                if change == 'private': self.api.private = True
                if change == 'branch': self.api.refs['agent/source'] = self.base
                if change == 'base': self.api.refs['main'] = self.head
                with self.assertRaises(ValueError): self.invoke(apply=True)
                self.assertEqual(self.api.mutations, 0)

    def test_existing_task_branch_advances_one_exact_commit_and_repeat_is_read_only(self):
        self.api.refs['agent/source']=self.base
        self.api.refs['main']='c'*40  # Unrelated main movement must not block an owned task branch.
        before=(self.git('rev-parse','HEAD'),(self.root/'.git/index').read_bytes())
        report=self.invoke(apply=True,expected_remote_head=self.base)
        self.assertEqual('published',report['status'])
        self.assertEqual(self.head,self.api.refs['agent/source'])
        self.assertEqual(3,self.api.mutations)
        self.assertEqual(before,(self.git('rev-parse','HEAD'),(self.root/'.git/index').read_bytes()))
        self.api.mutations=0
        self.assertEqual('already_present',self.invoke(apply=True,expected_remote_head=self.base)['status'])
        self.assertEqual(0,self.api.mutations)

    def test_existing_branch_requires_an_exact_present_parent_and_does_not_select_main(self):
        for state in ('missing','moved','not_parent'):
            with self.subTest(state=state):
                self.api=FakeGitHub(self)
                expected=self.base
                if state=='moved':self.api.refs['agent/source']='e'*40
                if state=='not_parent':
                    expected='e'*40;self.api.refs['agent/source']=expected
                with self.assertRaises(ValueError):self.invoke(apply=True,expected_remote_head=expected)
                self.assertEqual(0,self.api.mutations)

    def test_uncertain_branch_update_resumes_by_reading_the_saved_exact_head(self):
        self.api.refs['agent/source']=self.base
        original=self.api.__call__
        def interrupted(path,body=None,missing=False,method=None):
            value=original(path,body,missing,method)
            if method=='PATCH':raise ValueError('Fixture response lost after ref saved')
            return value
        with self.assertRaisesRegex(ValueError,'response lost'):
            recovery.publish(self.root,'fixture/source','agent/source',self.head,self.head,True,interrupted,expected_remote_head=self.base)
        self.assertEqual(self.head,self.api.refs['agent/source'])
        self.api.mutations=0
        self.assertEqual('already_present',self.invoke(apply=True,expected_remote_head=self.base)['status'])
        self.assertEqual(0,self.api.mutations)

    def test_concurrent_task_update_is_rejected_without_force(self):
        self.api.refs['agent/source']=self.base
        original=self.api.__call__
        def raced(path,body=None,missing=False,method=None):
            if method=='PATCH':self.api.refs['agent/source']='e'*40
            return original(path,body,missing,method)
        with self.assertRaisesRegex(ValueError,'non-fast-forward'):
            recovery.publish(self.root,'fixture/source','agent/source',self.head,self.head,True,raced,expected_remote_head=self.base)
        self.assertEqual('e'*40,self.api.refs['agent/source'])

    def test_default_branch_publish_and_dirty_worktree_are_not_recovery_routes(self):
        self.api.refs['main'] = self.base
        self.git('branch', '-m', 'main-task')
        with patch.object(recovery, 'remote_metadata', return_value='main-task'):
            with self.assertRaises(ValueError): self.invoke(apply=True)
        self.assertEqual(self.api.mutations, 0)
        (self.root / 'source.py').write_text('unfinished change')
        with self.assertRaises(ValueError): self.invoke(apply=True)
        self.assertEqual((self.root / 'source.py').read_text(), 'unfinished change')

    def test_changed_secret_filename_is_blocked_before_api_and_binary_is_unsupported(self):
        self.git('add', '-f', '.env')
        self.git('commit', '--amend', '--no-edit', '-q')
        self.head = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'privacy') as caught:
            self.invoke(apply=True)
        self.assertNotIn('SYNTHETIC_IGNORED_VALUE', str(caught.exception))
        self.assertEqual(self.api.calls, [])
        with self.assertRaisesRegex(ValueError, 'Binary'):
            (self.root / '.env').unlink()
            (self.root / 'source.py').write_bytes(b'binary\0fixture')
            self.git('add', '-u')
            self.git('commit', '--amend', '--no-edit', '-q')
            self.head = self.git('rev-parse', 'HEAD')
            self.invoke(apply=True)

    def test_remote_tree_mismatch_does_not_create_a_commit_or_ref(self):
        self.api.bad_tree = True
        with self.assertRaisesRegex(ValueError, 'tree differs'):
            self.invoke(apply=True)
        self.assertEqual(self.api.mutations, 1)
        self.assertNotIn('agent/source', self.api.refs)

    def test_expected_head_and_signed_source_are_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, 'expected commit'):
            self.invoke(apply=True, expected_head=self.base)
        raw = subprocess.check_output(['git', '-C', str(self.root), 'cat-file', 'commit', self.head])
        header, message = raw.split(b'\n\n', 1)
        signed = header + b'\ngpgsig SYNTHETIC_SIGNATURE\n\n' + message
        sha = self.git('hash-object', '-w', '-t', 'commit', '--stdin', data=signed)
        self.git('update-ref', 'HEAD', sha, self.head)
        self.head = sha
        with self.assertRaisesRegex(ValueError, 'signatures are never stripped'):
            self.invoke(apply=True)
        self.assertEqual(self.api.calls, [])

    def test_adopted_cli_plan_without_python_B_does_not_dirty_its_repository(self):
        package = Path(recovery.__file__).parent
        cloud = self.root / 'cloud'
        cloud.mkdir()
        for name in ('workflow.py', 'git_recovery.py', 'resume_status.py', 'privacy_scan.py'):
            (cloud / name).write_bytes((package / name).read_bytes())
        self.git('add', 'cloud')
        self.git('commit', '-qm', 'adopted fixture')
        head = self.git('rev-parse', 'HEAD')
        before = (self.root / '.git/index').read_bytes()
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        result = subprocess.run([sys.executable, str(cloud / 'workflow.py'), 'recover-git', '--operation', 'publish',
                                 '--target', str(self.root), '--expected-head', head,
                                 '--expected-remote-head', self.head], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(result.stdout)['status'], 'planned')
        self.assertEqual(json.loads(result.stdout)['expected_remote_head'], self.head)
        self.assertFalse((cloud / '__pycache__').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual((self.root / '.git/index').read_bytes(), before)

    def merge_metadata(self):
        tree = self.git('rev-parse', self.head + '^{tree}')
        header = f'tree {tree}\nparent {self.base}\nparent {self.head}\nauthor Recovery fixture <fixture@example.invalid> 1700000000 +0000\ncommitter Recovery fixture <fixture@example.invalid> 1700000000 +0000'
        signature = '-----BEGIN PGP SIGNATURE-----\nSYNTHETIC_SIGNATURE\n-----END PGP SIGNATURE-----\n'
        raw = (header + '\ngpgsig -----BEGIN PGP SIGNATURE-----\n SYNTHETIC_SIGNATURE\n -----END PGP SIGNATURE-----\n\nMerge fixture\n').encode()
        sha = self.git('hash-object', '-t', 'commit', '--stdin', data=raw)
        self.api.merge = dict(sha=sha, tree={'sha': tree}, parents=[{'sha': self.base}, {'sha': self.head}],
                              verification=dict(verified=True, payload=header + '\n\nMerge fixture\n', signature=signature))
        self.api.refs['main'] = sha
        self.git('switch', '-q', 'main')
        return sha

    def test_signed_merge_recovery_fast_forwards_without_reset_and_is_repeatable(self):
        target = self.merge_metadata()
        plan = self.invoke('sync', remote_head=target, expected_head=self.base)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        report = self.invoke('sync', apply=True, remote_head=target, expected_head=self.base)
        self.assertEqual(report['status'], 'synced')
        self.assertEqual(self.git('rev-parse', 'HEAD'), target)
        self.assertEqual(self.git('rev-parse', 'origin/main'), target)
        self.assertEqual(self.api.mutations, 0)
        self.assertFalse((self.root / 'old.txt').exists())
        self.assertEqual((self.root / '.env').read_text(), 'SYNTHETIC_IGNORED_VALUE')
        self.assertEqual(self.invoke('sync', apply=True, remote_head=target, expected_head=self.base)['status'], 'already_current')

    def test_corrupt_signature_payload_cannot_change_the_checkout(self):
        target = self.merge_metadata()
        self.api.merge['verification']['signature'] += 'corrupt'
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.invoke('sync', apply=True, remote_head=target, expected_head=self.base)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)


class GitHubBoundaryTests(unittest.TestCase):
    def test_auth_or_policy_failure_is_not_replayed_or_printed(self):
        failure = subprocess.CompletedProcess([], 1, b'', b'SYNTHETIC_PRIVATE_DIAGNOSTIC (HTTP 403)')
        api = recovery.GitHub('fixture/source', executable='gh')
        with patch.object(recovery.subprocess, 'run', return_value=failure) as run:
            with self.assertRaisesRegex(ValueError, '403') as caught:
                api('/git/refs', {'ref': 'refs/heads/agent/source', 'sha': 'a' * 40})
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', str(caught.exception))
        self.assertNotIn('auth', run.call_args.args[0])

    def test_direct_network_choice_changes_only_the_child_environment(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://example.invalid', 'NO_PROXY': 'localhost'}):
            before = dict(os.environ)
            api = recovery.GitHub('fixture/source', executable='gh', network='direct')
            self.assertNotIn('HTTPS_PROXY', api.env)
            self.assertIn('localhost', api.env['NO_PROXY'])
            self.assertEqual(dict(os.environ), before)


if __name__ == '__main__':
    unittest.main()
