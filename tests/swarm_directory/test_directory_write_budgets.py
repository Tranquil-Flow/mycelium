# SPDX-License-Identifier: AGPL-3.0-only
"""M8: heartbeats have their own budget, and nothing unsigned spends the directory-wide write budget.

The real ``Directory`` core on an in-memory store; no fakes.
Review: R3 F5 (the global 600/min write budget was charged before signatures were checked and was shared with
heartbeats: junk POSTs from ~20 addresses turned every swarm offline).
"""
import json

from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory, Limits
from mycelium_swarm_directory.store import MemoryStore

SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'
NOW = 1_800_000_000


def build(**limits):
    owner = Ed25519Signer.generate()
    directory = Directory(MemoryStore(), verify, clock=lambda: NOW, limits=Limits(**limits))
    return directory, owner


def post(directory, path, document, client):
    raw = document if isinstance(document, bytes) else json.dumps(document).encode()
    return directory.handle('POST', path, raw, client)


def publish(directory, owner, swarm_id=SWARM, client='ip:owner'):
    record = docs.swarm_record(owner, swarm_id=swarm_id, seq=1, name='Demo', model='Qwen/Qwen3-0.6B')
    status, value = post(directory, '/records', record, client)
    assert status == 200, value


def beat(owner, at=NOW, swarm_id=SWARM):
    return docs.heartbeat(owner, swarm_id, status='serving', members=2, served_model='Qwen/Qwen3-0.6B', at=at)


def test_700_junk_posts_from_30_addresses_do_not_starve_a_valid_heartbeat():
    directory, owner = build()
    publish(directory, owner)
    junk = [b'{', b'not json', json.dumps({'protocol': 'x'}).encode(),
            json.dumps(beat(Ed25519Signer.generate(), swarm_id='sw-zzzzzzzzzzzzzzzzzzzz')).encode(),  # unknown swarm
            json.dumps({**beat(owner), 'members': 99}).encode()]  # tampered after signing
    outcomes = [post(directory, '/heartbeat', junk[n % len(junk)], 'ip:junk%d' % (n % 30))[0] for n in range(700)]
    assert 503 not in outcomes
    status, value = post(directory, '/heartbeat', beat(owner), 'ip:honest')
    assert (status, value['status']) == (200, 'serving')


def test_valid_signed_writes_are_what_spends_the_global_write_budget():
    directory, owner = build(writes_per_minute=3)
    others = [Ed25519Signer.generate() for _ in range(5)]
    statuses = []
    for n, key in enumerate(others):
        record = docs.swarm_record(key, swarm_id='sw-%020d' % n, seq=1, name='N%d' % n, model='Qwen/Qwen3-0.6B')
        statuses.append(post(directory, '/records', record, 'ip:w%d' % n)[0])
    assert statuses == [200, 200, 200, 503, 503]  # three signed writes spend it; the rest are told it is busy


def test_a_spent_write_budget_does_not_stop_heartbeats():
    directory, owner = build(writes_per_minute=1, heartbeats_per_minute=10)
    publish(directory, owner)  # spends the one write
    spam = docs.swarm_record(Ed25519Signer.generate(), swarm_id='sw-bbbbbbbbbbbbbbbbbbbb', seq=1, name='Spam',
                             model='Qwen/Qwen3-0.6B')
    assert post(directory, '/records', spam, 'ip:spam')[1] == {'error': 'directory_busy'}
    for second in range(3):
        status, value = post(directory, '/heartbeat', beat(owner, at=NOW + second), 'ip:honest')
        assert status == 200, value


def test_heartbeats_have_their_own_bounded_budget():
    directory, owner = build(heartbeats_per_minute=2)
    publish(directory, owner)
    codes = []
    for second in range(4):
        codes.append(post(directory, '/heartbeat', beat(owner, at=NOW + second), 'ip:h%d' % second)[0])
    assert codes == [200, 200, 503, 503]


def test_unsigned_or_unauthorised_heartbeats_do_not_spend_the_heartbeat_budget():
    directory, owner = build(heartbeats_per_minute=2)
    publish(directory, owner)
    stranger = Ed25519Signer.generate()
    for n in range(20):
        assert post(directory, '/heartbeat', beat(stranger), 'ip:s%d' % n)[0] == 403
        assert post(directory, '/heartbeat', b'{}', 'ip:t%d' % n)[0] in {400, 422}
    assert post(directory, '/heartbeat', beat(owner), 'ip:honest')[0] == 200


def test_signed_queries_never_spend_the_write_budgets():
    directory, owner = build(writes_per_minute=1, heartbeats_per_minute=1)
    publish(directory, owner)  # the one write
    grantee = Ed25519Signer.generate()
    for n in range(5):
        query = docs.sign(grantee, {'protocol': docs.GRANTEE_QUERY, 'grantee_key': grantee.public_key, 'at': NOW,
                                    'nonce': docs.new_nonce()})
        assert post(directory, '/my-grants', query, 'ip:q%d' % n)[0] == 200
