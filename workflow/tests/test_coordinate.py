import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/coordinate.py'
spec = importlib.util.spec_from_file_location('coordinate', SCRIPT)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
HEAD = 'a' * 40


def claim(key='ui', paths=None, **kw):
    r = dict(action='claim', task=key, owner='session-' + key, workspace='vm-' + key,
             batch='experience-01', branch='codex/' + key, base=HEAD,
             scopes=paths or ['ui'])
    r.update(kw)
    return r


def change(state, action, key='ui', now=1000, **kw):
    t = state['tasks'][key]
    request = dict(action=action, task=key, owner=t['owner'], generation=t['generation'])
    request.update(kw)
    return c.transition(state, request, now)['state']


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.state = c.transition(c.empty('owner/project'), claim(), 1000)['state']

    def test_disjoint_work_and_overlapping_file_directory(self):
        s = c.transition(self.state, claim('finance', ['model.py']), 1001)['state']
        self.assertEqual(len(s['tasks']), 2)
        for path in ['ui', 'ui/menu.py', '.']:
            with self.subTest(path=path), self.assertRaises(c.Conflict):
                c.transition(s, claim('overlap', [path]), 1002)
        with self.assertRaises(c.Conflict):
            c.transition(s, claim('same-branch', ['other'], branch='codex/ui'), 1002)
        with self.assertRaises(c.Conflict):
            c.transition(s, claim('same-workspace', ['other'], workspace='vm-ui'), 1002)

    def test_paths_are_contained_not_just_intersecting(self):
        s = c.transition(c.empty('o/r'), claim(paths=['src/one.py']), 1000)['state']
        for path in ['src', 'src/two.py', '../outside', '/absolute', 'src/../x', 'a\\b', 'a\nb']:
            with self.subTest(path=path), self.assertRaises(c.Conflict):
                change(s, 'check', paths=[path])
        change(s, 'check', paths=['src/one.py'])
        with self.assertRaises(c.Conflict):
            c.transition(self.state, claim('unsafe', ['x'], branch='codex/../oops'), 1000)

    def test_expiry_does_not_release_and_recovery_fences_old_session(self):
        with self.assertRaises(c.Conflict):
            c.transition(self.state, claim('other'), 9000)
        with self.assertRaises(c.Conflict):
            change(self.state, 'renew', now=9000)
        request = dict(action='recover', task='ui', generation=1, new_owner='new-session',
                       new_workspace='new-vm', new_branch='codex/ui-recovery', head=HEAD)
        with self.assertRaises(c.Conflict):
            c.transition(self.state, request, 9000)
        request.update(confirmed_stopped=True, reason='Writer stopped; remote branch preserved.')
        s = c.transition(self.state, request, 9000)['state']
        self.assertEqual(s['tasks']['ui']['history'][0]['branch'], 'codex/ui')
        with self.assertRaises(c.Conflict):
            c.transition(s, dict(action='resume', task='ui', owner='session-ui',
                                 generation=1, head=HEAD), 9001)
        s = change(s, 'resume', now=9001, head=HEAD)
        self.assertEqual(s['tasks']['ui']['generation'], 2)

    def test_pause_handoff_expand_and_resume(self):
        s = change(self.state, 'pause', head=HEAD, handoff='PR saved')
        with self.assertRaises(c.Conflict):
            c.transition(s, claim('other'), 2000)
        with self.assertRaises(c.Conflict):
            change(s, 'handoff', new_owner='new', new_workspace='new-vm',
                   new_branch='codex/ui-next', head=HEAD, handoff='PR')
        s = change(s, 'handoff', new_owner='new', new_workspace='new-vm',
                   new_branch='codex/ui-next', head=HEAD, handoff='PR', confirmed_stopped=True)
        s = change(s, 'resume', head=HEAD)
        s = change(s, 'expand', scopes=['icons'])
        change(s, 'check', paths=['icons/a.svg'])
        with self.assertRaises(c.Conflict):
            change(s, 'ready', head=HEAD, paths=['other.js'], handoff='PR')

    def test_integration_seals_batch_and_binds_exact_source(self):
        with self.assertRaises(c.Conflict):
            c.transition(self.state, claim('merge', [], role='integrator', scopes=[]), 1000)
        s = change(self.state, 'ready', head=HEAD, paths=['ui/menu.py'], handoff='PR')
        s = c.transition(s, claim('merge', role='integrator', scopes=[]), 1000)['state']
        change(s, 'check', key='merge', paths=['ui/menu.py'])
        with self.assertRaises(c.Conflict):
            change(s, 'check', key='merge', paths=['other-source.py'])
        with self.assertRaises(c.Conflict):
            c.transition(s, claim('late', ['late']), 1001)
        with self.assertRaises(c.Conflict):
            change(s, 'resume', head=HEAD)
        with self.assertRaises(c.Conflict):
            c.transition(s, claim('other-merge', role='integrator', scopes=[], batch='batch-02'), 1001)
        b = 'b' * 40
        with self.assertRaises(c.Conflict):
            change(s, 'verify', key='merge', head=b, members={'ui': HEAD},
                   checks=[dict(name='tests', status='passed', source_sha=HEAD)])
        s = change(s, 'verify', key='merge', head=b, members={'ui': HEAD},
                   checks=[dict(name='tests', status='passed', source_sha=b)])
        change(s, 'check', key='merge', publish=True, head=b)
        with self.assertRaises(c.Conflict):
            change(s, 'check', key='merge', publish=True, head=HEAD)
        s = change(s, 'complete', key='merge', head=b)
        self.assertEqual(s['tasks']['ui']['status'], 'integrated')
        self.assertEqual(s['batches']['experience-01']['status'], 'completed')
        c.transition(s, claim('next-ui', ['ui'], batch='experience-02'), 1001)

    def test_aborted_integration_keeps_ready_work(self):
        s = change(self.state, 'ready', head=HEAD, paths=[], handoff='PR')
        s = c.transition(s, claim('merge', role='integrator', scopes=[]), 1000)['state']
        s = change(s, 'abandon', key='merge', confirmed_stopped=True, reason='Saved branch; stop batch.')
        self.assertEqual(s['tasks']['ui']['status'], 'ready')
        self.assertEqual(s['batches']['experience-01']['status'], 'open')

    def test_expired_integrator_can_transfer_without_reopening_batch(self):
        s = change(self.state, 'ready', head=HEAD, paths=[], handoff='PR')
        s = c.transition(s, claim('merge', role='integrator', scopes=[]), 1000)['state']
        request = dict(action='recover', task='merge', generation=1, new_owner='replacement',
                       new_workspace='replacement-vm', new_branch='codex/merge-recovery',
                       head=HEAD, confirmed_stopped=True, reason='Original integrator stopped; source saved.')
        s = c.transition(s, request, 9000)['state']
        self.assertEqual(s['batches']['experience-01']['status'], 'sealed')
        with self.assertRaises(c.Conflict):
            c.transition(s, dict(action='renew', task='merge', owner='session-merge', generation=1), 9001)
        s = change(s, 'resume', key='merge', now=9001, head=HEAD)
        with self.assertRaises(c.Conflict):
            change(s, 'check', key='merge', now=9001, publish=True, head=HEAD)
        s = change(s, 'verify', key='merge', now=9001, head=HEAD, members={'ui': HEAD},
                   checks=[dict(name='replacement validated source', status='passed', source_sha=HEAD)])
        change(s, 'complete', key='merge', now=9001, head=HEAD)

    def test_immutable_inputs_and_corrupt_registry_rejected(self):
        old = copy.deepcopy(self.state)
        change(self.state, 'renew')
        self.assertEqual(self.state, old)
        bad = copy.deepcopy(self.state)
        bad['tasks']['ui']['scopes'] = ['../unsafe']
        with self.assertRaises(c.Conflict):
            c.transition(bad, claim('other'), 1000)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.a, self.b = self.root / 'a', self.root / 'b'
        self.git(self.root, 'init', '--bare', '-q', str(self.remote))
        self.git(self.root, 'init', '-q', '-b', 'main', str(self.a))
        self.git(self.a, 'config', 'user.name', 'Fixture')
        self.git(self.a, 'config', 'user.email', 'fixture@example.invalid')
        (self.a / 'README.md').write_text('source stays here\n')
        self.git(self.a, 'add', '.')
        self.git(self.a, 'commit', '-qm', 'source fixture')
        self.base = self.git(self.a, 'rev-parse', 'HEAD')
        self.git(self.a, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.a, 'push', '-q', 'origin', 'main')
        self.git(self.root, 'clone', '-q', '-b', 'main', str(self.remote), str(self.b))
        for p in [self.a, self.b]:
            self.git(p, 'config', 'user.name', 'Fixture')
            self.git(p, 'config', 'user.email', 'fixture@example.invalid')
        self.sa, self.sb = c.GitStore(self.a), c.GitStore(self.b)
        self.sa.initialize('fixture/project')

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_registry_init_cannot_reset_and_preserves_dirty_source_index(self):
        before = self.git(self.a, 'rev-parse', 'HEAD')
        (self.a / 'staged.txt').write_text('keep staged')
        self.git(self.a, 'add', 'staged.txt')
        (self.a / 'README.md').write_text('keep unstaged')
        status = self.git(self.a, 'status', '--porcelain')
        req = claim(base=self.base)
        self.sa.apply(req)
        self.assertEqual(self.git(self.a, 'status', '--porcelain'), status)
        self.assertEqual(self.git(self.a, 'rev-parse', 'HEAD'), before)
        with self.assertRaises(c.Conflict):
            self.sa.initialize('fixture/project')
        self.assertEqual(self.sa.snapshot()['state']['revision'], 1)
        self.assertEqual(self.sa.run('show', self.sa.remote_head() + ':README.md').stdout,
                         'source stays here\n')

    def test_two_real_git_writers_one_atomic_winner_then_safe_retry(self):
        barrier = threading.Barrier(2)
        def writer(store, request):
            snap = store.snapshot()
            proposal = c.plan(snap, request)
            barrier.wait(timeout=10)
            try:
                store.commit(proposal)
                return 'won', request
            except c.Conflict:
                return 'lost', request
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer, self.sa, claim('one', ['one'], base=self.base)),
                       pool.submit(writer, self.sb, claim('two', ['two'], base=self.base))]
            results = [f.result(timeout=20) for f in futures]
        self.assertEqual(sorted(x[0] for x in results), ['lost', 'won'])
        self.assertEqual(self.sa.snapshot()['state']['revision'], 1)
        loser = next(req for result, req in results if result == 'lost')
        self.sa.apply(loser)
        self.assertEqual(len(self.sa.snapshot()['state']['tasks']), 2)
        self.assertEqual(self.git(self.a, 'rev-parse', 'HEAD'), self.base)

    def test_same_task_race_and_stale_snapshot_cannot_overwrite(self):
        snap = self.sa.snapshot()
        p1 = c.plan(snap, claim(base=self.base))
        p2 = c.plan(snap, claim(base=self.base, owner='other-session', workspace='other-vm'))
        self.sa.commit(p1)
        with self.assertRaises(c.Conflict):
            self.sb.commit(p2)
        with self.assertRaises(c.Conflict):
            c.plan(self.sb.snapshot(), claim(base=self.base))
        self.assertEqual(self.sa.snapshot()['state']['tasks']['ui']['owner'], 'session-ui')

    def test_two_work_branches_integrate_and_require_new_checks(self):
        heads = {}
        for store, root, name in [(self.sa, self.a, 'one'), (self.sb, self.b, 'two')]:
            self.git(root, 'checkout', '-qb', 'codex/' + name)
            (root / (name + '.txt')).write_text(name)
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-qm', name)
            head = self.git(root, 'rev-parse', 'HEAD')
            self.git(root, 'push', '-q', 'origin', 'HEAD')
            heads[name] = head
            store.apply(claim(name, [name + '.txt'], base=self.base))
            store.apply(dict(action='ready', task=name, owner='session-' + name, generation=1,
                             head=head, handoff='PR-' + name))
        self.git(self.a, 'checkout', '-qb', 'codex/merge', 'main')
        self.git(self.a, 'fetch', '-q', 'origin', 'codex/one', 'codex/two')
        self.git(self.a, 'merge', '--no-edit', '--no-ff', 'origin/codex/one', 'origin/codex/two')
        integrated = self.git(self.a, 'rev-parse', 'HEAD')
        self.git(self.a, 'push', '-q', 'origin', 'HEAD')
        self.sa.apply(claim('merge', role='integrator', scopes=[], base=self.base))
        self.assertEqual((self.a / 'one.txt').read_text(), 'one')
        self.assertEqual((self.a / 'two.txt').read_text(), 'two')
        req = dict(action='verify', task='merge', owner='session-merge', generation=1,
                   head=integrated, members=heads,
                   checks=[dict(name='both saved results', status='passed', source_sha=integrated)])
        self.sa.apply(req)
        self.sa.apply(dict(action='check', task='merge', owner='session-merge', generation=1,
                           head=integrated, publish=True))
        # A source push after validation invalidates the publication gate.
        (self.a / 'extra.txt').write_text('changed after tests')
        self.git(self.a, 'add', '.')
        self.git(self.a, 'commit', '-qm', 'changed source')
        self.git(self.a, 'push', '-q', 'origin', 'HEAD')
        with self.assertRaises(c.Conflict):
            self.sa.apply(dict(action='check', task='merge', owner='session-merge', generation=1,
                               head=integrated, publish=True))
        changed = self.git(self.a, 'rev-parse', 'HEAD')
        self.sa.apply(dict(action='expand', task='merge', owner='session-merge', generation=1,
                           scopes=['extra.txt']))
        self.sa.apply(dict(action='renew', task='merge', owner='session-merge', generation=1,
                           head=changed))
        with self.assertRaises(c.Conflict):
            self.sa.apply(dict(action='check', task='merge', owner='session-merge', generation=1,
                               head=changed, publish=True))
        req.update(head=changed, checks=[dict(name='fresh complete source check', status='passed',
                                             source_sha=changed)])
        self.sa.apply(req)
        self.sa.apply(dict(action='complete', task='merge', owner='session-merge', generation=1,
                           head=changed))
        final = self.sa.snapshot()['state']
        self.assertEqual(final['tasks']['one']['status'], 'integrated')
        self.assertEqual(final['tasks']['two']['status'], 'integrated')
        self.assertNotIn('publication', final['tasks']['merge'])

    def test_actual_changed_files_cannot_hide_scope_violation(self):
        self.git(self.a, 'checkout', '-qb', 'codex/ui')
        (self.a / 'outside.txt').write_text('outside')
        self.git(self.a, 'add', '.')
        self.git(self.a, 'commit', '-qm', 'outside')
        head = self.git(self.a, 'rev-parse', 'HEAD')
        self.git(self.a, 'push', '-q', 'origin', 'HEAD')
        self.sa.apply(claim(base=self.base))
        with self.assertRaises(c.Conflict):
            self.sa.apply(dict(action='ready', task='ui', owner='session-ui', generation=1,
                               head=head, paths=['ui/fake'], handoff='PR'))
        self.assertEqual(self.sa.snapshot()['state']['tasks']['ui']['status'], 'active')

    def test_rename_cannot_hide_deletion_of_another_tasks_file(self):
        self.git(self.a, 'checkout', '-qb', 'codex/ui')
        (self.a / 'ui').mkdir()
        self.git(self.a, 'mv', 'README.md', 'ui/copied.md')
        self.git(self.a, 'commit', '-qm', 'rename outside file into scope')
        head = self.git(self.a, 'rev-parse', 'HEAD')
        self.git(self.a, 'push', '-q', 'origin', 'HEAD')
        self.sa.apply(claim(base=self.base))
        with self.assertRaises(c.Conflict):
            self.sa.apply(dict(action='ready', task='ui', owner='session-ui', generation=1,
                               head=head, paths=['ui/copied.md'], handoff='PR'))


if __name__ == '__main__':
    unittest.main()
