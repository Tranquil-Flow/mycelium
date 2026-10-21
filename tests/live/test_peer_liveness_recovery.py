# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 items h + j: a quarantined peer recovers, its liveness is visible, and a remote peer's idle probe can be answered.

Live demo (2026-10-21): the owner's route refused every chat with ``affected_peer_quarantined`` and nothing recovered it.
Findings pinned here:

* the real ``_verify_observation`` feeds the detector a *signed* fresh receipt, so ``recovery_fresh_observations``
  consecutive answered probes un-quarantine a peer (this part already worked);
* but the idle probe's whole wire life was ``IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS`` = 0.5 s -- the TTL of the signed
  ``health`` command -- while a polled mailbox needs a claim hop, the execution and a result hop.  For a peer reached
  through a relay the probe expired (9-24 per run, 523 of 523 on the laptop), every expiry is a "miss", and two answered
  probes in a row almost never happen: the quarantine was self-sustaining.  A managed-control session now carries a
  longer floor for idle probes only (active-request liveness keeps its 0.5 s / 2 s detection budget);
* the per-peer state is now readable: ``/api/status`` -> ``peer_liveness``.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import mycelium_live.route as route_module
from mycelium_live.liveness import (
    LivenessState,
    LivenessSubject,
    ObservationSource,
    SubjectKind,
    TrafficAwareLivenessDetector,
)
from mycelium_live.route import AffectedPeerQuarantined, PhysicalLiveRoute


class Session:
    def __init__(self, *, fail=False, floor=None):
        self.fail = fail
        self.deadlines = []
        if floor is not None:
            self.idle_probe_floor_seconds = floor

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        self.deadlines.append(deadline_monotonic_s)
        if self.fail:
            raise TimeoutError("command_deadline_exceeded")
        return {"protocol": "mycelium.node_observation.v1", "details": {}}


def make_route(monkeypatch, session, clock):
    """A route whose ``_verify_observation`` is the REAL one over a stub controller (signed receipt -> detector)."""
    detector = TrafficAwareLivenessDetector()
    subject = LivenessSubject("node-2", SubjectKind.PEER, 1)
    detector.register_subject(subject, observed_at_ms=1_000)
    monkeypatch.setattr(route_module, "time", SimpleNamespace(
        monotonic=lambda: clock["t"] / 1000.0, time=__import__("time").time, sleep=__import__("time").sleep))
    route = PhysicalLiveRoute.__new__(PhysicalLiveRoute)
    route._liveness = detector
    route._liveness_subjects = {"node-2": subject}
    route._open, route._closed = True, False
    route._lock = threading.RLock()
    route._sessions = {"node-2": session}
    route._active_route_requests = {}
    route._last_health = {}
    route._signed_observations = []
    route._peers = {"node-2": SimpleNamespace(process_transport="managed")}
    route._identities = {"node-2": {"process_id": "p"}}
    route._plan = {"run_id": "r", "deployment_id": "d"}
    route._endpoints = {"node-2": {"endpoint_id": "e"}}
    route._verification_keys = {}
    route._controller = SimpleNamespace(_verified_observation=lambda response, **kw: {"verified": True, "details": {}})
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"
    route._record_measured_round_trip = lambda *a, **k: None
    return route, detector, subject


def tick(route, clock, t):
    clock["t"] = t
    route._monitor_idle_keepalives_once()


def test_two_answered_probes_in_a_row_un_quarantine_a_peer(monkeypatch):
    clock = {"t": 6_000}
    session = Session(fail=True)
    route, detector, subject = make_route(monkeypatch, session, clock)
    for t in (6_000, 11_000, 16_000):
        tick(route, clock, t)
    assert detector.subject_snapshot(subject).state is LivenessState.QUARANTINED
    with pytest.raises(AffectedPeerQuarantined):
        route._reject_if_affected_peer_quarantined(frozenset({"node-2"}))

    session.fail = False                       # the link is healthy again
    tick(route, clock, 21_000)
    assert detector.subject_snapshot(subject).state is LivenessState.QUARANTINED   # one fresh answer is not enough
    tick(route, clock, 26_000)
    assert detector.subject_snapshot(subject).state is LivenessState.RECOVERED
    route._reject_if_affected_peer_quarantined(frozenset({"node-2"}))              # admission works again
    assert route._peer_subject_is_quarantined("node-2") is False


def test_a_miss_between_two_answers_does_not_count_as_recovered(monkeypatch):
    clock = {"t": 6_000}
    session = Session(fail=True)
    route, detector, subject = make_route(monkeypatch, session, clock)
    for t in (6_000, 11_000, 16_000):
        tick(route, clock, t)
    session.fail = False
    tick(route, clock, 21_000)
    session.fail = True
    tick(route, clock, 26_000)
    session.fail = False
    tick(route, clock, 31_000)
    # one answer since the miss: not recovered (the detector has dropped it back to suspect, which admits requests)
    assert detector.subject_snapshot(subject).state is LivenessState.SUSPECT


def test_a_probe_to_a_managed_control_peer_gets_its_session_floor(monkeypatch):
    clock = {"t": 6_000}
    session = Session(floor=4.0)
    route, _detector, _subject = make_route(monkeypatch, session, clock)
    tick(route, clock, 6_000)
    assert session.deadlines == [pytest.approx(6.0 + 4.0)]


def test_a_session_without_a_floor_keeps_the_half_second_probe(monkeypatch):
    clock = {"t": 6_000}
    session = Session()
    route, _detector, _subject = make_route(monkeypatch, session, clock)
    tick(route, clock, 6_000)
    assert session.deadlines == [pytest.approx(6.0 + 0.5)]


def test_the_managed_control_session_declares_the_floor():
    from mycelium_a13_macos.control_session import ManagedNodeSession
    assert 3.0 <= ManagedNodeSession.idle_probe_floor_seconds <= 10.0


def test_peer_liveness_overview_names_state_and_last_seen(monkeypatch):
    clock = {"t": 6_000}
    session = Session(fail=True)
    route, detector, subject = make_route(monkeypatch, session, clock)
    seen = route.peer_liveness_overview()
    assert seen["node-2"]["label"] == "fresh" and seen["node-2"]["consecutive_misses"] == 0
    tick(route, clock, 6_000)
    assert route.peer_liveness_overview()["node-2"]["label"] == "stale"
    for t in (11_000, 16_000):
        tick(route, clock, t)
    quarantined = route.peer_liveness_overview()["node-2"]
    assert quarantined["label"] == "quarantined" and quarantined["state"] == "quarantined"
    assert quarantined["consecutive_misses"] == 3 and quarantined["last_seen_seconds_ago"] == pytest.approx(15.0)
    session.fail = False
    for t in (21_000, 26_000):
        tick(route, clock, t)
    assert route.peer_liveness_overview()["node-2"]["label"] == "fresh"   # recovered


def test_only_peer_subjects_are_listed(monkeypatch):
    clock = {"t": 6_000}
    route, detector, _subject = make_route(monkeypatch, Session(), clock)
    detector.register_subject(LivenessSubject("node-1->node-2", SubjectKind.EDGE, 1), observed_at_ms=1_000)
    assert list(route.peer_liveness_overview()) == ["node-2"]


def test_the_open_route_registry_feeds_the_module_level_overview(monkeypatch):
    clock = {"t": 6_000}
    route, _detector, _subject = make_route(monkeypatch, Session(), clock)
    assert route_module.open_route_peer_liveness() is None
    route_module._register_open_route(route)
    try:
        assert list(route_module.open_route_peer_liveness()) == ["node-2"]
    finally:
        route_module._unregister_open_route(route)
    assert route_module.open_route_peer_liveness() is None


def test_api_status_shows_per_peer_liveness(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    app = DemoApp(tmp_path / "app", check_platform=False).start()
    try:
        assert app.status()["peer_liveness"] is None            # no route open
        monkeypatch.setattr(route_module, "open_route_peer_liveness",
                            lambda: {"node-2": {"label": "quarantined", "state": "quarantined"}})
        assert app.status()["peer_liveness"]["node-2"]["label"] == "quarantined"
    finally:
        app.close()
