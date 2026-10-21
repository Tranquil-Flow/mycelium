# SPDX-License-Identifier: AGPL-3.0-only
"""The A6 execution-plan builder emits exactly what the physical gate admits.

The builder never executes a case: these tests prove the emitted plan passes the
gate's own ``validate_plan`` and carries the serving tree's source-owned digests.
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from mycelium_a6_case_observations import (
    CONTRACT_MANIFEST_PATH,
    SOURCE_MANIFEST_PATH,
    SPEC_PATH,
)
from scripts import build_a6_execution_plan as builder
from scripts.run_a6_recovery_gate import (
    EXPECTED_ORIGIN,
    EXPECTED_PLACEMENTS,
    REQUIRED_CASES,
    REQUIRED_CLEANUP_CHECKS,
    TRUSTED_CASE_RUNNER,
    TRUSTED_CASE_RUNNER_V2,
    validate_plan,
)

ROOT = Path(__file__).resolve().parents[2]


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _build(tmp_path: Path, **overrides: object) -> dict:
    arguments = {
        "repo_root": ROOT,
        "operator_plan": tmp_path / "operator-plan.json",
        "evidence_root": tmp_path / "evidence",
    }
    arguments.update(overrides)
    return builder.build_plan(**arguments)


def test_the_built_plan_passes_the_gate_validation(tmp_path: Path) -> None:
    document = _build(tmp_path)

    validated = validate_plan(document)

    assert [case["case_id"] for case in validated["cases"]] == list(REQUIRED_CASES)
    assert [check["check_id"] for check in validated["cleanup_checks"]] == list(
        REQUIRED_CLEANUP_CHECKS
    )
    assert document["product_origin"] == EXPECTED_ORIGIN
    assert document["expected_placements"] == list(EXPECTED_PLACEMENTS)
    assert document["expected_placements"] == ["node-0", "node-2", "node-3"]


@pytest.mark.parametrize("placements", [("node-0", "node-2"), ("node-0", "node-2", "node-3")])
def test_two_and_three_node_topologies_are_admissible(tmp_path: Path, placements) -> None:
    document = _build(tmp_path, placements=placements)
    assert validate_plan(document)["expected_placements"] == list(placements)


@pytest.mark.parametrize("placements", [
    ("node-0",), ("node-0", "node-3"), ("node-2", "node-0", "node-3"),
    ("node-0", "node-2", "node-3", "node-4")])
def test_other_topologies_are_refused(tmp_path: Path, placements) -> None:
    with pytest.raises(ValueError, match="physical_plan_placements_invalid"):
        _build(tmp_path, placements=placements)


def test_the_digests_are_the_serving_trees_own(tmp_path: Path) -> None:
    document = _build(tmp_path)
    manifest = json.loads((ROOT / SOURCE_MANIFEST_PATH).read_text("utf-8"))

    assert document["source_digest"] == manifest["source_digest"]
    assert document["spec_digest"] == _digest(ROOT / SPEC_PATH)
    assert document["contract_digest"] == _digest(ROOT / CONTRACT_MANIFEST_PATH)


def test_the_cleanup_authority_case_uses_the_admitted_v2_runner(tmp_path: Path) -> None:
    document = _build(tmp_path)
    by_case = {case["case_id"]: case for case in document["cases"]}
    operator_plan = str(tmp_path / "operator-plan.json")

    assert by_case["cleanup_authority_failure"]["argv"] == [
        sys.executable,
        str(TRUSTED_CASE_RUNNER_V2),
        "--case",
        "cleanup_authority_failure",
        "--contract-version",
        "v2",
        "--operator-plan",
        operator_plan,
    ]
    assert by_case["no_successor"]["argv"] == [
        sys.executable,
        str(TRUSTED_CASE_RUNNER),
        "--case",
        "no_successor",
        "--operator-plan",
        operator_plan,
    ]


def test_each_case_evidence_file_sits_directly_in_the_evidence_root(tmp_path: Path) -> None:
    document = _build(tmp_path)

    for case in document["cases"]:
        assert Path(case["evidence_file"]).parent == tmp_path / "evidence"
        assert Path(case["evidence_file"]).name == f"{case['case_id']}.json"


def test_relative_paths_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a6_plan_operator_plan_not_absolute"):
        _build(tmp_path, operator_plan=Path("operator-plan.json"))
    with pytest.raises(ValueError, match="a6_plan_evidence_root_not_absolute"):
        _build(tmp_path, evidence_root=Path("evidence"))


def test_the_cli_writes_a_private_plan_and_refuses_to_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    argv = [
        "--operator-plan",
        str(tmp_path / "operator-plan.json"),
        "--evidence-root",
        str(tmp_path / "evidence"),
        "--output",
        str(output),
    ]

    assert builder.main(argv) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    validate_plan(json.loads(output.read_text("utf-8")))
    assert not (tmp_path / "evidence").exists()

    assert builder.main(argv) == 2


def test_the_cli_can_create_the_private_evidence_root(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    argv = [
        "--operator-plan",
        str(tmp_path / "operator-plan.json"),
        "--evidence-root",
        str(evidence),
        "--output",
        str(tmp_path / "plan.json"),
        "--create-evidence-root",
    ]

    assert builder.main(argv) == 0
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o700


def test_an_existing_evidence_root_is_never_reused(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    argv = [
        "--operator-plan",
        str(tmp_path / "operator-plan.json"),
        "--evidence-root",
        str(evidence),
        "--output",
        str(tmp_path / "plan.json"),
        "--create-evidence-root",
    ]

    assert builder.main(argv) == 2
    assert not (tmp_path / "plan.json").exists()
