# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic LiveRouterPort A6 continuation through the ordinary gateway path.

The route is deliberately simulated; this proves wiring only and cannot satisfy the
physical/browser gate.
"""
from __future__ import annotations

from dataclasses import replace
import threading
import time

from mycelium_a6_m16 import M16RecoveryAuthority
from mycelium_a6_recovery import (
    validate_recovery_runtime,
    build_recovery_qualification,
    recovery_request_binding,
)
from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_live.router_port import LiveRouterPort
from mycelium_live.route import InferenceCancelled
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from tests.live.test_a6_m16_recovery import (
    _coordinator,
    _graph,
    _qualified_recovery_inputs,
)
from tests.request_gateway.test_core import (
    MutableQualificationSource,
    _synthetic_qualification,
)


TOKENIZER_DIGEST = "sha256:" + "9" * 64


class TokenCodec:
    tokenizer_digest = TOKENIZER_DIGEST

    def encode(self, prompt: str) -> tuple[int, ...]:
        assert prompt == "live recovery prompt"
        return (7, 8, 9)

    def decode_token(self, token_id: int) -> str:
        return f"<{token_id}>"


class FailingThenRecoveringRoute:
    is_simulated = True

    def __init__(
        self,
        *,
        fail_successor: bool = False,
        fail_successor_attempts: int = 0,
        block_successor: bool = False,
        after_successor=None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._scoped_runtime_incidents: list[dict[str, object]] = []
        self.released: list[str] = []
        self.source_sink = None
        self.stale_source_emit_attempted = False
        self.fail_successor_attempts = 2 if fail_successor else fail_successor_attempts
        self.source_qualification_digest = None
        self.source_absolute_deadline_ms = None
        self.block_successor = block_successor
        self.successor_started = threading.Event()
        self.cancelled = threading.Event()
        self.after_successor = after_successor

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
        **_kwargs,
    ) -> None:
        # Match LiveRoute's current immutable-path boundary on every attempt.
        assert selected_placement_ids is None, "locked_path_cannot_select_placements"
        selected_placement_ids = tuple(
            hop["placement_id"] for hop in locked_path_manifest["ordered_hops"]
        )
        call = {
            "prompt_token_ids": tuple(prompt_token_ids),
            "max_new_tokens": max_new_tokens,
            "request_id": request_id,
            "selected_placement_ids": tuple(selected_placement_ids),
            "route_identity": dict(route_identity),
            "locked_path_manifest": dict(locked_path_manifest),
            "command_identity": dict(command_identity),
        }
        self.calls.append(call)
        if len(self.calls) == 1:
            self.source_qualification_digest = command_identity[
                "qualification_digest"
            ]
            self.source_absolute_deadline_ms = command_identity[
                "absolute_deadline_ms"
            ]
            self.source_sink = sink
            sink.emit(0, 101)
            sink.emit(1, 102)
            self._scoped_runtime_incidents.append(
                {
                    "request_id": request_id,
                    "scope": "placement",
                    "state": "failed",
                }
            )
            raise RuntimeError("injected_assigned_peer_failure")

        assert tuple(prompt_token_ids) == (7, 8, 9, 101, 102)
        assert max_new_tokens == 2
        assert route_identity["request_attempt"] == 2
        assert command_identity["request_attempt"] == 2
        assert command_identity["qualification_digest"] == (
            self.source_qualification_digest
        )
        assert command_identity["absolute_deadline_ms"] == (
            self.source_absolute_deadline_ms
        )
        assert set(selected_placement_ids).isdisjoint(
            self.calls[0]["selected_placement_ids"]
        )
        if self.block_successor:
            self.successor_started.set()
            assert self.cancelled.wait(timeout=2)
            raise InferenceCancelled("injected_successor_cancel")
        assert self.source_sink is not None
        self.stale_source_emit_attempted = True
        self.source_sink.emit(2, 999)
        sink.emit(0, 103)
        if len(self.calls) - 1 <= self.fail_successor_attempts:
            raise RuntimeError("injected_successor_prefill_failure")
        sink.emit(1, 104)
        if self.after_successor is not None:
            self.after_successor()

    def release_request(self, request_id: str) -> None:
        self.released.append(request_id)

    def cancel_request(self, request_id: str, **_kwargs) -> bool:
        self.cancelled.set()
        return True

    def close(self) -> None:
        return None


class CompletionCleanupRaceRoute(FailingThenRecoveringRoute):
    """Hold a completed successor after completion-cleanup authorization."""

    def __init__(self) -> None:
        super().__init__()
        self.completion_cleanup_authorized = threading.Event()
        self.finish_successor = threading.Event()

    def infer(self, *args, authorize_cleanup, **kwargs) -> None:
        super().infer(*args, authorize_cleanup=authorize_cleanup, **kwargs)
        authorize_cleanup(time.monotonic() + 1.0)

    def recovery_cutover_committed(self, _request_id: str) -> None:
        self.completion_cleanup_authorized.set()
        assert self.finish_successor.wait(timeout=2)

    def cancel_request(self, request_id: str, **_kwargs) -> bool:
        self.finish_successor.set()
        return True


class MissingCleanupReceiptAfterCutoverRoute(FailingThenRecoveringRoute):
    """Exercise fail-closed cleanup after successor ownership is committed."""

    is_simulated = False

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
        command = None
        for call in self.calls:
            candidate = call["command_identity"]
            if (
                isinstance(candidate, dict)
                and candidate.get("request_attempt") == request_attempt
            ):
                command = candidate
                break
        assert command is not None
        return {
            **command,
            "request_id": request_id,
            "path_id": path_id,
            "path_attempt": path_attempt,
            "path_digest": path_digest,
            "cleanup_owner_id": cleanup_owner_id,
            "node_ids": [],
        }


def _runtime(
    tmp_path,
    route,
    *,
    base_graph=None,
    recovery_clock_ms=lambda: 1_200,
    qualification_expiry_ms=1_900_000,
    qualification_calls=None,
    production_qualifier=False,
):
    gateway_qualification = _synthetic_qualification()
    graph = replace(
        _graph() if base_graph is None else base_graph,
        deployment_id=gateway_qualification.deployment_id,
        deployment_epoch=gateway_qualification.deployment_epoch,
        topology_version=gateway_qualification.topology_version,
        manifest_digest=gateway_qualification.manifest_digest,
    )
    coordinator = _coordinator(graph)

    def qualification_source(
        request_id,
        checkpoint,
        successor_identity,
        successor_manifest,
    ):
        if qualification_calls is not None:
            qualification_calls.append(request_id)
        source_identity = coordinator.route_identity(request_id)
        source_request = coordinator.request_context(request_id)
        expected, source_compatibility, _surrogate = _qualified_recovery_inputs(
            coordinator,
            source_request,
            source_identity,
        )
        assert expected.generation_settings_digest == checkpoint.generation_settings_digest
        qualification = build_recovery_qualification(
            coordinator.recovery_deployment_binding(),
            qualification_id="synthetic-live-a6-successor",
            request_binding=recovery_request_binding(checkpoint),
            successor_binding={
                "request_id": request_id,
                "request_attempt": successor_identity["request_attempt"],
                "path_id": successor_identity["path_id"],
                "path_attempt": successor_identity["path_attempt"],
                "path_manifest_digest": successor_identity[
                    "path_manifest_digest"
                ],
                "placement_ids": [
                    hop.placement_id for hop in successor_manifest.ordered_hops
                ],
            },
            compatibility=source_compatibility,
            compatible=True,
            issued_at_unix_ms=1_100,
            expiry_unix_ms=qualification_expiry_ms,
            physical_challenge="passed",  # synthetic deterministic test input only
        )
        return source_compatibility, qualification

    if production_qualifier:
        deployment_dir = tmp_path / "a6-qualifier-deployment"
        deployment_dir.mkdir(exist_ok=True)
        (deployment_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (deployment_dir / "tokenizer.json").write_text(
            '{"version":"1.0"}\n',
            encoding="utf-8",
        )
        qualification_source = LiveA6RecoveryQualifier(
            graph=graph,
            deployment_dir=deployment_dir,
            tokenizer_digest=TOKENIZER_DIGEST,
            physical_challenge=lambda **_kwargs: {
                "passed": True,
                "parity_policy_digest": "sha256:" + "b" * 64,
                "workload_envelope_digest": "sha256:" + "c" * 64,
            },
            clock_ms=lambda: 1_100,
        )

    router = LiveRouterPort(
        route=route,
        execution_graph=graph,
        runtime_coordinator=coordinator,
        recovery_authority=M16RecoveryAuthority(coordinator),
        recovery_qualification_source=qualification_source,
        recovery_tokenizer_digest=TOKENIZER_DIGEST,
        recovery_a4_atomic_commit="a" * 40,
        recovery_clock_ms=recovery_clock_ms,
    )
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(gateway_qualification),
        backend=RouterSessionBackend(
            router=router,
            codec=TokenCodec(),
            clock=lambda: 100.0,
        ),
        request_id_source=lambda: "live-a6-001",
        max_buffered_events=16,
        replay_checkpoint_root=tmp_path,
    )
    return coordinator, router, service, gateway_qualification


def _submit_and_drain(service, qualification):
    request_id = service.submit(
        InferenceSubmission(
            prompt="live recovery prompt",
            max_new_tokens=4,
            qualification=qualification_binding(qualification),
        )
    )
    subscription = service.subscribe(request_id, last_event_id=None)
    events = []
    try:
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
    finally:
        subscription.close()
    return request_id, events


def test_live_router_replays_exact_context_and_continues_one_gateway_stream(tmp_path) -> None:
    route = FailingThenRecoveringRoute()
    coordinator, router, service, gateway_qualification = _runtime(tmp_path, route)
    subscription = None
    try:
        request_id, events = _submit_and_drain(service, gateway_qualification)

        tokens = [event for event in events if event.kind == "token"]
        terminals = [
            event
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ]
        assert [(event.token_index, event.text) for event in tokens] == [
            (0, "<101>"),
            (1, "<102>"),
            (2, "<103>"),
            (3, "<104>"),
        ], (
            router.recovery_failure_reason(request_id),
            [(event.kind, event.code) for event in terminals],
        )
        assert [event.kind for event in terminals] == ["completed"]
        assert service.terminal_event_count(request_id) == 1
        assert len(route.calls) == 2
        assert route.stale_source_emit_attempted is True
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "completed"
        assert projected["reservation_count"] == 0
    finally:
        if subscription is not None:
            subscription.close()
        service.close()


def test_live_router_revalidates_exact_qualification_at_cutover(tmp_path) -> None:
    now = {"value": 1_200}
    qualification_calls = []
    route = FailingThenRecoveringRoute(
        after_successor=lambda: now.__setitem__("value", 1_301)
    )
    coordinator, router, service, qualification = _runtime(
        tmp_path,
        route,
        recovery_clock_ms=lambda: now["value"],
        qualification_expiry_ms=1_300,
        qualification_calls=qualification_calls,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [event.kind for event in events if event.kind in {"completed", "failed", "cancelled"}] == ["failed"]
        assert qualification_calls == [request_id, request_id]
        assert router.recovery_failure_reason(request_id) == "recovery_qualification_stale"
        projected = next(item for item in coordinator.status()["requests"] if item["request_id"] == request_id)
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_live_router_aborts_when_no_disjoint_successor_exists(tmp_path) -> None:
    full = _graph()
    source_only = replace(
        full,
        stages=(
            replace(full.stages[0], placements=(full.stages[0].placements[0],)),
            replace(full.stages[1], placements=(full.stages[1].placements[0],)),
        ),
        edges=(full.edges[0],),
        loopback_edges=(full.loopback_edges[0],),
    )
    route = FailingThenRecoveringRoute()
    coordinator, router, service, qualification = _runtime(
        tmp_path,
        route,
        base_graph=source_only,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
        ]
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["failed"]
        assert service.terminal_event_count(request_id) == 1
        assert len(route.calls) == 1
        assert router.recovery_failure_reason(request_id) is not None
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_live_router_second_disjoint_successor_recovers_after_first_successor_failure(tmp_path) -> None:
    route = FailingThenRecoveringRoute(fail_successor_attempts=1)
    coordinator, router, service, qualification = _runtime(
        tmp_path,
        route,
        production_qualifier=True,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
            "<103>",
            "<104>",
        ]
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["completed"]
        assert service.terminal_event_count(request_id) == 1
        assert len(route.calls) == 3
        assert [call["route_identity"]["request_attempt"] for call in route.calls] == [1, 2, 2]
        assert route.calls[2]["route_identity"]["path_attempt"] > (
            route.calls[1]["route_identity"]["path_attempt"]
        )
        assert router.recovery_failure_reason(request_id) is None
        assert set(route.calls[2]["selected_placement_ids"]).isdisjoint(
            route.calls[0]["selected_placement_ids"]
        )
        assert set(route.calls[2]["selected_placement_ids"]).isdisjoint(
            route.calls[1]["selected_placement_ids"]
        )
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "completed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_live_router_aborts_if_successor_fails_before_cutover(tmp_path) -> None:
    route = FailingThenRecoveringRoute(fail_successor=True)
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
        ]
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["failed"]
        assert service.terminal_event_count(request_id) == 1
        assert len(route.calls) == 3
        assert route.stale_source_emit_attempted is True
        assert router.recovery_failure_reason(request_id) == "successor_execution_failed"
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
    finally:
        service.close()


def test_live_router_cleanup_receipt_failure_after_cutover_publishes_failure_to_prove_terminal(
    tmp_path,
) -> None:
    """REVERSAL, adopted from pilot ``1704ac2``: a blocked request IS visible to the user.

    This test was written as ``..._stays_nonterminal`` on the A6 candidate's own base
    (``f156ba0``) and asserted ``terminal_event_count == 0``.  Pilot ``1704ac2``
    ("fix(gateway): publish a failure-to-prove terminal for a blocked request") deliberately
    reversed that invariant, because a request the route refused to settle produced NO terminal
    at all and the client could not tell a stuck request from a slow one -- which exit criterion
    5 ("zero stuck requests") forbids.  The candidate's assertion encoded the superseded
    invariant; it is updated here, not deleted, so the reversal stays visible.

    What the published terminal claims: kind ``failed``, code ``request_cleanup_unproven`` --
    FAILURE TO PROVE, and nothing else.  It asserts nothing about the command, so it contradicts
    no ledger.  Everything the route itself refused to commit must stay uncommitted, and the
    negative assertions below pin exactly that: no success terminal, no cancellation, no route
    cleanup receipt, and no cleanup proven for the successor attempt.
    """
    route = MissingCleanupReceiptAfterCutoverRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)

        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
        ]
        terminals = [
            event
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ]
        # Positive: exactly ONE terminal, and it is the route's own failure to prove.
        assert [event.kind for event in terminals] == ["failed"], [
            (event.kind, event.code) for event in terminals
        ]
        assert [event.code for event in terminals] == ["request_cleanup_unproven"]
        assert service.terminal_event_count(request_id) == 1
        assert service._sessions[request_id].cleanup_unproven is True
        # Negative: the failure-to-prove terminal is NOT a success and NOT a cancellation.
        assert all(event.kind != "completed" for event in events)
        assert all(event.kind != "cancelled" for event in events)
        # Negative: the route still did not prove owner-scoped cleanup, so the port holds the
        # blocked reason and NO cleanup receipt for the successor attempt.
        assert router.request_status(request_id) == "TERMINAL_BLOCKED"
        assert router.recovery_failure_reason(request_id) == "cleanup_authority_failed"
        assert router._pending[request_id].terminal_blocked_reason == (
            "command_cleanup_receipt_missing"
        )
        assert router._pending[request_id].cleanup_receipt is None
        # Negative: the route never committed its terminal CAS, so the coordinator still shows
        # no terminal, an open reservation, and cleanup NOT complete.
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert coordinator.route_identity(request_id)["request_attempt"] == 2
        assert projected["terminal_state"] is None
        assert projected["phase"] == "cleanup"
        assert projected["reservation_count"] == 2
        public = next(
            item
            for item in service.recovery_public_projection()["requests"]
            if item["request_id"] == request_id
        )
        assert public["terminal"] is None
        assert public["cleanup_complete"] is False
        assert public["attempt"] == 2
    finally:
        service.close()


def test_live_router_cancel_during_successor_prefill_stays_cancelled(tmp_path) -> None:
    route = FailingThenRecoveringRoute(block_successor=True)
    coordinator, _router, service, qualification = _runtime(tmp_path, route)
    subscription = None
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="live recovery prompt",
                max_new_tokens=4,
                qualification=qualification_binding(qualification),
            )
        )
        subscription = service.subscribe(request_id, last_event_id=None)
        assert route.successor_started.wait(timeout=2)
        projection = service.recovery_public_projection()
        projected_recovery = next(
            item for item in projection["requests"] if item["request_id"] == request_id
        )
        assert projected_recovery["phase"] == "recovery_prefill"
        assert service.cancel(request_id) is True
        events = []
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
        ]
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["cancelled"]
        assert service.terminal_event_count(request_id) == 1
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "cancelled"
        assert projected["reservation_count"] == 0
    finally:
        if subscription is not None:
            subscription.close()
        service.close()


def test_live_router_cancel_after_cutover_cannot_lose_to_completed_command(tmp_path) -> None:
    route = CompletionCleanupRaceRoute()
    coordinator, _router, service, qualification = _runtime(tmp_path, route)
    subscription = None
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="live recovery prompt",
                max_new_tokens=4,
                qualification=qualification_binding(qualification),
            )
        )
        subscription = service.subscribe(request_id, last_event_id=None)
        assert route.completion_cleanup_authorized.wait(timeout=2)
        assert coordinator.route_identity(request_id)["request_attempt"] == 2

        assert service.cancel(request_id) is True

        events = []
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["cancelled"]
        assert coordinator.terminal_state(request_id) == "cancelled"
    finally:
        route.finish_successor.set()
        if subscription is not None:
            subscription.close()
        service.close()


def test_live_router_shutdown_during_successor_prefill_stays_cancelled(tmp_path) -> None:
    route = FailingThenRecoveringRoute(block_successor=True)
    coordinator, _router, service, qualification = _runtime(tmp_path, route)
    subscription = None
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="live recovery prompt",
                max_new_tokens=4,
                qualification=qualification_binding(qualification),
            )
        )
        subscription = service.subscribe(request_id, last_event_id=None)
        assert route.successor_started.wait(timeout=2)

        service.close()

        events = []
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>",
            "<102>",
        ]
        assert [
            event.kind
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ] == ["cancelled"]
        assert service.terminal_event_count(request_id) == 1
        projected = next(
            item
            for item in coordinator.status()["requests"]
            if item["request_id"] == request_id
        )
        assert projected["terminal_state"] == "cancelled"
        assert projected["reservation_count"] == 0
    finally:
        if subscription is not None:
            subscription.close()
        service.close()


def test_gateway_public_recovery_projection_uses_gateway_committed_count(tmp_path) -> None:
    route = FailingThenRecoveringRoute()
    _coordinator_value, _router_value, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        projection = service.recovery_public_projection()

        assert projection["protocol"] == "mycelium.full_context_recovery_runtime.v1"
        runtime = validate_recovery_runtime(projection)
        request = next(item for item in runtime["requests"] if item["request_id"] == request_id)
        assert request["committed_token_count"] == 4
        assert request["attempt"] == 2
        assert request["phase"] == "terminal"
        assert request["terminal"] == "completed"
        serialized = repr(projection)
        assert "live recovery prompt" not in serialized
        assert "encoded_prompt" not in serialized
        assert "committed_prefix_tokens" not in serialized
    finally:
        service.close()
