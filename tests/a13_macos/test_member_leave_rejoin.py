# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10: re-join after a voluntary leave.

A Mac that left (smoothly or now) keeps its node key, node id, consent files and model cache.  It asks for a new
pairing, the owner approves as usual (the V1.1 re-join path), and the seed issues a new generation.  The voluntary
leave is sticky locally: an owner that revoked the generation in answer to the signed withdrawal does not turn the
Mac's own "I left" into "the owner revoked me", and a restart does not try to resume the old membership.
"""
from __future__ import annotations

import pytest

from mycelium_a13_macos.peer import RemoteMacMemberError
from test_join_window_flow import CONSENT as APP_CONSENT, STANDING, _owner, _target
from test_member_leave import _joined_pair
from test_peer import CONSENT, QUOTA, _enroll, _member, seed  # noqa: F401 - seed is a fixture


# -- peer level ---------------------------------------------------------------------------------------------------------

def test_a_voluntary_leave_opens_the_rejoin_path_and_keeps_identity_and_cache(tmp_path, seed):
    authority, factory = seed
    root = tmp_path / 'target'
    target = _member(root, factory)
    target.start()
    request, _handoff, accepted = _enroll(authority, target)
    key = (root / 'identity' / 'node.key').read_bytes()
    (root / 'serving').mkdir()
    (root / 'serving' / 'model-bytes').write_bytes(b'cached')

    with pytest.raises(RemoteMacMemberError, match='peer_already_enrolled'):
        target.pair_request(CONSENT, ttl_seconds=300)            # a current member does not re-pair
    target.record_voluntary_leave()
    assert target.status()['membership_state'] == 'left'
    fresh = target.pair_request(CONSENT, ttl_seconds=300)
    assert fresh['node_id'] == request['node_id']
    joined = target.accept_handoff(authority.approve_pairing(fresh, ttl_seconds=120, quota=QUOTA))
    assert joined['membership_generation'] == accepted['membership_generation'] + 1
    assert target.status()['membership_state'] == 'current'
    assert (root / 'identity' / 'node.key').read_bytes() == key
    assert (root / 'serving' / 'model-bytes').read_bytes() == b'cached'
    target.stop()


def test_after_the_owner_self_revoked_the_withdrawn_generation_the_mac_still_rejoins(tmp_path, seed):
    authority, factory = seed
    target = _member(tmp_path / 'target', factory)
    target.start()
    request, _handoff, accepted = _enroll(authority, target)
    node = request['node_id']
    generation = accepted['membership_generation']
    target.record_voluntary_leave()
    # what the owner does on the signed final withdrawal (DemoAuthority.revoke)
    revoked = authority.coordinator.advance_member_generation(node_id=node, expected_generation=generation,
                                                              lifecycle_state='STOPPED')
    assert revoked['generation'] == generation + 1
    # the leaving Mac sees "revoked" from the owner on any later call; its own leave stays the recorded truth
    target._record_disposition('revoked')  # noqa: SLF001 - what translate() does on peer_member_revoked
    assert target.status()['membership_state'] == 'left'
    joined = target.accept_handoff(authority.approve_pairing(target.pair_request(CONSENT, ttl_seconds=300),
                                                             ttl_seconds=120, quota=QUOTA))
    assert joined['membership_generation'] > generation + 1
    assert authority.coordinator.member(node)['generation'] == joined['membership_generation']
    assert authority.coordinator.member(node)['lifecycle_state'] != 'STOPPED'
    assert target.status()['membership_state'] == 'current'
    target.stop()


def test_a_restart_after_leaving_does_not_resume_the_old_membership(tmp_path, seed):
    authority, factory = seed
    root = tmp_path / 'target'
    target = _member(root, factory)
    target.start()
    request, _handoff, accepted = _enroll(authority, target)
    target.record_voluntary_leave()
    authority.coordinator.advance_member_generation(node_id=request['node_id'],
                                                    expected_generation=accepted['membership_generation'],
                                                    lifecycle_state='STOPPED')
    target.stop()
    again = _member(root, factory)
    again.start()                                              # must not raise and must not try to resume
    assert again.status()['membership_state'] == 'left'
    assert authority.coordinator.member(request['node_id'])['generation'] == accepted['membership_generation'] + 1
    fresh = again.pair_request(CONSENT, ttl_seconds=300)
    assert fresh['node_id'] == request['node_id']
    again.stop()


def test_a_left_mac_cannot_work_or_renew_until_it_rejoins(tmp_path, seed):
    authority, factory = seed
    target = _member(tmp_path / 'target', factory)
    target.start()
    _enroll(authority, target)
    target.record_voluntary_leave()
    with pytest.raises(RemoteMacMemberError):
        target.renew()
    target.stop()


def test_an_owner_revoked_membership_still_does_not_rejoin_by_itself(tmp_path, seed):
    authority, factory = seed
    target = _member(tmp_path / 'target', factory)
    target.start()
    _enroll(authority, target)
    target._record_disposition('revoked')  # noqa: SLF001
    with pytest.raises(RemoteMacMemberError, match='peer_already_enrolled'):
        target.pair_request(CONSENT, ttl_seconds=300)
    target.stop()


# -- the application ------------------------------------------------------------------------------------------------------

def _rejoin(owner, target):
    again = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
    waiting = target.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                                 'join': again['join_link']})
    assert waiting['state'] == 'waiting_for_owner'
    windows = owner.action('/api/pair/window/list', {})['windows']
    [window] = [w for w in windows if w.get('pending') and w['window_id'] == again['window_id']]
    owner.action('/api/pair/window/approve', {'window_id': window['window_id'],
                                              'request_digest': window['pending']['request_digest']})
    return target.action('/api/join/window/poll', {})


@pytest.mark.parametrize('mode', ['instant', 'smooth'])
def test_the_app_rejoins_after_leaving_with_the_same_node_a_new_generation_and_its_cache(tmp_path, monkeypatch, mode):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        node, generation = target.authority.member.node_id, target.authority.member.generation
        cache = target.authority.peer.state_root / 'serving'
        cache.mkdir(exist_ok=True)
        (cache / 'model-bytes').write_bytes(b'cached')
        target.action('/api/leave', {'mode': mode, 'confirm': True})
        if mode == 'smooth':
            target._leave_thread.join(timeout=60)
        status = target.status()
        assert status['leave']['state'] == 'left' and status['authority']['membership_state'] == 'left'
        assert target._resource_check() == 'owner_stop' and target._left is True

        joined = _rejoin(owner, target)
        assert joined['state'] == 'joined'
        assert target.authority.member.node_id == node
        assert target.authority.member.generation > generation
        after = target.status()
        assert after['authority']['membership_state'] == 'current'
        assert after['leave'] is None                          # the finished leave no longer fences this Mac
        assert target._left is False and target._resource_check() != 'owner_stop'
        assert after['join_flow']['standing_consent']['state'] == 'granted'
        assert (cache / 'model-bytes').read_bytes() == b'cached'
        assert not (target.root / 'leave-state.json').exists()
    finally:
        target.close(); owner.close()


def test_a_failed_rejoin_leaves_the_mac_left(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        target.action('/api/leave', {'mode': 'instant', 'confirm': True})
        again = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        target.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                           'join': again['join_link']})
        # the owner never approves: the poll says waiting and nothing was un-left
        assert target.action('/api/join/window/poll', {})['state'] == 'waiting_for_owner'
        assert target.status()['leave']['state'] == 'left' and target._left is True
        assert target._resource_check() == 'owner_stop'
    finally:
        target.close(); owner.close()


def test_a_current_member_still_gets_already_joined(tmp_path, monkeypatch):
    owner, target = _joined_pair(tmp_path, monkeypatch)
    try:
        again = owner.action('/api/pair/window/open', {'ttl_seconds': 600})
        with pytest.raises(Exception) as caught:
            target.action('/api/join/window', {'consent': APP_CONSENT, 'standing_consent': STANDING,
                                               'join': again['join_link']})
        assert str(caught.value) == 'already_joined'
    finally:
        target.close(); owner.close()


def test_an_app_relaunched_after_leaving_stays_left_even_if_the_owner_revoked(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    owner, first = _joined_pair(tmp_path, monkeypatch)
    relaunched = None
    try:
        node, generation = first.authority.member.node_id, first.authority.member.generation
        first.action('/api/leave', {'mode': 'instant', 'confirm': True})
        owner.action('/api/revoke', {'node_id': node, 'generation': generation})
        factory = first._test_peer_factory
        first.close()
        relaunched = DemoApp(tmp_path / 'target', check_platform=False, _test_peer_factory=factory,
                             _test_join_base_url=owner.public_relay.proxy.origin).start()
        status = relaunched.status()
        assert status['authority']['membership_state'] == 'left'
        assert status['leave']['state'] == 'left' and relaunched._left is True
        assert relaunched.recovery_error is None
        assert _rejoin(owner, relaunched)['state'] == 'joined'
        assert relaunched.status()['leave'] is None
    finally:
        if relaunched is not None:
            relaunched.close()
        first.close(); owner.close()
