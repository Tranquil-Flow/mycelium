# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from mycelium_a6_recovery import build_recovery_qualification

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


def test_unexecuted_qualification_does_not_manufacture_positive_subclaims() -> None:
    binding = {
        "deployment_id": "deployment-a6",
        "deployment_epoch": 1,
        "topology_generation": 1,
        "model_id": "model-a6",
        "model_revision": "immutable-revision",
        "representation_digest": SHA_C,
        "graph_digest": SHA_D,
    }
    compatibility = {
        "model_id": "model-a6",
        "model_revision": "immutable-revision",
        "representation_digest": SHA_C,
        "manifest_digest": SHA_A,
        "tokenizer_digest": SHA_D,
        "vocabulary_digest": SHA_A,
        "special_token_policy_digest": SHA_B,
        "architecture_digest": SHA_C,
        "context_policy_digest": SHA_D,
        "output_semantics_digest": SHA_A,
        "generation_settings_digest": SHA_B,
        "qualification_parity_policy_digest": SHA_C,
        "workload_envelope_digest": SHA_D,
    }
    qualification = build_recovery_qualification(
        binding,
        qualification_id="qualification-unexecuted",
        request_binding={
            "request_id": "request-a6",
            "source_attempt": 1,
            "committed_watermark": 2,
            "recovery_attempt": 0,
            "owner_qualification_digest": SHA_A,
            "checkpoint_digest": SHA_B,
        },
        successor_binding={
            "request_id": "request-a6",
            "request_attempt": 2,
            "path_id": "path-successor",
            "path_attempt": 1,
            "path_manifest_digest": SHA_C,
            "placement_ids": ["node-0", "node-2"],
        },
        compatibility=compatibility,
        compatible=False,
        issued_at_unix_ms=1_000,
        expiry_unix_ms=2_000,
        reason_codes=("physical_challenge_unexecuted",),
        physical_challenge="unexecuted",
    )

    assert qualification["freshness"] == "unverified"
    assert qualification["parity"] == "unverified"
    assert qualification["resource"] == "unverified"
    assert qualification["route"] == "unverified"
    assert qualification["fallback"] == "unverified"
