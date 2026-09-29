# SPDX-License-Identifier: AGPL-3.0-or-later
"""The A0 literal verification record is a prepared structure, never a claim."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
    assert (ROOT / record["predecessor_record"]).is_file()


def test_prepared_record_claims_no_execution_and_no_completion():
    record = _record()
    assert record["state"] == "prepared_not_executed"
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
