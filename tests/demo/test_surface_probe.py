# SPDX-License-Identifier: AGPL-3.0-only
"""M7 (probe only): the public-surface checklist probe asserts the product routes are closed and no cookie is set.

Run here only against loopback stand-ins (the shipped proxy in front of recording stubs).  Nothing in this file
contacts a real host; the operator runs the CLI against the live origin when the demo is turned back on.
Review: R3 M7 (a) /api/v1/inference and /api/v1/bootstrap answer 404 on the public host, (b) no Set-Cookie on /swarms/.
"""
import io
import json
import urllib.error

import pytest

from mycelium_swarm_directory import surface_probe
from _proxy_world import world


def names(result, ok):
    return sorted(check['name'] for check in result['checks'] if check['ok'] is ok)


def test_a_closed_proxy_passes_every_check_with_zero_upstream_connections_to_the_product():
    with world() as (public, product, directory):
        result = surface_probe.probe('http://127.0.0.1:%d' % public.server_port, client_ip='203.0.113.7')
        assert result['verdict'] == 'closed_and_cookie_free', result
        assert names(result, False) == [] and names(result, None) == []
        assert {check['name'] for check in result['checks']} >= {
            'POST /api/v1/inference', 'GET /api/v1/bootstrap', 'GET /api/v1/product/export', 'GET /anything',
            'no Set-Cookie on /swarms/', 'no Set-Cookie on /swarms/api/v1/config'}
        assert product.requests == []
        assert [r['path'] for r in directory.requests] == ['/swarms/', '/swarms/api/v1/config']


def test_a_proxy_that_still_serves_the_product_routes_fails_and_names_them():
    with world(enable_product_routes=True) as (public, product, _directory):
        result = surface_probe.probe('http://127.0.0.1:%d' % public.server_port, client_ip='203.0.113.7')
        assert result['verdict'] == 'FAIL'
        assert 'POST /api/v1/inference' in names(result, False) and 'GET /api/v1/bootstrap' in names(result, False)


class Response(io.BytesIO):
    def __init__(self, status, headers):
        super().__init__(b'{}')
        self.status, self.headers = status, headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake(status_for, cookie_on=()):
    def opener(request, timeout):
        path = '/' + request.full_url.split('://', 1)[1].split('/', 1)[1]
        status = status_for(path, request.get_method())
        headers = {'set-cookie': '__cf_bm=x; Path=/'} if path in cookie_on else {}
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, 'x', headers, io.BytesIO(b'{}'))
        return Response(status, headers)
    return opener


def test_a_set_cookie_on_the_page_fails_whatever_the_case_of_the_header():
    opener = fake(lambda path, method: 200 if path.startswith('/swarms') else 404, cookie_on=('/swarms/',))
    result = surface_probe.probe('https://mycelium.now', opener=opener)
    assert result['verdict'] == 'FAIL' and names(result, False) == ['no Set-Cookie on /swarms/']


def test_anything_but_404_on_a_product_route_fails_even_a_403_or_a_200():
    for wrong in (200, 403, 500):
        opener = fake(lambda path, method, wrong=wrong: wrong if path == '/api/v1/bootstrap' else
                      (200 if path.startswith('/swarms') else 404))
        result = surface_probe.probe('https://mycelium.now', opener=opener)
        assert result['verdict'] == 'FAIL' and names(result, False) == ['GET /api/v1/bootstrap'], wrong


def test_an_unreachable_host_is_unknown_and_unhealthy_never_a_pass():
    def opener(request, timeout):
        raise OSError('down')
    result = surface_probe.probe('https://mycelium.now', opener=opener)
    assert result['verdict'] == 'FAIL' and names(result, True) == [] and names(result, None)


def test_cli_prints_one_json_line_and_exits_by_verdict(capsys):
    good = fake(lambda path, method: 200 if path.startswith('/swarms') else 404)
    assert surface_probe.main(['--base', 'https://mycelium.now'], opener=good) == 0
    assert json.loads(capsys.readouterr().out)['verdict'] == 'closed_and_cookie_free'
    bad = fake(lambda path, method: 200)
    assert surface_probe.main(['--base', 'https://mycelium.now'], opener=bad) == 1
    assert json.loads(capsys.readouterr().out)['verdict'] == 'FAIL'


@pytest.mark.parametrize('base', ['http://mycelium.now', 'ftp://x', 'https://u:p@mycelium.now', 'https://x/path',
                                  'mycelium.now', 'http://10.0.0.5:4350'])
def test_cli_refuses_anything_but_an_https_origin_or_a_loopback_one(base):
    with pytest.raises(SystemExit):
        surface_probe.main(['--base', base], opener=fake(lambda p, m: 404))
