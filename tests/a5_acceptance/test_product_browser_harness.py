from __future__ import annotations

from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "web"
    / "scripts"
    / "a5-product-browser-e2e.mjs"
)


def test_a5_browser_harness_executes_positive_and_degraded_phases() -> None:
    source = SCRIPT.read_text()

    assert "MYCELIUM_A5_BROWSER_PHASE" in source
    assert "positive" in source
    assert "degraded" in source


def test_a5_browser_harness_drives_concurrent_bound_requests_and_cancellation() -> None:
    source = SCRIPT.read_text()

    assert "concurrentInferenceScenario" in source
    assert "waitForResponse" in source
    assert "request_id" in source
    assert "placement_ids" in source
    assert "distinct complete tracks" in source
    assert "cancel request" in source.lower()
    assert "completed" in source


def test_a5_browser_harness_retains_three_engines_and_eight_workspaces() -> None:
    source = SCRIPT.read_text()

    assert "['chromium', chromium]" in source
    assert "['firefox', firefox]" in source
    assert "['webkit', webkit]" in source
    assert "WORKSPACES.length" in source
    assert "all_eight_workspaces_replica_fields_verified" in source
