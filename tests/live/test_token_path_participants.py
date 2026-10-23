# SPDX-License-Identifier: AGPL-3.0-only
"""Native signed-bind -> relay composition, private authority and fake compute.

The host policy is explicitly provisioned, not inferred from an incoming grant.
This does not pretend that managed bootstrap or owner/chat activation is wired.
"""
from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from tests.live.test_token_path_admission import world, real_prefill_node, payload
from tests.live.test_token_path_hops import setup, header


def provision(w, n):
    api = importlib.import_module('mycelium_node.token_participants')
    members = {p['node_id']: {**p, 'verification_key_digest':
        w.entry.verification_key_digest if p['node_id'] == 'entry' else 'sha256:' + 'f' * 64}
        for p in w.pins['participants']}
    peers = {p['node_id']: deepcopy(p) for p in w.pins['participants']}
    policy = api.ParticipantAdmissionPolicy(
        run_id='desk', requester_id='requester', owner_endpoint_id=w.owner.endpoint_id,
        owner_key=w.owner.public_key_record(), members=lambda: members,
        peers=lambda: peers, max_new_tokens=6, stop_token_ids=(99,),
        process_deadline_monotonic_s=1800., lease_deadline_monotonic_s=100.,
        clock_uncertainty_ms=100, clock=lambda: w.now, unix_ms=lambda: 100000)
    n.provision_token_path(policy)
    return policy, members, peers


def bind(w):
    return {'request_id': 'request', 'control': deepcopy(w.control),
            'token_path': {k: v for k, v in payload(w).items() if k != 'control'}}


def entry(w):
    n = real_prefill_node(w)
    n.router.relay = SimpleNamespace(token_path_gate=None)
    n._token_path_admission = None
    n._request_controls.clear()
    return n


def test_dispatch_registers_native_journal_before_publishing(world):
    n = entry(world)
    provision(world, n)
    n._bind_request_control(bind(world))
    observations = []
    class Emitter:
        def emit_progress(self, **kwargs):
            assert n._token_journals.active_count == 1
            observations.append(kwargs)
            # This registration unit uses an immediate in-process consumer.
            # The CPU-child/TLS/pump test separately crosses authenticated ACK.
            receipt = kwargs['receipt']
            journal = n._token_journals._active[receipt['statement']['grant_digest']]
            journal.acknowledge(receipt['statement']['sequence'],
                                receipt['signature']['signed_statement_digest'])
    n._token_response_emitter = Emitter()
    result = n.dispatch(dict(protocol='mycelium.physical_node_control.v1',
        run_id='desk', deployment_id='deployment', command_id='actual-start',
        command='infer_start', payload=payload(world)))
    assert result['observation']['details']['status'] == 'COMPLETED'
    assert observations and all(r['command_id'] == 'actual-start' and r['node_id'] == 'entry' for r in observations)
    assert observations[-1]['receipt']['statement']['status'] == 'COMPLETED'


def test_native_bind_derives_scope_then_entry_start_reuses_admission(world):
    n = entry(world)
    provision(world, n)
    result = n._bind_request_control(bind(world))
    assert result['observation']['details']['token_grant_digest'] == sha256_bytes(
        canonical_json_bytes(world.grant['statement']))
    assert world.prefill == 0
    assert n._infer_start(payload(world))['observation']['details']['status'] == 'COMPLETED'
    assert world.prefill == 1 and world.rounds == 5
    with pytest.raises(RuntimeError, match='token_path_grant_replay'):
        n._infer_start(payload(world))
    assert world.prefill == 1


def test_remote_bind_installs_gate_used_by_actual_relay(world):
    s = setup(world, install=False)
    n = real_prefill_node(world)
    n.node_id = 'tail'
    n._identity = lambda: {'run_id': 'desk', 'node_id': 'tail'}
    n.router = s.router
    n._token_path_admission = None
    n._request_controls.clear()
    provision(world, n)
    n._bind_request_control(bind(world))
    assert s.router.receive_hop(header(world), b'activation', source_node_id='entry').disposition == 'COMPLETED'
    assert len(s.runtime.executed) == 1
    n._request_controls['request']['cancellation_generation'] += 1
    result = s.router.receive_hop(header(world, 'DECODE', 1), b'activation', source_node_id='entry')
    assert result.reason == 'token_path_grant_stale'
    assert len(s.runtime.executed) == 1


@pytest.mark.parametrize('change', ['grant_entry', 'grant_owner', 'grant_member', 'grant_model',
    'grant_request', 'manifest', 'control', 'peers', 'members', 'expired', 'unknown'])
def test_bind_cannot_self_authorize_or_install_changed_scope(world, change):
    n = entry(world)
    _, members, peers = provision(world, n)
    p = bind(world)
    scope = p['token_path']['token_grant']['statement']['scope']
    if change == 'grant_entry':
        scope['entry_key_digest'] = 'sha256:' + 'a' * 64
    elif change == 'grant_owner':
        scope['owner_key_digest'] = 'sha256:' + 'a' * 64
    elif change == 'grant_member':
        scope['participants'][1]['generation'] += 1
    elif change == 'grant_model':
        scope['model_digest'] = 'sha256:' + 'a' * 64
    elif change == 'grant_request':
        p['token_path']['request']['prompt_token_ids'].append(12)
    elif change == 'manifest':
        p['token_path']['path_manifest']['path_id'] = 'foreign'
    elif change == 'control':
        p['control']['deployment_id'] = 'foreign'
    elif change == 'peers':
        peers['tail']['generation'] += 1
    elif change == 'members':
        members['tail']['generation'] += 1
    elif change == 'expired':
        world.now = 1801.
    else:
        p['token_path']['unknown'] = True
    g = p['token_path']['token_grant']
    g['signature'] = world.owner.sign(g['statement'])
    with pytest.raises((RuntimeError, ValueError)):
        n._bind_request_control(p)
    assert world.prefill == world.rounds == 0
    assert 'request' not in n._request_controls


@pytest.mark.parametrize('change', ['peer', 'member', 'cancel', 'expired'])
def test_changes_after_bind_refuse_before_prefill(world, change):
    n = entry(world)
    _, members, peers = provision(world, n)
    n._bind_request_control(bind(world))
    if change == 'peer':
        peers['tail']['generation'] += 1
    elif change == 'member':
        members['entry']['verification_key_digest'] = 'sha256:' + 'b' * 64
    elif change == 'cancel':
        n._request_controls['request']['cancellation_generation'] += 1
    else:
        world.now = 16.
    with pytest.raises((RuntimeError, ValueError)):
        n._infer_start(payload(world))
    assert world.prefill == 0


def test_legacy_binding_does_not_enable_or_bypass_a_provisioned_gate(world):
    n = entry(world)
    n._bind_request_control({'request_id': 'request', 'control': deepcopy(world.control)})
    assert n.router.relay.token_path_gate is None
    with pytest.raises(RuntimeError, match='token_path_disabled'):
        n._bind_request_control(bind(world))
    assert world.prefill == 0


def test_provisioning_cannot_reset_process_replay_registry(world):
    n = entry(world)
    provision(world, n)
    n._bind_request_control(bind(world))
    with pytest.raises(RuntimeError, match='token_path_policy_already_installed'):
        provision(world, n)
    with pytest.raises((RuntimeError, ValueError), match='token_path_grant_replay'):
        n._bind_request_control(bind(world))


def test_concurrent_start_claim_is_taken_before_prefill(world):
    import threading
    n = entry(world)
    provision(world, n)
    n._bind_request_control(bind(world))
    entered, release = threading.Event(), threading.Event()
    original = n._infer_start_legacy
    errors = []
    def held(p):
        entered.set()
        assert release.wait(2)
        return original(p)
    n._infer_start_legacy = held
    def run():
        try:
            n._infer_start(payload(world))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match='token_path_grant_replay'):
            n._infer_start(payload(world))
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert world.prefill == 1
