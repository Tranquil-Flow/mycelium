from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from tests.a15_acceptance.validator import (
    DIRECT_PREREQUISITES,
    DIGEST_CLASSES,
    NODE_PROTOCOL,
    PROTOCOL,
    artifact_digest,
    seal_candidate,
    validate_executed_result,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return artifact_digest(label)


def _candidate() -> dict:
    source_commit = "1" * 40
    source_tree_digest = _digest("source-tree")
    artifacts = {
        f"executed/A{index}.json": f"sealed executed A{index} result"
        for index in range(2, 16)
    }
    nodes = []
    for index in range(2, 16):
        gate_id = f"A{index}"
        reference = f"executed/{gate_id}.json"
        nodes.append(
            {
                "protocol": NODE_PROTOCOL,
                "node_digest": _digest(f"unsealed-{gate_id}"),
                "gate_id": gate_id,
                "result_kind": "release_closure" if gate_id == "A15" else "physical_gate",
                "outcome": "verified",
                "source_commit": source_commit,
                "source_tree_digest": source_tree_digest,
                "tree_state": "clean",
                "provenance": "live",
                "subject": f"{gate_id}:executed_gate_result",
                "artifact_reference": reference,
                "artifact_digest": artifact_digest(artifacts[reference]),
                "dependency_gate_ids": sorted(DIRECT_PREREQUISITES[gate_id]),
                "dependency_digests": [],
                "verifier_policy_digest": _digest("verifier-policy"),
                "executed_at": "2026-08-18T09:00:00Z",
                "fresh_until": "2026-08-19T09:00:00Z",
                "contract_digest": _digest("contract-manifest"),
                "model_digest": _digest("model-revision"),
                "representation_digest": _digest("representation"),
                "runtime_digest": _digest("runtime-build"),
                "environment_digest": _digest("execution-environment"),
                "authority_generation": 7,
            }
        )

    exclusion = {
        "exclusion_digest": _digest("unsealed-exclusion"),
        "requirement_ids": ["optional_review_note"],
        "scope": "documentation_wording_only",
        "reason": "bounded synthetic validator fixture",
        "risk": "none_to_executed_authority",
        "compensating_behavior": "public_claim_withheld",
        "reviewer_identity": "fixture_reviewer",
        "signer_key_id": "fixture_owner_key",
        "issued_at": "2026-08-18T08:00:00Z",
        "expires_at": "2026-08-19T08:00:00Z",
        "signature": "pending",
    }
    candidate = {
        "protocol": PROTOCOL,
        "root": {
            "root_digest": _digest("unsealed-root"),
            "source_commit": source_commit,
            "source_tree_digest": source_tree_digest,
            "tree_state": "clean",
            "gate_commit_digests": {
                f"A{index}": f"{index:040x}" for index in range(3, 15)
            },
            "result_node_digests": {},
            "digest_class_roots": {
                digest_class: _digest(f"class-{digest_class}")
                for digest_class in DIGEST_CLASSES
            },
            "exclusion_digests": [],
            "reviewer_reproduction_digest": _digest("reviewer-reproduction"),
            "decision_generation": 1,
        },
        "nodes": nodes,
        "exclusions": [exclusion],
        "artifacts": artifacts,
    }
    seal_candidate(candidate)
    return candidate


def _node(candidate: dict, gate_id: str) -> dict:
    return next(node for node in candidate["nodes"] if node["gate_id"] == gate_id)


def test_valid_content_addressed_executed_result_is_accepted() -> None:
    result = validate_executed_result(_candidate(), now=NOW)
    assert result == {"accepted": True, "findings": [], "protocol": PROTOCOL}


@pytest.mark.parametrize(
    ("mutation", "required_finding"),
    [
        ("dirty_tree", "clean_tree_required"),
        ("missing_result_dependency", "dependency_digests_not_exact"),
        ("extra_result_dependency", "dependency_digests_not_exact"),
        ("provenance_substitution", "provenance_requirement_unsatisfied"),
        ("missing_sbom", "digest_class_set_mismatch"),
        ("unsigned_exclusion", "exclusion_signature_missing"),
        ("expired_evidence", "mandatory_evidence_expired"),
        ("handwritten_readiness", "unsupported_completion_claim"),
        ("inconsistent_artifact", "artifact_digest_inconsistent"),
        ("missing_reviewer", "reviewer_reproduction_missing"),
    ],
)
def test_real_candidate_mutations_are_rejected(
    mutation: str, required_finding: str
) -> None:
    candidate = copy.deepcopy(_candidate())
    if mutation == "dirty_tree":
        candidate["root"]["tree_state"] = "dirty"
        for node in candidate["nodes"]:
            node["tree_state"] = "dirty"
        seal_candidate(candidate)
    elif mutation == "missing_result_dependency":
        _node(candidate, "A12")["dependency_gate_ids"].remove("A4")
        seal_candidate(candidate)
    elif mutation == "extra_result_dependency":
        _node(candidate, "A14")["dependency_gate_ids"].append("A3")
        seal_candidate(candidate)
    elif mutation == "provenance_substitution":
        _node(candidate, "A12")["provenance"] = "replay"
        seal_candidate(candidate)
    elif mutation == "missing_sbom":
        del candidate["root"]["digest_class_roots"]["sbom"]
        seal_candidate(candidate)
    elif mutation == "unsigned_exclusion":
        del candidate["exclusions"][0]["signature"]
        seal_candidate(candidate)
    elif mutation == "expired_evidence":
        _node(candidate, "A12")["fresh_until"] = "2026-08-18T10:00:00Z"
        seal_candidate(candidate)
    elif mutation == "handwritten_readiness":
        candidate["release_ready"] = True
    elif mutation == "inconsistent_artifact":
        _node(candidate, "A12")["artifact_digest"] = _digest("altered-artifact")
        seal_candidate(candidate)
    elif mutation == "missing_reviewer":
        candidate["root"]["reviewer_reproduction_digest"] = None
        seal_candidate(candidate)
    else:  # pragma: no cover - the parameter list is closed above
        raise AssertionError(mutation)

    result = validate_executed_result(candidate, now=NOW)
    assert result["accepted"] is False
    assert required_finding in result["findings"]
