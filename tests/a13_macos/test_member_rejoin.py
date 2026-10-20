# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 item d: a member whose membership expired re-joins through a new invite without resetting its state.

Live demo (2026-10-21): the laptop's membership lapsed while a run was stuck; ``/api/join/window`` answered
``peer_already_enrolled`` and the only way back was to move the app state aside, re-join as a NEW node and download the
model again.  An expired, stale or unknown membership may now ask for a new pairing: the node key, node id, consent and
model cache stay, the owner approves as usual, and the seed issues the next generation.  A current membership still
refuses, and so does one the owner revoked.
"""
import time

import pytest

from mycelium_node.identity import load_node_signer
from mycelium_a13_macos.peer import RemoteMacMemberError
from test_join_window_flow import CONSENT as APP_CONSENT, STANDING, _owner, _target
from test_peer import CONSENT, QUOTA, _enroll, _member, seed  # noqa: F401 - seed is a fixture


def _expire(target):
    """The owner stopped renewing us: the member records an expired disposition (as a failed renewal does)."""
    target._record_disposition('expired')  # noqa: SLF001 - the same call translate() makes on peer_member_expired


def test_an_expired_member_asks_for_a_new_pairing_and_keeps_its_identity(tmp_path, seed):
    authority, factory = seed
    root = tmp_path / 'target'
    target = _member(root, factory)
    target.start()
    request, _handoff, accepted = _enroll(authority, target)
    node_id = request['node_id']
    key = (root / 'identity' / 'node.key').read_bytes()
    (root / 'serving').mkdir()
    (root / 'serving' / 'model-bytes').write_bytes(b'cached')
    membership_before = (root / 'membership.state.json').read_bytes()

    _expire(target)
    assert target.status()['membership_state'] == 'expired'
    fresh = target.pair_request(CONSENT, ttl_seconds=300)
    assert fresh['node_id'] == node_id
    assert (root / 'membership.state.json').read_bytes() == membership_before  # nothing replaced until the owner approves

    handoff = authority.approve_pairing(fresh, ttl_seconds=120, quota=QUOTA)
    joined = target.accept_handoff(handoff)
    assert joined['membership_generation'] == accepted['membership_generation'] + 1
    assert joined['membership_state'] == 'current'
    assert target.status()['membership_state'] == 'current'
    assert (root / 'identity' / 'node.key').read_bytes() == key
    assert (root / 'serving' / 'model-bytes').read_bytes() == b'cached'
    assert authority.coordinator.member(node_id)['generation'] == joined['membership_generation']
    assert target.renew()['membership_state'] == 'current'  # the new membership is usable
    target.stop()


def test_a_restart_after_the_rejoin_resumes_the_new_membership(tmp_path, seed):
    authority, factory = seed
    root = tmp_path / 'target'
    target = _member(root, factory)
    target.start()
    _enroll(authority, target)
    _expire(target)
    handoff = authority.approve_pairing(target.pair_request(CONSENT, ttl_seconds=300), ttl_seconds=120, quota=QUOTA)
    joined = target.accept_handoff(handoff)
    target.stop()
    again = _member(root, factory)
    again.start()
    assert again.status()['membership_state'] == 'current'
    assert again.status()['membership_generation'] == joined['membership_generation'] + 1  # resume advances once
    again.stop()


def test_a_current_membership_still_refuses_a_second_pairing(tmp_path, seed):
    authority, factory = seed
    target = _member(tmp_path / 'target', factory)
    target.start()
    _enroll(authority, target)
    with pytest.raises(RemoteMacMemberError, match='peer_already_enrolled'):
        target.pair_request(CONSENT, ttl_seconds=300)
    target.stop()


def test_a_membership_the_owner_revoked_does_not_rejoin_by_itself(tmp_path, seed):
    authority, factory = seed
    target = _member(tmp_path / 'target', factory)
    target.start()
    _enroll(authority, target)
    target._record_disposition('revoked')  # noqa: SLF001
    with pytest.raises(RemoteMacMemberError, match='peer_already_enrolled'):
        target.pair_request(CONSENT, ttl_seconds=300)
    target.stop()


def test_a_relaunch_that_finds_the_member_stale_can_still_ask_to_rejoin(tmp_path, seed):
    from mycelium_node import NodeMembershipSession
    from mycelium_node.durable_membership import load_membership_state
    authority, factory = seed
    root = tmp_path / 'target'
    target = _member(root, factory)
    target.start()
    request, _handoff, _accepted = _enroll(authority, target)
    # Another incarnation of the same node resumed at the owner (a second copy of the app): ours is now stale.
    state = load_membership_state(root)
    signer = load_node_signer(root / 'identity' / 'node.key', endpoint_id=state['endpoint_id'])
    other = NodeMembershipSession(
        node_id=state['node_id'], swarm_id=state['swarm_id'], seed_node_id=state['seed_node_id'], signer=signer,
        incarnation='alternate-restart', software_version='mycelium-a13-macos-demo/1', peer_class='mac_mlx_iroh',
        runtime_capability={'runtime_backend': 'mlx', 'transport': 'iroh', 'activation_protocol': 'mycelium.router_wire.v1'})
    resume = other.resume_request(previous_generation=state['membership_generation'],
                                  previous_incarnation=state['incarnation'],
                                  endpoint_addrs=[f"https://{state['endpoint_id']}.invalid/control"])
    client = factory(bundle=None, membership=state, now=time.time(), timeout=2)
    client.identity(now=time.time() + 1)
    other.accept_resume(client.resume(resume_envelope=resume), seed_key_digest=state['seed_key_digest'])
    target.stop()
    relaunched = _member(root, factory)
    with pytest.raises(RemoteMacMemberError, match='peer_generation_stale'):
        relaunched.start()
    fresh = relaunched.pair_request(CONSENT, ttl_seconds=300)
    assert fresh['node_id'] == request['node_id']
    joined = relaunched.accept_handoff(authority.approve_pairing(fresh, ttl_seconds=120, quota=QUOTA))
    assert joined['membership_state'] == 'current' and joined['membership_generation'] == 3
    relaunched.stop()


def test_the_app_join_window_accepts_an_expired_member_without_a_reset(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, 'https://relay-fixture.invalid')
    target = None
    try:
        opened = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        target = _target(tmp_path, owner)
        target.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                           'join': opened['join_link']})
        [window] = owner.action('/api/pair/window/list', {})['windows']
        owner.action('/api/pair/window/approve', {'window_id': window['window_id'],
                                                  'request_digest': window['pending']['request_digest']})
        assert target.action('/api/join/window/poll', {})['state'] == 'joined'
        node_id = target.authority.member.node_id
        generation = target.authority.member.generation
        cache = target.authority.peer.state_root / 'serving'
        cache.mkdir(exist_ok=True)
        (cache / 'model-bytes').write_bytes(b'cached')

        _expire(target.authority.peer)
        assert target.status()['authority']['membership_state'] == 'expired'
        again = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        waiting = target.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                                     'join': again['join_link']})
        assert waiting['state'] == 'waiting_for_owner'
        windows = owner.action('/api/pair/window/list', {})['windows']
        [window] = [w for w in windows if w.get('pending') and w['window_id'] == again['window_id']]
        owner.action('/api/pair/window/approve', {'window_id': window['window_id'],
                                                  'request_digest': window['pending']['request_digest']})
        assert target.action('/api/join/window/poll', {})['state'] == 'joined'
        assert target.authority.member.node_id == node_id
        assert target.authority.member.generation == generation + 1
        assert target.status()['authority']['membership_state'] == 'current'
        assert (cache / 'model-bytes').read_bytes() == b'cached'
    finally:
        if target is not None:
            target.close()
        owner.close()


def test_the_app_relaunches_with_a_lapsed_membership_and_offers_the_rejoin(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.peer import RemoteMacMember
    from mycelium_a13_macos.public_relay import SeedRelayProxy  # noqa: F401 - _owner patches the relay
    owner = _owner(tmp_path, monkeypatch, 'https://relay-fixture.invalid')
    first = relaunched = None
    try:
        opened = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        first = _target(tmp_path, owner)
        first.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING, 'join': opened['join_link']})
        [window] = owner.action('/api/pair/window/list', {})['windows']
        owner.action('/api/pair/window/approve', {'window_id': window['window_id'],
                                                  'request_digest': window['pending']['request_digest']})
        assert first.action('/api/join/window/poll', {})['state'] == 'joined'
        node_id = first.authority.member.node_id
        base_factory = first._test_peer_factory
        first.close()

        class Lapsed(RemoteMacMember):
            def _resume(self, membership):  # the owner no longer accepts this generation
                self._record_disposition('stale')
                raise RemoteMacMemberError('peer_generation_stale')

        from mycelium_a13_macos.peer import RemoteMacMemberError

        def factory(root):
            peer = base_factory(root)
            peer.__class__ = Lapsed
            return peer
        relaunched = DemoApp(tmp_path / 'target', check_platform=False, _test_peer_factory=factory,
                             _test_join_base_url=owner.public_relay.proxy.origin).start()  # must not raise
        assert relaunched.recovery_error == 'peer_generation_stale'
        assert relaunched.status()['authority']['membership_state'] == 'stale'
        again = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        relaunched._test_peer_factory = base_factory
        waiting = relaunched.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                                         'join': again['join_link']})
        assert waiting['state'] == 'waiting_for_owner'
        [window] = [w for w in owner.action('/api/pair/window/list', {})['windows']
                    if w.get('pending') and w['window_id'] == again['window_id']]
        owner.action('/api/pair/window/approve', {'window_id': window['window_id'],
                                                  'request_digest': window['pending']['request_digest']})
        relaunched.authority.peer.__class__ = RemoteMacMember
        assert relaunched.action('/api/join/window/poll', {})['state'] == 'joined'
        assert relaunched.authority.member.node_id == node_id
    finally:
        for app in (relaunched, first):
            if app is not None:
                app.close()
        owner.close()
