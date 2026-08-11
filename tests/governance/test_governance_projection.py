from pathlib import Path

from mycelium_governance import PROTOCOL, governance_readiness


ROOT = Path(__file__).resolve().parents[2]


def test_governance_projection_is_browser_safe_and_never_release_ready() -> None:
    projection = governance_readiness(ROOT, clock_unix_ms=lambda: 1234)

    assert projection["protocol"] == PROTOCOL
    assert projection["observed_at_unix_ms"] == 1234
    assert projection["source_kind"] == "source_control"
    assert projection["governance_gate_ok"] is True
    assert projection["release_ready"] is False
    assert projection["authorized_product_action_count"] == 8
    assert len(projection["release_exclusions"]) >= 1
    assert all("/Users/" not in str(value) for value in projection.values())
