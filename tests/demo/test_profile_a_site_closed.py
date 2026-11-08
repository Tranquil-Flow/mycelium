# SPDX-License-Identifier: AGPL-3.0-only
"""PV14: Profile A stays closed after the public-site extras (notice, forget control, richer card).

Profile A: ``mycelium.now/`` serves a static explainer; the directory is a phonebook; there is NO hosted chat at the
apex and no new public API.  The per-swarm chat page (``/chat``) lives on each swarm's own relay origin only.
Run here against loopback stand-ins only (the shipped proxy in front of recording stubs).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import server, surface_probe
from _proxy_world import IP, call, world

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'mycelium_swarm_directory' / 'web'


def test_the_probe_still_passes_and_the_root_still_redirects_to_swarms():
    with world(redirect_root_to_swarms=True) as (public, product, directory):
        result = surface_probe.probe('http://127.0.0.1:%d' % public.server_port, client_ip='203.0.113.7')
        assert result['verdict'] == 'closed_and_cookie_free', result
        status, headers, _raw = call(public.server_port, 'GET', '/', headers=IP)
        assert (status, headers['location']) == (302, '/swarms/')
        assert product.requests == []


@pytest.mark.parametrize('method,path', [('GET', '/chat'), ('GET', '/chat/'), ('GET', '/chat/chat.js'),
                                         ('GET', '/chat/chat.css'), ('POST', '/chat/api/state'),
                                         ('POST', '/chat/api/send'), ('POST', '/chat/api/events'),
                                         ('GET', '/swarms/chat'), ('GET', '/swarms/chat.js'),
                                         ('GET', '/swarms/notice')])
def test_no_hosted_chat_page_or_chat_api_exists_on_the_public_origin(method, path):
    with world() as (public, product, directory):
        status, headers, _raw = call(public.server_port, method, path, body=b'{}' if method == 'POST' else b'',
                                     headers={**IP, 'Content-Type': 'application/json', 'X-Mycelium-Chat': '1'})
        assert status in (403, 404), (path, status)
        assert 'set-cookie' not in headers
        assert product.requests == []
        # whatever reached the directory upstream is only a closed API route it answers 404 itself
        assert not [r for r in directory.requests if r['path'].startswith('/chat')]


def test_the_proxy_serves_exactly_the_named_directory_assets_and_no_inference_route():
    assets = proxy._DIRECTORY_ASSET_PATHS
    assert assets == {'/', '/apex.js', '/swarms', '/swarms/', '/swarms/swarms.js', '/swarms/swarms.css', '/swarms/site.css', '/swarms/design.css', '/swarms/theme.js', '/swarms/design.js', '/swarms/mycelium-geometry.js', '/swarms/mycelium-hero.js', '/swarms/mycelium-field.js', '/swarms/spore-print.js'}
    # every file the directory server serves is one of them, and the web folder holds only those files
    assert set(server.ASSETS) == assets
    assert sorted(path.name for path in WEB.iterdir()) == ['apex.html', 'apex.js', 'design.css', 'design.js', 'index.html', 'mycelium-field.js', 'mycelium-geometry.js', 'mycelium-hero.js', 'site.css', 'spore-print.js', 'swarms.css', 'swarms.js', 'theme.js']


def test_the_directory_server_itself_does_not_answer_chat_paths():
    import http.client
    from mycelium_swarm_directory.crypto import verify
    from mycelium_swarm_directory.directory import Directory
    from mycelium_swarm_directory.store import MemoryStore
    running = server.DirectoryServer(Directory(MemoryStore(), verify)).start()
    try:
        for path in ('/chat', '/chat/chat.js', '/chat/api/state', '/swarms/api/v1/chat', '/swarms/notice'):
            connection = http.client.HTTPConnection('127.0.0.1', running.server_port, timeout=3)
            connection.request('GET', path)
            response = connection.getresponse()
            response.read()
            assert response.status == 404, path
            connection.close()
    finally:
        running.close()


def test_the_directory_page_and_notice_name_no_public_inference_endpoint():
    html = (WEB / 'index.html').read_text()
    js = (WEB / 'swarms.js').read_text()
    assert '/api/v1/inference' not in html + js  # prompts only ever go to a swarm's own /swarm-entry/v1 door
    assert json.dumps(sorted(surface_probe.CLOSED_ROUTES)).count('/api/v1/inference') == 1
