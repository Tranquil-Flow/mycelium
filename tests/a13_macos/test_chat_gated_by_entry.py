# SPDX-License-Identifier: AGPL-3.0-only
"""M2: ``/chat`` on the owner relay honours the swarm entry's kill switch, daily cap and access mode.

FAKE (labelled): the chat backend is a duck-typed stand-in.  The entry guard, the usage ledger and
``mycelium_a13_macos.chat.handle`` are the shipped code.
Review: R3 F2 (``/chat`` was a second, un-gated door to the same model next to ``/swarm-entry/v1/*``).
"""
from __future__ import annotations

import json

import pytest

from mycelium_a13_macos import chat
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryGateway, EntryGuard, EntryLimits, EntryPolicy, UsageLedger

SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'
HEADERS = {'X-Mycelium-Chat': '1', 'Content-Type': 'application/json', 'Cf-Connecting-Ip': '203.0.113.7'}
NOW = 1_800_000_000


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.busy = False

    def send(self, client, body):
        if not isinstance(body.get('prompt'), str) or not body['prompt'].strip() \
                or type(body.get('max_new_tokens', 128)) is not int:
            raise chat.ChatError(400, 'chat_request_invalid')  # what the real ChatService.send raises, before any work
        if self.busy:
            raise chat.ChatError(429, 'chat_busy')
        self.sent.append((client, dict(body)))
        return {'request_id': 'r' * 24, 'max_new_tokens': body['max_new_tokens'], 'clamped': False, 'served_by': []}

    def events(self, body):
        return {'tokens': [], 'next': 0, 'done': True}

    def state(self):
        return {'available': True, 'busy': False}


class Publisher:
    def __init__(self, gateway):
        self.gateway = gateway


class App:
    installation_role = 'owner'

    def __init__(self):
        self.chat = FakeChat()
        self.swarm_publisher = None


def build(tmp_path, *, access_mode='open', published=True, **limits):
    owner, entry = Ed25519Signer.generate(), Ed25519Signer.generate()
    quota = {'max_requests': 50, 'max_tokens': 5000, 'ttl_seconds': 3600} if access_mode == 'open' else None
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=1, name='Demo', model='Qwen/Qwen3-0.6B',
                               access_mode=access_mode, open_quota=quota,
                               entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    policy = EntryPolicy.from_record(record, verify, entry_key=entry.public_key)
    ledger = UsageLedger(tmp_path / 'entry.sqlite3')
    guard = EntryGuard(lambda: policy if published else None, entry, verify, ledger, clock=lambda: NOW,
                       limits=EntryLimits(per_ip_per_minute=10_000, per_key_per_minute=10_000, **limits))
    app = App()
    app.swarm_publisher = Publisher(  # type: ignore[assignment]
        EntryGateway(guard, lambda: app.chat))
    return app, guard, owner, policy


def send(app, *, prompt='hi', tokens=16, headers=None, via_relay=True):
    body = json.dumps({'prompt': prompt, 'max_new_tokens': tokens}).encode()
    status, _kind, raw = chat.handle(app, 'POST', '/chat/api/send', headers or HEADERS, body, via_relay=via_relay)
    return status, json.loads(raw)


def kill(guard, owner, seq=1):
    raw = json.dumps(docs.kill_switch(owner, SWARM, engaged=True, seq=seq, at=NOW)).encode()
    guard.apply_kill(raw, 'ip:owner')


def test_an_unpublished_owner_keeps_the_shared_chat_page_as_before(tmp_path):
    app, _guard, _owner, _policy = build(tmp_path, published=False)
    status, value = send(app)
    assert status == 200 and value['request_id'] and len(app.chat.sent) == 1
    app.swarm_publisher = None  # never published at all
    assert send(app)[0] == 200


def test_the_kill_switch_closes_the_relay_chat_door(tmp_path):
    app, guard, owner, _policy = build(tmp_path)
    assert send(app)[0] == 200
    kill(guard, owner)
    status, value = send(app)
    assert (status, value) == (403, {'error': 'entry_killed_by_owner'})
    assert len(app.chat.sent) == 1  # the killed request never reached the backend


@pytest.mark.parametrize('mode', ['request', 'invite'])
def test_a_swarm_that_is_not_open_is_not_reachable_through_relay_chat(tmp_path, mode):
    app, _guard, _owner, _policy = build(tmp_path, access_mode=mode)
    status, value = send(app)
    assert (status, value) == (403, {'error': 'entry_chat_requires_entry'})
    assert app.chat.sent == []


def test_relay_chat_spends_the_same_swarm_wide_daily_budget(tmp_path):
    app, guard, _owner, policy = build(tmp_path, swarm_daily_requests=2, swarm_daily_tokens=10_000)
    assert send(app, headers={**HEADERS, 'Cf-Connecting-Ip': '203.0.113.1'})[0] == 200
    assert send(app, headers={**HEADERS, 'Cf-Connecting-Ip': '203.0.113.2'})[0] == 200
    status, value = send(app, headers={**HEADERS, 'Cf-Connecting-Ip': '203.0.113.3'})
    assert (status, value) == (429, {'error': 'entry_swarm_daily_cap'})
    assert len(app.chat.sent) == 2
    assert guard.ledger.swarm_usage(policy.swarm_id, NOW // 86400) == (2, 32)


def test_relay_chat_tokens_are_clamped_to_what_the_swarm_has_left(tmp_path):
    app, _guard, _owner, _policy = build(tmp_path, swarm_daily_requests=10, swarm_daily_tokens=20)
    assert send(app, tokens=16)[0] == 200
    assert send(app, tokens=16)[0] == 200
    assert [body['max_new_tokens'] for _client, body in app.chat.sent] == [16, 4]
    assert send(app, tokens=16)[1] == {'error': 'entry_swarm_daily_cap'}


def test_a_request_the_backend_refuses_is_not_charged(tmp_path):
    app, guard, _owner, policy = build(tmp_path, swarm_daily_requests=1)
    app.chat.busy = True
    assert send(app) == (429, {'error': 'chat_busy'})
    assert guard.ledger.swarm_usage(policy.swarm_id, NOW // 86400) == (0, 0)
    app.chat.busy = False
    assert send(app)[0] == 200  # the one daily request is still there


def test_malformed_bodies_are_refused_by_the_chat_service_without_charging(tmp_path):
    app, guard, _owner, policy = build(tmp_path)
    for body in ({'prompt': 'x', 'max_new_tokens': 'many'}, {'prompt': '  '}, {'nope': 1}):
        status, _kind, raw = chat.handle(app, 'POST', '/chat/api/send', HEADERS, json.dumps(body).encode(), via_relay=True)
        assert status == 400, body
    assert guard.ledger.swarm_usage(policy.swarm_id, NOW // 86400) == (0, 0)


def test_the_local_loopback_chat_page_is_not_gated_by_the_public_entry(tmp_path):
    app, guard, owner, _policy = build(tmp_path)
    kill(guard, owner)
    status, value = send(app, via_relay=False, headers={'X-Mycelium-Chat': '1', 'Content-Type': 'application/json'})
    assert status == 200 and app.chat.sent[0][0] == 'local'
