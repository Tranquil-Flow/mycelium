# SPDX-License-Identifier: AGPL-3.0-only
"""Real loopback relay responses, with only the upstream connection injected."""
import http.client
import json
from urllib.request import build_opener, ProxyHandler

import pytest

from mycelium_a13_macos import public_relay, managed_control


@pytest.fixture
def proxy(monkeypatch):
    class BrokenBackend(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): raise OSError('PRIVATE backend detail')
        def close(self): pass
    monkeypatch.setattr(public_relay.http.client, 'HTTPSConnection', BrokenBackend)
    p = public_relay.SeedRelayProxy().start()
    try:
        yield p
    finally:
        p.close()


def failing_handler(*args):
    raise ValueError('PRIVATE handler detail')


@pytest.mark.parametrize('path,method,headers,setup,status,code', [
    ('/unknown', 'POST', {}, None, 404, 'relay_route_unknown'),
    ('/unknown', 'OPTIONS', {}, None, 404, 'relay_route_unknown'),
    ('/managed-control/poll', 'POST', {'Cookie': 'PRIVATE'}, None, 400, 'relay_headers_invalid'),
    ('/managed-control/poll', 'POST', {'Content-Length': '-1'}, None, 400, 'relay_frame_invalid'),
    ('/managed-control/poll', 'POST', {}, None, 503, 'relay_seed_not_bound'),
    ('/managed-control/staging/read', 'POST', {}, 'backend', 502, 'relay_backend_unavailable'),
    ('/member-report/v1', 'POST', {'Cookie': 'PRIVATE'}, None, 400, 'relay_headers_invalid'),
    ('/member-report/v1', 'POST', {'Content-Length': '-1'}, None, 400, 'relay_frame_invalid'),
    ('/member-report/v1', 'POST', {}, None, 503, 'relay_join_not_bound'),
    ('/member-report/v1', 'POST', {}, 'local', 500, 'relay_join_failed'),
    ('/chat', 'POST', {}, None, 503, 'relay_chat_not_bound'),
    ('/chat', 'POST', {}, 'chat', 500, 'relay_chat_failed'),
    ('/swarm-entry/v1/open', 'POST', {}, None, 503, 'relay_swarm_entry_not_bound'),
    ('/swarm-entry/v1/open', 'POST', {}, 'swarm', 500, 'relay_swarm_entry_failed'),
])
def test_relay_own_errors_are_canonical_json(proxy, path, method, headers, setup, status, code):
    if setup == 'backend':
        proxy.server.backend = (1, None)
    elif setup:
        setattr(proxy.server, setup, failing_handler)
    connection = http.client.HTTPConnection('127.0.0.1', proxy.server.server_port, timeout=2)
    try:
        connection.request(method, path, body=b'', headers=headers)
        response = connection.getresponse()
        raw = response.read()
        assert response.status == status
        assert response.getheader('Content-Type') == 'application/json'
        assert raw == json.dumps({'error': {'code': code}}, sort_keys=True, separators=(',', ':')).encode()
        assert response.getheader('Cache-Control') == 'no-store'
        if path.startswith('/swarm-entry/'):
            assert response.getheader('Access-Control-Allow-Origin') == '*'
    finally:
        connection.close()


def test_real_proxy_failure_surfaces_in_client_without_retry(proxy, monkeypatch):
    proxy.server.backend = (1, None)
    client = object.__new__(managed_control.ManagedControlClient)
    client.base_url = proxy.origin
    client.timeout = 2
    client._opener = build_opener(ProxyHandler({}))
    monkeypatch.setattr(client, '_read_response', lambda response: response.read())
    original = client._opener.open
    calls = []
    def open_(request, **kwargs):
        calls.append(request.full_url)
        return original(request, **kwargs)
    monkeypatch.setattr(client._opener, 'open', open_)
    with pytest.raises(managed_control.ManagedControlError) as caught:
        client._request('/managed-control/staging/read', {'signed': 'fixture'})
    assert caught.value.code == 'relay_backend_unavailable'
    assert caught.value.status == 502
    assert caught.value.request_path == '/managed-control/staging/read'
    assert len(calls) == 1
