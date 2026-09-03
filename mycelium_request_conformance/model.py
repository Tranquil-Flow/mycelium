"""Independent deterministic model for request-gateway lifecycle semantics.

This module intentionally imports no production gateway code.  Its transitions are
pure and immutable so traces can be replayed and minimized byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib


_MAX_TOKEN_TEXT_BYTES = 1 << 20
_MAX_PROMPT_UTF8_BYTES = 131_072
_MAX_NEW_TOKENS = 4_096


class Phase(str, Enum):
    NEW = "new"
    ADMITTED = "admitted"
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
    text_digest: str | None = None
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
    request_digest: bytes | None
    payload_digest: bytes | None
    max_new_tokens: int | None
    events: tuple[Event, ...]
    token_digests: tuple[bytes, ...]
    expected_token_index: int
    latest_sequence: int
    acknowledged_through: int
    discarded_through: int
    stream_cursor: int
    delivered_through: int
    attached: bool
    terminal_count: int
    outcome: str | None
    counters: SideEffectCounters
    publication_pending_kind: str | None = None


@dataclass(frozen=True)
class StepResult:
    state: ModelState
    code: str


@dataclass(frozen=True)
class Action:
    kind: str
    authority: Authority | None = None
    payload: str | None = None
    max_new_tokens: int | None = None
    token_index: int | None = None
    text: str | None = None
    field: str | None = None
    value: object | None = None
    cursor: int | None = None

    @classmethod
    def admit(
        cls,
        authority: Authority,
        *,
        payload: str,
        max_new_tokens: int = 4,
    ) -> Action:
        return cls(
            "admit",
            authority=authority,
            payload=payload,
            max_new_tokens=max_new_tokens,
        )

    @classmethod
    def token(cls, token_index: int, text: str) -> Action:
        return cls("token", token_index=token_index, text=text)

    @classmethod
    def start(cls) -> Action:
        return cls("start")

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
    def next_event(cls) -> Action:
        return cls("next_event")

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
            request_digest=None,
            payload_digest=None,
            max_new_tokens=None,
            events=(),
            token_digests=(),
            expected_token_index=0,
            latest_sequence=-1,
            acknowledged_through=-1,
            discarded_through=-1,
            stream_cursor=-1,
            delivered_through=-1,
            attached=False,
            terminal_count=0,
            outcome=None,
            counters=SideEffectCounters(),
        )

    def apply(self, action: Action, *, state: ModelState | None = None) -> StepResult:
        """Apply one action, modeling the terminal-publication window.

        The real service publishes a terminal after the backend commits its
        outcome; a cancel that lands in between is forwarded to the backend
        (which bumps its cancel counter) and loses to the already-committed
        outcome. ``publication_pending_kind`` marks that window in the serial
        model: any non-cancel action settles it first (publication completes
        before the next operation observes the service), while a cancel
        landing inside it bumps the backend cancel counter — except when the
        pending terminal is itself a cancellation, which the service absorbs
        via ``cancellation_started`` without a second backend forward.
        """

        current_state = self.initial_state if state is None else state
        pending = current_state.publication_pending_kind
        if pending is not None:
            if action.kind == "cancel" and pending == "completed":
                # The worker has not yet delivered the backend's committed
                # completion; a cancel forwarded in this window bumps the
                # backend's cancel counter and loses to the committed
                # outcome. Cancelled/failed terminals publish on the
                # service's own paths and absorb or precede the forward, so
                # no window bump applies to them.
                counters = replace(
                    current_state.counters,
                    backend_cancels=current_state.counters.backend_cancels + 1,
                )
                return StepResult(
                    replace(
                        current_state,
                        publication_pending_kind=None,
                        counters=counters,
                    ),
                    "already_terminal",
                )
            current_state = replace(
                current_state, publication_pending_kind=None
            )
        handler = getattr(self, f"_apply_{action.kind}", None)
        if handler is None:
            return StepResult(current_state, "unknown_action")
        return handler(current_state, action)

    @staticmethod
    def _authority_difference(current: Authority, captured: Authority) -> str | None:
        if not current.ready or not captured.ready:
            return "readiness_revoked"
        for field, code in (
            ("deployment", "qualification_mismatch"),
            ("epoch", "deployment_epoch_changed"),
            ("path", "path_changed"),
            ("evidence", "qualification_mismatch"),
            ("qualification", "stale_qualification"),
        ):
            if getattr(current, field) != getattr(captured, field):
                return code
        return None

    @staticmethod
    def _request_identity_digest(
        authority: Authority,
        payload_digest: bytes,
        max_new_tokens: int,
    ) -> bytes:
        digest = hashlib.sha256()
        values = (
            authority.deployment,
            str(authority.epoch),
            authority.path,
            authority.evidence,
            authority.qualification,
            "true" if authority.ready else "false",
            str(max_new_tokens),
        )
        for value in values:
            encoded = value.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        digest.update(payload_digest)
        return digest.digest()

    def _apply_admit(self, state: ModelState, action: Action) -> StepResult:
        authority = action.authority
        payload = action.payload
        max_new_tokens = action.max_new_tokens
        if (
            authority is None
            or not isinstance(payload, str)
            or not isinstance(max_new_tokens, int)
            or isinstance(max_new_tokens, bool)
            or not 1 <= max_new_tokens <= _MAX_NEW_TOKENS
        ):
            return StepResult(state, "invalid_submission")
        try:
            payload_bytes = payload.encode("utf-8")
        except UnicodeEncodeError:
            return StepResult(state, "invalid_submission")
        if len(payload_bytes) > _MAX_PROMPT_UTF8_BYTES:
            return StepResult(state, "invalid_submission")
        payload_digest = hashlib.sha256(payload_bytes).digest()
        request_digest = self._request_identity_digest(
            authority,
            payload_digest,
            max_new_tokens,
        )
        if state.phase is not Phase.NEW:
            if request_digest == state.request_digest:
                return StepResult(state, "exact_request_replay")
            return StepResult(state, "conflicting_request_replay")
        difference = self._authority_difference(state.current, authority)
        if difference is not None:
            return StepResult(state, difference)

        accepted = Event(sequence=0, kind="accepted")
        counters = replace(
            state.counters,
            total_events=1,
            maximum_buffered=1,
        )
        return StepResult(
            replace(
                state,
                phase=Phase.ADMITTED,
                captured=authority,
                request_digest=request_digest,
                payload_digest=payload_digest,
                max_new_tokens=max_new_tokens,
                events=(accepted,),
                latest_sequence=0,
                counters=counters,
            ),
            "admitted",
        )

    def _apply_start(self, state: ModelState, action: Action) -> StepResult:
        del action
        if state.phase is not Phase.ADMITTED:
            return StepResult(state, "invalid_start_transition")
        difference = self._revalidation_code(state)
        if difference is not None:
            return self._finish(
                state,
                Phase.FAILED,
                difference,
                cancel_backend=True,
                resources_started=False,
            )
        counters = replace(
            state.counters,
            runtime_starts=state.counters.runtime_starts + 1,
            capacity_acquires=state.counters.capacity_acquires + 1,
            kv_acquires=state.counters.kv_acquires + 1,
        )
        return StepResult(
            replace(state, phase=Phase.STREAMING, counters=counters),
            "started",
        )

    def _revalidation_code(self, state: ModelState) -> str | None:
        if state.captured is None:
            return "request_state_released"
        return self._authority_difference(state.current, state.captured)

    @staticmethod
    def _trim_acknowledged_replay(
        state: ModelState,
        *,
        target_size: int,
    ) -> ModelState:
        events = state.events
        discarded_through = state.discarded_through
        while (
            len(events) > target_size
            and events
            and events[0].sequence <= state.acknowledged_through
        ):
            discarded_through = max(discarded_through, events[0].sequence)
            events = events[1:]
        if events == state.events:
            return state
        return replace(
            state,
            events=events,
            discarded_through=discarded_through,
        )

    def _apply_token(self, state: ModelState, action: Action) -> StepResult:
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        token_index = action.token_index
        text = action.text
        if (
            not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or token_index < 0
            or not isinstance(text, str)
            or len(text) > _MAX_TOKEN_TEXT_BYTES
        ):
            return self._finish(
                state, Phase.FAILED, "invalid_backend_token", cancel_backend=True
            )
        try:
            token_bytes = text.encode("utf-8")
        except UnicodeEncodeError:
            return self._finish(
                state, Phase.FAILED, "invalid_backend_token", cancel_backend=True
            )
        if len(token_bytes) > _MAX_TOKEN_TEXT_BYTES:
            return self._finish(
                state, Phase.FAILED, "invalid_backend_token", cancel_backend=True
            )
        token_digest = hashlib.sha256(token_bytes).digest()
        difference = self._revalidation_code(state)
        if difference is not None:
            return self._finish(state, Phase.FAILED, difference, cancel_backend=True)
        if token_index < state.expected_token_index:
            if (
                token_index < len(state.token_digests)
                and state.token_digests[token_index] == token_digest
            ):
                return StepResult(state, "exact_token_replay")
            return self._finish(
                state,
                Phase.FAILED,
                "token_order_violation",
                cancel_backend=True,
            )
        if (
            token_index != state.expected_token_index
            or state.max_new_tokens is None
            or token_index >= state.max_new_tokens
        ):
            return self._finish(
                state, Phase.FAILED, "token_order_violation", cancel_backend=True
            )
        state = self._trim_acknowledged_replay(
            state,
            target_size=self.buffer_capacity - 2,
        )
        if len(state.events) >= self.buffer_capacity - 1:
            return StepResult(state, "backpressured")

        event = Event(
            sequence=state.latest_sequence + 1,
            kind="token",
            token_index=token_index,
            text_digest=token_digest.hex(),
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
                token_digests=state.token_digests + (token_digest,),
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
        if state.phase is Phase.ADMITTED:
            return self._finish(
                state,
                Phase.CANCELLED,
                "cancelled",
                cancel_backend=True,
                resources_started=False,
            )
        if state.phase is not Phase.STREAMING:
            return StepResult(state, "already_terminal")
        return self._finish(state, Phase.CANCELLED, "cancelled", cancel_backend=True)

    @staticmethod
    def _apply_publish(state: ModelState, action: Action) -> StepResult:
        """Settle a pending terminal publication (idempotent no-op).

        The sequential production harness waits for terminal publication
        between actions; this explicit step models that wait. In the
        concurrent race alphabet publication is an independent event, so
        traces that never publish keep the window open.
        """

        del action
        return StepResult(state, "publication_settled")

    @staticmethod
    def _apply_change_authority(state: ModelState, action: Action) -> StepResult:
        field = action.field
        value = action.value
        if field not in {
            "deployment",
            "epoch",
            "path",
            "evidence",
            "qualification",
            "ready",
        }:
            return StepResult(state, "invalid_authority_change")
        if field == "ready":
            valid = isinstance(value, bool)
        elif field == "epoch":
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, str)
        if not valid:
            return StepResult(state, "invalid_authority_change")
        return StepResult(
            replace(state, current=replace(state.current, **{field: value})),
            "authority_changed",
        )

    @staticmethod
    def _apply_disconnect(state: ModelState, action: Action) -> StepResult:
        del action
        if state.phase is Phase.NEW:
            return StepResult(state, "unknown_request")
        if not state.attached:
            return StepResult(state, "already_disconnected")
        return StepResult(replace(state, attached=False), "disconnected")

    @staticmethod
    def _apply_reconnect(state: ModelState, action: Action) -> StepResult:
        cursor = action.cursor
        if state.phase is Phase.NEW:
            return StepResult(state, "unknown_request")
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
                stream_cursor=cursor,
                delivered_through=cursor,
                attached=True,
            ),
            "reconnected",
        )

    @staticmethod
    def _apply_next_event(state: ModelState, action: Action) -> StepResult:
        del action
        if state.phase is Phase.NEW:
            return StepResult(state, "unknown_request")
        if not state.attached:
            return StepResult(state, "stream_not_attached")
        event = next(
            (event for event in state.events if event.sequence > state.stream_cursor),
            None,
        )
        if event is None:
            return StepResult(state, "no_event_available")
        return StepResult(
            replace(state, delivered_through=event.sequence),
            f"delivered_{event.kind}",
        )

    @staticmethod
    def _apply_ack(state: ModelState, action: Action) -> StepResult:
        cursor = action.cursor
        if state.phase is Phase.NEW:
            return StepResult(state, "unknown_request")
        if not state.attached:
            return StepResult(state, "stream_not_attached")
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor <= state.stream_cursor
            or cursor > state.delivered_through
        ):
            return StepResult(state, "invalid_stream_ack")
        return StepResult(
            replace(
                state,
                acknowledged_through=cursor,
                stream_cursor=cursor,
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
        resources_started: bool = True,
    ) -> StepResult:
        if state.phase not in {Phase.ADMITTED, Phase.STREAMING}:
            return StepResult(state, "already_terminal")
        terminal_kind = {
            Phase.COMPLETED: "completed",
            Phase.CANCELLED: "cancelled",
            Phase.FAILED: "failed",
        }[phase]
        trimmed = self._trim_acknowledged_replay(
            state,
            target_size=self.buffer_capacity - 1,
        )
        retained = trimmed.events
        if len(retained) >= self.buffer_capacity:
            raise RuntimeError("terminal_event_capacity_invariant")
        event = Event(
            sequence=state.latest_sequence + 1,
            kind=terminal_kind,
            code=None if phase is not Phase.FAILED else code,
        )
        events = retained + (event,)
        counters = replace(
            state.counters,
            backend_cancels=(
                state.counters.backend_cancels
                + int(cancel_backend and resources_started)
            ),
            capacity_releases=(
                state.counters.capacity_releases + int(resources_started)
            ),
            kv_cleanups=state.counters.kv_cleanups + int(resources_started),
            terminal_events=state.counters.terminal_events + 1,
            failures=state.counters.failures + int(phase is Phase.FAILED),
            total_events=state.counters.total_events + 1,
            maximum_buffered=max(state.counters.maximum_buffered, len(events)),
        )
        return StepResult(
            replace(
                trimmed,
                phase=phase,
                captured=None,
                payload_digest=None,
                max_new_tokens=None,
                token_digests=(),
                events=events,
                latest_sequence=event.sequence,
                terminal_count=1,
                outcome=code,
                counters=counters,
                publication_pending_kind=terminal_kind,
            ),
            code,
        )
