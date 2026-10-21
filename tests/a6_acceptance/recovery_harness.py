# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic A6 reference model; never product or physical recovery evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Mapping, Sequence


A6_PRODUCT_INTERFACE = "mycelium.full_context_recovery_runtime.v1"
GATE_STATE = "design_only"
EVIDENCE_KIND = "deterministic_fixture"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class RecoveryRejected(ValueError):
    """A fail-closed result from the test-only reference model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Authority:
    request_id: str
    attempt: int
    path_generation: int
    path_id: str


@dataclass(frozen=True)
class DeliveryEvent:
    session_id: str
    request_id: str
    sequence: int
    token: int
    attempt: int
    path_generation: int


@dataclass(frozen=True)
class ReplayCheckpoint:
    session_id: str
    request_id: str
    source_attempt: int
    source_path_generation: int
    encoded_prompt: tuple[int, ...]
    generation_settings_digest: str
    committed_prefix: tuple[int, ...]
    committed_token_count: int
    committed_prefix_digest: str
    delivery_cursor: int
    checkpoint_digest: str


@dataclass(frozen=True)
class FailureObservation:
    authority: str
    request_id: str
    attempt: int
    path_generation: int
    scope: str


@dataclass(frozen=True)
class ReplayResult:
    protocol: str
    gate_state: str
    evidence_kind: str
    physical_recovery_evidence: bool
    reconstructed_context: tuple[int, ...]
    suppressed_output_positions: tuple[int, ...]
    first_publishable_position: int
    successor_authority: Authority


class A6RecoveryHarness:
    """Small deterministic model of gateway authority and request-local recovery."""

    _RESOURCE_NAMES = (
        "source_path",
        "source_capacity",
        "source_command",
        "source_kv",
        "successor_path",
        "successor_capacity",
        "successor_replay_command",
        "successor_kv",
        "transport_stream",
    )

    def __init__(
        self,
        *,
        session_id: str,
        request_id: str,
        encoded_prompt: Sequence[int],
        generation_settings: Mapping[str, object],
        source_attempt: int = 7,
        source_path_generation: int = 11,
        source_path_id: str = "source-path",
    ) -> None:
        self.session_id = session_id
        self.request_id = request_id
        self.encoded_prompt = tuple(encoded_prompt)
        self.generation_settings_digest = _digest(dict(generation_settings))
        self.authority = Authority(
            request_id=request_id,
            attempt=source_attempt,
            path_generation=source_path_generation,
            path_id=source_path_id,
        )
        self._source_authority = self.authority
        self._ledger: list[DeliveryEvent] = []
        self.failure: FailureObservation | None = None
        self.phase = "active"
        self.terminal_outcome: str | None = None
        self.live_resources = {name: 0 for name in self._RESOURCE_NAMES}
        for name in ("source_path", "source_capacity", "source_command", "source_kv"):
            self.live_resources[name] = 1
        self.release_count = {name: 0 for name in self._RESOURCE_NAMES}

    @property
    def ledger(self) -> tuple[DeliveryEvent, ...]:
        return tuple(self._ledger)

    @property
    def committed_prefix(self) -> tuple[int, ...]:
        return tuple(event.token for event in self._ledger)

    def _require_current(self, writer: Authority) -> None:
        if writer != self.authority:
            raise RecoveryRejected("stale_request_attempt_or_path_generation")

    def publish(
        self, *, position: int, token: int, writer: Authority, caller: str
    ) -> bool:
        """Atomically append one gateway-owned delivery identity and watermark step."""

        if caller != "request_gateway":
            raise RecoveryRejected("delivery_authority_not_gateway")
        self._require_current(writer)
        if self.terminal_outcome is not None:
            raise RecoveryRejected("request_already_terminal")
        expected = len(self._ledger) + 1
        if position < expected:
            existing = self._ledger[position - 1] if position > 0 else None
            if (
                existing is not None
                and existing.token == token
                and existing.attempt == writer.attempt
                and existing.path_generation == writer.path_generation
            ):
                return False
            raise RecoveryRejected("conflicting_delivery_identity")
        if position != expected:
            raise RecoveryRejected("noncontiguous_delivery_position")
        self._ledger.append(
            DeliveryEvent(
                session_id=self.session_id,
                request_id=self.request_id,
                sequence=position,
                token=token,
                attempt=writer.attempt,
                path_generation=writer.path_generation,
            )
        )
        return True

    def _checkpoint(self) -> ReplayCheckpoint:
        payload = {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "source_attempt": self._source_authority.attempt,
            "source_path_generation": self._source_authority.path_generation,
            "encoded_prompt": self.encoded_prompt,
            "generation_settings_digest": self.generation_settings_digest,
            "committed_prefix": self.committed_prefix,
            "committed_token_count": len(self._ledger),
            "committed_prefix_digest": _digest(self.committed_prefix),
            "delivery_cursor": len(self._ledger),
        }
        return ReplayCheckpoint(**payload, checkpoint_digest=_digest(payload))

    def freeze_checkpoint(self, *, caller: str) -> ReplayCheckpoint:
        if caller != "request_gateway":
            raise RecoveryRejected("checkpoint_authority_not_gateway")
        if self.phase not in {"failure_observed", "checkpoint_frozen"}:
            raise RecoveryRejected("checkpoint_phase_invalid")
        self.phase = "checkpoint_frozen"
        return self._checkpoint()

    def observe_failure(self, observation: FailureObservation) -> None:
        if self.phase != "active":
            raise RecoveryRejected("failure_phase_invalid")
        if observation.authority != "a4_liveness_authority":
            raise RecoveryRejected("failure_authority_not_a4")
        if observation.scope != "request":
            raise RecoveryRejected("failure_scope_widened")
        if (
            observation.request_id != self.request_id
            or observation.attempt != self.authority.attempt
            or observation.path_generation != self.authority.path_generation
        ):
            raise RecoveryRejected("failure_identity_mismatch")
        self.failure = observation
        self.phase = "failure_observed"

    def recover(
        self,
        *,
        checkpoint: ReplayCheckpoint,
        encoded_prompt: Sequence[int],
        committed_prefix: Sequence[int],
        successor_attempt: int,
        successor_path_generation: int,
        successor_path_id: str,
    ) -> ReplayResult:
        if self.failure is None:
            raise RecoveryRejected("failure_not_observed")
        if checkpoint != self._checkpoint():
            raise RecoveryRejected("checkpoint_not_current_gateway_authority")
        if tuple(encoded_prompt) != checkpoint.encoded_prompt:
            raise RecoveryRejected("encoded_prompt_mismatch")
        if tuple(committed_prefix) != checkpoint.committed_prefix:
            raise RecoveryRejected("committed_prefix_mismatch")
        if successor_attempt != checkpoint.source_attempt + 1:
            raise RecoveryRejected("successor_attempt_not_exact_increment")
        if successor_path_generation != checkpoint.source_path_generation + 1:
            raise RecoveryRejected("successor_path_generation_not_exact_increment")

        successor = Authority(
            request_id=self.request_id,
            attempt=successor_attempt,
            path_generation=successor_path_generation,
            path_id=successor_path_id,
        )
        for name in (
            "successor_path",
            "successor_capacity",
            "successor_replay_command",
            "successor_kv",
            "transport_stream",
        ):
            self.live_resources[name] = 1
        self.authority = successor
        self.phase = "recovered_decode"
        return ReplayResult(
            protocol=A6_PRODUCT_INTERFACE,
            gate_state=GATE_STATE,
            evidence_kind=EVIDENCE_KIND,
            physical_recovery_evidence=False,
            reconstructed_context=checkpoint.encoded_prompt
            + checkpoint.committed_prefix,
            suppressed_output_positions=tuple(
                range(1, checkpoint.committed_token_count + 1)
            ),
            first_publishable_position=checkpoint.committed_token_count + 1,
            successor_authority=successor,
        )

    def publish_terminal(self, *, outcome: str, writer: Authority) -> bool:
        self._require_current(writer)
        if self.terminal_outcome is not None:
            if self.terminal_outcome == outcome:
                return False
            raise RecoveryRejected("conflicting_terminal_result")
        self.terminal_outcome = outcome
        return True

    def cleanup(self, *, writer: Authority) -> bool:
        self._require_current(writer)
        changed = False
        for name, live in self.live_resources.items():
            if live:
                self.release_count[name] += live
                self.live_resources[name] = 0
                changed = True
        self.phase = "terminal" if self.terminal_outcome is not None else "aborted"
        return changed

    def tampered_checkpoint(self, **changes: object) -> ReplayCheckpoint:
        """Convenience for negative fixtures; it grants no checkpoint authority."""

        return replace(self._checkpoint(), **changes)
