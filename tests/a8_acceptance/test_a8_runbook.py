# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift gate for the A8 physical runbook: every physical case has an
operator-executable section with capture and seal steps, and the runbook
carries no execution claim (the lane cannot execute without the unrelated
network + public origin)."""

from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "handover" / "A8_PHYSICAL_RUNBOOK.md"
INVENTORY = ROOT / "tests" / "a8_acceptance" / "inventory.v1.json"


def _physical_case_ids() -> set[str]:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    return {
        case["case_id"]
        for section in ("physical_positive_cases", "physical_negative_cases")
        for case in inventory[section]
    }


def test_runbook_exists_and_declares_design_only() -> None:
    source = RUNBOOK.read_text("utf-8")
    assert "design_only" in source
    assert "mycelium.internet_native_qualification.v1" in source


def test_runbook_covers_every_physical_case() -> None:
    source = RUNBOOK.read_text("utf-8")
    covered = set(re.findall(r"^## ([a-z][a-z0-9_]+)", source, re.MULTILINE))
    assert _physical_case_ids() <= covered


def test_every_case_section_has_capture_and_seal_steps() -> None:
    source = RUNBOOK.read_text("utf-8")
    sections = re.split(r"^## ", source, flags=re.MULTILINE)[1:]
    by_id = {
        section.splitlines()[0].strip(): section
        for section in sections
        if section.strip()
    }
    for case_id in _physical_case_ids():
        section = by_id[case_id]
        assert "Capture:" in section, case_id
        assert f"a8_run_physical_gate.py run {case_id}" in section, case_id
        assert "--seal" in section, case_id


def test_runbook_carries_no_execution_claims() -> None:
    source = RUNBOOK.read_text("utf-8")
    for claim in (
        '"result": "passed"',
        '"executed": true',
        "executed: true",
        "gate passed",
        "A8 complete",
    ):
        assert claim not in source, claim
    # "not executed" / "not_executed" statements are the honest form.
    assert "not executed" in source or "not_executed" in source
