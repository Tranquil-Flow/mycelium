# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import hashlib

import pytest

from mycelium_qualification.signing import (
    build_ed25519_verifier,
    generate_ed25519_signer,
)
from mycelium_swarm_artifacts import (
    ACQUISITION_PROTOCOL,
    AVAILABILITY_BUNDLE_PROTOCOL,
    AVAILABILITY_PROTOCOL,
    CHUNK_RECEIPT_PROTOCOL,
    CHUNK_REQUEST_PROTOCOL,
    GRANT_PROTOCOL,
    MANIFEST_PROTOCOL,
    POLICY_PROTOCOL,
    SwarmArtifactContractError,
    canonical_digest,
    merkle_proofs,
    merkle_root,
    select_transfer_sources,
    sign_availability,
    sign_chunk_receipt,
    sign_chunk_request,
    sign_grant,
    source_ref,
    validate_acquisition_status,
    validate_availability,
    validate_availability_bundle,
    validate_chunk_receipt,
    validate_chunk_request,
    validate_grant,
    validate_policy,
    validate_stage_pack_manifest,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
REVISION = "e" * 40


def _content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _manifest() -> dict:
    contents = [b"abcd", b"efgh", b"ij"]
    digests = [_content_digest(content) for content in contents]
    proofs = merkle_proofs(digests)
    document = {
        "protocol": MANIFEST_PROTOCOL,
        "manifest_id": "manifest-1",
        "manifest_digest": DIGEST_A,
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": REVISION,
        "model_artifact_digest": DIGEST_B,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "bfloat16",
        "representation_digest": DIGEST_C,
        "owner_decision_digest": DIGEST_D,
        "feasibility_digest": DIGEST_A,
        "evidence_generation": 7,
        "assignment_id": "assignment-1",
        "assignment_digest": DIGEST_B,
        "graph_digest": DIGEST_C,
        "recipient_member_id": "member-3",
        "recipient_membership_generation": 9,
        "placement_id": "placement-2",
        "stage_id": "stage-2",
        "layer_start": 24,
        "layer_end_exclusive": 36,
        "component_scope": ["final_norm", "lm_head", "transformer_layers"],
        "tensor_scope_digest": DIGEST_D,
        "pack_format": "mycelium.stage_pack_stream.v1",
        "files": [
            {
                "relative_path": "layers/layers-24-35.safetensors",
                "components": ["transformer_layers"],
                "offset_bytes": 0,
                "size_bytes": 4,
                "content_digest": _content_digest(contents[0]),
            },
            {
                "relative_path": "final_norm.safetensors",
                "components": ["final_norm"],
                "offset_bytes": 4,
                "size_bytes": 4,
                "content_digest": _content_digest(contents[1]),
            },
            {
                "relative_path": "lm_head.safetensors",
                "components": ["lm_head"],
                "offset_bytes": 8,
                "size_bytes": 2,
                "content_digest": _content_digest(contents[2]),
            },
        ],
        "stage_pack_digest": _content_digest(b"".join(contents)),
        "chunk_size_bytes": 4,
        "total_size_bytes": 10,
        "merkle_root": merkle_root(digests),
        "chunks": [
            {
                "index": index,
                "offset_bytes": index * 4,
                "size_bytes": len(content),
                "content_digest": digests[index],
                "merkle_proof": list(proofs[index]),
            }
            for index, content in enumerate(contents)
        ],
        "issued_at_unix_ms": 1_000,
        "expires_at_unix_ms": 2_000,
        "owner_provenance": "owner-approved-exact-representation",
    }
    document["manifest_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "manifest_digest"}
    )
    return document


def _binding(manifest: dict) -> dict:
    fields = {
        "model_id",
        "model_revision",
        "representation_digest",
        "owner_decision_digest",
        "feasibility_digest",
        "evidence_generation",
        "assignment_id",
        "assignment_digest",
        "graph_digest",
        "recipient_member_id",
        "recipient_membership_generation",
        "placement_id",
        "stage_id",
        "layer_start",
        "layer_end_exclusive",
        "component_scope",
        "tensor_scope_digest",
    }
    return {field: copy.deepcopy(manifest[field]) for field in fields}


def _policy() -> dict:
    return {
        "protocol": POLICY_PROTOCOL,
        "chunk_size_bytes": 65_536,
        "maximum_sources": 3,
        "per_source_concurrency": 2,
        "aggregate_concurrency": 4,
        "maximum_retries_per_chunk": 3,
        "maximum_source_rotations": 4,
        "partial_state_ttl_seconds": 3_600,
        "disk_reserve_bytes": 1_073_741_824,
        "per_source_bytes_per_second": 10_000_000,
        "aggregate_bytes_per_second": 20_000_000,
        "serving_traffic_reserve_ratio": 0.4,
        "multi_source_threshold_bytes": 131_072,
        "minimum_predicted_improvement_ratio": 0.2,
        "allow_redundant_hedging": False,
        "thermal_classes_allowed": ["fair", "nominal"],
        "power_classes_allowed": ["battery_ok", "external_power"],
    }


def _availability(member: str, chunks: list[str], *, rate: int, priority: int) -> dict:
    return {
        "protocol": AVAILABILITY_PROTOCOL,
        "advertisement_id": f"advertisement-{member}",
        "source_member_id": member,
        "membership_generation": 3,
        "manifest_digest": DIGEST_A,
        "available_chunk_digests": sorted(chunks),
        "verified_bytes": len(chunks) * 65_536,
        "max_concurrent_streams": 2,
        "max_bytes_per_second": rate,
        "serving_priority": priority,
        "transfer_health": "healthy",
        "observed_at_unix_ms": 900,
        "valid_until_unix_ms": 1_100,
    }


def test_manifest_binds_exact_assignment_and_verifies_every_merkle_proof() -> None:
    manifest = _manifest()
    assert (
        validate_stage_pack_manifest(manifest, expected_binding=_binding(manifest))
        == manifest
    )

    drifted = _binding(manifest)
    drifted["representation_digest"] = DIGEST_B
    with pytest.raises(
        SwarmArtifactContractError, match="stage_pack_authorization_drift"
    ):
        validate_stage_pack_manifest(manifest, expected_binding=drifted)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda value: value.update(extra=True), "stage_pack_manifest_invalid"),
        (
            lambda value: value["chunks"][0].update(content_digest=DIGEST_A),
            "stage_pack_merkle_proof_invalid",
        ),
        (
            lambda value: value["chunks"][1].update(offset_bytes=5),
            "stage_pack_chunks_invalid",
        ),
        (
            lambda value: value.update(
                component_scope=["transformer_layers", "unassigned_head"]
            ),
            "stage_pack_component_scope_invalid",
        ),
        (
            lambda value: value["files"][1].update(relative_path="../escape"),
            "stage_pack_files_invalid",
        ),
        (
            lambda value: value["files"][1].update(offset_bytes=5),
            "stage_pack_files_invalid",
        ),
    ],
)
def test_manifest_rejects_unknown_fields_corruption_gaps_and_scope_widening(
    mutation, code: str
) -> None:
    document = _manifest()
    mutation(document)
    with pytest.raises(SwarmArtifactContractError, match=code):
        validate_stage_pack_manifest(document)


def test_signed_availability_is_fresh_generation_and_manifest_bound() -> None:
    signer = generate_ed25519_signer(endpoint_id="source-endpoint")
    verifier = build_ed25519_verifier([signer.public_key_record()])
    statement = _availability("member-a", [DIGEST_A], rate=10_000, priority=2)
    signed = sign_availability(statement, signer)
    assert (
        validate_availability(
            signed,
            verifier=verifier,
            now_unix_ms=1_000,
            expected_manifest_digest=DIGEST_A,
            expected_membership_generation=3,
        )["source_member_id"]
        == "member-a"
    )

    stale = copy.deepcopy(signed)
    stale["valid_until_unix_ms"] = 999
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_ineligible"
    ):
        validate_availability(
            stale,
            verifier=verifier,
            now_unix_ms=1_000,
            expected_manifest_digest=DIGEST_A,
            expected_membership_generation=3,
        )


def test_availability_bundle_is_dynamic_member_generation_bound_and_closed() -> None:
    signer = generate_ed25519_signer(endpoint_id="source-endpoint")
    verifier = build_ed25519_verifier([signer.public_key_record()])
    signed = sign_availability(
        _availability("member-a", [DIGEST_A], rate=10_000, priority=2), signer
    )
    bundle = {
        "protocol": AVAILABILITY_BUNDLE_PROTOCOL,
        "source_member_id": "member-a",
        "membership_generation": 3,
        "advertisements": [signed],
        "published_at_unix_ms": 950,
    }
    checked = validate_availability_bundle(
        bundle,
        verifier=verifier,
        now_unix_ms=1_000,
        expected_source_member_id="member-a",
        expected_membership_generation=3,
    )
    assert checked["advertisements"] == [signed]

    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_bundle_invalid"
    ):
        validate_availability_bundle(
            {**bundle, "membership_generation": 4},
            verifier=verifier,
            now_unix_ms=1_000,
            expected_source_member_id="member-a",
            expected_membership_generation=3,
        )
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_bundle_invalid"
    ):
        validate_availability_bundle(
            {**bundle, "private_address": "100.64.0.1"},
            verifier=verifier,
            now_unix_ms=1_000,
            expected_source_member_id="member-a",
            expected_membership_generation=3,
        )


def test_grant_rejects_replay_generation_and_representation_substitution() -> None:
    signer = generate_ed25519_signer(endpoint_id="provisioner")
    verifier = build_ed25519_verifier([signer.public_key_record()])
    statement = {
        "protocol": GRANT_PROTOCOL,
        "grant_id": "grant-1",
        "nonce": "nonce-1",
        "provisioner_generation": 8,
        "recipient_member_id": "member-3",
        "recipient_membership_generation": 9,
        "manifest_digest": DIGEST_A,
        "assignment_digest": DIGEST_B,
        "representation_digest": DIGEST_C,
        "feasibility_digest": DIGEST_D,
        "allowed_chunk_digests": [DIGEST_A, DIGEST_B],
        "maximum_total_bytes": 131_072,
        "maximum_concurrency": 2,
        "maximum_bytes_per_second": 1_000_000,
        "authorized_source_member_ids": ["member-a", "member-b"],
        "origin_fallback_allowed": False,
        "issued_at_unix_ms": 900,
        "not_before_unix_ms": 950,
        "expires_at_unix_ms": 1_100,
    }
    signed = sign_grant(statement, signer)
    arguments = {
        "verifier": verifier,
        "now_unix_ms": 1_000,
        "expected_recipient_member_id": "member-3",
        "expected_recipient_membership_generation": 9,
        "expected_provisioner_generation": 8,
        "expected_manifest_digest": DIGEST_A,
        "expected_assignment_digest": DIGEST_B,
        "expected_representation_digest": DIGEST_C,
        "expected_feasibility_digest": DIGEST_D,
    }
    assert validate_grant(signed, **arguments)["grant_id"] == "grant-1"
    with pytest.raises(SwarmArtifactContractError, match="artifact_grant_ineligible"):
        validate_grant(signed, **{**arguments, "expected_provisioner_generation": 9})
    with pytest.raises(SwarmArtifactContractError, match="artifact_grant_ineligible"):
        validate_grant(
            signed, **{**arguments, "expected_representation_digest": DIGEST_A}
        )


def test_recipient_signed_chunk_request_and_source_receipt_bind_exact_range() -> None:
    manifest = _manifest()
    provisioner = generate_ed25519_signer(endpoint_id="provisioner")
    recipient = generate_ed25519_signer(endpoint_id="recipient-member-3")
    source = generate_ed25519_signer(endpoint_id="source-member-a")
    provisioner_verifier = build_ed25519_verifier([provisioner.public_key_record()])
    recipient_verifier = build_ed25519_verifier([recipient.public_key_record()])
    source_verifier = build_ed25519_verifier([source.public_key_record()])
    chunk = manifest["chunks"][0]
    grant = sign_grant(
        {
            "protocol": GRANT_PROTOCOL,
            "grant_id": "grant-range-1",
            "nonce": "grant-nonce-1",
            "provisioner_generation": 11,
            "recipient_member_id": "member-3",
            "recipient_membership_generation": 9,
            "manifest_digest": manifest["manifest_digest"],
            "assignment_digest": manifest["assignment_digest"],
            "representation_digest": manifest["representation_digest"],
            "feasibility_digest": manifest["feasibility_digest"],
            "allowed_chunk_digests": sorted(
                item["content_digest"] for item in manifest["chunks"]
            ),
            "maximum_total_bytes": manifest["total_size_bytes"],
            "maximum_concurrency": 2,
            "maximum_bytes_per_second": 1_000_000,
            "authorized_source_member_ids": ["member-a"],
            "origin_fallback_allowed": False,
            "issued_at_unix_ms": 1_100,
            "not_before_unix_ms": 1_200,
            "expires_at_unix_ms": 1_900,
        },
        provisioner,
    )
    request = sign_chunk_request(
        {
            "protocol": CHUNK_REQUEST_PROTOCOL,
            "request_id": "request-range-1",
            "request_nonce": "request-nonce-1",
            "grant": grant,
            "source_member_id": "member-a",
            "recipient_member_id": "member-3",
            "recipient_membership_generation": 9,
            "manifest_digest": manifest["manifest_digest"],
            "chunk_digest": chunk["content_digest"],
            "offset_bytes": 1,
            "length_bytes": 2,
            "issued_at_unix_ms": 1_400,
            "expires_at_unix_ms": 1_700,
        },
        recipient,
    )
    assert (
        validate_chunk_request(
            request,
            provisioner_verifier=provisioner_verifier,
            recipient_verifier=recipient_verifier,
            now_unix_ms=1_500,
            expected_source_member_id="member-a",
            expected_manifest=manifest,
            expected_provisioner_generation=11,
        )
        == request
    )

    availability = sign_availability(
        {
            "protocol": AVAILABILITY_PROTOCOL,
            "advertisement_id": "advertisement-member-a",
            "source_member_id": "member-a",
            "membership_generation": 3,
            "manifest_digest": manifest["manifest_digest"],
            "available_chunk_digests": sorted(
                item["content_digest"] for item in manifest["chunks"]
            ),
            "verified_bytes": manifest["total_size_bytes"],
            "max_concurrent_streams": 2,
            "max_bytes_per_second": 1_000_000,
            "serving_priority": 1,
            "transfer_health": "healthy",
            "observed_at_unix_ms": 1_300,
            "valid_until_unix_ms": 1_800,
        },
        source,
    )
    returned = b"bc"
    receipt = sign_chunk_receipt(
        {
            "protocol": CHUNK_RECEIPT_PROTOCOL,
            "request_id": request["request_id"],
            "source_member_id": "member-a",
            "source_membership_generation": 3,
            "recipient_member_id": "member-3",
            "manifest_digest": manifest["manifest_digest"],
            "chunk_digest": chunk["content_digest"],
            "offset_bytes": 1,
            "length_bytes": 2,
            "range_content_digest": _content_digest(returned),
            "advertisement_id": availability["advertisement_id"],
            "responded_at_unix_ms": 1_500,
        },
        source,
    )
    assert (
        validate_chunk_receipt(
            receipt,
            source_verifier=source_verifier,
            request=request,
            availability=availability,
            returned_bytes=returned,
        )
        == receipt
    )

    widened = copy.deepcopy(request)
    widened["length_bytes"] = chunk["size_bytes"]
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_request_ineligible"
    ):
        validate_chunk_request(
            widened,
            provisioner_verifier=provisioner_verifier,
            recipient_verifier=recipient_verifier,
            now_unix_ms=1_500,
            expected_source_member_id="member-a",
            expected_manifest=manifest,
            expected_provisioner_generation=11,
        )

    substituted_receipt = copy.deepcopy(receipt)
    substituted_receipt["range_content_digest"] = DIGEST_A
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_receipt_mismatch"
    ):
        validate_chunk_receipt(
            substituted_receipt,
            source_verifier=source_verifier,
            request=request,
            availability=availability,
            returned_bytes=returned,
        )


def test_policy_is_closed_bounded_and_reserves_serving_capacity() -> None:
    assert validate_policy(_policy())["maximum_sources"] == 3
    invalid = _policy()
    invalid["serving_traffic_reserve_ratio"] = 1.0
    with pytest.raises(SwarmArtifactContractError, match="artifact_policy_invalid"):
        validate_policy(invalid)


def test_source_selection_is_dynamic_bounded_and_avoids_duplicate_chunks() -> None:
    sources = [
        _availability("member-a", [DIGEST_A, DIGEST_B], rate=20_000, priority=2),
        _availability("member-b", [DIGEST_B, DIGEST_C], rate=15_000, priority=1),
        _availability("member-c", [DIGEST_C], rate=5_000, priority=0),
    ]
    selected = select_transfer_sources(
        missing_chunk_digests=[DIGEST_A, DIGEST_B, DIGEST_C],
        advertisements=sources,
        policy=_policy(),
        predicted_improvement_ratio=0.3,
        serving_reserve_satisfied=True,
    )
    assigned = [digest for chunks in selected.values() for digest in chunks]
    assert sorted(assigned) == [DIGEST_A, DIGEST_B, DIGEST_C]
    assert len(assigned) == len(set(assigned))
    assert set(selected) == {"member-a", "member-b"}

    single = select_transfer_sources(
        missing_chunk_digests=[DIGEST_A, DIGEST_B, DIGEST_C],
        advertisements=sources,
        policy=_policy(),
        predicted_improvement_ratio=0.3,
        serving_reserve_satisfied=False,
    )
    assert set(single) == {"member-a"}
    assert single["member-a"] == (DIGEST_A, DIGEST_B)


def test_multi_source_selection_uses_distinct_complete_replicas() -> None:
    sources = [
        _availability(
            "member-a", [DIGEST_A, DIGEST_B, DIGEST_C], rate=20_000, priority=2
        ),
        _availability(
            "member-b", [DIGEST_A, DIGEST_B, DIGEST_C], rate=15_000, priority=1
        ),
    ]

    selected = select_transfer_sources(
        missing_chunk_digests=[DIGEST_A, DIGEST_B, DIGEST_C],
        advertisements=sources,
        policy=_policy(),
        predicted_improvement_ratio=0.3,
        serving_reserve_satisfied=True,
    )

    assert set(selected) == {"member-a", "member-b"}
    assigned = [digest for chunks in selected.values() for digest in chunks]
    assert sorted(assigned) == [DIGEST_A, DIGEST_B, DIGEST_C]
    assert len(assigned) == len(set(assigned))


def _status() -> dict:
    return {
        "protocol": ACQUISITION_PROTOCOL,
        "generation": 5,
        "acquisition_id": "acquisition-1",
        "state": "transferring",
        "phase": "transferring",
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": REVISION,
        "representation": "bfloat16",
        "assignment_id": "assignment-1",
        "placement_id": "placement-2",
        "stage_id": "stage-2",
        "layer_start": 24,
        "layer_end_exclusive": 36,
        "total_bytes": 300,
        "cached_verified_bytes": 100,
        "transferred_verified_bytes": 120,
        "missing_bytes": 80,
        "quarantined_bytes": 0,
        "duplicate_bytes_prevented": 40,
        "eligible_source_count": 3,
        "active_source_count": 2,
        "sources": [
            {
                "source_ref": "source-000000000001",
                "state": "active",
                "verified_bytes": 70,
            },
            {
                "source_ref": "source-000000000002",
                "state": "active",
                "verified_bytes": 50,
            },
        ],
        "origin_bytes": 0,
        "aggregate_bytes_per_second": 50.0,
        "eta_seconds": 1.6,
        "chunk_count": 3,
        "verified_chunk_count": 2,
        "resumed_chunk_count": 1,
        "source_rotation_count": 0,
        "manifest_digest": DIGEST_A,
        "assignment_digest": DIGEST_B,
        "representation_digest": DIGEST_C,
        "feasibility_digest": DIGEST_D,
        "evidence_generation": 7,
        "promotion_digest": None,
        "reason_code": None,
        "retryable": False,
        "started_at_unix_ms": 1_000,
        "updated_at_unix_ms": 1_100,
        "terminal_at_unix_ms": None,
    }


def test_public_status_is_accounted_privacy_reduced_and_strict() -> None:
    status = _status()
    assert validate_acquisition_status(status) == status
    assert source_ref("member-a", "acquisition-1").startswith("source-")
    assert "member-a" not in source_ref("member-a", "acquisition-1")

    private = copy.deepcopy(status)
    private["sources"][0]["endpoint"] = "10.0.0.1"
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_acquisition_status_invalid"
    ):
        validate_acquisition_status(private)

    inconsistent = copy.deepcopy(status)
    inconsistent["missing_bytes"] = 79
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_acquisition_status_invalid"
    ):
        validate_acquisition_status(inconsistent)


def test_ready_and_failed_terminal_states_are_mutually_truthful() -> None:
    ready = _status()
    ready.update(
        state="ready",
        phase=None,
        cached_verified_bytes=100,
        transferred_verified_bytes=200,
        missing_bytes=0,
        sources=[
            {
                "source_ref": "source-000000000001",
                "state": "rotated",
                "verified_bytes": 120,
            },
            {
                "source_ref": "source-000000000002",
                "state": "rotated",
                "verified_bytes": 80,
            },
        ],
        active_source_count=0,
        verified_chunk_count=3,
        eta_seconds=0.0,
        promotion_digest=DIGEST_A,
        terminal_at_unix_ms=1_200,
    )
    assert validate_acquisition_status(ready)["state"] == "ready"

    invalid = copy.deepcopy(ready)
    invalid["promotion_digest"] = None
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_acquisition_status_invalid"
    ):
        validate_acquisition_status(invalid)


# A chunk request is signed on the recipient's clock and verified on the
# source member's host: allow the source to be up to MAX_CLOCK_SKEW_SECONDS
# behind the recipient, never extend the request's expiry.
_SKEW_NOW_MS = 1_000_000


def _skewed_chunk_request(
    *, issued_ms: int, expires_ms: int, grant_not_before_ms: int | None = None
) -> tuple:
    not_before = (
        _SKEW_NOW_MS - 50_000 if grant_not_before_ms is None else grant_not_before_ms
    )
    manifest = _manifest()
    provisioner = generate_ed25519_signer(endpoint_id="provisioner")
    recipient = generate_ed25519_signer(endpoint_id="recipient-member-3")
    grant = sign_grant(
        {
            "protocol": GRANT_PROTOCOL,
            "grant_id": "grant-skew-1",
            "nonce": "grant-skew-nonce-1",
            "provisioner_generation": 11,
            "recipient_member_id": "member-3",
            "recipient_membership_generation": 9,
            "manifest_digest": manifest["manifest_digest"],
            "assignment_digest": manifest["assignment_digest"],
            "representation_digest": manifest["representation_digest"],
            "feasibility_digest": manifest["feasibility_digest"],
            "allowed_chunk_digests": sorted(
                item["content_digest"] for item in manifest["chunks"]
            ),
            "maximum_total_bytes": manifest["total_size_bytes"],
            "maximum_concurrency": 2,
            "maximum_bytes_per_second": 1_000_000,
            "authorized_source_member_ids": ["member-a"],
            "origin_fallback_allowed": False,
            "issued_at_unix_ms": not_before - 50_000,
            "not_before_unix_ms": not_before,
            "expires_at_unix_ms": _SKEW_NOW_MS + 600_000,
        },
        provisioner,
    )
    request = sign_chunk_request(
        {
            "protocol": CHUNK_REQUEST_PROTOCOL,
            "request_id": "request-skew-1",
            "request_nonce": "request-skew-nonce-1",
            "grant": grant,
            "source_member_id": "member-a",
            "recipient_member_id": "member-3",
            "recipient_membership_generation": 9,
            "manifest_digest": manifest["manifest_digest"],
            "chunk_digest": manifest["chunks"][0]["content_digest"],
            "offset_bytes": 0,
            "length_bytes": 2,
            "issued_at_unix_ms": issued_ms,
            "expires_at_unix_ms": expires_ms,
        },
        recipient,
    )
    arguments = {
        "provisioner_verifier": build_ed25519_verifier(
            [provisioner.public_key_record()]
        ),
        "recipient_verifier": build_ed25519_verifier([recipient.public_key_record()]),
        "now_unix_ms": _SKEW_NOW_MS,
        "expected_source_member_id": "member-a",
        "expected_manifest": manifest,
        "expected_provisioner_generation": 11,
    }
    return request, arguments


@pytest.mark.parametrize("ahead_ms", [200, 29_000])
def test_chunk_request_from_a_recipient_clock_up_to_30_s_ahead_is_accepted(
    ahead_ms: int,
) -> None:
    issued = _SKEW_NOW_MS + ahead_ms
    request, arguments = _skewed_chunk_request(
        issued_ms=issued, expires_ms=issued + 60_000
    )
    assert validate_chunk_request(request, **arguments) == request


def test_chunk_request_from_a_recipient_clock_31_s_ahead_is_refused() -> None:
    issued = _SKEW_NOW_MS + 31_000
    request, arguments = _skewed_chunk_request(
        issued_ms=issued, expires_ms=issued + 60_000
    )
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_request_ineligible"
    ):
        validate_chunk_request(request, **arguments)


def test_chunk_request_expiry_gets_no_clock_skew_allowance() -> None:
    request, arguments = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS - 60_000, expires_ms=_SKEW_NOW_MS - 1
    )
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_request_ineligible"
    ):
        validate_chunk_request(request, **arguments)


def test_chunk_request_clock_allowance_is_the_membership_allowance() -> None:
    import mycelium_swarm_artifacts
    from mycelium_membership import MAX_CLOCK_SKEW_SECONDS

    assert mycelium_swarm_artifacts.MAX_CLOCK_SKEW_MS == round(
        MAX_CLOCK_SKEW_SECONDS * 1000
    )


# A grant is issued with not_before = now on the provisioner's host and
# verified on the recipient (and on each source member): allow the verifier to
# be up to MAX_CLOCK_SKEW_SECONDS behind the provisioner, never extend expiry.
_GRANT_ISSUED_MS = 2_000_000
_GRANT_EXPIRES_MS = _GRANT_ISSUED_MS + 600_000


def _issued_now_grant() -> tuple:
    signer = generate_ed25519_signer(endpoint_id="provisioner")
    signed = sign_grant(
        {
            "protocol": GRANT_PROTOCOL,
            "grant_id": "grant-issued-now-1",
            "nonce": "grant-issued-now-nonce-1",
            "provisioner_generation": 8,
            "recipient_member_id": "member-3",
            "recipient_membership_generation": 9,
            "manifest_digest": DIGEST_A,
            "assignment_digest": DIGEST_B,
            "representation_digest": DIGEST_C,
            "feasibility_digest": DIGEST_D,
            "allowed_chunk_digests": [DIGEST_A, DIGEST_B],
            "maximum_total_bytes": 131_072,
            "maximum_concurrency": 2,
            "maximum_bytes_per_second": 1_000_000,
            "authorized_source_member_ids": ["member-a"],
            "origin_fallback_allowed": False,
            "issued_at_unix_ms": _GRANT_ISSUED_MS,
            "not_before_unix_ms": _GRANT_ISSUED_MS,
            "expires_at_unix_ms": _GRANT_EXPIRES_MS,
        },
        signer,
    )
    arguments = {
        "verifier": build_ed25519_verifier([signer.public_key_record()]),
        "expected_recipient_member_id": "member-3",
        "expected_recipient_membership_generation": 9,
        "expected_provisioner_generation": 8,
        "expected_manifest_digest": DIGEST_A,
        "expected_assignment_digest": DIGEST_B,
        "expected_representation_digest": DIGEST_C,
        "expected_feasibility_digest": DIGEST_D,
    }
    return signed, arguments


@pytest.mark.parametrize("behind_ms", [200, 29_000])
def test_grant_on_a_verifier_clock_up_to_30_s_behind_the_provisioner_is_accepted(
    behind_ms: int,
) -> None:
    signed, arguments = _issued_now_grant()
    checked = validate_grant(
        signed, now_unix_ms=_GRANT_ISSUED_MS - behind_ms, **arguments
    )
    assert checked == signed


def test_grant_on_a_verifier_clock_31_s_behind_the_provisioner_is_refused() -> None:
    signed, arguments = _issued_now_grant()
    with pytest.raises(SwarmArtifactContractError, match="artifact_grant_ineligible"):
        validate_grant(signed, now_unix_ms=_GRANT_ISSUED_MS - 31_000, **arguments)


def test_grant_expiry_gets_no_clock_skew_allowance() -> None:
    signed, arguments = _issued_now_grant()
    assert validate_grant(signed, now_unix_ms=_GRANT_EXPIRES_MS, **arguments)
    with pytest.raises(SwarmArtifactContractError, match="artifact_grant_ineligible"):
        validate_grant(signed, now_unix_ms=_GRANT_EXPIRES_MS + 1, **arguments)


@pytest.mark.parametrize("behind_ms", [200, 29_000])
def test_chunk_request_on_a_source_clock_behind_the_provisioner_is_accepted(
    behind_ms: int,
) -> None:
    # The grant inside the request was issued "now" on the provisioner; the
    # source member's clock is behind both the provisioner and the recipient.
    request, arguments = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS + behind_ms,
        expires_ms=_SKEW_NOW_MS + behind_ms + 60_000,
        grant_not_before_ms=_SKEW_NOW_MS + behind_ms,
    )
    assert validate_chunk_request(request, **arguments) == request


def test_chunk_request_on_a_source_clock_31_s_behind_the_provisioner_is_refused() -> None:
    request, arguments = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS + 29_000,
        expires_ms=_SKEW_NOW_MS + 89_000,
        grant_not_before_ms=_SKEW_NOW_MS + 31_000,
    )
    with pytest.raises(SwarmArtifactContractError, match="artifact_grant_ineligible"):
        validate_chunk_request(request, **arguments)


# Availability is signed on the source member's clock and checked by the
# provisioner and the recipient; a receipt's responded_at is the source's
# clock and is compared with the recipient's request issued_at. Allow up to
# MAX_CLOCK_SKEW_SECONDS between those hosts; expiry gets no allowance.
_SOURCE_NOW_MS = 3_000_000


def _source_availability(**overrides) -> tuple:
    signer = generate_ed25519_signer(endpoint_id="source-endpoint")
    statement = {
        **_availability("member-a", [DIGEST_A], rate=10_000, priority=2),
        "observed_at_unix_ms": _SOURCE_NOW_MS,
        "valid_until_unix_ms": _SOURCE_NOW_MS + 600_000,
        **overrides,
    }
    return (
        sign_availability(statement, signer),
        build_ed25519_verifier([signer.public_key_record()]),
    )


def _check_availability(signed, verifier, now: int) -> dict:
    return validate_availability(
        signed,
        verifier=verifier,
        now_unix_ms=now,
        expected_manifest_digest=DIGEST_A,
        expected_membership_generation=3,
    )


def _check_bundle(signed, verifier, now: int) -> dict:
    bundle = {
        "protocol": AVAILABILITY_BUNDLE_PROTOCOL,
        "source_member_id": "member-a",
        "membership_generation": 3,
        "advertisements": [signed],
        "published_at_unix_ms": _SOURCE_NOW_MS,
    }
    return validate_availability_bundle(
        bundle,
        verifier=verifier,
        now_unix_ms=now,
        expected_source_member_id="member-a",
        expected_membership_generation=3,
    )


@pytest.mark.parametrize("behind_ms", [200, 29_000])
def test_availability_on_a_verifier_clock_up_to_30_s_behind_the_source_is_accepted(
    behind_ms: int,
) -> None:
    signed, verifier = _source_availability()
    assert _check_availability(signed, verifier, _SOURCE_NOW_MS - behind_ms) == signed
    bundle = _check_bundle(signed, verifier, _SOURCE_NOW_MS - behind_ms)
    assert bundle["advertisements"] == [signed]


def test_availability_on_a_verifier_clock_31_s_behind_the_source_is_refused() -> None:
    signed, verifier = _source_availability()
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_ineligible"
    ):
        _check_availability(signed, verifier, _SOURCE_NOW_MS - 31_000)
    early, early_verifier = _source_availability(
        observed_at_unix_ms=_SOURCE_NOW_MS - 60_000
    )
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_bundle_invalid"
    ):
        # The bundle itself was published 31 s in the verifier's future.
        _check_bundle(early, early_verifier, _SOURCE_NOW_MS - 31_000)


def test_availability_expiry_gets_no_clock_skew_allowance() -> None:
    signed, verifier = _source_availability()
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_availability_ineligible"
    ):
        _check_availability(signed, verifier, _SOURCE_NOW_MS + 600_001)


def _receipt_for(request: dict, *, responded_ms: int) -> tuple:
    source = generate_ed25519_signer(endpoint_id="source-member-a")
    availability = sign_availability(
        {
            "protocol": AVAILABILITY_PROTOCOL,
            "advertisement_id": "advertisement-member-a",
            "source_member_id": "member-a",
            "membership_generation": 3,
            "manifest_digest": request["manifest_digest"],
            "available_chunk_digests": [request["chunk_digest"]],
            "verified_bytes": 65_536,
            "max_concurrent_streams": 2,
            "max_bytes_per_second": 1_000_000,
            "serving_priority": 1,
            "transfer_health": "healthy",
            "observed_at_unix_ms": _SKEW_NOW_MS - 120_000,
            "valid_until_unix_ms": _SKEW_NOW_MS + 600_000,
        },
        source,
    )
    returned = b"ab"
    receipt = sign_chunk_receipt(
        {
            "protocol": CHUNK_RECEIPT_PROTOCOL,
            "request_id": request["request_id"],
            "source_member_id": "member-a",
            "source_membership_generation": 3,
            "recipient_member_id": "member-3",
            "manifest_digest": request["manifest_digest"],
            "chunk_digest": request["chunk_digest"],
            "offset_bytes": request["offset_bytes"],
            "length_bytes": request["length_bytes"],
            "range_content_digest": _content_digest(returned),
            "advertisement_id": "advertisement-member-a",
            "responded_at_unix_ms": responded_ms,
        },
        source,
    )
    arguments = {
        "source_verifier": build_ed25519_verifier([source.public_key_record()]),
        "request": request,
        "availability": availability,
        "returned_bytes": returned,
    }
    return receipt, arguments


@pytest.mark.parametrize("behind_ms", [200, 29_000])
def test_receipt_from_a_source_clock_up_to_30_s_behind_the_recipient_is_accepted(
    behind_ms: int,
) -> None:
    request, _ = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS, expires_ms=_SKEW_NOW_MS + 60_000
    )
    receipt, arguments = _receipt_for(request, responded_ms=_SKEW_NOW_MS - behind_ms)
    assert validate_chunk_receipt(receipt, **arguments) == receipt


def test_receipt_from_a_source_clock_31_s_behind_the_recipient_is_refused() -> None:
    request, _ = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS, expires_ms=_SKEW_NOW_MS + 60_000
    )
    receipt, arguments = _receipt_for(request, responded_ms=_SKEW_NOW_MS - 31_000)
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_receipt_mismatch"
    ):
        validate_chunk_receipt(receipt, **arguments)


def test_receipt_after_the_request_expiry_gets_no_clock_skew_allowance() -> None:
    request, _ = _skewed_chunk_request(
        issued_ms=_SKEW_NOW_MS, expires_ms=_SKEW_NOW_MS + 60_000
    )
    receipt, arguments = _receipt_for(request, responded_ms=_SKEW_NOW_MS + 60_001)
    with pytest.raises(
        SwarmArtifactContractError, match="artifact_chunk_receipt_mismatch"
    ):
        validate_chunk_receipt(receipt, **arguments)

