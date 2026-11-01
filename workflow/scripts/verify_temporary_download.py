#!/usr/bin/env python3
"""Consume a disposable canary to check a non-GitHub single-download endpoint.

The canary must NOT be the URL intended for the recipient. This tool does not
create tokens or storage, and cannot prove expiry or a fresh token's validity.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import json
import re
import sys
from threading import Barrier, BrokenBarrierError
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BYTES = 500 * 1024 * 1024
REJECTED = (404, 410)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def validate_contract(contract):
    if not isinstance(contract, dict) or set(contract) != {'url', 'size', 'sha256'}:
        raise ValueError('Expected only url, size and sha256 in the canary contract.')
    url, size, sha256 = (contract[k] for k in ('url', 'size', 'sha256'))
    if type(size) is not int or not 1 <= size <= MAX_BYTES:
        raise ValueError('Invalid expected byte size.')
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ValueError('Expected lowercase SHA256.')
    if not isinstance(url, str) or len(url) > 2048 or any(ord(ch) < 33 for ch in url):
        raise ValueError('Invalid URL.')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid URL.') from None
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or not host or port not in (None, 443) or parsed.fragment):
        raise ValueError('Canary must be direct HTTPS without embedded credentials or fragments.')
    if (host == 'localhost' or host.endswith('.local') or
            host == 'github.com' or host.endswith('.github.com') or
            host.endswith('.githubusercontent.com') or host.endswith('.github.io') or
            host.endswith('.githubassets.com')):
        raise ValueError('GitHub and local hosts are not independent APK hosting.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError('Private or reserved IPs are not permitted.')
    return url, size, sha256


def request_once(url, maximum, opener_factory=None):
    """One GET; never retry, follow redirects or print token-bearing URLs."""
    opener = (opener_factory or (lambda: build_opener(NoRedirect())))()
    try:
        request = Request(url, headers={'Accept': 'application/octet-stream',
                                        'Accept-Encoding': 'identity'}, method='GET')
        with opener.open(request, timeout=45) as response:
            status = response.getcode()
            if status != 200:
                return {'kind': 'unexpected_status', 'http': status}
            total = 0
            sha = hashlib.sha256()
            while True:
                chunk = response.read(min(1024 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    return {'kind': 'oversize'}
                sha.update(chunk)
            return {'kind': 'downloaded', 'size': total, 'sha256': sha.hexdigest()}
    except HTTPError as error:
        status = error.code
        error.close()
        return {'kind': 'rejected' if status in REJECTED else 'unexpected_status', 'http': status}
    except (URLError, TimeoutError, OSError, ValueError):
        return {'kind': 'request_error'}


def probe(contract, fetch=request_once):
    url, size, sha = validate_contract(contract)
    # Two simultaneous attempts are essential: sequential retries alone do not
    # catch a provider that lets two racing requests consume the same token.
    start = Barrier(2)

    def concurrent_fetch(_):
        try:
            start.wait(timeout=10)
        except BrokenBarrierError:
            return {'kind': 'request_error'}
        return fetch(url, size)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(concurrent_fetch, (0, 1)))
    final = fetch(url, size)  # The token must remain unusable after the winner.
    outcomes = [first, second]
    winners = [x for x in outcomes if x.get('kind') == 'downloaded'
               and x.get('size') == size and x.get('sha256') == sha]
    rejects = [x for x in outcomes if x.get('kind') == 'rejected'
               and x.get('http') in REJECTED]
    good = len(winners) == len(rejects) == 1 and final.get('kind') == 'rejected'
    return {
        'status': 'canary_verified' if good else 'failed',
        'concurrent_results': [x['kind'] for x in outcomes],
        'final_result': final.get('kind', 'invalid'),
        'payload_integrity': 'verified' if len(winners) == 1 else 'not_verified',
        'single_use': 'observed' if good else 'not_verified',
        'expiry_60_minutes': 'not_verified',
        'recipient_link': 'not_tested',
        'scope': 'One disposable canary; this does not verify TTL, persistence or any new token.'
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consume-canary', action='store_true', required=True,
                        help='Acknowledge that this disposable link will be consumed.')
    args = parser.parse_args(argv)
    del args
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError('Canary contract too large.')
        contract = json.loads(raw)
        result = probe(contract)
    except (ValueError, TypeError, json.JSONDecodeError):
        result = {'status': 'failed', 'error': 'Invalid canary contract; no URL requested.'}
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0 if result['status'] == 'canary_verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
