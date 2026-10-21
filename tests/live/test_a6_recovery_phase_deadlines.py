# SPDX-License-Identifier: AGPL-3.0-only
"""A6 recovery is bounded per phase, not by one 30 s deadline.

Spec ``2026-08-18-mycelium-a6-full-context-replay-recovery.md`` section 5 asks
for separately frozen successor qualification, prefill, cutover and total
recovery deadlines. These tests drive the ordinary gateway -> LiveRouterPort
-> production ``LiveA6RecoveryQualifier`` path with a simulated route and an
injected recovery clock, so every phase duration is exact and no test sleeps
through a real deadline. They prove wiring and bounds only; they are not
physical or browser qualification.
"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from mycelium_a6_m16 import M16RecoveryAuthority
from mycelium_a6_recovery import validate_recovery_runtime
from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_live.route import InferenceCancelled
from mycelium_live.router_port import LiveRouterPort, RecoveryPhaseDeadlines
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from tests.live.test_a6_live_router_recovery import TOKENIZER_DIGEST, TokenCodec
from tests.live.test_a6_m16_recovery import _coordinator, _graph
from tests.request_gateway.test_core import (
    MutableQualificationSource,
    _synthetic_qualification,
)

_TERMINALS = {"completed", "failed", "cancelled"}
#: The checkpoint freezes at this recovery-clock value.
_START_MS = 1_200
#: Measured on the physical fleet: the successor physical challenge answered
#: about 6 s after the fault (runs/v2/RUNDAY-A6 window a2, drive.log).
_QUALIFICATION_MS = 6_000


class _Clock:
    """The injected recovery clock; tests advance it, nothing sleeps."""

    def __init__(self, start_ms: int = _START_MS) -> None:
        self._value = start_ms
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._value

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self._value += milliseconds


class SlowSuccessorRoute:
    """A source that fails after one committed token, then a slow successor.

    The successor spends ``prefill_ms`` before its first token and
    ``per_token_ms`` between tokens on the recovery clock. ``stall_after``
    stops the stream after that many tokens: the clock jumps ``stall_ms``
    and the successor waits (in real time, bounded) for the router to
    interrupt it. ``stall_at_clock_ms`` stalls the same way once the clock
    reaches that value while tokens are still arriving on time.
    """

    def __init__(
        self,
        clock: _Clock,
        *,
        successor_tokens: int,
        prefill_ms: int = 1_000,
        per_token_ms: int = 600,
        stall_after: int | None = None,
        stall_ms: int = 0,
        stall_at_clock_ms: int | None = None,
        is_simulated: bool = True,
        cancel_receipt_delay_s: float = 0.3,
    ) -> None:
        self.is_simulated = is_simulated
        self._clock = clock
        self._successor_tokens = successor_tokens
        self._prefill_ms = prefill_ms
        self._per_token_ms = per_token_ms
        self._stall_after = stall_after
        self._stall_ms = stall_ms
        self._stall_at_clock_ms = stall_at_clock_ms
        self._cancel_receipt_delay_s = cancel_receipt_delay_s
        self.calls: list[dict[str, object]] = []
        self._scoped_runtime_incidents: list[dict[str, object]] = []
        self.cancelled = threading.Event()
        self.stalled = threading.Event()
        self.successor_emitted = 0
        self.published_receipts: list[dict[str, object]] = []
        self._publish = None
        self._successor_command: dict[str, object] | None = None

    def is_alive(self) -> bool:
        return True

    def infer(
        self,
        prompt_token_ids,
        *,
        max_new_tokens,
        request_id,
        sink,
        selected_placement_ids=None,
        route_identity,
        locked_path_manifest,
        command_identity,
        publish_cleanup_receipt=None,
        **_kwargs,
    ) -> None:
        assert selected_placement_ids is None
        self.calls.append(
            {
                "request_id": request_id,
                "max_new_tokens": max_new_tokens,
                "command_identity": dict(command_identity),
            }
        )
        if len(self.calls) == 1:
            sink.emit(0, 101)
            self._scoped_runtime_incidents.append(
                {"request_id": request_id, "scope": "placement", "state": "failed"}
            )
            raise RuntimeError("injected_assigned_peer_failure")

        assert tuple(prompt_token_ids) == (7, 8, 9, 101)
        assert max_new_tokens == self._successor_tokens
        assert command_identity["request_attempt"] == 2
        self._publish = publish_cleanup_receipt
        self._successor_command = dict(command_identity)
        self._clock.advance(self._prefill_ms)
        for index in range(max_new_tokens):
            if index:
                self._clock.advance(self._per_token_ms)
            if (index == self._stall_after) or (
                self._stall_at_clock_ms is not None
                and self._clock() >= self._stall_at_clock_ms
            ):
                self._clock.advance(self._stall_ms)
                self.stalled.set()
                if not self.cancelled.wait(timeout=5):
                    raise RuntimeError("test_successor_never_interrupted")
                raise InferenceCancelled("injected_successor_interrupted")
            sink.emit(index, 200 + index)
            self.successor_emitted += 1

    def cancel_request(self, request_id: str, **_kwargs) -> bool:
        self.cancelled.set()
        command = self._successor_command
        if not self.is_simulated and command is not None and self._publish is not None:
            # A physical cancellation fan-out returns (and publishes the
            # exact cleanup receipt) after the interrupted worker raised.
            threading.Event().wait(self._cancel_receipt_delay_s)
            receipt = {
                **command,
                "cancellation_generation": command["cancellation_generation"] + 1,
                "cleanup_owner_id": f"physical-live-route:{command['deployment_id']}",
                "node_ids": ["successor-node"],
            }
            self.published_receipts.append(receipt)
            self._publish(receipt)
        return True

    def request_cleanup_receipt_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> dict[str, object] | None:
        if request_attempt != 1:
            return None
        command = self.calls[0]["command_identity"]
        return {
            **command,
            "request_id": request_id,
            "path_id": path_id,
            "path_attempt": path_attempt,
            "path_digest": path_digest,
            "cleanup_owner_id": cleanup_owner_id,
            "node_ids": [],
        }

    def release_request(self, request_id: str) -> None:
        return None

    def close(self) -> None:
        return None


def _stack(tmp_path, route, clock, *, deadlines=None, qualification_ms=_QUALIFICATION_MS):
    gateway_qualification = _synthetic_qualification()
    graph = replace(
        _graph(),
        deployment_id=gateway_qualification.deployment_id,
        deployment_epoch=gateway_qualification.deployment_epoch,
        topology_version=gateway_qualification.topology_version,
        manifest_digest=gateway_qualification.manifest_digest,
    )
    coordinator = _coordinator(graph)
    deployment_dir = tmp_path / "a6-qualifier-deployment"
    deployment_dir.mkdir()
    (deployment_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (deployment_dir / "tokenizer.json").write_text('{"version":"1.0"}\n', encoding="utf-8")
    challenges: list[int] = []

    def physical_challenge(**_kwargs):
        # The successor's physical challenge costs real time on the fleet.
        clock.advance(qualification_ms)
        challenges.append(clock())
        return {
            "passed": True,
            "parity_policy_digest": "sha256:" + "b" * 64,
            "workload_envelope_digest": "sha256:" + "c" * 64,
        }

    qualifier = LiveA6RecoveryQualifier(
        graph=graph,
        deployment_dir=deployment_dir,
        tokenizer_digest=TOKENIZER_DIGEST,
        physical_challenge=physical_challenge,
        clock_ms=clock,
    )
    extra = {} if deadlines is None else {"recovery_deadlines": deadlines}
    router = LiveRouterPort(
        route=route,
        execution_graph=graph,
        runtime_coordinator=coordinator,
        recovery_authority=M16RecoveryAuthority(coordinator),
        recovery_qualification_source=qualifier,
        recovery_tokenizer_digest=TOKENIZER_DIGEST,
        recovery_a4_atomic_commit="a" * 40,
        recovery_clock_ms=clock,
        **extra,
    )
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(gateway_qualification),
        backend=RouterSessionBackend(router=router, codec=TokenCodec(), clock=lambda: 100.0),
        request_id_source=lambda: "live-a6-deadline-001",
        max_buffered_events=256,
        replay_checkpoint_root=tmp_path,
    )
    return coordinator, router, service, gateway_qualification, challenges


def _run(service, qualification, *, max_new_tokens):
    request_id = service.submit(
        InferenceSubmission(
            prompt="live recovery prompt",
            max_new_tokens=max_new_tokens,
            qualification=qualification_binding(qualification),
        )
    )
    subscription = service.subscribe(request_id, last_event_id=None)
    events = []
    try:
        while True:
            event = subscription.next_event(timeout=10)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
    finally:
        subscription.close()
    return request_id, events


def _projected(coordinator, request_id):
    return next(
        item for item in coordinator.status()["requests"] if item["request_id"] == request_id
    )


def test_slow_cpu_successor_recovers_a_64_token_answer(tmp_path) -> None:
    """The browser workload: 64 tokens, one committed before the fault, then
    a successor that qualifies in 6 s and decodes 63 tokens at 0.6 s/token
    (about 45 s of recovery). One 30 s deadline aborted this; per-phase
    deadlines let it complete with one cutover and exact-once delivery."""

    clock = _Clock()
    route = SlowSuccessorRoute(clock, successor_tokens=63, prefill_ms=1_000, per_token_ms=600)
    coordinator, router, service, qualification, challenges = _stack(tmp_path, route, clock)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        tokens = [(event.token_index, event.text) for event in events if event.kind == "token"]
        terminals = [event for event in events if event.kind in _TERMINALS]
        assert [event.kind for event in terminals] == ["completed"], (
            router.recovery_failure_reason(request_id),
            [(event.kind, event.code) for event in terminals],
        )
        assert tokens == [(0, "<101>")] + [
            (index + 1, f"<{200 + index}>") for index in range(63)
        ]
        assert service.terminal_event_count(request_id) == 1
        # Recovery really took longer than the old single 30 s bound.
        assert clock() - _START_MS > 30_000
        # Fresh qualification at reservation and again before the cutover CAS.
        assert len(challenges) == 2
        assert route.cancelled.is_set() is False
        view = router.a6_request_view(request_id)
        assert view is None or view.get("recovery_deadline_exceeded") in (None, False)
        projected = _projected(coordinator, request_id)
        assert projected["terminal_state"] == "completed"
        assert projected["reservation_count"] == 0
        public = next(
            item
            for item in validate_recovery_runtime(service.recovery_public_projection())["requests"]
            if item["request_id"] == request_id
        )
        assert public["recovery_attempt"] == 1
        assert public["cleanup_complete"] is True
    finally:
        service.close()


def test_successor_that_stalls_mid_stream_aborts_at_the_progress_deadline(tmp_path) -> None:
    """Tokens stop after five; the successor is interrupted once the progress
    (idle) deadline elapses -- well inside the old 30 s single bound, which
    would never have fired here -- and the request fails as a recovery
    timeout with exact cleanup and no successor output published."""

    clock = _Clock()
    deadlines = RecoveryPhaseDeadlines()
    stall_ms = deadlines.progress_ms + 1_000
    route = SlowSuccessorRoute(
        clock, successor_tokens=63, prefill_ms=1_000, per_token_ms=600,
        stall_after=5, stall_ms=stall_ms)
    coordinator, router, service, qualification, _challenges = _stack(tmp_path, route, clock)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        assert route.stalled.is_set()
        assert route.cancelled.is_set()
        # Still inside the old checkpoint expiry: only the progress deadline fired.
        assert clock() - _START_MS < 30_000
        assert [event.text for event in events if event.kind == "token"] == ["<101>"]
        assert [event.kind for event in events if event.kind in _TERMINALS] == ["failed"]
        assert service.terminal_event_count(request_id) == 1
        assert router.recovery_failure_reason(request_id) == "recovery_timeout"
        view = router.a6_request_view(request_id)
        assert view["recovery_deadline_exceeded"] is True
        assert view["recovery_deadline_phase"] == "progress"
        assert view["recovery_output_finished"] is False
        projected = _projected(coordinator, request_id)
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_successor_that_never_produces_a_first_token_aborts_at_the_prefill_deadline(
    tmp_path,
) -> None:
    clock = _Clock()
    deadlines = RecoveryPhaseDeadlines(prefill_ms=10_000)
    route = SlowSuccessorRoute(
        clock, successor_tokens=63, prefill_ms=0, stall_after=0, stall_ms=11_000)
    coordinator, router, service, qualification, _challenges = _stack(
        tmp_path, route, clock, deadlines=deadlines)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        assert route.cancelled.is_set()
        assert clock() - _START_MS < 30_000
        assert [event.kind for event in events if event.kind in _TERMINALS] == ["failed"]
        assert router.recovery_failure_reason(request_id) == "recovery_timeout"
        assert router.a6_request_view(request_id)["recovery_deadline_phase"] == "prefill"
        assert _projected(coordinator, request_id)["reservation_count"] == 0
    finally:
        service.close()


def test_successor_that_does_not_qualify_in_time_aborts_before_fencing(tmp_path) -> None:
    """A successor whose qualification outlives the qualification deadline is
    never fenced in or started: the request aborts at that phase."""

    clock = _Clock()
    deadlines = RecoveryPhaseDeadlines()
    route = SlowSuccessorRoute(clock, successor_tokens=63)
    coordinator, router, service, qualification, challenges = _stack(
        tmp_path, route, clock, qualification_ms=deadlines.qualification_ms + 1_000)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        assert len(challenges) == 1
        assert len(route.calls) == 1, "the successor must never start"
        assert [event.text for event in events if event.kind == "token"] == ["<101>"]
        assert [event.kind for event in events if event.kind in _TERMINALS] == ["failed"]
        assert router.recovery_failure_reason(request_id) == "recovery_timeout"
        view = router.a6_request_view(request_id)
        assert view["recovery_deadline_phase"] == "qualification"
        projected = _projected(coordinator, request_id)
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_progressing_successor_still_stops_at_the_total_recovery_cap(tmp_path) -> None:
    clock = _Clock()
    # Every phase bound must fit inside the total (a phase longer than the
    # total is rejected as a configuration error), so the 20 s cap carries
    # 10 s phase bounds; each phase is met (6 s qualification, 1 s prefill,
    # 1 s per token) and only the total cap can stop this successor.
    deadlines = RecoveryPhaseDeadlines(
        qualification_ms=10_000, prefill_ms=10_000, progress_ms=10_000,
        cutover_ms=10_000, total_ms=20_000)
    route = SlowSuccessorRoute(
        clock, successor_tokens=63, prefill_ms=1_000, per_token_ms=1_000,
        stall_at_clock_ms=_START_MS + 20_000)
    coordinator, router, service, qualification, _challenges = _stack(
        tmp_path, route, clock, deadlines=deadlines)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        assert route.cancelled.is_set()
        assert 0 < route.successor_emitted < 63
        assert [event.text for event in events if event.kind == "token"] == ["<101>"]
        assert [event.kind for event in events if event.kind in _TERMINALS] == ["failed"]
        assert router.recovery_failure_reason(request_id) == "recovery_timeout"
        assert router.a6_request_view(request_id)["recovery_deadline_phase"] == "total"
        assert _projected(coordinator, request_id)["reservation_count"] == 0
    finally:
        service.close()


def test_deadline_abort_waits_for_the_interrupted_successors_cleanup_receipt(
    tmp_path,
) -> None:
    """Physical route: the interrupted worker raises before the cancellation
    fan-out returns the exact cleanup receipt. The abort must wait for that
    receipt (bounded) and finish truthfully -- a cleaned failed terminal --
    instead of recording ``command_cleanup_receipt_missing`` and leaving the
    request TERMINAL_BLOCKED (the 67bb87b7 browser-matrix symptom)."""

    clock = _Clock()
    # The stall outlives both the progress deadline and the old single 30 s
    # bound, so the interruption happens with either deadline policy.
    route = SlowSuccessorRoute(
        clock, successor_tokens=63, prefill_ms=1_000, per_token_ms=600,
        stall_after=5, stall_ms=30_000, is_simulated=False)
    coordinator, router, service, qualification, _challenges = _stack(tmp_path, route, clock)
    try:
        request_id, events = _run(service, qualification, max_new_tokens=64)
        assert route.cancelled.is_set()
        assert len(route.published_receipts) == 1
        terminals = [event for event in events if event.kind in _TERMINALS]
        assert [(event.kind, event.code) for event in terminals][:1] != [
            ("failed", "request_cleanup_unproven")
        ]
        assert [event.kind for event in terminals] == ["failed"]
        assert router.recovery_failure_reason(request_id) == "recovery_timeout"
        view = router.a6_request_view(request_id)
        # The gateway releases the settled request (request_status is then
        # UNKNOWN by contract); the retained view keeps its terminal.
        assert view["terminal_status"] == "FAILED"
        assert view["terminal_blocked_reason"] is None
        assert view["recovery_deadline_phase"] == "progress"
        projected = _projected(coordinator, request_id)
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"qualification_ms": 0}, "a6_recovery_deadlines_invalid"),
        ({"progress_ms": -1}, "a6_recovery_deadlines_invalid"),
        ({"total_ms": 10_000, "prefill_ms": 20_000}, "a6_recovery_deadlines_invalid"),
        ({"total_ms": 7_200_000}, "a6_recovery_deadlines_invalid"),
        ({"cutover_ms": True}, "a6_recovery_deadlines_invalid"),
    ],
)
def test_recovery_deadlines_fail_closed_on_invalid_values(overrides, code) -> None:
    with pytest.raises(ValueError, match=code):
        RecoveryPhaseDeadlines(**overrides)


def test_recovery_deadlines_are_configurable_from_the_environment() -> None:
    configured = RecoveryPhaseDeadlines.from_environment(
        {
            "MYCELIUM_A6_RECOVERY_TOTAL_SECONDS": "900",
            "MYCELIUM_A6_RECOVERY_PROGRESS_SECONDS": "20",
        }
    )
    assert configured.total_ms == 900_000
    assert configured.progress_ms == 20_000
    assert configured.qualification_ms == RecoveryPhaseDeadlines().qualification_ms
    assert RecoveryPhaseDeadlines.from_environment({}) == RecoveryPhaseDeadlines()
    with pytest.raises(ValueError, match="a6_recovery_deadlines_invalid"):
        RecoveryPhaseDeadlines.from_environment({"MYCELIUM_A6_RECOVERY_TOTAL_SECONDS": "nan"})
