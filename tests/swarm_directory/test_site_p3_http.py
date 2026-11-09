# SPDX-License-Identifier: AGPL-3.0-only
import http.client
import json
from dataclasses import replace
import pytest
from test_entry_guard import world, send, grant_for
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import MemoryStore
from mycelium_swarm_directory.wire import WireError


def test_apex_explains_without_identity_or_chat_surface():
    running = DirectoryServer(Directory(MemoryStore(), verify)).start()
    try:
        c = http.client.HTTPConnection('127.0.0.1', running.server_port)
        c.request('GET', '/')
        r = c.getresponse(); body = r.read().decode()
        assert r.status == 200
        assert 'AI for everyone, controlled by no one.' in body and 'This is the first step' in body
        assert 'href="https://demo.mycelium.now/chat"' not in body
        assert 'Demo offline' in body and 'Start your own swarm</a>' in body
        assert 'id="demo-chat" class="btn" href="/swarms/#new"' in body
        assert '<a href="/swarms/#contributing">Download the app</a> and connect at least two devices to run your own swarm.' in body
        assert 'A swarm needs at least two devices.' in body
        assert 'A one-Mac route runs the model on that Mac alone.' not in body
        assert 'A one-device route runs the model on that device alone.' not in body
        assert 'The fast demo route' not in body
        assert 'apex.js' in body and 'swarms.js' not in body
        assert not r.getheader('Set-Cookie')
        c.close()
    finally:
        running.close()


def test_apex_proxy_composes_exact_static_assets_and_keeps_prompt_routes_closed():
    import threading
    from mycelium_demo.public_origin_proxy import ProxyConfig, PublicOriginProxyServer
    from mycelium_swarm_directory.server import ASSETS, WEB
    d=DirectoryServer(Directory(MemoryStore(),verify)).start()
    config=ProxyConfig(listen_host='127.0.0.1',listen_port=4350,upstream_host='127.0.0.1',upstream_port=1,
                       public_host='mycelium.now',capability='a'*64,directory_host='127.0.0.1',directory_port=d.server_port)
    proxy=PublicOriginProxyServer(('127.0.0.1',0),config)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
    try:
        for path in ('/','/apex.js','/swarms/site.css','/vision','/swarms/vision.css','/swarms/vision.js'):
            c=http.client.HTTPConnection('127.0.0.1',proxy.server_port)
            c.request('GET',path,headers={'Cf-Connecting-Ip':'203.0.113.7'})
            response=c.getresponse();body=response.read()
            assert response.status==200 and body==(WEB/ASSETS[path][0]).read_bytes()
            assert response.getheader('Set-Cookie') is None
            assert "script-src 'self'" in (response.getheader('Content-Security-Policy') or '')
            c.close()
        for method,path in [('POST','/'),('POST','/apex.js'),('POST','/chat/api/send'),('POST','/api/v1/inference'),('GET','/apex.html'),('GET','/?tracking=x'),('POST','/vision'),('GET','/vision.html'),('GET','/vision/'),('GET','/vision?mode=technical')]:
            c=http.client.HTTPConnection('127.0.0.1',proxy.server_port)
            c.request(method,path,body=b'{}' if method=='POST' else None,headers={'Cf-Connecting-Ip':'203.0.113.7','Content-Type':'application/json'})
            response=c.getresponse();response.read()
            assert response.status in (403,404)
            c.close()
    finally:
        proxy.shutdown();proxy.server_close();thread.join(5);d.close()


def test_entry_rejects_unregistered_model_before_quota_or_backend(world):
    visitor = Ed25519Signer.generate()
    grant = grant_for(world, visitor)
    world['policy']['value'] = replace(world['policy']['value'], model='Unknown/typo')
    assert send(world, visitor, grant) == (400, {'error': 'model_not_registered'})
    assert world['chat'].sent == []
    assert world['ledger'].usage(grant['grant_id']) == (0, 0)
    world['policy']['value'] = replace(world['policy']['value'], access_mode='open')
    with pytest.raises(WireError, match='model_not_registered'):
        world['guard'].admit_relay_chat('ip:1', 16)
