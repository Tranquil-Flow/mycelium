"""A15 executed-artifact intake tests.

GREEN production slice, not a `scaffold_red` suite: the intake this exercises is
implemented, and every assertion below is decidable against retained artifacts.

Two halves:

* negative/tamper coverage built from synthetic *declarations* (the declaration
  is test input; the artifact bytes it points at are still real files on disk),
  and
* execution over the REAL retained pilot artifacts:
  `runs/wp8-02-observational-p84r1/*` plus the real
  `docs/handover/*.completion-record.v1.json` completion records and the frozen
  checklist evidence bindings.

The real-artifact half is deliberately strict: if a retained input is absent the
test FAILS (fail-closed) rather than skipping, unless the operator explicitly
sets `MYCELIUM_A15_REQUIRE_REAL=0` for a checkout that does not retain them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mycelium_a15_run_intake import (
    CLAIM_BOUNDARY,
    PROTOCOL,
    REPORT_PROTOCOL,
    intake_run,
    load_declaration,
    missing_verifier_contracts,
    sha256_file,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = Path(
    os.environ.get(
        "MYCELIUM_A15_RUN_DIR",
        "/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-observational-p84r1",
    )
)
EVIDENCE_ROOTS = tuple(
    Path(item)
    for item in os.environ.get(
        "MYCELIUM_A15_EVIDENCE_ROOTS", "/Users/evinova-self/mycelium-physical-run"
    ).split(os.pathsep)
    if item
)
REQUIRE_REAL = os.environ.get("MYCELIUM_A15_REQUIRE_REAL", "1") == "1"

# Pinned digests of the exact retained bytes this slice is exercised over.
READINESS_GATE_DIGEST = "sha256:a9983be6aeed2c8c99f214aa034e6f201759156e0a41546d06996abbf4af95d5"
PACKAGE_DIGEST = "sha256:79adcfaf9355ff140873b571d121f137681eed56ed070c1e5ecfe098417ca22c"
GATE_EXECUTION_DIGEST = "sha256:d9e3b6fcd302ba9428556801a2469dfb645b5823bf41e73980c7cd8a28ed3d0a"
HYPOTHESIS_DIGEST = "sha256:2a76aa59a83c0c6a0a5d1ce5a438a52987af80a00534fd9bb215f1418bba5bff"
CHECKLIST_DIGEST = "sha256:f9be913827f8d67412587db93eccc87689bddc656a255a130eeedc4c7b3ad8d5"  # refreshed to current checklist bytes (post A9 spec row + evidence-shape normalization)
A3_RECORD_DIGEST = "sha256:462d7232ab006ba7bbde12f578d948e766f45b9a60b10f79764935c05e65333d"
A4_RECORD_DIGEST = "sha256:1423354a43a4d85680a7c9a319649709d9c42c2f8a8856311deafb52785a28bd"
A5_RECORD_DIGEST = "sha256:9aa287d2c7e35a1db36c01e4f2326891747b497d77b43285e8b3fbc2a2ea1628"
A8_RECORD_DIGEST = "sha256:9e51f6623939da580022db60dc4b14ae59bea7628bb02b4eb7eae3dc8ef403d8"

SOURCE_COMMIT = "fb0314cf1cc5a57d14a2096226878267506b27f6"
NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

# The pinned A15 bytes, bound to the *preserved* original artifact paths.
#
# `readiness-gate.json` and `PACKAGE.json` are working files: the pilot/A13 lane
# re-runs overwrite them in place, so their live bytes are no longer the bytes the A15
# handover pinned. The pinned originals are retained inside the same run directory under
# `*-preserved-*` names and verify against the unchanged pins below. Exact
# retained-artifact checks therefore bind these paths and never the mutable working
# files. The pins themselves are deliberately NOT changed to fit the live bytes.
PRESERVED_READINESS_GATE = "readiness-gate-preserved-fb0314c.json"
PRESERVED_PACKAGE = "PACKAGE-preserved-0.1.0-preview.84.json"
PRESERVED_ARTIFACTS = {
    "readiness_gate": (PRESERVED_READINESS_GATE, READINESS_GATE_DIGEST, 40541),
    "package": (PRESERVED_PACKAGE, PACKAGE_DIGEST, 4077),
    "gate_execution": ("gate-execution.json", GATE_EXECUTION_DIGEST, 516),
    "hypothesis": ("HYPOTHESIS.md", HYPOTHESIS_DIGEST, 3619),
}
COMPLETION_RECORD_DIGESTS = {
    "a3-completion-record.v1.json": A3_RECORD_DIGEST,
    "a4-completion-record.v1.json": A4_RECORD_DIGEST,
    "a5-completion-record.v1.json": A5_RECORD_DIGEST,
    "a8-completion-record.v1.json": A8_RECORD_DIGEST,
}


def _real_present() -> bool:
    return RUN_DIR.is_dir() and (RUN_DIR / "readiness-gate.json").is_file()


requires_real = pytest.mark.skipif(
    not REQUIRE_REAL and not _real_present(),
    reason="retained pilot artifacts absent and MYCELIUM_A15_REQUIRE_REAL=0",
)


def base_declaration() -> dict:
    """A declaration over the REAL retained run artifacts."""
    return {
        "protocol": PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "run_dir": str(RUN_DIR),
        "evidence_roots": [str(root) for root in EVIDENCE_ROOTS],
        "repo_root": str(REPO_ROOT),
        "source": {"commit": SOURCE_COMMIT, "branch": "integration/core-pilot"},
        "artifacts": [
            {
                "artifact_id": "readiness_gate",
                "path": "readiness-gate.json",
                "declared_digest": READINESS_GATE_DIGEST,
                "protocol": "mycelium.pilot_readiness_gate.v1",
                "provenance": "live",
                "required": True,
                "fresh_until": None,
            },
            {
                "artifact_id": "package",
                "path": "PACKAGE.json",
                "declared_digest": PACKAGE_DIGEST,
                "protocol": "mycelium.pilot_readiness_gate.package.v1",
                "provenance": "live",
                "required": True,
                "fresh_until": None,
            },
            {
                "artifact_id": "gate_execution",
                "path": "gate-execution.json",
                "declared_digest": GATE_EXECUTION_DIGEST,
                # gate-execution.json carries no protocol field; the intake must
                # report that rather than invent one.
                "protocol": None,
                "provenance": "live",
                "required": True,
                "fresh_until": None,
            },
            {
                "artifact_id": "hypothesis",
                "path": "HYPOTHESIS.md",
                "declared_digest": HYPOTHESIS_DIGEST,
                "protocol": None,
                "provenance": "source",
                "required": True,
                "fresh_until": None,
            },
        ],
        "completion_records": [
            {"gate_id": "A3", "path": "docs/handover/a3-completion-record.v1.json"},
            {"gate_id": "A4", "path": "docs/handover/a4-completion-record.v1.json"},
            {"gate_id": "A5", "path": "docs/handover/a5-completion-record.v1.json"},
            {"gate_id": "A8", "path": "docs/handover/a8-completion-record.v1.json"},
        ],
        "checklist": {
            "path": "docs/handover/mycelium-completion-checklist.v1.json",
            "declared_digest": CHECKLIST_DIGEST,
        },
    }


def run_intake(declaration: dict, *, now: datetime = NOW) -> dict:
    return intake_run(declaration, now=now, repo_root=str(REPO_ROOT))


def preserved_declaration() -> dict:
    """A declaration over the preserved originals of the pinned retained bytes.

    Same pinned digests and same run directory as ``base_declaration``; only the two
    mutable working filenames are replaced by the preserved original artifact paths.
    """
    declaration = base_declaration()
    for artifact in declaration["artifacts"]:
        artifact["path"] = PRESERVED_ARTIFACTS[artifact["artifact_id"]][0]
    return declaration


def single_artifact(declaration: dict, artifact_id: str) -> dict:
    """Reduce a declaration to exactly one artifact (isolate that channel)."""
    declaration["artifacts"] = [
        item for item in declaration["artifacts"] if item["artifact_id"] == artifact_id
    ]
    return declaration


def fixture_run_dir(tmp_path: Path) -> Path:
    """Copy the real run artifacts into a scratch dir so tampering never touches them."""
    if not _real_present():
        pytest.fail("retained pilot run artifacts are absent; intake cannot be exercised")
    target = tmp_path / "run"
    target.mkdir()
    for name in (
        "readiness-gate.json",
        "PACKAGE.json",
        "gate-execution.json",
        "HYPOTHESIS.md",
    ):
        shutil.copy2(RUN_DIR / name, target / name)
    if (RUN_DIR / "package").is_dir():
        shutil.copytree(RUN_DIR / "package", target / "package")
    return target


def codes(report: dict) -> set[str]:
    return set(report["findings"])


# --------------------------------------------------------------------------- #
# declaration shape and fail-closed refusal
# --------------------------------------------------------------------------- #


def test_reports_never_claim_readiness():
    report = run_intake(base_declaration())
    assert report["protocol"] == REPORT_PROTOCOL
    assert report["authoritative"] is False
    assert report["release_ready"] is False
    assert report["gate_state"] == "design_only"
    assert "not_a_release_decision" in report["scope_limits"]


def test_declaration_protocol_rejected():
    declaration = base_declaration()
    declaration["protocol"] = "mycelium.not_the_intake.v1"
    with pytest.raises(ValueError, match="a15_run_intake_declaration_invalid"):
        load_declaration_values(declaration)


def load_declaration_values(declaration: dict) -> dict:
    """Validate an in-memory declaration through the same closed-shape path."""
    from mycelium_a15_run_intake import validate_declaration

    return validate_declaration(declaration)


def test_declaration_unknown_field_rejected():
    declaration = base_declaration()
    declaration["release_ready"] = True
    with pytest.raises(ValueError, match="a15_run_intake_declaration_invalid"):
        load_declaration_values(declaration)


def test_declaration_handwritten_completion_claim_rejected():
    declaration = base_declaration()
    declaration["source"]["clean_bootstrap"] = True
    with pytest.raises(ValueError, match="a15_run_intake_declaration_invalid"):
        load_declaration_values(declaration)


def test_load_declaration_from_disk_roundtrip(tmp_path: Path):
    path = tmp_path / "declaration.json"
    path.write_text(json.dumps(base_declaration()), encoding="utf-8")
    parsed = load_declaration(path)
    assert parsed["protocol"] == PROTOCOL
    assert len(parsed["artifacts"]) == 4


# --------------------------------------------------------------------------- #
# negative / tamper coverage — fail closed, never pass on an unsettled input
# --------------------------------------------------------------------------- #


@requires_real
def test_missing_required_artifact_fails_closed(tmp_path: Path):
    declaration = base_declaration()
    declaration["run_dir"] = str(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    report = run_intake(declaration)
    assert "artifact_missing" in codes(report)
    assert report["intake_ok"] is False
    assert report["release_ready"] is False


@requires_real
def test_tampered_artifact_byte_detected(tmp_path: Path):
    run_dir = fixture_run_dir(tmp_path)
    target = run_dir / "readiness-gate.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["verdict"] = "NO-GO"
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    declaration = base_declaration()
    declaration["run_dir"] = str(run_dir)
    report = run_intake(declaration)
    assert "artifact_digest_mismatch" in codes(report)
    assert report["intake_ok"] is False
    entry = next(
        item for item in report["artifact_results"] if item["artifact_id"] == "readiness_gate"
    )
    assert entry["verified"] is False
    assert entry["observed_digest"] != entry["declared_digest"]


@requires_real
def test_undeclared_digest_fails_closed():
    declaration = base_declaration()
    declaration["artifacts"][0]["declared_digest"] = None
    report = run_intake(declaration)
    assert "artifact_digest_undeclared" in codes(report)
    assert report["intake_ok"] is False


@requires_real
def test_non_allowlisted_protocol_fails_closed():
    declaration = base_declaration()
    declaration["artifacts"][0]["protocol"] = "mycelium.handwritten_pass.v1"
    report = run_intake(declaration)
    assert "artifact_protocol_not_allowlisted" in codes(report)


@requires_real
def test_path_escape_fails_closed():
    declaration = base_declaration()
    declaration["artifacts"][0]["path"] = "../../../etc/hosts"
    report = run_intake(declaration)
    assert "artifact_path_escapes_root" in codes(report)


@requires_real
def test_fixture_provenance_cannot_satisfy_live_requirement():
    declaration = base_declaration()
    declaration["artifacts"][0]["provenance"] = "fixture"
    report = run_intake(declaration)
    assert "provenance_substitution" in codes(report)


@requires_real
def test_stale_artifact_window_is_expired_not_ignored():
    """Isolate the ARTIFACT expiry channel from the sealed-evidence channel.

    Only one artifact and zero completion records are declared, so
    `mandatory_evidence_expired` can only come from the artifact-level window; the
    sealed A3/A4/A8 windows (independently expired) cannot mask or manufacture it.
    The open-window arm is the control: identical inputs, no expiry finding.
    """
    declaration = single_artifact(preserved_declaration(), "readiness_gate")
    declaration["completion_records"] = []

    declaration["artifacts"][0]["fresh_until"] = "2099-01-01T00:00:00Z"
    open_window = run_intake(declaration)
    assert "mandatory_evidence_expired" not in codes(open_window)
    assert open_window["artifact_results"][0].get("expired") is None

    declaration["artifacts"][0]["fresh_until"] = "2026-09-20T00:00:00Z"
    closed_window = run_intake(declaration)
    entry = closed_window["artifact_results"][0]
    assert "mandatory_evidence_expired" in codes(closed_window)
    assert entry["expired"] is True
    assert entry["fresh_until"] == "2026-09-20T00:00:00Z"
    assert entry["verified"] is True
    assert closed_window["intake_ok"] is False
    assert closed_window["release_ready"] is False
    # The ONLY delta between the two arms is the closed window.
    assert codes(closed_window) - codes(open_window) == {"mandatory_evidence_expired"}

    # And the window is decided against the caller's instant, not hardcoded.
    before = run_intake(declaration, now=datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc))
    assert "mandatory_evidence_expired" not in codes(before)


@requires_real
def test_closed_window_alone_is_a_finding(tmp_path: Path):
    """Parent-review reproduction: the only declared input is one real retained file
    with a closed declared window, in a scratch run dir so no other channel (package
    archive, sealed evidence, checklist binding) can contribute."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    shutil.copy2(RUN_DIR / PRESERVED_READINESS_GATE, run_dir / "readiness-gate.json")

    declaration = single_artifact(preserved_declaration(), "readiness_gate")
    declaration["run_dir"] = str(run_dir)
    declaration["artifacts"][0]["path"] = "readiness-gate.json"
    declaration["completion_records"] = []

    declaration["artifacts"][0]["fresh_until"] = "2099-01-01T00:00:00Z"
    open_window = run_intake(declaration)

    declaration["artifacts"][0]["fresh_until"] = "2026-09-20T00:00:00Z"
    closed_window = run_intake(declaration)

    assert "mandatory_evidence_expired" not in codes(open_window)
    assert "mandatory_evidence_expired" in codes(closed_window)
    assert codes(closed_window) - codes(open_window) == {"mandatory_evidence_expired"}
    entry = closed_window["artifact_results"][0]
    assert entry["observed_digest"] == READINESS_GATE_DIGEST
    assert entry["verified"] is True
    assert entry["expired"] is True
    assert closed_window["intake_ok"] is False
    assert closed_window["release_ready"] is False
    assert closed_window["authoritative"] is False


@requires_real
def test_freshness_window_inversion_rejected():
    declaration = base_declaration()
    declaration["artifacts"][0]["fresh_until"] = "2026-09-21T13:00:00Z"
    declaration["artifacts"][0]["observed_at"] = "2026-09-21T14:00:00Z"
    report = run_intake(declaration)
    assert "freshness_binding_invalid" in codes(report)


@requires_real
def test_source_commit_shape_invalid_rejected():
    declaration = base_declaration()
    declaration["source"]["commit"] = "not-a-commit"
    report = run_intake(declaration)
    assert "source_commit_invalid" in codes(report)


@requires_real
def test_source_commit_absent_from_repo_fails_closed():
    declaration = base_declaration()
    declaration["source"]["commit"] = "0" * 40
    report = run_intake(declaration)
    assert "source_commit_absent" in codes(report)


@requires_real
def test_source_binding_mismatch_detected(tmp_path: Path):
    run_dir = fixture_run_dir(tmp_path)
    declaration = base_declaration()
    declaration["run_dir"] = str(run_dir)
    declaration["source"]["commit"] = "e23d243" + "0" * 33
    report = run_intake(declaration)
    assert "source_binding_mismatch" in codes(report)


@requires_real
def test_evidence_root_escape_fails_closed():
    declaration = base_declaration()
    declaration["completion_records"] = [
        {"gate_id": "A5", "path": "docs/handover/a5-completion-record.v1.json"}
    ]
    declaration["evidence_roots"] = []
    report = run_intake(declaration)
    assert "artifact_reference_escapes_evidence_roots" in codes(report)


# --------------------------------------------------------------------------- #
# execution over the REAL retained artifacts
# --------------------------------------------------------------------------- #


@requires_real
def test_real_run_artifacts_verified_against_pinned_digests():
    report = run_intake(preserved_declaration())
    results = {item["artifact_id"]: item for item in report["artifact_results"]}
    assert set(results) == {"readiness_gate", "package", "gate_execution", "hypothesis"}
    assert set(PRESERVED_ARTIFACTS) == set(results)
    for artifact_id, (name, digest, size) in PRESERVED_ARTIFACTS.items():
        entry = results[artifact_id]
        assert entry["path"] == name
        assert entry["observed_digest"] == digest
        assert entry["byte_length"] == size
        assert entry["verified"] is True
        assert entry["read_bytes"] > 0
    assert "artifact_digest_mismatch" not in codes(report)
    assert "artifact_missing" not in codes(report)


@requires_real
def test_mutable_working_files_are_reported_not_silently_accepted():
    """The default working run dir is mutable (another lane re-runs into it).

    Whatever the live working bytes are, the declaration's pins must stay the pinned
    values and a divergence must be a fail-closed finding -- never a silent pass and
    never a pin relaxed to fit the live bytes.
    """
    report = run_intake(base_declaration())
    results = {item["artifact_id"]: item for item in report["artifact_results"]}
    for artifact_id in ("readiness_gate", "package"):
        _name, pinned, _size = PRESERVED_ARTIFACTS[artifact_id]
        entry = results[artifact_id]
        assert entry["declared_digest"] == pinned
        if entry["observed_digest"] != pinned:
            assert entry["verified"] is False
            assert "artifact_digest_mismatch" in codes(report)
            assert report["intake_ok"] is False


@requires_real
def test_real_readiness_gate_protocol_and_verdict_are_read_not_assumed():
    report = run_intake(base_declaration())
    gate = next(
        item for item in report["artifact_results"] if item["artifact_id"] == "readiness_gate"
    )
    assert gate["observed_protocol"] == "mycelium.pilot_readiness_gate.v1"
    assert gate["declared_protocol"] == gate["observed_protocol"]
    # A GO pilot readiness verdict is retained as an observed fact only.
    assert report["retained_observations"]["pilot_readiness_verdict"] == "GO"
    assert report["release_ready"] is False


@requires_real
def test_real_package_file_digests_recomputed_from_bytes():
    report = run_intake(base_declaration())
    package = report["retained_observations"]["package"]
    assert package["declared_package_digest"] == (
        "sha256:10f68278ef6b617aa850f0e78598af814a3af94247c520f79fc53240ee2c85f7"
    )
    assert package["file_digest_count"] == 12
    assert package["file_digests_mismatched"] == 0
    assert package["source_archive_present"] is False


@requires_real
def test_real_source_archive_absence_is_a_finding():
    report = run_intake(base_declaration())
    assert "source_archive_absent" in codes(report)


@requires_real
def test_real_completion_record_digests_and_sealed_evidence():
    report = run_intake(base_declaration())
    records = {item["gate_id"]: item for item in report["sealed_evidence_results"]}
    assert records["A4"]["record_digest"] == A4_RECORD_DIGEST
    assert records["A8"]["record_digest"] == A8_RECORD_DIGEST
    assert records["A4"]["sealed_digest_matched"] == 1
    assert records["A8"]["sealed_digest_matched"] == 1
    assert records["A4"]["sealed_digest_mismatched"] == 0
    assert records["A8"]["sealed_digest_mismatched"] == 0


@requires_real
def test_real_a3_sealed_evidence_divergence_is_reported():
    """Retained A3 record points at bytes that do not verify as declared."""
    report = run_intake(base_declaration())
    a3 = next(item for item in report["sealed_evidence_results"] if item["gate_id"] == "A3")
    assert a3["record_digest"] == A3_RECORD_DIGEST
    problems = set(a3["findings"])
    assert problems & {"sealed_evidence_missing", "sealed_evidence_digest_mismatch"}
    assert a3["sealed_digest_matched"] == 0
    assert report["intake_ok"] is False


@requires_real
def test_real_a5_sealed_evidence_resolves_under_evidence_root():
    report = run_intake(base_declaration())
    a5 = next(item for item in report["sealed_evidence_results"] if item["gate_id"] == "A5")
    assert a5["record_digest"] == A5_RECORD_DIGEST
    assert a5["sealed_evidence_count"] == 8
    assert a5["sealed_digest_matched"] == 8
    assert a5["sealed_digest_mismatched"] == 0


@requires_real
def test_real_completion_record_freshness_is_expired():
    """A3/A4/A8 sealed windows closed in August 2026; intake reports expiry."""
    report = run_intake(base_declaration())
    expired = {
        item["gate_id"] for item in report["sealed_evidence_results"] if item["expired"]
    }
    assert {"A3", "A8"} <= expired
    assert "mandatory_evidence_expired" in codes(report)


@requires_real
def test_real_checklist_bindings_all_verified():
    report = run_intake(base_declaration())
    checklist = report["retained_observations"]["checklist"]
    assert checklist["declared_digest"] == CHECKLIST_DIGEST
    assert checklist["binding_count"] == 61
    assert checklist["bindings_verified"] == 61
    assert checklist["bindings_missing"] == 0
    assert checklist["bindings_mismatched"] == 0
    assert checklist["verified_digest"] is True


@requires_real
def test_real_a15_gate_state_is_still_design_only():
    report = run_intake(base_declaration())
    assert report["retained_observations"]["checklist"]["a15_state"] == "design_only"
    assert "gate_state_not_design_only" not in codes(report)


@requires_real
def test_real_report_lists_missing_mandatory_evidence():
    report = run_intake(base_declaration())
    missing = report["missing_evidence"]
    for gate in ("A3", "A7", "A12", "A14"):
        assert f"executed_gate_result:{gate}" in missing
    assert "reviewer_reproduction" in missing
    assert "signed_exclusions" in missing
    assert "release_decision_producer" in missing
    assert report["missing_evidence_count"] == len(missing)


@requires_real
def test_missing_verifier_contracts_named_exactly():
    report = run_intake(base_declaration())
    contracts = {item["contract"] for item in report["missing_verifier_contracts"]}
    assert contracts == {
        "mycelium.executed_gate_result.v1",
        "mycelium.executed_result_graph.v1",
        "mycelium.executed_release_decision.v1",
        "mycelium.exclusion_signature_verifier.v1",
        "mycelium.verifier_policy_registry.v1",
        "mycelium.artifact_reference_root_policy.v1",
        "mycelium.authority_generation_currentness.v1",
    }
    for item in report["missing_verifier_contracts"]:
        assert item["reason"]
    assert {item["contract"] for item in missing_verifier_contracts()} == contracts


@requires_real
def test_synthetic_validator_is_not_the_intake():
    """The design-only synthetic validator must not be imported as the verifier."""
    report = run_intake(base_declaration())
    assert report["production_verifier"] is False
    assert "tests/a15_acceptance/validator.py" in report["non_authoritative_inputs"]
    import mycelium_a15_run_intake as module

    assert not hasattr(module, "validate_executed_result")
    assert "validator" not in Path(module.__file__).name


@requires_real
def test_report_digest_is_content_addressed_and_stable():
    first = run_intake(base_declaration())
    second = run_intake(base_declaration())
    assert first["report_digest"] == second["report_digest"]
    payload = dict(first)
    digest = payload.pop("report_digest")
    assert digest == "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@requires_real
def test_real_undeclared_artifact_protocol_is_a_finding():
    """gate-execution.json / HYPOTHESIS.md carry no protocol field: report it."""
    report = run_intake(base_declaration())
    results = {item["artifact_id"]: item for item in report["artifact_results"]}
    assert results["gate_execution"]["observed_protocol"] is None
    assert results["hypothesis"]["observed_protocol"] is None
    assert "artifact_protocol_undeclared" in codes(report)
    assert results["readiness_gate"]["observed_protocol"] == "mycelium.pilot_readiness_gate.v1"


@requires_real
def test_pinned_bytes_bind_preserved_retained_originals():
    """Guard: every pinned digest binds retained bytes that still exist unmodified.

    The preserved originals are the retained copies of the exact bytes the A15 handover
    pinned, so this check never reads the mutable working files in the same directory.
    """
    for artifact_id, (name, digest, size) in PRESERVED_ARTIFACTS.items():
        path = RUN_DIR / name
        assert path.is_file(), f"preserved retained original {name} is missing"
        assert sha256_file(path) == digest, artifact_id
        assert path.stat().st_size == size, artifact_id
    for name, digest in COMPLETION_RECORD_DIGESTS.items():
        assert sha256_file(REPO_ROOT / "docs" / "handover" / name) == digest


@requires_real
def test_source_commit_is_present_in_this_worktree():
    report = run_intake(base_declaration())
    assert report["source"]["commit"] == SOURCE_COMMIT
    assert report["source"]["commit_present"] is True
    # The worktree state is *observed*, never assumed: while this slice is being
    # authored the tree carries uncommitted changes, and the intake says so.
    assert report["source"]["tree_state_observed"] in {"clean", "dirty"}
    if report["source"]["tree_state_observed"] != "clean":
        assert "source_tree_not_clean" in codes(report)
    assert report["source"]["measured_heads"] == [SOURCE_COMMIT]


@requires_real
def test_intake_is_deterministic_across_now():
    early = run_intake(base_declaration(), now=NOW)
    late = run_intake(base_declaration(), now=NOW + timedelta(days=400))
    assert early["findings"] == late["findings"]
    assert early["report_digest"] == late["report_digest"]


# --------------------------------------------------------------------------- #
# path containment — a symlinked component must not reach outside the root
# --------------------------------------------------------------------------- #


@requires_real
def test_symlinked_intermediate_directory_cannot_escape_root(tmp_path: Path):
    """A lexically clean relative path through a symlinked directory is an escape."""
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy2(RUN_DIR / PRESERVED_READINESS_GATE, outside / "readiness-gate.json")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "link").symlink_to(outside, target_is_directory=True)

    declaration = single_artifact(preserved_declaration(), "readiness_gate")
    declaration["run_dir"] = str(run_dir)
    declaration["completion_records"] = []
    declaration["artifacts"][0]["path"] = "link/readiness-gate.json"

    report = run_intake(declaration)
    entry = report["artifact_results"][0]
    assert "artifact_path_escapes_root" in codes(report)
    assert entry["finding"] == "artifact_path_escapes_root"
    assert entry["observed_digest"] is None
    assert entry["verified"] is False
    assert report["intake_ok"] is False
    assert report["release_ready"] is False


@requires_real
def test_real_nested_directory_inside_root_still_resolves(tmp_path: Path):
    """Control for the escape case: a real nested directory inside the root is fine."""
    nested = tmp_path / "run" / "nested"
    nested.mkdir(parents=True)
    shutil.copy2(RUN_DIR / PRESERVED_READINESS_GATE, nested / "readiness-gate.json")

    declaration = single_artifact(preserved_declaration(), "readiness_gate")
    declaration["run_dir"] = str(tmp_path / "run")
    declaration["completion_records"] = []
    declaration["artifacts"][0]["path"] = "nested/readiness-gate.json"

    report = run_intake(declaration)
    entry = report["artifact_results"][0]
    assert entry["observed_digest"] == READINESS_GATE_DIGEST
    assert entry["verified"] is True
    assert "artifact_path_escapes_root" not in codes(report)


@requires_real
def test_symlinked_final_artifact_is_not_a_regular_file(tmp_path: Path):
    """A final-component symlink inside the root is still refused as non-regular."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    shutil.copy2(RUN_DIR / PRESERVED_READINESS_GATE, run_dir / "actual.json")
    (run_dir / "readiness-gate.json").symlink_to(run_dir / "actual.json")

    declaration = single_artifact(preserved_declaration(), "readiness_gate")
    declaration["run_dir"] = str(run_dir)
    declaration["completion_records"] = []
    declaration["artifacts"][0]["path"] = "readiness-gate.json"

    report = run_intake(declaration)
    entry = report["artifact_results"][0]
    assert "artifact_not_regular_file" in codes(report)
    assert entry["finding"] == "artifact_not_regular_file"
    assert entry["verified"] is False
    assert report["intake_ok"] is False


# --------------------------------------------------------------------------- #
# CLI contract over the same real retained inputs
# --------------------------------------------------------------------------- #


def run_cli(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts" / "a15_run_intake.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@requires_real
def test_cli_declared_run_writes_not_ready_report_and_exits_0(tmp_path: Path):
    """End-to-end CLI over the real retained inputs, report written outside the repo."""
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(preserved_declaration()), encoding="utf-8")
    report_path = tmp_path / "intake-report.json"

    completed = run_cli(
        "--declaration",
        str(declaration_path),
        "--repo-root",
        str(REPO_ROOT),
        "--now",
        "2026-09-21T12:00:00Z",
        "--report",
        str(report_path),
    )
    assert completed.returncode == 0, completed.stderr
    assert report_path.is_file()
    payload = json.loads(completed.stdout)
    assert payload["authoritative"] is False
    assert payload["production_verifier"] is False
    assert payload["release_ready"] is False
    assert payload["gate_state"] == "design_only"
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["report_digest"] == payload["report_digest"]
    assert written["release_ready"] is False
    artifacts = {item["artifact_id"]: item for item in written["artifact_results"]}
    for artifact_id, (name, digest, size) in PRESERVED_ARTIFACTS.items():
        assert artifacts[artifact_id]["path"] == name
        assert artifacts[artifact_id]["observed_digest"] == digest
        assert artifacts[artifact_id]["byte_length"] == size
