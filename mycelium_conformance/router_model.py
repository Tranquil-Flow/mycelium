"""Independent request-lifecycle automaton derived from Router documentation.

This model intentionally uses immutable values and declarative invariants.  It does
not import or mirror production Router transition tables.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


TERMINAL_PHASES = frozenset({"COMPLETED", "CANCELLED", "FAILED"})


@dataclass(frozen=True)
class ModelEvent:
    """One externally meaningful lifecycle or replay event."""

    kind: str
    path_id: str = ""
    path_attempt: int | None = None
    sequence: int | None = None
    peer: str = ""
    hop_index: int | None = None
    payload: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ModelState:
    """Immutable state used to prove rejection cannot partially mutate."""

    phase: str = "NEW"
    path_id: str = ""
    path_attempt: int = -1
    next_sequence: int = 0
    emitted_tokens: tuple[int, ...] = ()
    reservations: int = 0
    release_count: int = 0
    runtime_cancel_count: int = 0
    terminal_count: int = 0
    recovery_count: int = 0
    preserved_context: tuple[int, ...] = ()
    recovery_uses_transferred_kv: bool = False
    hop_executions: int = 0
    accepted_events: tuple[ModelEvent, ...] = ()


@dataclass(frozen=True)
class Transition:
    """State plus stable harness disposition/error taxonomy."""

    state: ModelState
    accepted: bool
    mutated: bool
    code: str
    milestones: tuple[str, ...] = ()


class RouterModel:
    """Pure deterministic Router request automaton.

    Rules come from ``ROUTER_HANDOVER.md``: ordered admission/prefill/lock,
    attempt-scoped replay protection, final-peer token ownership, terminal
    exactly-once cleanup, and recovery by recomputing prompt plus committed
    tokens rather than transferring KV state.
    """

    def __init__(
        self,
        *,
        prompt_tokens: tuple[int, ...],
        maximum_new_tokens: int,
        path_width: int,
        maximum_recovery_attempts: int,
    ) -> None:
        if maximum_new_tokens <= 0:
            raise ValueError("maximum_new_tokens_must_be_positive")
        if path_width <= 0:
            raise ValueError("path_width_must_be_positive")
        if maximum_recovery_attempts < 0:
            raise ValueError("maximum_recovery_attempts_must_be_non_negative")
        self.prompt_tokens = tuple(prompt_tokens)
        self.maximum_new_tokens = maximum_new_tokens
        self.path_width = path_width
        self.maximum_recovery_attempts = maximum_recovery_attempts

    @staticmethod
    def initial_state() -> ModelState:
        return ModelState()

    def apply(self, state: ModelState, event: ModelEvent) -> Transition:
        """Apply one event; every rejection returns the identical state object."""

        handler = getattr(self, f"_on_{event.kind.lower()}", None)
        if handler is None:
            return self._reject(state, "unknown_event")
        return handler(state, event)

    def _on_admit(self, state: ModelState, event: ModelEvent) -> Transition:
        if state.phase != "NEW":
            return self._reject(state, "duplicate_request")
        updated = replace(
            state,
            phase="ADMITTING",
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("ADMISSION",))

    def _on_begin_prefill(
        self, state: ModelState, event: ModelEvent
    ) -> Transition:
        if state.phase == "NEW":
            return self._reject(state, "unknown_request")
        if state.phase != "ADMITTING":
            return self._reject(state, "invalid_phase")
        if event.path_attempt != 0:
            return self._reject(state, "future_path_attempt")
        if not event.path_id:
            return self._reject(state, "path_mismatch")
        updated = replace(
            state,
            phase="PREFILL",
            path_id=event.path_id,
            path_attempt=0,
            reservations=self.path_width,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("PREFILL",))

    def _on_lock(self, state: ModelState, event: ModelEvent) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "PREFILL")
        if invalid is not None:
            return invalid
        updated = replace(
            state,
            phase="LOCKED",
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("LOCK",))

    def _on_prefill_complete(
        self, state: ModelState, event: ModelEvent
    ) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "LOCKED")
        if invalid is not None:
            return invalid
        updated = replace(
            state,
            phase="DECODING",
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("DECODE_READY",))

    def _on_token(self, state: ModelState, event: ModelEvent) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "DECODING")
        if invalid is not None:
            return invalid
        if event.peer == "non_final":
            return self._reject(state, "non_final_peer")
        if event.peer != "final":
            return self._reject(state, "off_path_peer")
        sequence = self._validate_sequence(state, event, kind="TOKEN")
        if sequence is not None:
            return sequence
        if len(event.payload) != 1 or not isinstance(event.payload[0], int):
            return self._reject(state, "invalid_payload")

        emitted = state.emitted_tokens + (event.payload[0],)
        completed = len(emitted) >= self.maximum_new_tokens
        updated = replace(
            state,
            phase="COMPLETED" if completed else state.phase,
            next_sequence=state.next_sequence + 1,
            emitted_tokens=emitted,
            reservations=0 if completed else state.reservations,
            release_count=state.release_count + int(completed),
            runtime_cancel_count=state.runtime_cancel_count + int(completed),
            terminal_count=state.terminal_count + int(completed),
            accepted_events=state.accepted_events + (event,),
        )
        milestones = ("TOKEN_DELIVERY", "COMPLETION") if completed else ("TOKEN_DELIVERY",)
        return self._accept(updated, "accepted", milestones)

    def _on_failure(self, state: ModelState, event: ModelEvent) -> Transition:
        if state.phase == "NEW":
            return self._reject(state, "unknown_request")
        if state.phase in TERMINAL_PHASES:
            return self._reject(state, "terminal_state")
        if state.phase not in {"PREFILL", "LOCKED", "DECODING"}:
            return self._reject(state, "invalid_phase")
        invalid = self._validate_path(state, event)
        if invalid is not None:
            return invalid
        if event.peer == "non_owner":
            return self._reject(state, "non_owner_peer")
        if event.peer != "path":
            return self._reject(state, "off_path_peer")
        sequence = self._validate_failure_sequence(state, event)
        if sequence is not None:
            return sequence

        preserved = self.prompt_tokens + state.emitted_tokens
        can_recover = state.recovery_count < self.maximum_recovery_attempts
        requested_outcome = event.payload[0] if event.payload else "success"
        if not can_recover:
            updated = replace(
                state,
                phase="FAILED",
                reservations=0,
                release_count=state.release_count + int(state.reservations > 0),
                runtime_cancel_count=(
                    state.runtime_cancel_count + int(bool(state.path_id))
                ),
                terminal_count=state.terminal_count + 1,
                preserved_context=preserved,
                accepted_events=state.accepted_events + (event,),
            )
            return self._accept(updated, "recovery_exhausted", ("FAILURE",))

        next_attempt = state.path_attempt + 1
        if requested_outcome == "failure":
            updated = replace(
                state,
                phase="FAILED",
                path_id=f"path-{next_attempt}",
                path_attempt=next_attempt,
                reservations=0,
                release_count=state.release_count + 2,
                runtime_cancel_count=state.runtime_cancel_count + 2,
                terminal_count=state.terminal_count + 1,
                recovery_count=state.recovery_count + 1,
                preserved_context=preserved,
                recovery_uses_transferred_kv=False,
                accepted_events=state.accepted_events + (event,),
            )
            return self._accept(
                updated,
                "recovery_failed",
                ("FAILURE", "RECOVERY_PREFILL", "RECOVERY_FAILED"),
            )

        updated = replace(
            state,
            phase="DECODING",
            path_id=f"path-{next_attempt}",
            path_attempt=next_attempt,
            reservations=self.path_width,
            release_count=state.release_count + int(state.reservations > 0),
            runtime_cancel_count=state.runtime_cancel_count + int(bool(state.path_id)),
            recovery_count=state.recovery_count + 1,
            preserved_context=preserved,
            recovery_uses_transferred_kv=False,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(
            updated,
            "recovered",
            ("FAILURE", "RECOVERY_PREFILL", "LOCK", "DECODE_READY"),
        )

    def _on_cancel(self, state: ModelState, event: ModelEvent) -> Transition:
        if state.phase == "NEW":
            return self._reject(state, "unknown_request")
        if state.phase == "CANCELLED" and event in state.accepted_events:
            return self._reject(state, "idempotent_duplicate")
        if state.phase in TERMINAL_PHASES:
            return self._reject(state, "terminal_state")
        updated = replace(
            state,
            phase="CANCELLED",
            reservations=0,
            release_count=state.release_count + int(state.reservations > 0),
            runtime_cancel_count=state.runtime_cancel_count + int(bool(state.path_id)),
            terminal_count=state.terminal_count + 1,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("CANCELLATION",))

    def _on_prefill_hop(
        self, state: ModelState, event: ModelEvent
    ) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "PREFILL")
        if invalid is not None:
            return invalid
        if (
            event.hop_index is None
            or event.hop_index < 0
            or event.hop_index >= self.path_width
        ):
            return self._reject(state, "invalid_hop_index")
        expected_peer = "entry" if event.hop_index == 0 else "previous"
        if event.peer != expected_peer:
            return self._reject(state, "off_path_peer")
        replay = self._replay_disposition(state, event, kind="PREFILL_HOP")
        if replay is not None:
            return replay
        if event.sequence != -1:
            return self._reject(state, "invalid_sequence")
        updated = replace(
            state,
            hop_executions=state.hop_executions + 1,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("PREFILL_HOP_EXECUTION",))

    def _on_enqueue_hop(
        self, state: ModelState, event: ModelEvent
    ) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "DECODING")
        if invalid is not None:
            return invalid
        if (
            event.hop_index is None
            or event.hop_index < 0
            or event.hop_index >= self.path_width
        ):
            return self._reject(state, "invalid_hop_index")
        expected_peer = "entry" if event.hop_index == 0 else "previous"
        if event.peer != expected_peer:
            return self._reject(state, "off_path_peer")
        replay = self._replay_disposition(state, event, kind="ENQUEUE_HOP")
        if replay is not None:
            return replay
        if event.sequence is None:
            return self._reject(state, "invalid_sequence")
        if event.sequence > state.next_sequence:
            return self._reject(state, "future_sequence")
        if event.sequence < state.next_sequence:
            return self._reject(state, "replayed_sequence")
        updated = replace(
            state,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("HOP_QUEUED",))

    def _on_hop(self, state: ModelState, event: ModelEvent) -> Transition:
        invalid = self._validate_phase_and_path(state, event, "DECODING")
        if invalid is not None:
            return invalid
        if (
            event.hop_index is None
            or event.hop_index < 0
            or event.hop_index >= self.path_width
        ):
            return self._reject(state, "invalid_hop_index")
        expected_peer = "entry" if event.hop_index == 0 else "previous"
        if event.peer != expected_peer:
            return self._reject(state, "off_path_peer")
        replay = self._replay_disposition(state, event, kind="HOP")
        if replay is not None:
            return replay
        if event.sequence is None:
            return self._reject(state, "invalid_sequence")
        if event.sequence > state.next_sequence:
            return self._reject(state, "future_sequence")
        if event.sequence < state.next_sequence:
            return self._reject(state, "replayed_sequence")
        updated = replace(
            state,
            hop_executions=state.hop_executions + 1,
            accepted_events=state.accepted_events + (event,),
        )
        return self._accept(updated, "accepted", ("HOP_EXECUTION",))

    def _validate_phase_and_path(
        self,
        state: ModelState,
        event: ModelEvent,
        required_phase: str,
    ) -> Transition | None:
        if state.phase == "NEW":
            return self._reject(state, "unknown_request")
        if state.phase in TERMINAL_PHASES:
            return self._reject(state, "terminal_state")
        if state.phase != required_phase:
            return self._reject(state, "invalid_phase")
        return self._validate_path(state, event)

    def _validate_path(
        self, state: ModelState, event: ModelEvent
    ) -> Transition | None:
        if event.path_attempt is None:
            return self._reject(state, "invalid_path_attempt")
        if event.path_attempt < state.path_attempt:
            return self._reject(state, "stale_path_attempt")
        if event.path_attempt > state.path_attempt:
            return self._reject(state, "future_path_attempt")
        if event.path_id != state.path_id:
            return self._reject(state, "path_mismatch")
        return None

    def _validate_sequence(
        self,
        state: ModelState,
        event: ModelEvent,
        *,
        kind: str,
    ) -> Transition | None:
        if event.sequence is None or event.sequence < 0:
            return self._reject(state, "replayed_sequence")
        if event.sequence > state.next_sequence:
            return self._reject(state, "future_sequence")
        if event.sequence < state.next_sequence:
            replay = self._replay_disposition(state, event, kind=kind)
            if replay is not None:
                return replay
            return self._reject(state, "replayed_sequence")
        return None

    def _validate_failure_sequence(
        self, state: ModelState, event: ModelEvent
    ) -> Transition | None:
        if event.sequence is None:
            return self._reject(state, "invalid_sequence")
        if event.sequence < state.next_sequence:
            return self._reject(state, "replayed_sequence")
        if event.sequence > state.next_sequence:
            return self._reject(state, "future_sequence")
        return None

    def _replay_disposition(
        self,
        state: ModelState,
        event: ModelEvent,
        *,
        kind: str,
    ) -> Transition | None:
        same_identity = tuple(
            accepted
            for accepted in state.accepted_events
            if accepted.kind == kind
            and accepted.path_id == event.path_id
            and accepted.path_attempt == event.path_attempt
            and accepted.sequence == event.sequence
            and accepted.hop_index == event.hop_index
        )
        if not same_identity:
            return None
        if event in same_identity:
            return self._reject(state, "idempotent_duplicate")
        return self._reject(state, "conflicting_duplicate")

    @staticmethod
    def _reject(state: ModelState, code: str) -> Transition:
        return Transition(
            state=state,
            accepted=False,
            mutated=False,
            code=code,
        )

    @staticmethod
    def _accept(
        state: ModelState,
        code: str,
        milestones: tuple[str, ...],
    ) -> Transition:
        return Transition(
            state=state,
            accepted=True,
            mutated=True,
            code=code,
            milestones=milestones,
        )
