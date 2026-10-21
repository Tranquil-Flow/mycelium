# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic A7 reference model; never product or physical recovery evidence."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping, Sequence

GATE_STATE = "design_only"
EVIDENCE_KIND = "deterministic_fixture"
A6_PRODUCT_INTERFACE = "mycelium.full_context_recovery_runtime.v1"
REQUIRED_FENCE_INSTALLERS = frozenset(
    {
        "request_gateway",
        "router",
        "surviving_stage_runtimes",
        "transport_ingress",
        "standby_runtime",
    }
)


class KvRecoveryRejected(ValueError):
    """A fail-closed result from the test-only A7 model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KvCompatibility:
    model_id: str
    immutable_revision: str
    representation_manifest_digest: str
    half_open_layer_range: tuple[int, int]
    component_roles: tuple[str, ...]
    backend: str
    runtime_family: str
    runtime_build: str
    checkpoint_schema_version: str
    cache_layout: str
    dtype: str
    byte_order: str
    position_semantics: str
    sequence_semantics: str


@dataclass(frozen=True)
class FenceAuthority:
    request_id: str
    attempt: int
    path_generation: int
    fencing_generation: int


@dataclass(frozen=True)
class StageCheckpoint:
    stage_id: str
    source_owner: str
    successor_owner: str
    request_id: str
    source_attempt: int
    source_path_generation: int
    source_membership_incarnation: int
    successor_membership_incarnation: int
    checkpoint_sequence: int
    predecessor_digest: str
    checkpoint_digest: str
    committed_token_count: int
    committed_prefix_digest: str
    compatibility: KvCompatibility


@dataclass(frozen=True)
class KvDecision:
    mode: str
    reasons: tuple[str, ...]
    invoked_interface: str | None
    gate_state: str
    evidence_kind: str
    physical_recovery_evidence: bool
    qualification_claim: bool
    promotion_authorized: bool
    successor_authority: FenceAuthority | None
    transferred_stages: tuple[str, ...]
    transferred_prompt_tokens: int
    transferred_committed_prefix_tokens: int
    recovery_prefill_performed: bool


class A7KvRecoveryHarness:
    """Pure reference state for exact KV admission, transfer, fencing and fallback."""

    def __init__(
        self,
        *,
        request_id: str,
        gateway_committed_token_count: int,
        gateway_committed_prefix_digest: str,
        compatibility: KvCompatibility,
        stage_owners: Mapping[str, str],
        replacements: Mapping[str, str],
        source_attempt: int = 8,
        source_path_generation: int = 12,
        fencing_generation: int = 19,
    ) -> None:
        if not replacements or not set(replacements) < set(stage_owners):
            raise ValueError("replacements_must_be_nonempty_strict_stage_subset")
        self.request_id = request_id
        self.gateway_committed_token_count = gateway_committed_token_count
        self.gateway_committed_prefix_digest = gateway_committed_prefix_digest
        self.compatibility = compatibility
        self.stage_owners = dict(stage_owners)
        self.replacements = dict(replacements)
        self.authority = FenceAuthority(
            request_id=request_id,
            attempt=source_attempt,
            path_generation=source_path_generation,
            fencing_generation=fencing_generation,
        )
        self._source_authority = self.authority
        self.fence_installed = False
        self.published: list[tuple[int, int, str]] = []

    @staticmethod
    def _compatibility_reasons(
        expected: KvCompatibility, actual: KvCompatibility
    ) -> list[str]:
        return [
            f"compatibility.{field.name}_mismatch"
            for field in fields(KvCompatibility)
            if getattr(expected, field.name) != getattr(actual, field.name)
        ]

    def _checkpoint_reasons(self, checkpoints: Sequence[StageCheckpoint]) -> list[str]:
        reasons: list[str] = []
        by_stage = {checkpoint.stage_id: checkpoint for checkpoint in checkpoints}
        if len(by_stage) != len(checkpoints):
            reasons.append("checkpoint.duplicate_stage")
        if set(by_stage) != set(self.replacements):
            reasons.append("checkpoint.replaced_stage_set_mismatch")
            return reasons

        sequences: list[int] = []
        for stage_id in sorted(by_stage):
            checkpoint = by_stage[stage_id]
            sequences.append(checkpoint.checkpoint_sequence)
            if checkpoint.request_id != self.request_id:
                reasons.append(f"checkpoint.{stage_id}.request_mismatch")
            if checkpoint.source_owner != self.stage_owners[stage_id]:
                reasons.append(f"checkpoint.{stage_id}.source_owner_mismatch")
            if checkpoint.successor_owner != self.replacements[stage_id]:
                reasons.append(f"checkpoint.{stage_id}.successor_owner_mismatch")
            if checkpoint.source_attempt != self.authority.attempt:
                reasons.append(f"checkpoint.{stage_id}.attempt_mismatch")
            if checkpoint.source_path_generation != self.authority.path_generation:
                reasons.append(f"checkpoint.{stage_id}.path_generation_mismatch")
            if checkpoint.committed_token_count != self.gateway_committed_token_count:
                reasons.append(f"checkpoint.{stage_id}.watermark_count_mismatch")
            if (
                checkpoint.committed_prefix_digest
                != self.gateway_committed_prefix_digest
            ):
                reasons.append(f"checkpoint.{stage_id}.watermark_digest_mismatch")
            for mismatch in self._compatibility_reasons(
                self.compatibility, checkpoint.compatibility
            ):
                reasons.append(f"checkpoint.{stage_id}.{mismatch}")
        if len(set(sequences)) != 1:
            reasons.append("checkpoint.mixed_sequence")
        return reasons

    @staticmethod
    def _decision(
        *,
        mode: str,
        reasons: Sequence[str],
        invoked_interface: str | None,
        successor_authority: FenceAuthority | None = None,
        transferred_stages: Sequence[str] = (),
    ) -> KvDecision:
        return KvDecision(
            mode=mode,
            reasons=tuple(sorted(set(reasons))),
            invoked_interface=invoked_interface,
            gate_state=GATE_STATE,
            evidence_kind=EVIDENCE_KIND,
            physical_recovery_evidence=False,
            qualification_claim=False,
            promotion_authorized=False,
            successor_authority=successor_authority,
            transferred_stages=tuple(sorted(transferred_stages)),
            transferred_prompt_tokens=0,
            transferred_committed_prefix_tokens=0,
            recovery_prefill_performed=False,
        )

    def decide(
        self,
        *,
        successor_compatibility: KvCompatibility,
        checkpoints: Sequence[StageCheckpoint],
        installed_fence_participants: set[str],
        a6_available: bool,
        a6_interface: str | None,
    ) -> KvDecision:
        reasons = self._compatibility_reasons(
            self.compatibility, successor_compatibility
        )
        reasons.extend(self._checkpoint_reasons(checkpoints))
        missing_installers = REQUIRED_FENCE_INSTALLERS - installed_fence_participants
        unknown_installers = installed_fence_participants - REQUIRED_FENCE_INSTALLERS
        reasons.extend(f"fence.{name}_missing" for name in sorted(missing_installers))
        reasons.extend(f"fence.{name}_unknown" for name in sorted(unknown_installers))

        if reasons:
            if a6_available and a6_interface == A6_PRODUCT_INTERFACE:
                return self._decision(
                    mode="full_context_replay",
                    reasons=reasons,
                    invoked_interface=A6_PRODUCT_INTERFACE,
                )
            fallback_reasons = list(reasons)
            fallback_reasons.append("a6.current_product_interface_unavailable")
            return self._decision(
                mode="aborted",
                reasons=fallback_reasons,
                invoked_interface=None,
            )

        successor_authority = FenceAuthority(
            request_id=self.request_id,
            attempt=self.authority.attempt + 1,
            path_generation=self.authority.path_generation + 1,
            fencing_generation=self.authority.fencing_generation + 1,
        )
        self.authority = successor_authority
        self.fence_installed = True
        for stage_id, successor_owner in self.replacements.items():
            self.stage_owners[stage_id] = successor_owner
        return self._decision(
            mode="fenced_kv_successor",
            reasons=(),
            invoked_interface=None,
            successor_authority=successor_authority,
            transferred_stages=self.replacements,
        )

    def publish(
        self,
        *,
        position: int,
        token: int,
        stage_id: str,
        stage_owner: str,
        writer: FenceAuthority,
    ) -> None:
        if not self.fence_installed or writer != self.authority:
            raise KvRecoveryRejected("stale_writer_generation")
        if self.stage_owners.get(stage_id) != stage_owner:
            raise KvRecoveryRejected("stage_owner_not_current")
        expected = self.gateway_committed_token_count + len(self.published) + 1
        if position != expected:
            raise KvRecoveryRejected("publication_not_next_contiguous_position")
        self.published.append((position, token, stage_id))
