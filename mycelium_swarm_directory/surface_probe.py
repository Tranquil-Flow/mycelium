# SPDX-License-Identifier: AGPL-3.0-only
"""Public-surface checklist probe for the hosted demo origin (run by the operator before turning it on).

    python -m mycelium_swarm_directory.surface_probe --base https://mycelium.now

Asserts, from outside, what the hardening promises: (a) the legacy product-gateway routes
(``POST /api/v1/inference``, ``GET /api/v1/bootstrap``, ``GET /api/v1/product/export``, any other path) answer
exactly 404 and (b) the directory page and its config set no cookie.  It sends no body beyond ``{}``, follows no
redirect, and only ever contacts the one ``--base`` origin (``https://<host>`` or a loopback ``http://``).
Prints one JSON line; exit 0 only when every check passed.  Could not tell (unreachable) counts as unhealthy.
This module makes no request on import; nothing in the test suite points it at a real host.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from . import hosted_defaults

_MOUNT = hosted_defaults.DIRECTORY_MOUNT_PREFIX
#: (method, path) that must answer 404 on the public origin while the directory is mounted.
CLOSED_ROUTES = (
    ('POST', '/api/v1/inference'),
    ('GET', '/api/v1/bootstrap'),
    ('GET', '/api/v1/product/export'),
    ('GET', '/api/v1/observatory/events'),
    ('GET', '/api/v1/swarm/status'),
    ('GET', '/anything'),
)
#: Pages that must set no cookie (and must answer 200).
COOKIE_FREE = (_MOUNT + '/', _MOUNT + '/api/v1/config')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _fetch(base: str, method: str, path: str, client_ip: str | None, opener: Callable[..., Any],
           timeout: float) -> tuple[int, Any] | None:
    # Cloudflare's browser integrity check answers 403 to the default Python-urllib agent, so name the probe.
    headers = {'Accept': '*/*', 'User-Agent': 'mycelium-surface-probe/1 (+https://mycelium.now/swarms/)'}
    if client_ip:
        headers['Cf-Connecting-Ip'] = client_ip  # only for loopback tests; Cloudflare sets it on the real origin
    data = None
    if method == 'POST':
        data, headers['Content-Type'] = b'{}', 'application/json'
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with opener(request, timeout) as response:
            return getattr(response, 'status', 200), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.headers
    except BaseException:  # noqa: BLE001 - a probe never raises
        return None


def _sets_cookie(headers: Any) -> bool:
    try:
        return any(str(name).lower() == 'set-cookie' for name, _value in headers.items())
    except AttributeError:
        return True  # cannot read the headers: not provably cookie-free


def probe(base: str, *, client_ip: str | None = None, opener: Callable[..., Any] = _default_opener,
          timeout: float = 10.0) -> dict[str, Any]:
    base = base.rstrip('/')
    checks: list[dict[str, Any]] = []
    for method, path in CLOSED_ROUTES:
        seen = _fetch(base, method, path, client_ip, opener, timeout)
        checks.append({'name': '%s %s' % (method, path), 'status': None if seen is None else seen[0],
                       'ok': None if seen is None else seen[0] == 404})
    for path in COOKIE_FREE:
        seen = _fetch(base, 'GET', path, client_ip, opener, timeout)
        ok = None if seen is None else (seen[0] == 200 and not _sets_cookie(seen[1]))
        checks.append({'name': 'no Set-Cookie on ' + path, 'status': None if seen is None else seen[0], 'ok': ok})
    passed = all(check['ok'] is True for check in checks)
    return {'base': base, 'checks': checks, 'verdict': 'closed_and_cookie_free' if passed else 'FAIL'}


def _base(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ''
    loopback = False
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if (parts.scheme not in {'https', 'http'} or not host or parts.username is not None or parts.password is not None
            or parts.path not in {'', '/'} or parts.query or parts.fragment
            or (parts.scheme == 'http' and not loopback)):
        raise argparse.ArgumentTypeError('base must be https://<host> or a loopback http origin')
    return value.rstrip('/')


def main(argv: list[str] | None = None, *, opener: Callable[..., Any] = _default_opener) -> int:
    parser = argparse.ArgumentParser(prog='python -m mycelium_swarm_directory.surface_probe', allow_abbrev=False)
    parser.add_argument('--base', required=True, type=_base, help='public origin, e.g. https://mycelium.now')
    args = parser.parse_args(argv)
    result = probe(args.base, opener=opener)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result['verdict'] == 'closed_and_cookie_free' else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
