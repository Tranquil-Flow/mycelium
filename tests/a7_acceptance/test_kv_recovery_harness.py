# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path

import pytest

from .kv_recovery_harness import (
    A6_PRODUCT_INTERFACE,
    REQUIRED_FENCE_INSTALLERS,
    A7KvRecoveryHarness,
    KvCompatibility,
    KvRecoveryRejected,
    StageCheckpoint,
)


MANIFEST = Path(__file__).with_name("harness.v1.json")
A6_INVENTORY = Path(__file__).parents[1] / "a6_acceptance" / "inventory.v1.json"
A6_HARNESS_MANIFEST = Path(__file__).parents[1] / "a6_acceptance" / "harness.v1.json"
EXPECTED_COVERAGE = {
    "kv_compatibility",
    "successor_fencing",
    "stage_local_ownership_transfer",
    "stale_writer_rejection",
    "exact_fallback_behavior",
    "cross_representation_backend_schema_rejection",
}
EXPECTED_INTERFACES = {
    "a7.stage_local_kv_checkpoint_export.v1",
    "a7.standby_checkpoint_store_ack.v1",
    "a7.route_wide_acknowledged_watermark.v1",
    "a7.exact_kv_successor_qualification.v1",
    "a7.distributed_request_fence_install.v1",
    "a7.stage_local_kv_ownership_transfer.v1",
    A6_PRODUCT_INTERFACE,
}


def _compatibility() -> KvCompatibility:
    return KvCompatibility(
        model_id="qwen3-0.6b",
        immutable_revision="rev-123",
        representation_manifest_digest="sha256:representation",
        half_open_layer_range=(8, 16),
        component_roles=("transformer_layers",),
        backend="mlx",
        runtime_family="mycelium-mlx",
        runtime_build="build-7",
        checkpoint_schema_version="kv.v3",
        cache_layout="batch_sequence_kv_head_dimension",
        dtype="float16",
        byte_order="little",
        position_semantics="absolute_zero_based",
        sequence_semantics="next_position_after_committed_watermark",
    )


def _harness() -> A7KvRecoveryHarness:
    return A7KvRecoveryHarness(
        request_id="request-a",
        gateway_committed_token_count=3,
        gateway_committed_prefix_digest="sha256:prefix",
        compatibility=_compatibility(),
        stage_owners={
            "stage-0": "source-a",
            "stage-1": "source-b",
            "stage-2": "source-c",
        },
        replacements={"stage-1": "standby-b", "stage-2": "standby-c"},
    )


def _checkpoints(
    harness: A7KvRecoveryHarness,
    *,
    compatibility: KvCompatibility | None = None,
) -> tuple[StageCheckpoint, ...]:
    identity = harness.compatibility if compatibility is None else compatibility
    return tuple(
        StageCheckpoint(
            stage_id=stage_id,
            source_owner=harness.stage_owners[stage_id],
            successor_owner=successor_owner,
            request_id=harness.request_id,
            source_attempt=harness.authority.attempt,
            source_path_generation=harness.authority.path_generation,
            source_membership_incarnation=4,
            successor_membership_incarnation=9,
            checkpoint_sequence=17,
            predecessor_digest="sha256:checkpoint-16",
            checkpoint_digest=f"sha256:checkpoint-17-{stage_id}",
            committed_token_count=harness.gateway_committed_token_count,
            committed_prefix_digest=harness.gateway_committed_prefix_digest,
            compatibility=identity,
        )
        for stage_id, successor_owner in sorted(harness.replacements.items())
    )


def _decide_success(harness: A7KvRecoveryHarness):
    return harness.decide(
        successor_compatibility=harness.compatibility,
        checkpoints=_checkpoints(harness),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )


def test_manifest_is_closed_design_only_lists_interfaces_and_binds_a6_dependency() -> (
    None
):
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    a6_inventory = json.loads(A6_INVENTORY.read_text("utf-8"))
    a6_harness = json.loads(A6_HARNESS_MANIFEST.read_text("utf-8"))

    assert set(manifest) == {
        "protocol",
        "gate_state",
        "evidence_kind",
        "claim_boundary",
        "depends_on",
        "a6_dependency",
        "coverage",
        "production_interfaces_required_after_a4",
        "physical_recovery_evidence",
        "qualification_claim",
        "promotion_authorized",
    }
    assert manifest["gate_state"] == "design_only"
    assert manifest["evidence_kind"] == "deterministic_fixture"
    assert "never" in manifest["claim_boundary"]
    assert manifest["depends_on"] == ["A4", "A6"]
    dependency = manifest["a6_dependency"]
    assert set(dependency) == {
        "inventory_protocol",
        "fallback_interface",
        "required_state",
    }
    assert dependency["inventory_protocol"] == a6_inventory["protocol"]
    assert dependency["fallback_interface"] == A6_PRODUCT_INTERFACE
    assert A6_PRODUCT_INTERFACE in {
        item["interface_id"]
        for item in a6_harness["production_interfaces_required_after_a4"]
    }
    assert dependency["required_state"] == "physically_qualified_before_a7_integration"
    assert set(manifest["coverage"]) == EXPECTED_COVERAGE
    interfaces = manifest["production_interfaces_required_after_a4"]
    assert {item["interface_id"] for item in interfaces} == EXPECTED_INTERFACES
    assert len(interfaces) == len(EXPECTED_INTERFACES)
    assert all(
        set(item) == {"interface_id", "owner", "capability"} for item in interfaces
    )
    assert manifest["physical_recovery_evidence"] is False
    assert manifest["qualification_claim"] is False
    assert manifest["promotion_authorized"] is False


@pytest.mark.parametrize("field", [item.name for item in fields(KvCompatibility)])
def test_every_kv_compatibility_field_is_exact_and_falls_back_through_a6(
    field: str,
) -> None:
    harness = _harness()
    original = getattr(harness.compatibility, field)
    mismatch = (999, 1000) if isinstance(original, tuple) else f"{original}-mismatch"
    successor = replace(harness.compatibility, **{field: mismatch})
    owners_before = harness.stage_owners.copy()
    authority_before = harness.authority

    decision = harness.decide(
        successor_compatibility=successor,
        checkpoints=_checkpoints(harness),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )

    assert decision.mode == "full_context_replay"
    assert decision.reasons == (f"compatibility.{field}_mismatch",)
    assert decision.invoked_interface == A6_PRODUCT_INTERFACE
    assert decision.gate_state == "design_only"
    assert decision.evidence_kind == "deterministic_fixture"
    assert decision.physical_recovery_evidence is False
    assert harness.stage_owners == owners_before
    assert harness.authority == authority_before
    assert harness.fence_installed is False


@pytest.mark.parametrize(
    "field",
    ["representation_manifest_digest", "backend", "checkpoint_schema_version"],
)
def test_cross_representation_backend_or_schema_checkpoint_is_rejected(
    field: str,
) -> None:
    harness = _harness()
    incompatible = replace(
        harness.compatibility,
        **{field: f"cross-{field}"},
    )

    decision = harness.decide(
        successor_compatibility=harness.compatibility,
        checkpoints=_checkpoints(harness, compatibility=incompatible),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )

    assert decision.mode == "full_context_replay"
    assert decision.reasons == tuple(
        f"checkpoint.{stage_id}.compatibility.{field}_mismatch"
        for stage_id in ("stage-1", "stage-2")
    )
    assert decision.invoked_interface == A6_PRODUCT_INTERFACE


def test_success_requires_every_fence_installer_and_advances_all_generations_once() -> (
    None
):
    harness = _harness()
    source = harness.authority
    missing_transport = set(REQUIRED_FENCE_INSTALLERS) - {"transport_ingress"}

    fallback = harness.decide(
        successor_compatibility=harness.compatibility,
        checkpoints=_checkpoints(harness),
        installed_fence_participants=missing_transport,
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )
    assert fallback.mode == "full_context_replay"
    assert fallback.reasons == ("fence.transport_ingress_missing",)
    assert harness.authority == source
    assert harness.fence_installed is False

    success = _decide_success(harness)
    assert success.mode == "fenced_kv_successor"
    assert success.reasons == ()
    assert success.invoked_interface is None
    assert success.successor_authority is not None
    assert success.successor_authority.attempt == source.attempt + 1
    assert success.successor_authority.path_generation == source.path_generation + 1
    assert (
        success.successor_authority.fencing_generation == source.fencing_generation + 1
    )
    assert harness.fence_installed is True


def test_stage_local_ownership_transfer_changes_only_replaced_stages() -> None:
    harness = _harness()
    owners_before = harness.stage_owners.copy()

    decision = _decide_success(harness)

    assert decision.transferred_stages == ("stage-1", "stage-2")
    assert harness.stage_owners == {
        "stage-0": owners_before["stage-0"],
        "stage-1": "standby-b",
        "stage-2": "standby-c",
    }
    assert decision.transferred_prompt_tokens == 0
    assert decision.transferred_committed_prefix_tokens == 0
    assert decision.recovery_prefill_performed is False


def test_stale_source_writer_is_rejected_without_publication_or_owner_mutation() -> (
    None
):
    harness = _harness()
    source = harness.authority
    successor = _decide_success(harness).successor_authority
    assert successor is not None
    owners_after_cutover = harness.stage_owners.copy()

    with pytest.raises(KvRecoveryRejected, match="stale_writer_generation"):
        harness.publish(
            position=4,
            token=304,
            stage_id="stage-1",
            stage_owner="source-b",
            writer=source,
        )
    assert harness.published == []
    assert harness.stage_owners == owners_after_cutover
    harness.publish(
        position=4,
        token=304,
        stage_id="stage-1",
        stage_owner="standby-b",
        writer=successor,
    )
    assert harness.published == [(4, 304, "stage-1")]


def test_any_kv_rejection_has_exact_a6_fallback_or_honest_abort() -> None:
    mismatch = replace(_compatibility(), checkpoint_schema_version="kv.v999")
    with_a6_harness = _harness()

    with_a6 = with_a6_harness.decide(
        successor_compatibility=mismatch,
        checkpoints=_checkpoints(with_a6_harness),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )
    assert with_a6.mode == "full_context_replay"
    assert with_a6.reasons == ("compatibility.checkpoint_schema_version_mismatch",)
    assert with_a6.invoked_interface == A6_PRODUCT_INTERFACE

    no_a6_harness = _harness()
    without_a6 = no_a6_harness.decide(
        successor_compatibility=mismatch,
        checkpoints=_checkpoints(no_a6_harness),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=False,
        a6_interface=None,
    )
    assert without_a6.mode == "aborted"
    assert without_a6.reasons == (
        "a6.current_product_interface_unavailable",
        "compatibility.checkpoint_schema_version_mismatch",
    )
    assert without_a6.invoked_interface is None

    wrong_interface_harness = _harness()
    wrong_interface = wrong_interface_harness.decide(
        successor_compatibility=mismatch,
        checkpoints=_checkpoints(wrong_interface_harness),
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface="test_only_direct_replay_seam",
    )
    assert wrong_interface.mode == "aborted"
    assert "a6.current_product_interface_unavailable" in wrong_interface.reasons
    assert wrong_interface.invoked_interface is None


def test_gateway_watermark_mismatch_falls_back_without_transfer_or_fence() -> None:
    harness = _harness()
    checkpoints = list(_checkpoints(harness))
    checkpoints[0] = replace(checkpoints[0], committed_prefix_digest="sha256:other")
    owners_before = harness.stage_owners.copy()

    decision = harness.decide(
        successor_compatibility=harness.compatibility,
        checkpoints=checkpoints,
        installed_fence_participants=set(REQUIRED_FENCE_INSTALLERS),
        a6_available=True,
        a6_interface=A6_PRODUCT_INTERFACE,
    )

    assert decision.mode == "full_context_replay"
    assert decision.reasons == ("checkpoint.stage-1.watermark_digest_mismatch",)
    assert harness.stage_owners == owners_before
    assert harness.fence_installed is False
