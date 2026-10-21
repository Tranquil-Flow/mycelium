# SPDX-License-Identifier: AGPL-3.0-only
"""The A0 literal verification record is a prepared structure, never a claim."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import fill_a0_literal_record as fill

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs" / "handover" / "a0-literal-verification-record.v1.json"
FIELDS = {"protocol", "gate_id", "state", "claim_boundary", "specification",
          "predecessor_record", "candidate", "obligations", "atomic_commit",
          "remaining_before_complete"}
OBLIGATION_FIELDS = {"obligation_id", "authority", "origin", "commands",
                     "required_observations", "executed_on_candidate", "result", "evidence"}
REQUIRED_OBLIGATIONS = ("7.1_rejection_classes", "7.2_contract_owner_suites",
                        "7.3_aggregate_and_child_audits", "7.4_negative_repository_copies",
                        "7.5_real_browser_path", "full_regressions")


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_record_is_closed_and_binds_the_exact_a0_specification():
    record = _record()
    assert set(record) == FIELDS
    assert record["protocol"] == "mycelium.a0_literal_verification_record.v1"
    assert record["gate_id"] == "A0"
    spec = record["specification"]
    digest = "sha256:" + hashlib.sha256((ROOT / spec["path"]).read_bytes()).hexdigest()
    assert spec["digest"] == digest
    assert (ROOT / spec["governing_plan"]).is_file()
    predecessor = ROOT / record["predecessor_record"]
    # The sealed predecessor record is kept out of the published tree; when it is
    # absent only its recorded path is checked.
    assert predecessor.is_file() or record["predecessor_record"] == (
        "docs/handover/a0-completion-record.v1.json")


def test_the_committed_record_is_prepared_or_fully_executed():
    record = _record()
    assert record["state"] in {"prepared_not_executed", "executed_on_candidate",
                               "complete_on_candidate"}
    fill.validate_record(record)


def test_prepared_record_claims_no_execution_and_no_completion():
    record = _record()
    if record["state"] != "prepared_not_executed":
        pytest.skip("the committed record is executed; see the executed-state tests")
    assert record["candidate"]["commit"] is None and record["candidate"]["clean_tree"] is None
    assert record["atomic_commit"]["commit"] is None
    assert record["atomic_commit"]["reading"] is None
    ids = tuple(item["obligation_id"] for item in record["obligations"])
    assert ids == REQUIRED_OBLIGATIONS
    for item in record["obligations"]:
        assert set(item) == OBLIGATION_FIELDS
        assert item["executed_on_candidate"] is False
        assert item["result"] is None and item["evidence"] is None
        assert item["commands"] and item["required_observations"]
    negatives = next(item for item in record["obligations"]
                     if item["obligation_id"] == "7.4_negative_repository_copies")
    assert {"unlisted_post_surface_copy_fails_deterministically",
            "unregistered_boundary_protocol_copy_fails_deterministically",
            "promoted_milestone_copy_fails_deterministically"} <= set(
                negatives["required_observations"])
    browser = next(item for item in record["obligations"]
                   if item["obligation_id"] == "7.5_real_browser_path")
    assert "never_release_ready" in browser["required_observations"]
    assert record["remaining_before_complete"]


def test_every_named_command_target_exists_in_the_tree():
    for item in _record()["obligations"]:
        for command in item["commands"]:
            for token in command.replace("&&", " ").split():
                if token.startswith(("tests/", "scripts/")) and token.endswith((".py", ".mjs")):
                    base = ROOT / "ui" / "web" if token.endswith(".mjs") else ROOT
                    assert (base / token).is_file(), token


# ---- executed state: admitted only when every obligation is bound to one clean candidate ----

CANDIDATE = "a" * 40


def _receipt(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"obligation": name}), encoding="utf-8")
    return path


def _results(tmp_path: Path, result: str = "passed") -> dict:
    return {
        obligation: {"result": result, "evidence": str(_receipt(tmp_path, obligation))}
        for obligation in REQUIRED_OBLIGATIONS
    }


def test_filling_binds_every_obligation_to_the_candidate(tmp_path: Path):
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path), bound_at_unix_ms=1)
    fill.validate_record(filled)
    assert filled["state"] == "executed_on_candidate"
    assert filled["candidate"] == {**_record_prepared()["candidate"], "commit": CANDIDATE,
                                   "clean_tree": True, "bound_at_unix_ms": 1}
    for item in filled["obligations"]:
        assert item["executed_on_candidate"] is True
        assert item["result"] == "passed"
        receipt = tmp_path / f"{item['obligation_id']}.json"
        # Owner-private paths never enter the committed record: only the name and digest.
        assert item["evidence"] == {
            "receipt": receipt.name,
            "digest": "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest(),
        }
    assert filled["atomic_commit"] == _record_prepared()["atomic_commit"]


def test_a_failed_obligation_is_recorded_as_failed_not_hidden(tmp_path: Path):
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path, "failed"), bound_at_unix_ms=1)
    fill.validate_record(filled)
    assert {item["result"] for item in filled["obligations"]} == {"failed"}


@pytest.mark.parametrize("candidate", ["abc", "A" * 40, "g" * 40, None])
def test_a_non_commit_candidate_is_refused(tmp_path: Path, candidate):
    with pytest.raises(ValueError, match="a0_record_candidate_invalid"):
        fill.fill_record(_record_prepared(), candidate=candidate,
                         results=_results(tmp_path), bound_at_unix_ms=1)


def test_a_partial_result_set_is_refused(tmp_path: Path):
    results = _results(tmp_path)
    results.pop("7.5_real_browser_path")
    with pytest.raises(ValueError, match="a0_record_results_incomplete"):
        fill.fill_record(_record_prepared(), candidate=CANDIDATE, results=results,
                         bound_at_unix_ms=1)


def test_an_unknown_result_or_missing_receipt_is_refused(tmp_path: Path):
    results = _results(tmp_path)
    results["7.1_rejection_classes"]["result"] = "mostly"
    with pytest.raises(ValueError, match="a0_record_result_invalid"):
        fill.fill_record(_record_prepared(), candidate=CANDIDATE, results=results,
                         bound_at_unix_ms=1)
    results = _results(tmp_path)
    results["7.1_rejection_classes"]["evidence"] = str(tmp_path / "absent.json")
    with pytest.raises(ValueError, match="a0_record_evidence_missing"):
        fill.fill_record(_record_prepared(), candidate=CANDIDATE, results=results,
                         bound_at_unix_ms=1)


def test_a_half_executed_record_is_refused(tmp_path: Path):
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path), bound_at_unix_ms=1)
    filled["obligations"][0]["executed_on_candidate"] = False
    with pytest.raises(ValueError, match="a0_record_obligation_not_executed"):
        fill.validate_record(filled)
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path), bound_at_unix_ms=1)
    filled["candidate"]["clean_tree"] = False
    with pytest.raises(ValueError, match="a0_record_candidate_tree_not_clean"):
        fill.validate_record(filled)


def test_the_cli_refuses_unless_head_is_the_candidate_on_a_clean_tree(tmp_path: Path):
    repo = tmp_path / "repo"
    record_dir = repo / "docs" / "handover"
    record_dir.mkdir(parents=True)
    (record_dir / RECORD.name).write_text(json.dumps(_record_prepared()), encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps(_results(tmp_path)), encoding="utf-8")
    base = ["--repo-root", str(repo), "--results", str(results_file)]

    assert fill.main([*base, "--candidate", "b" * 40]) == 2
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    assert fill.main([*base, "--candidate", head]) == 2
    (repo / "dirty.txt").unlink()
    assert fill.main([*base, "--candidate", head]) == 0
    written = json.loads((record_dir / RECORD.name).read_text("utf-8"))
    assert written["state"] == "executed_on_candidate"
    assert written["candidate"]["commit"] == head


def _record_prepared() -> dict:
    # The fill tests run against the prepared form whether or not the committed record
    # has been executed, so they keep their coverage after the real fill lands.
    record = fill.prepared_form(_record())
    fill.validate_record(record)
    return record


def test_the_prepared_form_of_the_committed_record_claims_nothing():
    record = _record_prepared()
    assert record["state"] == "prepared_not_executed"
    assert record["claim_boundary"] == fill.PREPARED_CLAIM_BOUNDARY
    assert record["remaining_before_complete"] == list(fill.PREPARED_REMAINING)
    assert record["candidate"]["commit"] is None
    assert all(item["result"] is None for item in record["obligations"])


def test_an_executed_record_states_its_own_boundary(tmp_path: Path):
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path), bound_at_unix_ms=1)
    assert filled["claim_boundary"] == fill.EXECUTED_CLAIM_BOUNDARY
    assert filled["remaining_before_complete"] == list(fill.EXECUTED_REMAINING)
    # A record that says "nothing has been executed" while carrying results is refused.
    filled["claim_boundary"] = fill.PREPARED_CLAIM_BOUNDARY
    with pytest.raises(ValueError, match="a0_record_claim_boundary_stale"):
        fill.validate_record(filled)
    filled = fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                              results=_results(tmp_path), bound_at_unix_ms=1)
    filled["remaining_before_complete"] = list(fill.PREPARED_REMAINING)
    with pytest.raises(ValueError, match="a0_record_claim_boundary_stale"):
        fill.validate_record(filled)


def _executed(tmp_path: Path, result: str = "passed") -> dict:
    return fill.fill_record(_record_prepared(), candidate=CANDIDATE,
                            results=_results(tmp_path, result), bound_at_unix_ms=1)


def test_resolving_r4_completes_an_all_passed_executed_record(tmp_path: Path):
    complete = fill.resolve_r4(_executed(tmp_path), reading=fill.R4_READING, commit=CANDIDATE)
    fill.validate_record(complete)
    assert complete["state"] == "complete_on_candidate"
    assert complete["claim_boundary"] == fill.COMPLETE_CLAIM_BOUNDARY
    assert complete["remaining_before_complete"] == []
    assert complete["atomic_commit"]["reading"] == fill.R4_READING
    assert complete["atomic_commit"]["commit"] == CANDIDATE
    assert complete["atomic_commit"]["owner_ruling"] == "R4"
    assert complete["obligations"] == _executed(tmp_path)["obligations"]


def test_r4_cannot_complete_a_record_with_a_failed_obligation(tmp_path: Path):
    with pytest.raises(ValueError, match="a0_record_obligation_failed"):
        fill.resolve_r4(_executed(tmp_path, "failed"), reading=fill.R4_READING, commit=CANDIDATE)


def test_r4_refuses_a_prepared_record_another_reading_or_another_commit(tmp_path: Path):
    with pytest.raises(ValueError, match="a0_record_not_executed"):
        fill.resolve_r4(_record_prepared(), reading=fill.R4_READING, commit=CANDIDATE)
    with pytest.raises(ValueError, match="a0_record_r4_reading_invalid"):
        fill.resolve_r4(_executed(tmp_path), reading="one atomic commit", commit=CANDIDATE)
    with pytest.raises(ValueError, match="a0_record_r4_commit_not_the_candidate"):
        fill.resolve_r4(_executed(tmp_path), reading=fill.R4_READING, commit="b" * 40)


def test_a_complete_record_that_drifts_is_refused(tmp_path: Path):
    complete = fill.resolve_r4(_executed(tmp_path), reading=fill.R4_READING, commit=CANDIDATE)
    drifted = json.loads(json.dumps(complete))
    drifted["remaining_before_complete"] = list(fill.EXECUTED_REMAINING)
    with pytest.raises(ValueError, match="a0_record_claim_boundary_stale"):
        fill.validate_record(drifted)
    drifted = json.loads(json.dumps(complete))
    drifted["atomic_commit"]["commit"] = "b" * 40
    with pytest.raises(ValueError, match="a0_record_r4_commit_not_the_candidate"):
        fill.validate_record(drifted)
    drifted = json.loads(json.dumps(complete))
    drifted["obligations"][0]["result"] = "failed"
    with pytest.raises(ValueError, match="a0_record_obligation_failed"):
        fill.validate_record(drifted)
    # An executed record may not carry an R4 binding without being completed.
    executed = _executed(tmp_path)
    executed["atomic_commit"] = dict(complete["atomic_commit"])
    with pytest.raises(ValueError, match="a0_record_r4_bound_before_complete"):
        fill.validate_record(executed)


def test_the_prepared_form_of_a_complete_record_clears_r4(tmp_path: Path):
    complete = fill.resolve_r4(_executed(tmp_path), reading=fill.R4_READING, commit=CANDIDATE)
    prepared = fill.prepared_form(complete)
    fill.validate_record(prepared)
    assert prepared["atomic_commit"]["reading"] is None
    assert prepared["atomic_commit"]["commit"] is None


def test_the_cli_resolves_r4_on_an_executed_record(tmp_path: Path):
    repo = tmp_path / "repo"
    record_dir = repo / "docs" / "handover"
    record_dir.mkdir(parents=True)
    (record_dir / RECORD.name).write_text(json.dumps(_executed(tmp_path)), encoding="utf-8")
    base = ["--repo-root", str(repo), "--resolve-r4"]
    assert fill.main([*base, "--candidate", "b" * 40]) == 1
    assert fill.main([*base, "--candidate", CANDIDATE]) == 0
    written = json.loads((record_dir / RECORD.name).read_text("utf-8"))
    assert written["state"] == "complete_on_candidate"
    assert written["atomic_commit"]["commit"] == CANDIDATE
