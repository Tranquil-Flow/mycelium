# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: per-peer liveness and last-request timings are READ from the open route, against the real detector."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.diagnostics.collect import collect
from mycelium_live.liveness import (
    LivenessPolicy,
    LivenessSubject,
    SubjectKind,
    TrafficAwareLivenessDetector,
)

NODE = "nodeaaaa1111"


class PhysicalLiveRoute:
    """Stand-in with the real detector and the attributes the live route keeps (read-only use)."""

    def __init__(self, detector, inferences=()):
        self._open, self._closed = True, False
        self._liveness = detector
        self._recent_inferences = list(inferences)

    def _monotonic_ms(self):
        return int(time.monotonic() * 1000)


def detector(misses):
    policy = LivenessPolicy()
    live = TrafficAwareLivenessDetector(policy=policy)
    subject = LivenessSubject(subject_id=NODE, kind=SubjectKind.PEER, membership_generation=1)
    base = int(time.monotonic() * 1000) - 60_000  # last fresh a minute ago
    live.register_subject(subject, observed_at_ms=base)
    for i in range(misses):  # one explicit idle-probe miss per keepalive interval
        live.record_keepalive_miss(subject, observed_at_ms=base + policy.idle_keepalive_ms * (i + 1))
    return live, policy


def make_app():
    members = ({"node_id": NODE, "generation": 1, "lifecycle_state": "active", "liveness_status": "idle_fresh",
                "liveness_age_seconds": 2.0, "liveness_dead": False},)
    return SimpleNamespace(installation_role="owner", root=None,
                           authority=SimpleNamespace(coordinator=SimpleNamespace(members=lambda: members),
                                                     control_mailbox=None))


def test_a_quarantined_peer_is_read_from_the_open_route_and_explained():
    live, policy = detector(misses=LivenessPolicy().quarantine_misses)
    route = PhysicalLiveRoute(live)  # kept alive for the call
    doc = collect(make_app())
    peer = doc["peers"][0]
    assert peer["liveness"]["available"] is True and peer["liveness"]["label"] == "quarantined"
    assert peer["liveness"]["consecutive_misses"] >= policy.quarantine_misses
    item = next(f for f in doc["findings"] if f["id"] == "peer_quarantined")
    assert "Chats are refused because The joined Mac is quarantined" in item["title"]
    del route


def test_a_fresh_peer_is_fresh():
    live, _ = detector(misses=0)
    route = PhysicalLiveRoute(live)
    peer = collect(make_app())["peers"][0]
    assert peer["liveness"]["label"] == "fresh"
    assert not any(f["id"].startswith("peer_") and f["id"] != "peer_clock_skew" for f in collect(make_app())["findings"])
    del route


def test_last_request_timings_come_from_the_route_and_per_hop_is_honestly_unavailable():
    live, _ = detector(misses=0)
    inference = {"context_tokens": 12, "output_tokens": 20, "prefill_ms": 180.0, "ttft_ms": 260.0, "tpot_ms": 50.0,
                 "total_ms": 1250.0, "peer_counter_deltas": [{"node_id": NODE, "frames_sent": 40,
                                                              "frames_received": 41, "applied_operation_count": 40}]}
    route = PhysicalLiveRoute(live, [inference])
    timings = collect(make_app())["timings"]
    last = timings["last_request"]
    assert timings["available"] is True
    assert last["tokens_per_second"] == pytest.approx(16.0) and last["decode_tokens_per_second"] == pytest.approx(20.0)
    assert last["ttft_ms"] == 260.0 and last["frames_by_peer"][0]["frames_sent"] == 40
    assert timings["per_hop"]["available"] is False and "hop" in timings["per_hop"]["reason"]
    del route


def test_a_closed_route_is_not_read():
    live, _ = detector(misses=0)
    route = PhysicalLiveRoute(live)
    route._closed = True
    doc = collect(make_app())
    assert doc["peers"][0]["liveness"]["available"] is False
    assert doc["timings"]["available"] is False
    del route
