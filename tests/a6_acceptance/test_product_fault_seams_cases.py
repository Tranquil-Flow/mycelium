# SPDX-License-Identifier: AGPL-3.0-only
"""Case seams that cancel, time out, exhaust attempts or block cleanup.

The router is a programmable fake whose recovery view reacts to the signals
the seam delivers; the gateway is the real ``RequestGatewayService`` with an
owner-minted session, exactly as every ordinary ASGI submission creates one.
"""
from __future__ import annotations

import signal
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a6_product_fault_seams import LiveProductFaultTarget, ProductCaseSeam
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService
from tests.request_gateway.test_core import MutableQualificationSource, _synthetic_qualification

REQUEST_ID = "r-1"
OWNER_TOKEN = "o" * 32


class _Backend:
    """Holds the request in flight until the gateway cancels it."""

    def __init__(self, on_cancel=None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[str] = []
        self._on_cancel = on_cancel

    def run(self, request_id, submission, emit_token, is_cancelled):
        emit_token(0, "first")
        self.started.set()
        self.release.wait(timeout=10)
        return "cancelled" if self.cancelled else "completed"

    def cancel(self, request_id):
        self.cancelled.append(request_id)
        if self._on_cancel is not None:
            self._on_cancel()
        self.release.set()
        return True


def _owned_service(backend):
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: REQUEST_ID,
    )
    request_id = service.submit(
        InferenceSubmission(prompt="private prompt", max_new_tokens=4,
                            qualification=qualification_binding(qualification)),
        owner_token=OWNER_TOKEN,
    )
    assert request_id == REQUEST_ID and backend.started.wait(timeout=5)
    return service


class _Router:
    """A6 router reads whose view is a function of the faults delivered."""

    def __init__(self, nodes=("node-0", "node-2")) -> None:
        identity = {"deployment_id": "d", "deployment_epoch": 1, "qualification_digest": "q",
                    "request_id": REQUEST_ID, "request_attempt": 1, "path_id": "p",
                    "path_attempt": 0, "path_digest": "pd", "topology_generation": 1,
                    "command_id": "c", "publisher_generation": 1, "cancellation_generation": 0}
        self.binding = {**identity, "source_node_id": "node-0", "source_attempt": 1,
                        "absolute_deadline_ms": 10**15, "path_manifest_digest": "pd"}
        self.placements = {f"p-{index}": node for index, node in enumerate(nodes)}
        self.state: dict = {"recovery_phase": "active", "recovery_attempts": 0}
        self.killed = False
        self.committed_attempt = 1
        self._coordinator = SimpleNamespace(
            status=lambda: {"requests": []},
            route_identity=lambda _rid: {
                "request_id": REQUEST_ID, "request_attempt": self.committed_attempt,
                "path_id": "p", "path_attempt": self.committed_attempt - 1})

    def a6_inflight_request_ids(self):
        return () if self.killed else (REQUEST_ID,)

    def a6_controlled_fault_binding(self, _request_id):
        return dict(self.binding)

    def a6_source_failure_observed(self, _binding):
        return self.killed

    def a6_request_view(self, _request_id):
        return {"retained": True, **self.state}

    def a6_placement_node_ids(self):
        return dict(self.placements)


def _target(router, service, on_signal):
    incidents: list[dict] = []
    signals: list[tuple[str, int]] = []

    def session(node_id):
        def deliver(signum):
            signals.append((node_id, signum))
            if node_id == "node-0" and signum == signal.SIGKILL and not router.killed:
                router.killed = True
                incidents.append({"incident_id": "i-1", "request_id": REQUEST_ID,
                                  "subject_id": "node-0", "scope": "request",
                                  "fatal_accepted": False, "reason": "node_process_exited"})
            on_signal(node_id, signum)
            return True
        return SimpleNamespace(_process=SimpleNamespace(_signal_process_group=deliver))

    route = SimpleNamespace(
        _sessions={node: session(node) for node in router.placements.values()},
        a4_scoped_runtime_incidents=lambda: list(incidents))
    successors = {1: ["p-1"], 2: ["p-2"]}
    qualifier = SimpleNamespace(issued_qualification_facts=lambda _rid: {
        "successor_path_attempt": router.state.get("recovery_attempts", 0),
        "successor_placement_ids": successors.get(router.state.get("recovery_attempts"), []),
    })
    target = LiveProductFaultTarget(router=router, route=route, service=service,
                                    qualifier=qualifier, wait_seconds=3, poll_seconds=0.001)
    return target, signals


def test_fault_cancel_runs_the_real_gateway_cancellation_of_an_owner_minted_session():
    backend = _Backend()
    service = _owned_service(backend)
    try:
        # The ordinary owner check refuses any caller without the owner token,
        # which is why the fault control cannot use the public cancel.
        with pytest.raises(AdmissionError, match="session_owner_mismatch"):
            service.cancel(REQUEST_ID)
        target, _signals = _target(_Router(), service, lambda *_: None)
        record = SimpleNamespace(request_id=REQUEST_ID)
        assert target.cancel(record) is True
        assert backend.cancelled == [REQUEST_ID]
        assert target.cancel(record) is False  # exactly once
    finally:
        backend.release.set()
        service.close()


def test_cancellation_during_prefill_resumes_the_stalled_successor_before_cleanup():
    router = _Router()
    resumed = threading.Event()

    def on_cancel():
        router.state["cancellation_linearized"] = True
        # The cancellation's cleanup fan-out reaches the successor only once
        # the seam has resumed it.
        assert resumed.wait(timeout=5)
        router.state["cancellation_requested"] = True

    backend = _Backend(on_cancel)
    service = _owned_service(backend)

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1)
        if node_id == "node-2" and signum == signal.SIGCONT:
            resumed.set()

    try:
        target, signals = _target(router, service, on_signal)
        seam = ProductCaseSeam("cancellation_during_prefill", target)
        seam.arm(case_id="cancellation_during_prefill")
        state = seam.apply(case_id="cancellation_during_prefill")
        assert state == {"source_failure_observed": True,
                         "cancellation_observed_during_prefill": True}
        assert backend.cancelled == [REQUEST_ID]
        assert signals == [("node-0", signal.SIGKILL), ("node-2", signal.SIGSTOP),
                           ("node-2", signal.SIGCONT)]
    finally:
        backend.release.set()
        service.close()


def test_cancellation_before_recovery_holds_the_source_before_stalling_the_others():
    router = _Router()
    backend = _Backend(lambda: router.state.update(cancellation_requested=True))
    service = _owned_service(backend)

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="successor_pending", recovery_attempts=1)

    try:
        target, signals = _target(router, service, on_signal)
        seam = ProductCaseSeam("cancellation_before_recovery", target)
        seam.arm(case_id="cancellation_before_recovery")
        state = seam.apply(case_id="cancellation_before_recovery")
        assert state == {"source_failure_observed": True,
                         "cancellation_observed_before_recovery": True,
                         "before_phase": "successor_pending",
                         "after_phase": "successor_pending",
                         "cancel_accepted": True, "cancellation_requested": True}
        # The source is held before the (slow, remote) stall of the others,
        # so it cannot finish the request first; then it is killed, and the
        # other stalled peers are resumed.
        assert signals == [("node-0", signal.SIGSTOP), ("node-2", signal.SIGSTOP),
                           ("node-0", signal.SIGKILL), ("node-2", signal.SIGCONT)]
    finally:
        backend.release.set()
        service.close()


def test_cancellation_before_recovery_accepts_the_live_failure_observed_phase():
    # The live router enters ``failure_observed`` as soon as the source fails,
    # before any successor is reserved; that is still before recovery.
    router = _Router()
    backend = _Backend(lambda: router.state.update(cancellation_requested=True))
    service = _owned_service(backend)

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="failure_observed")

    try:
        target, _signals = _target(router, service, on_signal)
        seam = ProductCaseSeam("cancellation_before_recovery", target)
        seam.arm(case_id="cancellation_before_recovery")
        state = seam.apply(case_id="cancellation_before_recovery")
        assert state["cancellation_observed_before_recovery"] is True
        assert state["before_phase"] == state["after_phase"] == "failure_observed"
    finally:
        backend.release.set()
        service.close()


def test_timeout_during_recovery_waits_for_the_router_deadline_then_resumes():
    router = _Router()

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1)
        elif node_id == "node-2" and signum == signal.SIGSTOP:
            # The router's checkpoint-expiry guard interrupts the stalled work.
            threading.Timer(0.05, lambda: router.state.update(
                recovery_deadline_exceeded=True)).start()
        elif node_id == "node-2" and signum == signal.SIGCONT:
            # Cleanup of the interrupted command is proven once resumed.
            router.state.update(recovery_failure_reason="recovery_timeout",
                                terminal_status="FAILED")

    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              on_signal)
    seam = ProductCaseSeam("timeout_during_recovery", target)
    seam.arm(case_id="timeout_during_recovery")
    assert seam.apply(case_id="timeout_during_recovery") == {
        "source_failure_observed": True, "recovery_deadline_exceeded": True}
    assert signals[-1] == ("node-2", signal.SIGCONT)


def _exhaustion(nodes):
    router = _Router(nodes)
    kills = []

    def on_signal(node_id, signum):
        if signum != signal.SIGKILL:
            return
        kills.append(node_id)
        if len(kills) == 1:  # the source: the first successor starts
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1)
        elif len(kills) == 2 and len(nodes) > 2:  # a second disjoint successor
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=2)
        else:
            # Two placements: the router counts the second attempt before its
            # reservation fails, then fails the request; or the second real
            # successor failed too.
            router.state.update(recovery_phase="cleanup", recovery_attempts=2,
                                terminal_status="FAILED")

    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              on_signal)
    seam = ProductCaseSeam("exhausted_recovery_attempts", target)
    seam.arm(case_id="exhausted_recovery_attempts")
    return seam.apply(case_id="exhausted_recovery_attempts"), signals


def test_exhausted_attempts_needs_two_real_successors_not_the_attempt_counter():
    state, signals = _exhaustion(("node-0", "node-2"))
    # The router reports two attempts and FAILED, but only one successor ran.
    assert state["attempts_used"] == 1 and state["attempts_exhausted"] is False
    assert [item for item in signals if item[1] == signal.SIGKILL] == [
        ("node-0", signal.SIGKILL), ("node-2", signal.SIGKILL)]


def test_exhausted_attempts_kills_each_actual_successor_on_three_placements():
    state, signals = _exhaustion(("node-0", "node-2", "node-3"))
    assert state["attempts_used"] == 2 and state["attempts_exhausted"] is True
    assert state["successor_nodes_failed"] == [["node-2"], ["node-3"]]
    assert [item for item in signals if item[1] == signal.SIGKILL] == [
        ("node-0", signal.SIGKILL), ("node-2", signal.SIGKILL), ("node-3", signal.SIGKILL)]


def test_exhausted_attempts_waits_for_the_recovery_deadline_terminal(monkeypatch):
    # The second successor's FAILED terminal can come from the router's
    # recovery deadline, well after the seam's ordinary wait bound.
    import mycelium_a6_product_fault_seams as seams

    monkeypatch.setattr(seams, "RECOVERY_DEADLINE_WAIT_SECONDS", 5.0)
    router = _Router(("node-0", "node-2", "node-3"))
    kills = []

    def on_signal(node_id, signum):
        if signum != signal.SIGKILL:
            return
        kills.append(node_id)
        if len(kills) == 1:
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1)
        elif len(kills) == 2:
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=2)
        else:
            threading.Timer(0.5, lambda: router.state.update(
                recovery_phase="cleanup", terminal_status="FAILED",
                recovery_failure_reason="recovery_timeout")).start()

    target, _signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                               on_signal)
    target._wait_seconds = 0.2
    seam = ProductCaseSeam("exhausted_recovery_attempts", target)
    seam.arm(case_id="exhausted_recovery_attempts")
    state = seam.apply(case_id="exhausted_recovery_attempts")
    assert state["attempts_used"] == 2 and state["attempts_exhausted"] is True
    assert state["terminal_status"] == "FAILED" and state["recovery_attempts"] == 2
    assert state["recovery_failure_reason"] == "recovery_timeout"
    assert state["successor_nodes_killed"] == "node-2;node-3"


class _HoldRouter(_Router):
    """A router with the A6 completion-cleanup hold the cleanup seam arms."""

    def __init__(self, nodes=("node-0", "node-2"), on_release=None) -> None:
        super().__init__(nodes)
        self.hold_calls: list[str] = []
        self._on_release = on_release

    def a6_arm_completion_cleanup_hold(self, _request_id):
        self.hold_calls.append("arm")
        return True

    def a6_release_completion_cleanup_hold(self, _request_id):
        self.hold_calls.append("release")
        if self._on_release is not None:
            self._on_release()
        return True


def _cleanup_case(router, on_signal, *, delivered=(1, 2)):
    events: list = []

    def recorded(node_id, signum):
        events.append((node_id, signum))
        on_signal(node_id, signum)

    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              recorded)
    target.gateway_committed = lambda _rid: delivered
    seam = ProductCaseSeam("cleanup_authority_failure", target)
    seam.arm(case_id="cleanup_authority_failure")
    return seam.apply(case_id="cleanup_authority_failure"), signals


def _successor_reaches_its_held_cleanup(router):
    # The source failed with 2 delivered tokens; the successor generated 12
    # (EOS far below its bound) and is held at its completion cleanup.
    router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1,
                        recovery_output_limit=63, token_count=2, cursor=2)
    threading.Timer(0.05, lambda: router.state.update(
        recovery_output_count=12, recovery_output_finished=True,
        recovery_cleanup_held=True)).start()


def _refused_before_cutover(router):
    # Released after the stall: cleanup cannot be proven, the recovery aborts
    # before cutover, the request is terminal-blocked, the suffix discarded.
    router.state.update(recovery_cleanup_held=False,
                        terminal_blocked_reason="command_cleanup_receipt_missing",
                        recovery_failure_reason="successor_execution_failed")


def test_cleanup_authority_failure_proves_a_refusal_before_cutover():
    router = _HoldRouter(on_release=lambda: _refused_before_cutover(router))

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            assert router.hold_calls == ["arm"]  # armed before the fault
            _successor_reaches_its_held_cleanup(router)

    state, signals = _cleanup_case(router, on_signal)
    assert state["cleanup_authority_refused"] is True
    assert state["cutover_committed"] is False
    assert state["unpublishable_suffix_discarded"] is True
    assert state["successor_stalled"] is True and state["cleanup_held"] is True
    assert state["successor_output_count"] == 12
    assert state["prefix_token_count"] == state["delivered_token_count"] == 2
    # The stall lands while the cleanup is held, then the hold is released.
    assert router.hold_calls == ["arm", "release"]
    assert signals == [("node-0", signal.SIGKILL), ("node-2", signal.SIGSTOP),
                       ("node-2", signal.SIGCONT)]


def test_cleanup_authority_failure_stall_precedes_the_hold_release():
    order: list[str] = []
    router = _HoldRouter(on_release=lambda: (order.append("release"),
                                             _refused_before_cutover(router)))

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            _successor_reaches_its_held_cleanup(router)
        elif node_id == "node-2" and signum == signal.SIGSTOP:
            order.append("stall")

    _cleanup_case(router, on_signal)
    assert order == ["stall", "release"]


def test_cleanup_authority_failure_fails_when_the_cutover_committed():
    def committed_then_blocked():
        router.committed_attempt = 2
        router.state.update(recovery_phase="recovered_decode", recovery_cleanup_held=False,
                            terminal_blocked_reason="command_cleanup_receipt_missing")

    router = _HoldRouter(on_release=committed_then_blocked)

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            _successor_reaches_its_held_cleanup(router)

    state, _signals = _cleanup_case(router, on_signal)
    assert state["cutover_committed"] is True
    assert state["cleanup_authority_refused"] is False
    assert state["unpublishable_suffix_discarded"] is False


@pytest.mark.parametrize("published", [
    {"token_count": 14, "cursor": 14},  # the router took the suffix
    {"token_count": 2, "cursor": 2},    # the client was delivered it anyway
])
def test_cleanup_authority_failure_fails_when_the_suffix_is_published(published):
    def published_suffix():
        _refused_before_cutover(router)
        router.state.update(published)

    router = _HoldRouter(on_release=published_suffix)

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            _successor_reaches_its_held_cleanup(router)

    state, _signals = _cleanup_case(router, on_signal, delivered=(1, 14))
    assert state["cleanup_authority_refused"] is True
    assert state["unpublishable_suffix_discarded"] is False


def test_cleanup_authority_failure_refuses_without_the_router_hold():
    from mycelium_a6_fault_control import FaultRejected

    router = _Router()  # no A6 hold: the stall could race the cleanup proof
    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              lambda *_: None)
    seam = ProductCaseSeam("cleanup_authority_failure", target)
    seam.arm(case_id="cleanup_authority_failure")
    with pytest.raises(FaultRejected, match="a6_fault_cleanup_hold_unavailable"):
        seam.apply(case_id="cleanup_authority_failure")
    assert signals == []  # no fault was applied


def test_cleanup_authority_failure_refuses_when_the_hold_cannot_be_armed():
    from mycelium_a6_fault_control import FaultRejected

    router = _HoldRouter()
    router.a6_arm_completion_cleanup_hold = lambda _rid: False
    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              lambda *_: None)
    target.gateway_committed = lambda _rid: (1, 2)
    seam = ProductCaseSeam("cleanup_authority_failure", target)
    seam.arm(case_id="cleanup_authority_failure")
    with pytest.raises(FaultRejected, match="a6_fault_cleanup_hold_unarmed"):
        seam.apply(case_id="cleanup_authority_failure")
    assert signals == []


def test_cleanup_authority_failure_stops_waiting_when_the_successor_fails_by_itself():
    router = _HoldRouter()

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1,
                                recovery_output_limit=63, recovery_output_count=0)
            # The successor fails before reaching its cleanup authorization;
            # the router keeps the recovery_prefill phase and fails the request.
            threading.Timer(0.05, lambda: router.state.update(
                terminal_status="FAILED",
                recovery_failure_reason="successor_execution_failed")).start()

    started = time.monotonic()
    state, signals = _cleanup_case(router, on_signal)
    assert time.monotonic() - started < 5.0
    assert state["cleanup_authority_refused"] is False
    assert state["successor_stalled"] is False
    assert state["phase_at_stall_wait"] == "recovery_prefill"
    assert router.hold_calls == ["arm", "release"]  # never left armed
    assert [item for item in signals if item[1] == signal.SIGSTOP] == []

@pytest.mark.parametrize("terminal", [
    {"terminal_status": "COMPLETED"},
    {"terminal_blocked_reason": "command_cleanup_receipt_missing"},
])
def test_cleanup_reservation_release_counts_a_blocked_terminal_as_terminal(terminal):
    router = _Router()
    reservations = {"count": 2}
    router._coordinator.status = lambda: {"requests": [
        {"request_id": REQUEST_ID, "reservation_count": reservations["count"]}]}
    target, _signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                               lambda *_: None)
    seam = ProductCaseSeam("cleanup_reservation_release", target)
    seam.arm(case_id="cleanup_reservation_release")
    # The completion cleanup was not proven in budget: the request ends
    # terminal-blocked (no terminal status) and its reservations are released.
    router.state.update(terminal)
    reservations["count"] = 0
    state = seam.apply(case_id="cleanup_reservation_release")
    assert state["request_terminal"] is True
    assert state["release_observed"] is True and state["release_count_positive"] is True
    assert state["reservations_before"] == 2 and state["reservations_after"] == 0


_LOST_LINK_CHALLENGE = {
    "outcome": "infer_error", "error": "node_process_exited", "placement_ids": ["p-1"],
    "participant_exits": [{"node_id": "node-2", "returncode": 255,
                           "stderr_last_line": "Timeout, server h not responding."}],
}


@pytest.mark.parametrize("challenge", [_LOST_LINK_CHALLENGE, {"outcome": "in_flight"}, None])
def test_successor_prefill_failure_says_why_its_successor_never_started(challenge):
    router = _Router(nodes=("node-0", "node-2", "node-3"))
    router.a6_withhold_recovery_successors = lambda _rid: True

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="successor_pending", recovery_attempts=1)

    target, signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                              on_signal)
    target.qualifier.physical_challenge_outcome = lambda _rid: challenge
    seam = ProductCaseSeam("successor_prefill_failure", target)
    seam.arm(case_id="successor_prefill_failure")
    state = seam.apply(case_id="successor_prefill_failure")
    assert state["successor_attempt_started"] is False
    assert state["successor_failure_injected"] is False
    assert signals == [("node-0", signal.SIGKILL)]  # no successor was signalled
    expected = challenge or {}
    assert state["successor_challenge_outcome"] == expected.get("outcome")
    assert state["successor_challenge_error"] == expected.get("error")
    assert state["successor_transport_exit"] == (
        "node-2:255:Timeout, server h not responding." if challenge is _LOST_LINK_CHALLENGE
        else None)
    assert state["recovery_phase"] == "successor_pending"


def test_cleanup_authority_failure_says_why_its_successor_never_started():
    router = _HoldRouter()

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            router.state.update(recovery_phase="successor_pending", recovery_attempts=1)

    events: list = []
    target, _signals = _target(router, SimpleNamespace(terminal_event_count=lambda _r: 0),
                               lambda n, s: (events.append((n, s)), on_signal(n, s)))
    target.gateway_committed = lambda _rid: (1, 1)
    target.qualifier.physical_challenge_outcome = lambda _rid: _LOST_LINK_CHALLENGE
    seam = ProductCaseSeam("cleanup_authority_failure", target)
    seam.arm(case_id="cleanup_authority_failure")
    state = seam.apply(case_id="cleanup_authority_failure")
    assert state["cleanup_authority_refused"] is False and state["cleanup_held"] is False
    assert router.hold_calls == ["arm", "release"]
    assert state["successor_challenge_error"] == "node_process_exited"
    assert state["successor_transport_exit"] == "node-2:255:Timeout, server h not responding."


def test_executed_successor_cases_carry_no_challenge_facts():
    router = _HoldRouter(on_release=lambda: _refused_before_cutover(router))

    def on_signal(node_id, signum):
        if node_id == "node-0" and signum == signal.SIGKILL:
            _successor_reaches_its_held_cleanup(router)

    state, _signals = _cleanup_case(router, on_signal)
    assert state["cleanup_authority_refused"] is True
    assert not any(key.startswith("successor_challenge") for key in state)
