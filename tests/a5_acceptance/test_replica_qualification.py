"""Deterministic tests for the A5 replica qualification leaf.

Spec: docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md
§2 (qualifier is the only authority that may mark a replica track qualified),
§10 (mixed generation, stale authority, incumbent preservation, fail-closed).
"""

from __future__ import annotations

from typing import Any

import pytest

from mycelium_replica_contracts import validate_replica_qualification
from mycelium_qualification.replica import (
    ReplicaQualificationInput,
    qualify_replica_track,
)


def _sha(letter: str) -> str:
    return "sha256:" + letter * 64


def _input(**overrides: Any) -> ReplicaQualificationInput:
    base: dict[str, Any] = dict(
        deployment_id="deployment-fixture",
        deployment_epoch=1,
        replica_group_id="group-000",
        placement_id="placement-replica",
        placement_ids=("placement-replica", "placement-stage-1"),
        track_id="track-fixture",
        traffic_fraction=0.5,
        qualifier_generation=3,
        issued_at_unix_ms=10_000,
        expires_at_unix_ms=600_000,
        evidence_bundle_digest=_sha("a"),
        load_proof_digest=_sha("b"),
        assignment_digest=_sha("e"),
        artifact_verification_digest=_sha("c"),
        parity_verified=True,
        startup_challenge_passed=True,
        memory_within_bounds=True,
        cleanup_within_bounds=True,
        directed_link_qualified=True,
        workload_envelope_digest=_sha("d"),
    )
    base.update(overrides)
    return ReplicaQualificationInput(**base)


def test_all_evidence_passing_yields_route_ready():
    document = qualify_replica_track(_input())
    assert document["route_ready"] is True
    assert document["rejected_reasons"] == []
    assert document["placement_ids"] == ["placement-replica", "placement-stage-1"]
    validate_replica_qualification(document)  # fail closed if invalid


def test_qualification_id_is_self_binding_digest():
    document = qualify_replica_track(_input())
    assert document["qualification_id"] == document["qualification_digest"]
    assert document["qualification_id"].startswith("sha256:")
    assert len(document["qualification_id"]) == 71


def test_same_input_same_digest_different_input_different_digest():
    a = qualify_replica_track(_input())
    b = qualify_replica_track(_input())
    c = qualify_replica_track(_input(deployment_epoch=2))
    assert a["qualification_id"] == b["qualification_id"]
    assert a["qualification_id"] != c["qualification_id"]


def test_parity_failure_fails_closed():
    document = qualify_replica_track(_input(parity_verified=False))
    assert document["route_ready"] is False
    assert document["rejected_reasons"] == ["parity_mismatch"]


def test_startup_challenge_failure_fails_closed():
    document = qualify_replica_track(_input(startup_challenge_passed=False))
    assert "startup_challenge_failed" in document["rejected_reasons"]
    assert document["route_ready"] is False


def test_memory_bounds_failure_fails_closed():
    document = qualify_replica_track(_input(memory_within_bounds=False))
    assert "memory_budget_exceeded" in document["rejected_reasons"]
    assert document["route_ready"] is False


def test_cleanup_bounds_failure_fails_closed():
    document = qualify_replica_track(_input(cleanup_within_bounds=False))
    assert "cleanup_budget_exceeded" in document["rejected_reasons"]
    assert document["route_ready"] is False


def test_directed_link_unqualified_fails_closed():
    document = qualify_replica_track(_input(directed_link_qualified=False))
    assert "directed_link_unqualified" in document["rejected_reasons"]
    assert document["route_ready"] is False


def test_zero_generation_is_stale_authority():
    document = qualify_replica_track(_input(qualifier_generation=0))
    assert document["route_ready"] is False
    assert "stale_authority" in document["rejected_reasons"]


def test_expiry_at_or_before_issuance_is_stale_authority():
    document = qualify_replica_track(
        _input(issued_at_unix_ms=10_000, expires_at_unix_ms=10_000)
    )
    assert document["route_ready"] is False
    assert "stale_authority" in document["rejected_reasons"]


def test_malformed_load_proof_digest_rejected_at_boundary():
    with pytest.raises(ValueError, match="malformed evidence digest"):
        qualify_replica_track(_input(load_proof_digest="not-a-digest"))


def test_malformed_artifact_digest_rejected_at_boundary():
    with pytest.raises(ValueError, match="malformed evidence digest"):
        qualify_replica_track(_input(artifact_verification_digest="sha256:XYZ"))


def test_multiple_failures_merge_sorted_unique():
    document = qualify_replica_track(
        _input(
            parity_verified=False,
            memory_within_bounds=False,
            qualifier_generation=0,
        )
    )
    assert document["rejected_reasons"] == sorted(
        set(document["rejected_reasons"])
    )
    assert {"parity_mismatch", "memory_budget_exceeded", "stale_authority"} == set(
        document["rejected_reasons"]
    )


def test_extra_rejections_merge_and_deduplicate():
    document = qualify_replica_track(
        _input(parity_verified=False),
        extra_rejections=["replica_loss", "parity_mismatch", "replica_loss"],
    )
    reasons = document["rejected_reasons"]
    assert reasons == sorted(set(reasons))
    assert reasons.count("replica_loss") == 1
    assert "parity_mismatch" in reasons
    assert document["route_ready"] is False


def test_extra_rejection_must_be_nonempty_string():
    with pytest.raises(ValueError, match="non-empty"):
        qualify_replica_track(_input(), extra_rejections=[""])


def test_failed_qualification_preserves_incumbent():
    """A failed decision never mutates a previously qualified document."""
    good = qualify_replica_track(_input())
    bad = qualify_replica_track(_input(parity_verified=False))
    assert good["route_ready"] is True
    assert bad["route_ready"] is False
    # the good document remains valid and unchanged after the bad decision
    assert qualify_replica_track(_input())["qualification_id"] == good["qualification_id"]
    validate_replica_qualification(good)


def test_output_always_validates():
    """Every decision output must pass the closed-shape validator."""
    cases = [
        _input(),
        _input(parity_verified=False),
        _input(cleanup_within_bounds=False),
        _input(qualifier_generation=0),
        _input(directed_link_qualified=False),
    ]
    for case in cases:
        validate_replica_qualification(qualify_replica_track(case))


def test_negative_gate_derivations_preserve_complete_track_binding():
    from mycelium_replica_contracts import compatibility_fixtures
    from scripts.run_a5_negative_illegal_gate import (
        _qualifier_leaf_evidence,
        _rejected_document,
    )

    original = compatibility_fixtures()["replica-qualification-v1.json"]
    rejected = _rejected_document(original)
    assert rejected["placement_ids"] == original["placement_ids"]
    assert rejected["route_ready"] is False
    leaf = _qualifier_leaf_evidence(original)
    assert leaf["parity_fail"]["route_ready"] is False
