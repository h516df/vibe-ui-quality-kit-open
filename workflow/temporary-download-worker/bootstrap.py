#!/usr/bin/env python3
"""Idempotent, manual-only Cloudflare bootstrap for one APK download Worker.

Requires three GitHub Actions secrets in the caller's environment, never CLI
arguments: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, APK_ONCE_ADMIN_TOKEN.
No deletes, upgrades, account changes, public R2 access, or APK token issuance.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


API = 'https://api.cloudflare.com/client/v4'
WRANGLER = ('npx', '--yes', 'wrangler@4.156.0')
ROOT = Path(__file__).resolve().parent
ACCOUNT_RE = re.compile(r'^[0-9a-f]{32}$')
UUID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
SUBDOMAIN_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class BootstrapError(Exception):
    pass


def resource_names(account_id):
    if not ACCOUNT_RE.fullmatch(account_id):
        raise BootstrapError('invalid_account_id')
    suffix = account_id[:8]
    return {'worker': f'apk-once-download-{suffix}',
            'bucket': f'apk-once-private-{suffix}',
            'database': f'apk-once-tokens-{suffix}'}


class CloudflareAPI:
    def __init__(self, account_id, token, opener=None):
        resource_names(account_id)
        if not isinstance(token, str) or len(token) < 20:
            raise BootstrapError('missing_api_token')
        self.account_id = account_id
        self.token = token
        self.opener = opener or build_opener(NoRedirect())

    def call(self, method, suffix, payload=None, allow_missing=False):
        if not suffix.startswith('/') or '://' in suffix or '..' in suffix:
            raise BootstrapError('invalid_api_path')
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(API + '/accounts/' + self.account_id + suffix,
                          data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/json',
                                   'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 201):
                    raise BootstrapError('cloudflare_unexpected_http_status')
                raw = response.read(1024 * 1024 + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and allow_missing:
                return None
            # Never expose response bodies; Cloudflare or proxies may echo data.
            raise BootstrapError(f'cloudflare_http_{code}') from None
        except (URLError, TimeoutError, OSError):
            raise BootstrapError('cloudflare_network_failure') from None
        if len(raw) > 1024 * 1024:
            raise BootstrapError('cloudflare_response_too_large')
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise BootstrapError('cloudflare_invalid_response') from None
        if not isinstance(parsed, dict) or parsed.get('success') is not True:
            raise BootstrapError('cloudflare_api_rejected')
        return parsed.get('result')


def ensure_resources(api, names, *, apply):
    # Resolve Workers subdomain before writing anything; never overwrite an
    # existing account-wide subdomain with a guessed value.
    sub = api.call('GET', '/workers/subdomain', allow_missing=True)
    subdomain = (sub or {}).get('subdomain')
    if not isinstance(subdomain, str) or not SUBDOMAIN_RE.fullmatch(subdomain):
        raise BootstrapError('workers_subdomain_needs_account_setup')

    bucket = api.call('GET', '/r2/buckets/' + quote(names['bucket']), allow_missing=True)
    bucket_exists = bucket is not None
    if bucket_exists and (not isinstance(bucket, dict) or bucket.get('name') != names['bucket']):
        raise BootstrapError('unexpected_existing_bucket')
    databases = api.call('GET', '/d1/database?name=' + quote(names['database']) + '&per_page=100')
    if not isinstance(databases, list):
        raise BootstrapError('unexpected_d1_list')
    matches = [db for db in databases if db.get('name') == names['database']]
    if len(matches) > 1:
        raise BootstrapError('duplicate_d1_database_name')
    database_exists = bool(matches)

    if apply:
        if not bucket_exists:
            api.call('POST', '/r2/buckets', {'name': names['bucket'], 'storageClass': 'Standard'})
        if not matches:
            created = api.call('POST', '/d1/database', {'name': names['database']})
            matches = [created]
    database_id = (matches[0] or {}).get('uuid') if matches else None
    if database_id is not None and not UUID_RE.fullmatch(database_id):
        raise BootstrapError('invalid_d1_database_id')
    if apply and not database_id:
        raise BootstrapError('d1_database_not_available')

    return {'subdomain': subdomain, 'database_id': database_id,
            'bucket_created': apply and not bucket_exists,
            'database_created': apply and not database_exists,
            'bucket_exists': bucket_exists, 'database_exists': database_exists}


def worker_config(names, info):
    origin = f"https://{names['worker']}.{info['subdomain']}.workers.dev"
    return ({
        'name': names['worker'], 'main': 'src/index.js',
        'compatibility_date': '2026-10-01', 'workers_dev': True,
        'observability': {'enabled': False},
        'vars': {'PUBLIC_BASE_URL': origin},
        'r2_buckets': [{'binding': 'APKS', 'bucket_name': names['bucket']}],
        'd1_databases': [{'binding': 'DB', 'database_name': names['database'],
                          'database_id': info['database_id']}],
    }, origin)


def run_wrangler(*args, cwd, stdin=None):
    stage = '_'.join(str(value).replace('-', '_') for value in args[:2])
    try:
        result = subprocess.run([*WRANGLER, *args], cwd=cwd,
                                input=stdin, text=True, capture_output=True,
                                timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError('wrangler_failed_to_start') from None
    # Don't print stdout/stderr: an upstream tool could echo a secret.
    if result.returncode:
        raise BootstrapError('wrangler_' + stage + '_failed')


def deploy(names, info, admin_token):
    if not isinstance(admin_token, str) or len(admin_token.encode('utf-8')) < 32:
        raise BootstrapError('missing_or_short_admin_secret')
    config, origin = worker_config(names, info)
    with tempfile.TemporaryDirectory(prefix='apk-once-') as temp:
        target = Path(temp)
        shutil.copytree(ROOT / 'src', target / 'src')
        shutil.copytree(ROOT / 'migrations', target / 'migrations')
        (target / 'wrangler.json').write_text(json.dumps(config), encoding='utf-8')
        run_wrangler('d1', 'execute', names['database'], '--remote',
                     '--file=migrations/0001_init.sql', '--yes', cwd=target)
        run_wrangler('deploy', cwd=target)
        run_wrangler('secret', 'put', 'ADMIN_TOKEN', cwd=target,
                     stdin=admin_token + '\n')
    # Runtime health is read-only, not a signed APK or 60-minute acceptance.
    req = Request(origin + '/healthz', method='GET')
    health = False
    for attempt in range(3):
        try:
            with build_opener(NoRedirect()).open(req, timeout=20) as response:
                health = response.status == 200 and b'"ok"' in response.read(512)
        except (HTTPError, URLError, TimeoutError, OSError):
            health = False
        if health:
            break
        if attempt < 2:
            time.sleep(2)
    if not health:
        raise BootstrapError('worker_health_not_verified')
    return origin


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true', help='Create free resources and deploy; default is read-only')
    args = p.parse_args(argv)
    try:
        account = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
        token = os.environ.get('CLOUDFLARE_API_TOKEN', '')
        admin = os.environ.get('APK_ONCE_ADMIN_TOKEN', '')
        names = resource_names(account)
        if args.apply and len(admin.encode('utf-8')) < 32:
            raise BootstrapError('missing_or_short_admin_secret')
        info = ensure_resources(CloudflareAPI(account, token), names, apply=args.apply)
        if not args.apply:
            print(json.dumps({'status': 'plan', 'resources': names,
                              'bucket_exists': info['bucket_exists'],
                              'database_exists': info['database_exists']}, separators=(',', ':')))
            return 0
        origin = deploy(names, info, admin)
        print(json.dumps({'status': 'deployed_health_verified', 'origin': origin,
                          'resources': names, 'bucket_created': info['bucket_created'],
                          'database_created': info['database_created']}, separators=(',', ':')))
        return 0
    except BootstrapError as exc:
        print(json.dumps({'status': 'blocked', 'code': str(exc)}, separators=(',', ':')),
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
