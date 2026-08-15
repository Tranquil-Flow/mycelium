from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from mycelium_member_models import (
    MemberModelInventoryError,
    create_member_model_inventory,
    inventory_entry_from_catalog_projection,
    reconcile_member_model_catalog,
    validate_member_model_inventory,
)
from mycelium_qualification.signing import generate_ed25519_signer


REVISION = "a" * 40
ARTIFACT = "sha256:" + "b" * 64
REPRESENTATION = "sha256:" + "c" * 64


def _projection(
    model_id: str = "Qwen/Qwen3-8B",
    *,
    artifact_digest: str = ARTIFACT,
) -> dict:
    return {
        "model_id": model_id,
        "revision": REVISION,
        "state": "compatible",
        "architecture": "Qwen3ForCausalLM",
        "adapter_id": "qwen3",
        "checkpoint_format": "safetensors_sharded",
        "quantization": "bfloat16",
        "num_layers": 36,
        "weight_bytes": 16_000_000_000,
        "exact_tensor_accounting": True,
        "required_file_count": 5,
        "present_file_count": 5,
        "reasons": [],
        "serving_representations": [
            {
                "quantization": "int8-weight-only",
                "quantizer": "mycelium.rowwise_symmetric_int8.v1",
                "runtime_dtype": "float32",
                "resident_weight_bytes": 8_000_000_000,
                "load_peak_weight_bytes": 8_500_000_000,
                "preparation_required": True,
                "representation_digest": REPRESENTATION,
            }
        ],
        "artifact_digest": artifact_digest,
        "snapshot_path": "/private/cache/must-not-escape",
    }


def _bundle(member_id: str = "node-peer", generation: int = 7):
    signer = generate_ed25519_signer(endpoint_id=f"member-model-{member_id}")
    bundle = create_member_model_inventory(
        member_id=member_id,
        membership_generation=generation,
        entries=[inventory_entry_from_catalog_projection(_projection())],
        observed_at_unix_ms=10_000,
        valid_until_unix_ms=20_000,
        signer=signer,
    )
    authority = {
        "member_id": member_id,
        "membership_generation": generation,
        "verification_key": signer.public_key_record(),
    }
    return bundle, authority


def test_signed_inventory_is_current_generation_bound_and_privacy_reduced() -> None:
    bundle, authority = _bundle()

    checked = validate_member_model_inventory(
        bundle,
        authority=authority,
        now_unix_ms=15_000,
    )

    encoded = repr(checked)
    assert "snapshot_path" not in encoded
    assert "/private/" not in encoded
    assert checked["statement"]["entries"][0]["model_id"] == "Qwen/Qwen3-8B"
    with pytest.raises(
        MemberModelInventoryError,
        match="member_model_inventory_generation_mismatch",
    ):
        validate_member_model_inventory(
            bundle,
            authority={**authority, "membership_generation": 8},
            now_unix_ms=15_000,
        )
    with pytest.raises(MemberModelInventoryError, match="member_model_inventory_stale"):
        validate_member_model_inventory(
            bundle,
            authority=authority,
            now_unix_ms=20_000,
        )


def test_catalog_diagnostics_are_normalized_to_safe_reason_codes() -> None:
    projection = _projection()
    projection["reasons"] = [
        "architecture_adapter:unsupported model_type: 'xlm-roberta'",
        "Missing weight artifact",
    ]

    entry = inventory_entry_from_catalog_projection(projection)

    assert entry["reasons"] == [
        "architecture_adapter:unsupported_model_type:_xlm-roberta",
        "missing_weight_artifact",
    ]


def test_tamper_and_unknown_fields_fail_signature_or_closed_shape() -> None:
    bundle, authority = _bundle()
    tampered = copy.deepcopy(bundle)
    tampered["statement"]["entries"][0]["weight_bytes"] += 1
    with pytest.raises(
        MemberModelInventoryError,
        match="member_model_inventory_signature_invalid",
    ):
        validate_member_model_inventory(
            tampered,
            authority=authority,
            now_unix_ms=15_000,
        )
    widened = copy.deepcopy(bundle)
    widened["statement"]["entries"][0]["cache_root"] = "/private/cache"
    with pytest.raises(
        MemberModelInventoryError,
        match="member_model_inventory_entry_invalid",
    ):
        validate_member_model_inventory(
            widened,
            authority=authority,
            now_unix_ms=15_000,
        )


def test_remote_only_identity_is_visible_but_not_compatible_or_selectable() -> None:
    bundle, authority = _bundle()

    catalog = reconcile_member_model_catalog(
        local_entries=(),
        inventories=(bundle,),
        authorities=(authority,),
        now_unix_ms=15_000,
        generation=9,
    )

    assert catalog["discovery"] == {
        "scope": "coordinator_and_members",
        "accepted_member_count": 1,
        "rejected_member_count": 0,
        "blockers": [],
    }
    assert len(catalog["entries"]) == 1
    entry = catalog["entries"][0]
    assert entry["state"] == "discovered"
    assert entry["metadata_reconciled"] is False
    assert entry["discovery_scope"] == ["member_inventory"]
    assert entry["discovery_blockers"] == [
        "owner_metadata_reconciliation_required"
    ]
    assert entry["route_ready"] is False


def test_matching_local_identity_reconciles_but_conflict_is_bounded() -> None:
    bundle, authority = _bundle()
    local_projection = _projection()
    local = SimpleNamespace(
        model_id=local_projection["model_id"],
        revision=local_projection["revision"],
        projection=lambda: copy.deepcopy(local_projection),
    )
    catalog = reconcile_member_model_catalog(
        local_entries=(local,),
        inventories=(bundle,),
        authorities=(authority,),
        now_unix_ms=15_000,
        generation=9,
    )
    assert catalog["entries"][0]["metadata_reconciled"] is True
    assert catalog["entries"][0]["discovered_member_count"] == 1

    conflicting = copy.deepcopy(bundle)
    conflicting["statement"]["entries"][0]["artifact_digest"] = (
        "sha256:" + "d" * 64
    )
    signer = generate_ed25519_signer(endpoint_id="member-model-node-conflict")
    conflicting = create_member_model_inventory(
        member_id="node-conflict",
        membership_generation=3,
        entries=[conflicting["statement"]["entries"][0]],
        observed_at_unix_ms=10_000,
        valid_until_unix_ms=20_000,
        signer=signer,
    )
    conflict_authority = {
        "member_id": "node-conflict",
        "membership_generation": 3,
        "verification_key": signer.public_key_record(),
    }
    conflicted = reconcile_member_model_catalog(
        local_entries=(local,),
        inventories=(conflicting,),
        authorities=(conflict_authority,),
        now_unix_ms=15_000,
        generation=10,
    )
    assert conflicted["entries"][0]["metadata_reconciled"] is False
    assert conflicted["entries"][0]["discovery_blockers"] == [
        "member_inventory_identity_conflict"
    ]
    assert conflicted["discovery"]["blockers"] == [
        "member_inventory_identity_conflict"
    ]
