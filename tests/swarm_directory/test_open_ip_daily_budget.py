# SPDX-License-Identifier: AGPL-3.0-only
"""M6: a per-address daily budget in front of the swarm-wide one, so one address cannot burn the whole demo.

FAKE (labelled): the chat backend is a duck-typed stand-in; guard, ledger, gateway and relay chat are the shipped code.
Review: R3 F4 (one address, rotating throwaway keys, could take the swarm's whole 200-request day).
"""
import json

import pytest

from mycelium_a13_macos import chat
from mycelium_swarm_directory import docs, hosted_defaults
from mycelium_swarm_directory.crypto import Ed25519Signer
from mycelium_swarm_directory.entry import EntryLimits
from mycelium_swarm_directory.ratelimit import DailyBudget

from test_entry_guard import SWARM, send, world  # noqa: F401  (fixture re-export)
from test_open_guard_rails import DAY, PREFIX, make_open, post, visitor

BIG = 10_000


def rotating_sends(world, count, client):
    """One address, a fresh throwaway key (and open grant) for every request."""
    out = []
    for _ in range(count):
        key = Ed25519Signer.generate()
        status, value = post(world, 'open-grant', docs.open_grant_request(key, SWARM, at=world['clock'].t), client)
        assert status == 200, value
        out.append(send(world, key, value['grant'], client=client))
    return out


def test_defaults_are_named_in_the_one_module_and_wired_into_the_limits():
    limits = EntryLimits()
    assert (limits.ip_daily_requests, limits.ip_daily_tokens, limits.open_grants_per_ip_per_day) == \
        (hosted_defaults.IP_DAILY_REQUESTS, hosted_defaults.IP_DAILY_TOKENS,
         hosted_defaults.OPEN_GRANTS_PER_IP_PER_DAY)
    assert hosted_defaults.IP_DAILY_REQUESTS < hosted_defaults.SWARM_DAILY_REQUESTS
    assert hosted_defaults.IP_DAILY_TOKENS < hosted_defaults.SWARM_DAILY_TOKENS


def test_one_address_rotating_ten_keys_cannot_take_more_than_its_daily_share(world):
    make_open(world, swarm_daily_requests=200, swarm_daily_tokens=40_000, ip_daily_requests=3, ip_daily_tokens=BIG)
    outcomes = rotating_sends(world, 10, 'relay:203.0.113.9')
    assert [status for status, _ in outcomes] == [200] * 3 + [429] * 7
    assert outcomes[3][1] == {'error': 'entry_ip_daily_cap'}
    assert len(world['chat'].sent) == 3
    assert world['ledger'].swarm_usage(SWARM, world['clock'].t // DAY)[0] == 3  # refused ones cost the swarm nothing


def test_a_second_address_still_gets_service_when_the_first_is_spent(world):
    make_open(world, ip_daily_requests=2, ip_daily_tokens=BIG)
    assert [s for s, _ in rotating_sends(world, 3, 'relay:203.0.113.9')] == [200, 200, 429]
    assert [s for s, _ in rotating_sends(world, 2, 'relay:198.51.100.4')] == [200, 200]


def test_the_per_address_token_budget_clamps_then_refuses(world):
    make_open(world, ip_daily_requests=50, ip_daily_tokens=40)
    seen = [send(world, *_visit(world), max_new_tokens=16, client='relay:203.0.113.9') for _ in range(4)]
    assert [s for s, _ in seen] == [200, 200, 200, 429]
    assert [v['max_new_tokens'] for _, v in seen[:3]] == [16, 16, 8]
    assert seen[3][1] == {'error': 'entry_ip_daily_cap'}


def _visit(world):
    key = Ed25519Signer.generate()
    _status, value = post(world, 'open-grant', docs.open_grant_request(key, SWARM, at=world['clock'].t), 'relay:203.0.113.9')
    return key, value['grant']


def test_a_request_the_backend_refuses_gives_the_address_its_budget_back(world):
    make_open(world, ip_daily_requests=1, ip_daily_tokens=BIG)
    key, grant = _visit(world)
    world['chat'].busy = True
    assert send(world, key, grant, client='relay:203.0.113.9') == (429, {'error': 'chat_busy'})
    world['chat'].busy = False
    world['clock'].t += 1
    assert send(world, key, grant, client='relay:203.0.113.9')[0] == 200


def test_the_budget_resets_on_the_next_utc_day(world):
    make_open(world, ip_daily_requests=1, ip_daily_tokens=BIG)
    assert [s for s, _ in rotating_sends(world, 2, 'relay:203.0.113.9')] == [200, 429]
    world['clock'].t = (world['clock'].t // DAY + 1) * DAY + 5
    assert rotating_sends(world, 1, 'relay:203.0.113.9')[0][0] == 200


def test_open_grants_per_address_per_day_are_bounded(world):
    make_open(world, open_grants_per_ip_per_day=3)
    statuses = []
    for _ in range(5):
        key = Ed25519Signer.generate()
        statuses.append(post(world, 'open-grant', docs.open_grant_request(key, SWARM, at=world['clock'].t),
                             'relay:203.0.113.9')[0])
    assert statuses == [200, 200, 200, 429, 429]
    other = Ed25519Signer.generate()
    assert post(world, 'open-grant', docs.open_grant_request(other, SWARM, at=world['clock'].t), 'relay:198.51.100.4')[0] == 200
    world['clock'].t += DAY + 5  # the window slides past a day
    again = Ed25519Signer.generate()
    assert post(world, 'open-grant', docs.open_grant_request(again, SWARM, at=world['clock'].t), 'relay:203.0.113.9')[0] == 200


def test_the_state_route_shows_the_callers_daily_budget_used(world):
    make_open(world, ip_daily_requests=5, ip_daily_tokens=100)
    rotating_sends(world, 2, 'relay:203.0.113.9')
    status, state = post(world, 'state', {}, 'relay:203.0.113.9')
    assert status == 200
    budget = state['ip_budget']
    assert (budget['requests_left'], budget['tokens_left']) == (3, 100 - 2 * 16)
    assert budget['resets_at'] == (world['clock'].t // DAY + 1) * DAY
    other = post(world, 'state', {}, 'relay:198.51.100.4')[1]['ip_budget']
    assert (other['requests_left'], other['tokens_left']) == (5, 100)


def test_the_budget_applies_to_open_swarms_only(world):
    world['guard'].limits = EntryLimits(per_ip_per_minute=BIG, per_key_per_minute=BIG, ip_daily_requests=1,
                                        ip_daily_tokens=16)  # fixture swarm: request mode, grants bound it
    from test_entry_guard import grant_for
    carol = Ed25519Signer.generate()
    grant = grant_for(world, carol, max_requests=3)
    assert [send(world, carol, grant, client='relay:203.0.113.9')[0] for _ in range(3)] == [200, 200, 200]
    assert 'ip_budget' not in post(world, 'state', {}, 'relay:203.0.113.9')[1]


def test_relay_chat_spends_the_same_per_address_budget(world):
    make_open(world, ip_daily_requests=2, ip_daily_tokens=BIG)
    publisher = type('Publisher', (), {'gateway': world['gateway']})()
    app = type('App', (), {'installation_role': 'owner', 'chat': world['chat'], 'swarm_publisher': publisher})()
    headers = {'X-Mycelium-Chat': '1', 'Content-Type': 'application/json', 'Cf-Connecting-Ip': '203.0.113.9'}
    body = json.dumps({'prompt': 'hi', 'max_new_tokens': 8}).encode()
    codes = [chat.handle(app, 'POST', '/chat/api/send', headers, body, via_relay=True)[0] for _ in range(3)]
    assert codes == [200, 200, 429]
    assert rotating_sends(world, 1, 'relay:' + '203.0.113.9')[0][1] == {'error': 'entry_ip_daily_cap'}  # shared with the entry


# -- the client key: IPv6 is judged by its /64 -------------------------------------------------------------------

@pytest.mark.parametrize('first,second,same', [
    ('2001:db8:1:2:aaaa:bbbb:cccc:dddd', '2001:db8:1:2::1', True),        # same /64
    ('2001:db8:1:2::1', '2001:db8:1:3::1', False),                        # neighbouring /64
    ('2001:DB8:1:2::1', '2001:db8:1:2:0:0:0:9', True),                    # case and zero-compression do not matter
    ('::ffff:203.0.113.9', '203.0.113.9', True),                          # v4-mapped is the v4 address
    ('203.0.113.9', '203.0.113.10', False),                               # v4 stays per address
])
def test_client_key_normalises_ipv6_to_its_64(first, second, same):
    key = lambda ip: chat.client_key({'Cf-Connecting-Ip': ip}, via_relay=True)  # noqa: E731
    assert (key(first) == key(second)) is same
    assert key(first).startswith('relay:')


@pytest.mark.parametrize('bad', ['', 'not-an-ip', '1.2.3.4.5', '2001:db8::zz', '[::1]', '203.0.113.9, 1.1.1.1'])
def test_client_key_for_a_bad_header_is_the_shared_unknown_bucket(bad):
    assert chat.client_key({'Cf-Connecting-Ip': bad}, via_relay=True) == 'relay:unknown'
    assert chat.client_key({}, via_relay=False) == 'local'


# -- the counter itself -------------------------------------------------------------------------------------------

def test_daily_budget_reserves_releases_and_stays_bounded():
    budget = DailyBudget(requests=2, tokens=30, max_keys=100)
    assert budget.reserve('a', 1, 20) == 20
    assert budget.reserve('a', 1, 20) == 10  # clamped to what is left
    assert budget.reserve('a', 1, 20) is None  # no requests left
    budget.release('a', 1, requests=1, tokens=10)
    assert budget.left('a', 1) == (1, 10)
    assert budget.reserve('a', 2, 20) == 20  # a new day starts empty
    for n in range(1000):
        budget.reserve('flood-%d' % n, 2, 1)
    assert budget.size() <= 100
    assert budget.reserve('fresh', 2, 1) == 1  # a flood evicts old keys, it never locks everyone out
