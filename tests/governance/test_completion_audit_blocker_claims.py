"""RED-first coverage for the blocker-truth check added for review finding F1.

Finding F1: the checklist's *form* being valid does not show the checklist is
*true*. A stale blocker passed ``scripts/mycelium_completion_audit.py`` because the
audit validated the shape of the document and never checked a blocker against the
artifacts that blocker names; the A13 blocker went stale the same way twice.

What these tests keep:

* The RED as recorded. The audit of the pinned pre-change revision
  :data:`BASE_COMMIT` *accepts* a fixture whose blocker is refuted by the artifact
  it names, while that same revision *rejects* the very same tree when the
  checklist's form is wrong -- so the old green was a false accept of a
  contradicted claim, not a vacuous pass of a broken harness.
* The GREEN. The current audit refutes that fixture on the claim alone, with the
  fixture otherwise shape-valid (no shape finding), and accepts truthful claims.
* The boundary. A claim that cannot be decided (no readable HEAD, an artifact that
  is not a readable regular file) is a finding, never a silent pass.

Fixtures are thrown away after each test: every checklist they use is derived at
run time from the live ``docs/handover/mycelium-completion-checklist.v1.json`` and
written into ``tmp_path`` together with byte-identical copies of the artifacts the
checklist names. Nothing here writes into the repository, and nothing here reads
the checklist's live verdict as an input: the live checklist is only used as the
realistic document skeleton these fixtures vary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.mycelium_completion_audit import (
    DEFAULT_CHECKLIST,
    PLAN_PATH,
    audit,
    audit_blocker_claims,
    blocker_claims,
)

ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = "scripts/mycelium_completion_audit.py"
#: The revision the F1 review found the gap at; its audit script is the "before".
BASE_COMMIT = "b1a61a69ca1d94a79f33c07dd9f4bbb50cc3196b"
GATE = "A6"
FIXTURE_CHECKLIST = "blocker-claim-fixture.json"
PROBE = "claim-probe/a6-replay-status.v1.txt"
PROBE_PRESENT = "replay-path-qualified"
PROBE_ABSENT = "blocker-claim-fixture-absent-marker"
GREEN_RESULT = {
    "checked_gates": 13,
    "findings": [],
    "ok": True,
    "primary_gate": "A6",
    "protocol": "mycelium.completion_checklist.v1",
}


def _clean_environment() -> dict[str, str]:
    """Environment for child processes: no inherited Python or Git overrides."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        and not key.startswith("GIT_")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _real_checklist() -> dict:
    return json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))


def _gate(checklist: dict, gate_id: str) -> dict:
    return next(gate for gate in checklist["gates"] if gate["gate_id"] == gate_id)


def _materialize(root: Path, checklist: dict) -> None:
    """Copy every artifact the checklist names, plus the plan, byte for byte."""

    relative_paths = {PLAN_PATH}
    for gate in checklist["gates"]:
        relative_paths.add(gate["specification"])
        if gate["handover"] is not None:
            relative_paths.add(gate["handover"])
        for evidence in gate["evidence_bindings"]:
            relative_paths.add(evidence["artifact_reference"])
    for relative in sorted(relative_paths):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())


def _fixture(tmp_path: Path, blockers: list[str], *, probe: bool = True) -> Path:
    """Write a checklist whose only delta from the live one is the gate's blockers."""

    checklist = _real_checklist()
    _gate(checklist, GATE)["blockers"] = list(blockers)
    _materialize(tmp_path, checklist)
    if probe:
        artifact = tmp_path / PROBE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            "# A6 replay status probe written by the blocker-claim fixture\n"
            f"{PROBE_PRESENT}\n",
            "utf-8",
        )
    (tmp_path / FIXTURE_CHECKLIST).write_text(json.dumps(checklist), "utf-8")
    return tmp_path


def _stale_blockers(tmp_path: Path) -> list[str]:
    """Blockers of the F1 kind: each asserts something its named artifact refutes."""

    specification = _gate(_real_checklist(), GATE)["specification"]
    return [
        (
            "the A6 full-context replay implementation is absent from the candidate "
            f"tree [[claim:path_absent {specification}]]"
        ),
        (
            "the governing plan is not committed anywhere in the candidate "
            f"[[claim:path_absent {PLAN_PATH}]]"
        ),
        (
            "the A6 replay status artifact still carries the withdrawn marker "
            f"[[claim:marker_present {PROBE} {PROBE_ABSENT}]]"
        ),
        (
            "the A6 replay probe no longer records a qualified successor path "
            f"[[claim:marker_absent {PROBE} {PROBE_PRESENT}]]"
        ),
    ]


def _truthful_blockers() -> list[str]:
    specification = _gate(_real_checklist(), GATE)["specification"]
    return [
        (
            "the A6 full-context replay contract is committed in the candidate "
            f"tree [[claim:path_exists {specification}]]"
        ),
        (
            "the governing plan is present [[claim:path_exists "
            f"{PLAN_PATH}]]"
        ),
        "the candidate keeps its docs directory [[claim:path_exists docs]]",
        (
            "the A6 replay status artifact records the qualified successor path "
            f"[[claim:marker_present {PROBE} {PROBE_PRESENT}]]"
        ),
        (
            "the A6 replay status artifact does not carry the withdrawn marker "
            f"[[claim:marker_absent {PROBE} {PROBE_ABSENT}]]"
        ),
        (
            "no successor replay has been executed "
            "[[claim:path_absent docs/handover/no-such-successor-replay.v1.json]]"
        ),
    ]


def _boundary_blockers(specification: str) -> list[str]:
    return [
        "a stale blocker under an unknown claim kind [[claim:no_such_kind artifact]]",
        "a stale blocker with no argument [[claim:path_exists]]",
        "a stale blocker whose marker is never closed "
        f"[[claim:path_absent {specification}",
        "a stale blocker naming a path outside the repository "
        "[[claim:path_absent /etc/hosts]]",
        "a marker claim about an artifact that is not a regular file "
        "[[claim:marker_present docs fixture-token]]",
    ]


def _write_script(tmp_path: Path) -> Path:
    """Extract the audit as it stood at the pinned pre-change revision."""

    completed = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BASE_COMMIT}:{AUDIT_SCRIPT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_clean_environment(),
    )
    if completed.returncode != 0:
        pytest.skip(
            f"pinned revision {BASE_COMMIT[:12]} is unavailable in this clone, so "
            "the recorded F1 red cannot be re-run here"
        )
    script = tmp_path / "base-mycelium-completion-audit.py"
    script.write_bytes(completed.stdout)
    return script


def _run_audit_cli(
    script: Path, root: Path, tmp_path: Path, *, json_output: bool = True
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        str(script),
        "--repo-root",
        str(root),
        "--checklist",
        FIXTURE_CHECKLIST,
    ]
    if json_output:
        command.append("--json")
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        cwd=str(tmp_path),
        env=_clean_environment(),
    )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=_clean_environment(),
    )
    assert completed.returncode == 0, (arguments, completed.stderr)
    return completed.stdout.strip()


def _git_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    """Materialize the fixture inside a real work tree; return root, HEAD, candidate."""

    root = _fixture(tmp_path, [], probe=False)
    identity = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
    _git(root, "-c", "init.defaultBranch=fixture", "init", "-q")
    _git(root, "add", "-A")
    _git(
        root,
        *identity,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "fixture head",
    )
    head = _git(root, "rev-parse", "HEAD")
    # A candidate commit that exists locally and is on no branch: the "not
    # integrated on the pilot branch" shape the recorded A6 blocker had.
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    candidate = _git(
        root,
        *identity,
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        tree,
        "-m",
        "unintegrated candidate",
    )
    return root, head, candidate


def _rewrite_blockers(root: Path, blockers: list[str]) -> None:
    """Replace the gate's blockers in an already-written fixture checklist."""

    checklist = json.loads((root / FIXTURE_CHECKLIST).read_text("utf-8"))
    _gate(checklist, GATE)["blockers"] = list(blockers)
    (root / FIXTURE_CHECKLIST).write_text(json.dumps(checklist), "utf-8")


def test_the_fixture_form_is_valid_without_a_claim_clause(tmp_path: Path) -> None:
    """The skeleton every other test varies already passes the whole audit."""

    root = _fixture(tmp_path, ["dependency A6 atomic completion"], probe=False)

    result = audit(root, FIXTURE_CHECKLIST)

    assert result == GREEN_RESULT


def test_the_pre_change_audit_accepts_a_blocker_its_artifact_refutes(
    tmp_path: Path,
) -> None:
    """RED: the F1 defect, re-run against the pinned pre-change audit."""

    root = _fixture(tmp_path, _stale_blockers(tmp_path))
    base = _write_script(tmp_path)
    assert base.read_bytes() != (ROOT / AUDIT_SCRIPT).read_bytes()

    completed = _run_audit_cli(base, root, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == GREEN_RESULT


def test_the_pre_change_audit_was_not_vacuously_green_on_this_tree(
    tmp_path: Path,
) -> None:
    """Control for the red: the pinned audit fails on this tree when the form is wrong."""

    root = _fixture(tmp_path, [])
    base = _write_script(tmp_path)

    completed = _run_audit_cli(base, root, tmp_path, json_output=False)

    assert completed.returncode == 1
    assert "Mycelium completion audit FAILED" in completed.stderr
    assert "blockers_invalid" in completed.stderr


def test_the_fixed_audit_refutes_the_contradicted_blockers(tmp_path: Path) -> None:
    """GREEN: the same fixture, with the same valid form, now fails on truth alone."""

    root = _fixture(tmp_path, _stale_blockers(tmp_path))

    result = audit(root, FIXTURE_CHECKLIST)

    assert result["ok"] is False
    assert result["checked_gates"] == 13
    assert result["findings"] == [
        "gate:A6:blocker:0:claim_contradicted:path_absent",
        "gate:A6:blocker:1:claim_contradicted:path_absent",
        "gate:A6:blocker:2:claim_contradicted:marker_present",
        "gate:A6:blocker:3:claim_contradicted:marker_absent",
    ]

    completed = _run_audit_cli(ROOT / AUDIT_SCRIPT, root, tmp_path, json_output=False)

    assert completed.returncode == 1
    assert completed.stdout == ""
    for finding in result["findings"]:
        assert finding in completed.stderr


def test_truthful_blocker_claims_pass(tmp_path: Path) -> None:
    root = _fixture(tmp_path, _truthful_blockers())

    result = audit(root, FIXTURE_CHECKLIST)

    assert result == GREEN_RESULT


def test_malformed_and_undecidable_claims_are_findings(tmp_path: Path) -> None:
    specification = _gate(_real_checklist(), GATE)["specification"]
    root = _fixture(tmp_path, _boundary_blockers(specification))

    result = audit(root, FIXTURE_CHECKLIST)

    assert result["ok"] is False
    assert result["findings"] == [
        "gate:A6:blocker:0:claim_invalid:no_such_kind",
        "gate:A6:blocker:1:claim_invalid:path_exists",
        "gate:A6:blocker:2:claim_invalid:malformed",
        "gate:A6:blocker:3:claim_invalid:path_absent",
        "gate:A6:blocker:4:claim_unresolved:marker_present",
    ]


def test_git_claims_are_decided_against_the_work_tree(tmp_path: Path) -> None:
    truthful, head, candidate = _git_fixture(tmp_path / "truthful")
    _rewrite_blockers(
        truthful,
        [
            f"the candidate head is present [[claim:commit_present {head}]]",
            f"the candidate head is integrated [[claim:commit_reachable {head}]]",
            "the unintegrated candidate is absent from this line "
            f"[[claim:commit_unreachable {candidate}]]",
        ],
    )

    assert audit(truthful, FIXTURE_CHECKLIST) == GREEN_RESULT

    stale, stale_head, stale_candidate = _git_fixture(tmp_path / "stale")
    _rewrite_blockers(
        stale,
        [
            "a candidate that is not in the object database "
            "[[claim:commit_present 0000000000000000000000000000000000000000]]",
            "the current head is unintegrated "
            f"[[claim:commit_unreachable {stale_head}]]",
            "the unintegrated candidate is integrated "
            f"[[claim:commit_reachable {stale_candidate}]]",
        ],
    )

    result = audit(stale, FIXTURE_CHECKLIST)

    assert result["ok"] is False
    assert result["findings"] == [
        "gate:A6:blocker:0:claim_contradicted:commit_present",
        "gate:A6:blocker:1:claim_contradicted:commit_unreachable",
        "gate:A6:blocker:2:claim_contradicted:commit_reachable",
    ]


def test_git_claims_are_undecidable_without_a_readable_head(tmp_path: Path) -> None:
    """A repository with no HEAD commit cannot decide a Git clause, so it is a finding."""

    root = _fixture(
        tmp_path,
        [f"the candidate is present [[claim:commit_present {BASE_COMMIT}]]"],
        probe=False,
    )
    _git(root, "-c", "init.defaultBranch=fixture", "init", "-q")

    result = audit(root, FIXTURE_CHECKLIST)

    assert result["findings"] == ["gate:A6:blocker:0:claim_unresolved:commit_present"]


def test_prose_without_a_clause_is_not_a_claim() -> None:
    assert blocker_claims("dependency A6 atomic completion") == []
    assert blocker_claims(
        "two clauses [[claim:path_exists docs]] and [[claim:no_such_kind x]]"
    ) == [("claim", "path_exists", ("docs",)), ("invalid", "no_such_kind", ())]


def test_the_live_checklist_carries_no_clauses_and_gains_no_findings() -> None:
    checklist = _real_checklist()

    for gate in checklist["gates"]:
        assert audit_blocker_claims(ROOT, gate["gate_id"], gate["blockers"]) == []

    assert audit(ROOT) == GREEN_RESULT
