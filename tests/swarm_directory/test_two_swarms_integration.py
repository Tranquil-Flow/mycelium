# SPDX-License-Identifier: AGPL-3.0-only
"""LOCAL integration: two in-process swarms behind the real relay, entry hook and chat service.

What is real here: the directory HTTP server (sqlite), the A13 relay proxy
(``public_relay.SeedRelayProxy``), the swarm-entry hook, the publisher, the
entry guard, ``chat.ChatService`` and its ``RegistryClient`` (bootstrap ->
qualification -> POST inference -> SSE).  What is FAKE (labelled): the product
gateway behind each swarm is a small HTTP server speaking the registry API
contract with canned tokens instead of a real distributed model route; the
owner run is a stub reporting ``serving``/``route_ready``.  Browsers are
simulated by Python Ed25519 signers sending the same signed documents as
``web/swarms.js``.
"""
from __future__ import annotations

import json
import time

import pytest

from mycelium_a13_macos import swarm_entry
from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_a13_macos.swarm_publish import SwarmPublisher
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory, Limits
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore

from _swarm_fixtures import FakeApp, fake_gateway, request


def directory_post(world, path, document):
    status, value, _ = request(world['directory'].origin, '/swarms/api/v1' + path, document,
                               headers={'X-Mycelium-Swarms': '1'})
    return status, value


def entry_post(proxy, route, document, ip):
    status, value, headers = request(proxy.origin, '/swarm-entry/v1/' + route, document,
                                     headers={'Cf-Connecting-Ip': ip})
    return status, value


def chat(proxy, signer, swarm_id, grant, prompt, ip, max_new_tokens=16):
    send = docs.entry_send(signer, swarm_id, grant, prompt, max_new_tokens, at=int(time.time()))
    status, accepted = entry_post(proxy, 'send', send, ip)
    if status != 200:
        return status, accepted
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, page = entry_post(proxy, 'events', {'request_id': accepted['request_id'], 'after': 0}, ip)
        assert status == 200
        if page['done']:
            return 200, {'text': ''.join(page['tokens']), 'error': page['error'], 'quota': accepted['quota']}
        time.sleep(0.05)
    raise AssertionError('chat did not finish')


@pytest.fixture(scope='module')
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp('swarms')
    directory = DirectoryServer(Directory(SqliteStore(root / 'directory.sqlite3'), verify,
                                          limits=Limits(writes_per_ip_per_minute=500, writes_per_key_per_minute=100),
                                          allow_loopback_entries=True)).start()
    cleanup = [directory.close]
    swarms = {}
    for name, tokens in (('a', ['Ber', 'lin', '.']), ('b', ['Pa', 'ris', '.'])):
        gateway, handler = fake_gateway(tokens)
        cleanup.append(gateway.shutdown)
        (root / name).mkdir()
        app = FakeApp(root / name, 'http://127.0.0.1:%d' % gateway.server_port)
        proxy = SeedRelayProxy().start()
        cleanup.append(proxy.close)
        publisher = SwarmPublisher(app, origin=lambda proxy=proxy: proxy.origin)
        app.swarm_publisher = publisher
        swarm_entry.bind(app, proxy)
        swarms[name] = {'app': app, 'proxy': proxy, 'publisher': publisher, 'backend': handler}
    # Swarm A: created in the A13 app (app key = owner + entry), request access.
    swarms['a']['publisher'].publish({'directory': directory.origin, 'name': 'Coworking Macs',
                                      'access_mode': 'request'})
    # Swarm B: created in a browser (owner key elsewhere), open; the Mac attaches by link.
    browser_owner = Ed25519Signer.generate()
    b_id = docs.new_swarm_id()
    status, _ = request(directory.origin, '/swarms/api/v1/records',
                        docs.swarm_record(browser_owner, swarm_id=b_id, seq=1, name='Open Berlin',
                                          model='Qwen/Qwen3-0.6B', access_mode='open',
                                          open_quota={'max_requests': 3, 'max_tokens': 100, 'ttl_seconds': 3600}),
                        headers={'X-Mycelium-Swarms': '1'})[:2]
    assert status == 200
    swarms['b']['owner'] = browser_owner
    swarms['b']['id'] = b_id
    world = {'directory': directory, **swarms}
    yield world
    for publisher in (swarms['a']['publisher'], swarms['b']['publisher']):
        publisher.close()
    for function in reversed(cleanup):
        function()


def listing(world):
    status, value, _ = request(world['directory'].origin, '/swarms/api/v1/swarms')
    assert status == 200
    return {row['swarm_id']: row for row in value['swarms']}


def _step_01_web_created_swarm_attaches_the_mac_by_owner_signature(world):
    b = world['b']
    link = docs.swarm_link(world['directory'].origin, b['id'], b['owner'].public_key)
    state = b['publisher'].publish({'link': link})
    assert state['attached'] is False  # the entry waits for the owner's signature
    # unattached: the entry refuses everything even though it is reachable
    assert entry_post(b['proxy'], 'state', {}, 'ip:9')[0] == 503
    inbox = directory_post(world, '/owner-inbox', docs.owner_query(b['owner'], b['id'], at=int(time.time())))[1]
    offer, = inbox['entry_offers']
    assert offer['entry_fingerprint'] == state['entry_fingerprint']  # what both screens show
    record = docs.swarm_record(b['owner'], swarm_id=b['id'], seq=2, name='Open Berlin', model='Qwen/Qwen3-0.6B',
                               access_mode='open', open_quota={'max_requests': 3, 'max_tokens': 100,
                                                               'ttl_seconds': 3600},
                               entry={'key': offer['entry_key'], 'origin': offer['origin']})
    assert directory_post(world, '/records', record)[0] == 200
    assert b['publisher'].tick()['attached'] is True


def _step_02_every_swarm_is_listed_with_live_status(world):
    rows = listing(world)
    a_id = world['a']['publisher'].state['swarm_id']
    assert set(rows) == {a_id, world['b']['id']}
    assert rows[a_id]['status'] == 'serving' and rows[a_id]['members'] == 2
    assert rows[a_id]['access_mode'] == 'request' and rows[a_id]['entry_origin'] == world['a']['proxy'].origin
    assert rows[world['b']['id']]['status'] == 'serving' and rows[world['b']['id']]['access_mode'] == 'open'
    assert all('seen by this swarm' in row['privacy'] for row in rows.values())


def _step_03_request_access_owner_approves_in_app_and_chat_returns_tokens(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    carol = Ed25519Signer.generate()
    now = int(time.time())
    assert directory_post(world, '/access-requests', docs.access_request(carol, a_id, note='visitor', at=now))[0] == 200
    inbox = a['publisher'].inbox({})
    assert [r['requester_key'] for r in inbox['requests'] if r['state'] == 'pending'] == [carol.public_key]
    a['publisher'].approve({'requester_key': carol.public_key, 'hours': 1, 'max_requests': 5, 'max_tokens': 200})
    mine = directory_post(world, '/my-grants', docs.grantee_query(carol, at=now))[1]
    grant, = mine['grants']
    status, answer = chat(a['proxy'], carol, a_id, grant, 'Capital of Germany?', 'ip:10')
    assert status == 200 and answer == {'text': 'Berlin.', 'error': None,
                                        'quota': {'requests_left': 4, 'tokens_left': 184}}
    assert a['backend'].inference_posts[-1]['prompt'] == 'Capital of Germany?'


def _step_04_denied_request_is_reported_and_has_no_grant(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    dave = Ed25519Signer.generate()
    now = int(time.time())
    directory_post(world, '/access-requests', docs.access_request(dave, a_id, note='', at=now))
    a['publisher'].deny({'requester_key': dave.public_key})
    mine = directory_post(world, '/my-grants', docs.grantee_query(dave, at=now))[1]
    assert mine['grants'] == [] and mine['denied'] == [a_id]


def _step_05_without_a_grant_the_entry_refuses_even_if_the_directory_is_bypassed(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    mallory = Ed25519Signer.generate()
    before = len(a['backend'].inference_posts)
    now = int(time.time())
    self_signed = docs.grant(mallory, swarm_id=a_id, grantee_key=mallory.public_key, issued_at=now,
                             expires_at=now + 3600, max_requests=99, max_tokens=9999)
    assert chat(a['proxy'], mallory, a_id, self_signed, 'hi', 'ip:66') == \
        (403, {'error': 'entry_grant_issuer_invalid'})
    assert chat(a['proxy'], mallory, a_id, {}, 'hi', 'ip:66') == (403, {'error': 'entry_grant_invalid'})
    unsigned = {k: v for k, v in docs.entry_send(mallory, a_id, self_signed, 'hi', 8, at=now).items() if k != 'sig'}
    assert entry_post(a['proxy'], 'send', unsigned, 'ip:66')[0] == 400
    assert len(a['backend'].inference_posts) == before  # nothing reached the model


def _step_06_forged_and_expired_grants_are_refused_at_the_entry(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    erin = Ed25519Signer.generate()
    now = int(time.time())
    real = docs.grant(a['publisher'].signer, swarm_id=a_id, grantee_key=erin.public_key, issued_at=now,
                      expires_at=now + 3600, max_requests=1, max_tokens=50)
    forged = dict(real, max_requests=1000)
    assert chat(a['proxy'], erin, a_id, forged, 'hi', 'ip:67') == (403, {'error': 'entry_grant_signature_invalid'})
    expired = docs.grant(a['publisher'].signer, swarm_id=a_id, grantee_key=erin.public_key, issued_at=now - 7200,
                         expires_at=now - 60, max_requests=1, max_tokens=50)
    assert chat(a['proxy'], erin, a_id, expired, 'hi', 'ip:67') == (403, {'error': 'entry_grant_expired'})


def _step_07_quota_exhausted_is_refused(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    finn = Ed25519Signer.generate()
    now = int(time.time())
    directory_post(world, '/access-requests', docs.access_request(finn, a_id, note='', at=now))
    a['publisher'].approve({'requester_key': finn.public_key, 'hours': 1, 'max_requests': 1, 'max_tokens': 50})
    grant, = directory_post(world, '/my-grants', docs.grantee_query(finn, at=now))[1]['grants']
    assert chat(a['proxy'], finn, a_id, grant, 'one', 'ip:68')[1]['text'] == 'Berlin.'
    assert chat(a['proxy'], finn, a_id, grant, 'two', 'ip:68') == (429, {'error': 'entry_quota_exhausted'})


def _step_08_open_swarm_auto_grant_chat_and_grants_do_not_cross_swarms(world):
    a, b = world['a'], world['b']
    gina = Ed25519Signer.generate()
    now = int(time.time())
    status, value = entry_post(b['proxy'], 'open-grant', docs.open_grant_request(gina, b['id'], at=now), 'ip:69')
    assert status == 200
    grant = value['grant']
    assert chat(b['proxy'], gina, b['id'], grant, 'Capital of France?', 'ip:69')[1]['text'] == 'Paris.'
    # B's grant is worthless at A (other owner, other swarm), and A's open-grant route is closed
    assert chat(a['proxy'], gina, a['publisher'].state['swarm_id'], grant, 'x', 'ip:69')[0] == 403
    status, value = entry_post(a['proxy'], 'open-grant',
                               docs.open_grant_request(gina, a['publisher'].state['swarm_id'], at=now), 'ip:69')
    assert (status, value) == (403, {'error': 'entry_swarm_not_open'})


def _step_09_relay_answers_the_cors_preflight_only_for_the_entry(world):
    proxy = world['a']['proxy']
    status, _, headers = request(proxy.origin, '/swarm-entry/v1/send', method='OPTIONS',
                                 headers={'Origin': 'https://swarms.example', 'Access-Control-Request-Method': 'POST'})
    assert status == 204 and headers['Access-Control-Allow-Origin'] == '*'
    assert 'Access-Control-Allow-Credentials' not in headers
    assert request(proxy.origin, '/seed/x', method='OPTIONS')[0] in (404, 501)
    status, state, headers = request(proxy.origin, '/swarm-entry/v1/state', {}, headers={})
    assert status == 200 and state['available'] is True and headers['Access-Control-Allow-Origin'] == '*'


def _step_10_status_follows_the_owner_run_and_unpublish_goes_offline(world):
    a = world['a']
    a_id = a['publisher'].state['swarm_id']
    a['app'].owner_run.state = 'running'
    assert a['publisher'].heartbeat_now()['error_code'] is None
    assert listing(world)[a_id]['status'] == 'warming'
    a['app'].owner_run.state = 'serving'
    a['publisher'].unpublish({})
    assert listing(world)[a_id]['status'] == 'offline'
    assert entry_post(a['proxy'], 'state', {}, 'ip:70')[0] == 503


def _step_11_refusal_probe_reports_refused_without_grant(world):
    from mycelium_swarm_directory.probe import probe
    b = world['b']  # still published and serving
    before = len(b['backend'].inference_posts)
    result = probe(b['proxy'].origin, b['id'])
    assert result['verdict'] == 'refused_without_grant', result
    assert tuple(result['self_signed_grant']) == (403, 'entry_grant_issuer_invalid')
    assert len(b['backend'].inference_posts) == before


_SCENARIO = (
    _step_01_web_created_swarm_attaches_the_mac_by_owner_signature,
    _step_02_every_swarm_is_listed_with_live_status,
    _step_03_request_access_owner_approves_in_app_and_chat_returns_tokens,
    _step_04_denied_request_is_reported_and_has_no_grant,
    _step_05_without_a_grant_the_entry_refuses_even_if_the_directory_is_bypassed,
    _step_06_forged_and_expired_grants_are_refused_at_the_entry,
    _step_07_quota_exhausted_is_refused,
    _step_08_open_swarm_auto_grant_chat_and_grants_do_not_cross_swarms,
    _step_09_relay_answers_the_cors_preflight_only_for_the_entry,
    _step_10_status_follows_the_owner_run_and_unpublish_goes_offline,
    _step_11_refusal_probe_reports_refused_without_grant,
)


def test_two_swarm_scenario_runs_its_steps_in_order(world):
    """One scenario, one test: the steps share the module world and build on each
    other (step 01 attaches swarm B, step 10 unpublishes), so pytest-xdist must not
    spread them across workers. The failing step is named in the traceback."""
    for step in _SCENARIO:
        step(world)
