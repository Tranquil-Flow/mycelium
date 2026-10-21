# SPDX-License-Identifier: AGPL-3.0-only
"""Pre-physical P3: A6 successor cases on the run-day three-placement topology.

The run-day fleet serves three complete-model placements (node-0, node-2 and
node-3). Every A6 case runs on that same plan, so case 8
(``successor_prefill_failure``) and case 12 (``exhausted_recovery_attempts``)
must both hold on it:

* case 8 fails the first successor during its recovery prefill and must end
  fail-closed: the product may not cut over to a second successor;
* case 12 kills two successors and must still find its second one.

The composition is the real gateway, router port, M16 coordinator, recovery
authority and production qualifier with the real ``ProductCaseSeam``; only
the route is simulated (a killed node's command fails as a dead node's would).
The observation is derived and validated by the source-owned writer and the
trusted case validator. This is a local check, never physical evidence.
"""
from __future__ import annotations

import itertools
import signal
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from mycelium_a6_case_observations import CaseObservationRecorder, derive_observation
from mycelium_a6_fault_control import EXACT_FLEET_GRANT
from mycelium_a6_product_fault_seams import LiveProductFaultTarget, ProductCaseSeam
from mycelium_router.contracts import LayerRange, PlacementEdge, Stage, StageCost
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from scripts import run_a6_recovery_case as runner
from tests.live.test_a6_live_router_recovery import _runtime
from tests.live.test_a6_m16_recovery import _graph, _placement

NODES = ("node-0", "node-2", "node-3")
_TERMINALS = {"completed", "failed", "cancelled"}
#: How long an unkilled successor runs before it completes by itself.
_SUCCESSOR_RUN_SECONDS = 3.0


def three_placement_graph():
    """One stage holding the whole model three times, as the run-day plan does."""

    base = _graph()
    placements = tuple(_placement(f"placement-{index}", node)
                       for index, node in enumerate(NODES))
    stage = Stage(
        "replica-stage-000",
        LayerRange(0, 8, 8),
        ("input_embedding", "decoder", "lm_head"),
        StageCost(1.0, 1.0, 64),
        placements,
    )
    return replace(
        base,
        entry_stage_id=stage.stage_id,
        final_stage_id=stage.stage_id,
        stages=(stage,),
        edges=(),
        loopback_edges=tuple(
            PlacementEdge(f"loopback:{p.placement_id}", p.placement_id, p.placement_id,
                          f"local:{p.node_id}")
            for p in placements),
    )


class _Process:
    def __init__(self, route: "ReplicaRoute", node_id: str) -> None:
        self._route, self._node_id = route, node_id

    def _signal_process_group(self, signum: int) -> bool:
        self._route.signals.append((self._node_id, signum))
        if signum == signal.SIGKILL:
            self._route.killed[self._node_id].set()
        return True


class ReplicaRoute:
    """Simulated three-replica route whose killed nodes fail their command."""

    is_simulated = True

    def __init__(self, graph) -> None:
        self._nodes = {p.placement_id: p.node_id
                       for stage in graph.stages for p in stage.placements}
        self.killed = {node: threading.Event() for node in self._nodes.values()}
        self.signals: list[tuple[str, int]] = []
        self.calls: list[dict] = []
        self._scoped_runtime_incidents: list[dict] = []
        self._incident_ids = itertools.count(1)
        self._sessions = {node: SimpleNamespace(_process=_Process(self, node))
                          for node in self._nodes.values()}

    def a4_scoped_runtime_incidents(self):
        return tuple(dict(item) for item in self._scoped_runtime_incidents)

    def is_alive(self) -> bool:
        return True

    def infer(self, prompt_token_ids, *, max_new_tokens, request_id, sink,
              route_identity, locked_path_manifest, command_identity, **_kwargs):
        nodes = sorted({self._nodes[hop["placement_id"]]
                        for hop in locked_path_manifest["ordered_hops"]})
        self.calls.append({"nodes": nodes, "route_identity": dict(route_identity)})
        source = len(self.calls) == 1
        if source:
            sink.emit(0, 101)
            sink.emit(1, 102)
            wait = 30.0
        else:
            sink.emit(0, 103)
            wait = _SUCCESSOR_RUN_SECONDS
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            dead = [node for node in nodes if self.killed[node].is_set()]
            if dead:
                identity = {key: value for key, value in command_identity.items()
                            if key != "absolute_deadline_ms"}
                self._scoped_runtime_incidents.append({
                    **identity, "incident_id": f"incident-{next(self._incident_ids)}",
                    "request_id": request_id, "subject_id": dead[0], "scope": "request",
                    "fatal_accepted": False, "state": "failed",
                    "reason": "node_process_exited"})
                raise RuntimeError("node_process_exited")
            time.sleep(0.005)
        if source:
            raise AssertionError("the source was never killed")
        for index in range(1, max_new_tokens):
            sink.emit(index, 103 + index)

    def release_request(self, _request_id: str) -> None:
        return None

    def cancel_request(self, _request_id: str, **_kwargs) -> bool:
        return True

    def close(self) -> None:
        return None


def _run_case(tmp_path, case_id: str):
    graph = three_placement_graph()
    route = ReplicaRoute(graph)
    coordinator, router, service, qualification = _runtime(
        tmp_path, route, base_graph=graph, production_qualifier=True)
    target = LiveProductFaultTarget(
        router=router, route=route, service=service,
        qualifier=router._recovery_qualification_source,
        grant=EXACT_FLEET_GRANT, wait_seconds=10.0, poll_seconds=0.002)
    events: list = []
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification)))
        subscription = service.subscribe(request_id, last_event_id=None)

        def drain() -> None:
            try:
                while True:
                    event = subscription.next_event(timeout=30)
                    if event is None:
                        return
                    events.append(event)
                    subscription.ack(event.sequence)
                    if event.kind in _TERMINALS:
                        return
            finally:
                subscription.close()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        assert target.wait(lambda: router.a6_inflight_request_ids() == (request_id,)
                           and len(events) >= 2), "source never committed a prefix"
        seam = ProductCaseSeam(case_id, target)
        seam.arm(case_id=case_id)
        post_state = dict(seam.apply(case_id=case_id))
        reader.join(timeout=30)
        (executed,) = target.executed
        settled = CaseObservationRecorder(
            authority=None, target=target, settle_seconds=10.0)._settle(executed)
        observation = derive_observation(case_id, executed, settled)
        return SimpleNamespace(
            route=route, router=router, coordinator=coordinator, target=target,
            request_id=request_id, post_state=post_state, executed=executed,
            settled=settled, observation=observation,
            kinds=[event.kind for event in events if event.kind in _TERMINALS],
            tokens=[event.text for event in events if event.kind == "token"])
    finally:
        service.close()


def test_p3_three_placement_successor_prefill_failure_fails_closed(tmp_path):
    """Case 8: a failed first successor ends fail-closed; node-3 never takes over."""

    run = _run_case(tmp_path, "successor_prefill_failure")

    assert run.post_state["successor_attempt_started"] is True
    assert run.post_state["successor_failure_injected"] is True
    # Source node-0 is killed, then the first successor (node-2) during prefill.
    assert run.route.signals == [("node-0", signal.SIGKILL), ("node-2", signal.SIGKILL)]
    # The source and exactly one successor ran; no second successor started.
    assert [call["nodes"] for call in run.route.calls] == [["node-0"], ["node-2"]]
    assert run.kinds == ["failed"]
    assert run.post_state["successors_withheld"] is True
    assert run.tokens == ["<101>", "<102>"]
    # No cutover: the committed route identity is still the source attempt.
    source_attempt = run.executed.binding["source_attempt"]
    committed = run.coordinator.route_identity(run.request_id)
    assert committed["request_attempt"] == source_attempt
    assert run.settled.coordinator_terminal == "failed"
    assert run.settled.reservation_count == 0
    # The source-owned observation is fail-closed and the trusted validator
    # accepts it as the negative case.
    assert run.observation["fail_closed"] is True
    assert run.observation["rejection_observed"] is True
    assert run.observation["terminal_state"] in {"aborted", "TERMINAL_BLOCKED"}
    runner.validate_case_observation("successor_prefill_failure", run.observation)


def test_p3_three_placement_exhausted_attempts_reaches_the_second_successor(tmp_path):
    """Case 12 on the same topology: both successors run and both are killed."""

    run = _run_case(tmp_path, "exhausted_recovery_attempts")

    assert run.post_state["attempts_used"] == 2
    assert run.post_state["successor_nodes_failed"] == [["node-2"], ["node-3"]]
    assert run.post_state["recovery_attempts"] == 2
    assert run.post_state["attempts_exhausted"] is True
    assert [call["nodes"] for call in run.route.calls] == [
        ["node-0"], ["node-2"], ["node-3"]]
    assert run.kinds == ["failed"]
    assert run.settled.coordinator_terminal == "failed"
    assert run.settled.reservation_count == 0
    assert run.observation["fail_closed"] is True
    assert run.observation["rejection_observed"] is True
    runner.validate_case_observation("exhausted_recovery_attempts", run.observation)
