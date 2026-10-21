# SPDX-License-Identifier: AGPL-3.0-only
"""Behaviour of the A15 release-decision derivation over a labelled synthetic world.

A valid synthetic graph derives ``ready`` under explicitly admitted synthetic trust; every
mutation derives ``not_ready`` and names the narrow invalid or missing result. Nothing
here is live or physical evidence and no test evaluates the real repository.
"""

from __future__ import annotations

import copy
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mycelium_a15_release_decision as producer
from mycelium_a15_release_decision import (
    DIGEST_CLASSES,
    DIRECT_PREREQUISITES,
    GRAPH_PROTOCOL,
    INPUT_GATES,
    REVIEWER_REQUIREMENT,
    ROLE_RESULT_VERIFIER_PREFIX,
    SCOPE_PRODUCTION,
    SCOPE_SYNTHETIC,
    ReleaseDecisionRefused,
    source_tree_digest,
    verify_decision_bytes,
)
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from tests.a15_acceptance import validator as frozen
from tests.a15_release_decision.synthetic_world import (
    JUNIT_PASSING,
    NOW,
    SyntheticWorld,
    build_template_repo,
    commit_file,
    git,
    independent_node_digest as node_digest,
    label_digest,
    passing,
    seal_nodes,
)


def assert_not_ready(result: Any, *expected: str) -> None:
    assert result.decision == "not_ready", result.findings
    body = result.document["body"]
    assert body["decision"] == "not_ready"
    assert body["graph"] is None and body["valid_until"] is None
    for finding in expected:
        assert finding in result.findings, result.findings


def requirement_state(result: Any, requirement_id: str) -> str:
    states = {
        item["requirement_id"]: item["state"] for item in result.document["body"]["requirements"]
    }
    return states[requirement_id]


def node_of(candidate: dict[str, Any], gate_id: str) -> dict[str, Any]:
    return next(node for node in candidate["nodes"] if node["gate_id"] == gate_id)


def reseal(candidate: dict[str, Any]) -> None:
    seal_nodes({node["gate_id"]: node for node in candidate["nodes"]})


# -- positive derivation ---------------------------------------------------------------


def test_valid_synthetic_graph_derives_ready(world: SyntheticWorld) -> None:
    result = world.built().produce()

    assert result.decision == "ready", result.findings
    assert result.findings == ()
    body = result.document["body"]
    assert body["decision"] == "ready"
    assert body["decision_generation"] == 1 and result.decision_generation == 1
    assert body["authority_scope"] == SCOPE_SYNTHETIC
    assert body["valid_until"] == "2026-10-12T09:00:00Z"
    assert [node["gate_id"] for node in body["graph"]["nodes"]] == [*INPUT_GATES, "A15"]
    assert {item["state"] for item in body["requirements"]} == {"verified"}
    assert set(body["graph"]["root"]["digest_class_roots"]) == set(DIGEST_CLASSES)
    assert verify_decision_bytes(result.sealed_bytes) == result.document


def test_ready_is_derived_from_the_graph_and_not_from_any_declaration(
    world: SyntheticWorld,
) -> None:
    """Review finding 1: the same producer yields both states, from evidence only."""
    ready = world.built().produce()
    world.specs["digest_class:test"]["files"] = {
        "junit.xml": JUNIT_PASSING.replace(
            b'<testcase classname="synthetic.fixture" name="second"/>',
            b'<testcase classname="synthetic.fixture" name="second"><failure/></testcase>',
        )
    }
    blocked = world.built().produce(now=NOW + timedelta(seconds=1))

    assert ready.decision == "ready"
    assert_not_ready(blocked)
    assert blocked.findings == ("junit_failures_present:digest_class:test",)
    states = {
        item["requirement_id"]: item["state"] for item in blocked.document["body"]["requirements"]
    }
    assert states.pop("digest_class:test") == "not_verified"
    assert set(states.values()) == {"verified"}


@pytest.mark.parametrize(
    "claim",
    [{"release_ready": True}, {"decision": "ready"}, {"complete": "yes"}, {"ready": 1}],
)
def test_declared_readiness_is_rejected(world: SyntheticWorld, claim: dict[str, Any]) -> None:
    world.candidate_mutators.append(lambda candidate: candidate.update(claim))
    assert_not_ready(world.built().produce(), "unsupported_completion_claim")


def test_boolean_nested_in_a_node_is_an_unsupported_claim(world: SyntheticWorld) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        node_of(candidate, "A7")["outcome"] = True
        reseal(candidate)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "unsupported_completion_claim")


# -- review finding 6: graph boundary reconciled with the frozen validator ---------------


def test_prerequisite_graph_equals_the_frozen_validator_graph() -> None:
    assert {gate: set(deps) for gate, deps in DIRECT_PREREQUISITES.items()} == (
        frozen.DIRECT_PREREQUISITES
    )
    assert set(INPUT_GATES) == set(frozen.DIRECT_PREREQUISITES) - {"A15"}
    assert GRAPH_PROTOCOL == frozen.PROTOCOL


def test_emitted_graph_is_accepted_by_the_frozen_synthetic_validator(
    world: SyntheticWorld,
) -> None:
    result = world.built().produce()
    body = result.document["body"]
    graph = body["graph"]
    artifacts: dict[str, str] = {}
    for node in graph["nodes"]:
        reference = node["artifact_reference"]
        if node["gate_id"] == "A15":
            artifacts[reference] = canonical_json_bytes(body["closure_statement"]).decode("ascii")
        else:
            root_id, _, relative = reference.partition("/")
            artifacts[reference] = (world.roots[root_id] / relative).read_text("ascii")
    frozen_candidate = {
        "protocol": graph["protocol"],
        "root": graph["root"],
        "nodes": graph["nodes"],
        "exclusions": [],
        "artifacts": artifacts,
    }

    assert set(graph) == {"protocol", "root", "nodes"}
    assert frozen.validate_executed_result(frozen_candidate, now=NOW) == {
        "accepted": True,
        "findings": [],
        "protocol": frozen.PROTOCOL,
    }


def test_caller_supplied_closure_root_node_is_refused(world: SyntheticWorld) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        root_node = copy.deepcopy(node_of(candidate, "A14"))
        root_node.update(
            gate_id="A15",
            subject="A15:executed_gate_result",
            result_kind="release_closure",
            dependency_gate_ids=sorted(DIRECT_PREREQUISITES["A15"]),
        )
        candidate["nodes"].append(root_node)
        reseal(candidate)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "derived_root_node_supplied")


def test_missing_gate_node_fails_closed(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate["nodes"].remove(node_of(candidate, "A11"))
    )
    assert_not_ready(world.built().produce(), "node_set_mismatch", "result_missing:gate_result:A11")


def test_extra_gate_node_fails_closed(world: SyntheticWorld) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        extra = copy.deepcopy(node_of(candidate, "A14"))
        extra.update(gate_id="A16", subject="A16:executed_gate_result")
        extra["node_digest"] = node_digest(extra)
        candidate["nodes"].append(extra)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "node_set_mismatch")


def test_missing_direct_edge_fails_closed(world: SyntheticWorld) -> None:
    world.node_mutators.append(lambda nodes: nodes["A12"]["dependency_gate_ids"].remove("A4"))
    assert_not_ready(world.built().produce(), "dependency_digests_not_exact:gate_result:A12")


def test_extra_transitive_edge_fails_closed(world: SyntheticWorld) -> None:
    world.node_mutators.append(lambda nodes: nodes["A14"]["dependency_gate_ids"].append("A3"))
    assert_not_ready(world.built().produce(), "dependency_digests_not_exact:gate_result:A14")


def test_dependency_digest_of_another_node_fails_closed(world: SyntheticWorld) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        node = node_of(candidate, "A9")
        node["dependency_digests"] = [node_of(candidate, "A3")["node_digest"]]
        node["node_digest"] = node_digest(node)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "dependency_digests_not_exact:gate_result:A9")


def test_dependency_cycle_fails_closed(world: SyntheticWorld) -> None:
    world.node_mutators.append(lambda nodes: nodes["A3"]["dependency_gate_ids"].append("A4"))
    assert_not_ready(world.built().produce(), "dependency_cycle_detected")


def test_conflicting_duplicate_node_fails_closed(world: SyntheticWorld) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        duplicate = copy.deepcopy(node_of(candidate, "A6"))
        duplicate["runtime_digest"] = label_digest("another-runtime")
        duplicate["node_digest"] = node_digest(duplicate)
        candidate["nodes"].append(duplicate)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "conflicting_duplicate_node:gate_result:A6")


def test_identical_duplicate_node_fails_closed(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate["nodes"].append(copy.deepcopy(node_of(candidate, "A6")))
    )
    assert_not_ready(world.built().produce(), "duplicate_logical_result:gate_result:A6")


def test_node_digest_must_match_content(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: node_of(candidate, "A14").update(
            environment_digest=label_digest("edited-after-sealing")
        )
    )
    assert_not_ready(world.built().produce(), "node_digest_mismatch:gate_result:A14")


def test_unknown_node_field_fails_closed(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: node_of(candidate, "A5").update(operator_note="looks fine")
    )
    assert_not_ready(world.built().produce(), "node_shape_invalid")


def test_missing_digest_class_fails_closed(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(lambda candidate: candidate["class_results"].pop("sbom"))
    result = world.built().produce()
    assert_not_ready(result, "digest_class_set_mismatch", "result_missing:digest_class:sbom")
    assert requirement_state(result, "digest_class:sbom") == "not_verified"


# -- review finding 2: the artifact, not the envelope, decides the outcome ---------------


def test_envelope_verified_but_signed_receipt_failed(world: SyntheticWorld) -> None:
    world.specs["gate_result:A12"]["fields"]["outcome"] = "fail"
    result = world.built().produce()
    assert node_of(world.candidate, "A12")["outcome"] == "verified"
    assert_not_ready(
        result,
        "result_outcome_not_pass:gate_result:A12",
        "outcome_contradicts_artifact:gate_result:A12",
    )


def test_envelope_must_claim_verified(world: SyntheticWorld) -> None:
    world.node_mutators.append(lambda nodes: nodes["A8"].update(outcome="blocked"))
    assert_not_ready(world.built().produce(), "verified_outcome_required:gate_result:A8")


def test_handwritten_pass_artifact_is_not_a_result(world: SyntheticWorld) -> None:
    """Forged manual pass: correct digest over bytes that are not a signed receipt."""
    world.build()
    forged = canonical_json_bytes({"gate": "A9", "outcome": "pass", "verified_by": "operator"})
    world.receipt_path("gate_result:A9").write_bytes(forged)

    def mutate(candidate: dict[str, Any]) -> None:
        node_of(candidate, "A9")["artifact_digest"] = sha256_bytes(forged)
        reseal(candidate)

    mutate(world.candidate)
    world.candidate_path.write_bytes(canonical_json_bytes(world.candidate))
    assert_not_ready(
        world.produce(),
        "result_receipt_invalid:gate_result:A9",
        "outcome_contradicts_artifact:gate_result:A9",
    )


def test_junit_skip_is_not_a_pass(world: SyntheticWorld) -> None:
    world.specs["digest_class:test"]["files"] = {
        "junit.xml": JUNIT_PASSING.replace(
            b'<testcase classname="synthetic.fixture" name="first"/>',
            b'<testcase classname="synthetic.fixture" name="first"><skipped/></testcase>',
        )
    }
    assert_not_ready(world.built().produce(), "junit_skips_present:digest_class:test")


def test_junit_receipt_without_executed_cases_is_not_a_pass(world: SyntheticWorld) -> None:
    world.specs["digest_class:test"]["files"] = {
        "junit.xml": b'<testsuites><testsuite name="synthetic-test-fixture" tests="0"/></testsuites>'
    }
    assert_not_ready(world.built().produce(), "junit_no_executed_tests:digest_class:test")


def test_unsigned_result_is_rejected(world: SyntheticWorld) -> None:
    world.specs["gate_result:A4"]["signer"] = None
    assert_not_ready(world.built().produce(), "result_signature_missing:gate_result:A4")


def test_result_signed_by_unknown_verifier_is_rejected(world: SyntheticWorld) -> None:
    world.specs["gate_result:A4"]["signer"] = generate_ed25519_signer(
        endpoint_id="synthetic-fixture-unregistered"
    )
    assert_not_ready(world.built().produce(), "result_signature_untrusted:gate_result:A4")


def test_signature_does_not_cover_altered_statement(world: SyntheticWorld) -> None:
    world.build()
    receipt = dict(world.receipts["gate_result:A4"])
    receipt["runtime_digest"] = label_digest("swapped-runtime")
    content = canonical_json_bytes(receipt)
    world.receipt_path("gate_result:A4").write_bytes(content)
    node = node_of(world.candidate, "A4")
    node.update(artifact_digest=sha256_bytes(content), runtime_digest=receipt["runtime_digest"])
    reseal(world.candidate)
    world.candidate_path.write_bytes(canonical_json_bytes(world.candidate))
    assert_not_ready(world.produce(), "result_signature_invalid:gate_result:A4")


def test_valid_receipt_cannot_be_replayed_for_another_gate(world: SyntheticWorld) -> None:
    world.build()
    reference, digest = world.references["gate_result:A3"]

    def mutate(candidate: dict[str, Any]) -> None:
        node_of(candidate, "A4").update(artifact_reference=reference, artifact_digest=digest)
        reseal(candidate)

    mutate(world.candidate)
    world.candidate_path.write_bytes(canonical_json_bytes(world.candidate))
    assert_not_ready(
        world.produce(),
        "result_binding_mismatch:gate_result:A4",
        "outcome_contradicts_artifact:gate_result:A4",
    )


def test_evidence_manifest_cannot_reach_outside_the_receipt_directory(
    world: SyntheticWorld,
) -> None:
    world.build()
    receipt = dict(world.receipts["gate_result:A6"])
    manifest = copy.deepcopy(receipt["evidence_manifest"])
    manifest["files"][0]["path"] = "../A5/result.json"
    receipt["evidence_manifest"] = manifest
    receipt["signature"] = world.verifier.sign(
        {key: value for key, value in receipt.items() if key != "signature"}
    )
    content = canonical_json_bytes(receipt)
    world.receipt_path("gate_result:A6").write_bytes(content)
    node_of(world.candidate, "A6")["artifact_digest"] = sha256_bytes(content)
    reseal(world.candidate)
    world.candidate_path.write_bytes(canonical_json_bytes(world.candidate))
    assert_not_ready(world.produce(), "evidence_manifest_invalid:gate_result:A6")


def test_key_not_authorised_for_the_result_kind_is_rejected(world: SyntheticWorld) -> None:
    roles = world.registry["keys"][0]["roles"]
    roles.remove(ROLE_RESULT_VERIFIER_PREFIX + "junit_suite")
    assert_not_ready(world.built().produce(), "result_signer_not_authorized:digest_class:test")


def test_result_kind_outside_the_allowlist_is_rejected(world: SyntheticWorld) -> None:
    world.specs["gate_result:A5"]["kind"] = "operator_affidavit"
    assert_not_ready(world.built().produce(), "result_kind_not_allowlisted:gate_result:A5")


def test_result_kind_cannot_stand_in_for_another_requirement(world: SyntheticWorld) -> None:
    world.specs["gate_result:A5"]["kind"] = "junit_suite"
    world.specs["gate_result:A5"]["files"] = {"junit.xml": JUNIT_PASSING}
    assert_not_ready(world.built().produce(), "result_kind_wrong_for_requirement:gate_result:A5")


def test_verifier_policy_must_be_allowlisted_by_the_registry(world: SyntheticWorld) -> None:
    world.registry["verifier_policy_digests"]["browser_engine_matrix"] = label_digest("other")
    assert_not_ready(
        world.built().produce(), "verifier_policy_not_allowlisted:digest_class:browser"
    )


def test_node_verifier_policy_must_be_the_kind_policy(world: SyntheticWorld) -> None:
    world.specs["gate_result:A10"]["fields"]["verifier_policy_digest"] = label_digest("lenient")
    assert_not_ready(world.built().produce(), "verifier_policy_mismatch:gate_result:A10")


@pytest.mark.parametrize("provenance", ["fixture", "replay", "historical"])
def test_non_live_provenance_cannot_satisfy_a_live_required_kind(
    world: SyntheticWorld, provenance: str
) -> None:
    world.specs["gate_result:A12"]["fields"]["provenance"] = provenance
    result = world.built().produce()
    assert node_of(world.candidate, "A12")["provenance"] == provenance
    assert_not_ready(result, "provenance_requirement_unsatisfied:gate_result:A12")


def test_envelope_cannot_relabel_fixture_receipt_as_live(world: SyntheticWorld) -> None:
    world.specs["gate_result:A12"]["fields"]["provenance"] = "fixture"
    world.node_mutators.append(lambda nodes: nodes["A12"].update(provenance="live"))
    assert_not_ready(
        world.built().produce(),
        "provenance_requirement_unsatisfied:gate_result:A12",
        "envelope_contradicts_artifact:gate_result:A12",
    )


def test_synthetic_evidence_is_never_admitted_under_production_trust(
    tmp_path: Path, template: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = SyntheticWorld(tmp_path, template, registry_class=SCOPE_PRODUCTION)
    world.build()
    # Test-only: recognise this throwaway production-labelled registry so the production
    # branch can be exercised. The source allowlist itself stays empty.
    monkeypatch.setattr(producer, "PRODUCTION_REGISTRY_PINS", frozenset({world.registry_digest}))
    result = world.produce(allow_synthetic_trust=False)
    assert_not_ready(
        result,
        "synthetic_evidence_not_admissible:gate_result:A3",
        "synthetic_evidence_not_admissible:digest_class:physical",
        "synthetic_evidence_not_admissible:external_reviewer_reproduction",
    )
    assert result.document["body"]["authority_scope"] == SCOPE_PRODUCTION


def test_synthetic_trust_requires_explicit_admission(world: SyntheticWorld) -> None:
    result = world.built().produce(allow_synthetic_trust=False)
    assert_not_ready(result, "synthetic_trust_registry_not_admitted")


def test_physical_labelled_evidence_is_refused_under_synthetic_trust(
    world: SyntheticWorld,
) -> None:
    world.specs["gate_result:A7"]["evidence_class"] = "physical_qualification"
    assert_not_ready(world.built().produce(), "evidence_class_scope_mismatch:gate_result:A7")


def test_required_property_must_be_attested(world: SyntheticWorld) -> None:
    world.specs["gate_result:A7"]["fields"]["properties"] = ["clean_tree"]
    assert_not_ready(
        world.built().produce(), "required_property_missing:gate_result:A7#physical"
    )


def test_one_browser_engine_cannot_replace_the_engine_matrix(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"] = passing(["engine:chromium"])
    assert_not_ready(
        world.built().produce(),
        "required_case_missing:digest_class:browser#engine:firefox",
        "required_case_missing:digest_class:browser#engine:webkit",
    )


def test_failed_case_is_not_a_pass(world: SyntheticWorld) -> None:
    world.specs["digest_class:model"]["cases"] = [
        {"case_id": "synthetic_fixture_case", "outcome": "fail"}
    ]
    assert_not_ready(world.built().produce(), "case_not_passed:digest_class:model")


def test_physical_bundle_is_checked_by_the_bundle_verifier(world: SyntheticWorld) -> None:
    world.build()
    bundle = world.receipt_directory("digest_class:physical") / "bundle"
    (bundle / "provenance" / "synthetic-dependencies.lock").write_bytes(b"synthetic-test-altered\n")
    assert_not_ready(world.produce(), "physical_bundle_invalid:digest_class:physical")


def test_incomplete_sbom_fails_closed(world: SyntheticWorld) -> None:
    world.specs["digest_class:sbom"]["files"] = {"sbom.json": world.sbom_bytes(omit="rust-lock")}
    assert_not_ready(world.built().produce(), "sbom_incomplete:digest_class:sbom")


def test_sbom_for_another_revision_fails_closed(world: SyntheticWorld) -> None:
    world.specs["digest_class:sbom"]["files"] = {"sbom.json": world.sbom_bytes(revision="2" * 40)}
    assert_not_ready(world.built().produce(), "sbom_source_mismatch:digest_class:sbom")


def test_self_inconsistent_sbom_fails_closed(world: SyntheticWorld) -> None:
    world.specs["digest_class:sbom"]["files"] = {
        "sbom.json": world.sbom_bytes().replace(b"python-lock", b"python-l0ck")
    }
    assert_not_ready(world.built().produce(), "sbom_invalid:digest_class:sbom")


def test_contract_manifest_must_be_the_source_contract_manifest(world: SyntheticWorld) -> None:
    world.specs["digest_class:contract"]["files"] = {
        "contract-manifest.v1.json": b'{"synthetic-test-fixture":"other contract"}'
    }
    assert_not_ready(world.built().produce(), "contract_manifest_mismatch:digest_class:contract")


def test_node_bound_to_another_contract_fails_closed(world: SyntheticWorld) -> None:
    world.specs["gate_result:A6"]["fields"]["contract_digest"] = label_digest("other-contract")
    assert_not_ready(world.built().produce(), "contract_binding_inconsistent:gate_result:A6")


@pytest.mark.parametrize(
    ("field", "finding"),
    [
        ("model_digest", "model_binding_inconsistent"),
        ("representation_digest", "representation_binding_inconsistent"),
    ],
)
def test_result_from_another_model_or_representation_fails_closed(
    world: SyntheticWorld, field: str, finding: str
) -> None:
    world.specs["gate_result:A13"]["fields"][field] = label_digest("other")
    assert_not_ready(world.built().produce(), f"{finding}:gate_result:A13")


# -- altered and absent artifacts -------------------------------------------------------


def test_altered_receipt_bytes_fail_closed(world: SyntheticWorld) -> None:
    world.build()
    path = world.receipt_path("gate_result:A12")
    path.write_bytes(path.read_bytes().replace(b'"outcome":"pass"', b'"outcome":"fail"'))
    result = world.produce()
    assert_not_ready(result)
    assert set(result.findings) == {
        "artifact_digest_inconsistent:gate_result:A12",
        "outcome_contradicts_artifact:gate_result:A12",
    }
    assert requirement_state(result, "gate_result:A12") == "not_verified"
    assert requirement_state(result, "gate_result:A13") == "invalidated_by_dependency"
    assert requirement_state(result, "gate_result:A9") == "verified"


def test_altered_evidence_file_fails_closed(world: SyntheticWorld) -> None:
    world.build()
    (world.receipt_directory("digest_class:audit") / "audit.json").write_bytes(b"{}")
    assert_not_ready(world.produce(), "artifact_digest_inconsistent:digest_class:audit")


def test_absent_receipt_fails_closed(world: SyntheticWorld) -> None:
    world.build()
    world.receipt_path("gate_result:A3").unlink()
    assert_not_ready(world.produce(), "artifact_missing:gate_result:A3")


def test_absent_evidence_file_fails_closed(world: SyntheticWorld) -> None:
    world.build()
    (world.receipt_directory("gate_result:A3") / "result.json").unlink()
    assert_not_ready(world.produce(), "artifact_missing:gate_result:A3")


def test_private_path_in_evidence_is_a_privacy_leak(world: SyntheticWorld) -> None:
    world.specs["digest_class:audit"]["files"] = {
        "audit.json": b'{"synthetic-test-fixture":"/Users/someone/private/notes"}'
    }
    assert_not_ready(world.built().produce(), "privacy_leak_detected:digest_class:audit")


# -- review finding 3: source binding ---------------------------------------------------


def test_checkout_whose_head_is_not_the_declared_commit_is_rejected(
    world: SyntheticWorld,
) -> None:
    world.build()
    commit_file(world.repo, "later.txt", b"synthetic-test-fixture: later\n", "later")
    assert git(world.repo, "status", "--porcelain") == ""
    assert git(world.repo, "cat-file", "-t", world.commit) == "commit"
    result = world.produce()
    assert_not_ready(result)
    assert set(result.findings) == {"source_head_mismatch", "source_tree_mismatch"}
    assert requirement_state(result, "source_integrity") == "not_verified"


def test_explicit_repo_root_is_the_only_source_authority(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    world.build()
    unrelated = tmp_path / "unrelated-checkout"
    build_template_repo(unrelated)
    commit_file(unrelated, "other.txt", b"synthetic-test-fixture: unrelated\n", "unrelated")
    assert "source" in world.candidate and set(world.candidate["source"]) == {
        "commit",
        "tree_digest",
    }
    assert_not_ready(world.produce(repo_root=unrelated), "source_head_mismatch")


def test_environment_cannot_redirect_the_observed_checkout(
    world: SyntheticWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.build()
    unrelated = tmp_path / "unrelated-checkout"
    build_template_repo(unrelated)
    commit_file(unrelated, "other.txt", b"synthetic-test-fixture: unrelated\n", "unrelated")
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    result = world.produce()
    monkeypatch.undo()
    assert result.decision == "ready", result.findings
    assert result.document["body"]["source"]["commit"] == world.commit


def test_declared_tree_must_match_observed_tree(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate["source"].update(
            tree_digest=source_tree_digest("sha1", "3" * 40)
        )
    )
    assert_not_ready(world.built().produce(), "source_tree_mismatch")


def test_dirty_tracked_tree_cannot_back_a_clean_build_claim(world: SyntheticWorld) -> None:
    world.build()
    (world.repo / "candidate.txt").write_bytes(b"synthetic-test-fixture: edited\n")
    assert_not_ready(world.produce(), "clean_tree_required")


def test_untracked_source_input_is_rejected(world: SyntheticWorld) -> None:
    world.build()
    (world.repo / "untracked_build_input.py").write_bytes(b"# synthetic-test-fixture\n")
    assert_not_ready(world.produce(), "untracked_source_input_present")


def test_repo_root_must_be_the_checkout_root(world: SyntheticWorld) -> None:
    assert_not_ready(world.built().produce(repo_root=world.repo / "gates"), "source_checkout_invalid")


def test_directory_that_is_not_a_checkout_is_rejected(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    plain = tmp_path / "plain-directory"
    plain.mkdir()
    assert_not_ready(world.built().produce(repo_root=plain), "source_checkout_invalid")


def test_abbreviated_commit_is_not_an_exact_source(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate["source"].update(commit=candidate["source"]["commit"][:12])
    )
    assert_not_ready(world.built().produce(), "exact_source_commit_required")


def test_node_from_another_source_fails_closed(world: SyntheticWorld) -> None:
    world.specs["gate_result:A5"]["fields"]["source_commit"] = "4" * 40
    assert_not_ready(world.built().produce(), "source_binding_inconsistent:gate_result:A5")


# -- review finding 4: one tree-id representation ---------------------------------------


def test_tree_digest_has_one_representation_for_both_object_formats() -> None:
    sha1_digest = source_tree_digest("sha1", "a" * 40)
    sha256_digest = source_tree_digest("sha256", "a" * 64)
    assert frozen.SHA256.fullmatch(sha1_digest) and frozen.SHA256.fullmatch(sha256_digest)
    assert sha1_digest != sha256_digest
    assert sha1_digest != source_tree_digest("sha1", "b" * 40)
    for object_format, tree_id in [("sha1", "a" * 64), ("sha256", "a" * 40), ("md5", "a" * 32)]:
        with pytest.raises(ValueError):
            source_tree_digest(object_format, tree_id)


def test_real_sha1_checkout_tree_validates_in_source_nodes_and_root(
    world: SyntheticWorld,
) -> None:
    result = world.built().produce()
    tree_id = git(world.repo, "rev-parse", "HEAD^{tree}")
    expected = source_tree_digest("sha1", tree_id)
    body = result.document["body"]
    assert body["source"]["tree_digest"] == expected
    assert body["graph"]["root"]["source_tree_digest"] == expected
    assert {node["source_tree_digest"] for node in body["graph"]["nodes"]} == {expected}
    assert tree_id not in result.sealed_bytes.decode("ascii")


def test_bare_git_tree_id_is_not_an_accepted_representation(world: SyntheticWorld) -> None:
    bare = git(world.repo, "rev-parse", "HEAD^{tree}")
    world.candidate_mutators.append(lambda candidate: candidate["source"].update(tree_digest=bare))
    assert_not_ready(world.built().produce(), "source_tree_digest_invalid")


def test_sha256_object_format_checkout_is_refused_explicitly(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    checkout = tmp_path / "sha256-checkout"
    build_template_repo(checkout, object_format="sha256")
    assert len(git(checkout, "rev-parse", "HEAD")) == 64
    assert_not_ready(
        world.built().produce(repo_root=checkout), "source_object_format_unsupported"
    )


# -- gate commit graph -----------------------------------------------------------------


def test_gate_commit_set_must_be_exact(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(lambda candidate: candidate["gate_commits"].pop("A9"))
    assert_not_ready(world.built().produce(), "gate_commit_set_mismatch")


def test_two_gates_cannot_share_one_commit(world: SyntheticWorld) -> None:
    world.gate_commits["A6"] = world.gate_commits["A5"]
    assert_not_ready(world.built().produce(), "gate_commit_binding_invalid")


def test_gate_commit_must_be_an_ancestor_of_the_source(world: SyntheticWorld) -> None:
    world.gate_commits["A6"] = "5" * 40
    assert_not_ready(world.built().produce(), "gate_commit_not_ancestor:A6")


def test_prerequisite_gate_commit_must_precede_its_dependent(world: SyntheticWorld) -> None:
    commits = world.gate_commits
    commits["A3"], commits["A4"] = commits["A4"], commits["A3"]
    assert_not_ready(world.built().produce(), "gate_commit_prerequisite_not_ancestor:A4")


# -- review finding 7: evidence roots ---------------------------------------------------


def test_resolution_does_not_depend_on_root_order(world: SyntheticWorld) -> None:
    world.build()
    reversed_roots = dict(reversed(list(world.roots.items())))
    assert list(reversed_roots) != list(world.roots)
    assert world.produce(evidence_roots=reversed_roots).decision == "ready"


def test_same_relative_reference_under_two_roots_is_resolved_by_identity(
    world: SyntheticWorld,
) -> None:
    world.build()
    shadow = world.roots["matrices"] / "A3"
    shadow.mkdir()
    (shadow / "receipt.json").write_bytes(b'{"synthetic-test-fixture":"shadow"}')
    assert world.produce().decision == "ready"

    def mutate(candidate: dict[str, Any]) -> None:
        node_of(candidate, "A3")["artifact_reference"] = "matrices/A3/receipt.json"
        reseal(candidate)

    world.candidate_mutators.append(mutate)
    world.build()
    (shadow / "receipt.json").write_bytes(b'{"synthetic-test-fixture":"shadow"}')
    assert_not_ready(
        world.produce(now=NOW + timedelta(seconds=1)),
        "artifact_digest_inconsistent:gate_result:A3",
    )


def test_reference_to_an_undeclared_root_is_refused(world: SyntheticWorld) -> None:
    world.build()
    only_gates = {"gates": world.roots["gates"]}
    assert_not_ready(
        world.produce(evidence_roots=only_gates), "artifact_root_unknown:digest_class:test"
    )


def test_two_identities_for_one_directory_are_ambiguous(world: SyntheticWorld) -> None:
    world.build()
    roots = {**world.roots, "alias": world.roots["gates"]}
    assert_not_ready(world.produce(evidence_roots=roots), "evidence_root_ambiguous")


def test_nested_roots_are_ambiguous(world: SyntheticWorld) -> None:
    world.build()
    roots = {**world.roots, "nested": world.roots["gates"] / "A3"}
    assert_not_ready(world.produce(evidence_roots=roots), "evidence_root_ambiguous")


def test_missing_root_directory_is_reported(world: SyntheticWorld, tmp_path: Path) -> None:
    world.build()
    roots = {**world.roots, "absent": tmp_path / "does-not-exist"}
    assert_not_ready(world.produce(evidence_roots=roots), "evidence_root_missing:absent")


@pytest.mark.parametrize(
    "reference", ["gates/../matrices/test/receipt.json", "/gates/A5/receipt.json", "receipt.json"]
)
def test_unsafe_reference_is_refused(world: SyntheticWorld, reference: str) -> None:
    def mutate(candidate: dict[str, Any]) -> None:
        node_of(candidate, "A5")["artifact_reference"] = reference
        reseal(candidate)

    world.candidate_mutators.append(mutate)
    assert_not_ready(world.built().produce(), "artifact_reference_invalid:gate_result:A5")


def test_symlinked_artifact_is_refused(world: SyntheticWorld, tmp_path: Path) -> None:
    world.build()
    path = world.receipt_path("gate_result:A5")
    outside = tmp_path / "outside-receipt.json"
    shutil.move(path, outside)
    path.symlink_to(outside)
    assert_not_ready(world.produce(), "artifact_reference_invalid:gate_result:A5")


# -- review finding 8: freshness and currentness ----------------------------------------


def test_evidence_executed_in_the_future_is_rejected(world: SyntheticWorld) -> None:
    world.specs["gate_result:A12"]["fields"].update(
        executed_at="2026-10-11T12:00:01Z", fresh_until="2026-10-13T09:00:00Z"
    )
    assert_not_ready(world.built().produce(), "evidence_executed_in_future:gate_result:A12")


def test_expired_evidence_is_rejected(world: SyntheticWorld) -> None:
    world.specs["gate_result:A12"]["fields"]["fresh_until"] = "2026-10-11T12:00:00Z"
    result = world.built().produce()
    assert_not_ready(result)
    assert set(result.findings) == {
        "mandatory_evidence_expired:gate_result:A12",
        "outcome_contradicts_artifact:gate_result:A12",
    }


def test_freshness_window_must_be_well_formed(world: SyntheticWorld) -> None:
    world.specs["gate_result:A12"]["fields"]["fresh_until"] = "2026-10-11T09:00:00Z"
    assert_not_ready(world.built().produce(), "freshness_binding_invalid:gate_result:A12")


@pytest.mark.parametrize("current", [6, 8])
def test_authority_generation_must_be_the_observed_current_generation(
    world: SyntheticWorld, current: int
) -> None:
    world.registry["current_authority_generation"] = current
    assert_not_ready(world.built().produce(), "authority_generation_not_current:gate_result:A3")


def test_single_stale_generation_node_is_named(world: SyntheticWorld) -> None:
    world.specs["gate_result:A10"]["fields"]["authority_generation"] = 6
    result = world.built().produce()
    assert_not_ready(result, "authority_generation_not_current:gate_result:A10")
    assert requirement_state(result, "gate_result:A11") == "invalidated_by_dependency"
    assert requirement_state(result, "gate_result:A12") == "verified"


def test_expired_trust_registry_is_rejected(world: SyntheticWorld) -> None:
    world.registry["expires_at"] = "2026-10-11T11:59:59Z"
    assert_not_ready(world.built().produce(), "trust_registry_expired")


def test_trust_registry_issued_in_the_future_is_rejected(world: SyntheticWorld) -> None:
    world.registry["issued_at"] = "2026-10-11T12:00:01Z"
    assert_not_ready(world.built().produce(), "trust_registry_not_yet_valid")


def test_now_must_be_timezone_aware_utc(world: SyntheticWorld) -> None:
    with pytest.raises(ReleaseDecisionRefused) as refusal:
        world.built().produce(now=datetime(2026, 10, 11, 12, 0))
    assert refusal.value.code == "now_must_be_utc"
    assert list(world.ledger.iterdir()) == []


# -- trust origin ----------------------------------------------------------------------


def test_trust_registry_must_match_the_pinned_digest(world: SyntheticWorld) -> None:
    world.build()
    result = world.produce(trust_registry_sha256=label_digest("another-registry"))
    assert_not_ready(result, "trust_registry_digest_mismatch")
    assert requirement_state(result, "trust_registry") == "not_verified"


@pytest.mark.parametrize("location", ["evidence", "checkout"])
def test_trust_registry_cannot_come_from_the_checked_inputs(
    world: SyntheticWorld, location: str
) -> None:
    world.build()
    directory = {
        "evidence": world.roots["gates"],
        "checkout": world.repo / ".git",
    }[location]
    planted = directory / "trust-registry.json"
    shutil.copyfile(world.registry_path, planted)
    result = world.produce(trust_registry_path=planted)
    planted.unlink()
    assert_not_ready(result, "trust_registry_origin_invalid")


def test_trust_registry_planted_in_the_ledger_is_refused(world: SyntheticWorld) -> None:
    world.build()
    planted = world.ledger / "trust-registry.json"
    shutil.copyfile(world.registry_path, planted)
    with pytest.raises(ReleaseDecisionRefused) as refusal:
        world.produce(trust_registry_path=planted)
    assert refusal.value.code == "decision_history_tampered"
    assert sorted(path.name for path in world.ledger.iterdir()) == ["trust-registry.json"]


def test_key_cannot_be_both_operator_and_independent_reviewer(world: SyntheticWorld) -> None:
    world.registry["keys"][0]["roles"].append("independent_reviewer")
    assert_not_ready(world.built().produce(), "trust_registry_invalid")


# -- reviewer reproduction -------------------------------------------------------------


def test_missing_reviewer_reproduction_withholds_release(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate.update(reviewer_reproduction=None)
    )
    result = world.built().produce()
    assert_not_ready(result, "reviewer_reproduction_missing")
    assert requirement_state(result, REVIEWER_REQUIREMENT) == "not_verified"


def test_operator_key_cannot_sign_the_independent_reproduction(world: SyntheticWorld) -> None:
    world.specs[REVIEWER_REQUIREMENT]["signer"] = world.verifier
    assert_not_ready(
        world.built().produce(), "result_signer_not_authorized:external_reviewer_reproduction"
    )


def test_unresolved_review_finding_withholds_release(world: SyntheticWorld) -> None:
    cases = world.specs[REVIEWER_REQUIREMENT]["cases"]
    next(case for case in cases if case["case_id"] == "review_findings_resolved")[
        "outcome"
    ] = "fail"
    assert_not_ready(
        world.built().produce(),
        "required_case_not_passed:external_reviewer_reproduction#review_findings_resolved",
    )


def test_reproduction_must_bind_the_exact_gate_results(world: SyntheticWorld) -> None:
    world.reviewer_binds_nodes = False
    world.specs[REVIEWER_REQUIREMENT]["fields"]["bound_result_digests"] = [label_digest("other")]
    assert_not_ready(world.built().produce(), "reviewer_result_binding_mismatch")


# -- signed exclusions (spec section 3) -------------------------------------------------


WEBKIT = "digest_class:browser#engine:webkit"


def exclude_webkit(world: SyntheticWorld, **fields: Any) -> None:
    world.specs["digest_class:browser"]["cases"] = [
        {"case_id": "engine:chromium", "outcome": "pass"},
        {"case_id": "engine:firefox", "outcome": "pass"},
        {"case_id": "engine:webkit", "outcome": "blocked"},
    ]
    world.exclusions.append(world.make_exclusion([WEBKIT], **fields))


def test_signed_bounded_exclusion_is_recorded_and_never_becomes_a_pass(
    world: SyntheticWorld,
) -> None:
    exclude_webkit(world)
    result = world.built().produce()
    assert result.decision == "ready", result.findings
    body = result.document["body"]
    assert body["withheld_claims"] == [WEBKIT]
    assert requirement_state(result, "digest_class:browser") == "excluded_approved"
    assert body["exclusions"] == [
        {
            "exclusion_digest": world.exclusions[0]["exclusion_digest"],
            "expires_at": "2026-10-18T08:00:00Z",
            "requirement_ids": [WEBKIT],
            "state": "approved",
        }
    ]
    assert body["valid_until"] == "2026-10-12T09:00:00Z"
    assert body["graph"]["root"]["exclusion_digests"] == [world.exclusions[0]["exclusion_digest"]]


def test_blocked_case_without_exclusion_withholds_release(world: SyntheticWorld) -> None:
    exclude_webkit(world)
    world.exclusions.clear()
    assert_not_ready(world.built().produce(), f"required_case_not_passed:{WEBKIT}")


def test_exclusion_does_not_cover_an_executed_failure(world: SyntheticWorld) -> None:
    exclude_webkit(world)
    world.specs["digest_class:browser"]["cases"][2]["outcome"] = "fail"
    assert_not_ready(world.built().produce(), f"required_case_not_passed:{WEBKIT}")


def test_decision_validity_is_bounded_by_the_exclusion_expiry(world: SyntheticWorld) -> None:
    exclude_webkit(world, expires_at="2026-10-11T18:00:00Z")
    result = world.built().produce()
    assert result.decision == "ready"
    assert result.document["body"]["valid_until"] == "2026-10-11T18:00:00Z"


def test_unsigned_exclusion_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world, sign=False)
    assert_not_ready(world.built().produce(), "exclusion_signature_missing")


def test_exclusion_without_a_signature_field_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world)
    del world.exclusions[0]["signature"]
    assert_not_ready(world.built().produce(), "exclusion_signature_missing")


def test_exclusion_signed_by_a_non_owner_key_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world, signer=world.verifier)
    assert_not_ready(world.built().produce(), "exclusion_signer_not_authorized")


def test_exclusion_signed_by_an_unknown_key_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world, signer=generate_ed25519_signer(endpoint_id="synthetic-fixture-unknown"))
    assert_not_ready(world.built().produce(), "exclusion_signature_untrusted")


def test_exclusion_scope_changed_after_signing_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world)
    world.exclusions[0]["requirement_ids"] = ["digest_class:browser#engine:firefox"]
    assert_not_ready(world.built().produce(), "exclusion_digest_mismatch")


def test_expired_exclusion_withholds_release(world: SyntheticWorld) -> None:
    exclude_webkit(world, issued_at="2026-10-01T08:00:00Z", expires_at="2026-10-11T12:00:00Z")
    assert_not_ready(world.built().produce(), "exclusion_expired")


def test_exclusion_issued_in_the_future_is_rejected(world: SyntheticWorld) -> None:
    exclude_webkit(world, issued_at="2026-10-11T12:00:01Z")
    assert_not_ready(world.built().produce(), "exclusion_issued_in_future")


def test_exclusion_lifetime_is_bounded(world: SyntheticWorld) -> None:
    exclude_webkit(world, expires_at="2026-11-10T08:00:01Z")
    assert_not_ready(world.built().produce(), "exclusion_lifetime_unbounded")


def test_exclusion_scope_must_be_exact(world: SyntheticWorld) -> None:
    exclude_webkit(world, scope="all_browser_requirements")
    assert_not_ready(world.built().produce(), "exclusion_scope_unbounded")


def test_exclusion_reason_is_size_bounded(world: SyntheticWorld) -> None:
    exclude_webkit(world, reason="synthetic " * 200)
    assert_not_ready(world.built().produce(), "exclusion_reason_unbounded")


@pytest.mark.parametrize(
    "requirement",
    [
        "source_integrity",
        "clean_tree",
        "content_digest_integrity",
        "owner_authority",
        "privacy_and_credential_safety",
        "qualified_only_selection",
        "fail_closed_execution",
        "provenance_truth",
        "external_reviewer_reproduction",
        "gate_result:A12",
        "digest_class:physical",
        "digest_class:sbom",
    ],
)
def test_exclusion_cannot_cover_integrity_or_provenance_classes(
    world: SyntheticWorld, requirement: str
) -> None:
    world.exclusions.append(world.make_exclusion([requirement]))
    assert_not_ready(world.built().produce(), "mandatory_requirement_excluded")


def test_exclusion_of_an_unknown_requirement_is_rejected(world: SyntheticWorld) -> None:
    world.exclusions.append(world.make_exclusion(["digest_class:browser#engine:servo"]))
    assert_not_ready(world.built().produce(), "exclusion_requirement_unknown")


def test_exclusions_cannot_remove_the_whole_engine_matrix(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"] = [
        {"case_id": case_id, "outcome": "blocked"}
        for case_id in ("engine:chromium", "engine:firefox", "engine:webkit")
    ]
    world.exclusions.append(
        world.make_exclusion(
            [f"digest_class:browser#engine:{engine}" for engine in ("chromium", "firefox", "webkit")]
        )
    )
    assert_not_ready(world.built().produce(), "exclusion_scope_unbounded")


def test_exclusion_count_is_bounded(world: SyntheticWorld) -> None:
    world.exclusions.extend(
        world.make_exclusion([WEBKIT], reason=f"synthetic fixture {index}") for index in range(17)
    )
    assert_not_ready(world.built().produce(), "exclusion_count_exceeded")


def test_exclusion_requirement_count_is_bounded(world: SyntheticWorld) -> None:
    world.exclusions.append(world.make_exclusion([WEBKIT] * 9))
    assert_not_ready(world.built().produce(), "exclusion_requirement_count_exceeded")

