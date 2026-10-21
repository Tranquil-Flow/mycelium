# SPDX-License-Identifier: AGPL-3.0-only
"""The product-mounted A6 fault kills or stalls real peers and reports measurements."""
from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_m16_runtime import path_manifest_document_digest
from mycelium_a6_fault_control import (
    EXACT_FLEET_GRANT,
    INJECTION_POINTS,
    FaultRejected,
)
from mycelium_a6_product_fault_control import CONTROL_PATH, ProductFaultControl
from mycelium_a6_product_fault_seams import (
    LiveProductFaultTarget,
    ProductCaseSeam,
    build_product_case_seams,
)
from mycelium_live.a6_recovery import CONTROLLED_QUALIFICATION_FAULTS, LiveA6RecoveryQualifier
from mycelium_a6_recovery import recovery_request_binding
from tests.a6_acceptance.a6_measured_stack import CASE, build_measured_stack, settle
from tests.live.test_a6_physical_qualification import _checkpoint, _manifest


class _RealPeer:
    """A real child process standing in for one node's owned process group."""

    def __init__(self, on_signal=None) -> None:
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      start_new_session=True)
        self.signals: list[int] = []
        self._on_signal = on_signal

    def _signal_process_group(self, signum: int) -> bool:
        import os
        os.killpg(self.child.pid, signum)
        self.signals.append(signum)
        if self._on_signal is not None:
            self._on_signal(signum)
        return True

    def close(self) -> None:
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=5)


class _KillableSession:
    """Route session whose sends fail exactly as a dead node's would."""

    returncode = None

    def __init__(self, peer: _RealPeer) -> None:
        self._process = peer
        self.calls: list[str] = []

    def send(self, *, command_id, command, payload):
        self.calls.append(command)
        if self._process.child.poll() is not None:
            raise RuntimeError("node_process_exited")
        return {"sent": True}


def _post(port: int, case_id: str) -> int:
    body = json.dumps({"action": "execute_controlled_source_failure",
                       "case_id": case_id}).encode("ascii")
    head = (f"POST {CONTROL_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"X-Mycelium-A6-Fleet-Grant: {EXACT_FLEET_GRANT}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.sendall(head.encode("ascii") + body)
        reply = bytearray()
        while chunk := sock.recv(4096):
            reply.extend(chunk)
    return int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])


def test_every_frozen_case_is_bound_to_one_product_seam():
    target = SimpleNamespace()
    seams = build_product_case_seams(target)  # type: ignore[arg-type]
    assert sorted(seam.case_id for seam in seams.values()) == sorted(INJECTION_POINTS)
    assert all(key == INJECTION_POINTS[seam.case_id].seam_id for key, seam in seams.items())


def test_target_refuses_without_the_exact_grant():
    with pytest.raises(FaultRejected, match="explicit_fleet_grant_required"):
        LiveProductFaultTarget(router=None, route=None, service=None, grant="nope")


@pytest.mark.parametrize("inflight", [(), ("a", "b")])
def test_arm_refuses_unless_exactly_one_request_is_in_flight(inflight):
    router = SimpleNamespace(a6_inflight_request_ids=lambda: inflight)
    target = LiveProductFaultTarget(router=router, route=None, service=None)
    with pytest.raises(FaultRejected, match="inflight_request_unavailable"):
        target.arm(CASE)


def test_signal_is_refused_when_the_node_has_no_owned_process_group():
    route = SimpleNamespace(_sessions={"node-0": SimpleNamespace(_process=None)})
    target = LiveProductFaultTarget(router=None, route=route, service=None)
    with pytest.raises(FaultRejected, match="peer_signal_refused"):
        target.signal_node("node-0", signal.SIGKILL)
    refused = SimpleNamespace(_signal_process_group=lambda _signum: False)
    route._sessions["node-0"] = SimpleNamespace(_process=refused)
    with pytest.raises(FaultRejected, match="peer_signal_refused"):
        target.signal_node("node-0", signal.SIGKILL)


def test_stalled_peers_are_always_resumed_after_the_case():
    peer = _RealPeer()
    try:
        route = SimpleNamespace(_sessions={"node-2": SimpleNamespace(_process=peer)})
        target = LiveProductFaultTarget(router=None, route=route, service=None)
        target.signal_node("node-2", signal.SIGSTOP)
        target.resume_stopped_nodes()
        assert peer.signals == [signal.SIGSTOP, signal.SIGCONT]
        assert peer.child.poll() is None
    finally:
        peer.close()


def test_positive_case_kills_the_real_source_peer_and_measures_the_route_incident(
        monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        source_node = stack.binding["source_node_id"]
        peer = _RealPeer(on_signal=lambda signum: (
            peer.child.wait(timeout=5), stack.hold.set()) if signum == signal.SIGKILL else None)
        try:
            stack.route._sessions[source_node] = _KillableSession(peer)
            target = LiveProductFaultTarget(router=stack.router, route=stack.route,
                                            service=stack.service, wait_seconds=10)
            seams = build_product_case_seams(target)
            with ProductFaultControl(grant=EXACT_FLEET_GRANT, seams=seams,
                                     product_port=8787) as control:
                assert _post(control.port, CASE) == 200
                assert _post(control.port, CASE) == 409
            assert peer.signals == [signal.SIGKILL]
            assert peer.child.returncode == -signal.SIGKILL
            assert settle(stack) is True
            (executed,) = target.executed
            assert executed.case_id == CASE and executed.request_id == stack.request_id
            state = executed.post_state
            assert state["source_failure_observed"] is True
            assert state["recovery_required"] is True
            incident = state["actual_incident"]
            assert incident["subject_id"] == source_node
            assert incident["reason"] == "node_process_exited"
            assert incident["scope"] == "request" and incident["fatal_accepted"] is False
            assert incident["incident_id"] in [
                item["incident_id"] for item in stack.route.a4_scoped_runtime_incidents()]
            assert stack.router.a6_source_failure_observed(stack.binding) is True
            # The test-only transport slot was never used for this failure.
            assert stack.route._test_only_transport_fault_consumed is False
        finally:
            stack.hold.set()
            peer.close()


def test_a_refused_signal_executes_nothing_and_records_no_case(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        source_node = stack.binding["source_node_id"]
        stack.route._sessions[source_node] = SimpleNamespace(
            returncode=None, _process=SimpleNamespace(_signal_process_group=lambda _s: False),
            send=lambda **_kwargs: {"sent": True})
        target = LiveProductFaultTarget(router=stack.router, route=stack.route,
                                        service=stack.service, wait_seconds=1)
        with ProductFaultControl(grant=EXACT_FLEET_GRANT, seams=build_product_case_seams(target),
                                 product_port=8787) as control:
            assert _post(control.port, CASE) == 403
            assert control.executed_case_ids == ()
        assert target.executed == ()
        assert settle(stack) is True
        # No peer was killed, so no dead-peer incident can exist for the request.
        assert [item for item in stack.route.a4_scoped_runtime_incidents()
                if item.get("reason") == "node_process_exited"] == []


class _ScriptedRouter:
    """Router reads whose recovery view advances only when a peer is signalled."""

    def __init__(self, binding, views):
        self._binding = binding
        self._views = list(views)
        self.current = self._views.pop(0)
        self.withheld = False

    def a6_inflight_request_ids(self):
        return (self._binding["request_id"],)

    def a6_controlled_fault_binding(self, _request_id):
        return dict(self._binding)

    def a6_source_failure_observed(self, _binding):
        return self.current.get("source_failure_observed", False)

    def a6_request_view(self, _request_id):
        return dict(self.current)

    def a6_placement_node_ids(self):
        return {"p-0": "node-0", "p-1": "node-2"}

    def a6_withhold_recovery_successors(self, _request_id):
        self.withheld = True
        return True

    def advance(self):
        if self._views:
            self.current = self._views.pop(0)


def _binding():
    identity = {"deployment_id": "d", "deployment_epoch": 1, "qualification_digest": "q",
                "request_id": "r-1", "request_attempt": 1, "path_id": "p", "path_attempt": 0,
                "path_digest": "pd", "topology_generation": 1, "command_id": "c",
                "publisher_generation": 1, "cancellation_generation": 0}
    return {**identity, "source_node_id": "node-0", "source_attempt": 1,
            "absolute_deadline_ms": 10**15, "path_manifest_digest": "pd"}


def _scripted_target(views):
    binding = _binding()
    router = _ScriptedRouter(binding, views)
    incidents: list[dict] = []

    def signal_group(signum):
        if signum == signal.SIGKILL and not incidents:
            incidents.append({"incident_id": "i-1", "request_id": "r-1", "subject_id": "node-0",
                              "scope": "request", "fatal_accepted": False,
                              "reason": "node_process_exited"})
        router.advance()
        return True

    process = SimpleNamespace(_signal_process_group=signal_group)
    route = SimpleNamespace(
        _sessions={"node-0": SimpleNamespace(_process=process),
                   "node-2": SimpleNamespace(_process=process)},
        a4_scoped_runtime_incidents=lambda: list(incidents))
    service = SimpleNamespace(terminal_event_count=lambda _rid: 0)
    return router, LiveProductFaultTarget(router=router, route=route, service=service,
                                          wait_seconds=0.2, poll_seconds=0.001)


def test_no_successor_withholds_candidates_and_reports_the_measured_abort():
    router, target = _scripted_target([
        {"recovery_phase": "active"},
        {"source_failure_observed": True, "recovery_phase": "successor_pending"},
    ])
    seam = ProductCaseSeam("no_successor", target)
    assert seam.arm(case_id="no_successor")["request_in_flight"] is True
    router.current.update(source_failure_observed=True, terminal_status="FAILED",
                          recovery_failure_reason="recovery_successor_unavailable")
    router._views = []
    state = seam.apply(case_id="no_successor")
    assert router.withheld is True
    assert state == {"source_failure_observed": True, "candidate_available": False,
                     "truthful_abort_required": True}


def test_no_successor_reports_available_candidates_when_recovery_did_not_refuse():
    router, target = _scripted_target([
        {"recovery_phase": "active"},
        {"source_failure_observed": True, "terminal_status": "COMPLETED",
         "recovery_failure_reason": None},
    ])
    seam = ProductCaseSeam("no_successor", target)
    seam.arm(case_id="no_successor")
    state = seam.apply(case_id="no_successor")
    assert state["candidate_available"] is True and state["truthful_abort_required"] is False


def test_unobserved_source_failure_is_refused_not_reported():
    router, target = _scripted_target([{"recovery_phase": "active"}])
    router.a6_source_failure_observed = lambda _binding: False
    seam = ProductCaseSeam(CASE, target)
    seam.arm(case_id=CASE)
    with pytest.raises(FaultRejected, match="source_failure_unobserved"):
        seam.apply(case_id=CASE)
    assert target.executed == ()


def _qualifier(tmp_path: Path, calls: list) -> LiveA6RecoveryQualifier:
    deployment = tmp_path / "deployment"
    deployment.mkdir(exist_ok=True)
    (deployment / "config.json").write_text('{"model_type":"qwen2","hidden_size":16}\n')
    (deployment / "tokenizer.json").write_text('{"version":"1.0","added_tokens":[]}\n')
    checkpoint = _checkpoint()

    def challenge(**kwargs):
        calls.append(kwargs)
        return {"passed": True, "parity_policy_digest": "sha256:" + "1" * 64,
                "workload_envelope_digest": "sha256:" + "2" * 64}

    graph = SimpleNamespace(deployment_id="deployment-a6", deployment_epoch=7,
                            topology_version=11, model_id="model-a6",
                            resolved_commit="immutable-model-revision",
                            manifest_digest=checkpoint.representation_digest)
    return LiveA6RecoveryQualifier(graph=graph, deployment_dir=deployment,
                                   tokenizer_digest=checkpoint.tokenizer_digest,
                                   physical_challenge=challenge, clock_ms=lambda: 2_000)


def _qualify(qualifier):
    checkpoint = _checkpoint()
    identity = {"request_id": checkpoint.request_id, "request_attempt": 2,
                "path_id": "successor-path", "path_attempt": 1,
                "path_manifest_digest": path_manifest_document_digest(_manifest()),
                "deployment_id": "deployment-a6", "deployment_epoch": 7,
                "topology_generation": 11}
    return checkpoint, qualifier(checkpoint.request_id, checkpoint, identity, _manifest())


def test_unarmed_qualifier_issues_an_eligible_record_and_its_measured_facts(tmp_path):
    calls: list = []
    qualifier = _qualifier(tmp_path, calls)
    checkpoint, (source, qualification) = _qualify(qualifier)
    assert qualification["compatible"] is True and qualification["physical_challenge"] == "passed"
    facts = qualifier.issued_qualification_facts(checkpoint.request_id)
    assert facts["controlled_fault"] is None and facts["request_binding_matches"] is True
    assert facts["successor_placement_ids"] == ["successor-placement"]
    assert len(calls) == 1


def test_qualification_faults_need_the_grant_and_are_one_shot(tmp_path):
    qualifier = _qualifier(tmp_path, [])
    with pytest.raises(PermissionError):
        qualifier.arm_controlled_fault(request_id="r", kind="stale", grant="nope")
    with pytest.raises(ValueError):
        qualifier.arm_controlled_fault(request_id="r", kind="approve", grant=EXACT_FLEET_GRANT)
    qualifier.arm_controlled_fault(request_id="r", kind="stale", grant=EXACT_FLEET_GRANT)
    with pytest.raises(ValueError):
        qualifier.arm_controlled_fault(request_id="r", kind="foreign", grant=EXACT_FLEET_GRANT)


@pytest.mark.parametrize("kind", CONTROLLED_QUALIFICATION_FAULTS)
def test_each_qualification_fault_issues_a_record_the_authority_must_refuse(tmp_path, kind):
    calls: list = []
    qualifier = _qualifier(tmp_path, calls)
    request_id = _checkpoint().request_id
    qualifier.arm_controlled_fault(request_id=request_id, kind=kind, grant=EXACT_FLEET_GRANT)
    checkpoint, (source, qualification) = _qualify(qualifier)
    facts = qualifier.issued_qualification_facts(request_id)
    assert facts["controlled_fault"] == kind
    if kind == "incompatible":
        assert qualification["compatible"] is False
        assert qualification["compatibility"] != source
        assert facts["compatibility_matches_source"] is False
    elif kind == "stale":
        assert qualification["expiry_unix_ms"] <= facts["measured_at_unix_ms"]
    elif kind == "foreign":
        assert qualification["request_binding"] != recovery_request_binding(checkpoint)
        assert facts["request_binding_matches"] is False
    elif kind == "unexecuted":
        assert qualification["physical_challenge"] == "unexecuted" and calls == []
    else:
        assert qualification["successor_binding"]["path_attempt"] != _manifest().path_attempt
        assert facts["issued_path_attempt"] != facts["successor_path_attempt"]
    # One-shot: the next issue for the same request is unperturbed.
    _checkpoint_again, (_source, again) = _qualify(qualifier)
    assert again["compatible"] is True and again["physical_challenge"] == "passed"
    assert qualifier.issued_qualification_facts(request_id)["controlled_fault"] is None


def test_timeout_seam_waits_are_bounded_and_resume_stalled_peers():
    router, target = _scripted_target([
        {"recovery_phase": "active"},
        {"source_failure_observed": True, "recovery_phase": "successor_pending"},
    ])
    started = time.monotonic()
    seam = ProductCaseSeam("timeout_during_recovery", target)
    seam.arm(case_id="timeout_during_recovery")
    state = seam.apply(case_id="timeout_during_recovery")
    assert state == {"source_failure_observed": True, "recovery_deadline_exceeded": False}
    assert time.monotonic() - started < 5
