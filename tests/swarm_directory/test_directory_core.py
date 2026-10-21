# SPDX-License-Identifier: AGPL-3.0-only
"""Swarm directory core: signed records only, every swarm visible, bounded, durable."""
import json

import pytest

from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory, Limits, PRIVACY_NOTE
from mycelium_swarm_directory.store import MemoryStore, SqliteStore
from mycelium_swarm_directory.wire import WireError, canonical, fingerprint


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    clock = Clock()
    directory = Directory(MemoryStore(), verify, clock=clock)
    return directory, clock


def record(owner, swarm_id='sw-aaaaaaaaaaaaaaaaaaaa', seq=1, **over):
    return docs.swarm_record(owner, swarm_id=swarm_id, seq=seq, name=over.get('name', 'Berlin coworking'),
                             model=over.get('model', 'Qwen/Qwen3-0.6B'), description=over.get('description', ''),
                             access_mode=over.get('access_mode', 'request'), entry=over.get('entry'),
                             open_quota=over.get('open_quota'))


def call(directory, method, path, body=None, client='ip:1'):
    raw = b'' if body is None else json.dumps(body).encode()
    status, value = directory.handle(method, path, raw, client)
    return status, value


def test_create_and_list_shows_every_swarm_with_public_fields(env):
    directory, clock = env
    alice, bob = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert call(directory, 'POST', '/records', record(alice))[0] == 200
    assert call(directory, 'POST', '/records', record(bob, 'sw-bbbbbbbbbbbbbbbbbbbb', access_mode='invite'))[0] == 200
    status, listing = call(directory, 'GET', '/swarms')
    assert status == 200
    rows = {row['swarm_id']: row for row in listing['swarms']}
    assert set(rows) == {'sw-aaaaaaaaaaaaaaaaaaaa', 'sw-bbbbbbbbbbbbbbbbbbbb'}  # invite-only is visible too
    row = rows['sw-aaaaaaaaaaaaaaaaaaaa']
    assert row['name'] == 'Berlin coworking' and row['model'] == 'Qwen/Qwen3-0.6B'
    assert row['owner_fingerprint'] == fingerprint(alice.public_key)
    assert row['status'] == 'offline' and row['members'] == 0 and row['access_mode'] == 'request'
    assert row['privacy'] == PRIVACY_NOTE and 'seen by this swarm' in PRIVACY_NOTE
    assert rows['sw-bbbbbbbbbbbbbbbbbbbb']['access_mode'] == 'invite'
    # the signed record travels with the listing so clients verify it themselves
    assert row['record']['sig'] and row['record']['owner_key'] == alice.public_key


def test_unsigned_forged_and_tampered_records_are_refused(env):
    directory, _ = env
    alice, mallory = Ed25519Signer.generate(), Ed25519Signer.generate()
    good = record(alice)
    unsigned = {k: v for k, v in good.items() if k != 'sig'}
    assert call(directory, 'POST', '/records', unsigned) == (400, {'error': 'document_fields_invalid'})
    tampered = dict(good, name='Free GPUs')
    assert call(directory, 'POST', '/records', tampered) == (403, {'error': 'signature_invalid'})
    forged = dict(record(mallory), owner_key=alice.public_key)
    assert call(directory, 'POST', '/records', forged) == (403, {'error': 'signature_invalid'})
    assert call(directory, 'GET', '/swarms')[1]['swarms'] == []


def test_only_the_owner_updates_and_seq_must_grow(env):
    directory, _ = env
    alice, mallory = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert call(directory, 'POST', '/records', record(alice))[0] == 200
    assert call(directory, 'POST', '/records', record(mallory)) == (403, {'error': 'swarm_owner_mismatch'})
    assert call(directory, 'POST', '/records', record(alice, name='Renamed')) == (409, {'error': 'record_seq_stale'})
    assert call(directory, 'POST', '/records', record(alice, seq=2, name='Renamed'))[0] == 200
    assert call(directory, 'GET', '/swarms/sw-aaaaaaaaaaaaaaaaaaaa')[1]['swarm']['name'] == 'Renamed'


def test_delete_by_owner_and_old_record_cannot_be_replayed(env):
    directory, _ = env
    alice = Ed25519Signer.generate()
    first = record(alice)
    call(directory, 'POST', '/records', first)
    assert call(directory, 'POST', '/delete', docs.delete(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaaa', 2)) \
        == (403, {'error': 'swarm_owner_mismatch'})
    assert call(directory, 'POST', '/delete', docs.delete(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', 2))[0] == 200
    assert call(directory, 'GET', '/swarms')[1]['swarms'] == []
    assert call(directory, 'POST', '/records', first) == (409, {'error': 'swarm_id_retired'})


def test_heartbeat_status_serving_warming_and_expiry_to_offline(env):
    directory, clock = env
    alice, entry = Ed25519Signer.generate(), Ed25519Signer.generate()
    call(directory, 'POST', '/records', record(alice, entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'}))
    hb = docs.heartbeat(entry, 'sw-aaaaaaaaaaaaaaaaaaaa', status='warming', members=2, served_model=None, at=clock.t)
    assert call(directory, 'POST', '/heartbeat', hb)[0] == 200
    row = call(directory, 'GET', '/swarms')[1]['swarms'][0]
    assert row['status'] == 'warming' and row['members'] == 2
    clock.t += 10
    hb = docs.heartbeat(entry, 'sw-aaaaaaaaaaaaaaaaaaaa', status='serving', members=2, served_model='Qwen/Qwen3-0.6B', at=clock.t)
    call(directory, 'POST', '/heartbeat', hb)
    assert call(directory, 'GET', '/swarms')[1]['swarms'][0]['status'] == 'serving'
    # replayed (not newer) heartbeat refused
    assert call(directory, 'POST', '/heartbeat', hb) == (409, {'error': 'heartbeat_stale'})
    # a stranger cannot heartbeat
    stranger = docs.heartbeat(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaaa', status='serving', members=9,
                              served_model=None, at=clock.t + 1)
    assert call(directory, 'POST', '/heartbeat', stranger) == (403, {'error': 'heartbeat_signer_not_authorized'})
    clock.t += Limits().heartbeat_ttl + 1
    assert call(directory, 'GET', '/swarms')[1]['swarms'][0]['status'] == 'offline'


def test_heartbeat_with_skewed_clock_is_refused(env):
    directory, clock = env
    alice = Ed25519Signer.generate()
    call(directory, 'POST', '/records', record(alice))
    hb = docs.heartbeat(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', status='serving', members=1, served_model=None,
                        at=clock.t + 3600)
    assert call(directory, 'POST', '/heartbeat', hb) == (400, {'error': 'clock_skew'})


def test_access_request_owner_inbox_grant_and_deny(env):
    directory, clock = env
    alice, carol, dave = (Ed25519Signer.generate() for _ in range(3))
    call(directory, 'POST', '/records', record(alice))
    req = docs.access_request(carol, 'sw-aaaaaaaaaaaaaaaaaaaa', note='hi, demo visitor', at=clock.t)
    assert call(directory, 'POST', '/access-requests', req)[0] == 200
    call(directory, 'POST', '/access-requests', docs.access_request(dave, 'sw-aaaaaaaaaaaaaaaaaaaa', note='', at=clock.t))
    # inbox needs the owner's signature
    status, _ = call(directory, 'POST', '/owner-inbox', docs.owner_query(carol, 'sw-aaaaaaaaaaaaaaaaaaaa', at=clock.t))
    assert status == 403
    status, inbox = call(directory, 'POST', '/owner-inbox', docs.owner_query(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', at=clock.t))
    assert status == 200 and {r['requester_key'] for r in inbox['requests']} == {carol.public_key, dave.public_key}
    grant = docs.grant(alice, swarm_id='sw-aaaaaaaaaaaaaaaaaaaa', grantee_key=carol.public_key, issued_at=clock.t,
                       expires_at=clock.t + 3600, max_requests=5, max_tokens=500)
    assert call(directory, 'POST', '/grants', grant)[0] == 200
    assert call(directory, 'POST', '/deny', docs.deny(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', dave.public_key, at=clock.t))[0] == 200
    status, mine = call(directory, 'POST', '/my-grants', docs.grantee_query(carol, at=clock.t))
    assert status == 200 and mine['grants'] == [grant]
    states = {r['requester_key']: r['state'] for r in call(directory, 'POST', '/owner-inbox',
              docs.owner_query(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', at=clock.t + 1))[1]['requests']}
    assert states == {carol.public_key: 'granted', dave.public_key: 'denied'}
    status, theirs = call(directory, 'POST', '/my-grants', docs.grantee_query(dave, at=clock.t))
    assert theirs['grants'] == [] and theirs['denied'] == ['sw-aaaaaaaaaaaaaaaaaaaa']


def test_grant_deposit_must_be_signed_by_the_swarm_owner(env):
    directory, clock = env
    alice, mallory, carol = (Ed25519Signer.generate() for _ in range(3))
    call(directory, 'POST', '/records', record(alice))
    forged = docs.grant(mallory, swarm_id='sw-aaaaaaaaaaaaaaaaaaaa', grantee_key=carol.public_key, issued_at=clock.t,
                        expires_at=clock.t + 60, max_requests=1, max_tokens=10)
    assert call(directory, 'POST', '/grants', forged) == (403, {'error': 'grant_issuer_not_owner'})


def test_query_replay_is_refused(env):
    directory, clock = env
    alice = Ed25519Signer.generate()
    call(directory, 'POST', '/records', record(alice))
    query = docs.owner_query(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', at=clock.t)
    assert call(directory, 'POST', '/owner-inbox', query)[0] == 200
    assert call(directory, 'POST', '/owner-inbox', query) == (409, {'error': 'nonce_replayed'})


def test_entry_offer_then_owner_attaches_entry(env):
    directory, clock = env
    alice, mac = Ed25519Signer.generate(), Ed25519Signer.generate()
    call(directory, 'POST', '/records', record(alice))
    offer = docs.entry_offer(mac, 'sw-aaaaaaaaaaaaaaaaaaaa', 'https://quiet-river.trycloudflare.com', at=clock.t)
    assert call(directory, 'POST', '/entry-offers', offer)[0] == 200
    inbox = call(directory, 'POST', '/owner-inbox', docs.owner_query(alice, 'sw-aaaaaaaaaaaaaaaaaaaa', at=clock.t))[1]
    assert [o['entry_fingerprint'] for o in inbox['entry_offers']] == [fingerprint(mac.public_key)]
    attached = record(alice, seq=2, entry={'key': mac.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    assert call(directory, 'POST', '/records', attached)[0] == 200
    row = call(directory, 'GET', '/swarms')[1]['swarms'][0]
    assert row['entry_origin'] == 'https://quiet-river.trycloudflare.com'
    assert row['entry_fingerprint'] == fingerprint(mac.public_key)


def test_entry_origin_must_be_https_unless_loopback_enabled(env):
    directory, _ = env
    alice = Ed25519Signer.generate()
    bad = record(alice, entry={'key': alice.public_key, 'origin': 'http://evil.example'})
    assert call(directory, 'POST', '/records', bad) == (400, {'error': 'entry_origin_invalid'})
    local = record(alice, entry={'key': alice.public_key, 'origin': 'http://127.0.0.1:4555'})
    assert call(directory, 'POST', '/records', local) == (400, {'error': 'entry_origin_invalid'})
    dev = Directory(MemoryStore(), verify, clock=Clock(), allow_loopback_entries=True)
    assert call(dev, 'POST', '/records', local)[0] == 200


def test_size_caps_and_per_owner_and_total_caps():
    clock = Clock()
    directory = Directory(MemoryStore(), verify, clock=clock, limits=Limits(max_swarms=3, max_swarms_per_owner=2))
    alice = Ed25519Signer.generate()
    assert call(directory, 'POST', '/records', record(alice, name='x' * 49)) == (400, {'error': 'name_invalid'})
    status, _ = directory.handle('POST', '/records', b'{' + b' ' * 9000 + b'}', 'ip:1')
    assert status == 413
    call(directory, 'POST', '/records', record(alice, 'sw-aaaaaaaaaaaaaaaaaaa1'))
    call(directory, 'POST', '/records', record(alice, 'sw-aaaaaaaaaaaaaaaaaaa2'))
    assert call(directory, 'POST', '/records', record(alice, 'sw-aaaaaaaaaaaaaaaaaaa3')) == (429, {'error': 'owner_swarm_cap'})
    call(directory, 'POST', '/records', record(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaa4'))
    assert call(directory, 'POST', '/records', record(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaa5')) \
        == (429, {'error': 'directory_full'})


def test_per_ip_and_per_key_write_rate_limits():
    clock = Clock()
    directory = Directory(MemoryStore(), verify, clock=clock, limits=Limits(writes_per_ip_per_minute=3,
                                                                           writes_per_key_per_minute=2))
    alice = Ed25519Signer.generate()
    call(directory, 'POST', '/records', record(alice), client='ip:9')
    for at in (1, 2):
        call(directory, 'POST', '/access-requests',
             docs.access_request(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaaa', note='', at=clock.t + at), client='ip:9')
    assert call(directory, 'POST', '/access-requests',
                docs.access_request(Ed25519Signer.generate(), 'sw-aaaaaaaaaaaaaaaaaaaa', note='', at=clock.t),
                client='ip:9') == (429, {'error': 'rate_limited_ip'})
    carol = Ed25519Signer.generate()
    for i in range(2):
        call(directory, 'POST', '/access-requests',
             docs.access_request(carol, 'sw-aaaaaaaaaaaaaaaaaaaa', note=str(i), at=clock.t), client='ip:%d' % i)
    assert call(directory, 'POST', '/access-requests',
                docs.access_request(carol, 'sw-aaaaaaaaaaaaaaaaaaaa', note='x', at=clock.t),
                client='ip:7') == (429, {'error': 'rate_limited_key'})
    clock.t += 61
    assert call(directory, 'POST', '/access-requests',
                docs.access_request(carol, 'sw-aaaaaaaaaaaaaaaaaaaa', note='y', at=clock.t), client='ip:9')[0] == 200


def test_sqlite_store_is_durable_across_restart(tmp_path):
    clock = Clock()
    path = tmp_path / 'directory.sqlite3'
    alice = Ed25519Signer.generate()
    first = Directory(SqliteStore(path), verify, clock=clock)
    call(first, 'POST', '/records', record(alice))
    first.store.close()
    second = Directory(SqliteStore(path), verify, clock=clock)
    assert [r['swarm_id'] for r in call(second, 'GET', '/swarms')[1]['swarms']] == ['sw-aaaaaaaaaaaaaaaaaaaa']
    assert (path.stat().st_mode & 0o777) == 0o600


def test_canonical_bytes_match_the_js_rules():
    assert canonical({'b': 1, 'a': ['x', None, True], 'c': {'z': 'ä', 'y': 'a/b'}}) == \
        '{"a":["x",null,true],"b":1,"c":{"y":"a/b","z":"ä"}}'.encode()
    with pytest.raises(WireError):
        canonical({'a': 1.5})
    with pytest.raises(WireError):
        canonical({'a': 'bell\x07'})


def test_unknown_routes_and_methods(env):
    directory, _ = env
    assert call(directory, 'GET', '/nope') == (404, {'error': 'route_unknown'})
    assert call(directory, 'DELETE', '/swarms') == (405, {'error': 'method_not_allowed'})


def test_signed_read_queries_have_their_own_budget_and_never_spend_the_write_budget():
    """The page polls /my-grants and /owner-inbox every few seconds; that must not lock a key or a shared address out of writing."""
    clock = Clock()
    directory = Directory(MemoryStore(), verify, clock=clock, limits=Limits(writes_per_ip_per_minute=3, writes_per_key_per_minute=2))
    alice = Ed25519Signer.generate()
    swarm = 'sw-aaaaaaaaaaaaaaaaaaaa'
    call(directory, 'POST', '/records', record(alice), client='ip:9')
    for i in range(30):  # far above both write budgets
        assert call(directory, 'POST', '/my-grants', docs.grantee_query(alice, at=clock.t), client='ip:9')[0] == 200, i
        assert call(directory, 'POST', '/owner-inbox', docs.owner_query(alice, swarm, at=clock.t), client='ip:9')[0] == 200, i
    # the write budget is untouched: three writes in all fit per address, the fourth is the limit
    for _ in range(2):
        assert call(directory, 'POST', '/access-requests', docs.access_request(Ed25519Signer.generate(), swarm, note='', at=clock.t),
                    client='ip:9')[0] == 200
    assert call(directory, 'POST', '/access-requests', docs.access_request(Ed25519Signer.generate(), swarm, note='', at=clock.t),
                client='ip:9') == (429, {'error': 'rate_limited_ip'})


def test_signed_read_queries_are_still_bounded():
    clock = Clock()
    directory = Directory(MemoryStore(), verify, clock=clock, limits=Limits(queries_per_key_per_minute=5, queries_per_ip_per_minute=100))
    alice = Ed25519Signer.generate()
    outcomes = [call(directory, 'POST', '/my-grants', docs.grantee_query(alice, at=clock.t), client='ip:9')[0] for _ in range(7)]
    assert outcomes == [200] * 5 + [429, 429]
    other = Ed25519Signer.generate()
    ip_limited = Directory(MemoryStore(), verify, clock=clock, limits=Limits(queries_per_key_per_minute=100, queries_per_ip_per_minute=3))
    codes = [call(ip_limited, 'POST', '/my-grants', docs.grantee_query(other, at=clock.t), client='ip:4')[0] for _ in range(5)]
    assert codes == [200] * 3 + [429, 429]
