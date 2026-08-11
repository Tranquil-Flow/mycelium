from __future__ import annotations

from pathlib import Path

from scripts.governance_gate import PROTOCOL, run


ROOT = Path(__file__).resolve().parents[2]


def test_aggregate_governance_gate_requires_every_child_authority() -> None:
    result = run(ROOT)
    assert result["protocol"] == PROTOCOL
    assert result["ok"] is True
    assert result["release_ready"] is False
    assert set(result["children"]) == {"claim_boundary", "contracts", "governance"}
    assert all(child["ok"] is True for child in result["children"].values())
