# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable direct/relay transition history projected from live observations."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import runpy

import pytest

from mycelium_product_spine import ProductProjector
from mycelium_product_spine.app import ProductEvidenceApplication
from mycelium_product_spine.projector import _unknown_internet_native
from mycelium_product_spine.route_explorer import project_route_explorer
from mycelium_product_spine.transport_history import MAX_RETAINED, TransportTransitionHistory

ROOT = Path(__file__).resolve().parents[2]
_conformance = runpy.run_path(str(ROOT / "tests/a14_acceptance/conformance.py"))
NOW = 1_000_000
ENDPOINT = "sha256:" + "a" * 64


def _observation(generation: int, transport: str, *, endpoint: str = ENDPOINT,
                 observed_at: int | None = None, observation_id: str | None = None):
    base = copy.deepcopy(_unknown_internet_native(NOW)["activation_observation"])
    base.update(observation_id=observation_id or f"transport-{generation}-{transport}",
                connection_generation=generation, path_class=transport,
                path_source="bound_live_connection", endpoint_pseudonym=endpoint,
                observed_at_unix_ms=observed_at if observed_at is not None else NOW - 5000 + generation,
                evidence_lifetime_until_unix_ms=NOW + 600_000, freshness="current",
                metrics=dict(rtt_ms=4, warm_rtt_ms=2, jitter_ms=1, goodput_bytes_per_second=1000,
                             loss_ratio=0, sample_count=2, measured_zero=True))
    return base


def _document(*history):
    document = _unknown_internet_native(NOW)
    history = list(history) or [document["activation_observation"]]
    document.update(activation_history=history, activation_observation=history[-1])
    return document


def _explorer(internet):
    members, route, _internet = _conformance["inputs"](NOW)
    snapshot = _conformance["ConformanceProjector"](pseudonym_salt=b"c" * 32).project(
        members=members, route_status=route, qualification=None, internet_native=internet,
        now_unix_ms=NOW)
    return snapshot["route_explorer"]


def _observed(explorer):
    return [(s["transport"], s["connection_generation"], s["history"])
            for s in explorer["segments"] if s["edge_kind"] == "transport_observation"]


def test_direct_then_relay_retains_the_direct_segment_and_adds_the_relay_segment():
    ledger = TransportTransitionHistory()
    first = ledger.observe(_document(_observation(1, "direct")), now_unix_ms=NOW)
    before = _explorer(first)
    # The live source may drop the earlier observation (e.g. a cold route restart).
    second = ledger.observe(_document(_observation(2, "relay")), now_unix_ms=NOW)
    after = _explorer(second)
    assert _observed(before) == [("direct", 1, False)]
    assert _observed(after) == [("direct", 1, True), ("relay", 2, False)]
    direct_before = [s for s in before["segments"] if s["transport"] == "direct"][0]
    direct_after = [s for s in after["segments"] if s["transport"] == "direct"][0]
    assert {k: v for k, v in direct_before.items() if k != "history"} == {
        k: v for k, v in direct_after.items() if k != "history"}
    assert ledger.transitions() == [{
        "endpoint_pseudonym": ENDPOINT, "from_path_class": "direct", "to_path_class": "relay",
        "from_connection_generation": 1, "to_connection_generation": 2,
        "from_observation_id": "transport-1-direct", "to_observation_id": "transport-2-relay"}]


def test_a_rewritten_observation_is_refused_and_never_becomes_current():
    ledger = TransportTransitionHistory()
    ledger.observe(_document(_observation(1, "direct")), now_unix_ms=NOW)
    forged = _observation(1, "relay", observation_id="transport-1-direct")
    published = ledger.observe(_document(forged), now_unix_ms=NOW)
    assert ledger.rejections["rewrite"] == 1
    assert published["activation_observation"]["path_class"] == "unknown"
    assert published["relay_projection"] is None
    assert [item["path_class"] for item in published["activation_history"]] == ["direct", "unknown"]
    assert _observed(_explorer(published)) == [("direct", 1, False)]


@pytest.mark.parametrize("candidate,reason", [
    (lambda: _observation(0, "relay", observed_at=NOW), "regression"),
    (lambda: _observation(3, "relay", observed_at=NOW - 10_000), "regression"),
    (lambda: _observation(2, "relay", observed_at=NOW,
                          observation_id="transport-2-contradiction"), "contradiction"),
])
def test_stale_or_contradicting_transitions_are_refused(candidate, reason):
    ledger = TransportTransitionHistory()
    ledger.observe(_document(_observation(1, "direct"), _observation(2, "direct")),
                   now_unix_ms=NOW)
    published = ledger.observe(_document(candidate()), now_unix_ms=NOW)
    assert ledger.rejections[reason] == 1
    assert published["activation_observation"]["path_class"] == "unknown"
    assert [(item["path_class"], item["connection_generation"])
            for item in published["activation_history"][:-1]] == [("direct", 1), ("direct", 2)]


def test_unknown_current_does_not_turn_the_latest_observation_into_history():
    ledger = TransportTransitionHistory()
    ledger.observe(_document(_observation(1, "direct"), _observation(2, "relay")),
                   now_unix_ms=NOW)
    published = ledger.observe(_document(), now_unix_ms=NOW)
    assert published["activation_observation"]["path_class"] == "unknown"
    assert _observed(_explorer(published)) == [("direct", 1, True), ("relay", 2, False)]


def test_per_endpoint_history_is_independent():
    other = "sha256:" + "b" * 64
    ledger = TransportTransitionHistory()
    published = ledger.observe(_document(
        _observation(1, "direct"), _observation(1, "relay", endpoint=other),
        _observation(2, "relay")), now_unix_ms=NOW)
    explorer = _explorer(published)
    by_endpoint = {}
    for segment in explorer["segments"]:
        if segment["edge_kind"] == "transport_observation":
            by_endpoint.setdefault(segment["destination"], []).append(
                (segment["transport"], segment["history"]))
    assert sorted(by_endpoint.values()) == sorted([
        [("direct", True), ("relay", False)], [("relay", False)]])


def test_ledger_is_bounded_and_drops_the_oldest_deterministically():
    ledger = TransportTransitionHistory()
    for generation in range(1, MAX_RETAINED + 6):
        transport = "direct" if generation % 2 else "relay"
        published = ledger.observe(_document(_observation(generation, transport)),
                                   now_unix_ms=NOW)
    history = published["activation_history"]
    assert len(history) == MAX_RETAINED
    assert history[0]["connection_generation"] == 6
    assert history[-1]["connection_generation"] == MAX_RETAINED + 5


def test_refresh_and_republication_do_not_change_transition_segments():
    ledger = TransportTransitionHistory()
    document = _document(_observation(1, "direct"), _observation(2, "relay"))
    first = _explorer(ledger.observe(document, now_unix_ms=NOW))
    second = _explorer(ledger.observe(copy.deepcopy(document), now_unix_ms=NOW + 1))
    assert first["segments"] == second["segments"]


def test_live_product_application_keeps_transition_history_across_source_restart():
    sources = [_document(_observation(1, "direct")), _document(_observation(2, "relay"))]
    calls = {"count": 0}

    def internet_native():
        document = sources[min(calls["count"], len(sources) - 1)]
        calls["count"] += 1
        return document

    members, route, _internet = _conformance["inputs"](NOW)
    app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"c" * 32),
        membership_source=lambda: members, route_source=lambda: route,
        qualification_source=lambda: None, internet_native_source=internet_native,
        clock_unix_ms=lambda: NOW)
    app._publish()
    snapshot = app._publish()
    explorer = snapshot["route_explorer"]
    assert explorer["source_mode"] in ("live", "degraded")
    assert _observed(explorer) == [("direct", 1, True), ("relay", 2, False)]
    assert explorer == project_route_explorer(
        {k: v for k, v in snapshot.items() if k != "route_explorer"})
    assert "RAW-ENDPOINT-CANARY" not in json.dumps(snapshot)
