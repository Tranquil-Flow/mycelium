# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import replace
import pytest

from mycelium_router.contracts import FailureReport, RouterConfig
from mycelium_router.fakes import (ManualClock, FakeTopologyProvider, FakeDeviceStateProvider,
    FakeCapacityPort, FakeRuntimePort, FakeTransportPort, InMemoryClientSink, SequenceIdSource)
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


def world():
    graph = graph_fixture()
    graph = replace(graph, stages=tuple(replace(s, placements=tuple(
        replace(p, runtime_backend='mobile-rust-qwen2') if p.node_id == 'node-c' else p
        for p in s.placements)) for s in graph.stages))
    clock = ManualClock()
    router = Router(node_id='node-a', topology=FakeTopologyProvider(graph),
        device_states=FakeDeviceStateProvider(state_table()), capacity=FakeCapacityPort(clock=clock),
        runtime=FakeRuntimePort(), transport=FakeTransportPort(), clock=clock,
        id_source=SequenceIdSource(), config=RouterConfig(maximum_recovery_attempts=1))
    req = request_fixture(request_id='thermal', prompt_token_ids=(11,12), max_new_tokens=4, expected_new_tokens=4)
    router.admit(req, InMemoryClientSink(), excluded_placements=frozenset({'node-b-stage-000'}))
    record = router.get_request(req.request_id)
    placement = next(p for s in graph.stages for p in s.placements if p.node_id == 'node-c'
                     and p.placement_id in {h.placement_id for h in record.manifest.ordered_hops})
    report = FailureReport(request_id=req.request_id, path_id=record.manifest.path_id,
        path_attempt=record.manifest.path_attempt, token_index=len(record.generated_token_ids),
        scope='PLACEMENT', reason='thermal_ceiling_reached', placement_id=placement.placement_id, node_id='node-c')
    return router, clock, report, graph


def test_authenticated_thermal_failure_excludes_phone_until_deadline(monkeypatch):
    from mycelium_deadlines import PHONE_THERMAL_COOLDOWN_SECONDS
    router, clock, report, graph = world()
    entry = router.entry
    assert entry._phone_thermal_exclusions() == frozenset()
    assert router.receive_failure_report(report, source_node_id='attacker') is False
    assert entry._phone_thermal_exclusions() == frozenset()
    router.receive_failure_report(report, source_node_id='node-c')
    assert entry._phone_thermal_exclusions() == frozenset({'node-c'})
    seen = []
    def capture(*args, **kw):
        seen.append(kw['excluded_devices'])
        raise RuntimeError('captured-plan')
    monkeypatch.setattr(entry.builder, 'start', capture)
    # Both ordinary and distributed admission use the same cooldown, not just recovery.
    for distributed in (False, True):
        req = request_fixture(request_id='next-'+str(distributed))
        with pytest.raises(RuntimeError, match='captured-plan'):
            (entry.start_distributed_prefill if distributed else entry.admit)(req, InMemoryClientSink())
        assert 'node-c' in seen[-1]
    clock.advance(PHONE_THERMAL_COOLDOWN_SECONDS - 0.01)
    assert entry._phone_thermal_exclusions() == frozenset({'node-c'})
    clock.advance(0.01)
    assert entry._phone_thermal_exclusions() == frozenset()
    with pytest.raises(RuntimeError, match='captured-plan'):
        entry.admit(request_fixture(request_id='cooled'), InMemoryClientSink())
    assert not seen[-1]
