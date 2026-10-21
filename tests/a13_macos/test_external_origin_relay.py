# SPDX-License-Identifier: AGPL-3.0-only
"""C5: a swarm relay on an owner-chosen stable https origin (provider ``external``).

The owner points their own front (for example a named Cloudflare tunnel) at
``127.0.0.1:<proxy_port>``.  Mycelium starts only the in-process relay proxy: no
cloudflared, no network.  The same hostname then survives relay restarts, so the swarm
listing and join links need no republish.
"""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from mycelium_a13_macos import public_relay
from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.pairing import PairingError, PairingWindows, parse_join_text
from mycelium_a13_macos.public_relay import SeedRelayProxy, TemporaryHttpsRelay

ORIGIN = 'https://demo.mycelium.now'
WEB = Path(__file__).resolve().parents[2] / 'mycelium_a13_macos' / 'web'
CONSENT = {'peer_visibility': True, 'power': 'battery_allowed', 'storage_bytes': 2_000_000_000,
           'network': 'unmetered_only', 'background': False, 'diagnostics': False, 'thermal': 'stop_on_serious'}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """The external provider must never spawn anything (no cloudflared, no helper)."""
    def boom(*args, **kwargs):
        raise AssertionError('external provider must not spawn a process')
    monkeypatch.setattr(public_relay.subprocess, 'Popen', boom)
    monkeypatch.setattr(public_relay, 'installed_relay_binary', boom)


def external(tmp_path, origin=ORIGIN, port=None, **extra):
    return TemporaryHttpsRelay(tmp_path / 'relay', provider='external', public_origin=origin,
                               proxy_port=port or free_port(), **extra)


# -- the relay ------------------------------------------------------------------------------

@pytest.mark.parametrize('origin', [
    'http://demo.mycelium.now', 'https://user:pw@demo.mycelium.now', 'https://user@demo.mycelium.now',
    'https://demo.mycelium.now/', 'https://demo.mycelium.now/path', 'https://demo.mycelium.now?x=1',
    'https://demo.mycelium.now#f', 'https://demo.mycelium.now:8443', 'https://localhost', 'https://127.0.0.1',
    'https://192.168.1.5', 'https://[::1]', 'https://demo..mycelium.now', 'https://-demo.mycelium.now',
    'https://demo_x.mycelium.now', 'https://nodots', 'https://demo.mycelium.now.', 'https://dé.mycelium.now',
    'https://demo.mycelium.now ', '', None, 7, 'https://' + 'a' * 64 + '.example.org',
])
def test_external_origin_rejects_non_canonical_or_unsafe_values(origin):
    with pytest.raises(ValueError, match='relay_origin_invalid'):
        public_relay.validate_external_origin(origin)


def test_external_origin_accepts_a_plain_https_hostname():
    assert public_relay.validate_external_origin(ORIGIN) == ORIGIN
    assert public_relay.validate_external_origin('https://Friends.Example.ORG') == 'https://friends.example.org'


@pytest.mark.parametrize('port', [0, 80, 1023, 65536, -1, True, '8787', None, 8787.0])
def test_external_proxy_port_must_be_an_unprivileged_integer(tmp_path, port):
    with pytest.raises(ValueError, match='relay_proxy_port_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='external', public_origin=ORIGIN, proxy_port=port)


def test_external_requires_origin_and_port_and_cloudflare_refuses_them(tmp_path):
    with pytest.raises(ValueError, match='relay_origin_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='external', proxy_port=free_port())
    with pytest.raises(ValueError, match='relay_proxy_port_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='external', public_origin=ORIGIN)
    with pytest.raises(ValueError, match='relay_provider_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='cloudflare', public_origin=ORIGIN)
    with pytest.raises(ValueError, match='relay_provider_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='nginx')


def test_external_start_needs_approval_and_binds_nothing_before_it(tmp_path):
    port = free_port()
    relay = external(tmp_path, port=port)
    with pytest.raises(ValueError, match='relay_approval_required'):
        relay.start(acknowledged=False, max_seconds=60)
    assert relay.proxy is None
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))  # still free


def test_external_start_spawns_no_process_and_reports_the_chosen_origin(tmp_path):
    port = free_port()
    relay = external(tmp_path, port=port).start(acknowledged=True, max_seconds=60)
    try:
        status = relay.status()
        assert relay.process is None
        assert status['public_origin'] == ORIGIN and relay.public_origin == ORIGIN
        assert status['state'] == 'relay_process_running' and status['provider'] == 'external'
        assert status['local_forward_target'] == f'http://127.0.0.1:{port}'
        assert status['relay_host_trust'] == 'public_webpki' and status['error_code'] is None
        assert 'your own address' in status['privacy'].lower()
        assert relay.proxy.origin == f'http://127.0.0.1:{port}'
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    finally:
        relay.close()
    assert relay.status()['state'] == 'stopped' and relay.status()['public_origin'] == ORIGIN


def test_external_relay_keeps_the_same_port_and_origin_across_restarts(tmp_path):
    port = free_port()
    seen = []
    for _ in range(2):
        relay = external(tmp_path, port=port).start(acknowledged=True, max_seconds=60)
        seen.append((relay.public_origin, relay.status()['local_forward_target']))
        relay.close()
    assert seen[0] == seen[1] == (ORIGIN, f'http://127.0.0.1:{port}')


def test_external_port_in_use_is_a_bounded_error_and_leaves_nothing_running(tmp_path):
    with socket.socket() as holder:
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        relay = external(tmp_path, port=holder.getsockname()[1])
        with pytest.raises(ValueError, match='relay_proxy_port_unavailable'):
            relay.start(acknowledged=True, max_seconds=60)
        assert relay.proxy is None


def test_cloudflare_status_is_unchanged_by_the_new_provider(tmp_path):
    status = TemporaryHttpsRelay(tmp_path / 'r').status()
    assert set(status) == {'state', 'provider', 'public_origin', 'error_code', 'seed_bound', 'privacy', 'relay_host_trust'}
    assert status['provider'] == 'cloudflare'


# -- the hardened public routes behave the same behind the external origin ----------------------

def test_external_front_that_is_not_cloudflare_cannot_choose_its_rate_limit_identity(tmp_path):
    got = []
    relay = external(tmp_path).start(acknowledged=True, max_seconds=60)
    try:
        relay.proxy.bind_chat(lambda method, path, headers, body: (got.append(headers) or (200, 'application/json', b'{}')))
        req = Request(relay.proxy.origin + '/chat/api/state', data=b'{}', method='POST',
                      headers={'Cf-Connecting-Ip': '203.0.113.9', 'Content-Type': 'application/json'})
        with urlopen(req, timeout=3) as response:
            assert response.headers['Content-Security-Policy'].startswith("default-src 'self'")
    finally:
        relay.close()
    assert got and not any(k.lower() == 'cf-connecting-ip' for k in got[0])


def test_cloudflare_named_tunnel_front_keeps_the_visitor_address_header(tmp_path):
    got = []
    relay = external(tmp_path, cloudflare_front=True).start(acknowledged=True, max_seconds=60)
    try:
        relay.proxy.bind_chat(lambda method, path, headers, body: (got.append(headers) or (200, 'application/json', b'{}')))
        req = Request(relay.proxy.origin + '/chat/api/state', data=b'{}', method='POST',
                      headers={'Cf-Connecting-Ip': '203.0.113.9', 'Content-Type': 'application/json'})
        urlopen(req, timeout=3).read()
    finally:
        relay.close()
    assert got[0].get('Cf-Connecting-Ip') == '203.0.113.9'


def test_default_proxy_still_trusts_the_header_for_quick_tunnels():
    proxy = SeedRelayProxy()
    try:
        assert proxy.server.trust_client_ip is True
    finally:
        proxy.close()


# -- join links and the typed code ---------------------------------------------------------------

def test_join_link_carries_the_stable_origin_and_typed_form_stays_trycloudflare_only():
    clock = lambda: 1_800_000_000.0  # noqa: E731
    windows = PairingWindows(clock=clock)
    view = windows.open(origin=ORIGIN, seed_key_digest='sha256:' + 'ab' * 32, ttl_seconds=600)
    assert view['tunnel_name'] == 'demo.mycelium.now'
    parsed = parse_join_text(view['join_link'], now=clock())
    assert parsed['origin'] == ORIGIN and parsed['seed_key_digest'] == 'sha256:' + 'ab' * 32
    for typed in (f"demo.mycelium.now {view['short_code'].split('-', 1)[1]}", f"https://demo.mycelium.now ABCD-EFGH-JKLM",
                  "friends.example.org ABCD-EFGH-JKLM"):
        with pytest.raises(PairingError) as caught:
            parse_join_text(typed, now=clock())
        assert caught.value.code == 'join_code_invalid'


def test_trycloudflare_tunnel_name_view_is_unchanged():
    windows = PairingWindows(clock=lambda: 1_800_000_000.0)
    view = windows.open(origin='https://quiet-river.trycloudflare.com', seed_key_digest='sha256:' + 'ab' * 32, ttl_seconds=600)
    assert view['tunnel_name'] == 'quiet-river'


# -- the app ----------------------------------------------------------------------------------------

def start_body(port, **extra):
    return {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external', 'public_origin': ORIGIN,
            'proxy_port': port, **extra}


@pytest.fixture
def app(tmp_path):
    instance = DemoApp(tmp_path / 'app', check_platform=False).start()
    yield instance
    instance.close()


@pytest.mark.parametrize('mutate', [
    lambda b: {**b, 'relay_acknowledged': False},
    lambda b: {k: v for k, v in b.items() if k != 'relay_acknowledged'},
    lambda b: {k: v for k, v in b.items() if k != 'public_origin'},
    lambda b: {k: v for k, v in b.items() if k != 'proxy_port'},
    lambda b: {**b, 'extra': 1},
    lambda b: {**b, 'max_seconds': 5},
])
def test_app_external_start_without_consent_or_with_malformed_body_is_refused(app, mutate):
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', mutate(start_body(free_port())))
    assert str(caught.value) == 'network_consent_required'
    assert app.public_relay is None


def test_app_cloudflare_start_cannot_carry_an_origin(app):
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', {'relay_acknowledged': True, 'max_seconds': 1800, 'public_origin': ORIGIN})
    assert str(caught.value) == 'network_consent_required'


@pytest.mark.parametrize('origin,code', [('http://demo.mycelium.now', 'relay_origin_invalid'),
                                          ('https://u:p@demo.mycelium.now', 'relay_origin_invalid'),
                                          ('https://10.0.0.5', 'relay_origin_invalid')])
def test_app_external_start_rejects_bad_origins_with_a_specific_code(app, origin, code):
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', start_body(free_port(), public_origin=origin))
    assert str(caught.value) == code
    assert app.public_relay is None


def test_app_external_relay_before_create_makes_the_swarm_live_on_the_stable_origin(app):
    port = free_port()
    value = app.action('/api/network/start', start_body(port))
    assert value['public_origin'] == ORIGIN and value['local_forward_target'] == f'http://127.0.0.1:{port}'
    app.action('/api/create', {'consent': CONSENT})
    assert app.authority.coordinator.seed_url == ORIGIN
    assert app.authority.status()['membership_state'] == 'current'
    opened = app.action('/api/pair/window/open', {'ttl_seconds': 600})
    assert opened['tunnel_name'] == 'demo.mycelium.now'
    assert parse_join_text(opened['join_link'], now=__import__('time').time())['origin'] == ORIGIN
    with urlopen(app.public_relay.proxy.origin + '/seed/identity', timeout=3) as response:
        assert json.load(response)['statement']['seed_url'] == ORIGIN


def test_app_external_relay_binds_the_same_hardened_public_routes(app, monkeypatch):
    from mycelium_a13_macos import chat
    calls = []
    monkeypatch.setattr(chat, 'handle', lambda a, method, path, headers, body, *, via_relay: (
        calls.append(via_relay) or (200, 'application/json', b'{}')))
    app.action('/api/network/start', start_body(free_port()))
    proxy = app.public_relay.proxy
    assert proxy.server.local is not None and proxy.server.chat is not None and proxy.server.swarm is not None
    req = Request(proxy.origin + '/chat/api/state', data=b'{}', method='POST', headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=3) as response:
        assert 'frame-ancestors' in response.headers['Content-Security-Policy']
    assert calls == [True]


def test_app_external_restart_after_the_budget_keeps_the_address_and_needs_no_recovery_card(app):
    port = free_port()
    app.action('/api/network/start', start_body(port))
    app.action('/api/create', {'consent': CONSENT})
    swarm = app.authority.coordinator.swarm_id
    app.public_relay.close()
    app.public_relay = None  # the bounded budget ran out
    value = app.action('/api/network/restart', {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external'})
    assert value['network']['public_origin'] == ORIGIN
    assert value['network']['local_forward_target'] == f'http://127.0.0.1:{port}'
    assert value['recovery_card'] is None
    assert app.authority.coordinator.seed_url == ORIGIN and app.authority.coordinator.swarm_id == swarm
    with urlopen(app.public_relay.proxy.origin + '/seed/identity', timeout=3) as response:
        assert response.status == 200


def test_app_external_restart_refuses_without_a_saved_address(app):
    app.action('/api/create', {'consent': CONSENT})
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/restart', {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external'})
    assert str(caught.value) == 'relay_origin_invalid'


def test_app_relaunch_with_the_same_external_address_keeps_swarm_and_origin(tmp_path):
    root = tmp_path / 'app'
    port = free_port()
    first = DemoApp(root, check_platform=False).start()
    try:
        first.action('/api/network/start', start_body(port))
        first.action('/api/create', {'consent': CONSENT})
        swarm = first.authority.coordinator.swarm_id
    finally:
        first.close()
    second = DemoApp(root, check_platform=False).start()
    try:
        value = second.action('/api/network/start', start_body(port))
        assert value['public_origin'] == ORIGIN
        assert second.authority.coordinator.swarm_id == swarm
        assert second.authority.coordinator.seed_url == ORIGIN
        with urlopen(second.public_relay.proxy.origin + '/seed/identity', timeout=3) as response:
            assert response.status == 200
    finally:
        second.close()


# -- the listing survives relay restarts -----------------------------------------------------------

def test_published_listing_needs_no_republish_when_the_external_relay_restarts(tmp_path):
    from mycelium_a13_macos.swarm_publish import SwarmPublisher
    from mycelium_swarm_directory.crypto import verify
    from mycelium_swarm_directory.directory import Directory
    from mycelium_swarm_directory.store import MemoryStore

    DIRECTORY = 'https://swarms.example.org'

    class Run:
        def status(self):
            return {'state': 'serving', 'route_ready': True, 'registry_origin': 'http://127.0.0.1:5',
                    'participants': [{'node_id': 'a'}, {'node_id': 'b'}]}

    class App:
        installation_role = 'owner'

        def __init__(self, root):
            self.root, self.owner_run = root, Run()

        def run_model_contract(self):
            return type('M', (), {'model_id': 'Qwen/Qwen3-0.6B'})()

    class InProcessClient:
        directory = None

        def __init__(self, origin):
            assert origin == DIRECTORY

        def get(self, path):
            return self._call('GET', path, b'')

        def post(self, path, document):
            return self._call('POST', path, json.dumps(document).encode())

        def _call(self, method, path, raw):
            status, value = self.directory.handle(method, path, raw, 'ip:1')
            assert status == 200, value
            return value

    InProcessClient.directory = directory = Directory(MemoryStore(), verify)
    port = free_port()
    holder = {'relay': external(tmp_path / 'one', port=port).start(acknowledged=True, max_seconds=60)}
    app = App(tmp_path)
    publisher = SwarmPublisher(app, origin=lambda: holder['relay'].status()['public_origin'] if holder['relay'] else None,
                               client_factory=InProcessClient, chat=lambda: None)
    publisher.publish({'directory': DIRECTORY, 'name': 'Demo', 'access_mode': 'request'})
    record = dict(publisher.state['record'])
    assert directory.list_swarms()[0]['entry_origin'] == ORIGIN and record['entry']['origin'] == ORIGIN
    holder['relay'].close()
    holder['relay'] = None
    publisher.tick()  # relay down: the listing is untouched
    holder['relay'] = external(tmp_path / 'two', port=port).start(acknowledged=True, max_seconds=60)
    try:
        publisher.tick()
    finally:
        holder['relay'].close()
    assert publisher.state['record'] == record  # same signed record, same seq: nothing re-signed or re-posted
    assert directory.list_swarms()[0]['entry_origin'] == ORIGIN


# -- the page ----------------------------------------------------------------------------------------

def test_page_offers_use_my_own_address_with_origin_port_and_an_honest_explainer():
    html, script = (WEB / 'index.html').read_text(), (WEB / 'app.js').read_text()
    for needle in ('id="own-address"', 'id="own-origin"', 'id="own-port"', 'id="own-cloudflare"', 'id="own-start"',
                   'Use my own address'):
        assert needle in html
    assert 'point this hostname at this Mac yourself' in html and 'named tunnel' in html
    assert "provider:'external'" in script and 'public_origin' in script and 'proxy_port' in script
    for code in ('relay_proxy_port_invalid', 'relay_proxy_port_unavailable'):
        assert code in script
