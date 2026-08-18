"""Design-only validator for synthetic A15 executed-result candidates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


PROTOCOL = "mycelium.executed_result_graph.v1"
NODE_PROTOCOL = "mycelium.executed_gate_result.v1"
DIGEST_CLASSES = {
    "test",
    "audit",
    "physical",
    "browser",
    "model",
    "contract",
    "package",
    "sbom",
}
DIRECT_PREREQUISITES = {
    "A2": set(),
    "A3": {"A2"},
    "A4": {"A3"},
    "A5": {"A4"},
    "A6": {"A4"},
    "A7": {"A6"},
    "A8": {"A3"},
    "A9": {"A8"},
    "A10": {"A4"},
    "A11": {"A10"},
    "A12": {"A4", "A9"},
    "A13": {"A12"},
    "A14": {"A8"},
    "A15": {f"A{index}" for index in range(3, 15)},
}
TOP_FIELDS = {"protocol", "root", "nodes", "exclusions", "artifacts"}
NODE_FIELDS = {
    "protocol",
    "node_digest",
    "gate_id",
    "result_kind",
    "outcome",
    "source_commit",
    "source_tree_digest",
    "tree_state",
    "provenance",
    "subject",
    "artifact_reference",
    "artifact_digest",
    "dependency_gate_ids",
    "dependency_digests",
    "verifier_policy_digest",
    "executed_at",
    "fresh_until",
    "contract_digest",
    "model_digest",
    "representation_digest",
    "runtime_digest",
    "environment_digest",
    "authority_generation",
}
ROOT_FIELDS = {
    "root_digest",
    "source_commit",
    "source_tree_digest",
    "tree_state",
    "gate_commit_digests",
    "result_node_digests",
    "digest_class_roots",
    "exclusion_digests",
    "reviewer_reproduction_digest",
    "decision_generation",
}
EXCLUSION_FIELDS = {
    "exclusion_digest",
    "requirement_ids",
    "scope",
    "reason",
    "risk",
    "compensating_behavior",
    "reviewer_identity",
    "signer_key_id",
    "issued_at",
    "expires_at",
    "signature",
}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def artifact_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_digest(value: dict[str, Any], omitted: set[str]) -> str:
    return canonical_digest({key: item for key, item in value.items() if key not in omitted})


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _has_boolean(value: object) -> bool:
    if type(value) is bool:
        return True
    if isinstance(value, dict):
        return any(_has_boolean(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_boolean(item) for item in value)
    return False


def seal_candidate(candidate: dict[str, Any]) -> None:
    """Seal a synthetic candidate after a deliberate test mutation."""
    by_gate = {node["gate_id"]: node for node in candidate["nodes"]}
    for gate_id in sorted(by_gate, key=lambda item: int(item[1:])):
        node = by_gate[gate_id]
        node["dependency_digests"] = [
            by_gate[dependency]["node_digest"]
            for dependency in node["dependency_gate_ids"]
        ]
        node["node_digest"] = _content_digest(node, {"node_digest"})

    for exclusion in candidate["exclusions"]:
        exclusion["exclusion_digest"] = _content_digest(
            exclusion, {"exclusion_digest", "signature"}
        )
        if exclusion.get("signature") is not None:
            exclusion["signature"] = (
                "fixture-signature:"
                + hashlib.sha256(
                    (
                        exclusion["signer_key_id"]
                        + exclusion["exclusion_digest"]
                    ).encode("utf-8")
                ).hexdigest()
            )

    root = candidate["root"]
    root["result_node_digests"] = {
        gate_id: by_gate[gate_id]["node_digest"] for gate_id in sorted(by_gate)
    }
    root["exclusion_digests"] = [
        exclusion["exclusion_digest"] for exclusion in candidate["exclusions"]
    ]
    root["root_digest"] = _content_digest(root, {"root_digest"})


def validate_executed_result(
    candidate: object, *, now: datetime
) -> dict[str, object]:
    """Validate one synthetic candidate and return derived acceptance findings."""
    findings: set[str] = set()
    if not isinstance(candidate, dict):
        return {
            "accepted": False,
            "findings": ["candidate_shape_invalid"],
            "protocol": PROTOCOL,
        }
    if set(candidate) != TOP_FIELDS:
        findings.add("candidate_shape_invalid")
    if candidate.get("protocol") != PROTOCOL:
        findings.add("candidate_protocol_invalid")
    if _has_boolean(candidate):
        findings.add("unsupported_completion_claim")
    if any(key in candidate for key in {"complete", "release_ready", "ready"}):
        findings.add("unsupported_completion_claim")

    root = candidate.get("root")
    nodes = candidate.get("nodes")
    exclusions = candidate.get("exclusions")
    artifacts = candidate.get("artifacts")
    if not isinstance(root, dict) or set(root) != ROOT_FIELDS:
        findings.add("root_shape_invalid")
        root = {}
    if not isinstance(nodes, list):
        findings.add("node_set_mismatch")
        nodes = []
    if not isinstance(exclusions, list):
        findings.add("exclusion_set_invalid")
        exclusions = []
    if not isinstance(artifacts, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in (artifacts.items() if isinstance(artifacts, dict) else [])
    ):
        findings.add("artifact_registry_invalid")
        artifacts = {}

    by_gate: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != NODE_FIELDS:
            findings.add("node_shape_invalid")
            continue
        gate_id = node.get("gate_id")
        if not isinstance(gate_id, str) or gate_id in by_gate:
            findings.add("node_set_mismatch")
            continue
        by_gate[gate_id] = node
    if set(by_gate) != set(DIRECT_PREREQUISITES):
        findings.add("node_set_mismatch")

    source_commit = root.get("source_commit")
    source_tree_digest = root.get("source_tree_digest")
    if not isinstance(source_commit, str) or COMMIT.fullmatch(source_commit) is None:
        findings.add("exact_source_commit_required")
    if not isinstance(source_tree_digest, str) or SHA256.fullmatch(source_tree_digest) is None:
        findings.add("source_tree_digest_invalid")
    if root.get("tree_state") != "clean":
        findings.add("clean_tree_required")

    for gate_id, node in by_gate.items():
        if node.get("protocol") != NODE_PROTOCOL:
            findings.add("node_protocol_invalid")
        if node.get("node_digest") != _content_digest(node, {"node_digest"}):
            findings.add("node_digest_mismatch")
        if node.get("source_commit") != source_commit or node.get(
            "source_tree_digest"
        ) != source_tree_digest:
            findings.add("source_binding_inconsistent")
        if node.get("tree_state") != "clean":
            findings.add("clean_tree_required")
        if node.get("outcome") != "verified":
            findings.add("verified_outcome_required")
        if node.get("provenance") != "live":
            findings.add("provenance_requirement_unsatisfied")
        if node.get("subject") != f"{gate_id}:executed_gate_result":
            findings.add("subject_binding_invalid")

        dependency_gate_ids = node.get("dependency_gate_ids")
        actual_dependencies = (
            set(dependency_gate_ids) if isinstance(dependency_gate_ids, list) else set()
        )
        if actual_dependencies != DIRECT_PREREQUISITES.get(gate_id, set()):
            findings.add("dependency_digests_not_exact")
        expected_dependency_digests = (
            [
                by_gate[dependency]["node_digest"]
                for dependency in dependency_gate_ids
                if dependency in by_gate
            ]
            if isinstance(dependency_gate_ids, list)
            else []
        )
        if node.get("dependency_digests") != expected_dependency_digests:
            findings.add("dependency_digests_not_exact")

        reference = node.get("artifact_reference")
        if not isinstance(reference, str):
            findings.add("artifact_reference_invalid")
        else:
            path = PurePosixPath(reference)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != reference:
                findings.add("artifact_reference_invalid")
            content = artifacts.get(reference)
            if content is None or node.get("artifact_digest") != artifact_digest(content):
                findings.add("artifact_digest_inconsistent")

        for field in {
            "artifact_digest",
            "verifier_policy_digest",
            "contract_digest",
            "model_digest",
            "representation_digest",
            "runtime_digest",
            "environment_digest",
        }:
            value = node.get(field)
            if not isinstance(value, str) or SHA256.fullmatch(value) is None:
                findings.add(f"node_binding_invalid:{field}")
        generation = node.get("authority_generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            findings.add("node_binding_invalid:authority_generation")

        executed_at = _timestamp(node.get("executed_at"))
        fresh_until = _timestamp(node.get("fresh_until"))
        if executed_at is None or fresh_until is None or fresh_until <= executed_at:
            findings.add("freshness_binding_invalid")
        elif fresh_until <= now:
            findings.add("mandatory_evidence_expired")

    result_node_digests = root.get("result_node_digests")
    expected_result_digests = {
        gate_id: node["node_digest"] for gate_id, node in sorted(by_gate.items())
    }
    if result_node_digests != expected_result_digests:
        findings.add("result_node_digest_set_mismatch")
    if root.get("root_digest") != _content_digest(root, {"root_digest"}):
        findings.add("root_digest_mismatch")

    gate_commits = root.get("gate_commit_digests")
    if not isinstance(gate_commits, dict) or set(gate_commits) != {
        f"A{index}" for index in range(3, 15)
    }:
        findings.add("gate_commit_set_mismatch")
    elif any(
        not isinstance(value, str) or COMMIT.fullmatch(value) is None
        for value in gate_commits.values()
    ) or len(set(gate_commits.values())) != len(gate_commits):
        findings.add("gate_commit_binding_invalid")

    digest_roots = root.get("digest_class_roots")
    if not isinstance(digest_roots, dict) or set(digest_roots) != DIGEST_CLASSES:
        findings.add("digest_class_set_mismatch")
    elif any(
        not isinstance(value, str) or SHA256.fullmatch(value) is None
        for value in digest_roots.values()
    ):
        findings.add("digest_class_binding_invalid")

    exclusion_digests: list[str] = []
    for exclusion in exclusions:
        if not isinstance(exclusion, dict) or set(exclusion) != EXCLUSION_FIELDS:
            findings.add("exclusion_shape_invalid")
            if isinstance(exclusion, dict) and "signature" not in exclusion:
                findings.add("exclusion_signature_missing")
            continue
        exclusion_digests.append(exclusion["exclusion_digest"])
        if exclusion.get("exclusion_digest") != _content_digest(
            exclusion, {"exclusion_digest", "signature"}
        ):
            findings.add("exclusion_digest_mismatch")
        signature = exclusion.get("signature")
        expected_signature = (
            "fixture-signature:"
            + hashlib.sha256(
                (
                    str(exclusion.get("signer_key_id"))
                    + str(exclusion.get("exclusion_digest"))
                ).encode("utf-8")
            ).hexdigest()
        )
        if signature != expected_signature:
            findings.add("exclusion_signature_invalid")
        issued_at = _timestamp(exclusion.get("issued_at"))
        expires_at = _timestamp(exclusion.get("expires_at"))
        if issued_at is None or expires_at is None or expires_at <= issued_at:
            findings.add("exclusion_expiry_invalid")
        elif expires_at <= now:
            findings.add("exclusion_expired")
    if root.get("exclusion_digests") != exclusion_digests:
        findings.add("exclusion_digest_set_mismatch")

    reviewer_digest = root.get("reviewer_reproduction_digest")
    if not isinstance(reviewer_digest, str) or SHA256.fullmatch(reviewer_digest) is None:
        findings.add("reviewer_reproduction_missing")
    if root.get("decision_generation") != 1:
        findings.add("decision_generation_invalid")

    return {
        "accepted": not findings,
        "findings": sorted(findings),
        "protocol": PROTOCOL,
    }
