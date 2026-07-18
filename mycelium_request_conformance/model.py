"""Independent deterministic model for request-gateway lifecycle semantics.

This module intentionally imports no production gateway code.  Its transitions are
pure and immutable so traces can be replayed and minimized byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Phase(str, Enum):
    NEW = "new"
    STREAMING = "streaming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class Authority:
    deployment: str
    epoch: int
    path: str
    evidence: str
    qualification: str
    ready: bool


@dataclass(frozen=True)
class Event:
    sequence: int
    kind: str
    token_index: int | None = None
    text: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class SideEffectCounters:
    runtime_starts: int = 0
    backend_cancels: int = 0
    capacity_acquires: int = 0
    capacity_releases: int = 0
    kv_acquires: int = 0
    kv_cleanups: int = 0
    token_events: int = 0
    terminal_events: int = 0
    failures: int = 0
    total_events: int = 0
    maximum_buffered: int = 0


@dataclass(frozen=True)
class ModelState:
    phase: Phase
    current: Authority
    captured: Authority | None
    payload: str | None
    events: tuple[Event, ...]
    token_payloads: tuple[str, ...]
    expected_token_index: int
    latest_sequence: int
    acknowledged_through: int
    discarded_through: int
    attached: bool
    terminal_count: int
    outcome: str | None
    counters: SideEffectCounters


@dataclass(frozen=True)
class StepResult:
    state: ModelState
    code: str


@dataclass(frozen=True)
class Action:
    kind: str
    authority: Authority | None = None
    payload: str | None = None
    token_index: int | None = None
    text: str | None = None
    field: str | None = None
    value: object | None = None
    cursor: int | None = None

    @classmethod
    def admit(cls, authority: Authority, *, payload: str) -> Action:
        return cls("admit", authority=authority, payload=payload)

    @classmethod
    def token(cls, token_index: int, text: str) -> Action:
        return cls("token", token_index=token_index, text=text)

    @classmethod
    def complete(cls) -> Action:
        return cls("complete")

    @classmethod
    def cancel(cls) -> Action:
        return cls("cancel")

    @classmethod
    def disconnect(cls) -> Action:
        return cls("disconnect")

    @classmethod
    def reconnect(cls, cursor: int) -> Action:
        return cls("reconnect", cursor=cursor)

    @classmethod
    def ack(cls, cursor: int) -> Action:
        return cls("ack", cursor=cursor)

    @classmethod
    def change_authority(cls, field: str, value: object) -> Action:
        return cls("change_authority", field=field, value=value)


class GatewayModel:
    """Pure single-request lifecycle model with bounded retained replay."""

    def __init__(self, *, current: Authority, buffer_capacity: int = 4) -> None:
        if buffer_capacity < 2:
            raise ValueError("invalid_buffer_capacity")
        self.buffer_capacity = buffer_capacity
        self.initial_state = ModelState(
            phase=Phase.NEW,
            current=current,
            captured=None,
            payload=None,
            events=(),
            token_payloads=(),
            expected_token_index=0,
            latest_sequence=-1,
            acknowledged_through=-1,
            discarded_through=-1,
            attached=False,
            terminal_count=0,
            outcome=None,
            counters=SideEffectCounters(),
        )

    def apply(self, action: Action, *, state: ModelState | None = None) -> StepResult:
        current_state = self.initial_state if state is None else state
        handler = getattr(self, f"_apply_{action.kind}", None)
        if handler is None:
            return StepResult(current_state, "unknown_action")
        return handler(current_state, action)

    @staticmethod
    def _authority_difference(current: Authority, captured: Authority) -> str | None:
        if not current.ready or not captured.ready:
            return "route_not_ready"
        for field, code in (
            ("deployment", "deployment_changed"),
            ("epoch", "epoch_changed"),
            ("path", "path_changed"),
            ("evidence", "evidence_changed"),
            ("qualification", "qualification_changed"),
        ):
            if getattr(current, field) != getattr(captured, field):
                return code
        return None

    def _apply_admit(self, state: ModelState, action: Action) -> StepResult:
        authority = action.authority
        payload = action.payload
        if authority is None or not isinstance(payload, str):
            return StepResult(state, "invalid_submission")
        if state.phase is not Phase.NEW:
            if authority == state.captured and payload == state.payload:
                return StepResult(state, "exact_request_replay")
            return StepResult(state, "conflicting_request_replay")
        difference = self._authority_difference(state.current, authority)
        if difference is not None:
            return StepResult(state, difference)

        accepted = Event(sequence=0, kind="accepted")
        counters = replace(
            state.counters,
            runtime_starts=1,
            capacity_acquires=1,
            kv_acquires=1,
            total_events=1,
            maximum_buffered=1,
        )
        return StepResult(
            replace(
                state,
                phase=Phase.STREAMING,
                captured=authority,
                payload=payload,
                events=(accepted,),
                latest_sequence=0,
                counters=counters,
            ),
            "admitted",
        )

    def _revalidation_code(self, state: ModelState) -> str | None:
        if state.captured is None:
            return "request_state_released"
        return self._authority_difference(state.current, state.captured)

    def _apply_token(self, state: ModelState, action: Action) -> StepResult:
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        difference = self._revalidation_code(state)
        if difference is not None:
            return self._finish(state, Phase.FAILED, difference, cancel_backend=True)
        token_index = action.token_index
        text = action.text
        if (
            not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or token_index < 0
            or not isinstance(text, str)
        ):
            return self._finish(
                state, Phase.FAILED, "invalid_backend_token", cancel_backend=True
            )
        if token_index < state.expected_token_index:
            if (
                token_index < len(state.token_payloads)
                and state.token_payloads[token_index] == text
            ):
                return StepResult(state, "exact_token_replay")
            return self._finish(
                state,
                Phase.FAILED,
                "conflicting_token_replay",
                cancel_backend=True,
            )
        if token_index != state.expected_token_index:
            return self._finish(
                state, Phase.FAILED, "token_order_violation", cancel_backend=True
            )
        if len(state.events) >= self.buffer_capacity - 1:
            return StepResult(state, "backpressured")

        event = Event(
            sequence=state.latest_sequence + 1,
            kind="token",
            token_index=token_index,
            text=text,
        )
        events = state.events + (event,)
        counters = replace(
            state.counters,
            token_events=state.counters.token_events + 1,
            total_events=state.counters.total_events + 1,
            maximum_buffered=max(state.counters.maximum_buffered, len(events)),
        )
        return StepResult(
            replace(
                state,
                events=events,
                token_payloads=state.token_payloads + (text,),
                expected_token_index=token_index + 1,
                latest_sequence=event.sequence,
                counters=counters,
            ),
            "token_accepted",
        )

    def _apply_complete(self, state: ModelState, action: Action) -> StepResult:
        del action
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        difference = self._revalidation_code(state)
        if difference is not None:
            return self._finish(state, Phase.FAILED, difference, cancel_backend=True)
        return self._finish(state, Phase.COMPLETED, "completed", cancel_backend=False)

    def _apply_cancel(self, state: ModelState, action: Action) -> StepResult:
        del action
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        return self._finish(state, Phase.CANCELLED, "cancelled", cancel_backend=True)

    @staticmethod
    def _apply_change_authority(state: ModelState, action: Action) -> StepResult:
        if action.field not in {
            "deployment",
            "epoch",
            "path",
            "evidence",
            "qualification",
            "ready",
        }:
            return StepResult(state, "invalid_authority_change")
        return StepResult(
            replace(state, current=replace(state.current, **{action.field: action.value})),
            "authority_changed",
        )

    @staticmethod
    def _apply_disconnect(state: ModelState, action: Action) -> StepResult:
        del action
        if not state.attached:
            return StepResult(state, "already_disconnected")
        return StepResult(replace(state, attached=False), "disconnected")

    @staticmethod
    def _apply_reconnect(state: ModelState, action: Action) -> StepResult:
        cursor = action.cursor
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < -1:
            return StepResult(state, "invalid_last_event_id")
        if state.attached:
            return StepResult(state, "stream_already_attached")
        if cursor < state.discarded_through:
            return StepResult(state, "resume_cursor_expired")
        if cursor > state.latest_sequence:
            return StepResult(state, "invalid_last_event_id")
        retained = tuple(event for event in state.events if event.sequence >= cursor)
        return StepResult(
            replace(
                state,
                events=retained,
                acknowledged_through=max(state.acknowledged_through, cursor),
                discarded_through=max(state.discarded_through, cursor - 1),
                attached=True,
            ),
            "reconnected",
        )

    @staticmethod
    def _apply_ack(state: ModelState, action: Action) -> StepResult:
        cursor = action.cursor
        if not state.attached:
            return StepResult(state, "stream_not_attached")
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor <= state.acknowledged_through
            or cursor > state.latest_sequence
        ):
            return StepResult(state, "invalid_stream_ack")
        retained = tuple(event for event in state.events if event.sequence >= cursor)
        return StepResult(
            replace(
                state,
                events=retained,
                acknowledged_through=cursor,
                discarded_through=max(state.discarded_through, cursor - 1),
            ),
            "acknowledged",
        )

    def _finish(
        self,
        state: ModelState,
        phase: Phase,
        code: str,
        *,
        cancel_backend: bool,
    ) -> StepResult:
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        terminal_kind = {
            Phase.COMPLETED: "completed",
            Phase.CANCELLED: "cancelled",
            Phase.FAILED: "failed",
        }[phase]
        retained = state.events
        while len(retained) >= self.buffer_capacity:
            if retained[0].sequence > state.acknowledged_through:
                raise RuntimeError("terminal_event_capacity_invariant")
            retained = retained[1:]
        event = Event(
            sequence=state.latest_sequence + 1,
            kind=terminal_kind,
            code=None if phase is not Phase.FAILED else code,
        )
        events = retained + (event,)
        counters = replace(
            state.counters,
            backend_cancels=state.counters.backend_cancels + int(cancel_backend),
            capacity_releases=state.counters.capacity_releases + 1,
            kv_cleanups=state.counters.kv_cleanups + 1,
            terminal_events=state.counters.terminal_events + 1,
            failures=state.counters.failures + int(phase is Phase.FAILED),
            total_events=state.counters.total_events + 1,
            maximum_buffered=max(state.counters.maximum_buffered, len(events)),
        )
        return StepResult(
            replace(
                state,
                phase=phase,
                events=events,
                latest_sequence=event.sequence,
                terminal_count=1,
                outcome=code,
                counters=counters,
            ),
            code,
        )
