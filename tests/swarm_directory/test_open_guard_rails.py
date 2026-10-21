# SPDX-License-Identifier: AGPL-3.0-only
"""Open-mode guard rails on the swarm entry: a swarm-wide daily budget and an owner kill switch.

FAKE (labelled): the chat backend (``FakeChat``/``RunningChat``) is a duck-typed stand-in; the guard, ledger,
gateway and publisher are the shipped code.
"""
import json

import pytest

from mycelium_a13_macos.swarm_publish import SwarmPublisher, SwarmPublishError, action
from mycelium_swarm_directory import docs, hosted_defaults
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryLimits, EntryPolicy, UsageLedger
from mycelium_swarm_directory.wire import WireError

from _swarm_fixtures import FakeApp
from test_entry_guard import SWARM, FakeChat, grant_for, send, world  # noqa: F401  (fixture re-export)

DAY = 86400
PREFIX = '/swarm-entry/v1/'


def make_open(world, *, quota=None, **limits):
    owner, entry = world['owner'], world['entry']
    quota = quota or {'max_requests': 50, 'max_tokens': 5000, 'ttl_seconds': 3 * DAY}
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=2, name='Open', model='Qwen/Qwen3-0.6B',
                               access_mode='open', open_quota=quota,
                               entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    world['policy']['value'] = EntryPolicy.from_record(record, verify, entry_key=entry.public_key)
    world['guard'].limits = EntryLimits(per_ip_per_minute=10_000, per_key_per_minute=10_000,
                                        open_grants_per_ip_per_hour=10_000, events_per_ip_per_minute=10_000, **limits)


def visitor(world, n):
    """A fresh key from a fresh address that auto-gets its own open grant."""
    key = Ed25519Signer.generate()
    status, value = world['gateway'].handle(PREFIX + 'open-grant', json.dumps(
        docs.open_grant_request(key, SWARM, at=world['clock'].t)).encode(), 'ip:v%d' % n)
    assert status == 200, value
    return key, value['grant'], 'ip:v%d' % n


def post(world, route, document, client='ip:x'):
    return world['gateway'].handle(PREFIX + route, json.dumps(document).encode(), client)


# -- swarm-wide daily budget --------------------------------------------------------------------

def test_defaults_come_from_the_one_named_module():
    limits = EntryLimits()
    assert (limits.swarm_daily_requests, limits.swarm_daily_tokens) == (200, 40_000)
    assert (limits.swarm_daily_requests, limits.swarm_daily_tokens) == \
        (hosted_defaults.SWARM_DAILY_REQUESTS, hosted_defaults.SWARM_DAILY_TOKENS)


def test_many_distinct_keys_and_ips_cannot_exceed_the_swarm_request_budget(world):
    make_open(world, swarm_daily_requests=3, swarm_daily_tokens=10_000)
    outcomes = []
    for n in range(6):
        key, grant, ip = visitor(world, n)
        outcomes.append(send(world, key, grant, client=ip))
    assert [status for status, _ in outcomes] == [200, 200, 200, 429, 429, 429]
    assert outcomes[3][1] == {'error': 'entry_swarm_daily_cap'}
    assert len(world['chat'].sent) == 3
    # a refused request costs the visitor's own grant nothing
    key, grant, ip = visitor(world, 99)
    assert send(world, key, grant, client=ip)[1] == {'error': 'entry_swarm_daily_cap'}
    assert world['ledger'].usage(grant['grant_id']) == (0, 0)


def test_swarm_token_budget_clamps_then_refuses(world):
    make_open(world, swarm_daily_requests=100, swarm_daily_tokens=40)
    seen = []
    for n in range(4):
        key, grant, ip = visitor(world, n)
        seen.append(send(world, key, grant, max_new_tokens=16, client=ip))
    assert [s for s, _ in seen] == [200, 200, 200, 429]
    assert [v['max_new_tokens'] for _, v in seen[:3]] == [16, 16, 8]
    assert seen[2][1]['clamped'] is True and seen[2][1]['quota']['swarm_tokens_left'] == 0
    assert seen[3][1] == {'error': 'entry_swarm_daily_cap'}


def test_budget_resets_on_the_next_utc_day_and_survives_a_restart(world, tmp_path):
    make_open(world, swarm_daily_requests=2, swarm_daily_tokens=1000)
    for n in range(2):
        key, grant, ip = visitor(world, n)
        assert send(world, key, grant, client=ip)[0] == 200
    world['ledger'].close()
    world['guard'].ledger = world['ledger'] = UsageLedger(tmp_path / 'entry.sqlite3')
    key, grant, ip = visitor(world, 5)
    assert send(world, key, grant, client=ip)[1] == {'error': 'entry_swarm_daily_cap'}
    world['clock'].t = (world['clock'].t // DAY + 1) * DAY + 5  # next UTC day
    key, grant, ip = visitor(world, 6)
    assert send(world, key, grant, client=ip)[0] == 200


def test_backend_refusal_gives_the_swarm_budget_back(world):
    make_open(world, swarm_daily_requests=1, swarm_daily_tokens=1000)
    key, grant, ip = visitor(world, 1)
    world['chat'].busy = True
    assert send(world, key, grant, client=ip) == (429, {'error': 'chat_busy'})
    world['chat'].busy = False
    world['clock'].t += 1
    assert send(world, key, grant, client=ip)[0] == 200
    key2, grant2, ip2 = visitor(world, 2)
    assert send(world, key2, grant2, client=ip2)[1] == {'error': 'entry_swarm_daily_cap'}


def test_budget_applies_to_open_swarms_only_and_none_disables_it(world):
    world['guard'].limits = EntryLimits(swarm_daily_requests=1, swarm_daily_tokens=16)  # fixture swarm: request mode
    for n in range(3):
        carol = Ed25519Signer.generate()
        assert send(world, carol, grant_for(world, carol), client='ip:%d' % n)[0] == 200
    make_open(world, swarm_daily_requests=None, swarm_daily_tokens=None)
    for n in range(5):
        key, grant, ip = visitor(world, n)
        assert send(world, key, grant, client=ip)[0] == 200


def test_state_reports_the_remaining_swarm_budget_for_open_swarms(world):
    make_open(world, swarm_daily_requests=5, swarm_daily_tokens=100)
    key, grant, ip = visitor(world, 1)
    send(world, key, grant, max_new_tokens=16, client=ip)
    status, state = post(world, 'state', {})
    assert status == 200
    assert state['swarm_budget'] == {'requests_left': 4, 'tokens_left': 84,
                                     'resets_at': (world['clock'].t // DAY + 1) * DAY}
    assert state['killed'] is False


# -- owner kill switch ------------------------------------------------------------------------------

def kill_doc(world, *, engaged=True, seq=1, signer=None, at=None, swarm_id=SWARM):
    return docs.kill_switch(signer or world['owner'], swarm_id, engaged=engaged, seq=seq,
                            at=world['clock'].t if at is None else at)


def test_kill_switch_stops_new_admissions_and_new_open_grants(world):
    make_open(world)
    key, grant, ip = visitor(world, 1)
    assert send(world, key, grant, client=ip)[0] == 200
    assert post(world, 'kill', kill_doc(world)) == (200, {'engaged': True, 'seq': 1})
    world['clock'].t += 1
    sent = len(world['chat'].sent)
    assert send(world, key, grant, client=ip) == (403, {'error': 'entry_killed_by_owner'})
    fresh = Ed25519Signer.generate()
    assert post(world, 'open-grant', docs.open_grant_request(fresh, SWARM, at=world['clock'].t), 'ip:n')[1] == \
        {'error': 'entry_killed_by_owner'}
    assert len(world['chat'].sent) == sent
    assert post(world, 'state', {})[1]['killed'] is True


def test_only_the_owner_key_can_flip_the_switch_and_replays_are_dead(world):
    make_open(world)
    mallory = Ed25519Signer.generate()
    assert post(world, 'kill', kill_doc(world, signer=mallory)) == (403, {'error': 'entry_kill_not_owner'})
    assert post(world, 'kill', kill_doc(world, signer=world['entry'])) == (403, {'error': 'entry_kill_not_owner'})
    forged = dict(kill_doc(world, signer=mallory), owner_key=world['owner'].public_key)
    assert post(world, 'kill', forged) == (403, {'error': 'signature_invalid'})
    assert post(world, 'kill', kill_doc(world, swarm_id='sw-bbbbbbbbbbbbbbbbbbbb')) == \
        (404, {'error': 'entry_swarm_mismatch'})
    assert post(world, 'kill', kill_doc(world, at=world['clock'].t - 3600)) == (400, {'error': 'entry_clock_skew'})
    assert post(world, 'state', {})[1]['killed'] is False
    engage = kill_doc(world, engaged=True, seq=5)
    release = kill_doc(world, engaged=False, seq=6)
    assert post(world, 'kill', engage)[0] == 200 and post(world, 'kill', release)[0] == 200
    # replaying the older "engage" or re-sending the release changes nothing and says so
    assert post(world, 'kill', engage) == (409, {'error': 'entry_kill_seq_stale'})
    assert post(world, 'kill', release) == (409, {'error': 'entry_kill_seq_stale'})
    assert post(world, 'state', {})[1]['killed'] is False


def test_release_reopens_admission_with_the_same_grants_and_the_state_is_durable(world, tmp_path):
    make_open(world)
    key, grant, ip = visitor(world, 1)
    post(world, 'kill', kill_doc(world, seq=1))
    world['ledger'].close()
    world['guard'].ledger = world['ledger'] = UsageLedger(tmp_path / 'entry.sqlite3')
    assert send(world, key, grant, client=ip) == (403, {'error': 'entry_killed_by_owner'})  # restart keeps it killed
    assert post(world, 'kill', kill_doc(world, engaged=False, seq=2)) == (200, {'engaged': False, 'seq': 2})
    world['clock'].t += 1
    assert send(world, key, grant, client=ip)[0] == 200


class RunningChat(FakeChat):
    """FAKE: a generation that streams one token, is not done yet, and finishes later."""

    def __init__(self):
        super().__init__()
        self.finished = False

    def events(self, body):
        if self.finished:
            return {'tokens': ['Ber', 'lin'][body['after']:], 'next': 2, 'done': True, 'error': None,
                    'served_by': [], 'seconds': 1.0}
        return {'tokens': ['Ber'][body['after']:], 'next': 1, 'done': False, 'error': None, 'served_by': [],
                'seconds': None}


def test_in_flight_request_ends_with_an_explicit_terminal_and_stays_terminal(world):
    make_open(world)
    chat = RunningChat()
    world['gateway'].chat = lambda: chat
    key, grant, ip = visitor(world, 1)
    status, accepted = send(world, key, grant, client=ip)
    assert status == 200
    ask = lambda after: post(world, 'events', {'request_id': accepted['request_id'], 'after': after}, ip)[1]
    assert ask(0)['done'] is False
    post(world, 'kill', kill_doc(world))
    page = ask(1)
    assert page['done'] is True and page['error'] == 'entry_killed_by_owner' and page['killed'] is True
    chat.finished = True  # the backend finishing later must not turn the terminal into a success
    assert ask(1)['error'] == 'entry_killed_by_owner'


def test_request_that_finished_before_the_kill_is_left_alone(world):
    make_open(world)
    key, grant, ip = visitor(world, 1)
    status, accepted = send(world, key, grant, client=ip)
    page = post(world, 'events', {'request_id': accepted['request_id'], 'after': 0}, ip)[1]
    assert page['done'] is True and page['error'] is None
    post(world, 'kill', kill_doc(world))
    again = post(world, 'events', {'request_id': accepted['request_id'], 'after': 0}, ip)[1]
    assert again['done'] is True and again['error'] is None and again.get('killed') is None


def test_kill_route_is_rate_limited_by_ip_before_any_signature_work(world):
    make_open(world)
    world['guard'].limits = EntryLimits(per_ip_per_minute=2)
    bad = {'protocol': 'x'}
    assert post(world, 'kill', bad, 'ip:k')[0] == 400
    assert post(world, 'kill', bad, 'ip:k')[0] == 400
    assert post(world, 'kill', bad, 'ip:k') == (429, {'error': 'entry_rate_limited_ip'})


def test_kill_document_is_a_closed_set():
    owner = Ed25519Signer.generate()
    doc = docs.kill_switch(owner, SWARM, engaged=True, seq=1, at=1_800_000_000)
    assert docs.check(doc, docs.KILL_SWITCH, verify) == doc
    for bad in (dict(doc, engaged=1), dict(doc, engaged='yes'), dict(doc, seq=0), dict(doc, extra=1)):
        with pytest.raises(WireError):
            docs.check(bad, docs.KILL_SWITCH, verify)


# -- the A13 owner action -----------------------------------------------------------------------------

class StubDirectory:
    """FAKE directory client: accepts every post."""

    def __init__(self, origin):
        self.origin = origin

    def post(self, path, document):
        return {}


@pytest.fixture
def published(tmp_path):
    app = FakeApp(tmp_path, 'http://127.0.0.1:9')
    clock = type('C', (), {'t': 1_800_000_000, '__call__': lambda self: self.t})()
    publisher = SwarmPublisher(app, origin=lambda: 'https://quiet-river.trycloudflare.com', client_factory=StubDirectory,
                               clock=clock, chat=lambda: FakeChat())
    app.swarm_publisher = publisher
    publisher.publish({'directory': 'https://mycelium.now', 'name': 'Open demo', 'access_mode': 'open'})
    yield app, publisher, clock
    publisher.close()


def test_owner_app_kill_action_signs_with_the_app_key_and_blocks_the_entry(published):
    app, publisher, clock = published
    swarm = publisher.state['swarm_id']
    key = Ed25519Signer.generate()
    ask = lambda: publisher.gateway.handle(PREFIX + 'open-grant', json.dumps(
        docs.open_grant_request(key, swarm, at=int(clock()))).encode(), 'ip:a')
    assert ask()[0] == 200
    result = action(app, '/api/swarm/kill', {'engaged': True})
    assert result['killed'] is True and publisher.status()['killed'] is True
    clock.t += 1
    assert ask() == (403, {'error': 'entry_killed_by_owner'})
    assert action(app, '/api/swarm/kill', {'engaged': False})['killed'] is False
    clock.t += 1
    assert ask()[0] == 200


def test_owner_app_kill_action_validates_its_input_and_mode(published, tmp_path):
    app, publisher, _ = published
    for bad in ({}, {'engaged': 'yes'}, {'engaged': True, 'x': 1}, None):
        with pytest.raises(SwarmPublishError) as error:
            action(app, '/api/swarm/kill', bad)
        assert error.value.code == 'invalid_request'
    publisher.state['mode'] = 'link'  # browser-owned swarm: the browser holds the owner key
    with pytest.raises(SwarmPublishError) as error:
        action(app, '/api/swarm/kill', {'engaged': True})
    assert error.value.code == 'swarm_owner_in_browser'
