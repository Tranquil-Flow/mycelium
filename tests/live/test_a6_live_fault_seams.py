# SPDX-License-Identifier: AGPL-3.0-only
"""In-process controlled session; NOT a physical or browser qualification."""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a6_fault_control import (
    ControlledFaultAuthority, ControlledFaultMechanism, EXECUTE_SOURCE_FAILURE_ACTION,
    FaultRejected, INJECTION_POINTS,
)
from mycelium_a6_live_fault_seams import MeasuredSourceFailureSeam
from mycelium_live import route as route_module
from mycelium_live.liveness import LivenessSubject, SubjectKind
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from tests.live.test_a6_live_router_recovery import _runtime
from tests.request_gateway.test_core import _synthetic_qualification

CASE = "ordinary_browser_source_failure_recovery"

class Session:
    returncode = None
    def __init__(self):
        self.calls = []
    def send(self, *, command_id, command, payload):
        self.calls.append(command)
        return {"sent": True}

@pytest.fixture
def stack(monkeypatch, tmp_path):
    qualification = _synthetic_qualification()
    monkeypatch.setattr(route_module, "execution_graph_from_document", lambda _: SimpleNamespace())
    route = route_module.PhysicalLiveRoute(
        controller=SimpleNamespace(peers=()), endpoints={},
        run_plan={"run_id": "simulated-session", "deployment_id": qualification.deployment_id,
                  "request": {}, "nodes": [{"node_id": "unused", "configure": {"graph": {}}}]},
        _test_only_transport_fault=route_module._TestOnlyTransportFaultSlot(),
    )
    if os.environ.get("A6_TEST_MUTATION_DISABLE_FAULT") == "1":
        slot = route._test_only_transport_fault
        assert isinstance(slot, route_module._TestOnlyTransportFaultSlot)
        slot.disabled = True
    hold = threading.Event()
    reached = threading.Event()
    route._open = True
    route._deployment_qualification = qualification
    source = Session()
    route._sessions = {"source": source}
    route._reject_if_affected_peer_quarantined = lambda _: None
    route._peer_counters = lambda: (reached.set(), hold.wait(timeout=3), {})[-1]
    route._verify_observation = lambda *_args, **_kwargs: {}
    route._authorize_failed_request_cleanup = lambda *_args, **_kwargs: time.monotonic() + 1
    route._wait_for_cancellation_cleanup = lambda *_args, **_kwargs: None
    # Synthetic session teardown only: retain the route's real publication and
    # command-ledger CAS path, but replace signed physical proof with a labelled
    # fixture receipt. This is NOT a physical cleanup or A6 gate result.
    route._cancellation_cleanup_complete = lambda *_args, **_kwargs: True
    def fixture_receipt(*, request_id, participating_node_ids, **_kwargs):
        active = route._active_route_requests[request_id]
        return {**{key: active[key] for key in (
            "deployment_id", "deployment_epoch", "qualification_digest",
            "request_attempt", "path_id", "path_attempt", "path_digest",
            "topology_generation", "command_id", "publisher_generation",
            "cancellation_generation")}, "request_id": request_id,
            "cleanup_owner_id": f"physical-live-route:{active['deployment_id']}",
            "node_ids": sorted(participating_node_ids), "simulation_only": True}
    route._store_request_cleanup_receipt = fixture_receipt
    subject = LivenessSubject("source", SubjectKind.PEER, 1)
    route._liveness.register_subject(subject, observed_at_ms=int(time.monotonic() * 1000))
    route._liveness_subjects["source"] = subject
    coordinator, router, service, qualification = _runtime(tmp_path, route, qualification=qualification)
    route._graph = router.current_deployment()
    # Only the assigned entry placement runs a controlled local transport session.
    node = route._graph.stages[0].placements[0].node_id
    route._sessions = {node: source, **{p.node_id: Session() for stage in route._graph.stages
                                          for p in stage.placements if p.node_id != node}}
    # Match the local session transport contract exercised by infer().
    route._peers = {n: SimpleNamespace(node_id=n, process_transport="local") for n in route._sessions}
    route._liveness_subjects[node] = subject
    seam = MeasuredSourceFailureSeam(service=service, router=router, route=route,
                                     expected_request_id="live-a6-001", clock_monotonic=time.monotonic,
                                     release_source=hold.set)
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification)))
        assert reached.wait(timeout=3), "ordinary route did not reach the source command boundary"
        binding = router._a6_test_only_source_fault_binding(request_id)
        active = route._active_route_requests.get(request_id)
        assert binding is not None and active is not None
        assert active["entry_node_id"] == binding["source_node_id"], (active, binding)
        assert all(active.get(k) == binding[k] for k in (
            "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
            "request_attempt", "path_id", "path_attempt", "path_digest",
            "topology_generation", "command_id", "publisher_generation",
            "cancellation_generation") if k != "request_id"), (active, binding)
        yield service, router, route, source, hold, seam, request_id
    finally:
        hold.set()
        service.close()
        route._cleanup_reconciliation_stop.set()
        route._cleanup_reconciliation_executor.shutdown(wait=True, cancel_futures=True)

def mechanism(seam):
    return ControlledFaultMechanism(
        seams={INJECTION_POINTS[CASE].seam_id: seam},
        authority=ControlledFaultAuthority(grant="FLEET GRANTED TO A6", caller_host="127.0.0.1"),
        clock_ms=lambda: 1000,
    )

def test_measured_gateway_to_route_incident_and_no_hook_control(stack):
    service, router, route, source, hold, seam, request_id = stack
    before = seam.arm(case_id=CASE)
    assert before["admitted_through_ordinary_gateway"] is True
    assert before["request_in_flight"] is True
    assert before["source_attempt"] == 1
    assert before["request_id"] == request_id
    assert before["command_id"].startswith("request:")
    result = mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    incident = result.post_state["actual_incident"]
    for key in ("deployment_id", "deployment_epoch", "qualification_digest", "request_id",
                "request_attempt", "path_id", "path_attempt", "path_digest",
                "topology_generation", "command_id", "publisher_generation",
                "cancellation_generation"):
        assert incident[key] == before[key]
    assert incident["reason"] == "transport_not_running"
    assert incident["subject_id"] == before["source_node_id"]
    assert incident["fatal_accepted"] is False
    assert result.post_state["source_failure_observed"] is True
    assert result.post_state["recovery_required"] is True
    assert "infer_start" not in source.calls
    assert route.a4_scoped_runtime_incidents()[-1] == incident
    # Successor is never paused to manufacture an observable transient status.
    assert router._a6_test_only_source_failure_observed(before)
    with pytest.raises(FaultRejected):
        mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)

def test_incident_before_install_returns_is_not_missed(stack, monkeypatch):
    service, router, route, source, hold, seam, request_id = stack
    before = seam.arm(case_id=CASE)
    incident_written = threading.Event()
    real_record = route._record_scoped_runtime_incident
    def record(*args, **kwargs):
        real_record(*args, **kwargs)
        if kwargs.get("request_id") == request_id and kwargs.get("reason") == "transport_not_running":
            incident_written.set()
    monkeypatch.setattr(route, "_record_scoped_runtime_incident", record)
    real_install = route._install_test_only_transport_fault
    def install_and_let_incident_overtake(fault):
        installed = real_install(fault)
        if installed:
            hold.set()
            assert incident_written.wait(timeout=3), "actual fault did not write its incident"
        return installed
    monkeypatch.setattr(route, "_install_test_only_transport_fault", install_and_let_incident_overtake)
    result = mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    assert result.post_state["actual_incident"]["request_id"] == before["request_id"]
    assert result.post_state["recovery_required"] is True
    assert "infer_start" not in source.calls

def test_successor_can_settle_before_measurement_without_changing_source_verdict(stack, monkeypatch):
    service, router, route, source, hold, seam, request_id = stack
    settled = threading.Event()
    real_recover = router.recover_full_context
    def observed_recover(*args, **kwargs):
        try:
            return real_recover(*args, **kwargs)
        finally:
            settled.set()
    monkeypatch.setattr(router, "recover_full_context", observed_recover)
    real_install = route._install_test_only_transport_fault
    def install_after_successor(fault):
        installed = real_install(fault)
        if installed:
            hold.set()
            assert settled.wait(timeout=3), "ordinary successor did not settle"
            assert router.request_status(request_id) != "RECOVERY_REQUIRED"
        return installed
    monkeypatch.setattr(route, "_install_test_only_transport_fault", install_after_successor)
    result = mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    assert result.post_state["recovery_required"] is True
    assert result.post_state["actual_incident"]["request_id"] == request_id
    assert "infer_start" not in source.calls

def test_incident_without_router_recovery_transition_is_not_a_success(stack, monkeypatch):
    service, router, route, source, hold, seam, request_id = stack
    seam.arm(case_id=CASE)
    def fail_command_terminal(*args, **kwargs):
        raise RuntimeError("cleanup_authority_failed")
    monkeypatch.setattr(router, "_record_command_terminal", fail_command_terminal)
    with pytest.raises(FaultRejected, match="actual_incident_missing"):
        mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    assert any(item["reason"] == "transport_not_running"
               for item in route.a4_scoped_runtime_incidents())
    assert route._test_only_transport_fault_consumed is True
    assert router._a6_test_only_source_failure_observed(seam._armed_identity) is False

def test_wrong_foreign_expired_terminal_or_revoked_refuse_without_arming(stack):
    service, router, route, source, hold, seam, request_id = stack
    for replacement in ("foreign-request", "live-a6-002"):
        foreign = MeasuredSourceFailureSeam(service=service, router=router, route=route,
                                             expected_request_id=replacement)
        with pytest.raises(FaultRejected):
            foreign.arm(case_id=CASE)
    assert route._test_only_transport_fault.fault is None
    expired = MeasuredSourceFailureSeam(service=service, router=router, route=route,
                                         expected_request_id=request_id,
                                         clock_monotonic=lambda: float("inf"))
    with pytest.raises(FaultRejected):
        expired.arm(case_id=CASE)
    assert route._test_only_transport_fault.fault is None
    original = route._deployment_qualification
    route._deployment_qualification = SimpleNamespace(route_ready=False)
    with pytest.raises(FaultRejected):
        seam.arm(case_id=CASE)
    route._deployment_qualification = original
    assert route._test_only_transport_fault.fault is None

def test_disabled_fault_positive_control_cannot_pass_mechanism(stack):
    service, router, route, source, hold, seam, request_id = stack
    seam.arm(case_id=CASE)
    seam._release_source = lambda: (setattr(route._test_only_transport_fault, "disabled", True), hold.set())
    with pytest.raises(FaultRejected, match="actual_incident"):
        mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    assert not any(incident["reason"] == "transport_not_running"
                   for incident in route.a4_scoped_runtime_incidents())

@pytest.mark.parametrize("mutation", ["terminal", "cancelled", "path", "attempt",
                                       "command", "publisher", "source", "gateway_terminal"])
def test_retired_or_mismatched_binding_refuses_before_any_effect(stack, mutation):
    service, router, route, source, hold, seam, request_id = stack
    active = route._active_route_requests[request_id]
    if mutation == "gateway_terminal":
        service._sessions[request_id].terminal_count = 1
    elif mutation == "terminal":
        active["terminal"] = True
    elif mutation == "cancelled":
        active["cancellation_requested"] = True
    elif mutation == "source":
        active["entry_node_id"] = "foreign-node"
    else:
        key = {"path": "path_id", "attempt": "request_attempt",
               "command": "command_id", "publisher": "publisher_generation"}[mutation]
        active[key] = "foreign" if isinstance(active[key], str) else active[key] + 1
    with pytest.raises(FaultRejected):
        mechanism(seam).execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=CASE)
    assert route._test_only_transport_fault.fault is None
    assert route._test_only_transport_fault_consumed is False
    assert "infer_start" not in source.calls
