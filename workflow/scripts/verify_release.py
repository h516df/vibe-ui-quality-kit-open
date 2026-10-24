#!/usr/bin/env python3
"""Read-only GitHub release verification against an explicit delivery contract."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, unquote, urlparse


class Failure(Exception):
    def __init__(self, errors):
        self.errors = errors if isinstance(errors, list) else [errors]
        super().__init__('; '.join(self.errors))


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def validate(contract, root):
    if not isinstance(contract, dict) or type(contract.get('schema_version')) is not int or contract['schema_version'] != 1:
        raise Failure('Expected delivery contract schema_version 1.')
    repo = contract.get('repository')
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise Failure('Expected repository owner/name.')
    tag = contract.get('tag')
    if not isinstance(tag, str) or not tag or any(ord(c) < 32 for c in tag):
        raise Failure('Expected an explicit release tag.')
    if contract.get('kind') not in ('release', 'prerelease'):
        raise Failure('Explicit kind must be release or prerelease.')
    if type(contract.get('require_latest')) is not bool:
        raise Failure('require_latest must be an explicit boolean.')
    if contract['kind'] == 'prerelease' and contract['require_latest']:
        raise Failure('A prerelease cannot be the GitHub latest release.')
    target = contract.get('target_commit')
    if not isinstance(target, str) or not re.fullmatch(r'[0-9a-f]{40}', target):
        raise Failure('target_commit must be a complete Git commit SHA, read from Git.')
    assets = contract.get('assets')
    if not isinstance(assets, list) or not assets:
        raise Failure('At least one expected native Release asset is required.')
    names = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise Failure('Each expected asset must be an object.')
        name = asset.get('name')
        if (not isinstance(name, str) or not name or name in ('.', '..') or
                any(c in name for c in '/\\') or any(ord(c) < 32 for c in name) or name in names):
            raise Failure('Asset names must be unique filenames.')
        names.add(name)
        if type(asset.get('size')) is not int or asset['size'] < 1:
            raise Failure('Expected positive byte size for asset: ' + name)
        if not isinstance(asset.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']):
            raise Failure('Expected full SHA256 for asset: ' + name)
        if 'path' in asset:
            relative = asset['path']
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise Failure('Local asset path must be relative to the project root.')
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise Failure('Local asset must exist inside project root: ' + name)
            if path.stat().st_size != asset['size'] or digest(path) != asset['sha256']:
                raise Failure('Local asset does not match the pinned size/SHA256: ' + name)
    return contract


def github(path):
    try:
        result = subprocess.run(['gh', 'api', path], text=True, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise Failure('GitHub CLI is not installed.') from None
    except subprocess.TimeoutExpired:
        raise Failure('GitHub metadata request timed out; release is not verified.') from None
    if result.returncode:
        # CLI errors can contain signed redirect URLs. Never forward raw stderr.
        status = re.search(r'HTTP (\d{3})', result.stderr)
        reason = 'HTTP ' + status.group(1) if status else 'exit ' + str(result.returncode)
        raise Failure('GitHub metadata request failed (' + reason + '); release is not verified.')
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise Failure('GitHub returned invalid metadata; release is not verified.') from None


def tag_commit(base, tag, api):
    ref = api(base + '/git/ref/tags/' + quote(tag, safe=''))
    obj = ref.get('object') if isinstance(ref, dict) else None
    seen = set()
    for _ in range(8):
        if not isinstance(obj, dict) or not re.fullmatch(r'[0-9a-f]{40}', str(obj.get('sha', ''))):
            raise Failure('GitHub tag has no valid target commit.')
        if obj.get('type') == 'commit':
            return obj['sha']
        if obj.get('type') != 'tag' or obj['sha'] in seen:
            raise Failure('GitHub tag does not resolve to a commit.')
        seen.add(obj['sha'])
        ref = api(base + '/git/tags/' + obj['sha'])
        obj = ref.get('object') if isinstance(ref, dict) else None
    raise Failure('GitHub annotated tag resolution exceeded its bounded depth.')


def verify(contract, api=github):
    repo, tag = contract['repository'], contract['tag']
    base = '/repos/' + repo
    release = api(base + '/releases/tags/' + quote(tag, safe=''))
    if not isinstance(release, dict) or type(release.get('id')) is not int:
        raise Failure('GitHub returned no valid release record.')
    errors = []
    if release.get('tag_name') != tag:
        errors.append('Release tag does not match the requested version.')
    if release.get('draft') is not False:
        errors.append('Release is still a draft or its draft state is unknown.')
    if release.get('prerelease') is not (contract['kind'] == 'prerelease'):
        errors.append('Release prerelease flag does not match requested kind: ' + contract['kind'])
    source = tag_commit(base, tag, api)
    if source != contract['target_commit']:
        errors.append('Release tag target differs from the pinned commit.')
    if contract['require_latest']:
        latest = api(base + '/releases/latest')
        if not isinstance(latest, dict) or latest.get('id') != release['id'] or latest.get('tag_name') != tag:
            errors.append('GitHub latest does not point to the requested release.')
    remote_assets = release.get('assets')
    if not isinstance(remote_assets, list):
        errors.append('Native Release assets metadata is missing.')
        remote_assets = []
    verified = []
    for expected in contract['assets']:
        name = expected['name']
        matches = [a for a in remote_assets if isinstance(a, dict) and a.get('name') == name]
        if len(matches) != 1:
            errors.append('Expected exactly one native Release asset: ' + name)
            continue
        asset = matches[0]
        if asset.get('state') != 'uploaded':
            errors.append('Native asset is not uploaded: ' + name)
        if type(asset.get('size')) is not int or asset['size'] != expected['size']:
            errors.append('Native asset byte size differs: ' + name)
        if asset.get('digest') != 'sha256:' + expected['sha256']:
            errors.append('Native asset SHA256 differs or is unavailable: ' + name)
        url = asset.get('browser_download_url')
        try:
            parsed = urlparse(url) if isinstance(url, str) else urlparse('')
        except ValueError:
            parsed = urlparse('')
        if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment or
                unquote(parsed.path) != '/' + repo + '/releases/download/' + tag + '/' + name):
            errors.append('Native asset download URL is not the expected GitHub release URL: ' + name)
        verified.append({'name': name, 'size': expected['size'], 'sha256': expected['sha256'], 'download_url': url})
    if errors:
        raise Failure(errors)
    return {'status': 'verified', 'repository': repo, 'tag': tag, 'kind': contract['kind'],
            'release_id': release['id'], 'release_url': 'https://github.com/' + repo + '/releases/tag/' + quote(tag, safe=''),
            'draft': False, 'prerelease': contract['kind'] == 'prerelease',
            'latest_checked': contract['require_latest'], 'target_commit': source,
            'assets': verified, 'verification_scope': 'GitHub metadata and pinned digests; device installation and client download not tested'}


def write_receipt(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    if args.receipt and args.receipt.resolve() == args.contract.resolve():
        parser.error('Receipt must be separate from the contract.')
    result = {'status': 'failed'}
    try:
        try:
            contract = json.loads(args.contract.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            raise Failure('Cannot read a valid delivery contract.') from None
        validate(contract, args.root.resolve())
        if args.receipt:
            for asset in contract['assets']:
                if asset.get('path') and args.receipt.resolve() == (args.root / asset['path']).resolve():
                    parser.error('Receipt must be separate from every local artifact.')
        result = verify(contract)
    except Failure as error:
        result['errors'] = error.errors
    except (OSError, ValueError, TypeError, RuntimeError):
        result['errors'] = ['Local verification failed; release is not verified.']
    result['checked_at'] = datetime.now(timezone.utc).isoformat()
    if args.receipt:
        try:
            # A failed run replaces an old success; stale success cannot be reused.
            write_receipt(args.receipt, result)
        except OSError:
            result = {'status': 'failed', 'errors': ['Cannot write the requested verification receipt.']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
