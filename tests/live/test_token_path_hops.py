# SPDX-License-Identifier: AGPL-3.0-only
"""Real relay ingress/runtime fences with private signing and fake compute."""
from dataclasses import replace
import importlib
from types import SimpleNamespace

import pytest

from mycelium_router.contracts import HopHeader, RouterConfig
from mycelium_router.fakes import (FakeCapacityPort, FakeDeviceStateProvider,
    FakeRuntimePort, FakeTopologyProvider, FakeTransportPort, ManualClock, SequenceIdSource)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from mycelium_router.serialization import path_manifest_from_dict
from tests.live.test_token_path_admission import world, policy, real_prefill_node


def api():
    return importlib.import_module('mycelium_router.token_hop_authority')


def setup(w, *, install=True):
    n = real_prefill_node(w)
    manifest = path_manifest_from_dict(w.manifest)
    runtime, transport = FakeRuntimePort(), FakeTransportPort()
    router = Router(node_id='tail', topology=FakeTopologyProvider(n.graph),
        device_states=FakeDeviceStateProvider({}), capacity=FakeCapacityPort(),
        runtime=runtime, transport=transport, clock=ManualClock(),
        id_source=SequenceIdSource(), config=RouterConfig())
    from mycelium_router.contracts import RequestContext
    assert router.register_path(RequestContext(**{**w.request, 'prompt_token_ids': (10, 11)}),
        manifest, n.graph, entry_node_id='entry')
    peers = {p['node_id']: dict(p) for p in w.pins['participants']}
    current = [True]
    gate = api().GrantedHopGate(node_id='tail', clock=lambda: w.now,
                               peers=lambda: peers)
    router.relay.token_path_gate = gate
    grant = policy(w).admit(w.grant, request=w.request, bound_control=w.control)
    if install:
        gate.install(grant=grant, request=w.request, manifest=manifest,
                     graph=n.graph, current=lambda: current[0])
    return SimpleNamespace(router=router, runtime=runtime, transport=transport,
        gate=gate, current=current, peers=peers, grant=grant, manifest=manifest, graph=n.graph)


def header(w, phase='PREFILL', index=-1, **changes):
    values = dict(request_id='request', path_id='path', path_attempt=0,
        phase=phase, token_index=index, hop_index=1,
        source_placement_id='entry-stage-000', destination_placement_id='tail-stage-001',
        topology_version=1, prefill_chunk_token_count=0)
    values.update(changes)
    values['idempotency_key'] = hop_idempotency_key(**{k: values[k] for k in
        ('request_id', 'path_id', 'path_attempt', 'phase', 'token_index', 'hop_index')})
    return HopHeader(**values)


@pytest.mark.parametrize('queued', [False, True])
def test_required_grant_refuses_unknown_before_compute(world, queued):
    s = setup(world, install=False)
    method = s.router.enqueue_hop if queued else s.router.receive_hop
    result = method(header(world), b'activation', source_node_id='entry')
    assert (result.disposition, result.reason) == ('REJECTED', 'token_path_grant_missing')
    assert not s.runtime.executed and s.router.pending_batch_hops() == 0


@pytest.mark.parametrize('queued', [False, True])
def test_bound_remote_hops_execute_existing_runtime(world, queued):
    s = setup(world)
    method = s.router.enqueue_hop if queued else s.router.receive_hop
    for phase, index in [('PREFILL', -1), ('DECODE', 1), ('DECODE', 2)]:
        result = method(header(world, phase, index), b'activation', source_node_id='entry')
        if queued:
            assert result.disposition == 'QUEUED'
            s.router.drain_ready_batches(force=True)
        else:
            assert result.disposition == 'COMPLETED'
    assert len(s.runtime.executed) == 3


@pytest.mark.parametrize('change,code', [('expired', 'token_path_grant_expired'),
    ('cancelled', 'token_path_grant_stale'), ('peer', 'token_path_peer_stale'),
    ('endpoint', 'token_path_peer_stale'), ('source', 'token_path_peer_stale'),
    ('topology', 'token_path_hop_mismatch'), ('path', 'token_path_hop_mismatch'),
    ('budget', 'token_path_hop_budget'), ('verify', 'token_path_hop_phase')])
def test_remote_ingress_is_fenced_before_queue_or_compute(world, change, code):
    s = setup(world)
    h = header(world)
    source = 'entry'
    if change == 'expired':
        world.now = 16.
    elif change == 'cancelled':
        s.current[0] = False
    elif change == 'peer':
        s.peers['entry']['generation'] += 1
    elif change == 'endpoint':
        s.peers['entry']['endpoint_id'] = 'foreign'
    elif change == 'source':
        source = 'intruder'
    elif change == 'topology':
        h = header(world, topology_version=2)
    elif change == 'path':
        h = header(world, path_attempt=1)
    elif change == 'budget':
        h = header(world, 'DECODE', 6)
    else:
        h = header(world, 'VERIFY', 1)
    result = s.router.enqueue_hop(h, b'activation', source_node_id=source)
    assert (result.disposition, result.reason) == ('REJECTED', code)
    assert not s.runtime.executed and s.router.pending_batch_hops() == 0


@pytest.mark.parametrize('change', ['cancelled', 'expired', 'peer'])
def test_queued_work_rechecks_authority_at_runtime_boundary(world, change):
    s = setup(world)
    assert s.router.enqueue_hop(header(world), b'activation', source_node_id='entry').disposition == 'QUEUED'
    if change == 'cancelled':
        s.current[0] = False
    elif change == 'expired':
        world.now = 16.
    else:
        s.peers['entry']['generation'] += 1
    s.router.drain_ready_batches(force=True)
    assert not s.runtime.executed


def test_hop_index_gaps_and_recompute_are_refused(world):
    s = setup(world)
    s.router.receive_hop(header(world), b'activation', source_node_id='entry')
    result = s.router.receive_hop(header(world, 'DECODE', 2), b'activation', source_node_id='entry')
    assert result.reason == 'token_path_hop_order'
    assert len(s.runtime.executed) == 1
    # Existing transport idempotency may return its cached result; it cannot
    # execute the same work twice even after that cache has been retired.
    item = s.runtime.executed[0]
    assert s.router.relay._execute_runtime(item).failure_reason == 'token_path_hop_order'
    assert len(s.runtime.executed) == 1


def test_default_relay_has_no_grant_gate(world):
    s = setup(world)
    s.router.relay.token_path_gate = None
    result = s.router.receive_hop(header(world), b'activation', source_node_id='entry')
    assert result.disposition == 'COMPLETED' and len(s.runtime.executed) == 1


@pytest.mark.parametrize('queued', [False, True])
def test_revocation_during_compute_never_forwards_output(world, queued):
    s = setup(world)
    original = s.runtime.execute
    def revoked(item):
        result = original(item)
        s.current[0] = False
        return result
    s.router.receive_hop(header(world), b'activation', source_node_id='entry')
    s.runtime.execute = revoked
    method = s.router.enqueue_hop if queued else s.router.receive_hop
    result = method(header(world, 'DECODE', 1), b'activation', source_node_id='entry')
    if queued:
        results = s.router.drain_ready_batches(force=True)
        assert results[0].reason == 'token_path_grant_stale'
    else:
        assert result.reason == 'token_path_grant_stale'
    assert len(s.runtime.executed) == 2
    assert not s.transport.token_events


def test_install_is_bounded_and_cannot_reset_position(world):
    s = setup(world)
    with pytest.raises(ValueError, match='token_path_grant_replay'):
        s.gate.install(grant=s.grant, request=world.request, manifest=s.manifest,
                       graph=s.graph, current=lambda: True)


def test_install_does_not_accept_another_path_or_model(world):
    s = setup(world, install=False)
    with pytest.raises(ValueError, match='token_path_hop_mismatch'):
        s.gate.install(grant=s.grant, request=world.request,
            manifest=replace(s.manifest, path_attempt=1), graph=s.graph, current=lambda: True)
    with pytest.raises(ValueError, match='token_path_hop_mismatch'):
        s.gate.install(grant=s.grant, request=world.request,
            manifest=s.manifest, graph=replace(s.graph, manifest_digest='sha256:'+'f'*64),
            current=lambda: True)
