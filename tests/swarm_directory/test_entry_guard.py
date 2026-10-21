# SPDX-License-Identifier: AGPL-3.0-only
"""Entry-side enforcement: the swarm's own entry admits only owner-signed, live, in-quota grants."""
import json

import pytest

from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryGateway, EntryGuard, EntryLimits, EntryPolicy, UsageLedger
from mycelium_swarm_directory.wire import WireError

SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


class FakeChat:
    """Duck-typed chat.ChatService: records admitted sends (a fake backend, labelled)."""

    def __init__(self):
        self.sent = []
        self.busy = False

    def send(self, client, body):
        if self.busy:
            error = RuntimeError('chat_busy')
            error.status, error.code = 429, 'chat_busy'
            raise error
        self.sent.append((client, body))
        return {'request_id': 'r' * 24, 'max_new_tokens': body['max_new_tokens'], 'clamped': False, 'served_by': []}

    def events(self, body):
        return {'tokens': ['Ber', 'lin'], 'next': 2, 'done': True, 'error': None, 'served_by': [], 'seconds': 0.1}

    def state(self):
        return {'available': True, 'busy': False, 'warming_up': False, 'model_id': 'Qwen/Qwen3-0.6B', 'served_by': [],
                'limits': {}}


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    owner, entry = Ed25519Signer.generate(), Ed25519Signer.generate()
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=1, name='Demo', model='Qwen/Qwen3-0.6B',
                               access_mode='request', entry={'key': entry.public_key,
                                                             'origin': 'https://quiet-river.trycloudflare.com'})
    policy = {'value': EntryPolicy.from_record(record, verify, entry_key=entry.public_key)}
    ledger = UsageLedger(tmp_path / 'entry.sqlite3')
    guard = EntryGuard(lambda: policy['value'], entry, verify, ledger, clock=clock)
    chat = FakeChat()
    gateway = EntryGateway(guard, lambda: chat)
    return {'clock': clock, 'owner': owner, 'entry': entry, 'guard': guard, 'chat': chat, 'gateway': gateway,
            'policy': policy, 'ledger': ledger, 'record': record}


def grant_for(world, grantee, **over):
    t = world['clock'].t
    return docs.grant(over.pop('issuer', world['owner']), swarm_id=over.pop('swarm_id', SWARM),
                      grantee_key=grantee.public_key, issued_at=over.pop('issued_at', t),
                      expires_at=over.pop('expires_at', t + 3600), max_requests=over.pop('max_requests', 3),
                      max_tokens=over.pop('max_tokens', 1000))


def send(world, requester, grant, prompt='Capital of Germany?', max_new_tokens=16, client='ip:1', **over):
    document = docs.entry_send(requester, over.pop('swarm_id', SWARM), grant, prompt, max_new_tokens,
                               at=over.pop('at', world['clock'].t))
    return world['gateway'].handle('/swarm-entry/v1/send', json.dumps(document).encode(), client)


def test_valid_owner_grant_is_admitted_and_reaches_the_backend(world):
    carol = Ed25519Signer.generate()
    status, value = send(world, carol, grant_for(world, carol))
    assert status == 200 and value['request_id'] == 'r' * 24
    (client, body), = world['chat'].sent
    assert client.startswith('swarm:') and body == {'prompt': 'Capital of Germany?', 'max_new_tokens': 16}
    status, page = world['gateway'].handle('/swarm-entry/v1/events',
                                           json.dumps({'request_id': 'r' * 24, 'after': 0}).encode(), 'ip:1')
    assert status == 200 and ''.join(page['tokens']) == 'Berlin'


def test_no_grant_or_garbage_grant_is_refused_before_the_backend(world):
    carol = Ed25519Signer.generate()
    assert send(world, carol, {})[1] == {'error': 'entry_grant_invalid'}
    assert send(world, carol, {'protocol': 'x'})[0] == 403
    assert world['chat'].sent == []


def test_forged_grant_signed_by_a_stranger_is_refused(world):
    carol, mallory = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert send(world, carol, grant_for(world, carol, issuer=mallory)) == (403, {'error': 'entry_grant_issuer_invalid'})
    tampered = dict(grant_for(world, carol), max_tokens=999_999)
    assert send(world, carol, tampered) == (403, {'error': 'entry_grant_signature_invalid'})
    # an entry-signed grant is valid only for open swarms
    assert send(world, carol, grant_for(world, carol, issuer=world['entry'])) == \
        (403, {'error': 'entry_grant_issuer_invalid'})
    assert world['chat'].sent == []


def test_grant_for_someone_else_or_another_swarm_is_refused(world):
    carol, dave = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert send(world, dave, grant_for(world, carol)) == (403, {'error': 'entry_grant_not_for_key'})
    other = grant_for(world, carol, swarm_id='sw-bbbbbbbbbbbbbbbbbbbb')
    assert send(world, carol, other) == (403, {'error': 'entry_grant_swarm_mismatch'})


def test_expired_and_not_yet_valid_grants_are_refused(world):
    carol = Ed25519Signer.generate()
    t = world['clock'].t
    expired = grant_for(world, carol, issued_at=t - 7200, expires_at=t - 1)
    assert send(world, carol, expired) == (403, {'error': 'entry_grant_expired'})
    future = grant_for(world, carol, issued_at=t + 3600, expires_at=t + 7200)
    assert send(world, carol, future) == (403, {'error': 'entry_grant_not_yet_valid'})


def test_quota_is_charged_durably_and_exhaustion_refuses(world, tmp_path):
    carol = Ed25519Signer.generate()
    grant = grant_for(world, carol, max_requests=2, max_tokens=40)
    assert send(world, carol, grant, max_new_tokens=16)[0] == 200
    world['clock'].t += 1
    # a ledger reopened from disk keeps the charge (restart does not reset quota)
    world['ledger'].close()
    world['guard'].ledger = UsageLedger(tmp_path / 'entry.sqlite3')
    status, value = send(world, carol, grant, max_new_tokens=100)
    assert status == 200 and value['max_new_tokens'] == 24  # clamped to the remaining tokens
    world['clock'].t += 1
    assert send(world, carol, grant) == (429, {'error': 'entry_quota_exhausted'})


def test_request_signature_and_replay_and_clock_are_checked(world):
    carol, mallory = Ed25519Signer.generate(), Ed25519Signer.generate()
    grant = grant_for(world, carol)
    document = docs.entry_send(carol, SWARM, grant, 'hi', 8, at=world['clock'].t)
    raw = json.dumps(document).encode()
    assert world['gateway'].handle('/swarm-entry/v1/send', raw, 'ip:1')[0] == 200
    assert world['gateway'].handle('/swarm-entry/v1/send', raw, 'ip:1') == (409, {'error': 'entry_nonce_replayed'})
    # stolen grant: mallory signs a request carrying carol's grant
    assert send(world, mallory, grant) == (403, {'error': 'entry_grant_not_for_key'})
    impersonation = dict(docs.entry_send(mallory, SWARM, grant, 'hi', 8, at=world['clock'].t), key=carol.public_key)
    assert world['gateway'].handle('/swarm-entry/v1/send', json.dumps(impersonation).encode(), 'ip:1') == \
        (403, {'error': 'signature_invalid'})
    assert send(world, carol, grant, at=world['clock'].t - 3600) == (400, {'error': 'entry_clock_skew'})


def test_backend_refusal_refunds_the_quota(world):
    carol = Ed25519Signer.generate()
    grant = grant_for(world, carol, max_requests=1, max_tokens=16)
    world['chat'].busy = True
    assert send(world, carol, grant) == (429, {'error': 'chat_busy'})
    world['chat'].busy = False
    world['clock'].t += 1
    assert send(world, carol, grant)[0] == 200


def test_per_key_and_per_ip_rate_limits(world):
    world['guard'].limits = EntryLimits(per_key_per_minute=2, per_ip_per_minute=3)
    carol, dave = Ed25519Signer.generate(), Ed25519Signer.generate()
    gc, gd = grant_for(world, carol, max_requests=50, max_tokens=5000), grant_for(world, dave, max_requests=50,
                                                                                 max_tokens=5000)
    assert send(world, carol, gc, client='ip:1')[0] == 200
    assert send(world, carol, gc, client='ip:2')[0] == 200
    assert send(world, carol, gc, client='ip:3') == (429, {'error': 'entry_rate_limited_key'})
    assert send(world, dave, gd, client='ip:9')[0] == 200
    assert send(world, dave, gd, client='ip:9')[0] == 200
    world['guard'].limits = EntryLimits(per_key_per_minute=50, per_ip_per_minute=3)
    assert send(world, dave, gd, client='ip:9')[0] == 200
    assert send(world, dave, gd, client='ip:9') == (429, {'error': 'entry_rate_limited_ip'})


def test_revoked_grant_is_refused(world):
    carol = Ed25519Signer.generate()
    grant = grant_for(world, carol)
    world['ledger'].revoke(grant['grant_id'])
    assert send(world, carol, grant) == (403, {'error': 'entry_grant_revoked'})


def test_open_swarm_auto_grant_is_issued_once_per_key_and_bounded(world):
    owner, entry = world['owner'], world['entry']
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=2, name='Open', model='Qwen/Qwen3-0.6B',
                               access_mode='open', open_quota={'max_requests': 2, 'max_tokens': 64,
                                                               'ttl_seconds': 600},
                               entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    world['policy']['value'] = EntryPolicy.from_record(record, verify, entry_key=entry.public_key)
    carol = Ed25519Signer.generate()
    ask = lambda: world['gateway'].handle('/swarm-entry/v1/open-grant', json.dumps(
        docs.open_grant_request(carol, SWARM, at=world['clock'].t)).encode(), 'ip:1')
    status, value = ask()
    assert status == 200
    grant = value['grant']
    assert grant['issuer_key'] == entry.public_key and grant['max_requests'] == 2 and grant['max_tokens'] == 64
    assert grant['expires_at'] - grant['issued_at'] == 600
    assert ask()[1]['grant'] == grant  # same grant: asking again cannot reset the quota
    assert send(world, carol, grant)[0] == 200


def test_open_grant_refused_for_request_mode_swarm(world):
    carol = Ed25519Signer.generate()
    status, value = world['gateway'].handle('/swarm-entry/v1/open-grant', json.dumps(
        docs.open_grant_request(carol, SWARM, at=world['clock'].t)).encode(), 'ip:1')
    assert (status, value) == (403, {'error': 'entry_swarm_not_open'})


def test_policy_requires_a_valid_owner_record_naming_this_entry(world):
    other = Ed25519Signer.generate()
    with pytest.raises(WireError) as caught:
        EntryPolicy.from_record(world['record'], verify, entry_key=other.public_key)
    assert caught.value.code == 'entry_not_attached'
    tampered = dict(world['record'], access_mode='open')
    with pytest.raises(WireError):
        EntryPolicy.from_record(tampered, verify, entry_key=world['entry'].public_key)


def test_unpublished_entry_refuses_everything(world):
    world['policy']['value'] = None
    carol = Ed25519Signer.generate()
    assert send(world, carol, grant_for(world, carol)) == (503, {'error': 'entry_not_published'})
    assert world['gateway'].handle('/swarm-entry/v1/state', b'{}', 'ip:1') == (503, {'error': 'entry_not_published'})


def test_state_is_public_and_bounded(world):
    status, state = world['gateway'].handle('/swarm-entry/v1/state', b'{}', 'ip:1')
    assert status == 200 and state['swarm_id'] == SWARM and state['access_mode'] == 'request'
    assert state['available'] is True and state['limits']['max_new_tokens'] == 256
    assert 'seen by this swarm' in state['privacy']
