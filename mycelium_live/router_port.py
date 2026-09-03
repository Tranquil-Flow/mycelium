"""Adapt the request gateway's RouterPort onto a persistent LiveRoute."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast, Iterable, Mapping

from mycelium_m16_runtime import M16AdmissionError, M16RuntimeCoordinator
from mycelium_router.contracts import ExecutionGraph, RequestContext

from .command_controller import (
    CleanupResult,
    CleanupStatus,
    CommandController,
    CommandEnvelope,
    CommandIdentity,
    CommandKind,
    TerminalResult,
    TerminalStatus,
)
from .a4_contracts import validate_interruptible_stage_command
from .lock_order import LockOrderDetector, LockOrderViolation

from .route import AffectedPeerQuarantined, InferenceCancelled, LiveRoute, TokenSink


_LOGGER = logging.getLogger(__name__)


@dataclass
class _Pending:
    request: RequestContext | None = None
    placement_ids: tuple[str, ...] | None = None
    route_identity: dict[str, Any] | None = None
    path_manifest: dict[str, Any] | None = None
    command_identity: CommandIdentity | None = None
    cleanup_owner_id: str | None = None
    cleanup_receipt: dict[str, Any] | None = None
    tokens: list[tuple[int, int]] = field(default_factory=list)
    cursor: int = 0
    terminal_status: str | None = None
    terminal_error_code: str | None = None
    terminal_blocked_reason: str | None = None
    cancellation_linearized: bool = False
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    release_requested: bool = False
    execution_started: bool = False


class LiveRouterPort:
    """Drive one persistent physical route through the RouterPort contract."""

    requires_qualification_binding = True
    supports_workload_profiles = True
    supports_publisher_generation = True

    def __init__(
        self,
        *,
        route: LiveRoute,
        execution_graph: ExecutionGraph,
        runtime_coordinator: M16RuntimeCoordinator | None = None,
        lock_order_detector: LockOrderDetector | None = None,
    ) -> None:
        self._route = route
        self._graph = execution_graph
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        # Coordinator admission makes a request dispatch-visible before this
        # adapter has finished binding its command identity and pending state.
        # Serialize that publication boundary with dispatcher selection so a
        # fast dispatcher cannot consume and fail a request in the sub-ms gap.
        self._admission_dispatch_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._sinks: dict[str, object] = {}
        self._coordinator = runtime_coordinator
        self._commands = CommandController()
        self._lock_order = lock_order_detector or LockOrderDetector()
        self._closed = False
        worker_count = (
            4
            if runtime_coordinator is None
            else runtime_coordinator.maximum_concurrent_requests
        )
        self._worker_pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix=f"live-route-worker-{execution_graph.deployment_id[:12]}",
        )
        self._dispatcher = None
        if runtime_coordinator is not None:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name=f"m16-dispatch-{execution_graph.deployment_id[:16]}",
                daemon=True,
            )
            self._dispatcher.start()

    def current_deployment(self) -> ExecutionGraph:
        return self._graph

    def is_idle(self) -> bool:
        """Return true only after every admitted request has been released."""

        with self._lock:
            return not self._pending

    def admit(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        with self._admission_dispatch_lock:
            return self._admit_serialized(
                request,
                client_sink,
                pinned_deployment=pinned_deployment,
                **kwargs,
            )

    def _admit_serialized(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        if not self._route.is_alive():
            raise RuntimeError("route_not_open")

        pending = _Pending(request=request)
        coordinator = self._coordinator
        if coordinator is not None:
            profile = kwargs.get("workload_profile_id")
            if profile is None:
                profile = (
                    "sustained_batch_v1"
                    if request.qos_class == "batch"
                    else "interactive_chat_v1"
                )
            if not isinstance(profile, str):
                raise M16AdmissionError("workload_not_qualified")
            excluded_placements = kwargs.get("excluded_placements", frozenset())
            if not isinstance(excluded_placements, frozenset) or not all(
                isinstance(item, str) and item for item in excluded_placements
            ):
                raise M16AdmissionError("invalid_excluded_placements")
            manifest = coordinator.admit(
                request,
                workload_profile_id=profile,
                excluded_placements=excluded_placements,
            )
            pending.placement_ids = tuple(
                hop.placement_id for hop in manifest.ordered_hops
            )
            pending.route_identity = coordinator.route_identity(request.request_id)
            pending.path_manifest = coordinator.path_manifest(request.request_id)
            qualification = kwargs.get("qualification_binding")
            qualification_digest = getattr(
                qualification,
                "qualification_digest",
                None,
            )
            if qualification_digest is None:
                if getattr(self._route, "is_simulated", False) is not True:
                    coordinator.cancel(request.request_id)
                    raise M16AdmissionError("qualification_unavailable")
                qualification_digest = self._digest_document(
                    {
                        "deployment_id": self._graph.deployment_id,
                        "simulation_only": True,
                    }
                )
            issued_at_ms = int(time.monotonic() * 1_000)
            publisher_generation = kwargs.get("publisher_generation", 1)
            if type(publisher_generation) is not int or publisher_generation < 1:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("publisher_generation_invalid")
            first_placement = manifest.ordered_hops[0].placement_id
            graph_placement = next(
                (
                    (stage, placement)
                    for stage in self._graph.stages
                    for placement in stage.placements
                    if placement.placement_id == first_placement
                ),
                None,
            )
            if graph_placement is None:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("path_identity_invalid")
            stage, placement = graph_placement
            route_identity = pending.route_identity
            assert route_identity is not None
            command_id = f"request:{request.request_id}:attempt:{route_identity['request_attempt']}"
            identity = CommandIdentity(
                deployment_id=route_identity["deployment_id"],
                deployment_epoch=route_identity["deployment_epoch"],
                qualification_digest=qualification_digest,
                request_id=request.request_id,
                request_attempt=route_identity["request_attempt"],
                path_id=route_identity["path_id"],
                path_attempt=route_identity["path_attempt"],
                path_digest=route_identity["path_manifest_digest"],
                topology_generation=route_identity["topology_generation"],
                command_id=command_id,
                publisher_generation=publisher_generation,
                absolute_deadline_ms=issued_at_ms + 3_600_000,
            )
            cleanup_owner_id = f"physical-live-route:{self._graph.deployment_id}"
            idempotency_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        pending.route_identity,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            envelope = CommandEnvelope(
                identity=identity,
                stage_id=stage.stage_id,
                placement_id=placement.placement_id,
                assignment_id=placement.assignment_id,
                kind=CommandKind.PREFILL,
                issued_at_ms=issued_at_ms,
                idempotency_digest=idempotency_digest,
                cleanup_owner_id=cleanup_owner_id,
                maximum_request_bytes=131_072,
                maximum_response_bytes=16_777_216,
            )
            validate_interruptible_stage_command(
                {
                    "protocol": "mycelium.interruptible_stage_command.v1",
                    "deployment_id": identity.deployment_id,
                    "deployment_epoch": identity.deployment_epoch,
                    "qualification_digest": identity.qualification_digest,
                    "request_id": identity.request_id,
                    "request_attempt": identity.request_attempt,
                    "path_id": identity.path_id,
                    "path_attempt": identity.path_attempt,
                    "path_digest": identity.path_digest,
                    "topology_generation": identity.topology_generation,
                    "command_id": identity.command_id,
                    "stage_id": envelope.stage_id,
                    "placement_id": envelope.placement_id,
                    "assignment_id": envelope.assignment_id,
                    "command_kind": envelope.kind.value,
                    "issued_at_ms": envelope.issued_at_ms,
                    "idempotency_digest": envelope.idempotency_digest,
                    "cancellation_generation": identity.cancellation_generation,
                    "publisher_generation": identity.publisher_generation,
                    "absolute_deadline_ms": identity.absolute_deadline_ms,
                    "cooperative_step_ms": 100,
                    "cleanup_owner_id": envelope.cleanup_owner_id,
                    "maximum_request_bytes": envelope.maximum_request_bytes,
                    "maximum_response_bytes": envelope.maximum_response_bytes,
                    "expected_terminal_revision": envelope.expected_terminal_revision,
                }
            )
            try:
                with self._lock_order.scope(
                    "deployment",
                    owner_id=request.request_id,
                ):
                    registration = self._commands.register(envelope)
            except LockOrderViolation as error:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError(error.code) from error
            if not registration.accepted:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("command_registration_failed")
            pending.command_identity = identity
            pending.cleanup_owner_id = cleanup_owner_id
        with self._lock:
            if self._closed:
                if coordinator is not None:
                    coordinator.cancel(request.request_id)
                raise RuntimeError("router_port_closed")
            if request.request_id in self._pending:
                if coordinator is not None:
                    coordinator.cancel(request.request_id)
                raise RuntimeError("duplicate_request_id")
            self._pending[request.request_id] = pending
            self._sinks[request.request_id] = client_sink
            self._changed.notify_all()

        if coordinator is None:
            self._worker_pool.submit(
                self._run_route, request.request_id, request, pending
            )
        return request.request_id

    def _dispatch_loop(self) -> None:
        coordinator = self._coordinator
        assert coordinator is not None
        while True:
            with self._admission_dispatch_lock:
                with self._changed:
                    if self._closed:
                        return
                    request_id = coordinator.next_dispatch()
                    if request_id is None:
                        self._changed.wait(timeout=0.05)
                        continue
                    pending = self._pending.get(request_id)
                    stored = None if pending is None else pending.request
            if pending is None or stored is None:
                coordinator.complete(request_id, state="failed")
                continue
            self._worker_pool.submit(
                self._run_route, request_id, stored, pending
            )

    def _route_has_scoped_incident_for(self, request: RequestContext | None) -> bool:
        if request is None:
            return False
        incidents = getattr(self._route, "_scoped_runtime_incidents", None)
        if isinstance(incidents, Iterable):
            for incident in incidents:
                if (
                    isinstance(incident, Mapping)
                    and incident.get("request_id") == request.request_id
                ):
                    return True
        # Route-level cleanup incidents (request_cleanup_unproven, recorded
        # when exact cleanup could not be proven within the original
        # deadline) are equally authoritative: the runtime reservation must
        # retire so the bounded admission slot is not leaked while the SSE
        # terminal stays unpublished (fail-closed terminal, bounded runtime).
        route_incidents = getattr(self._route, "_incidents", None)
        if isinstance(route_incidents, Iterable):
            for incident in route_incidents:
                if (
                    isinstance(incident, Mapping)
                    and incident.get("request_id") == request.request_id
                    and incident.get("state") == "request_cleanup_unproven"
                ):
                    return True
        return False

    def _run_route(
        self,
        request_id: str,
        request: RequestContext,
        pending: _Pending,
    ) -> None:
        coordinator = self._coordinator
        first_token = False
        self_outer = self

        class _Collector:
            def emit(self, token_index: int, token_id: int) -> None:
                nonlocal first_token
                if coordinator is not None:
                    coordinator.mark_phase(
                        request_id,
                        "first_token" if not first_token else "decode",
                    )
                    first_token = True
                with self_outer._changed:
                    pending.tokens.append((token_index, token_id))
                    self_outer._changed.notify_all()

        terminal_error_code: str | None = None
        try:
            with self._changed:
                pending.execution_started = True
                placement_ids = pending.placement_ids
                route_identity = (
                    None
                    if pending.route_identity is None
                    else dict(pending.route_identity)
                )
                path_manifest = (
                    None
                    if pending.path_manifest is None
                    else dict(pending.path_manifest)
                )
                identity = pending.command_identity
            route_options: dict[str, object] = {}
            if placement_ids is not None and path_manifest is None:
                route_options["selected_placement_ids"] = placement_ids
            if route_identity is not None:
                route_options["route_identity"] = route_identity
            if path_manifest is not None:
                route_options["locked_path_manifest"] = path_manifest
            if identity is not None:
                route_options["command_identity"] = {
                    "deployment_id": identity.deployment_id,
                    "deployment_epoch": identity.deployment_epoch,
                    "qualification_digest": identity.qualification_digest,
                    "request_id": identity.request_id,
                    "request_attempt": identity.request_attempt,
                    "path_id": identity.path_id,
                    "path_attempt": identity.path_attempt,
                    "path_digest": identity.path_digest,
                    "topology_generation": identity.topology_generation,
                    "command_id": identity.command_id,
                    "publisher_generation": identity.publisher_generation,
                    "absolute_deadline_ms": identity.absolute_deadline_ms,
                    "cancellation_generation": identity.cancellation_generation,
                }
                route_options["authorize_cleanup"] = lambda deadline: (
                    self_outer._request_command_cancellation(
                        pending,
                        deadline_monotonic_s=deadline,
                        completion_cleanup=True,
                    )
                )
                route_options["publish_cleanup_receipt"] = lambda receipt: (
                    self_outer._publish_route_cleanup_receipt(pending, receipt)
                )
            self._route.infer(
                request.prompt_token_ids,
                max_new_tokens=request.max_new_tokens,
                request_id=request_id,
                sink=_Collector(),
                cancel_requested=pending.cancellation_requested.is_set,
                **route_options,
            )
        except InferenceCancelled:
            terminal = "CANCELLED"
            admission_refused = False
        except AffectedPeerQuarantined as error:
            terminal = "FAILED"
            admission_refused = True
            terminal_error_code = self._bounded_error_code(error)
        except Exception as error:
            terminal = "FAILED"
            admission_refused = False
            terminal_error_code = self._bounded_error_code(error)
        else:
            # Cancellation is linearized by the command controller before the
            # physical route is interrupted.  The route can still return its
            # completed result in the narrow window between that CAS and its
            # local cancellation check.  Do not then try to publish COMPLETED
            # for a command whose cancellation generation already advanced:
            # the controller correctly rejects that transition and the M16
            # reservation would otherwise remain in cleanup forever despite
            # an exact route cleanup receipt.
            terminal = "CANCELLED" if pending.cancellation_linearized else "COMPLETED"
            admission_refused = False
        if pending.cancellation_linearized and pending.cleanup_receipt is not None:
            # The route can publish exact physical cleanup before its original
            # inference command unwinds.  Any later return or exception belongs
            # to the retired generation and cannot override the already-sealed
            # cancellation terminal.
            terminal = "CANCELLED"
            terminal_error_code = None
            admission_refused = False
        if coordinator is not None:
            coordinator.mark_phase(request_id, "cleanup")
        if admission_refused:
            # The route refused admission before any node command was
            # issued, so no command-ledger terminal CAS is owed. Publish
            # the bounded failed terminal directly so the gateway closes
            # the stream with a definitive outcome instead of retaining a
            # nonterminal session (the cleanup-unproven shape).
            terminal_blocked_reason = None
            if coordinator is not None:
                coordinator.complete(request_id, state="failed")
        else:
            try:
                recorded_terminal = self._record_command_terminal(pending, terminal)
                if recorded_terminal == "FAILED" and terminal != "FAILED":
                    terminal_error_code = "deadline_exceeded"
                terminal = recorded_terminal
            except RuntimeError as error:
                terminal_blocked_reason = str(error)
                _LOGGER.error(
                    "live route terminal blocked request_id=%s reason=%s",
                    request_id,
                    terminal_blocked_reason[:256],
                )
                # If a scoped liveness incident already projected the affected
                # track's terminal status (e.g. a participating peer's transport
                # is fatally failed), the scoped incident is the authoritative
                # cleanup projection. Retire the runtime reservation so the
                # request leaves the cleanup phase; the SSE stream will close
                # without a terminal event, which the gateway's scoped-liveness
                # surfaces as the authoritative outcome. Without a scoped
                # incident we keep the fail-closed "cleanup" phase so the
                # operator can see the unproven cleanup.
                if coordinator is not None and self._route_has_scoped_incident_for(
                    pending.request
                ):
                    coordinator.complete(request_id, state="failed")
                elif coordinator is not None:
                    # Cleanup-unproven with no scoped incident: the stream
                    # terminal stays unpublished (fail-closed), but the
                    # dispatch slot must retire so this request cannot pin
                    # concurrent dispatch capacity forever (spec A4 §5).
                    coordinator.retire_dispatch_slot(request_id)
            else:
                terminal_blocked_reason = None
                if coordinator is not None:
                    coordinator.complete(request_id, state=terminal.lower())
        should_release = False
        with self._changed:
            pending.terminal_error_code = terminal_error_code
            if terminal_blocked_reason is None:
                pending.terminal_status = terminal
            else:
                pending.terminal_blocked_reason = terminal_blocked_reason
            if pending.release_requested and pending.terminal_status is not None:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def decode_one(self, request_id: str) -> bool:
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            while (
                pending.cursor >= len(pending.tokens)
                and pending.terminal_status is None
                and pending.terminal_blocked_reason is None
            ):
                self._changed.wait(timeout=30.0)
            if pending.cursor >= len(pending.tokens):
                return False
            token_index, token_id = pending.tokens[pending.cursor]
            pending.cursor += 1
            sink = self._sinks[request_id]
        sink.emit(token_index, token_id)
        return True

    def poll_one(self, request_id: str) -> bool:
        """Deliver one available token without waiting for route progress."""

        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.cursor >= len(pending.tokens):
                return False
            token_index, token_id = pending.tokens[pending.cursor]
            pending.cursor += 1
            sink = cast(TokenSink, self._sinks[request_id])
        sink.emit(token_index, token_id)
        return True

    def request_status(self, request_id: str) -> str:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return "UNKNOWN"
            if pending.terminal_status is not None and pending.cursor >= len(
                pending.tokens
            ):
                return pending.terminal_status
            if pending.terminal_blocked_reason is not None and pending.cursor >= len(
                pending.tokens
            ):
                return "TERMINAL_BLOCKED"
            return "DECODING"

    def request_error_code(self, request_id: str) -> str | None:
        """Return one bounded terminal failure code without private diagnostics."""

        with self._lock:
            pending = self._pending.get(request_id)
            return None if pending is None else pending.terminal_error_code

    def cancel(self, request_id: str) -> bool:
        return self.cancel_with_deadline(
            request_id,
            deadline_monotonic_s=time.monotonic() + 2.0,
        )

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool:
        route_cancel = None
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.terminal_status is not None:
                return False
            coordinator = self._coordinator
            if coordinator is not None and coordinator.phase(request_id) == "queued":
                coordinator.cancel(request_id)
                self._request_command_cancellation(
                    pending,
                    deadline_monotonic_s=deadline_monotonic_s,
                )
                pending.cancellation_linearized = True
                pending.cancellation_requested.set()
                self._record_command_terminal(pending, "CANCELLED")
                pending.terminal_status = "CANCELLED"
                self._changed.notify_all()
                return True
            cancellation_deadline = self._request_command_cancellation(
                pending,
                deadline_monotonic_s=deadline_monotonic_s,
            )
            # The command-controller CAS is terminal authority immediately,
            # including while physical cancel_request() is still returning.
            # Keep its state separate from the route's cancel_requested
            # callback: that callback must not become true until physical
            # fanout has installed the matching cancellation generation.
            pending.cancellation_linearized = True
            route_cancel = getattr(self._route, "cancel_request", None)
        if callable(route_cancel):
            try:
                route_cancel(
                    request_id,
                    deadline_monotonic_s=cancellation_deadline,
                )
            finally:
                with self._changed:
                    pending.cancellation_requested.set()
                    self._changed.notify_all()
        else:
            with self._changed:
                pending.cancellation_requested.set()
                self._changed.notify_all()
        return True

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool:
        """Advance gateway publication authority through the live command path."""

        route_update = None
        route_identity: dict[str, Any] | None = None
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.command_identity is None:
                return False
            identity = pending.command_identity
            if identity.publisher_generation == new_generation:
                return True
            if identity.publisher_generation != expected_generation:
                return False
            advanced = self._commands.advance_publisher_generation(
                identity,
                expected_generation=expected_generation,
                new_generation=new_generation,
            )
            if not advanced.accepted or advanced.snapshot is None:
                return False
            pending.command_identity = advanced.snapshot.identity
            if pending.execution_started:
                route_update = getattr(
                    self._route,
                    "update_publisher_generation",
                    None,
                )
                route_identity = dict(pending.route_identity or {})
        if callable(route_update):
            return bool(
                route_update(
                    request_id,
                    expected_generation=expected_generation,
                    new_generation=new_generation,
                    route_identity=route_identity,
                )
            )
        return True

    @staticmethod
    def _bounded_error_code(error: BaseException) -> str:
        code = getattr(error, "remote_code", None) or getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = str(error).partition(":")[0]
        if (
            not code
            or len(code) > 64
            or not code[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in code
            )
        ):
            return "runtime_error"
        return code

    @staticmethod
    def _digest_document(document: object) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )

    def _request_command_cancellation(
        self,
        pending: _Pending,
        *,
        deadline_monotonic_s: float | None = None,
        completion_cleanup: bool = False,
    ) -> float:
        identity = pending.command_identity
        if identity is None:
            return time.monotonic() + 2.0
        if identity.cancellation_generation > 0:
            snapshots = self._commands.snapshot(
                identity.request_id,
                request_attempt=identity.request_attempt,
            )
            if len(snapshots) != 1 or snapshots[0].cleanup_deadline_ms is None:
                raise RuntimeError("command_cancellation_deadline_missing")
            return snapshots[0].cleanup_deadline_ms / 1_000.0
        now_ms = int(time.monotonic() * 1_000)
        cleanup_deadline_ms = (
            None if deadline_monotonic_s is None else int(deadline_monotonic_s * 1_000)
        )
        result = self._commands.cancel(
            identity,
            new_cancellation_generation=identity.cancellation_generation + 1,
            observed_at_ms=now_ms,
            idempotency_digest=self._digest_document(
                {
                    "command_id": identity.command_id,
                    "cancellation_generation": identity.cancellation_generation + 1,
                }
            ),
            cleanup_deadline_ms=cleanup_deadline_ms,
            completion_cleanup=completion_cleanup,
        )
        if not result.accepted or result.snapshot is None:
            raise RuntimeError(f"command_cancellation_rejected:{result.reason}")
        pending.command_identity = result.snapshot.identity
        if result.snapshot.cleanup_deadline_ms is None:
            raise RuntimeError("command_cancellation_deadline_missing")
        return result.snapshot.cleanup_deadline_ms / 1_000.0

    def _publish_route_cleanup_receipt(
        self,
        pending: _Pending,
        receipt: Mapping[str, Any],
    ) -> None:
        """Publish physical cleanup at the route proof boundary."""

        should_release = False
        with self._changed:
            # Retain the exact receipt with command ownership before any
            # terminal publication.  The route may release its request-scoped
            # copy as soon as the gateway observes this callback; a racing
            # worker must not then fall back to a missing route lookup.
            pending.cleanup_receipt = dict(receipt)
            if pending.cancellation_linearized:
                self._record_command_terminal(
                    pending,
                    "CANCELLED",
                    cleanup_receipt=receipt,
                )
                # The route worker can return in the narrow interval after
                # physical teardown proves clean but before cancel_request()
                # publishes this receipt.  It then fail-closes terminal
                # publication and retires only the dispatch slot.  The exact
                # owner receipt is authoritative recovery for that ordering:
                # finish retained M16 reservations and revive the pending
                # terminal instead of leaving a proven-clean request stuck in
                # cleanup forever.
                coordinator = self._coordinator
                request = pending.request
                request_id = None if request is None else request.request_id
                phase = (
                    None
                    if coordinator is None or request_id is None
                    else coordinator.phase(request_id)
                )
                if coordinator is not None and request_id is not None:
                    if phase == "cleanup":
                        coordinator.complete(request_id, state="cancelled")
                    elif phase not in {
                        "queued",
                        "prefill",
                        "first_token",
                        "decode",
                        "cancelled",
                    }:
                        raise RuntimeError("command_cleanup_phase_invalid")
                if phase in {"cleanup", "cancelled"} or coordinator is None:
                    pending.terminal_error_code = None
                    pending.terminal_blocked_reason = None
                    pending.terminal_status = "CANCELLED"
                    if pending.release_requested and request_id is not None:
                        self._pending.pop(request_id, None)
                        self._sinks.pop(request_id, None)
                        should_release = True
                    self._changed.notify_all()
            else:
                self._record_command_cleanup(pending, receipt=receipt)
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def _record_command_cleanup(
        self,
        pending: _Pending,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        if identity is None or owner_id is None:
            return
        if receipt is None and pending.cleanup_receipt is not None:
            receipt = dict(pending.cleanup_receipt)
        if receipt is None:
            scoped_receipt_source = getattr(
                self._route,
                "request_cleanup_receipt_scoped",
                None,
            )
            if callable(scoped_receipt_source):
                receipt = scoped_receipt_source(
                    identity.request_id,
                    request_attempt=identity.request_attempt,
                    path_id=identity.path_id,
                    path_attempt=identity.path_attempt,
                    path_digest=identity.path_digest,
                    cleanup_owner_id=owner_id,
                )
            else:
                receipt_source = getattr(self._route, "request_cleanup_receipt", None)
                receipt = (
                    receipt_source(identity.request_id)
                    if callable(receipt_source)
                    else None
                )
        if receipt is None:
            if (
                pending.execution_started
                and getattr(self._route, "is_simulated", False) is not True
            ):
                raise RuntimeError("command_cleanup_receipt_missing")
            receipt = {
                "deployment_id": identity.deployment_id,
                "deployment_epoch": identity.deployment_epoch,
                "qualification_digest": identity.qualification_digest,
                "request_id": identity.request_id,
                "request_attempt": identity.request_attempt,
                "path_id": identity.path_id,
                "path_attempt": identity.path_attempt,
                "path_digest": identity.path_digest,
                "topology_generation": identity.topology_generation,
                "command_id": identity.command_id,
                "cancellation_generation": identity.cancellation_generation,
                "publisher_generation": identity.publisher_generation,
                "cleanup_owner_id": owner_id,
                "node_ids": [],
                "simulation_only": bool(pending.execution_started),
            }
        # Physical cleanup is fenced by the immutable request/path/command and
        # cancellation generation that was actually torn down.  Publisher
        # generation is independent gateway replay authority: it may advance
        # after the route freezes its cleanup subject but before the receipt
        # callback reaches this adapter.  The command controller has already
        # CAS-validated every such advance, so an older positive publisher
        # generation remains exact cleanup proof for the current command.  A
        # future generation is impossible and still fails closed.
        identity_fields = (
            ("deployment_id", identity.deployment_id),
            ("deployment_epoch", identity.deployment_epoch),
            ("qualification_digest", identity.qualification_digest),
            ("request_id", identity.request_id),
            ("request_attempt", identity.request_attempt),
            ("path_id", identity.path_id),
            ("path_attempt", identity.path_attempt),
            ("path_digest", identity.path_digest),
            ("topology_generation", identity.topology_generation),
            ("command_id", identity.command_id),
            (
                "cancellation_generation",
                identity.cancellation_generation,
            ),
        )
        identity_mismatch_fields = tuple(
            field
            for field, expected in identity_fields
            if receipt.get(field) != expected
        )
        receipt_publisher_generation = receipt.get("publisher_generation")
        publisher_generation_mismatch = (
            type(receipt_publisher_generation) is not int
            or not 1
            <= receipt_publisher_generation
            <= identity.publisher_generation
        )
        if (
            identity_mismatch_fields
            or publisher_generation_mismatch
        ):
            _LOGGER.error(
                "cleanup receipt identity mismatch request_id=%s fields=%s "
                "receipt_publisher_generation=%r controller_publisher_generation=%r "
                "receipt_cancellation_generation=%r "
                "controller_cancellation_generation=%r",
                identity.request_id,
                identity_mismatch_fields,
                receipt_publisher_generation,
                identity.publisher_generation,
                receipt.get("cancellation_generation"),
                identity.cancellation_generation,
            )
            raise RuntimeError("command_cleanup_receipt_identity_mismatch")
        if receipt.get("cleanup_owner_id") != owner_id:
            raise RuntimeError("command_cleanup_receipt_owner_mismatch")
        node_ids = receipt.get("node_ids", [])
        if not isinstance(node_ids, list) or not all(
            isinstance(node_id, str) and node_id for node_id in node_ids
        ):
            raise RuntimeError("command_cleanup_receipt_invalid")
        completed_at_ms = receipt.get("completed_at_monotonic_ms")
        now_ms = (
            completed_at_ms
            if isinstance(completed_at_ms, int)
            and not isinstance(completed_at_ms, bool)
            else int(time.monotonic() * 1_000)
        )
        cleanup = self._commands.record_cleanup(
            identity,
            owner_id=owner_id,
            result=CleanupResult(
                status=CleanupStatus.COMPLETED,
                released_resource_count=len(node_ids),
                result_digest=self._digest_document(receipt),
            ),
            observed_at_ms=now_ms,
            expected_cleanup_revision=0,
        )
        if not cleanup.accepted:
            raise RuntimeError(f"command_cleanup_rejected:{cleanup.reason}")

    def _record_command_terminal(
        self,
        pending: _Pending,
        terminal: str,
        *,
        cleanup_receipt: Mapping[str, Any] | None = None,
    ) -> str:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        if identity is None or owner_id is None:
            return terminal
        if terminal == "CANCELLED":
            if identity.cancellation_generation == 0:
                self._request_command_cancellation(pending)
                identity = pending.command_identity
                assert identity is not None
            status = TerminalStatus.CANCELLED
        elif terminal == "COMPLETED":
            status = TerminalStatus.COMPLETED
        else:
            status = TerminalStatus.ERROR
        self._record_command_cleanup(pending, receipt=cleanup_receipt)
        completed_at_ms = (
            cleanup_receipt.get("completed_at_monotonic_ms")
            if cleanup_receipt is not None
            else None
        )
        now_ms = (
            completed_at_ms
            if isinstance(completed_at_ms, int)
            and not isinstance(completed_at_ms, bool)
            else int(time.monotonic() * 1_000)
        )
        result = TerminalResult(
            identity=identity,
            status=status,
            observed_at_ms=now_ms,
            result_digest=self._digest_document(
                {"request_id": identity.request_id, "terminal": terminal}
            ),
            error_code="runtime_error" if status is TerminalStatus.ERROR else None,
        )
        mutation = self._commands.terminal_compare_and_swap(
            result,
            expected_terminal_revision=0,
        )
        if not mutation.accepted:
            snapshot = mutation.snapshot
            if (
                mutation.reason == "already_terminal"
                and snapshot is not None
                and snapshot.identity == identity
                and snapshot.terminal is not None
                and snapshot.terminal.status is status
                and snapshot.cleanup_result is not None
                and snapshot.cleanup_result.status is CleanupStatus.COMPLETED
            ):
                # The controller CAS is the terminal linearization point.  A
                # watchdog/cancellation observer can re-enter this adapter
                # after that CAS but before _Pending.terminal_status is
                # published. Exact identity, terminal status, and cleanup
                # proof make that observation idempotent; every conflicting
                # terminal continues to fail closed below.
                return terminal
            if (
                mutation.reason == "already_terminal"
                and snapshot is not None
                and snapshot.identity == identity
                and snapshot.terminal is not None
                and snapshot.terminal.status is TerminalStatus.DEADLINE_EXCEEDED
                and snapshot.cleanup_result is not None
                and snapshot.cleanup_result.status is CleanupStatus.COMPLETED
            ):
                # Cleanup proof and terminal CAS are deliberately separate.
                # If proof completes at the fixed cleanup deadline, the
                # controller can install DEADLINE_EXCEEDED between those two
                # operations. Preserve that authoritative terminal and project
                # it as a bounded failed request instead of stranding an exact,
                # fully cleaned command in the runtime cleanup phase.
                return "FAILED"
            raise RuntimeError(f"command_terminal_rejected:{mutation.reason}")
        return terminal

    def runtime_status(self) -> dict[str, Any] | None:
        coordinator = self._coordinator
        return None if coordinator is None else coordinator.status()

    def release_request(self, request_id: str) -> None:
        should_release = False
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if pending.terminal_status is None:
                pending.release_requested = True
            else:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def _release_route_request(self, pending: _Pending) -> None:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        scoped_release = getattr(self._route, "release_request_scoped", None)
        if (
            pending.execution_started
            and identity is not None
            and owner_id is not None
            and callable(scoped_release)
        ):
            scoped_release(
                identity.request_id,
                request_attempt=identity.request_attempt,
                path_id=identity.path_id,
                path_attempt=identity.path_attempt,
                path_digest=identity.path_digest,
                cleanup_owner_id=owner_id,
            )
            return
        request = pending.request
        if request is not None:
            self._route.release_request(request.request_id)

    def _retire_command(self, pending: _Pending) -> None:
        identity = pending.command_identity
        if identity is None:
            return
        retired = self._commands.retire(
            identity.request_id,
            expected_attempt=identity.request_attempt,
        )
        if not retired.accepted and retired.reason != "request_unknown":
            raise RuntimeError(f"command_retirement_rejected:{retired.reason}")

    def close(self, *, timeout_seconds: float = 4.0) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid_shutdown_timeout")
        shutdown_deadline = time.monotonic() + float(timeout_seconds)
        with self._changed:
            if self._closed:
                return
            self._closed = True
            request_ids = tuple(
                request_id
                for request_id, pending in self._pending.items()
                if pending.terminal_status is None
            )
            self._changed.notify_all()
        cancellation_errors: list[BaseException] = []

        def cancel_owned_request(request_id: str) -> None:
            try:
                self.cancel(request_id)
            except BaseException as error:
                cancellation_errors.append(error)

        cancellation_threads = tuple(
            threading.Thread(
                target=cancel_owned_request,
                args=(request_id,),
                name=f"live-route-shutdown-cancel-{index}",
                daemon=True,
            )
            for index, request_id in enumerate(request_ids)
        )
        for thread in cancellation_threads:
            thread.start()
        for thread in cancellation_threads:
            thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in cancellation_threads):
            raise RuntimeError("router_port_shutdown_cancellation_timeout")
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            if dispatcher.is_alive():
                raise RuntimeError("router_port_dispatcher_shutdown_timeout")
        self._worker_pool.shutdown(wait=False, cancel_futures=True)
        worker_threads = tuple(getattr(self._worker_pool, "_threads", ()))
        for thread in worker_threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in worker_threads):
            raise RuntimeError("router_port_worker_shutdown_timeout")
        if cancellation_errors:
            raise RuntimeError("router_port_shutdown_cancellation_failed") from (
                cancellation_errors[0]
            )
        with self._changed:
            releasable = tuple(
                (request_id, pending)
                for request_id, pending in self._pending.items()
                if pending.terminal_status is not None
            )
            blocked_request_ids = tuple(
                request_id
                for request_id, pending in self._pending.items()
                if pending.terminal_status is None
            )
            for request_id, _pending in releasable:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
        for request_id, pending in releasable:
            self._release_route_request(pending)
            self._retire_command(pending)
        if blocked_request_ids:
            raise RuntimeError("router_port_shutdown_cleanup_unproven")


__all__ = ["LiveRouterPort"]
