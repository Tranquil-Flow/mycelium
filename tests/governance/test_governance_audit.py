from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.governance_audit import LEDGER_PATH, PROTOCOL, audit


ROOT = Path(__file__).resolve().parents[2]


def _isolated(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    ledger = json.loads((ROOT / LEDGER_PATH).read_text("utf-8"))
    paths = {
        LEDGER_PATH,
        ledger["governing_plan"],
        ledger["architecture_ledger"],
        ledger["contract_manifest"]["path"],
        *(item["client"] for item in ledger["authorized_product_actions"]),
    }
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    return root, ledger


def _write(root: Path, document: dict) -> None:
    (root / LEDGER_PATH).write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def test_governance_ledger_matches_architecture_and_never_claims_release() -> None:
    result = audit(ROOT)
    assert result == {
        "checked_actions": 8,
        "checked_capabilities": 15,
        "checked_milestones": 7,
        "findings": [],
        "ledger_digest": result["ledger_digest"],
        "ok": True,
        "protocol": PROTOCOL,
        "release_ready": False,
    }
    assert str(result["ledger_digest"]).startswith("sha256:")


def test_governance_rejects_unsupported_milestone_promotion(tmp_path: Path) -> None:
    root, ledger = _isolated(tmp_path)
    promoted = copy.deepcopy(ledger)
    promoted["milestones"][1]["capability_claims"]["4.8"] = "qualified"
    _write(root, promoted)

    result = audit(root)

    assert result["ok"] is False
    assert "milestone_promotion_unsupported:M18:4.8" in result["findings"]


def test_governance_rejects_unpinned_action_protocol_and_absent_plan(
    tmp_path: Path,
) -> None:
    root, ledger = _isolated(tmp_path)
    invalid = copy.deepcopy(ledger)
    for action in invalid["authorized_product_actions"]:
        action["protocols"] = []
    invalid["read_only_boundary_protocols"] = []
    invalid["governing_plan"] = "docs/superpowers/plans/absent.md"
    _write(root, invalid)

    result = audit(root)

    assert result["ok"] is False
    assert "boundary_protocols_unpinned" in result["findings"]
    assert "governing_plan_unavailable" in result["findings"]


def test_governance_rejects_unknown_ledger_field_and_duplicate_milestone(
    tmp_path: Path,
) -> None:
    root, ledger = _isolated(tmp_path)
    invalid = copy.deepcopy(ledger)
    invalid["unexpected"] = True
    invalid["milestones"].append(copy.deepcopy(invalid["milestones"][0]))
    _write(root, invalid)

    result = audit(root)

    assert result["ok"] is False
    assert "ledger_shape_invalid" in result["findings"]
    assert "milestone_entry_invalid" in result["findings"]
