"""Independent, immutable Router-to-iroh adapter reference automaton.

The model deliberately describes observable protocol obligations rather than the
implementation's locks, threads, exceptions, or wire classes.  Asynchronous
boundaries are explicit actions (queue/send/reconnect/dispatch/ACK), so a test
can deterministically place close, rotation, cancellation, and deadline events
between them.  Payloads and receipt IDs are symbolic strings; ``frame_kind`` is
an independent canonical-ingress oracle with ``canonical``, ``malformed``, and
``truncated`` classes.

Only immutable stdlib values are used.  In particular, this module has no
production Router or sidecar imports.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

CLIENT_ROLES: Final[tuple[str, ...]] = (
    "send",
    "receive",
    "control",
    "forward",
    "cancellation-0",
    "cancellation-1",
)
LIFECYCLES: Final[frozenset[str]] = frozenset(
    {"NEW", "BOUND", "RUNNING", "CLOSING", "FATAL", "CLOSED"}
)
SEND_PHASES: Final[frozenset[str]] = frozenset(
    {"WAITING", "QUEUED", "SENDING", "RECONNECTING", "RETRYING", "CANCELLED"}
)
DELIVERY_PHASES: Final[frozenset[str]] = frozenset(
    {"RECEIVING", "RECEIVED", "DISPATCHING", "ACK_PENDING", "ACKING"}
)


@dataclass(frozen=True)
class EvidenceCounters:
    """Counters exposed as transport evidence after successful boundaries."""

    sent: int = 0
    received: int = 0
    dispatched: int = 0
    duplicates: int = 0

    def __post_init__(self) -> None:
        if min(self.sent, self.received, self.dispatched, self.duplicates) < 0:
            raise ValueError("evidence_counters_must_be_non_negative")
        if self.received != self.dispatched:
            raise ValueError("received_and_dispatched_evidence_must_match")


@dataclass(frozen=True)
class PendingSend:
    """One admitted send holding exactly one adapter queue permit."""

    message_id: str
    payload_id: str
    generation: int
    phase: str = "QUEUED"
    cancel_reason: str = ""

    def __post_init__(self) -> None:
        if not self.message_id or not self.payload_id:
            raise ValueError("pending_send_symbols_must_not_be_empty")
        if self.generation <= 0:
            raise ValueError("pending_send_generation_must_be_positive")
        if self.phase not in SEND_PHASES:
            raise ValueError("unknown_pending_send_phase")
        if (self.phase == "CANCELLED") != bool(self.cancel_reason):
            raise ValueError("cancelled_send_requires_exactly_one_reason")


@dataclass(frozen=True)
class ReconnectAttempt:
    """A replacement client connected outside the installation fence."""

    role: str
    replacement_id: str
    generation: int
    owner_message_id: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"send", "receive"}:
            raise ValueError("only_send_or_receive_clients_reconnect")
        if not self.replacement_id or self.generation <= 0:
            raise ValueError("invalid_reconnect_attempt")
        if self.role == "send" and not self.owner_message_id:
            raise ValueError("send_reconnect_requires_owner_message")
        if self.role == "receive" and self.owner_message_id:
            raise ValueError("receive_reconnect_has_no_owner_message")


@dataclass(frozen=True)
class ActiveDelivery:
    """A serial inbound delivery between receive, dispatch, and ACK fences."""

    message_id: str
    payload_id: str
    generation: int
    sequence: int
    phase: str = "RECEIVED"
    duplicate: bool = False

    def __post_init__(self) -> None:
        if not self.message_id or not self.payload_id:
            raise ValueError("delivery_symbols_must_not_be_empty")
        if self.generation <= 0 or self.sequence < 0:
            raise ValueError("invalid_delivery_generation_or_sequence")
        if self.phase not in DELIVERY_PHASES:
            raise ValueError("unknown_delivery_phase")
        if self.duplicate and self.phase in {"RECEIVED", "DISPATCHING"}:
            raise ValueError("duplicate_delivery_must_bypass_dispatch")


@dataclass(frozen=True)
class SeenDelivery:
    """Process-lifetime replay identity retained only after a successful ACK."""

    message_id: str
    payload_id: str


@dataclass(frozen=True)
class AdapterAction:
    """One deterministic external or scheduler action.

    Empty/``None`` parameters are resolved from current state.  This keeps
    generated traces compact while still allowing exact hand-written races.
    ``payload_id`` identifies canonical bytes without importing a wire codec.
    """

    name: str
    message_id: str | None = None
    payload_id: str = "frame-a"
    generation: int | None = None
    sequence: int | None = None
    frame_kind: str = "canonical"
    role: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("action_name_must_not_be_empty")
        if self.message_id is not None and not self.message_id:
            raise ValueError("message_id_must_be_none_or_non_empty")
        if not self.payload_id:
            raise ValueError("payload_id_must_not_be_empty")
        if self.generation is not None and not isinstance(self.generation, int):
            raise TypeError("generation_must_be_an_integer")
        if self.sequence is not None and not isinstance(self.sequence, int):
            raise TypeError("sequence_must_be_an_integer")

    @property
    def kind(self) -> str:
        """Compatibility spelling for harnesses that call actions events."""

        return self.name


@dataclass(frozen=True)
class ModelState:
    """Complete observable state plus immutable replay/interleaving context."""

    lifecycle: str = "NEW"
    router_bound: bool = False
    installed_client_roles: tuple[str, ...] = ()
    closed_installed_client_roles: tuple[str, ...] = ()
    closed_client_count: int = 0
    closed_replacement_count: int = 0
    pending_receipt_ids: tuple[str, ...] = ()
    pending_sends: tuple[PendingSend, ...] = ()
    queue_capacity: int = 2
    queue_permits: int = 2
    generation: int = 1
    dispatch_count: int = 0
    ack_count: int = 0
    cancellation_count: int = 0
    fatal_error: str | None = None
    fatal_detail: str = ""
    evidence: EvidenceCounters = EvidenceCounters()
    reconnect_attempts: tuple[ReconnectAttempt, ...] = ()
    active_delivery: ActiveDelivery | None = None
    seen_deliveries: tuple[SeenDelivery, ...] = ()
    receive_sequence: int = 0
    used_receipt_ids: tuple[str, ...] = ()
    next_receipt_index: int = 0
    next_delivery_index: int = 0
    next_replacement_index: int = 0

    def __post_init__(self) -> None:
        if self.lifecycle not in LIFECYCLES:
            raise ValueError("unknown_lifecycle")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity_must_be_positive")
        if not 0 <= self.queue_permits <= self.queue_capacity:
            raise ValueError("queue_permits_out_of_range")
        if self.queue_capacity - self.queue_permits != len(self.pending_sends):
            raise ValueError("one_permit_must_be_held_per_pending_send")
        pending_ids = tuple(item.message_id for item in self.pending_sends)
        if self.pending_receipt_ids != pending_ids:
            raise ValueError("pending_receipt_index_mismatch")
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("duplicate_pending_receipt")
        if not set(pending_ids) <= set(self.used_receipt_ids):
            raise ValueError("pending_receipts_must_be_marked_used")
        if len(set(self.used_receipt_ids)) != len(self.used_receipt_ids):
            raise ValueError("duplicate_used_receipt")
        if tuple(role for role in CLIENT_ROLES if role in self.installed_client_roles) != (
            self.installed_client_roles
        ):
            raise ValueError("installed_client_roles_must_have_stable_order")
        if len(set(self.installed_client_roles)) != len(self.installed_client_roles):
            raise ValueError("duplicate_installed_client_role")
        if not set(self.closed_installed_client_roles) <= set(
            self.installed_client_roles
        ):
            raise ValueError("closed_installed_clients_must_remain_installed")
        reconnect_roles = tuple(attempt.role for attempt in self.reconnect_attempts)
        if len(set(reconnect_roles)) != len(reconnect_roles):
            raise ValueError("one_reconnect_per_role")
        if self.lifecycle == "RUNNING" and self.installed_client_roles != CLIENT_ROLES:
            raise ValueError("running_requires_all_client_roles")
        if self.lifecycle in {"CLOSING", "CLOSED"} and self.installed_client_roles:
            raise ValueError("closed_lifecycle_cannot_have_installed_clients")
        if self.lifecycle == "CLOSING" and "receive" not in reconnect_roles:
            raise ValueError("closing_only_waits_for_receive_reconnect")
        if self.generation <= 0 or self.receive_sequence < 0:
            raise ValueError("generation_and_sequence_must_be_non_negative")
        counters = (
            self.closed_client_count,
            self.closed_replacement_count,
            self.dispatch_count,
            self.ack_count,
            self.cancellation_count,
            self.next_receipt_index,
            self.next_delivery_index,
            self.next_replacement_index,
        )
        if min(counters) < 0:
            raise ValueError("state_counters_must_be_non_negative")
        if self.closed_replacement_count > self.closed_client_count:
            raise ValueError("replacement_closures_are_client_closures")
        if self.evidence.dispatched > self.dispatch_count:
            raise ValueError("evidence_cannot_exceed_dispatch_attempts")
        if (self.lifecycle == "FATAL") != (self.fatal_error is not None):
            if self.lifecycle not in {"CLOSING", "CLOSED"} or self.fatal_error is None:
                raise ValueError("fatal_error_lifecycle_mismatch")

    @property
    def lifecycle_state(self) -> str:
        return self.lifecycle

    @property
    def installed_roles(self) -> tuple[str, ...]:
        return self.installed_client_roles

    @property
    def permits(self) -> int:
        return self.queue_permits

    @property
    def sent(self) -> int:
        return self.evidence.sent

    @property
    def received(self) -> int:
        return self.evidence.received

    @property
    def dispatched(self) -> int:
        return self.evidence.dispatched

    @property
    def duplicates(self) -> int:
        return self.evidence.duplicates

    @property
    def closed_replacement_client_count(self) -> int:
        return self.closed_replacement_count


@dataclass(frozen=True)
class Transition:
    """Result with a stable disposition and explicit mutation observation."""

    state: ModelState
    accepted: bool
    mutated: bool
    code: str
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class IrohAdapterModel:
    """Pure deterministic reference automaton for adapter conformance."""

    queue_capacity: int = 2
    initial_generation: int = 1
    seen_limit: int = 8

    def __post_init__(self) -> None:
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity_must_be_positive")
        if self.initial_generation <= 0:
            raise ValueError("initial_generation_must_be_positive")
        if self.seen_limit <= 0:
            raise ValueError("seen_limit_must_be_positive")

    def initial_state(self, *, router_bound: bool = False) -> ModelState:
        return ModelState(
            lifecycle="BOUND" if router_bound else "NEW",
            router_bound=router_bound,
            queue_capacity=self.queue_capacity,
            queue_permits=self.queue_capacity,
            generation=self.initial_generation,
        )

    def apply(self, state: ModelState, action: AdapterAction) -> Transition:
        """Apply one action; every rejection preserves object identity."""

        name = action.name.strip().lower().replace("-", "_")
        aliases = {
            "bind": "bind_router",
            "register_router": "bind_router",
            "restart": "start",
            "send_frame": "send",
            "complete_send": "send_confirmed",
            "send_disconnect": "send_reconnect_begin",
            "receive_disconnect": "receive_reconnect_begin",
            "receive_frame": "receive_delivery",
            "dispatch_success": "dispatch_complete",
            "ack": "ack_success",
            "ack_lost": "ack_fail",
            "lost_ack": "ack_fail",
            "delayed_ack": "ack_delayed",
            "delay_ack": "ack_delayed",
            "lose_confirmation": "deadline",
            "cancel_send": "deadline",
        }
        name = aliases.get(name, name)
        semantic = self._resolve_semantic_receive(state, action, name)
        if isinstance(semantic, Transition):
            return semantic
        action, name = semantic
        if name == "send_reconnect_complete":
            action = replace(action, role="send")
            name = "reconnect_complete"
        elif name == "receive_reconnect_complete":
            action = replace(action, role="receive")
            name = "reconnect_complete"
        elif name == "send_reconnect_fail":
            action = replace(action, role="send")
            name = "reconnect_fail"
        elif name == "receive_reconnect_fail":
            action = replace(action, role="receive")
            name = "reconnect_fail"
        elif name == "send_reconnect_begin":
            action = replace(action, role="send")
            name = "reconnect_begin"
        elif name == "receive_reconnect_begin":
            action = replace(action, role="receive")
            name = "reconnect_begin"
        handler = getattr(self, f"_on_{name}", None)
        if handler is None:
            return self._reject(state, "unknown_action")
        return handler(state, action)

    def _resolve_semantic_receive(
        self, state: ModelState, action: AdapterAction, name: str
    ) -> tuple[AdapterAction, str] | Transition:
        frame_aliases = {
            "receive_malformed_frame": "malformed",
            "receive_truncated_frame": "truncated",
            "malformed_frame": "malformed",
            "truncated_frame": "truncated",
        }
        if name in frame_aliases:
            return replace(action, frame_kind=frame_aliases[name]), "receive_delivery"
        if name in {"receive_stale_sequence", "receive_future_sequence"}:
            offset = -1 if name.endswith("stale_sequence") else 1
            return replace(action, sequence=state.receive_sequence + offset), "receive_delivery"
        if name in {"receive_stale_generation", "receive_future_generation"}:
            offset = -1 if name.endswith("stale_generation") else 1
            return replace(action, generation=state.generation + offset), "receive_delivery"
        if name in {"receive_exact_replay", "receive_collision"}:
            if not state.seen_deliveries:
                return self._reject(state, "replay_history_empty")
            seen = state.seen_deliveries[-1]
            payload = (
                seen.payload_id
                if name == "receive_exact_replay"
                else f"{seen.payload_id}#collision"
            )
            return (
                replace(action, message_id=seen.message_id, payload_id=payload),
                "receive_delivery",
            )
        return action, name

    def _on_bind_router(self, state: ModelState, _action: AdapterAction) -> Transition:
        if state.router_bound:
            return self._reject(state, "router_already_bound")
        if state.lifecycle in {"RUNNING", "FATAL", "CLOSING", "CLOSED"}:
            return self._reject(state, "cannot_bind_router_after_start")
        updated = replace(state, lifecycle="BOUND", router_bound=True)
        return self._accept(state, updated, "router_bound")

    def _on_start(self, state: ModelState, _action: AdapterAction) -> Transition:
        if state.lifecycle in {"CLOSING", "CLOSED"}:
            return self._reject(state, "transport_closed")
        if state.fatal_error is not None:
            return self._reject(state, state.fatal_error)
        if state.lifecycle == "RUNNING":
            return self._accept(state, state, "already_running")
        if not state.router_bound:
            return self._reject(state, "router_not_bound")
        updated = replace(
            state,
            lifecycle="RUNNING",
            installed_client_roles=CLIENT_ROLES,
            closed_installed_client_roles=(),
        )
        return self._accept(state, updated, "started", ("clients_installed",))

    def _on_close(self, state: ModelState, _action: AdapterAction) -> Transition:
        if state.lifecycle in {"CLOSING", "CLOSED"}:
            return self._accept(state, state, "already_closed")
        pending, newly_cancelled = self._cancel_pending(state.pending_sends, "transport_closed")
        waits_for_receive = any(
            attempt.role == "receive" for attempt in state.reconnect_attempts
        )
        already_closed_installed = len(state.closed_installed_client_roles)
        updated = self._with_pending(
            state,
            pending,
            lifecycle="CLOSING" if waits_for_receive else "CLOSED",
            installed_client_roles=(),
            closed_installed_client_roles=(),
            closed_client_count=(
                state.closed_client_count
                + len(state.installed_client_roles)
                - already_closed_installed
            ),
            cancellation_count=state.cancellation_count + newly_cancelled,
            active_delivery=None,
        )
        return self._accept(
            state,
            updated,
            "close_blocked_on_receive_reconnect" if waits_for_receive else "closed",
            ("clients_closed",),
        )

    def _on_fatal_receive(self, state: ModelState, action: AdapterAction) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        return self._fatal(state, action.error or "fatal_receive")

    def _on_rotate_peer(self, state: ModelState, action: AdapterAction) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        generation = state.generation + 1 if action.generation is None else action.generation
        if generation <= state.generation:
            return self._reject(state, "stale_peer_generation")
        pending, newly_cancelled = self._cancel_pending(
            state.pending_sends,
            "peer_rotated",
            before_generation=generation,
        )
        updated = self._with_pending(
            state,
            pending,
            generation=generation,
            cancellation_count=state.cancellation_count + newly_cancelled,
        )
        active = state.active_delivery
        if active is not None and active.generation < generation:
            if active.phase == "RECEIVING":
                return self._accept(
                    state,
                    updated,
                    "peer_rotated",
                    ("generation_committed",),
                )
            if active.phase == "DISPATCHING":
                code = "peer_rotated_during_dispatch"
                updated = replace(
                    updated,
                    dispatch_count=updated.dispatch_count + 1,
                )
            elif active.phase == "ACK_PENDING":
                code = "peer_rotated_during_ack"
            elif active.phase == "ACKING":
                # Rotation fences the sidecar session while ACK is on the wire;
                # the deterministic outcome is an ACK failure, not success.
                code = "ack_failed"
            else:
                code = "peer_rotated_before_dispatch"
            updated = replace(
                updated,
                lifecycle="FATAL",
                fatal_error=code,
                active_delivery=None,
            )
            return self._accept(state, updated, code, ("generation_committed", "fatal"))
        return self._accept(state, updated, "peer_rotated", ("generation_committed",))

    # ----- outbound confirmed-send path -----

    def _on_queue_send(self, state: ModelState, action: AdapterAction) -> Transition:
        return self._admit_send(state, action, phase="QUEUED")

    def _on_send(self, state: ModelState, action: AdapterAction) -> Transition:
        return self._admit_send(state, action, phase="SENDING")

    def _admit_send(
        self, state: ModelState, action: AdapterAction, *, phase: str
    ) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        if action.frame_kind != "canonical":
            return self._reject(state, "malformed_router_frame")
        generation = state.generation if action.generation is None else action.generation
        if generation < state.generation:
            return self._reject(state, "stale_peer_generation")
        if generation > state.generation:
            return self._reject(state, "future_peer_generation")
        if state.queue_permits == 0:
            return self._reject(state, "adapter_queue_full")
        message_id = action.message_id or f"send-{state.next_receipt_index}"
        if message_id in state.used_receipt_ids:
            return self._reject(state, "receipt_id_reused")
        selected_phase = "WAITING" if state.pending_sends else phase
        pending = state.pending_sends + (
            PendingSend(message_id, action.payload_id, generation, selected_phase),
        )
        updated = self._with_pending(
            state,
            pending,
            queue_permits=state.queue_permits - 1,
            used_receipt_ids=state.used_receipt_ids + (message_id,),
            next_receipt_index=state.next_receipt_index + int(action.message_id is None),
        )
        return self._accept(state, updated, "send_queued", ("permit_acquired",))

    def _on_send_begin(self, state: ModelState, action: AdapterAction) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        item = state.pending_sends[index]
        if item.phase != "QUEUED":
            return self._reject(state, "send_not_queued")
        pending = self._replace_item(state.pending_sends, index, replace(item, phase="SENDING"))
        updated = self._with_pending(state, pending)
        return self._accept(state, updated, "send_started")

    def _on_send_confirmed(self, state: ModelState, action: AdapterAction) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        item = state.pending_sends[index]
        if item.phase == "WAITING":
            return self._reject(state, "send_waiting_for_data_client")
        if item.phase == "RECONNECTING":
            return self._reject(state, "send_reconnecting")
        if item.phase == "CANCELLED":
            updated = self._release_pending(state, index)
            return self._accept(
                state, updated, item.cancel_reason, ("permit_released",)
            )
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        if item.generation != state.generation:
            updated = self._release_pending(state, index)
            return self._accept(state, updated, "peer_rotated", ("permit_released",))
        updated = self._release_pending(state, index)
        updated = replace(
            updated,
            evidence=replace(updated.evidence, sent=updated.evidence.sent + 1),
        )
        return self._accept(
            state,
            updated,
            "delivery_confirmed",
            ("receipt_returned", "permit_released"),
        )

    def _on_send_failed(self, state: ModelState, action: AdapterAction) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        updated = self._release_pending(state, index)
        return self._accept(
            state,
            updated,
            action.error or "delivery_not_confirmed",
            ("permit_released",),
        )

    def _on_deadline(self, state: ModelState, action: AdapterAction) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        item = state.pending_sends[index]
        if item.phase == "CANCELLED":
            return self._accept(state, state, "cancellation_already_started")
        cancelled = replace(
            item,
            phase="CANCELLED",
            cancel_reason="delivery_deadline_exceeded",
        )
        pending = self._replace_item(state.pending_sends, index, cancelled)
        updated = self._with_pending(
            state,
            pending,
            cancellation_count=state.cancellation_count + 1,
        )
        return self._accept(
            state, updated, "delivery_deadline_exceeded", ("cancel_sent",)
        )

    def _on_finish_cancelled(self, state: ModelState, action: AdapterAction) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        item = state.pending_sends[index]
        if item.phase != "CANCELLED":
            return self._reject(state, "pending_send_not_cancelled")
        updated = self._release_pending(state, index)
        return self._accept(
            state, updated, item.cancel_reason, ("permit_released",)
        )

    def _on_delay_confirmation(
        self, state: ModelState, action: AdapterAction
    ) -> Transition:
        index = self._pending_index(state, action.message_id)
        if index is None:
            return self._reject(state, "unknown_pending_receipt")
        return self._accept(state, state, "confirmation_delayed")

    # ----- explicit reconnect installation fences -----

    def _on_reconnect_begin(self, state: ModelState, action: AdapterAction) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        role = action.role or "receive"
        if role not in {"send", "receive"}:
            return self._reject(state, "invalid_reconnect_role")
        if any(attempt.role == role for attempt in state.reconnect_attempts):
            return self._reject(state, "reconnect_already_pending")
        owner = ""
        pending = state.pending_sends
        if role == "send":
            index = self._pending_index(state, action.message_id)
            if index is None:
                return self._reject(state, "unknown_pending_receipt")
            item = pending[index]
            if item.phase not in {"QUEUED", "SENDING", "RETRYING"}:
                return self._reject(state, "send_cannot_reconnect")
            owner = item.message_id
            pending = self._replace_item(
                pending, index, replace(item, phase="RECONNECTING")
            )
        attempt = ReconnectAttempt(
            role=role,
            replacement_id=f"{role}-replacement-{state.next_replacement_index}",
            generation=state.generation,
            owner_message_id=owner,
        )
        updated = self._with_pending(
            state,
            pending,
            reconnect_attempts=state.reconnect_attempts + (attempt,),
            next_replacement_index=state.next_replacement_index + 1,
            closed_client_count=(
                state.closed_client_count + (1 if role == "receive" else 0)
            ),
            closed_installed_client_roles=(
                state.closed_installed_client_roles + ("receive",)
                if role == "receive"
                else state.closed_installed_client_roles
            ),
        )
        return self._accept(state, updated, f"{role}_reconnect_blocked")

    def _on_reconnect_complete(
        self, state: ModelState, action: AdapterAction
    ) -> Transition:
        role = action.role or "receive"
        selected = self._reconnect_index(state, role)
        if selected is None:
            return self._reject(state, "reconnect_not_pending")
        attempt = state.reconnect_attempts[selected]
        reconnects = state.reconnect_attempts[:selected] + state.reconnect_attempts[selected + 1 :]
        pending = state.pending_sends
        changes: dict[str, object] = {"reconnect_attempts": reconnects}
        if state.lifecycle != "RUNNING":
            changes["closed_client_count"] = state.closed_client_count + 1
            changes["closed_replacement_count"] = state.closed_replacement_count + 1
            if attempt.owner_message_id:
                owner_index = self._pending_index(state, attempt.owner_message_id)
                if owner_index is not None:
                    pending = pending[:owner_index] + pending[owner_index + 1 :]
                    changes["queue_permits"] = state.queue_permits + 1
            changes["lifecycle"] = self._after_reconnect_lifecycle(state, reconnects)
            updated = self._with_pending(state, pending, **changes)
            return self._accept(
                state,
                updated,
                "reconnect_fenced_by_close"
                if state.lifecycle in {"CLOSING", "CLOSED"}
                else "reconnect_fenced_by_fatal",
                ("replacement_closed",),
            )
        if role == "send":
            changes["closed_client_count"] = state.closed_client_count + 1
        else:
            changes["closed_installed_client_roles"] = tuple(
                installed_role
                for installed_role in state.closed_installed_client_roles
                if installed_role != "receive"
            )
        if attempt.owner_message_id:
            owner_index = self._pending_index(state, attempt.owner_message_id)
            if owner_index is not None:
                owner = pending[owner_index]
                next_phase = (
                    "CANCELLED"
                    if owner.phase == "CANCELLED" or owner.generation != state.generation
                    else "RETRYING"
                )
                reason = owner.cancel_reason
                if next_phase == "CANCELLED" and not reason:
                    reason = "peer_rotated"
                    changes["cancellation_count"] = state.cancellation_count + 1
                pending = self._replace_item(
                    pending,
                    owner_index,
                    replace(owner, phase=next_phase, cancel_reason=reason),
                )
        updated = self._with_pending(state, pending, **changes)
        return self._accept(
            state, updated, f"{role}_reconnect_installed", ("old_client_closed",)
        )

    def _on_reconnect_fail(self, state: ModelState, action: AdapterAction) -> Transition:
        role = action.role or "receive"
        selected = self._reconnect_index(state, role)
        if selected is None:
            return self._reject(state, "reconnect_not_pending")
        attempt = state.reconnect_attempts[selected]
        reconnects = state.reconnect_attempts[:selected] + state.reconnect_attempts[selected + 1 :]
        pending = state.pending_sends
        permits = state.queue_permits
        if attempt.owner_message_id:
            owner_index = self._pending_index(state, attempt.owner_message_id)
            if owner_index is not None:
                pending = pending[:owner_index] + pending[owner_index + 1 :]
                permits += 1
        updated = self._with_pending(
            state,
            pending,
            queue_permits=permits,
            reconnect_attempts=reconnects,
            closed_client_count=state.closed_client_count + 1,
            closed_replacement_count=state.closed_replacement_count + 1,
            lifecycle=self._after_reconnect_lifecycle(state, reconnects),
        )
        if role == "receive" and state.lifecycle == "RUNNING":
            updated = replace(
                updated,
                lifecycle="FATAL",
                fatal_error="sidecar_receive_reconnect_failed",
                fatal_detail=action.error or "reconnect_failed",
            )
            return self._accept(
                state, updated, "sidecar_receive_reconnect_failed", ("fatal",)
            )
        return self._accept(
            state,
            updated,
            "delivery_not_confirmed" if role == "send" else "replacement_closed",
            ("replacement_closed",),
        )

    # ----- inbound receive -> dispatch -> ACK path -----

    def _on_receive_begin(self, state: ModelState, action: AdapterAction) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        if any(attempt.role == "receive" for attempt in state.reconnect_attempts):
            return self._reject(state, "receive_reconnecting")
        if state.active_delivery is not None:
            return self._reject(state, "receive_busy")
        sequence = state.receive_sequence if action.sequence is None else action.sequence
        if sequence != state.receive_sequence:
            relation = "stale_sequence" if sequence < state.receive_sequence else "future_sequence"
            return self._fatal(
                state,
                "sequence_gap",
                detail=relation,
                disposition=relation,
            )
        generation = state.generation if action.generation is None else action.generation
        message_id = action.message_id or f"recv-{state.next_delivery_index}"
        active = ActiveDelivery(
            message_id=message_id,
            payload_id=action.payload_id,
            generation=generation,
            sequence=sequence,
            phase="RECEIVING",
        )
        updated = replace(
            state,
            active_delivery=active,
            receive_sequence=state.receive_sequence + 1,
            next_delivery_index=(
                state.next_delivery_index + int(action.message_id is None)
            ),
        )
        return self._accept(state, updated, "receive_started")

    def _on_receive_complete(
        self, state: ModelState, _action: AdapterAction
    ) -> Transition:
        active = state.active_delivery
        if active is None or active.phase != "RECEIVING":
            return self._reject(state, "receive_not_in_progress")
        if active.generation != state.generation:
            relation = (
                "stale_peer_generation"
                if active.generation < state.generation
                else "future_peer_generation"
            )
            return self._fatal(state, "peer_rotated", detail=relation)
        previous = next(
            (
                item
                for item in state.seen_deliveries
                if item.message_id == active.message_id
            ),
            None,
        )
        if previous is not None and previous.payload_id != active.payload_id:
            return self._fatal(
                state,
                "replay_collision",
                detail=active.message_id,
            )
        duplicate = previous is not None
        evidence = state.evidence
        if duplicate:
            evidence = replace(evidence, duplicates=evidence.duplicates + 1)
        updated = replace(
            state,
            active_delivery=replace(
                active,
                phase="ACK_PENDING" if duplicate else "RECEIVED",
                duplicate=duplicate,
            ),
            evidence=evidence,
        )
        return self._accept(
            state,
            updated,
            "exact_replay" if duplicate else "delivery_received",
        )

    def _on_receive_delivery(
        self, state: ModelState, action: AdapterAction
    ) -> Transition:
        invalid = self._require_running(state)
        if invalid is not None:
            return invalid
        if any(attempt.role == "receive" for attempt in state.reconnect_attempts):
            return self._reject(state, "receive_reconnecting")
        if state.active_delivery is not None:
            return self._reject(state, "receive_busy")
        sequence = state.receive_sequence if action.sequence is None else action.sequence
        generation = state.generation if action.generation is None else action.generation
        message_id = action.message_id or f"recv-{state.next_delivery_index}"
        if sequence != state.receive_sequence:
            relation = "stale_sequence" if sequence < state.receive_sequence else "future_sequence"
            return self._fatal(
                state,
                "sequence_gap",
                detail=relation,
                disposition=relation,
            )
        consumed = replace(
            state,
            receive_sequence=state.receive_sequence + 1,
            next_delivery_index=(
                state.next_delivery_index + int(action.message_id is None)
            ),
        )
        if generation != state.generation:
            relation = (
                "stale_peer_generation"
                if generation < state.generation
                else "future_peer_generation"
            )
            return self._fatal(
                consumed,
                "peer_rotated",
                detail=relation,
                disposition=relation,
                previous=state,
            )
        if action.frame_kind != "canonical":
            detail = (
                action.frame_kind
                if action.frame_kind in {"malformed", "truncated"}
                else "unknown_frame_class"
            )
            return self._fatal(
                consumed,
                "malformed_router_frame",
                detail=detail,
                previous=state,
            )
        previous = next(
            (item for item in state.seen_deliveries if item.message_id == message_id),
            None,
        )
        if previous is not None and previous.payload_id != action.payload_id:
            return self._fatal(
                consumed,
                "replay_collision",
                detail=message_id,
                previous=state,
            )
        duplicate = previous is not None
        active = ActiveDelivery(
            message_id=message_id,
            payload_id=action.payload_id,
            generation=generation,
            sequence=sequence,
            phase="ACK_PENDING" if duplicate else "RECEIVED",
            duplicate=duplicate,
        )
        evidence = consumed.evidence
        if duplicate:
            evidence = replace(evidence, duplicates=evidence.duplicates + 1)
        updated = replace(consumed, active_delivery=active, evidence=evidence)
        return self._accept(
            state,
            updated,
            "exact_replay" if duplicate else "delivery_received",
            ("duplicate_detected",) if duplicate else (),
        )

    def _on_dispatch_begin(self, state: ModelState, _action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase != "RECEIVED":
            return self._reject(state, "delivery_not_ready_for_dispatch")
        updated = replace(
            state,
            active_delivery=replace(active, phase="DISPATCHING"),
        )
        return self._accept(state, updated, "dispatch_started")

    def _on_dispatch(self, state: ModelState, action: AdapterAction) -> Transition:
        started = self._on_dispatch_begin(state, action)
        if not started.accepted:
            return started
        completed = self._on_dispatch_complete(started.state, action)
        return Transition(
            state=completed.state,
            accepted=completed.accepted,
            mutated=completed.state != state,
            code=completed.code,
            effects=("dispatch_started",) + completed.effects,
        )

    def _on_dispatch_complete(
        self, state: ModelState, _action: AdapterAction
    ) -> Transition:
        active = state.active_delivery
        if active is None or active.phase != "DISPATCHING":
            return self._reject(state, "dispatch_not_in_progress")
        if active.generation != state.generation:
            return self._fatal(state, "peer_rotated_during_dispatch")
        updated = replace(
            state,
            active_delivery=replace(active, phase="ACK_PENDING"),
            dispatch_count=state.dispatch_count + 1,
        )
        return self._accept(state, updated, "dispatch_succeeded")

    def _on_dispatch_fail(self, state: ModelState, action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase not in {"RECEIVED", "DISPATCHING"}:
            return self._reject(state, "dispatch_not_in_progress")
        dispatch_count = state.dispatch_count + int(active.phase == "RECEIVED")
        failed = replace(state, dispatch_count=dispatch_count)
        return self._fatal(
            failed,
            "router_dispatch_failed",
            detail=action.error or "dispatch_failed",
            previous=state,
        )

    def _on_ack_begin(self, state: ModelState, _action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase != "ACK_PENDING":
            return self._reject(state, "delivery_not_ready_for_ack")
        if active.generation != state.generation:
            return self._fatal(state, "peer_rotated_during_ack")
        updated = replace(state, active_delivery=replace(active, phase="ACKING"))
        return self._accept(state, updated, "ack_started")

    def _on_ack_success(self, state: ModelState, action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase not in {"ACK_PENDING", "ACKING"}:
            return self._reject(state, "ack_not_in_progress")
        if active.phase == "ACK_PENDING":
            begun = self._on_ack_begin(state, action)
            if begun.state.lifecycle == "FATAL" or not begun.accepted:
                return begun
            state = begun.state
            active = state.active_delivery
            assert active is not None
        if active.generation != state.generation:
            return self._fatal(state, "peer_rotated_during_ack")
        evidence = state.evidence
        seen = state.seen_deliveries
        effects: tuple[str, ...] = ("ack_sent",)
        if not active.duplicate:
            seen = seen + (SeenDelivery(active.message_id, active.payload_id),)
            if len(seen) > self.seen_limit:
                seen = seen[-self.seen_limit :]
            evidence = replace(
                evidence,
                received=evidence.received + 1,
                dispatched=evidence.dispatched + 1,
            )
            effects += ("evidence_committed",)
        updated = replace(
            state,
            active_delivery=None,
            seen_deliveries=seen,
            ack_count=state.ack_count + 1,
            evidence=evidence,
        )
        return self._accept(state, updated, "acked", effects)

    def _on_ack_delayed(self, state: ModelState, _action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase not in {"ACK_PENDING", "ACKING"}:
            return self._reject(state, "ack_not_in_progress")
        return self._accept(state, state, "ack_delayed")

    def _on_ack_fail(self, state: ModelState, action: AdapterAction) -> Transition:
        active = state.active_delivery
        if active is None or active.phase not in {"ACK_PENDING", "ACKING"}:
            return self._reject(state, "ack_not_in_progress")
        return self._fatal(
            state,
            "ack_failed",
            detail=action.error or "ack_lost",
        )

    # ----- immutable helpers and invariants -----

    @staticmethod
    def _pending_index(state: ModelState, message_id: str | None) -> int | None:
        if message_id is None:
            return 0 if state.pending_sends else None
        return next(
            (
                index
                for index, pending in enumerate(state.pending_sends)
                if pending.message_id == message_id
            ),
            None,
        )

    @staticmethod
    def _reconnect_index(state: ModelState, role: str) -> int | None:
        return next(
            (
                index
                for index, attempt in enumerate(state.reconnect_attempts)
                if attempt.role == role
            ),
            None,
        )

    @staticmethod
    def _replace_item(
        items: tuple[PendingSend, ...], index: int, item: PendingSend
    ) -> tuple[PendingSend, ...]:
        return items[:index] + (item,) + items[index + 1 :]

    @staticmethod
    def _with_pending(
        state: ModelState,
        pending: tuple[PendingSend, ...],
        **changes: object,
    ) -> ModelState:
        values: dict[str, object] = {
            "pending_sends": pending,
            "pending_receipt_ids": tuple(item.message_id for item in pending),
        }
        values.update(changes)
        return replace(state, **values)

    def _release_pending(self, state: ModelState, index: int) -> ModelState:
        pending = state.pending_sends[:index] + state.pending_sends[index + 1 :]
        if pending and pending[0].phase == "WAITING":
            pending = self._replace_item(
                pending,
                0,
                replace(pending[0], phase="QUEUED"),
            )
        return self._with_pending(
            state,
            pending,
            queue_permits=state.queue_permits + 1,
        )

    @staticmethod
    def _cancel_pending(
        pending: tuple[PendingSend, ...],
        reason: str,
        *,
        before_generation: int | None = None,
    ) -> tuple[tuple[PendingSend, ...], int]:
        changed: list[PendingSend] = []
        count = 0
        for item in pending:
            should_cancel = (
                item.phase != "CANCELLED"
                and (before_generation is None or item.generation < before_generation)
            )
            if should_cancel:
                sidecar_cancellation = item.phase != "WAITING"
                item = replace(item, phase="CANCELLED", cancel_reason=reason)
                count += int(sidecar_cancellation)
            changed.append(item)
        return tuple(changed), count

    @staticmethod
    def _after_reconnect_lifecycle(
        state: ModelState, reconnects: tuple[ReconnectAttempt, ...]
    ) -> str:
        if state.lifecycle == "CLOSING" and not any(
            attempt.role == "receive" for attempt in reconnects
        ):
            return "CLOSED"
        return state.lifecycle

    @staticmethod
    def _require_running(state: ModelState) -> Transition | None:
        if state.lifecycle in {"CLOSING", "CLOSED"}:
            return IrohAdapterModel._reject(state, "transport_closed")
        if state.fatal_error is not None:
            return IrohAdapterModel._reject(state, state.fatal_error)
        if state.lifecycle != "RUNNING":
            return IrohAdapterModel._reject(state, "transport_not_running")
        return None

    @staticmethod
    def _reject(state: ModelState, code: str) -> Transition:
        return Transition(state=state, accepted=False, mutated=False, code=code)

    @staticmethod
    def _accept(
        previous: ModelState,
        updated: ModelState,
        code: str,
        effects: tuple[str, ...] = (),
    ) -> Transition:
        if updated == previous:
            updated = previous
        return Transition(
            state=updated,
            accepted=True,
            mutated=updated is not previous,
            code=code,
            effects=effects,
        )

    def _fatal(
        self,
        state: ModelState,
        code: str,
        *,
        detail: str = "",
        disposition: str | None = None,
        previous: ModelState | None = None,
    ) -> Transition:
        original = state if previous is None else previous
        updated = replace(
            state,
            lifecycle="FATAL",
            fatal_error=code,
            fatal_detail=detail,
            active_delivery=None,
        )
        return self._accept(
            original,
            updated,
            disposition or code,
            ("fatal",),
        )


# Concise public spellings used by different conformance harness styles.
Action = AdapterAction
ModelAction = AdapterAction
AdapterState = ModelState
ReferenceAutomaton = IrohAdapterModel
AdapterModel = IrohAdapterModel


__all__ = [
    "Action",
    "ActiveDelivery",
    "AdapterAction",
    "AdapterModel",
    "AdapterState",
    "CLIENT_ROLES",
    "EvidenceCounters",
    "IrohAdapterModel",
    "ModelAction",
    "ModelState",
    "PendingSend",
    "ReconnectAttempt",
    "ReferenceAutomaton",
    "SeenDelivery",
    "Transition",
]
