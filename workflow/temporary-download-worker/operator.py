#!/usr/bin/env python3
"""Trusted operator client. Never send ADMIN_TOKEN or a recipient link to CI logs."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BYTES = 500 * 1024 * 1024
HERE = Path(__file__).resolve().parent


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def base_url(raw):
    parsed = urlsplit(raw)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or host == 'github.com' or host.endswith('.github.com')
            or host.endswith('.githubusercontent.com')
            or host.endswith('.github.io')
            or host == 'localhost' or host.endswith('.local')):
        raise ValueError('Endpoint must be an independent HTTPS service origin.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError('Endpoint cannot be a private or reserved IP.')
    return raw.rstrip('/')


def digest(path):
    sha = hashlib.sha256()
    total = 0
    with open(path, 'rb') as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError('APK exceeds supported size.')
    if total == 0:
        raise ValueError('APK cannot be empty.')
    return total, sha.hexdigest()


def operator_call(endpoint, secret, route, payload):
    request = Request(endpoint + '/admin/' + route,
                      data=json.dumps(payload).encode('utf-8'),
                      headers={'Authorization': 'Bearer ' + secret,
                               'Content-Type': 'application/json'},
                      method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=40) as response:
            if response.status not in (200, 201):
                raise ValueError('Unexpected service status.')
            if response.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Unexpected response content type.')
            data = response.read(4097)
            if len(data) > 4096:
                raise ValueError('Response too large.')
            return json.loads(data)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ValueError('Service rejected request (HTTP %d).' % status) from None
    except (URLError, OSError):
        raise ValueError('Could not reach service.') from None


def save_secret_json(path, data):
    # O_EXCL prevents overwriting a previously issued, still-valid recipient URL.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, separators=(',', ':'))
            stream.write('\n')
    except Exception:
        raise


def load_issued(path):
    with open(path, encoding='utf-8') as stream:
        payload = json.load(stream)
    url = payload.get('url')
    if not isinstance(url, str):
        raise ValueError('Issued response does not contain URL.')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.query or parsed.fragment
            or not re.fullmatch(r'/d/[0-9a-f]{64}', parsed.path)):
        raise ValueError('Invalid issued URL format.')
    return payload, parsed.path[3:]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True, help='Worker HTTPS origin; no token path')
    commands = parser.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register', help='Verify APK vs R2 readback before registering')
    register.add_argument('--apk', required=True, help='Verified release APK on cloud runner')
    register.add_argument('--r2-readback', required=True, help='R2 object downloaded back via Wrangler')
    register.add_argument('--object-key', required=True)
    register.add_argument('--filename', required=True)
    issue = commands.add_parser('issue', help='Write sensitive URL once, to mode-0600 file')
    issue.add_argument('--object-key', required=True)
    issue.add_argument('--kind', required=True, choices=('canary', 'recipient', 'expiry'))
    issue.add_argument('--request-id', required=True, help='Stable idempotency key; never repeat')
    issue.add_argument('--output', required=True, help='New secret JSON file, not inside Git repo')
    canary = commands.add_parser('canary', help='Consume a disposable link; print only sanitized verdict')
    canary.add_argument('--issued', required=True)
    expiry = commands.add_parser('expiry', help='Verify a separate link after real 60-minute expiry')
    expiry.add_argument('--issued', required=True)
    inspect = commands.add_parser('inspect', help='Read status without consuming recipient URL')
    inspect.add_argument('--issued', required=True)
    revoke = commands.add_parser('revoke', help='Cancel a previously issued unused URL')
    revoke.add_argument('--issued', required=True)
    args = parser.parse_args(argv)
    try:
        endpoint = base_url(args.endpoint)
        if args.command in ('canary', 'expiry'):
            from importlib.util import module_from_spec, spec_from_file_location
            source = HERE.parent / 'scripts' / 'verify_temporary_download.py'
            spec = spec_from_file_location('verify_temporary_download', source)
            module = module_from_spec(spec)
            spec.loader.exec_module(module)
            issued, _ = load_issued(args.issued)
            if issued.get('kind') != args.command:
                raise ValueError('Refusing to probe a different link kind.')
            contract = {key: issued[key] for key in ('url', 'size', 'sha256')}
            if not contract['url'].startswith(endpoint + '/d/'):
                raise ValueError('Canary endpoint mismatch.')
            if args.command == 'expiry':
                if time.time() * 1000 < issued['expiresAt'] + 5000:
                    raise ValueError('Real expiration time has not elapsed.')
                requested = module.request_once(contract['url'], contract['size'])
                outcome = {'status': 'expiry_verified' if requested ==
                           {'kind': 'rejected', 'http': 410} else 'failed',
                           'http': requested.get('http'),
                           'scope': 'Real expired disposable link; not the recipient URL.'}
            else:
                outcome = module.probe(contract)
            print(json.dumps(outcome, separators=(',', ':')))
            return 0 if outcome['status'] == ('expiry_verified' if args.command == 'expiry'
                                              else 'canary_verified') else 1

        secret = os.environ.get('APK_ONCE_ADMIN_TOKEN', '')
        if len(secret.encode('utf-8')) < 32:
            raise ValueError('APK_ONCE_ADMIN_TOKEN must be provided securely (32+ bytes).')
        if args.command == 'register':
            size, sha = digest(args.apk)
            returned_size, returned_sha = digest(args.r2_readback)
            if (size, sha) != (returned_size, returned_sha):
                raise ValueError('R2 readback differs from original signed APK.')
            response = operator_call(endpoint, secret, 'register', {
                'objectKey': args.object_key, 'size': size,
                'sha256': sha, 'filename': args.filename,
            })
            if response.get('sha256') != sha or response.get('size') != size:
                raise ValueError('Server registration receipt mismatch.')
            print(json.dumps({'status': 'registered', 'size': size, 'sha256': sha}))
        elif args.command == 'issue':
            if Path(args.output).exists():
                raise ValueError('Output already exists; refusing to issue a fresh link.')
            response = operator_call(endpoint, secret, 'issue', {
                'objectKey': args.object_key, 'kind': args.kind,
                'requestId': args.request_id,
            })
            if response.get('kind') != args.kind or response.get('objectKey') != args.object_key:
                raise ValueError('Issuance receipt mismatch; do not deliver this link.')
            save_secret_json(args.output, response)
            print(json.dumps({'status': 'issued_secret_file', 'kind': args.kind,
                              'expiresAt': response.get('expiresAt')}))
        elif args.command in ('inspect', 'revoke'):
            issued, token = load_issued(args.issued)
            if not issued['url'].startswith(endpoint + '/d/'):
                raise ValueError('Issued link endpoint mismatch.')
            result = operator_call(endpoint, secret, args.command, {'token': token})
            if args.command == 'revoke':
                print(json.dumps({'status': result.get('status')}))
            else:
                matches = (result.get('objectKey') == issued.get('objectKey') and
                           result.get('sha256') == issued.get('sha256') and
                           result.get('size') == issued.get('size') and
                           result.get('kind') == issued.get('kind') and
                           result.get('expiresAt') == issued.get('expiresAt') and
                           result.get('expiresAt', 0) - result.get('issuedAt', 0) == 3600000)
                print(json.dumps({'status': 'matched' if matches else 'mismatch',
                                  'kind': result.get('kind'),
                                  'remainingSeconds': result.get('remainingSeconds'),
                                  'consumed': result.get('consumed'),
                                  'expired': result.get('expired'),
                                  'revoked': result.get('revoked')}))
                return 0 if matches and not any(result.get(k) for k in
                                                ('consumed', 'expired', 'revoked')) else 1
        return 0
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        # Avoid printing HTTP errors or any URL/secret. Messages above are fixed.
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
