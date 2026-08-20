"""Deterministic request-scoped command cancellation and terminal ownership.

This module owns only controller metadata.  It deliberately does not execute work,
terminate processes, release placement resources, or recover requests.  Runtimes call
``checkpoint`` between bounded work units and report terminal and cleanup outcomes
through generation-fenced compare-and-swap operations.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


MAX_INTERRUPTION_AND_CLEANUP_MS = 2_000
DEFAULT_COOPERATIVE_STEP_MS = 100
DEFAULT_MAXIMUM_REQUESTS = 4_096
DEFAULT_MAXIMUM_COMMANDS_PER_REQUEST = 64
MAX_CLEANUP_RESULT_BYTES = 4_096

_MAX_TEXT_BYTES = 256
_MAX_MONOTONIC_MS = (1 << 63) - 1
_MAX_RESOURCE_COUNT = 1_000_000
_SHA256_PREFIX = "sha256:"


class CommandKind(str, Enum):
    PREFILL = "prefill"
    DECODE = "decode"
    CLEANUP = "cleanup"
    SHUTDOWN = "shutdown"
    PROBE = "probe"


class TerminalStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PEER_UNAVAILABLE = "peer_unavailable"
    ERROR = "error"


class CleanupStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class CheckpointAction(str, Enum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    TERMINAL = "terminal"


_ERROR_CODES = frozenset(
    {
        "backend_ineligible",
        "command_rejected",
        "invalid_response",
        "runtime_error",
        "transport_error",
    }
)
_CLEANUP_ERROR_CODES = frozenset(
    {
        "cleanup_incomplete",
        "cleanup_owner_unavailable",
        "cleanup_result_invalid",
    }
)


def _bounded_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_MONOTONIC_MS:
        raise ValueError(f"{name} must be an integer in [{minimum}, {_MAX_MONOTONIC_MS}]")
    return value


def _digest(value: object, name: str) -> str:
    value = _bounded_text(value, name)
    if (
        len(value) != 71
        or not value.startswith(_SHA256_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 reference")
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _SHA256_PREFIX + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandIdentity:
    """Identity revalidated at every cooperative and mutation boundary."""

    deployment_id: str
    deployment_epoch: int
    qualification_digest: str
    request_id: str
    request_attempt: int
    path_id: str
    path_attempt: int
    path_digest: str
    topology_generation: int
    command_id: str
    publisher_generation: int
    absolute_deadline_ms: int
    cancellation_generation: int = 0

    def __post_init__(self) -> None:
        _bounded_text(self.deployment_id, "deployment_id")
        _integer(self.deployment_epoch, "deployment_epoch", minimum=1)
        _digest(self.qualification_digest, "qualification_digest")
        _bounded_text(self.request_id, "request_id")
        _integer(self.request_attempt, "request_attempt", minimum=1)
        _bounded_text(self.path_id, "path_id")
        _integer(self.path_attempt, "path_attempt")
        _digest(self.path_digest, "path_digest")
        _integer(self.topology_generation, "topology_generation", minimum=1)
        _bounded_text(self.command_id, "command_id")
        _integer(self.publisher_generation, "publisher_generation", minimum=1)
        _integer(self.absolute_deadline_ms, "absolute_deadline_ms", minimum=1)
        _integer(self.cancellation_generation, "cancellation_generation")


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """Closed command registration shape; no prompt, token, tensor, or KV data."""

    identity: CommandIdentity
    stage_id: str
    placement_id: str
    assignment_id: str
    kind: CommandKind
    issued_at_ms: int
    idempotency_digest: str
    cleanup_owner_id: str
    maximum_request_bytes: int
    maximum_response_bytes: int
    expected_terminal_revision: int = 0

    def __post_init__(self) -> None:
        _bounded_text(self.stage_id, "stage_id")
        _bounded_text(self.placement_id, "placement_id")
        _bounded_text(self.assignment_id, "assignment_id")
        if not isinstance(self.kind, CommandKind):
            raise ValueError("kind must be a CommandKind")
        _integer(self.issued_at_ms, "issued_at_ms")
        if self.issued_at_ms >= self.identity.absolute_deadline_ms:
            raise ValueError("command deadline must be after issue time")
        _digest(self.idempotency_digest, "idempotency_digest")
        _bounded_text(self.cleanup_owner_id, "cleanup_owner_id")
        _integer(self.maximum_request_bytes, "maximum_request_bytes", minimum=1)
        _integer(self.maximum_response_bytes, "maximum_response_bytes", minimum=1)
        if self.expected_terminal_revision != 0:
            raise ValueError("new commands must expect terminal revision zero")


@dataclass(frozen=True, slots=True)
class TerminalResult:
    identity: CommandIdentity
    status: TerminalStatus
    observed_at_ms: int
    result_digest: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TerminalStatus):
            raise ValueError("status must be a TerminalStatus")
        _integer(self.observed_at_ms, "observed_at_ms")
        _digest(self.result_digest, "result_digest")
        if self.status is TerminalStatus.ERROR:
            if self.error_code not in _ERROR_CODES:
                raise ValueError("terminal error code is not allowlisted")
        elif self.error_code is not None:
            raise ValueError("error_code is valid only for error terminal results")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    status: CleanupStatus
    released_resource_count: int
    result_digest: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CleanupStatus):
            raise ValueError("status must be a CleanupStatus")
        count = _integer(
            self.released_resource_count,
            "released_resource_count",
        )
        if count > _MAX_RESOURCE_COUNT:
            raise ValueError("released_resource_count exceeds the bounded envelope")
        _digest(self.result_digest, "cleanup result_digest")
        if self.status is CleanupStatus.FAILED:
            if self.error_code not in _CLEANUP_ERROR_CODES:
                raise ValueError("cleanup error code is not allowlisted")
        elif self.error_code is not None:
            raise ValueError("cleanup error_code is valid only for failed results")


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    identity: CommandIdentity
    kind: CommandKind
    terminal_revision: int
    terminal: TerminalResult | None
    cancellation_requested_at_ms: int | None
    cleanup_deadline_ms: int | None
    cleanup_result: CleanupResult | None
    cleanup_revision: int
    cleanup_completed_at_ms: int | None
    cleanup_within_interruption_budget: bool | None


@dataclass(frozen=True, slots=True)
class MutationResult:
    accepted: bool
    reason: str
    snapshot: CommandSnapshot | None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    accepted: bool
    reason: str
    action: CheckpointAction
    maximum_next_step_ms: int
    snapshot: CommandSnapshot | None


@dataclass(slots=True)
class _CommandState:
    envelope: CommandEnvelope
    identity: CommandIdentity
    terminal_revision: int = 0
    terminal: TerminalResult | None = None
    terminal_payload_digest: str | None = None
    cancellation_requested_at_ms: int | None = None
    cancellation_request: tuple[int, int, str, int, bool] | None = None
    cleanup_deadline_ms: int | None = None
    completion_cleanup_authorized: bool = False
    cleanup_result: CleanupResult | None = None
    cleanup_revision: int = 0
    cleanup_completed_at_ms: int | None = None


@dataclass(slots=True)
class _RequestState:
    attempt: int
    path_digest: str
    commands: dict[str, _CommandState]
    lock: threading.RLock
    retired: bool = False


class CommandController:
    """Bounded metadata owner for cooperative command execution.

    Every method is a short in-memory mutation.  The controller never invokes runtime
    callbacks and therefore never holds its locks across physical or blocking work.
    """

    def __init__(
        self,
        *,
        maximum_requests: int = DEFAULT_MAXIMUM_REQUESTS,
        maximum_commands_per_request: int = DEFAULT_MAXIMUM_COMMANDS_PER_REQUEST,
        cooperative_step_ms: int = DEFAULT_COOPERATIVE_STEP_MS,
        interruption_and_cleanup_ms: int = MAX_INTERRUPTION_AND_CLEANUP_MS,
    ) -> None:
        self._maximum_requests = _integer(
            maximum_requests,
            "maximum_requests",
            minimum=1,
        )
        if self._maximum_requests > DEFAULT_MAXIMUM_REQUESTS:
            raise ValueError("maximum_requests exceeds the frozen subject bound")
        self._maximum_commands_per_request = _integer(
            maximum_commands_per_request,
            "maximum_commands_per_request",
            minimum=1,
        )
        if self._maximum_commands_per_request > DEFAULT_MAXIMUM_COMMANDS_PER_REQUEST:
            raise ValueError("maximum_commands_per_request exceeds the command bound")
        self._cooperative_step_ms = _integer(
            cooperative_step_ms,
            "cooperative_step_ms",
            minimum=1,
        )
        self._interruption_and_cleanup_ms = _integer(
            interruption_and_cleanup_ms,
            "interruption_and_cleanup_ms",
            minimum=1,
        )
        if self._cooperative_step_ms > self._interruption_and_cleanup_ms:
            raise ValueError("cooperative step cannot exceed the interruption budget")
        if self._interruption_and_cleanup_ms > MAX_INTERRUPTION_AND_CLEANUP_MS:
            raise ValueError("interruption and cleanup cannot exceed 2000 ms")
        self._metadata_lock = threading.RLock()
        self._requests: dict[str, _RequestState] = {}

    def register(self, envelope: CommandEnvelope) -> MutationResult:
        """Register one command or accept an exact idempotent duplicate."""

        identity = envelope.identity
        with self._metadata_lock:
            request = self._requests.get(identity.request_id)
            if request is None:
                if len(self._requests) >= self._maximum_requests:
                    return MutationResult(False, "request_limit", None)
                request = _RequestState(
                    attempt=identity.request_attempt,
                    path_digest=identity.path_digest,
                    commands={},
                    lock=threading.RLock(),
                )
                self._requests[identity.request_id] = request

        with request.lock:
            if request.retired:
                return MutationResult(False, "request_unknown", None)
            if identity.request_attempt < request.attempt:
                return MutationResult(False, "stale_attempt", None)
            if identity.request_attempt > request.attempt:
                if not self._attempt_is_closed(request):
                    return MutationResult(False, "previous_attempt_live", None)
                request.attempt = identity.request_attempt
                request.path_digest = identity.path_digest
                request.commands = {}
            elif identity.path_digest != request.path_digest:
                return MutationResult(False, "path_conflict", None)

            state = request.commands.get(identity.command_id)
            if state is not None:
                if state.envelope == envelope:
                    return MutationResult(
                        True,
                        "duplicate",
                        self._snapshot(state),
                        duplicate=True,
                    )
                return MutationResult(
                    False,
                    "conflicting_duplicate",
                    self._snapshot(state),
                )
            if len(request.commands) >= self._maximum_commands_per_request:
                return MutationResult(False, "command_limit", None)
            state = _CommandState(envelope=envelope, identity=identity)
            request.commands[identity.command_id] = state
            return MutationResult(True, "registered", self._snapshot(state))

    def cancel(
        self,
        identity: CommandIdentity,
        *,
        new_cancellation_generation: int,
        observed_at_ms: int,
        idempotency_digest: str,
        cleanup_deadline_ms: int | None = None,
        completion_cleanup: bool = False,
    ) -> MutationResult:
        """Advance the cooperative cleanup generation exactly once.

        ``completion_cleanup`` is owner authority issued only after the runtime
        has determined a completed result. It uses the same interrupt/cleanup
        wire operation without rewriting that already-determined terminal as a
        user cancellation.
        """

        new_generation = _integer(
            new_cancellation_generation,
            "new_cancellation_generation",
            minimum=1,
        )
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        idempotency_digest = _digest(idempotency_digest, "idempotency_digest")
        if type(completion_cleanup) is not bool:
            raise ValueError("completion_cleanup must be a boolean")
        if cleanup_deadline_ms is None:
            requested_cleanup_deadline_ms = min(
                identity.absolute_deadline_ms,
                observed_at_ms + self._interruption_and_cleanup_ms,
            )
        else:
            requested_cleanup_deadline_ms = _integer(
                cleanup_deadline_ms,
                "cleanup_deadline_ms",
                minimum=1,
            )
            if (
                requested_cleanup_deadline_ms < observed_at_ms
                or requested_cleanup_deadline_ms
                > observed_at_ms + self._interruption_and_cleanup_ms
                or requested_cleanup_deadline_ms > identity.absolute_deadline_ms
            ):
                raise ValueError("cleanup deadline exceeds the original cancellation budget")
        request, state, rejection = self._locate(identity, allow_previous_cancel=True)
        if rejection is not None:
            return rejection
        assert request is not None and state is not None
        with request.lock:
            if request.retired:
                return MutationResult(False, "request_unknown", None)
            previous = state.cancellation_request
            candidate = (
                identity.cancellation_generation,
                new_generation,
                idempotency_digest,
                requested_cleanup_deadline_ms,
                completion_cleanup,
            )
            if previous == candidate:
                return MutationResult(
                    True,
                    "duplicate",
                    self._snapshot(state),
                    duplicate=True,
                )
            if identity != state.identity:
                return MutationResult(False, "stale_generation", self._snapshot(state))
            if observed_at_ms < state.envelope.issued_at_ms:
                return MutationResult(False, "observation_before_issue", self._snapshot(state))
            self._expire_if_due(state, observed_at_ms)
            if state.terminal is not None:
                return MutationResult(False, "already_terminal", self._snapshot(state))
            if new_generation != identity.cancellation_generation + 1:
                return MutationResult(False, "cancellation_generation_conflict", self._snapshot(state))

            state.identity = replace(
                state.identity,
                cancellation_generation=new_generation,
            )
            state.cancellation_request = candidate
            state.cancellation_requested_at_ms = observed_at_ms
            state.cleanup_deadline_ms = requested_cleanup_deadline_ms
            state.completion_cleanup_authorized = completion_cleanup
            return MutationResult(True, "cancel_requested", self._snapshot(state))

    def advance_publisher_generation(
        self,
        identity: CommandIdentity,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> MutationResult:
        """CAS the gateway-owned publisher generation on one live command.

        Reconnect/reset authority belongs to the request gateway.  The command
        controller only accepts the exact current command identity and a
        strictly monotonic generation, so an old browser/runtime generation
        cannot mutate a newer request attempt.
        """

        expected_generation = _integer(
            expected_generation,
            "expected_generation",
            minimum=1,
        )
        new_generation = _integer(
            new_generation,
            "new_generation",
            minimum=1,
        )
        if new_generation != expected_generation + 1:
            raise ValueError("publisher generation must advance exactly once")
        request, state, rejection = self._locate(identity)
        if rejection is not None:
            return rejection
        assert request is not None and state is not None
        with request.lock:
            if request.retired:
                return MutationResult(False, "request_unknown", None)
            if state.identity.publisher_generation != expected_generation:
                return MutationResult(
                    False,
                    "publisher_generation_cas_mismatch",
                    self._snapshot(state),
                )
            if state.terminal is not None or state.cleanup_result is not None:
                return MutationResult(
                    False,
                    "already_terminal",
                    self._snapshot(state),
                )
            state.identity = replace(
                state.identity,
                publisher_generation=new_generation,
            )
            return MutationResult(
                True,
                "publisher_generation_advanced",
                self._snapshot(state),
            )

    def checkpoint(
        self,
        identity: CommandIdentity,
        *,
        observed_at_ms: int,
    ) -> CheckpointResult:
        """Return the next bounded cooperative action without executing runtime work."""

        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        request, state, rejection = self._locate(identity)
        if rejection is not None:
            return CheckpointResult(
                False,
                rejection.reason,
                CheckpointAction.TERMINAL,
                0,
                rejection.snapshot,
            )
        assert request is not None and state is not None
        with request.lock:
            if request.retired:
                return CheckpointResult(
                    False,
                    "request_unknown",
                    CheckpointAction.TERMINAL,
                    0,
                    None,
                )
            if observed_at_ms < state.envelope.issued_at_ms:
                return CheckpointResult(
                    False,
                    "observation_before_issue",
                    CheckpointAction.TERMINAL,
                    0,
                    self._snapshot(state),
                )
            self._expire_if_due(state, observed_at_ms)
            snapshot = self._snapshot(state)
            if state.terminal is not None:
                return CheckpointResult(
                    True,
                    "already_terminal",
                    CheckpointAction.TERMINAL,
                    0,
                    snapshot,
                )
            if state.cancellation_requested_at_ms is not None:
                return CheckpointResult(
                    True,
                    "cancel_requested",
                    CheckpointAction.CANCEL,
                    0,
                    snapshot,
                )
            remaining = state.identity.absolute_deadline_ms - observed_at_ms
            return CheckpointResult(
                True,
                "continue",
                CheckpointAction.CONTINUE,
                min(self._cooperative_step_ms, remaining),
                snapshot,
            )

    def terminal_compare_and_swap(
        self,
        result: TerminalResult,
        *,
        expected_terminal_revision: int,
    ) -> MutationResult:
        """Install one terminal result; exact canonical duplicates are idempotent."""

        expected = _integer(
            expected_terminal_revision,
            "expected_terminal_revision",
        )
        request, state, rejection = self._locate(result.identity)
        if rejection is not None:
            return rejection
        assert request is not None and state is not None
        with request.lock:
            if request.retired:
                return MutationResult(False, "request_unknown", None)
            if result.observed_at_ms < state.envelope.issued_at_ms:
                return MutationResult(False, "result_before_issue", self._snapshot(state))
            self._expire_if_due(state, result.observed_at_ms)
            payload_digest = self._terminal_payload_digest(result)
            if state.terminal is not None:
                if state.terminal_payload_digest == payload_digest:
                    return MutationResult(
                        True,
                        "duplicate",
                        self._snapshot(state),
                        duplicate=True,
                    )
                return MutationResult(False, "already_terminal", self._snapshot(state))
            if expected != state.terminal_revision:
                return MutationResult(False, "terminal_cas_mismatch", self._snapshot(state))
            if (
                state.cancellation_requested_at_ms is not None
                and result.status is TerminalStatus.COMPLETED
                and not state.completion_cleanup_authorized
            ):
                return MutationResult(False, "cancelled_command_cannot_complete", self._snapshot(state))
            if (
                result.status is TerminalStatus.CANCELLED
                and state.cancellation_requested_at_ms is None
            ):
                return MutationResult(False, "cancel_generation_missing", self._snapshot(state))
            if (
                result.status is TerminalStatus.CANCELLED
                and (
                    state.cleanup_result is None
                    or state.cleanup_result.status is not CleanupStatus.COMPLETED
                )
            ):
                return MutationResult(False, "cleanup_required", self._snapshot(state))
            if (
                state.cancellation_requested_at_ms is not None
                and result.observed_at_ms < state.cancellation_requested_at_ms
            ):
                return MutationResult(False, "result_before_cancellation", self._snapshot(state))
            if (
                result.status is TerminalStatus.DEADLINE_EXCEEDED
                and result.observed_at_ms < state.identity.absolute_deadline_ms
                and (
                    state.cleanup_deadline_ms is None
                    or result.observed_at_ms < state.cleanup_deadline_ms
                )
            ):
                return MutationResult(False, "deadline_not_reached", self._snapshot(state))
            self._set_terminal(state, result, payload_digest)
            return MutationResult(True, "terminal", self._snapshot(state))

    def record_cleanup(
        self,
        identity: CommandIdentity,
        *,
        owner_id: str,
        result: CleanupResult,
        observed_at_ms: int,
        expected_cleanup_revision: int = 0,
    ) -> MutationResult:
        """Record, but never perform, owner-scoped cleanup."""

        owner_id = _bounded_text(owner_id, "owner_id")
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        expected_cleanup_revision = _integer(
            expected_cleanup_revision,
            "expected_cleanup_revision",
        )
        encoded_size = len(
            json.dumps(
                {
                    "status": result.status.value,
                    "released_resource_count": result.released_resource_count,
                    "result_digest": result.result_digest,
                    "error_code": result.error_code,
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if encoded_size > MAX_CLEANUP_RESULT_BYTES:
            return MutationResult(False, "cleanup_result_too_large", None)

        request, state, rejection = self._locate(identity)
        if rejection is not None:
            return rejection
        assert request is not None and state is not None
        with request.lock:
            if request.retired:
                return MutationResult(False, "request_unknown", None)
            if owner_id != state.envelope.cleanup_owner_id:
                return MutationResult(False, "cleanup_owner_mismatch", self._snapshot(state))
            earliest = (
                state.terminal.observed_at_ms
                if state.terminal is not None
                else state.cancellation_requested_at_ms
                if state.cancellation_requested_at_ms is not None
                else state.envelope.issued_at_ms
            )
            if observed_at_ms < earliest:
                return MutationResult(False, "cleanup_before_authority", self._snapshot(state))
            if state.cleanup_result is not None:
                if state.cleanup_result == result:
                    return MutationResult(
                        True,
                        "duplicate",
                        self._snapshot(state),
                        duplicate=True,
                    )
                return MutationResult(False, "cleanup_conflict", self._snapshot(state))
            if expected_cleanup_revision != state.cleanup_revision:
                return MutationResult(False, "cleanup_cas_mismatch", self._snapshot(state))
            state.cleanup_result = result
            state.cleanup_completed_at_ms = observed_at_ms
            state.cleanup_revision += 1
            return MutationResult(True, "cleanup_recorded", self._snapshot(state))

    def snapshot(
        self,
        request_id: str,
        *,
        request_attempt: int,
    ) -> tuple[CommandSnapshot, ...]:
        """Return a detached, operation-sorted snapshot of the current attempt."""

        request_id = _bounded_text(request_id, "request_id")
        request_attempt = _integer(request_attempt, "request_attempt", minimum=1)
        with self._metadata_lock:
            request = self._requests.get(request_id)
        if request is None:
            return ()
        with request.lock:
            if request.retired:
                return ()
            if request.attempt != request_attempt:
                return ()
            return tuple(
                self._snapshot(request.commands[key])
                for key in sorted(request.commands)
            )

    def retire(self, request_id: str, *, expected_attempt: int) -> MutationResult:
        """Drop closed controller metadata so the registry remains bounded."""

        request_id = _bounded_text(request_id, "request_id")
        expected_attempt = _integer(expected_attempt, "expected_attempt", minimum=1)
        with self._metadata_lock:
            request = self._requests.get(request_id)
            if request is None:
                return MutationResult(False, "request_unknown", None)
            with request.lock:
                if request.attempt != expected_attempt:
                    return MutationResult(False, "stale_attempt", None)
                if not self._attempt_is_closed(request):
                    return MutationResult(False, "request_live", None)
                request.retired = True
                del self._requests[request_id]
        return MutationResult(True, "retired", None)

    def _locate(
        self,
        identity: CommandIdentity,
        *,
        allow_previous_cancel: bool = False,
    ) -> tuple[_RequestState | None, _CommandState | None, MutationResult | None]:
        with self._metadata_lock:
            request = self._requests.get(identity.request_id)
        if request is None:
            return None, None, MutationResult(False, "request_unknown", None)
        with request.lock:
            if request.retired:
                return None, None, MutationResult(False, "request_unknown", None)
            if identity.request_attempt != request.attempt:
                reason = "stale_attempt" if identity.request_attempt < request.attempt else "future_attempt"
                return None, None, MutationResult(False, reason, None)
            if identity.path_digest != request.path_digest:
                return None, None, MutationResult(False, "path_mismatch", None)
            state = request.commands.get(identity.command_id)
            if state is None:
                return None, None, MutationResult(False, "command_unknown", None)
            if identity != state.identity:
                previous_cancel = state.cancellation_request
                is_previous_cancel = (
                    allow_previous_cancel
                    and previous_cancel is not None
                    and identity.cancellation_generation == previous_cancel[0]
                    and state.identity.cancellation_generation == previous_cancel[1]
                    and replace(
                        state.identity,
                        cancellation_generation=previous_cancel[0],
                    )
                    == identity
                )
                if not is_previous_cancel:
                    return request, state, MutationResult(
                        False,
                        "stale_generation",
                        self._snapshot(state),
                    )
            return request, state, None

    def _expire_if_due(self, state: _CommandState, observed_at_ms: int) -> None:
        if state.terminal is not None:
            return
        effective_deadline = state.identity.absolute_deadline_ms
        if state.cleanup_deadline_ms is not None:
            effective_deadline = min(effective_deadline, state.cleanup_deadline_ms)
        if observed_at_ms < effective_deadline:
            return
        payload = {
            "identity": self._identity_payload(state.identity),
            "status": TerminalStatus.DEADLINE_EXCEEDED.value,
            "observed_at_ms": effective_deadline,
            "source": "controller_deadline",
        }
        result = TerminalResult(
            identity=state.identity,
            status=TerminalStatus.DEADLINE_EXCEEDED,
            observed_at_ms=effective_deadline,
            result_digest=_canonical_digest(payload),
        )
        if state.cancellation_requested_at_ms is None:
            state.cancellation_requested_at_ms = effective_deadline
            state.cleanup_deadline_ms = min(
                _MAX_MONOTONIC_MS,
                effective_deadline + self._interruption_and_cleanup_ms,
            )
        self._set_terminal(state, result, self._terminal_payload_digest(result))

    @staticmethod
    def _set_terminal(
        state: _CommandState,
        result: TerminalResult,
        payload_digest: str,
    ) -> None:
        state.terminal = result
        state.terminal_payload_digest = payload_digest
        state.terminal_revision += 1

    @staticmethod
    def _identity_payload(identity: CommandIdentity) -> dict[str, Any]:
        return {
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

    @classmethod
    def _terminal_payload_digest(cls, result: TerminalResult) -> str:
        return _canonical_digest(
            {
                "identity": cls._identity_payload(result.identity),
                "status": result.status.value,
                "observed_at_ms": result.observed_at_ms,
                "result_digest": result.result_digest,
                "error_code": result.error_code,
            }
        )

    @staticmethod
    def _attempt_is_closed(request: _RequestState) -> bool:
        return bool(request.commands) and all(
            state.terminal is not None and state.cleanup_result is not None
            for state in request.commands.values()
        )

    @staticmethod
    def _snapshot(state: _CommandState) -> CommandSnapshot:
        within_budget: bool | None = None
        if (
            state.cleanup_result is not None
            and state.cleanup_deadline_ms is not None
            and state.cleanup_completed_at_ms is not None
        ):
            within_budget = state.cleanup_completed_at_ms <= state.cleanup_deadline_ms
        return CommandSnapshot(
            identity=state.identity,
            kind=state.envelope.kind,
            terminal_revision=state.terminal_revision,
            terminal=state.terminal,
            cancellation_requested_at_ms=state.cancellation_requested_at_ms,
            cleanup_deadline_ms=state.cleanup_deadline_ms,
            cleanup_result=state.cleanup_result,
            cleanup_revision=state.cleanup_revision,
            cleanup_completed_at_ms=state.cleanup_completed_at_ms,
            cleanup_within_interruption_budget=within_budget,
        )


__all__ = [
    "CheckpointAction",
    "CheckpointResult",
    "CleanupResult",
    "CleanupStatus",
    "CommandController",
    "CommandEnvelope",
    "CommandIdentity",
    "CommandKind",
    "CommandSnapshot",
    "DEFAULT_COOPERATIVE_STEP_MS",
    "MAX_INTERRUPTION_AND_CLEANUP_MS",
    "MutationResult",
    "TerminalResult",
    "TerminalStatus",
]
