#!/usr/bin/env python3
"""Read-only project recovery hints. No checkout, fetch, task dispatch or model calls."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
from urllib.parse import unquote, urlsplit

REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
FIELDS = 'number,title,url,headRefName,headRefOid,baseRefName,isDraft,updatedAt,statusCheckRollup'
RELEASE_FIELDS = '{tag_name,name,html_url,draft,prerelease,assets:[.assets[]|{name,size,state,digest,browser_download_url}]}'


def run(root, argv, timeout=20):
    env = dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout, env=env)
    if result.returncode:
        # Remote URLs and tool diagnostics may contain credentials. Never echo them.
        raise ValueError('Read-only ' + argv[0] + ' query failed; inspect the existing connection.')
    return result.stdout


def repository_name(remote):
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', remote)
    if not match or not REPOSITORY.fullmatch(match[1]):
        raise ValueError('Expected a credential-free github.com origin; do not copy credentials into arguments.')
    return match[1]


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key in recovery input.')
        result[key] = value
    return result


def read_snapshot(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Recovery snapshot exceeds 1 MiB.')
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=object_pairs)


def check_summary(rows):
    counts = dict(passed=0, failed=0, pending=0, skipped=0, unknown=0)
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError('Expected check rollup array.')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid check rollup item.')
        conclusion, state = row.get('conclusion'), row.get('state')
        if conclusion in ('FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE') or state in ('ERROR', 'FAILURE'):
            key = 'failed'
        elif conclusion == 'SUCCESS' or state == 'SUCCESS':
            key = 'passed'
        elif conclusion in ('SKIPPED', 'NEUTRAL'):
            key = 'skipped'
        elif row.get('status') in ('QUEUED', 'IN_PROGRESS', 'WAITING', 'PENDING', 'REQUESTED') or state in ('PENDING', 'EXPECTED'):
            key = 'pending'
        else:
            key = 'unknown'
        counts[key] += 1
    status = ('failed' if counts['failed'] else 'pending' if counts['pending'] else
              'inconclusive' if counts['unknown'] or counts['skipped'] else
              'passed' if counts['passed'] else 'not_run')
    return dict(status=status, counts=counts)


def summarize_snapshot(snapshot, repository, branch, head, dirty, now=None):
    if not isinstance(snapshot, dict) or snapshot.get('repository') != repository:
        raise ValueError('Recovery snapshot belongs to another repository.')
    rows = snapshot.get('pull_requests')
    if not isinstance(rows, list) or len(rows) > 100 or type(snapshot.get('complete')) is not bool:
        raise ValueError('Expected at most 100 pull requests and an explicit complete flag.')
    try:
        observed = dt.datetime.fromisoformat(snapshot['observed_at'].replace('Z', '+00:00'))
        age = ((now or dt.datetime.now(dt.timezone.utc)) - observed).total_seconds()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('Expected an observed_at timestamp with timezone.') from None
    if observed.tzinfo is None or age < -300:
        raise ValueError('Invalid recovery snapshot clock.')
    stale = age > 86400
    prs, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid pull request item.')
        number = row.get('number')
        if type(number) is not int or number <= 0 or number in ids:
            raise ValueError('Invalid or duplicate pull request number.')
        ids.add(number)
        if (not isinstance(row.get('headRefOid'), str) or not SHA.fullmatch(row['headRefOid']) or
                any(not isinstance(row.get(k), str) or not row[k] for k in ('title', 'headRefName', 'baseRefName')) or
                type(row.get('isDraft')) is not bool or
                row.get('url') != f'https://github.com/{repository}/pull/{number}'):
            raise ValueError('Invalid pull request identity; use the documented normalized snapshot.')
        same_branch = row['headRefName'] == branch
        checks = check_summary(row.get('statusCheckRollup'))
        prs.append(dict(number=number, title=row['title'][:240], url=row['url'],
                        branch=row['headRefName'], head=row['headRefOid'], draft=row['isDraft'],
                        current_branch=same_branch, current_source=same_branch and row['headRefOid'] == head,
                        checks=checks))
    current = [p for p in prs if p['current_branch']]
    check_action = {'failed': 'diagnose_current_checks', 'pending': 'inspect_pending_checks',
                    'not_run': 'inspect_required_checks', 'inconclusive': 'inspect_required_checks'}
    action = ('refresh_github_snapshot' if stale or not snapshot['complete'] else
              'inspect_local_changes' if dirty else
              'compare_current_source' if current and any(not p['current_source'] for p in current) else
              check_action.get(current[0]['checks']['status'], 'continue_current_task')
              if len(current) == 1 else 'review_related_work')
    return dict(status='stale' if stale else 'observed', observed_at=snapshot['observed_at'],
                complete=snapshot['complete'], pull_requests=prs, next_action=action,
                automatically_selected_pr=None)


def release_url(value, path):
    if not isinstance(value, str) or '\\' in value or re.search(r'[\x00-\x20\x7f]', value):
        raise ValueError('Invalid release URL.')
    url = urlsplit(value)
    decoded = unquote(url.path)
    if (url.scheme != 'https' or url.netloc != 'github.com' or url.query or url.fragment
            or decoded != path or posixpath.normpath(decoded) != decoded):
        raise ValueError('Release URL does not identify the expected repository artifact.')
    return value


def summarize_release(data, repository):
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or not isinstance(data, dict) or not isinstance(data.get('tag_name'), str)
            or not data['tag_name'] or len(data['tag_name']) > 200 or '\\' in data['tag_name']
            or data.get('name') is not None and not isinstance(data['name'], str)
            or type(data.get('draft')) is not bool or type(data.get('prerelease')) is not bool
            or not isinstance(data.get('assets'), list) or len(data['assets']) > 100):
        raise ValueError('Invalid release metadata.')
    tag = data['tag_name']
    result = dict(status='not_formal' if data['draft'] or data['prerelease'] else 'metadata_observed',
                  tag=tag, title=(data.get('name') or tag)[:240], draft=data['draft'], prerelease=data['prerelease'],
                  url=release_url(data.get('html_url'), f'/{repository}/releases/tag/{tag}'),
                  assets=[], source_verification='not_verified', download_verification='not_verified',
                  device_acceptance='not_verified', next_action='verify_candidate_against_delivery_contract')
    if result['status'] == 'not_formal':
        result['next_action'] = 'inspect_required_release_kind'
        return result
    pending = 0
    for item in data['assets']:
        if not isinstance(item, dict):
            raise ValueError('Invalid release attachment.')
        if item.get('state') != 'uploaded':
            pending += 1
            continue
        name, size, digest = item.get('name'), item.get('size'), item.get('digest')
        if (not isinstance(name, str) or not name or len(name) > 255 or '/' in name or '\\' in name
                or name in ('.', '..') or re.search(r'[\x00-\x1f\x7f]', name)
                or type(size) is not int or size <= 0
                or digest is not None and (not isinstance(digest, str) or
                                          not re.fullmatch(r'sha256:[0-9a-f]{64}', digest))):
            raise ValueError('Invalid release attachment identity or digest.')
        result['assets'].append(dict(name=name, size=size, sha256_metadata=digest[7:] if digest else None,
                                     url=release_url(item.get('browser_download_url'),
                                                     f'/{repository}/releases/download/{tag}/{name}')))
    result['pending_assets'] = pending
    if not result['assets']:
        result.update(status='assets_pending' if pending else 'no_assets',
                      next_action='inspect_missing_downloadable_artifact')
    return result


def focus_context(root, query):
    """Bounded locations/declaration hints from tracked working-tree source; no bodies."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError('Focus must contain 1-200 characters.')
    terms = list(dict.fromkeys(re.findall(r'\w{2,}', query.casefold())))
    if not terms or len(terms) > 8:
        raise ValueError('Use 1-8 focused words or symbol names.')
    root = Path(root).resolve()
    argv = ['git', 'grep', '-l', '-z', '-I', '-i', '-F']
    for term in terms:
        argv += ['-e', term]
    found = subprocess.run(argv + ['--', '.'], cwd=root, capture_output=True, timeout=20,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
    if found.returncode not in (0, 1):
        raise ValueError('Read-only focus search failed; inspect the existing repository.')
    extensions = {'.py', '.js', '.jsx', '.ts', '.tsx', '.kt', '.java', '.swift', '.rs',
                  '.go', '.cs', '.c', '.h', '.cpp', '.vue', '.svelte', '.html', '.css', '.md'}
    paths = []
    for raw in found.stdout.split(b'\0'):
        if not raw:
            continue
        path = raw.decode('utf-8')
        target = root / path
        if (target.suffix.casefold() in extensions and not target.is_symlink()
                and target.resolve().is_relative_to(root) and target.is_file()):
            paths.append(path)
    paths.sort(key=lambda p: (-sum(t in p.casefold() for t in terms), p.casefold()))
    declaration = re.compile(r'^\s*(?:(?:export|public|private|protected|internal|open|abstract|override|'
                             r'suspend|inline|data|sealed|async)\s+)*'
                             r'(?:class|interface|object|fun|function|def|fn|struct|enum)\s+([^\W\d]\w*)')
    files = []
    for path in paths[:8]:
        target = root / path
        item = dict(path=path, declarations=[])
        try:
            size = target.stat().st_size
            item['bytes'] = size
            if size > 128 * 1024:
                item['status'] = 'requires_targeted_read'
            else:
                content = target.read_bytes()
                item.update(status='observed', sha256=hashlib.sha256(content).hexdigest())
                if target.suffix.casefold() != '.md':
                    for number, line in enumerate(content.decode('utf-8-sig').splitlines(), 1):
                        match = declaration.match(line[:240])
                        if match:
                            item['declarations'].append(dict(name=match[1][:80], line=number))
                    item['declarations'].sort(key=lambda d: (-sum(t in d['name'].casefold() for t in terms), d['line']))
                    item['declarations'] = item['declarations'][:6]
        except (OSError, UnicodeError):
            item.update(status='requires_targeted_read', declarations=[])
        files.append(item)
    return dict(status='observed' if paths else 'no_matches', matched_files=len(paths),
                truncated=len(paths) > 8, files=files,
                scope='Tracked working-tree locations and declaration hints; not a complete dependency map.')


def inspect(root, snapshot_path=None, github=False, focus=None, skip_release=False):
    if skip_release and not (github or snapshot_path):
        raise ValueError('Skip-release requires GitHub recovery or an existing connector snapshot.')
    root = Path(root).resolve()
    git = lambda *args: run(root, ['git', *args]).strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Target must be the repository root.')
    head = git('rev-parse', 'HEAD')
    branch = git('branch', '--show-current') or None
    status = git('status', '--porcelain=v1', '-z', '--untracked-files=normal')
    remote = git('remote', 'get-url', 'origin')
    repository = repository_name(remote)
    result = dict(repository=repository, branch=branch, head=head, dirty=bool(status),
                  entrypoints=[p for p in ('AGENTS.md', 'README.md', 'CLOUD_DEVELOPMENT.md',
                               'HANDOFF.md', 'docs/TASK.md', '.ai-workflow.json') if (root / p).is_file()],
                  github=dict(status='not_read', next_action='read_related_pull_requests'),
                  delivery=dict(status='not_read', next_action='read_latest_release'),
                  product_acceptance='not_verified', writer_ownership='not_verified',
                  guidance='Read project entrypoints and the existing coordination registry; PR titles, checks and recency do not establish task ownership or product acceptance.')
    release_reason = None
    if skip_release:
        result['delivery'] = dict(status='not_requested', next_action='read_latest_release_before_delivery')
    if focus is not None:
        result['context'] = focus_context(root, focus)
    if snapshot_path:
        snapshot = read_snapshot(snapshot_path)
    elif github:
        try:
            rows = json.loads(run(root, ['gh', 'pr', 'list', '--repo', 'github.com/' + repository,
                                        '--state', 'open', '--limit', '100', '--json', FIELDS]))
            if not isinstance(rows, list):
                raise ValueError('Invalid PR list.')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = ('timeout' if isinstance(exc, subprocess.TimeoutExpired) else
                      'cli_unavailable' if isinstance(exc, FileNotFoundError) else 'query_failed')
            result['github'] = dict(status='unavailable', reason=reason,
                                    next_action='read_related_pull_requests_with_existing_connector')
            return result
        snapshot = dict(repository=repository, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        complete=len(rows) < 100, pull_requests=rows)
        if not skip_release:
            try:
                snapshot['latest_release'] = json.loads(run(root, ['gh', 'api', f'repos/{repository}/releases/latest',
                                                                  '--jq', RELEASE_FIELDS]))
            except (OSError, ValueError, subprocess.SubprocessError):
                snapshot['latest_release'] = None
                release_reason = 'query_failed'
    else:
        return result
    result['github'] = summarize_snapshot(snapshot, repository, branch, head, bool(status))
    if 'latest_release' in snapshot and not skip_release:
        if snapshot['latest_release'] is None:
            result['delivery'] = dict(status='unavailable', reason=release_reason or 'not_visible_or_missing',
                                      next_action='inspect_release_with_existing_connector')
        else:
            try:
                result['delivery'] = summarize_release(snapshot['latest_release'], repository)
            except (ValueError, TypeError):
                result['delivery'] = dict(status='invalid', next_action='refresh_release_metadata')
        if result['github']['status'] == 'stale':
            result['delivery'].update(status='stale', next_action='refresh_github_snapshot')
    return result


def recovery_brief(result):
    """A deterministic agent-owned next step, not permission or task dispatch."""
    github, delivery = result['github'], result['delivery']
    action = ('inspect_local_changes' if result['dirty'] else
              'identify_source_and_writer' if result['branch'] is None else
              github['next_action'])
    summaries = {
        'inspect_local_changes': '先核对并保留未提交改动，再续接原任务。',
        'identify_source_and_writer': '当前源码未附着分支，先核对来源与现有写手。',
        'read_related_pull_requests': '本地状态已读，按本次任务补读相关远端工作。',
        'read_related_pull_requests_with_existing_connector': '远端读取失败，保留本地结果并用已有授权连接补读一次。',
        'refresh_github_snapshot': '远端快照过时或不完整，先补足相关信息。',
        'compare_current_source': '当前分支与远端 PR 源码不同，先比较差异。',
        'diagnose_current_checks': '当前源码的检查失败，先读取具体诊断，再处理原因。',
        'inspect_pending_checks': '当前检查尚未完成，先做可独立工作，避免反复轮询。',
        'inspect_required_checks': '当前检查缺失、跳过或结论不明，先核对项目必需检查。',
        'continue_current_task': '当前 PR 源码与检查匹配，读取原交接并确认写手后续接。',
        'review_related_work': '尚未唯一对应当前 PR，结合原交接核对相关工作。',
    }
    incomplete = (github['status'] in ('unavailable', 'stale') or github.get('complete') is False
                  or delivery['status'] in ('unavailable', 'invalid', 'stale'))
    return dict(next_action=action, summary=summaries[action], responsibility='agent',
                automatic_execution=False, remote_read_incomplete=incomplete,
                delivery_next_action=delivery['next_action'],
                user_action='not_determined_by_readonly_probe',
                product_acceptance=result['product_acceptance'],
                writer_ownership=result['writer_ownership'])


def resume(args):
    result = inspect(args.target, args.github_json, args.github, getattr(args, 'focus', None),
                     getattr(args, 'skip_release', False))
    result['brief'] = recovery_brief(result)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 2 if result['brief']['remote_read_incomplete'] else 0
