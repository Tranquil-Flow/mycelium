# SPDX-License-Identifier: AGPL-3.0-only
"""A failing or restarting source degrades its own scope, never the snapshot.

The internet-native source was the only product source read without the
scoped degradation every other source has: while the cold-restart owner was
restarting, its error escaped ``_publish`` and the whole snapshot answered
503 ``product_snapshot_unavailable`` (A6 browser matrix on hardware).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mycelium_internet.contracts import compatibility_fixtures as internet_fixtures
import mycelium_product_spine
from mycelium_product_spine import ProductEvidenceApplication, ProductProjector
from tests.product_spine.test_application import _request, _sources

RESTARTING = "cold_route_restart_in_progress"


def _internet_native() -> dict:
    fixtures = internet_fixtures()
    activation = fixtures["internet-activation-observation-v1.json"]
    return {
        "bootstrap_status": fixtures["internet-bootstrap-status-v1.json"],
        "activation_observation": activation,
        "activation_history": [activation],
        "relay_projection": None,
        "qualification": None,
    }


def _snapshot(app) -> tuple[int, dict]:
    status, body = asyncio.run(_request(app, "/v1/product/snapshot"))
    return status, json.loads(body)


def _source(snapshot: dict, source_id: str) -> dict:
    return next(item for item in snapshot["source_states"] if item["source_id"] == source_id)


def test_failing_internet_native_source_degrades_to_unknown_and_keeps_history() -> None:
    members, route, qualification = _sources()
    calls = {"count": 0}

    def internet_native() -> dict:
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("private upstream detail")
        return _internet_native()

    app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"r" * 32),
        membership_source=lambda: members,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        internet_native_source=internet_native,
        clock_unix_ms=lambda: 1_500_000,
    )
    first_status, first = _snapshot(app)
    status, snapshot = _snapshot(app)

    assert first_status == 200
    assert status == 200, snapshot
    assert snapshot["publication"]["cursor"] == first["publication"]["cursor"] + 1
    internet = snapshot["internet_native"]
    current = internet["activation_observation"]
    assert (current["path_class"], current["path_source"], current["freshness"]) == (
        "unknown", "unknown", "unknown")
    assert internet["bootstrap_status"]["freshness"] == "unknown"
    assert internet["bootstrap_status"]["route_state"] == "unknown"
    # The verified observation retained before the failure is not dropped.
    assert [item["observation_id"] for item in internet["activation_history"][:-1]] == [
        "fixture-observation"]
    assert internet["activation_history"][-1] == current
    assert _source(snapshot, "membership-source")["status"] == "current"
    assert "private upstream detail" not in json.dumps(snapshot)


def test_restarting_sources_are_reported_restarting_and_the_route_unavailable() -> None:
    members, route, qualification = _sources()
    unavailable = mycelium_product_spine.ProductSourceUnavailable

    def restarting():
        raise unavailable(RESTARTING)

    app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"r" * 32),
        membership_source=restarting,
        assignment_source=restarting,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        internet_native_source=restarting,
        recovery_source=restarting,
        clock_unix_ms=lambda: 1_500_000,
    )
    status, snapshot = _snapshot(app)

    assert status == 200, snapshot
    for source_id in ("membership-source", "assignment-source", "recovery-source"):
        state = _source(snapshot, source_id)
        assert (state["status"], state["reason_code"]) == ("missing", RESTARTING), state
    assert snapshot["publication"]["source_mode"] == "degraded"
    assert any(notice["code"] == RESTARTING for notice in snapshot["notices"])
    bootstrap = snapshot["internet_native"]["bootstrap_status"]
    assert (bootstrap["route_state"], bootstrap["freshness"]) == ("unavailable", "unknown")


@pytest.mark.parametrize("code", ["", "Not A Code", "x" * 65, None, 7])
def test_source_unavailable_reason_is_a_bounded_code(code) -> None:
    with pytest.raises(ValueError, match="product_source_reason_invalid"):
        mycelium_product_spine.ProductSourceUnavailable(code)


def test_invalid_internet_native_evidence_still_fails_closed() -> None:
    members, route, qualification = _sources()
    document = _internet_native()
    document["unexpected"] = True
    app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"r" * 32),
        membership_source=lambda: members,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        internet_native_source=lambda: document,
        clock_unix_ms=lambda: 1_500_000,
    )
    status, body = asyncio.run(_request(app, "/v1/product/snapshot"))

    assert status == 503
    assert json.loads(body) == {"error": "product_snapshot_unavailable"}
