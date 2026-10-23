# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-start lease lifecycle: real signed membership, no model or device work."""
import json
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import token_policy
from mycelium_node.token_provisioning import load_host_trust, sign_route_policy
from mycelium_router.token_path_authority import TokenPathAuthorityError
from tests.node_agent.test_membership import _clocked_joined, _delayed_renewal, NOW


@pytest.fixture
def lease_world(tmp_path, monkeypatch):
    wall = [NOW]
    origin = time.monotonic()
    session, seed = _clocked_joined(tmp_path, wall)
    client = SimpleNamespace(signer=session.signer, trusted_owner_endpoint_id=seed.endpoint_id,
                             trusted_owner_key_record=seed.public_key_record())
    revoked = [False]

    def member():
        if revoked[0]:
            raise TokenPathAuthorityError('token_path_policy_stale')
        return dict(node_id=session.node_id, endpoint_id=session.signer.endpoint_id,
                    verification_key_digest=session.signer.verification_key_digest,
                    generation=session.generation, lease_expires_at=session.lease_expires_at)

    monkeypatch.setattr(time, 'time', lambda: wall[0])
    options = dict(root=tmp_path, client=client, current_member=member, run_id='cold',
                   deployment_id='deployment', max_wall_seconds=1800)
    path = token_policy.prepare_host_pin(**options)
    original = json.loads(path.read_bytes())
    shift = [0.]
    now = lambda: origin + wall[0] - NOW + shift[0]
    current = lambda: not revoked[0] and wall[0] < session.lease_expires_at
    load = lambda: load_host_trust(path, run_id='cold', deployment_id='deployment',
        node_id=session.node_id, current=current, clock=now, unix_ms=lambda: int(wall[0]*1000))
    # Both the target reader and native child have already loaded the original pin.
    readers = [load(), load()]
    pin = token_policy.member_pin(client, member())
    graph = SimpleNamespace(deployment_id='deployment', manifest_digest='sha256:'+'a'*64,
                            stages=[SimpleNamespace(placements=[SimpleNamespace(node_id=session.node_id)])])
    peers = lambda: {session.node_id: {k: pin[k] for k in ('node_id','endpoint_id','generation')}}

    def policy():
        return sign_route_policy(signer=seed, run_id='cold', deployment_id='deployment',
            model_digest=graph.manifest_digest, requester_id='owner', members={session.node_id:pin},
            max_new_tokens=48, stop_token_ids=(), expires_at_unix_ms=int((wall[0]+300)*1000))

    def renew():
        wall[0] = NOW + 200
        heartbeat = session.heartbeat(lifecycle_state='RUNNING', active_requests=0, force=True)
        message_id = heartbeat['message']['message_id']
        renewal = _delayed_renewal(seed, message_id='cold-renewal', heartbeat_message_id=message_id,
            issued_at=wall[0], expires_at=wall[0]+300, lease_expires_at=wall[0]+300)
        session.accept_lease_renewal(renewal, heartbeat_message_id=message_id)
        assert session.generation == original['member']['generation']
        return renewal

    return SimpleNamespace(**locals())


def test_cold_compile_outlasts_pin_but_authenticated_renewal_allows_install(lease_world):
    w = lease_world
    w.renew()
    w.wall[0] = NOW + 400  # Original 300 s lease expired; renewed 500 s lease is live.
    token_policy.prepare_host_pin(**w.options)
    for trust in w.readers:
        admitted = trust.admit(w.policy(), graph=w.graph, peers=w.peers)
        assert admitted.current()
        assert admitted.deadline_monotonic_s == pytest.approx(w.origin + 470)
    refreshed = json.loads(w.path.read_bytes())
    assert refreshed['lease_expires_at_unix_ms'] == int(w.session.lease_expires_at*1000)
    assert refreshed['process_deadline_monotonic_s'] == w.original['process_deadline_monotonic_s']


@pytest.mark.parametrize('kind', ['expired', 'revoked', 'unrefreshed'])
def test_install_never_extends_without_current_authenticated_membership(lease_world, kind):
    w = lease_world
    if kind != 'expired':
        w.renew()
    w.wall[0] = NOW + 400
    if kind == 'revoked':
        w.revoked[0] = True
    if kind != 'unrefreshed':
        with pytest.raises(TokenPathAuthorityError, match='token_path_policy_(expired|stale)'):
            token_policy.prepare_host_pin(**w.options)
    assert json.loads(w.path.read_bytes()) == w.original
    for trust in w.readers:
        with pytest.raises(TokenPathAuthorityError, match='token_path_policy_expired'):
            trust.admit(w.policy(), graph=w.graph, peers=w.peers)


@pytest.mark.parametrize('mutation', ['process', 'owner', 'member', 'mode', 'symlink'])
def test_refresh_cannot_replace_identity_or_process_ceiling(lease_world, mutation):
    w = lease_world
    w.renew()
    w.wall[0] = NOW + 400
    token_policy.prepare_host_pin(**w.options)
    pin = json.loads(w.path.read_bytes())
    if mutation == 'process': pin['process_deadline_monotonic_s'] += 10
    if mutation == 'owner': pin['owner_endpoint_id'] = 'other-owner'
    if mutation == 'member': pin['member']['generation'] += 1
    w.path.write_text(json.dumps(pin))
    if mutation == 'mode': w.path.chmod(0o644)
    if mutation == 'symlink':
        destination = w.path.with_name('other-pin')
        w.path.rename(destination)
        w.path.symlink_to(destination)
    with pytest.raises(TokenPathAuthorityError, match='token_path_host_pin_(invalid|stale)'):
        w.readers[0].admit(w.policy(), graph=w.graph, peers=w.peers)


def test_refresh_cannot_reset_process_deadline_or_extend_installed_policy(lease_world):
    w = lease_world
    w.renew()
    w.wall[0] = NOW + 400
    token_policy.prepare_host_pin(**w.options)
    installed = w.readers[0].admit(w.policy(), graph=w.graph, peers=w.peers)
    deadline = installed.deadline_monotonic_s
    # Reopening and later signed-policy dates cannot refresh an installed registry.
    token_policy.prepare_host_pin(**w.options)
    with pytest.raises(TokenPathAuthorityError, match='token_path_policy_already_installed'):
        w.readers[0].admit(w.policy(), graph=w.graph, peers=w.peers)
    assert installed.deadline_monotonic_s == deadline
    w.wall[0] = NOW + 471
    assert not installed.current()


def test_runtime_refreshes_authenticated_pin_immediately_before_install(tmp_path, monkeypatch):
    from test_managed_runtime import runtime, _world
    with _world(tmp_path/'world') as world:
        client = world['target_client']
        client.trusted_owner_endpoint_id = world['owner_signer'].endpoint_id
        target = runtime(tmp_path/'target', world,
                         current_member=world['target'].managed_control_snapshot)
        try:
            path = target.worker.token_policy_trust_file
            before = json.loads(path.read_bytes())
            world['target'].renew()  # Real pinned TLS and signed renewal acceptance.
            calls = []
            policy = target.worker._token_policy
            policy.graph = object()
            policy._peers = {'dummy': {}}

            def admit(*args, **kwargs):
                calls.append(json.loads(path.read_bytes()))
                raise TokenPathAuthorityError('test_boundary_reached')

            monkeypatch.setattr(policy.trust, 'admit', admit)
            with pytest.raises(TokenPathAuthorityError, match='test_boundary_reached'):
                policy.before('install_token_path', {'policy': {}})
            assert len(calls) == 1
            assert calls[0]['lease_expires_at_unix_ms'] == int(world['target'].managed_control_snapshot()['lease_expires_at']*1000)
            assert calls[0]['lease_expires_at_unix_ms'] > before['lease_expires_at_unix_ms']
            assert calls[0]['process_deadline_monotonic_s'] == before['process_deadline_monotonic_s']
            world['target']._record_disposition('revoked')
            with pytest.raises((ValueError, RuntimeError)):
                policy.before('install_token_path', {'policy': {}})
            assert len(calls) == 1
        finally:
            target.close()


def test_revocation_after_refresh_still_refuses_install(lease_world):
    w = lease_world
    w.renew()
    w.wall[0] = NOW + 400
    token_policy.prepare_host_pin(**w.options)
    w.revoked[0] = True
    for trust in w.readers:
        with pytest.raises(TokenPathAuthorityError, match='token_path_policy_stale'):
            trust.admit(w.policy(), graph=w.graph, peers=w.peers)


def test_unchanged_lease_is_not_extended_by_wall_clock_rollback(lease_world):
    w = lease_world
    # Wall clock moves backwards but elapsed process time passes the original
    # conservative lease deadline. No authenticated renewal was observed.
    w.shift[0] = 450
    w.wall[0] = NOW - 50
    with pytest.raises(TokenPathAuthorityError, match='token_path_policy_expired'):
        w.readers[0].admit(w.policy(), graph=w.graph, peers=w.peers)


def test_fresh_lease_cannot_pass_original_process_budget(lease_world):
    w = lease_world
    w.renew()
    w.wall[0] = NOW + 400
    token_policy.prepare_host_pin(**w.options)
    w.readers[0].ceiling = w.origin + 399
    with pytest.raises(TokenPathAuthorityError, match='token_path_policy_expired'):
        w.readers[0].admit(w.policy(), graph=w.graph, peers=w.peers)


def test_forged_renewal_cannot_refresh_host_lease(lease_world):
    from mycelium_membership import sign_membership_message
    from mycelium_node.membership import NodeMembershipError
    from mycelium_qualification.signing import generate_ed25519_signer
    w = lease_world
    w.wall[0] = NOW + 200
    heartbeat = w.session.heartbeat(lifecycle_state='RUNNING', active_requests=0, force=True)
    message_id = heartbeat['message']['message_id']
    envelope = _delayed_renewal(w.seed, message_id='forged-renewal', heartbeat_message_id=message_id,
        issued_at=w.wall[0], expires_at=w.wall[0]+300, lease_expires_at=w.wall[0]+300)
    # Well-formed and same endpoint, but not the accepted seed's key.
    envelope = sign_membership_message(signer=generate_ed25519_signer(endpoint_id=w.seed.endpoint_id),
                                       message=envelope['message'])
    with pytest.raises(NodeMembershipError, match='membership_seed_rotation_mismatch'):
        w.session.accept_lease_renewal(envelope, heartbeat_message_id=message_id)
    assert w.session.lease_expires_at == NOW + 300
    token_policy.prepare_host_pin(**w.options)
    assert json.loads(w.path.read_bytes()) == w.original
