# SPDX-License-Identifier: AGPL-3.0-only
"""A6 bridge from qualified replay checkpoints to M16-owned path authority.

This adapter consumes owner/qualifier artifacts and asks M16 to reserve a
successor. It never mints path IDs, request attempts, or compatibility facts.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from mycelium_a6_recovery import (
    ReplayCheckpoint,
    normalize_compatibility,
    normalize_recovery_successor_binding,
    recovery_request_binding,
    validate_recovery_qualification,
)
from mycelium_m16_runtime import M16AdmissionError, M16RuntimeCoordinator
from mycelium_router.contracts import PathManifest, RequestContext


def recovery_successor_binding(
    identity: Mapping[str, Any],
    manifest: PathManifest,
) -> dict[str, Any]:
    """Project exact M16-owned successor identity for qualifier binding."""

    if not isinstance(identity, Mapping) or not isinstance(manifest, PathManifest):
        raise M16AdmissionError("recovery_successor_binding_invalid")
    try:
        projected = normalize_recovery_successor_binding(
            {
                "request_id": identity["request_id"],
                "request_attempt": identity["request_attempt"],
                "path_id": identity["path_id"],
                "path_attempt": identity["path_attempt"],
                "path_manifest_digest": identity["path_manifest_digest"],
                "placement_ids": [
                    hop.placement_id for hop in manifest.ordered_hops
                ],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise M16AdmissionError("recovery_successor_binding_invalid") from exc
    if (
        projected["path_id"] != manifest.path_id
        or projected["path_attempt"] != manifest.path_attempt
        or identity.get("deployment_epoch") != manifest.deployment_epoch
        or identity.get("topology_generation") != manifest.topology_version
    ):
        raise M16AdmissionError("recovery_successor_binding_invalid")
    return projected


class M16RecoveryAuthority:
    """Fail-closed qualified successor reservation against live M16 authority."""

    def __init__(self, coordinator: M16RuntimeCoordinator) -> None:
        if not isinstance(coordinator, M16RuntimeCoordinator):
            raise TypeError("invalid_m16_coordinator")
        self._coordinator = coordinator

    def _validated_recovery_request(
        self,
        *,
        checkpoint: ReplayCheckpoint,
        source_request: RequestContext,
        expected_source_identity: Mapping[str, Any],
        now_unix_ms: int,
    ) -> tuple[dict[str, Any], RequestContext]:
        if not isinstance(checkpoint, ReplayCheckpoint):
            raise M16AdmissionError("recovery_checkpoint_invalid")
        try:
            checkpoint._verify_bindings()
        except ValueError as exc:
            raise M16AdmissionError("recovery_checkpoint_invalid") from exc
        if (
            type(now_unix_ms) is not int
            or now_unix_ms < checkpoint.created_at_unix_ms
            or now_unix_ms >= checkpoint.expiry_unix_ms
        ):
            raise M16AdmissionError("recovery_checkpoint_stale")

        deployment = self._coordinator.recovery_deployment_binding()
        if (
            checkpoint.deployment_id != deployment["deployment_id"]
            or checkpoint.deployment_epoch != deployment["deployment_epoch"]
            or checkpoint.model_id != deployment["model_id"]
            or checkpoint.representation_digest
            != deployment["representation_digest"]
        ):
            raise M16AdmissionError("recovery_compatibility_mismatch")

        current = self._coordinator.route_identity(checkpoint.request_id)
        checkpoint_source = {
            "request_id": checkpoint.request_id,
            "request_attempt": checkpoint.source_attempt,
            "path_id": checkpoint.path_id,
            "path_attempt": checkpoint.path_attempt,
            "path_manifest_digest": checkpoint.path_digest,
            "deployment_id": checkpoint.deployment_id,
            "deployment_epoch": checkpoint.deployment_epoch,
            "topology_generation": checkpoint.topology_generation,
        }
        if (
            dict(expected_source_identity) != current
            or checkpoint_source != current
            or source_request.request_id != checkpoint.request_id
            or source_request.prompt_token_ids != checkpoint.encoded_prompt
        ):
            raise M16AdmissionError("stale_source_identity")

        committed_count = checkpoint.committed_token_count
        if (
            committed_count != len(checkpoint.committed_prefix_tokens)
            or committed_count < 0
            or committed_count >= source_request.max_new_tokens
        ):
            raise M16AdmissionError("recovery_context_invalid")
        recovery_request = replace(
            source_request,
            prompt_token_ids=(
                checkpoint.encoded_prompt + checkpoint.committed_prefix_tokens
            ),
            max_new_tokens=source_request.max_new_tokens - committed_count,
            expected_new_tokens=(
                source_request.expected_new_tokens - committed_count
            ),
        )
        return current, recovery_request

    def reserve_candidate(
        self,
        *,
        checkpoint: ReplayCheckpoint,
        source_request: RequestContext,
        expected_source_identity: Mapping[str, Any],
        excluded_placements: frozenset[str],
        now_unix_ms: int,
    ) -> PathManifest:
        """Reserve an owner-issued candidate without fencing source publication."""

        current, recovery_request = self._validated_recovery_request(
            checkpoint=checkpoint,
            source_request=source_request,
            expected_source_identity=expected_source_identity,
            now_unix_ms=now_unix_ms,
        )
        return self._coordinator.reserve_recovery_successor(
            checkpoint.request_id,
            recovery_request=recovery_request,
            expected_source_identity=current,
            excluded_placements=excluded_placements,
        )

    def _discard_candidate(
        self,
        request_id: str,
        expected_source_identity: Mapping[str, Any],
    ) -> None:
        try:
            self._coordinator.discard_recovery_successor(
                request_id,
                expected_source_identity=expected_source_identity,
            )
        except M16AdmissionError:
            pass

    def qualify_reserved_successor(
        self,
        *,
        checkpoint: ReplayCheckpoint,
        successor_manifest: PathManifest,
        qualification: Mapping[str, Any],
        source_compatibility: Mapping[str, Any],
        expected_source_identity: Mapping[str, Any],
        now_unix_ms: int,
    ) -> PathManifest:
        """Validate qualifier authority against the exact reserved M16 path."""

        try:
            qualified = validate_recovery_qualification(qualification)
            source = normalize_compatibility(source_compatibility)
            if (
                type(now_unix_ms) is not int
                or now_unix_ms < qualified["issued_at_unix_ms"]
                or now_unix_ms >= qualified["expiry_unix_ms"]
            ):
                raise M16AdmissionError("recovery_qualification_stale")
            if qualified["physical_challenge"] != "passed":
                raise M16AdmissionError(
                    "recovery_physical_qualification_required"
                )
            if qualified["request_binding"] != recovery_request_binding(checkpoint):
                raise M16AdmissionError("recovery_request_binding_mismatch")
            successor_identity = self._coordinator.recovery_successor_identity(
                checkpoint.request_id
            )
            if qualified["successor_binding"] != recovery_successor_binding(
                successor_identity,
                successor_manifest,
            ):
                raise M16AdmissionError("recovery_successor_binding_mismatch")
            if (
                qualified["compatible"] is not True
                or qualified["freshness"] != "current"
                or qualified["parity"] != "exact"
                or qualified["resource"] != "reserved"
                or qualified["route"] != "current"
                or qualified["fallback"] != "none"
                or qualified["compatibility"] != source
            ):
                raise M16AdmissionError("recovery_compatibility_mismatch")

            deployment = self._coordinator.recovery_deployment_binding()
            if qualified["binding"] != deployment:
                raise M16AdmissionError("recovery_deployment_mismatch")
            if (
                source["model_id"] != deployment["model_id"]
                or source["model_revision"] != deployment["model_revision"]
                or source["representation_digest"]
                != deployment["representation_digest"]
                or source["manifest_digest"]
                != deployment["representation_digest"]
                or checkpoint.model_id != source["model_id"]
                or checkpoint.representation_digest
                != source["representation_digest"]
                or checkpoint.tokenizer_digest != source["tokenizer_digest"]
                or checkpoint.generation_settings_digest
                != source["generation_settings_digest"]
            ):
                raise M16AdmissionError("recovery_compatibility_mismatch")
            if (
                dict(expected_source_identity)
                != self._coordinator.route_identity(checkpoint.request_id)
            ):
                raise M16AdmissionError("stale_source_identity")
            return successor_manifest
        except ValueError as exc:
            self._discard_candidate(
                checkpoint.request_id,
                expected_source_identity,
            )
            raise M16AdmissionError("recovery_qualification_invalid") from exc
        except M16AdmissionError:
            self._discard_candidate(
                checkpoint.request_id,
                expected_source_identity,
            )
            raise

    def reserve_successor(
        self,
        *,
        checkpoint: ReplayCheckpoint,
        qualification: Mapping[str, Any],
        source_compatibility: Mapping[str, Any],
        source_request: RequestContext,
        expected_source_identity: Mapping[str, Any],
        excluded_placements: frozenset[str],
        now_unix_ms: int,
    ) -> PathManifest:
        """Compatibility wrapper for an already candidate-bound qualification."""

        manifest = self.reserve_candidate(
            checkpoint=checkpoint,
            source_request=source_request,
            expected_source_identity=expected_source_identity,
            excluded_placements=excluded_placements,
            now_unix_ms=now_unix_ms,
        )
        return self.qualify_reserved_successor(
            checkpoint=checkpoint,
            successor_manifest=manifest,
            qualification=qualification,
            source_compatibility=source_compatibility,
            expected_source_identity=expected_source_identity,
            now_unix_ms=now_unix_ms,
        )
