# SPDX-License-Identifier: AGPL-3.0-only
"""In-process ordinary gateway/router/route composition for measured-seam tests.

Extracted verbatim from ``tests/live/test_a6_live_fault_seams.stack`` so an
owned ``tests/a6_acceptance`` suite can drive the same real composition without
modifying another lane's file.  This is a controlled local session, never a
physical or browser qualification.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from types import SimpleNamespace

from mycelium_a6_live_fault_seams import MeasuredSourceFailureSeam
from mycelium_live import route as route_module
from mycelium_live.liveness import LivenessSubject, SubjectKind
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from tests.live.test_a6_live_router_recovery import _runtime
from tests.request_gateway.test_core import _synthetic_qualification

CASE = "ordinary_browser_source_failure_recovery"


def settle(stack, timeout: float = 8.0) -> bool:
    """Release the source and wait for the ordinary request to reach terminal.

    A controlled local composition that is torn down while its request is still
    admitted leaves parked non-daemon worker threads, and the interpreter then
    waits for them at shutdown.  Every test here settles its request first.
    """
    stack.hold.set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = stack.service._sessions.get(stack.request_id)
        if session is not None and (session.terminal_event is not None or session.worker_done):
            return True
        time.sleep(0.02)
    return False


class _Session:
    returncode = None

    def __init__(self):
        self.calls = []

    def send(self, *, command_id, command, payload):
        self.calls.append(command)
        return {"sent": True}


@contextlib.contextmanager
def build_measured_stack(monkeypatch, tmp_path):
    qualification = _synthetic_qualification()
    monkeypatch.setattr(route_module, "execution_graph_from_document", lambda _: SimpleNamespace())
    route = route_module.PhysicalLiveRoute(
        controller=SimpleNamespace(peers=()),
        endpoints={},
        run_plan={"run_id": "simulated-session",
                  "deployment_id": qualification.deployment_id, "request": {},
                  "nodes": [{"node_id": "unused", "configure": {"graph": {}}}]},
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
    source = _Session()
    route._sessions = {"source": source}
    route._reject_if_affected_peer_quarantined = lambda _: None
    route._peer_counters = lambda: (reached.set(), hold.wait(timeout=3), {})[-1]
    route._verify_observation = lambda *_args, **_kwargs: {}
    route._authorize_failed_request_cleanup = lambda *_args, **_kwargs: time.monotonic() + 1
    route._wait_for_cancellation_cleanup = lambda *_args, **_kwargs: None
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
    node = route._graph.stages[0].placements[0].node_id
    route._sessions = {node: source, **{p.node_id: _Session() for stage in route._graph.stages
                                        for p in stage.placements if p.node_id != node}}
    # Match the local session transport contract exercised by infer().
    route._peers = {n: SimpleNamespace(node_id=n, process_transport="local") for n in route._sessions}
    route._liveness_subjects[node] = subject
    seam = MeasuredSourceFailureSeam(service=service, router=router, route=route,
                                     expected_request_id="live-a6-001", clock_monotonic=time.monotonic,
                                     release_source=hold.set)
    stack = SimpleNamespace(coordinator=coordinator, router=router, service=service,
                            route=route, seam=seam, hold=hold, source=source,
                            request_id=None, binding=None,
                            qualification=qualification)
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification)))
        assert reached.wait(timeout=3), "ordinary route did not reach the source command boundary"
        binding = router._a6_test_only_source_fault_binding(request_id)
        active = route._active_route_requests.get(request_id)
        assert binding is not None and active is not None
        assert active["entry_node_id"] == binding["source_node_id"], (active, binding)
        stack.request_id = request_id
        stack.binding = binding
        yield stack
    finally:
        settle(stack)
        service.close()
        try:
            router.close(timeout_seconds=4.0)
        except (AttributeError, TypeError, RuntimeError):
            pass
        route._cleanup_reconciliation_stop.set()
        route._cleanup_reconciliation_executor.shutdown(wait=True, cancel_futures=True)
