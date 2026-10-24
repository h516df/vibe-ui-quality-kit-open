#!/usr/bin/env python3
"""Cooperative Codex task registry. Stdlib; isolated Git ref with atomic leases."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

REF = 'refs/heads/codex-coordination-v1'
STATE_PATH = '.codex-coordination/state.json'
RESERVED = {'active', 'paused', 'ready'}
TERMINAL = {'integrated', 'abandoned'}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')


class Conflict(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Conflict(message)


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), 'Invalid task/session/batch identifier.')
    return value


def sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), 'Expected full Git SHA.')
    return value


def scope(value):
    require(isinstance(value, str) and value and len(value) < 512, 'Invalid scope.')
    if value == '.':
        return value
    value = value.rstrip('/')
    require(not value.startswith(('/', '@')) and
            not any(c in value for c in '\\\n\r\0*?[]:') and
            all(p not in ('', '.', '..', '.git') for p in value.split('/')), 'Unsafe scope.')
    return value


def branch(value):
    require(isinstance(value, str) and value.startswith('codex/') and
            len(value) < 200 and not any(c in value for c in ' ~^:?*[\\\n\r\0') and
            '..' not in value and '@{' not in value and
            all(p and not p.startswith('.') and not p.endswith(('.', '.lock'))
                for p in value.split('/')), 'Use a safe codex/ task branch.')
    return value


def overlap(a, b):
    if a.startswith('@') or b.startswith('@'):
        return a == b
    return a == '.' or b == '.' or a == b or a.startswith(b + '/') or b.startswith(a + '/')


def covers(declared, changed):
    return declared == '.' or declared == changed or changed.startswith(declared + '/')


def ready_members(state, task):
    return {key: t['head'] for key, t in state['tasks'].items()
            if t['batch'] == task['batch'] and t['role'] == 'work' and t['status'] == 'ready'}


def source_scopes(state, task):
    declared = [s for s in task['scopes'] if not s.startswith('@')]
    if task['role'] == 'integrator':
        declared += [s for t in state['tasks'].values()
                     if t['batch'] == task['batch'] and t['role'] == 'work' and t['status'] == 'ready'
                     for s in t['scopes'] if not s.startswith('@')]
    return declared


def empty(project):
    require(isinstance(project, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', project),
            'Expected owner/repository.')
    return {'version': 1, 'project': project, 'revision': 0, 'tasks': {}, 'batches': {}}


def validate(state):
    require(isinstance(state, dict) and state.get('version') == 1 and
            type(state.get('revision')) is int and state['revision'] >= 0 and
            isinstance(state.get('tasks'), dict) and isinstance(state.get('batches'), dict),
            'Invalid registry; preserve it and investigate.')
    empty(state.get('project'))
    for key, batch in state['batches'].items():
        identifier(key)
        require(isinstance(batch, dict) and batch.get('status') in {'open', 'sealed', 'completed'},
                'Invalid batch.')
    for key, task in state['tasks'].items():
        identifier(key)
        require(isinstance(task, dict), 'Invalid task.')
        identifier(task.get('owner'))
        identifier(task.get('workspace'))
        identifier(task.get('batch'))
        require(task['batch'] in state['batches'], 'Task batch missing.')
        branch(task.get('branch'))
        require(task.get('status') in RESERVED | TERMINAL and
                task.get('role') in {'work', 'integrator'} and
                type(task.get('generation')) is int and task['generation'] >= 1 and
                isinstance(task.get('scopes'), list) and task['scopes'], 'Invalid task fields.')
        sha(task.get('head'))
        sha(task.get('base'))
        require(type(task.get('lease_until')) is int, 'Invalid lease.')
        for s in task['scopes']:
            require(s in {'@integration', '@publish'} or scope(s) == s, 'Invalid saved scope.')
    return state


def ttl(request):
    value = request.get('ttl', 3600)
    require(type(value) is int and 60 <= value <= 7200, 'Lease must be 60..7200 seconds.')
    return value


def owned(state, request, now, live=False):
    key = identifier(request.get('task'))
    require(key in state['tasks'], 'Task not found.')
    task = state['tasks'][key]
    require(request.get('owner') == task['owner'] and
            request.get('generation') == task['generation'], 'Ownership changed; stop this writer.')
    require(task['status'] in RESERVED, 'Task already finished.')
    if live:
        require(task['status'] == 'active' and task['lease_until'] > now,
                'No live lease; pause writes and re-read the registry.')
    return task


def free(state, candidate, ignore=None):
    for key, other in state['tasks'].items():
        if key == ignore or other['status'] not in RESERVED:
            continue
        require(candidate['branch'] != other['branch'], 'Branch reserved by ' + key)
        require(candidate['workspace'] != other['workspace'], 'Workspace reserved by ' + key)
        require(not any(overlap(a, b) for a in candidate['scopes'] for b in other['scopes']),
                'Scope reserved by ' + key + ' (expired/paused work is retained).')


def receipt(state, task, request):
    head = sha(request.get('head'))
    checks = request.get('checks')
    require(isinstance(checks, list) and checks and all(
        isinstance(c, dict) and isinstance(c.get('name'), str) and c['name'] and
        c.get('status') == 'passed' and c.get('source_sha') == head for c in checks),
        'Checks must pass on this exact integrated source.')
    members = ready_members(state, task)
    require(request.get('members') == members, 'Ready task heads changed; re-integrate and verify.')
    require(members, 'No ready member tasks.')
    return {'source_sha': head, 'members': members, 'checks': copy.deepcopy(checks)}


def transition(original, request, now=None):
    validate(original)
    require(isinstance(request, dict), 'Expected request object.')
    now = int(time.time()) if now is None else now
    require(type(now) is int, 'Invalid clock.')
    state = copy.deepcopy(original)
    action = request.get('action')
    if action == 'claim':
        key = identifier(request.get('task'))
        require(key not in state['tasks'], 'Task already registered; inspect or explicitly hand off.')
        owner = identifier(request.get('owner'))
        workspace = identifier(request.get('workspace'))
        batch = identifier(request.get('batch'))
        role = request.get('role', 'work')
        require(role in {'work', 'integrator'}, 'Invalid role.')
        require(batch not in state['batches'] or state['batches'][batch].get('status') == 'open',
                'Batch sealed; new work belongs to a later batch.')
        scopes = request.get('scopes', [])
        require(isinstance(scopes, list), 'Expected scopes array.')
        scopes = sorted(set(scope(s) for s in scopes))
        if role == 'integrator':
            require(not any(t['batch'] == batch and t['role'] == 'work' and
                            t['status'] in {'active', 'paused'} for t in state['tasks'].values()),
                    'Member work is not ready.')
            scopes += ['@integration', '@publish']
        require(scopes, 'Declare source scope.')
        task = {'owner': owner, 'workspace': workspace, 'batch': batch, 'role': role,
                'branch': branch(request.get('branch')), 'scopes': scopes,
                'base': sha(request.get('base')), 'head': sha(request.get('base')),
                'status': 'active', 'generation': 1, 'lease_until': now + ttl(request),
                'updated_at': now, 'history': []}
        free(state, task)
        state['tasks'][key] = task
        state['batches'].setdefault(batch, {'status': 'open'})
        if role == 'integrator':
            state['batches'][batch] = {'status': 'sealed', 'integrator': key}
    elif action == 'check':
        task = owned(state, request, now, live=True)
        paths = request.get('paths', [])
        require(isinstance(paths, list), 'Expected paths array.')
        require(all(any(covers(s, scope(p)) for s in source_scopes(state, task))
                    for p in paths), 'Changed paths exceed the reserved scope.')
        if request.get('publish'):
            require(task['role'] == 'integrator' and task.get('receipt') and
                    task['receipt']['source_sha'] == request.get('head') and
                    task['receipt']['members'] == ready_members(state, task),
                    'Only the verified integrator may publish this source.')
        return {'changed': False, 'state': state, 'task': copy.deepcopy(task)}
    elif action == 'recover':
        key = identifier(request.get('task'))
        require(key in state['tasks'], 'Task not found.')
        task = state['tasks'][key]
        require(task['status'] == 'paused' or
                task['status'] == 'active' and task['lease_until'] <= now,
                'Recovery requires paused or expired work; a live owner cannot be displaced.')
        require(request.get('confirmed_stopped') is True and request.get('reason'),
                'Expiry is not consent: verify the writer stopped and preserve its branch.')
        require(request.get('generation') == task['generation'], 'Ownership changed.')
        old = copy.deepcopy(task)
        task.update(owner=identifier(request.get('new_owner')),
                    workspace=identifier(request.get('new_workspace')),
                    branch=branch(request.get('new_branch')), head=sha(request.get('head')),
                    status='paused', generation=task['generation'] + 1, lease_until=0)
        require(task['branch'] != old['branch'] and task['workspace'] != old['workspace'] and
                task['owner'] != old['owner'], 'Recovery must isolate the old writer.')
        free(state, task, ignore=key)
        task['history'].append({'owner': old['owner'], 'branch': old['branch'],
                                'head': request['head'], 'reason': str(request['reason'])[:1000]})
        task.pop('receipt', None)
    else:
        live = action in {'renew', 'pause', 'ready', 'verify', 'complete', 'expand'}
        task = owned(state, request, now, live=live)
        key = request['task']
        if action == 'renew':
            task['lease_until'] = now + ttl(request)
            if request.get('head'):
                task['head'] = sha(request['head'])
                if task.get('receipt', {}).get('source_sha') != task['head']:
                    task.pop('receipt', None)
        elif action == 'expand':
            extra = request.get('scopes')
            require(isinstance(extra, list) and extra, 'Declare additional scope.')
            task['scopes'] = sorted(set(task['scopes'] + [scope(s) for s in extra]))
            free(state, task, ignore=key)
        elif action == 'pause':
            require(request.get('handoff'), 'Save handoff before pausing.')
            task.update(status='paused', lease_until=0, head=sha(request.get('head')),
                        handoff=str(request['handoff'])[:5000])
        elif action == 'resume':
            require(task['status'] in {'paused', 'ready'} or
                    task['status'] == 'active' and task['lease_until'] <= now, 'Already active.')
            require(state['batches'][task['batch']]['status'] == 'open' or
                    task['role'] == 'integrator', 'Batch sealed; do not change member heads.')
            task.update(status='active', lease_until=now + ttl(request),
                        head=sha(request.get('head')))
            task.pop('receipt', None)
        elif action == 'handoff':
            require(request.get('confirmed_stopped') is True and
                    request.get('handoff'), 'Explicit stopped-writer handoff required.')
            require(task['role'] == 'integrator' or
                    state['batches'][task['batch']]['status'] == 'open', 'Batch sealed.')
            old = copy.deepcopy(task)
            task.update(owner=identifier(request.get('new_owner')),
                        workspace=identifier(request.get('new_workspace')),
                        branch=branch(request.get('new_branch')), head=sha(request.get('head')),
                        generation=task['generation'] + 1, status='paused', lease_until=0,
                        handoff=str(request['handoff'])[:5000])
            require(task['owner'] != old['owner'] and task['workspace'] != old['workspace'],
                    'New session and workspace required.')
            free(state, task, ignore=key)
            task['history'].append({'owner': old['owner'], 'branch': old['branch'], 'head': request['head']})
            task.pop('receipt', None)
        elif action == 'ready':
            require(task['role'] == 'work' and request.get('handoff'), 'Worker handoff required.')
            paths = request.get('paths')
            require(isinstance(paths, list), 'Provide actual changed paths.')
            require(all(any(covers(s, scope(p)) for s in task['scopes'] if not s.startswith('@'))
                        for p in paths), 'Changed paths exceed reserved scope.')
            task.update(status='ready', lease_until=0, head=sha(request.get('head')),
                        handoff=str(request['handoff'])[:5000])
        elif action == 'verify':
            require(task['role'] == 'integrator', 'Integrator required.')
            task['receipt'] = receipt(state, task, request)
            task['head'] = task['receipt']['source_sha']
        elif action == 'complete':
            require(task['role'] == 'integrator' and task.get('receipt') and
                    request.get('head') == task['receipt']['source_sha'], 'Verified source required.')
            expected = ready_members(state, task)
            require(expected == task['receipt']['members'], 'Member heads changed.')
            publication = request.get('publication')
            if publication is not None:
                require(isinstance(publication, dict) and
                        publication.get('source_sha') == task['head'] and publication.get('url'),
                        'Publication receipt belongs to another source.')
                task['publication'] = copy.deepcopy(publication)
            for member in expected:
                state['tasks'][member].update(status='integrated', updated_at=now)
            task.update(status='integrated', lease_until=0)
            state['batches'][task['batch']]['status'] = 'completed'
        elif action == 'abandon':
            require(request.get('confirmed_stopped') is True and request.get('reason'),
                    'Explicit stop and retained-work reason required.')
            require(task['role'] == 'integrator' or
                    state['batches'][task['batch']]['status'] == 'open', 'Batch sealed.')
            if task['role'] == 'integrator':
                state['batches'][task['batch']] = {'status': 'open'}
            task.update(status='abandoned', lease_until=0, reason=str(request['reason'])[:1000])
        else:
            raise Conflict('Unknown action.')
        task['updated_at'] = now
    state['revision'] += 1
    validate(state)
    return {'changed': True, 'state': state}


def plan(snapshot, request, now=None):
    require(isinstance(snapshot, dict), 'Expected snapshot.')
    parent = sha(snapshot.get('head'))
    result = transition(snapshot.get('state'), request, now)
    result.update(expected_head=parent, parent_sha=parent, ref=REF,
                  path=STATE_PATH, message='coordination: ' + str(request.get('action')) + ' [skip ci]')
    return result


class GitStore:
    """Existing Git authentication only. Never checks out or modifies the source index."""
    def __init__(self, root, remote='origin'):
        self.root = str(Path(root).resolve())
        require(re.fullmatch(r'[A-Za-z0-9_.-]+', remote) and not remote.startswith('-'),
                'Use an existing remote name.')
        self.remote = remote
        self.run('rev-parse', '--git-dir')

    def run(self, *args, data=None, env=None, check=True):
        environ = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        if env:
            environ.update(env)
        r = subprocess.run(['git', '-C', self.root, *args], input=data,
                           capture_output=True, text=True, env=environ)
        if check and r.returncode:
            raise Conflict('Git ' + args[0] + ' failed; check connection/permissions without exposing credentials.')
        return r

    def remote_head(self, ref=REF):
        rows = self.run('ls-remote', '--refs', self.remote, ref).stdout.splitlines()
        require(len(rows) <= 1, 'Ambiguous remote ref.')
        return sha(rows[0].split()[0]) if rows else None

    def fetch(self, head):
        self.run('fetch', '--no-tags', '--no-write-fetch-head', self.remote, sha(head))

    def snapshot(self):
        head = self.remote_head()
        require(head, 'Registry absent; initialize explicitly before claiming.')
        self.fetch(head)
        state = json.loads(self.run('show', head + ':' + STATE_PATH).stdout)
        return {'head': head, 'state': validate(state)}

    def commit(self, proposal):
        """Exact-head compare-and-swap, restricted to the coordination ref."""
        parent = sha(proposal['parent_sha'])
        expected = proposal.get('expected_head')
        if expected is not None:
            require(parent == sha(expected), 'Proposal parent must equal expected coordination head.')
        require(proposal.get('ref') == REF, 'Ref writes restricted to coordination.')
        validate(proposal['state'])
        blob = self.run('hash-object', '-w', '--stdin',
                        data=json.dumps(proposal['state'], ensure_ascii=False, indent=2) + '\n').stdout.strip()
        with tempfile.TemporaryDirectory() as temp:
            env = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
            self.run('read-tree', parent, env=env)
            self.run('update-index', '--add', '--cacheinfo', '100644', blob, STATE_PATH, env=env)
            tree = self.run('write-tree', env=env).stdout.strip()
        ident = {'GIT_AUTHOR_NAME': 'Codex coordination', 'GIT_COMMITTER_NAME': 'Codex coordination',
                 'GIT_AUTHOR_EMAIL': 'coordination@example.invalid',
                 'GIT_COMMITTER_EMAIL': 'coordination@example.invalid'}
        head = self.run('commit-tree', tree, '-p', parent,
                        data=proposal['message'] + '\n', env=ident).stdout.strip()
        lease = '--force-with-lease=' + REF + ':' + (expected or '')
        r = self.run('push', '--porcelain', lease, self.remote, head + ':' + REF, check=False)
        if r.returncode:
            # A lost acknowledgement is not grounds to repeat the write.
            require(self.remote_head() == head, 'Coordination write rejected or moved; re-read and re-plan.')
        require(self.remote_head() == head, 'Coordination result moved; verify the recorded task before editing.')
        return {'head': head, 'revision': proposal['state']['revision']}

    def initialize(self, project, base='main'):
        require(self.remote_head() is None, 'Registry already exists; do not reset it.')
        require(re.fullmatch(r'[A-Za-z0-9_./-]+', base) and not base.startswith('-') and '..' not in base,
                'Unsafe base ref.')
        parent = self.remote_head('refs/heads/' + base)
        require(parent, 'Base branch absent.')
        self.fetch(parent)
        return self.commit({'expected_head': None, 'parent_sha': parent, 'ref': REF,
                            'state': empty(project), 'message': 'coordination: initialize [skip ci]'})

    def enrich(self, state, request):
        request = copy.deepcopy(request)
        action = request.get('action')
        if action not in {'pause', 'resume', 'ready', 'verify', 'complete', 'handoff', 'recover', 'renew'} and not (
                action == 'check' and request.get('publish')):
            return request
        task = state['tasks'].get(request.get('task'))
        require(task, 'Task not found.')
        observed = self.remote_head('refs/heads/' + task['branch'])
        require(observed and request.get('head') == observed, 'Source branch head changed; inspect it first.')
        if action in {'recover', 'handoff'} and request.get('new_branch') != task['branch']:
            require(self.remote_head('refs/heads/' + branch(request.get('new_branch'))) in (None, observed),
                    'The proposed handoff branch already contains different work.')
        self.fetch(observed)
        if action == 'ready':
            self.fetch(task['base'])
            request['paths'] = [p for p in self.run('diff', '--no-renames', '--name-only', '-z',
                                                   task['base'], observed).stdout.split('\0') if p]
        if action in {'verify', 'complete'} or action == 'check' and request.get('publish'):
            self.fetch(task['base'])
            actual_paths = [p for p in self.run('diff', '--no-renames', '--name-only', '-z',
                                               task['base'], observed).stdout.split('\0') if p]
            require(all(any(covers(s, scope(p)) for s in source_scopes(state, task))
                        for p in actual_paths), 'Integrated changes exceed the member/declared scope.')
            for t in state['tasks'].values():
                if t['batch'] == task['batch'] and t['role'] == 'work' and t['status'] == 'ready':
                    require(self.remote_head('refs/heads/' + t['branch']) == t['head'],
                            'A member branch moved after its handoff.')
                    self.fetch(t['head'])
                    require(self.run('merge-base', '--is-ancestor', t['head'], observed,
                                     check=False).returncode == 0, 'Integrated source omits a member commit.')
        return request

    def apply(self, request):
        snapshot = self.snapshot()
        result = plan(snapshot, self.enrich(snapshot['state'], request))
        return self.commit(result) if result['changed'] else result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--snapshot', required=True)
    p.add_argument('--request', required=True)
    p = sub.add_parser('initial')
    p.add_argument('--project', required=True)
    for name in ('git-init', 'git-status', 'git-apply'):
        p = sub.add_parser(name)
        p.add_argument('--repository', default='.')
        p.add_argument('--remote', default='origin')
        if name == 'git-init':
            p.add_argument('--project', required=True)
        if name == 'git-apply':
            p.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            result = plan(json.loads(Path(args.snapshot).read_text()),
                          json.loads(Path(args.request).read_text()))
        elif args.command == 'initial':
            result = empty(args.project)
        else:
            store = GitStore(args.repository, args.remote)
            if args.command == 'git-init':
                result = store.initialize(args.project)
            elif args.command == 'git-status':
                result = store.snapshot()
            else:
                result = store.apply(json.loads(Path(args.request).read_text()))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Conflict, OSError, ValueError, KeyError, TypeError) as exc:
        print('ERROR: ' + (str(exc) if isinstance(exc, Conflict) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
