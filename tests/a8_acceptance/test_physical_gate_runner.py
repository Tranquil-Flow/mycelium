# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gates for the A8 physical-gate runner and qualification
sealer. The runner is inert and fail-closed without live infrastructure and
never writes evidence unless a case genuinely executes."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from mycelium_internet.physical import (
    A8_PHYSICAL_CASES,
    PEER_REQUIRED_CASES,
    PeerRequired,
    PhysicalGateError,
    execute_case,
    preflight_document,
    seal_qualification,
)
from mycelium_internet.contracts import (
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    validate_internet_native_qualification,
)

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "a8_run_physical_gate.py"
INVENTORY = ROOT / "tests" / "a8_acceptance" / "inventory.v1.json"
PLACEHOLDER = "sha256:" + "a" * 64


def _inventory_cases() -> set[str]:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    return {
        case["case_id"]
        for section in ("physical_positive_cases", "physical_negative_cases")
        for case in inventory[section]
    }


def test_case_registry_matches_the_acceptance_inventory() -> None:
    assert set(A8_PHYSICAL_CASES) == _inventory_cases()


def test_peer_required_cases_are_exactly_the_external_peer_gates() -> None:
    expected = {
        "unrelated_https_invite_without_tailscale",
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "unqualified_external_member",
        "tailscale_unavailable",
        "ssh_unavailable",
    }
    assert set(PEER_REQUIRED_CASES) == expected


def test_preflight_dry_run_is_inert_and_claims_nothing() -> None:
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=PLACEHOLDER,
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is False
    assert document["result"] == "not_executed"
    assert document["evidence_digests"] == []
    assert document["projection_digest"] is None
    assert document["protocol"] == INTERNET_NATIVE_QUALIFICATION_PROTOCOL


def test_cli_preflight_exits_zero_with_inert_envelope(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/python3.14",
            str(RUNNER),
            "preflight",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            PLACEHOLDER,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    validate_internet_native_qualification(document)
    assert document["executed"] is False


def test_run_without_reachable_origin_fails_closed_and_writes_nothing(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "cleartext_or_redirect_bootstrap",
            origin="https://seed.example.invalid",
            evidence_root=evidence_root,
            adapter=None,
        )
    assert exc_info.value.code == "physical_infrastructure_unavailable"
    assert list(evidence_root.iterdir()) == []


def test_peer_required_cases_fail_closed_without_a_peer(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    for case_id in PEER_REQUIRED_CASES:
        with pytest.raises(PeerRequired) as exc_info:
            execute_case(
                case_id,
                origin="https://seed.example.invalid",
                evidence_root=evidence_root,
                adapter=None,
            )
        assert exc_info.value.code == "peer_required"
    assert list(evidence_root.iterdir()) == []


def test_unknown_case_id_is_rejected() -> None:
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "not_a_real_case",
            origin="https://seed.example.invalid",
            evidence_root=None,
            adapter=None,
        )
    assert exc_info.value.code == "case_unknown"


def test_seal_writes_locked_owner_private_record(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=PLACEHOLDER,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    validate_internet_native_qualification(document)
    record = seal_qualification(
        document,
        evidence_root=evidence_root,
    )
    assert record.exists()
    assert record.read_text("utf-8").startswith("{")
    assert record.stat().st_mode & 0o777 == 0o400
    assert evidence_root.stat().st_mode & 0o022 == 0


def test_seal_rejects_tampered_digest_form_with_negative_control(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=PLACEHOLDER,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    # Negative control: the un-tampered record still validates.
    validate_internet_native_qualification(document)
    tampered = json.loads(json.dumps(document))
    tampered["projection_digest"] = "sha256:" + "b" * 63
    assert tampered["projection_digest"] != document["projection_digest"]
    with pytest.raises(ValueError):
        validate_internet_native_qualification(tampered)
    with pytest.raises(ValueError):
        seal_qualification(tampered, evidence_root=evidence_root)
    assert list(evidence_root.iterdir()) == []


def test_seal_rejects_not_executed_passed_record(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=PLACEHOLDER,
    )
    document["result"] = "passed"
    with pytest.raises(ValueError):
        seal_qualification(document, evidence_root=evidence_root)
    assert list(evidence_root.iterdir()) == []


def test_cli_run_without_infra_exits_nonzero_and_writes_nothing(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/python3.14",
            str(RUNNER),
            "run",
            "cleartext_or_redirect_bootstrap",
            "--origin",
            "https://seed.example.invalid",
            "--evidence-root",
            str(evidence_root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "physical_infrastructure_unavailable" in completed.stderr
    assert list(evidence_root.iterdir()) == []
