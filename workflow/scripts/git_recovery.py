"""Explicit transport recovery for one public source commit or a verified fast-forward.

No automatic retries, force updates, login, PR merge or release operations.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import quote

from resume_status import repository_name
import privacy_scan

SHA = re.compile(r'[0-9a-f]{40}\Z')
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 100, 1024 * 1024, 5 * 1024 * 1024


def git(root, *args, data=None, optional=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_LITERAL_PATHSPECS='1')
    result = subprocess.run(['git', *args], cwd=root, input=data, capture_output=True, env=env, timeout=30)
    if result.returncode:
        if optional:
            return None
        raise ValueError('Local Git operation failed; inspect existing state without resetting it.')
    return result.stdout


def text(root, *args):
    return git(root, *args).decode('utf-8').strip()


class GitHub:
    def __init__(self, repository, executable=None, network='inherit'):
        self.repository = repository
        self.executable = executable or shutil.which('gh')
        if not self.executable:
            raise ValueError('Use the existing GitHub CLI entry; no login or installation is performed.')
        self.env = dict(os.environ, GH_PROMPT_DISABLED='1')
        if network == 'direct':
            self.env = {k: v for k, v in self.env.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
            self.env['NO_PROXY'] = self.env.get('NO_PROXY', '') + ',api.github.com'
        self.mutations = 0

    def __call__(self, path, body=None, missing=False):
        args = [self.executable, 'api', '--hostname', 'github.com', 'repos/' + self.repository + path]
        if body is not None:
            args += ['--method', 'POST', '--input', '-']
            self.mutations += 1
        try:
            result = subprocess.run(args, input=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                                    env=self.env, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('GitHub acknowledgement unavailable; read the target ref before retrying. No automatic replay.') from None
        if result.returncode:
            if missing and re.search(rb'HTTP 404\b', result.stderr):
                return None
            status = re.search(rb'HTTP (\d{3})\b', result.stderr)
            reason = status[1].decode() if status else 'transport_or_cli'
            raise ValueError('GitHub request failed (' + reason + '); no alternate identity, force or automatic retry.')
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise ValueError('GitHub returned invalid metadata; no subsequent mutation was attempted.') from None


def local_state(target):
    root = Path(target).resolve()
    if Path(text(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Target must be the repository root.')
    if git(root, 'status', '--porcelain=v1', '-z'):
        raise ValueError('Working tree is not clean; preserve and inspect existing changes first.')
    repository = repository_name(text(root, 'remote', 'get-url', 'origin'))
    branch = text(root, 'branch', '--show-current')
    if not branch:
        raise ValueError('A checked-out task/default branch is required.')
    return root, repository, branch, text(root, 'rev-parse', 'HEAD')


def require_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Use an explicitly verified full commit SHA.')
    return value


def changed_entries(root, base, head):
    records = git(root, 'diff', '--raw', '--no-abbrev', '-z', '--no-renames', base, head).split(b'\0')
    changes, blobs = [], []
    for offset in range(0, len(records) - 1, 2):
        header, path = records[offset], records[offset + 1].decode('utf-8')
        old_mode, mode, old_sha, sha, status = header[1:].decode('ascii').split()
        name = PurePosixPath(path)
        if name.is_absolute() or any(p in ('.', '..', '.git') for p in name.parts) or any(ord(c) < 32 for c in path):
            raise ValueError('Unsupported source path; no content was transmitted.')
        if status == 'D':
            changes.append(dict(path=path, mode=old_mode, type='blob', sha=None))
        elif status in ('A', 'M') and mode in ('100644', '100755'):
            changes.append(dict(path=path, mode=mode, type='blob', blob=sha))
            blobs.append(sha)
        else:
            raise ValueError('This bounded recovery supports regular UTF-8 source files and deletions only.')
    if not 1 <= len(changes) <= MAX_FILES:
        raise ValueError('Use one source commit changing 1-100 files.')
    if blobs:
        batch = ('\n'.join(blobs) + '\n').encode('ascii')
        sizes = git(root, 'cat-file', '--batch-check', data=batch).splitlines()
        if len(sizes) != len(blobs) or any(row.split()[1] != b'blob' for row in sizes):
            raise ValueError('Source blob identity is incomplete.')
        lengths = [int(row.split()[2]) for row in sizes]
        if max(lengths) > MAX_FILE_BYTES or sum(lengths) > MAX_TOTAL_BYTES:
            raise ValueError('Source exceeds recovery limits; use the existing normal Git/artifact route.')
        raw, cursor, contents = git(root, 'cat-file', '--batch', data=batch), 0, {}
        for sha, length in zip(blobs, lengths):
            end = raw.index(b'\n', cursor)
            if raw[cursor:end].split() != [sha.encode(), b'blob', str(length).encode()]:
                raise ValueError('Source changed while preparing the batch.')
            content = raw[end + 1:end + 1 + length]
            if b'\0' in content:
                raise ValueError('Binary files are not supported by this source-only recovery.')
            try:
                contents[sha] = content.decode('utf-8')
            except UnicodeError:
                raise ValueError('Source must be UTF-8; no content was transmitted.') from None
            cursor = end + length + 2
        for entry in changes:
            if 'blob' in entry:
                entry['content'] = contents[entry.pop('blob')]
    return changes


def prepare_publish(root, repository, branch, head, expected):
    if head != expected:
        raise ValueError('Local source differs from the explicitly expected commit.')
    commit = git(root, 'cat-file', 'commit', head)
    headers = commit.split(b'\n\n', 1)[0].splitlines()
    if any(not row.startswith((b'tree ', b'parent ', b'author ', b'committer ')) for row in headers):
        raise ValueError('Signed or nonstandard local commits require their original Git transport; signatures are never stripped.')
    parents = text(root, 'show', '-s', '--format=%P', head).split()
    if len(parents) != 1:
        raise ValueError('This recovery requires one non-merge source commit.')
    base = parents[0]
    entries = changed_entries(root, base, head)
    message, an, ae, ad, cn, ce, cd = git(root, 'show', '-s', '--format=%B%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI', head).decode('utf-8').split('\0')
    report = privacy_scan.Report(root=str(root))
    for entry in entries:
        if 'content' in entry:
            report.files_scanned += 1
            privacy_scan.check_file_name(str(root / entry['path']), entry['path'], report)
            privacy_scan.scan_text(str(root / entry['path']), entry['path'], entry['content'].splitlines(), report)
    privacy_scan.scan_text('commit-message', 'commit-message', message.splitlines(), report)
    if report.hits:
        raise ValueError('Committed source privacy check found ' + str(len(report.hits)) + ' issue(s); source and excerpts are not printed.')
    tree = text(root, 'rev-parse', head + '^{tree}')
    summary = dict(status='planned', operation='publish', repository=repository, branch=branch,
                   source=head, base=base, tree=tree, changed_files=len(entries), privacy_issues=0)
    payload = dict(message=message, tree=tree, parents=[base], author=dict(name=an, email=ae, date=ad.strip()),
                   committer=dict(name=cn, email=ce, date=cd.strip()))
    return summary, entries, payload


def remote_metadata(api, repository):
    value = api('')
    if (not isinstance(value, dict) or value.get('full_name', '').casefold() != repository.casefold()
            or value.get('private') is not False or not isinstance(value.get('default_branch'), str)):
        raise ValueError('Only the selected existing public GitHub repository is supported; visibility is never changed.')
    return value['default_branch']


def publish(root, repository, branch, head, expected, apply=False, api=None):
    summary, entries, payload = prepare_publish(root, repository, branch, head, expected)
    summary['mutation_requests'] = 0
    if not apply:
        return summary
    default = remote_metadata(api, repository)
    if branch == default:
        raise ValueError('Recovery creates a task branch only; default branch publication is not supported.')
    path = '/git/ref/heads/' + quote(branch, safe='/')
    existing = api(path, missing=True)
    if existing:
        if existing.get('object', {}).get('sha') != head:
            raise ValueError('Remote task branch differs; it is preserved without force or overwrite.')
        return dict(summary, status='already_present')
    if api('/git/ref/heads/' + quote(default, safe='/'))['object']['sha'] != summary['base']:
        raise ValueError('Default branch moved; review the base before publishing this source commit.')
    base_tree = api('/git/commits/' + summary['base'])['tree']['sha']
    created = api('/git/trees', dict(base_tree=base_tree, tree=entries))
    if created.get('sha') != summary['tree']:
        raise ValueError('Remote tree differs from the locally verified source; no ref was created.')
    created = api('/git/commits', payload)
    if created.get('sha') != head:
        raise ValueError('Remote commit identity differs; no ref was created.')
    api('/git/refs', dict(ref='refs/heads/' + branch, sha=head))
    if api(path)['object']['sha'] != head:
        raise ValueError('Source ref acknowledgement differs; read current state before any retry.')
    return dict(summary, status='published', mutation_requests=api.mutations)


def recovered_commit(metadata, target):
    verification = metadata.get('verification', {})
    if verification.get('verified') is not True or not verification.get('payload') or not verification.get('signature'):
        raise ValueError('A verified signed GitHub payload is required for a missing local commit.')
    header, message = verification['payload'].split('\n\n', 1)
    for signature in (verification['signature'].rstrip('\n'), verification['signature']):
        raw = (header + '\ngpgsig ' + signature.replace('\n', '\n ') + '\n\n' + message).encode('utf-8')
        if hashlib.sha1(b'commit ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == target:
            return raw
    raise ValueError('Recovered Git object hash differs; local state is preserved.')


def sync(root, repository, branch, head, expected, target, apply=False, api=None):
    require_sha(target)
    if head not in (expected, target):
        raise ValueError('Local branch moved; preserve existing work before synchronization.')
    summary = dict(status='planned', operation='sync', repository=repository, branch=branch,
                   local_head=head, target=target, mutation_requests=0, local_fast_forward=False)
    if not apply:
        return summary
    default = remote_metadata(api, repository)
    if branch != default or api('/git/ref/heads/' + quote(default, safe='/'))['object']['sha'] != target:
        raise ValueError('Requested target is not the observed default branch tip.')
    if head == target:
        return dict(summary, status='already_current')
    metadata = api('/git/commits/' + target)
    parents = metadata.get('parents', [])
    if expected not in [p.get('sha') for p in parents]:
        raise ValueError('Target is not a direct continuation of the expected base; no checkout was changed.')
    git(root, 'cat-file', '-e', metadata['tree']['sha'] + '^{tree}')
    for parent in parents:
        git(root, 'cat-file', '-e', require_sha(parent['sha']) + '^{commit}')
    if git(root, 'cat-file', '-e', target + '^{commit}', optional=True) is None:
        raw = recovered_commit(metadata, target)
        if text_bytes(git(root, 'hash-object', '-w', '-t', 'commit', '--stdin', data=raw)) != target:
            raise ValueError('Written immutable Git object differs; checkout remains unchanged.')
    if local_state(root)[3] != expected:
        raise ValueError('Local state changed before fast-forward; existing work is preserved.')
    git(root, 'merge', '--ff-only', target)
    tracking = 'refs/remotes/origin/' + default
    previous = git(root, 'rev-parse', '--verify', tracking, optional=True)
    if previous is None or text_bytes(previous) in (expected, target):
        git(root, 'update-ref', tracking, target, text_bytes(previous) if previous is not None else '0' * 40)
    return dict(summary, status='synced', local_fast_forward=True, local_head=text(root, 'rev-parse', 'HEAD'))


def text_bytes(value):
    return value.decode('utf-8').strip()


def recover(args, api=None):
    require_sha(args.expected_head)
    root, repository, branch, head = local_state(args.target)
    if args.operation == 'sync' and not args.remote_head:
        raise ValueError('Synchronization needs the explicitly verified merge commit SHA.')
    if args.apply and api is None:
        api = GitHub(repository, args.gh, args.network)
    if args.operation == 'publish':
        result = publish(root, repository, branch, head, args.expected_head, args.apply, api)
    else:
        result = sync(root, repository, branch, head, args.expected_head, args.remote_head, args.apply, api)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0
