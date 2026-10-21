# SPDX-License-Identifier: AGPL-3.0-only
"""A0 §7.4: aggregate failures against independent git-backed source copies.

The contract auditor resolves pinned fixtures/sources relative to its installed
checkout, not the supplied manifest. These copies exercise the manifest registry
and aggregate wiring, not independent verification of copied owner-source pins.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.claim_boundary_audit import audit_repository
from scripts.contract_audit import audit as audit_contracts
from scripts.governance_audit import LEDGER_PATH, REGISTRATION_PATH, audit as audit_governance
from scripts.governance_gate import PROTOCOL, run

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "contracts/contract-manifest.v1.json"
OBSERVATORY = "ui/web/src/data/observatorySource.ts"
GATE = ROOT / "scripts/governance_gate.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _copy(root: Path) -> Path:
    """Copy real audit inputs, with an independent index for the source scan."""
    ledger = json.loads((ROOT / LEDGER_PATH).read_bytes())
    registrations = json.loads((ROOT / REGISTRATION_PATH).read_bytes())
    paths = {
        LEDGER_PATH,
        REGISTRATION_PATH,
        *(item["owner_source"] for item in registrations["registrations"]),
        MANIFEST,
        ledger["governing_plan"],
        ledger["architecture_ledger"],
        "ui/web/src/app/contracts.ts",
        OBSERVATORY,
        *(action["client"] for action in ledger["authorized_product_actions"]),
    }
    for relative in sorted(paths):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    _git(root, "init", "-q")
    # The claim audit reads ONLY indexed production source, not untracked files.
    _git(root, "add", "--", OBSERVATORY)
    return root


def _cli(root: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(root), "--json"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def _assert_positive(root: Path) -> None:
    claims = cast(dict[str, Any], audit_repository(root))
    assert claims["ok"] is True
    assert claims["scan"]["scanned_source_files"] == 1
    assert claims["scan"]["tracked_files"] == 1
    assert audit_governance(root)["ok"] is True
    assert audit_contracts(root / MANIFEST)["ok"] is True
    expected = cast(dict[str, Any], run(root))
    assert expected["protocol"] == PROTOCOL
    assert expected["ok"] is True
    assert expected["release_ready"] is False
    assert set(expected["children"]) == {"claim_boundary", "contracts", "governance"}
    assert all(child["ok"] is True and child["finding_count"] == 0 for child in expected["children"].values())
    assert _cli(root) == (0, expected)


def test_unmodified_git_backed_copy_passes_aggregate(tmp_path: Path) -> None:
    _assert_positive(_copy(tmp_path / "positive"))


@pytest.mark.parametrize(
    ("mutation", "finding"),
    [
        ("unknown_ledger_field", "ledger_shape_invalid"),
        ("invalid_state", "capability_entry_invalid"),
        ("duplicate_milestone", "milestone_entry_invalid"),
        ("absent_governing_plan", "governing_plan_unavailable"),
    ],
)
def test_single_ledger_rejection_has_healthy_copy_and_nonzero_gate(
    tmp_path: Path, mutation: str, finding: str
) -> None:
    root = _copy(tmp_path / mutation)
    _assert_positive(root)
    path = root / LEDGER_PATH
    ledger = json.loads(path.read_bytes())
    if mutation == "unknown_ledger_field":
        ledger["unexpected"] = True
    elif mutation == "invalid_state":
        ledger["capabilities"][0]["state"] = "invented_state"
    elif mutation == "duplicate_milestone":
        ledger["milestones"].append(json.loads(json.dumps(ledger["milestones"][0])))
    else:
        ledger["governing_plan"] = "docs/superpowers/plans/absent.md"
    path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")

    findings = audit_governance(root)["findings"]
    assert finding in findings
    expected = cast(dict[str, Any], run(root))
    assert expected["ok"] is False
    assert expected["release_ready"] is False
    assert {name for name, result in expected["children"].items() if not result["ok"]} == {"governance"}
    assert expected["children"]["governance"]["finding_count"] == len(findings)
    assert _cli(root) == (1, expected)


def test_registration_owner_source_drift_fails_aggregate(tmp_path: Path) -> None:
    root = _copy(tmp_path / "source_drift")
    _assert_positive(root)
    path = root / REGISTRATION_PATH
    registration = json.loads(path.read_bytes())
    name = "mycelium.interactive_runtime.v1"
    entry = next(item for item in registration["registrations"] if item["name"] == name)
    source = root / entry["owner_source"]
    source.write_text(source.read_text("utf-8").replace(f'"{name}"', '"mycelium.drifted.v1"'), encoding="utf-8")
    assert f"boundary_protocol_registration_source_invalid:{name}" in audit_governance(root)["findings"]
    assert run(root)["ok"] is False
    assert _cli(root)[0] == 1


def test_fabricated_registration_cannot_claim_ledger_as_source(tmp_path: Path) -> None:
    root = _copy(tmp_path / "fabricated")
    _assert_positive(root)
    path = root / REGISTRATION_PATH
    registration = json.loads(path.read_bytes())
    name = "mycelium.fabricated.v1"
    registration["registrations"].append(
        {"name": name, "kind": "read_only", "owner_source": LEDGER_PATH}
    )
    path.write_text(json.dumps(registration, sort_keys=True) + "\n", encoding="utf-8")
    ledger_path = root / LEDGER_PATH
    ledger = json.loads(ledger_path.read_bytes())
    ledger["read_only_boundary_protocols"].append(name)
    ledger_path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
    findings = audit_governance(root)["findings"]
    assert f"boundary_protocol_registration_source_invalid:{name}" in findings
    assert f"boundary_protocol_unregistered:{name}" in findings
    assert run(root)["ok"] is False
    assert _cli(root)[0] == 1


@pytest.mark.parametrize(
    ("mutation", "child", "finding"),
    [
        ("unlisted_post", "claim_boundary", "observatory_ui_write_surface"),
        ("unregistered_protocol", "contracts", "manifest: contract registry mismatch"),
        ("unknown_ledger_protocol", "governance", "boundary_protocol_unregistered:mycelium.unknown_boundary.v1"),
        ("unknown_action_protocol", "governance", "boundary_protocol_unregistered:mycelium.unknown_action.v1"),
        ("promoted_milestone", "governance", "milestone_promotion_unsupported:M18:4.8"),
    ],
)
def test_negative_copies_refuse_exactly_one_child(
    tmp_path: Path, mutation: str, child: str, finding: str
) -> None:
    root = _copy(tmp_path / mutation)
    _assert_positive(root)  # Each separate copy was healthy before mutation.
    if mutation == "unlisted_post":
        with (root / OBSERVATORY).open("ab") as handle:
            handle.write(b"\nfetch('/__mycelium/unlisted', { method: 'POST' });\n")
        findings = audit_repository(root)["findings"]
        assert any(
            item["code"] == finding and item["path"] == OBSERVATORY
            and item["subject"] == "POST"
            for item in findings
        )
    elif mutation == "unregistered_protocol":
        path = root / MANIFEST
        manifest = json.loads(path.read_bytes())
        manifest["contracts"][0]["protocol"] = "mycelium.unregistered_boundary.v1"
        path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        assert finding in audit_contracts(path)["drift"]
    elif mutation == "unknown_ledger_protocol":
        path = root / LEDGER_PATH
        ledger = json.loads(path.read_bytes())
        ledger["read_only_boundary_protocols"][0] = "mycelium.unknown_boundary.v1"
        path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
        assert audit_contracts(root / MANIFEST)["ok"] is True
    elif mutation == "unknown_action_protocol":
        path = root / LEDGER_PATH
        ledger = json.loads(path.read_bytes())
        ledger["authorized_product_actions"][0]["protocols"].append("mycelium.unknown_action.v1")
        path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
        assert audit_contracts(root / MANIFEST)["ok"] is True
    else:
        path = root / LEDGER_PATH
        ledger = json.loads(path.read_bytes())
        milestone = next(row for row in ledger["milestones"] if row["id"] == "M18")
        milestone["capability_claims"]["4.8"] = "qualified"
        path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
        assert finding in audit_governance(root)["findings"]

    expected = cast(dict[str, Any], run(root))
    assert expected == run(root)  # Deterministic summary on unchanged bytes.
    assert expected["protocol"] == PROTOCOL
    assert expected["ok"] is False
    if mutation == "unknown_ledger_protocol":
        assert finding in audit_governance(root)["findings"]
    assert expected["release_ready"] is False
    assert {name for name, result in expected["children"].items() if not result["ok"]} == {child}
    assert expected["children"][child]["finding_count"] > 0
    assert all(
        result["finding_count"] == 0
        for name, result in expected["children"].items() if name != child
    )
    assert _cli(root) == (1, expected)
