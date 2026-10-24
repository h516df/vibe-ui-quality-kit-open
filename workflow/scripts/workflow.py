#!/usr/bin/env python3
"""Cloud workflow bootstrap/check/receipt. Python standard library; no credentials."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

VERSION = '0.1'
BEGIN, END = '<!-- ai-workflow:start -->', '<!-- ai-workflow:end -->'


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def repository(target):
    root = Path(target).resolve()
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Target must be the repository root.')
    return root


def write_target(root, relative):
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('Refusing symlink/outside-repository destination: ' + relative)
    return target


def adopt(args):
    root = repository(args.target)
    package = Path(__file__).resolve().parents[1]
    template = package / 'templates'
    if not template.is_dir():
        raise ValueError('Use adopt from the original workflow/scripts package.')
    files = {
        'docs/AI_WORKFLOW.md': (package / 'WORKFLOW.md').read_text(),
        'cloud/workflow.py': Path(__file__).read_text(),
        'cloud/coordinate.py': (package / 'scripts/coordinate.py').read_text(),
        'cloud/verify_release.py': (package / 'scripts/verify_release.py').read_text(),
        'cloud/resume_status.py': (package / 'scripts/resume_status.py').read_text(),
        'docs/DELIVERY.md': (package / 'DELIVERY.md').read_text(),
        '.ai-workflow.json': (template / 'project-checks.json').read_text(),
    }
    # Preserve customized check configuration on repeated adoption.
    config = write_target(root, '.ai-workflow.json')
    if config.exists():
        existing = json.loads(config.read_text())
        if existing.get('workflow_version') != VERSION:
            raise ValueError('Existing workflow version differs; review an explicit upgrade.')
        validate_config(existing)
        files.pop('.ai-workflow.json')
    handoff = write_target(root, 'HANDOFF.md')
    if not handoff.exists():
        files['HANDOFF.md'] = (template / 'HANDOFF.md').read_text()
    agents = write_target(root, 'AGENTS.md')
    old = agents.read_text() if agents.exists() else ''
    block = BEGIN + '\n' + (template / 'AGENTS.fragment.md').read_text().strip() + '\n' + END
    if BEGIN in old or END in old:
        if old.count(BEGIN) != 1 or old.count(END) != 1:
            raise ValueError('Ambiguous managed AGENTS block; review manually.')
        existing = old[old.index(BEGIN):old.index(END) + len(END)]
        if existing != block:
            raise ValueError('AGENTS workflow block differs; preserve it and review manually.')
    else:
        files['AGENTS.md'] = old.rstrip() + ('\n\n' if old.strip() else '') + block + '\n'
    # Preflight every path before writing any file. Only the deliberate append is allowed.
    for relative, content in files.items():
        target = write_target(root, relative)
        if relative != 'AGENTS.md' and target.exists() and target.read_text() != content:
            raise ValueError('Conflict; no files written: ' + relative)
    changes = {p: s for p, s in files.items()
               if not (root / p).exists() or (root / p).read_text() != s}
    for relative in changes:
        print(('WRITE ' if args.apply else 'PLAN ') + relative)
    if args.apply:
        for relative, content in changes.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    print(('Applied' if args.apply else 'Dry run') + f': {len(changes)} file(s). No push or deployment.')
    return 0


def validate_config(config):
    if config.get('version') != 1 or not isinstance(config.get('checks'), list):
        raise ValueError('Expected version 1 and a checks array.')
    names = set()
    for item in config['checks']:
        if not isinstance(item, dict):
            raise ValueError('Each check must be an object.')
        command = item.get('command')
        name = item.get('name')
        timeout = item.get('timeout_seconds', 120)
        if (not isinstance(name, str) or not name or name in names or
                item.get('level') not in ('quick', 'core') or
                not isinstance(command, list) or not command or
                any(not isinstance(s, str) or not s for s in command) or
                not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 900):
            raise ValueError('Invalid check name/level/argv/timeout, or duplicate name.')
        names.add(name)


def check(args):
    root = repository(args.target)
    config = json.loads((root / '.ai-workflow.json').read_text())
    validate_config(config)
    git(root, 'diff', '--check')
    print('PASS: working-tree whitespace guard (not a functional test).', flush=True)
    if not config['checks']:
        print('NOT CONFIGURED: no project checks; whitespace alone cannot pass.', file=sys.stderr)
        return 2
    if args.level == 'core' and not any(c['level'] == 'core' for c in config['checks']):
        print('NOT CONFIGURED: core functional checks.', file=sys.stderr)
        return 2
    count = 0
    for item in config['checks']:
        if args.level == 'quick' and item['level'] != 'quick':
            continue
        print('RUN: ' + item['name'], flush=True)
        result = subprocess.run(item['command'], cwd=root, timeout=item.get('timeout_seconds', 120))
        if result.returncode:
            print('FAIL: ' + item['name'], file=sys.stderr)
            return 1
        count += 1
    if not count:
        print('NOT CONFIGURED: no checks selected for this level.', file=sys.stderr)
        return 2
    print(f'PASS: {args.level}, {count} configured checks. This does not assert deployment or device validation.')
    return 0


def receipt(args):
    root = repository(args.target)
    head = git(root, 'rev-parse', 'HEAD')
    branch = args.branch or git(root, 'symbolic-ref', '--short', 'HEAD')
    if branch.startswith('-') or args.remote.startswith('-'):
        raise ValueError('Remote and branch must not be command options.')
    git(root, 'check-ref-format', 'refs/heads/' + branch)
    ref = 'refs/heads/' + branch
    rows = git(root, 'ls-remote', '--exit-code', args.remote, ref).splitlines()
    remote_sha = next((r.split()[0] for r in rows if r.split()[-1] == ref), None)
    dirty = bool(git(root, 'status', '--porcelain'))
    verified = remote_sha == head and not dirty
    print(json.dumps({'branch': branch, 'local_sha': head, 'remote_sha': remote_sha,
                      'clean': not dirty, 'verified': verified}))
    return 0 if verified else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('adopt', 'check', 'receipt', 'resume'):
        p = sub.add_parser(name)
        p.add_argument('--target', default='.')
        if name == 'adopt':
            p.add_argument('--apply', action='store_true')
        elif name == 'check':
            p.add_argument('--level', choices=['quick', 'core'], default='quick')
        elif name == 'receipt':
            p.add_argument('--remote', default='origin')
            p.add_argument('--branch')
        else:
            source = p.add_mutually_exclusive_group()
            source.add_argument('--github', action='store_true', help='Read open PRs and latest release metadata using existing gh authentication; never logs in.')
            source.add_argument('--github-json', help='Use a normalized snapshot from the current authorized GitHub connector.')
    args = parser.parse_args()
    try:
        if args.action == 'resume':
            from resume_status import resume
            return resume(args)
        return {'adopt': adopt, 'check': check, 'receipt': receipt}[args.action](args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Do not echo argv or remote URLs: they may contain credentials.
        print('ERROR: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
