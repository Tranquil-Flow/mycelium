# SPDX-License-Identifier: AGPL-3.0-only
"""Negatives for the adversarial admission review of the A15 release-decision producer.

Each case reproduces one reviewed bypass over the labelled synthetic test fixture world and
asserts that it now withholds or refuses. Digests and seals used by these cases are
recomputed here with ``hashlib`` and ``json`` only, never with the producer's helpers, so a
producer bug in what a digest covers cannot cancel out. Nothing here is live or physical
evidence and no test evaluates the real repository.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import mycelium_a15_release_decision as producer
from mycelium_a15_release_decision import (
    ReleaseDecisionRefused,
    append_revocation,
    public_projection_bytes,
    read_ledger,
    verify_decision_bytes,
)
from scripts.a15_release_decision import main
from tests.a15_release_decision.synthetic_world import (
    JUNIT_PASSING,
    NOW,
    SyntheticWorld,
    git,
    passing,
)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def reseal_document(document: dict[str, Any]) -> bytes:
    document["body_sha256"] = digest_of(canonical(document["body"]))
    return canonical(document) + b"\n"


def reseal_nodes(candidate: dict[str, Any]) -> None:
    by_gate = {node["gate_id"]: node for node in candidate["nodes"]}
    for gate_id in sorted(by_gate, key=lambda item: int(item[1:])):
        node = by_gate[gate_id]
        node["dependency_digests"] = [
            by_gate[item]["node_digest"] for item in node["dependency_gate_ids"]
        ]
        node["node_digest"] = digest_of(
            canonical({key: value for key, value in node.items() if key != "node_digest"})
        )


def refusal(callable_: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(ReleaseDecisionRefused) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


def assert_not_ready(result: Any, *expected: str) -> None:
    assert result.decision == "not_ready", result.findings
    assert result.document["body"]["graph"] is None
    for finding in expected:
        assert finding in result.findings, result.findings


def state_of(result: Any, requirement_id: str) -> str:
    return {
        item["requirement_id"]: item["state"] for item in result.document["body"]["requirements"]
    }[requirement_id]


# -- F1: revocation keyed on semantic identity -------------------------------------------


def test_reordered_candidate_cannot_bypass_an_explicit_revocation(world: SyntheticWorld) -> None:
    first = world.built().produce()
    assert first.decision == "ready"
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="critical_defect",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
    )
    world.candidate["nodes"].reverse()
    altered = canonical(world.candidate)
    assert digest_of(altered) != first.document["body"]["candidate_digest"]
    world.candidate_path.write_bytes(altered)
    again = world.produce(
        now=NOW + timedelta(minutes=2), expected_ledger_head=revoked.decision_digest
    )

    assert_not_ready(again, "source_revoked", "result_revoked:gate_result:A12")
    assert "candidate_revoked" not in again.findings


def test_explicit_revocation_records_the_revoked_semantic_identity(world: SyntheticWorld) -> None:
    first = world.built().produce()
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="invalid_evidence",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
    )
    recorded = revoked.document["body"]["revocation"]
    node_digests = sorted(node["artifact_digest"] for node in world.candidate["nodes"])
    assert recorded["revoked_source_commit"] == world.commit
    assert set(node_digests) <= set(recorded["revoked_result_digests"])
    assert world.candidate["reviewer_reproduction"]["artifact_digest"] in (
        recorded["revoked_result_digests"]
    )


def test_reissued_registry_cannot_bypass_a_compromised_key_revocation(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    assert refusal(
        append_revocation,
        ledger_dir=world.ledger,
        reason="compromised_key",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
    ) == "revocation_key_digests_required"
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="compromised_key",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
        compromised_key_digests=[world.verifier.verification_key_digest],
        trust_registry_path=world.registry_path,
    )
    world.registry["registry_generation"] = 2
    world.build()
    assert world.registry_digest != first.document["body"]["trust_registry_digest"]
    again = world.produce(
        now=NOW + timedelta(minutes=2), expected_ledger_head=revoked.decision_digest
    )
    assert_not_ready(again, "trust_registry_key_revoked")


# -- F2 / F9: exclusion bounds and bindings ----------------------------------------------


def engine_cases(**outcomes: str) -> list[dict[str, str]]:
    return [
        {"case_id": f"engine:{engine}", "outcome": outcome}
        for engine, outcome in sorted(outcomes.items())
    ]


def test_two_blocked_engines_cannot_be_excluded_together(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"] = engine_cases(
        chromium="pass", firefox="blocked", webkit="blocked"
    )
    world.exclusions.append(
        world.make_exclusion(
            ["digest_class:browser#engine:firefox", "digest_class:browser#engine:webkit"]
        )
    )
    assert_not_ready(world.built().produce(), "exclusion_scope_unbounded")


def test_two_absent_engines_cannot_be_excluded_separately(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"] = engine_cases(chromium="pass")
    world.exclusions.append(world.make_exclusion(["digest_class:browser#engine:firefox"]))
    world.exclusions.append(
        world.make_exclusion(["digest_class:browser#engine:webkit"], reason="synthetic fixture 2")
    )
    assert_not_ready(world.built().produce(), "exclusion_scope_unbounded")


def test_exclusion_for_a_passing_engine_does_not_mark_the_class_excluded(
    world: SyntheticWorld,
) -> None:
    world.exclusions.append(world.make_exclusion(["digest_class:browser#engine:webkit"]))
    result = world.built().produce()
    assert result.decision == "ready", result.findings
    assert state_of(result, "digest_class:browser") == "verified"
    assert result.document["body"]["withheld_claims"] == []


@pytest.mark.parametrize(
    "binding",
    [
        {"source_commit": "6" * 40},
        {"authority_generation": 6},
        {"bound_result_digest": "sha256:" + "7" * 64},
    ],
)
def test_exclusion_is_bound_to_source_generation_and_result(
    world: SyntheticWorld, binding: dict[str, Any]
) -> None:
    world.specs["digest_class:browser"]["cases"] = engine_cases(
        chromium="pass", firefox="pass", webkit="blocked"
    )
    world.exclusion_bindings = binding
    world.exclusions.append(world.make_exclusion(["digest_class:browser#engine:webkit"]))
    assert_not_ready(world.built().produce(), "exclusion_binding_mismatch")


# -- F3: the clean tree is hashed, not trusted from the index ----------------------------


def test_assume_unchanged_cannot_hide_a_modified_tracked_file(world: SyntheticWorld) -> None:
    world.build()
    git(world.repo, "update-index", "--assume-unchanged", "candidate.txt")
    (world.repo / "candidate.txt").write_bytes(b"synthetic-test-fixture: hidden edit\n")
    assert git(world.repo, "status", "--porcelain") == ""
    assert_not_ready(world.produce(), "clean_tree_required")


def test_skip_worktree_cannot_hide_a_modified_tracked_file(world: SyntheticWorld) -> None:
    world.build()
    git(world.repo, "update-index", "--skip-worktree", "gates/A3.txt")
    (world.repo / "gates" / "A3.txt").write_bytes(b"synthetic-test-fixture: hidden edit\n")
    assert git(world.repo, "status", "--porcelain") == ""
    assert_not_ready(world.produce(), "clean_tree_required")


def test_index_flag_alone_is_refused(world: SyntheticWorld) -> None:
    world.build()
    git(world.repo, "update-index", "--skip-worktree", "gates/A3.txt")
    assert_not_ready(world.produce(), "source_index_flag_present")


def test_locally_excluded_untracked_file_is_still_a_source_input(world: SyntheticWorld) -> None:
    world.build()
    (world.repo / ".git" / "info").mkdir(exist_ok=True)
    (world.repo / ".git" / "info" / "exclude").write_text("sitecustomize.py\n")
    (world.repo / "sitecustomize.py").write_bytes(b"# synthetic-test-fixture\n")
    assert git(world.repo, "status", "--porcelain") == ""
    assert_not_ready(world.produce(), "untracked_source_input_present")


def test_file_mode_change_cannot_be_hidden_by_repository_config(world: SyntheticWorld) -> None:
    world.build()
    git(world.repo, "config", "core.fileMode", "false")
    os.chmod(world.repo / "candidate.txt", 0o755)
    assert git(world.repo, "status", "--porcelain") == ""
    assert_not_ready(world.produce(), "clean_tree_required")


@pytest.mark.parametrize("name", ["info/grafts", "shallow"])
def test_grafted_or_shallow_history_is_refused(world: SyntheticWorld, name: str) -> None:
    world.build()
    target = world.repo / ".git" / name
    target.parent.mkdir(exist_ok=True)
    first_gate = world.gate_commits["A3"]
    target.write_text(f"{first_gate}\n")
    assert_not_ready(world.produce(), "source_history_rewritten")


def test_checkout_changed_during_evaluation_is_refused(
    world: SyntheticWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.build()
    original = producer._evaluate

    def evaluate_then_move(**arguments: Any) -> Any:
        evaluation = original(**arguments)
        (world.repo / "candidate.txt").write_bytes(b"synthetic-test-fixture: moved\n")
        return evaluation

    monkeypatch.setattr(producer, "_evaluate", evaluate_then_move)
    assert refusal(world.produce) == "source_changed_during_evaluation"
    assert list(world.ledger.iterdir()) == []


# -- F4: a sealed body is bound to its own closure statement -----------------------------


def test_synthetic_ready_relabelled_production_does_not_verify(world: SyntheticWorld) -> None:
    result = world.built().produce()
    assert result.decision == "ready"
    forged = copy.deepcopy(result.document)
    forged["body"]["authority_scope"] = "production"
    relabelled = reseal_document(forged)
    assert refusal(verify_decision_bytes, relabelled) == "decision_structure_invalid"

    for field, value in [
        ("candidate_digest", "sha256:" + "8" * 64),
        ("trust_registry_digest", "sha256:" + "9" * 64),
    ]:
        forged = copy.deepcopy(result.document)
        forged["body"][field] = value
        assert refusal(verify_decision_bytes, reseal_document(forged)) == (
            "decision_structure_invalid"
        )
    forged = copy.deepcopy(result.document)
    forged["body"]["source"]["commit"] = "a" * 40
    assert refusal(verify_decision_bytes, reseal_document(forged)) == "decision_structure_invalid"


def test_relabelled_document_is_not_readable_as_a_ledger(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    result = world.built().produce()
    forged = copy.deepcopy(result.document)
    forged["body"]["authority_scope"] = "production"
    other = tmp_path / "forged-ledger"
    other.mkdir()
    (other / "generation-000001.json").write_bytes(reseal_document(forged))
    assert refusal(read_ledger, other) == "decision_history_tampered"


def test_public_projection_names_the_trust_registry(world: SyntheticWorld) -> None:
    result = world.built().produce()
    public = json.loads(public_projection_bytes(result.sealed_bytes))
    assert public["body"]["trust_registry_digest"] == digest_of(world.registry_path.read_bytes())
    assert public["body"]["authority_scope"] == "synthetic_test_fixture"


# -- F5: the ledger head is pinned by the caller -----------------------------------------


def test_truncated_ledger_cannot_erase_a_revocation(world: SyntheticWorld) -> None:
    first = world.built().produce()
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="critical_defect",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
    )
    revoked.ledger_entry_path.unlink()
    assert refusal(
        world.produce,
        now=NOW + timedelta(minutes=2),
        expected_ledger_head=revoked.decision_digest,
    ) == "ledger_head_mismatch"
    assert sorted(path.name for path in world.ledger.iterdir()) == ["generation-000001.json"]


def test_fresh_ledger_directory_cannot_erase_history(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    first = world.built().produce()
    fresh = tmp_path / "fresh-ledger"
    fresh.mkdir()
    assert refusal(
        world.produce,
        ledger_dir=fresh,
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
    ) == "ledger_head_mismatch"
    assert list(fresh.iterdir()) == []


def test_expected_head_none_is_only_valid_for_an_empty_ledger(world: SyntheticWorld) -> None:
    first = world.built().produce(expected_ledger_head=None)
    assert first.decision_generation == 1
    assert refusal(
        world.produce, now=NOW + timedelta(minutes=1), expected_ledger_head=None
    ) == "ledger_head_mismatch"
    assert refusal(
        world.produce, now=NOW + timedelta(minutes=1), expected_ledger_head="latest"
    ) == "ledger_head_mismatch"


# -- F6: production trust needs a reviewed in-source pin ---------------------------------


def test_no_production_registry_is_pinned_in_source() -> None:
    assert producer.PRODUCTION_REGISTRY_PINS == frozenset()


def test_self_declared_production_registry_is_not_recognised(
    tmp_path: Path, template: tuple[Path, dict[str, str]]
) -> None:
    world = SyntheticWorld(tmp_path, template, registry_class="production")
    for spec in world.specs.values():
        spec["evidence_class"] = "physical_qualification"
    result = world.built().produce(allow_synthetic_trust=False)
    assert_not_ready(result, "trust_registry_not_recognised")
    assert result.document["body"]["authority_scope"] == "undetermined"
    assert state_of(result, "gate_result:A3") == "not_evaluated"


# -- F7: junit reports -------------------------------------------------------------------


def junit_world(world: SyntheticWorld, files: dict[str, bytes]) -> Any:
    world.specs["digest_class:test"]["files"] = files
    return world.built().produce()


def test_bare_testcase_without_a_counted_suite_is_not_a_pass(world: SyntheticWorld) -> None:
    assert_not_ready(
        junit_world(world, {"junit.xml": b'<testcase classname="synthetic" name="only"/>'}),
        "junit_report_invalid:digest_class:test",
    )


def test_suite_count_must_equal_the_counted_cases(world: SyntheticWorld) -> None:
    assert_not_ready(
        junit_world(world, {"junit.xml": JUNIT_PASSING.replace(b'tests="2"', b'tests="3"')}),
        "junit_counts_inconsistent:digest_class:test",
    )


@pytest.mark.parametrize("child", ["rerunFailure", "flakyFailure", "rerunError", "flakyError"])
def test_rerun_and_flaky_results_are_failures(world: SyntheticWorld, child: str) -> None:
    report = JUNIT_PASSING.replace(
        b'<testcase classname="synthetic.fixture" name="second"/>',
        f'<testcase classname="synthetic.fixture" name="second"><{child}/></testcase>'.encode(),
    )
    assert_not_ready(
        junit_world(world, {"junit.xml": report}), "junit_failures_present:digest_class:test"
    )


def test_failing_report_with_an_uppercase_suffix_is_read(world: SyntheticWorld) -> None:
    failing = JUNIT_PASSING.replace(
        b'<testcase classname="synthetic.fixture" name="second"/>',
        b'<testcase classname="synthetic.fixture" name="second"><failure/></testcase>',
    )
    assert_not_ready(
        junit_world(world, {"junit.xml": JUNIT_PASSING, "junit-fail.XML": failing}),
        "junit_failures_present:digest_class:test",
    )


def test_utf16_report_with_an_internal_entity_is_refused(world: SyntheticWorld) -> None:
    report = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE testsuite [<!ENTITY synthetic "<testcase name=\'entity\'/>">]>'
        '<testsuite name="synthetic-test-fixture" tests="1">&synthetic;</testsuite>'
    ).encode("utf-16")
    assert_not_ready(
        junit_world(world, {"junit.xml": report}), "junit_report_invalid:digest_class:test"
    )


# -- F8: gate commits are bound by the gate receipts -------------------------------------


def test_gate_commits_must_be_the_commits_the_gate_receipts_bind(world: SyntheticWorld) -> None:
    world.build()
    true_commits = dict(world.gate_commits)
    order = [true_commits[f"A{index}"] for index in range(3, 15)]
    shifted = order[1:] + [world.commit]
    world.candidate["gate_commits"] = {
        f"A{index}": commit for index, commit in zip(range(3, 15), shifted)
    }
    world.candidate_path.write_bytes(canonical(world.candidate))
    assert_not_ready(world.produce(), "gate_commit_binding_mismatch:gate_result:A6")


# -- F10: registry origin is judged on real file identity --------------------------------


def test_hard_linked_trust_registry_is_refused(world: SyntheticWorld, tmp_path: Path) -> None:
    world.build()
    planted = world.roots["gates"] / "trust-registry.json"
    os.link(world.registry_path, planted)
    result = world.produce()
    planted.unlink()
    assert_not_ready(result, "trust_registry_origin_invalid")


def test_case_variant_path_into_an_evidence_root_is_refused(world: SyntheticWorld) -> None:
    world.build()
    variant = world.roots["gates"].parent / world.roots["gates"].name.upper()
    if not variant.exists():
        pytest.skip("case-sensitive volume: no case-variant path to the evidence root")
    planted = world.roots["gates"] / "trust-registry.json"
    planted.write_bytes(world.registry_path.read_bytes())
    result = world.produce(trust_registry_path=variant / "trust-registry.json")
    planted.unlink()
    assert_not_ready(result, "trust_registry_origin_invalid")


# -- F11 / F12 ---------------------------------------------------------------------------


def cli_arguments(world: SyntheticWorld, public_out: Path, *extra: str) -> list[str]:
    return [
        "decide",
        "--candidate",
        str(world.candidate_path),
        "--repo-root",
        str(world.repo),
        "--evidence-root",
        f"gates={world.roots['gates']}",
        "--evidence-root",
        f"matrices={world.roots['matrices']}",
        "--trust-registry",
        str(world.registry_path),
        "--trust-registry-sha256",
        world.registry_digest,
        "--ledger-dir",
        str(world.ledger),
        "--public-out",
        str(public_out),
        *extra,
    ]


def test_cli_synthetic_ready_does_not_exit_zero(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    code = main(
        cli_arguments(
            world, tmp_path / "public.json", "--synthetic-self-test", "--expect-head", "none"
        ),
        clock=lambda: NOW,
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 4
    assert report["decision"] == "ready"
    assert report["authority_scope"] == "synthetic_test_fixture"


def test_cli_does_not_accept_abbreviated_options(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    code = main(
        cli_arguments(world, tmp_path / "public.json", "--synth", "--expect-head", "none"),
        clock=lambda: NOW,
    )
    assert code == 2
    assert json.loads(capsys.readouterr().err) == {"refused": "arguments_invalid"}
    assert list(world.ledger.iterdir()) == []


def test_cli_requires_the_expected_ledger_head(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    code = main(
        cli_arguments(world, tmp_path / "public.json", "--synthetic-self-test"),
        clock=lambda: NOW,
    )
    assert code == 2
    assert json.loads(capsys.readouterr().err) == {"refused": "arguments_invalid"}
    assert list(world.ledger.iterdir()) == []


def test_decision_result_carries_its_authority_scope(world: SyntheticWorld) -> None:
    result = world.built().produce()
    assert result.decision == "ready"
    assert result.authority_scope == "synthetic_test_fixture"


def test_trust_registry_lifetime_is_bounded(world: SyntheticWorld) -> None:
    world.registry["expires_at"] = "2099-01-01T00:00:00Z"
    assert_not_ready(world.built().produce(), "trust_registry_lifetime_unbounded")


def test_evidence_root_cannot_be_the_ledger_directory(world: SyntheticWorld) -> None:
    world.build()
    roots = {**world.roots, "ledger": world.ledger}
    assert_not_ready(world.produce(evidence_roots=roots), "evidence_root_overlaps_ledger")


def test_reviewer_reproduction_binds_the_class_results(world: SyntheticWorld) -> None:
    world.build()
    node_digests = sorted(node["node_digest"] for node in world.candidate["nodes"])
    world.reviewer_binds_nodes = False
    world.specs["external_reviewer_reproduction"]["fields"]["bound_result_digests"] = node_digests
    assert_not_ready(world.built().produce(), "reviewer_result_binding_mismatch")


# -- spec section 8: wrong route / qualification -----------------------------------------


@pytest.mark.parametrize(
    ("field", "finding"),
    [
        ("route_digest", "route_binding_inconsistent"),
        ("qualification_digest", "qualification_binding_inconsistent"),
        ("assignment_digest", "assignment_binding_inconsistent"),
    ],
)
def test_result_for_another_route_or_qualification_fails_closed(
    world: SyntheticWorld, field: str, finding: str
) -> None:
    world.specs["gate_result:A9"]["fields"][field] = digest_of(b"synthetic-test-fixture:other")
    assert_not_ready(world.built().produce(), f"{finding}:gate_result:A9")


def test_browser_matrix_passing_cases_are_unchanged_by_the_repairs(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"] = passing(
        ["engine:chromium", "engine:firefox", "engine:webkit"]
    )
    assert world.built().produce().decision == "ready"


# -- re-review N1: a key revocation still revokes the decision it names ------------------


def test_compromised_key_revocation_also_blocks_the_revoked_source_and_results(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="compromised_key",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
        compromised_key_digests=[world.reviewer.verification_key_digest],
        trust_registry_path=world.registry_path,
    )
    recorded = revoked.document["body"]["revocation"]
    assert recorded["revoked_source_commit"] == world.commit
    assert world.candidate["nodes"][0]["artifact_digest"] in recorded["revoked_result_digests"]

    world.candidate["nodes"].reverse()
    world.candidate_path.write_bytes(canonical(world.candidate))
    again = world.produce(
        now=NOW + timedelta(minutes=2), expected_ledger_head=revoked.decision_digest
    )
    # The key revocation makes the registry unusable, so results are not evaluated; the
    # reordered candidate is still stopped by the recorded source, independent of the key.
    assert_not_ready(again, "source_revoked", "trust_registry_key_revoked")
    assert "candidate_revoked" not in again.findings


def test_compromised_key_revocation_must_name_a_key_of_the_revoked_registry(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    first = world.built().produce()
    other_registry = tmp_path / "other-registry.json"
    other_registry.write_bytes(canonical({**world.registry, "registry_generation": 9}))
    common = {
        "ledger_dir": world.ledger,
        "reason": "compromised_key",
        "now": NOW + timedelta(minutes=1),
        "expected_ledger_head": first.decision_digest,
        "revoked_decision_digest": first.decision_digest,
    }
    typo = "sha256:" + "1" * 64
    assert refusal(
        append_revocation,
        **common,
        compromised_key_digests=[typo],
        trust_registry_path=world.registry_path,
    ) == "revocation_key_not_in_registry"
    assert refusal(
        append_revocation,
        **common,
        compromised_key_digests=[world.verifier.verification_key_digest, typo],
        trust_registry_path=world.registry_path,
    ) == "revocation_key_not_in_registry"
    assert refusal(
        append_revocation,
        **common,
        compromised_key_digests=[world.verifier.verification_key_digest],
        trust_registry_path=other_registry,
    ) == "revocation_registry_mismatch"
    assert refusal(
        append_revocation,
        **common,
        compromised_key_digests=[world.verifier.verification_key_digest],
    ) == "revocation_registry_mismatch"
    assert sorted(path.name for path in world.ledger.iterdir()) == ["generation-000001.json"]


# -- re-review N2: the caller names what is revoked --------------------------------------


def test_revocation_must_name_an_existing_generation(world: SyntheticWorld) -> None:
    first = world.built().produce()
    assert refusal(
        append_revocation,
        ledger_dir=world.ledger,
        reason="critical_defect",
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest="sha256:" + "2" * 64,
    ) == "revocation_target_unknown"
    assert sorted(path.name for path in world.ledger.iterdir()) == ["generation-000001.json"]


def test_revocation_revokes_the_named_newer_generation_and_not_an_older_ready(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    world.specs["digest_class:audit"]["cases"] = [
        {"case_id": "synthetic_fixture_case", "outcome": "fail"}
    ]
    second = world.built().produce(now=NOW + timedelta(minutes=1))
    assert second.decision == "not_ready"
    revoked = append_revocation(
        ledger_dir=world.ledger,
        reason="invalid_evidence",
        now=NOW + timedelta(minutes=2),
        expected_ledger_head=second.decision_digest,
        revoked_decision_digest=second.decision_digest,
    )
    recorded = revoked.document["body"]["revocation"]
    assert recorded["revoked_generation"] == 2
    assert recorded["revoked_decision_digest"] == second.decision_digest
    assert recorded["revoked_candidate_digest"] == second.document["body"]["candidate_digest"]
    assert recorded["revoked_candidate_digest"] != first.document["body"]["candidate_digest"]
    assert recorded["revoked_source_commit"] == world.commit


def test_cli_revoke_names_and_prints_what_it_revoked(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = world.built().produce()
    arguments = [
        "revoke",
        "--ledger-dir",
        str(world.ledger),
        "--expect-head",
        first.decision_digest,
        "--reason",
        "critical_defect",
        "--public-out",
        str(tmp_path / "public.json"),
    ]
    assert main(arguments, clock=lambda: NOW + timedelta(minutes=1)) == 2
    assert json.loads(capsys.readouterr().err) == {"refused": "arguments_invalid"}
    code = main(
        [*arguments, "--revoke-decision-digest", first.decision_digest],
        clock=lambda: NOW + timedelta(minutes=1),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["revoked_generation"] == 1
    assert report["revoked_decision_digest"] == first.decision_digest


# -- re-review N3: junit root totals and status attributes -------------------------------


@pytest.mark.parametrize(
    ("root_attributes", "finding"),
    [
        (b'failures="5"', "junit_failures_present"),
        (b'errors="1"', "junit_failures_present"),
        (b'skipped="2"', "junit_skips_present"),
        (b'tests="900"', "junit_counts_inconsistent"),
    ],
)
def test_totals_on_the_testsuites_root_are_read(
    world: SyntheticWorld, root_attributes: bytes, finding: str
) -> None:
    report = JUNIT_PASSING.replace(b"<testsuites>", b"<testsuites " + root_attributes + b">")
    assert report != JUNIT_PASSING
    assert_not_ready(junit_world(world, {"junit.xml": report}), f"{finding}:digest_class:test")


@pytest.mark.parametrize(
    ("attribute", "finding"),
    [
        (b'status="failed"', "junit_failures_present"),
        (b'status="error"', "junit_failures_present"),
        (b'result="failed"', "junit_failures_present"),
        (b'status="skipped"', "junit_skips_present"),
        (b'status="notrun"', "junit_skips_present"),
    ],
)
def test_testcase_status_attribute_without_a_child_is_not_a_pass(
    world: SyntheticWorld, attribute: bytes, finding: str
) -> None:
    report = JUNIT_PASSING.replace(
        b'<testcase classname="synthetic.fixture" name="second"/>',
        b'<testcase classname="synthetic.fixture" name="second" ' + attribute + b"/>",
    )
    assert report != JUNIT_PASSING
    assert_not_ready(junit_world(world, {"junit.xml": report}), f"{finding}:digest_class:test")


def test_testcase_with_a_passing_status_attribute_still_passes(world: SyntheticWorld) -> None:
    report = JUNIT_PASSING.replace(
        b'<testcase classname="synthetic.fixture" name="second"/>',
        b'<testcase classname="synthetic.fixture" name="second" status="run" result="passed"/>',
    )
    assert junit_world(world, {"junit.xml": report}).decision == "ready"


# -- interim hardening for the unsigned decision -----------------------------------------


def consistently_relabel_as_production(document: dict[str, Any]) -> bytes:
    """Rewrite every bound field so the body agrees with itself under another scope."""
    forged = copy.deepcopy(document)
    body = forged["body"]
    body["authority_scope"] = "production"
    body["closure_statement"]["authority_scope"] = "production"
    root = body["graph"]["root"]
    closure_node = body["graph"]["nodes"][-1]
    assert closure_node["gate_id"] == "A15"
    closure_node["artifact_digest"] = digest_of(canonical(body["closure_statement"]))
    closure_node["environment_digest"] = digest_of(
        canonical(
            {
                "authority_scope": "production",
                "trust_registry_digest": body["trust_registry_digest"],
            }
        )
    )
    closure_node["node_digest"] = digest_of(
        canonical({key: value for key, value in closure_node.items() if key != "node_digest"})
    )
    root["result_node_digests"]["A15"] = closure_node["node_digest"]
    root["root_digest"] = digest_of(
        canonical({key: value for key, value in root.items() if key != "root_digest"})
    )
    return reseal_document(forged)


def test_consistently_reforged_production_ready_does_not_verify_anywhere(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    result = world.built().produce()
    assert result.decision == "ready"
    forged = consistently_relabel_as_production(result.document)

    assert refusal(verify_decision_bytes, forged) == "decision_structure_invalid"
    assert refusal(public_projection_bytes, forged) == "decision_structure_invalid"
    other = tmp_path / "forged-ledger"
    other.mkdir()
    (other / "generation-000001.json").write_bytes(forged)
    assert refusal(read_ledger, other) == "decision_history_tampered"


def test_unpinned_production_scope_is_rejected_for_a_non_ready_document(
    world: SyntheticWorld,
) -> None:
    world.candidate_mutators.append(lambda candidate: candidate.update(reviewer_reproduction=None))
    result = world.built().produce()
    assert result.decision == "not_ready"
    forged = copy.deepcopy(result.document)
    forged["body"]["authority_scope"] = "production"
    assert refusal(verify_decision_bytes, reseal_document(forged)) == "decision_structure_invalid"
