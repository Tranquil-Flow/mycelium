# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: a reloading member is *warming* to the owner route - bounded wait, member_warming, never lost."""
from __future__ import annotations

import threading
import time

import pytest

from mycelium_idle.residency import MemberWarming
from mycelium_live.liveness import (
    LivenessState, LivenessSubject, ObservationSource, SubjectKind, TrafficAwareLivenessDetector,
)
from mycelium_live.route import FakeLiveRoute, PhysicalLiveRoute
from mycelium_live.router_port import LiveRouterPort

NODE = "node-2"


class WarmSession:
    """A member whose weights were unloaded: idle_reload starts a reload that needs N health polls."""

    def __init__(self, state="idle", polls_to_load=2, dead=False):
        self.state = state
        self.polls_to_load = polls_to_load
        self.polls = 0
        self.dead = dead
        self.commands = []

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        self.commands.append(command)
        if self.dead:
            raise TimeoutError("command_deadline_exceeded")
        if command == "idle_reload" and self.state == "idle":
            self.state = "warming"
        if command == "health" and self.state == "warming":
            self.polls += 1
            if self.polls >= self.polls_to_load:
                self.state = "loaded"
        return {"details": {"idle_residency": {"state": self.state}}}


def make_route(session, *, cached_state="idle", deadline_s=0.5):
    detector = TrafficAwareLivenessDetector()
    subject = LivenessSubject(NODE, SubjectKind.PEER, 4)
    assert detector.register_subject(subject, observed_at_ms=1_000).accepted
    clock = {"t": 6_000}
    route = PhysicalLiveRoute.__new__(PhysicalLiveRoute)
    route._liveness = detector
    route._liveness_subjects = {NODE: subject}
    route._open = True
    route._closed = False
    route._lock = threading.RLock()
    route._sessions = {NODE: session}
    route._active_route_requests = {}
    route._last_health = {NODE: {"details": {"idle_residency": {"state": cached_state}}}}
    route._replica_loss_placement_ids = frozenset()
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"
    route._monotonic_ms = lambda: clock["t"]
    route._idle_warm_deadline_s = deadline_s
    route._idle_warm_poll_s = 0.01

    def verify(node_id, response, *, event, require_known_key=True):
        detector.observe_receipt(subject, observed_at_ms=clock["t"],
                                 source=ObservationSource.APPLICATION_RECEIPT, signed=True)
        return dict(response)

    route._verify_observation = verify
    return route, detector, subject, clock


def test_a_loaded_cluster_sends_no_extra_commands():
    session = WarmSession(state="loaded")
    route, *_ = make_route(session, cached_state="loaded")
    route._warm_idle_peers(frozenset({NODE}))
    assert session.commands == []


def test_a_peer_with_no_idle_detail_is_left_alone():
    session = WarmSession()
    route, *_ = make_route(session)
    route._last_health = {NODE: {"details": {"transport": {}}}}
    route._warm_idle_peers(frozenset({NODE}))
    assert session.commands == []


def test_an_idle_peer_is_woken_and_waited_for_before_admission():
    session = WarmSession(state="idle", polls_to_load=3)
    route, detector, subject, _ = make_route(session)
    route._warm_idle_peers(frozenset({NODE}))
    assert session.commands[0] == "idle_reload" and set(session.commands[1:]) == {"health"}
    assert session.state == "loaded"
    snapshot = detector.subject_snapshot(subject)
    assert snapshot.state is LivenessState.FRESH and snapshot.consecutive_misses == 0


def test_a_slow_reload_raises_member_warming_and_is_not_lost():
    session = WarmSession(state="idle", polls_to_load=10**9)
    route, detector, subject, clock = make_route(session, deadline_s=0.1)
    with pytest.raises(MemberWarming) as error:
        route._warm_idle_peers(frozenset({NODE}))
    assert error.value.code == "member_warming" and error.value.node_ids == (NODE,)
    snapshot = detector.subject_snapshot(subject)
    assert snapshot.state is LivenessState.FRESH and snapshot.consecutive_misses == 0
    assert detector.incidents() == ()
    assert route._replica_loss_placement_ids == frozenset()  # not an A5 replica loss
    assert session.state == "warming"


def test_a_member_that_stops_answering_while_warming_is_not_hidden_behind_member_warming():
    session = WarmSession(state="idle", dead=True)
    route, *_ = make_route(session)
    with pytest.raises(TimeoutError):
        route._warm_idle_peers(frozenset({NODE}))


def test_a_member_that_never_comes_back_still_reaches_the_existing_quarantine_path():
    """Warming adds no immunity: unanswered keepalive probes accrue misses exactly as before."""
    session = WarmSession(state="idle", dead=True)
    route, detector, subject, clock = make_route(session)
    for t in (6_000, 11_000, 16_000):
        clock["t"] = t
        route._monitor_idle_keepalives_once()
    assert detector.subject_snapshot(subject).state is LivenessState.QUARANTINED
    from mycelium_live.route import AffectedPeerQuarantined
    with pytest.raises(AffectedPeerQuarantined):
        route._reject_if_affected_peer_quarantined(frozenset({NODE}))


def test_a_warming_member_that_answers_probes_stays_fresh():
    session = WarmSession(state="warming", polls_to_load=10**9)
    route, detector, subject, clock = make_route(session, cached_state="warming")
    for t in (6_000, 11_000, 16_000, 21_000):
        clock["t"] = t
        route._monitor_idle_keepalives_once()
    snapshot = detector.subject_snapshot(subject)
    assert snapshot.state is LivenessState.FRESH and snapshot.consecutive_misses == 0


def test_member_warming_refusal_publishes_a_bounded_failed_terminal_with_its_code(
        live_graph, request_factory, monkeypatch):
    class WarmingRoute(FakeLiveRoute):
        def infer(self, token_ids, *, max_new_tokens, request_id, sink, cancel_requested=None, **_kwargs):
            raise MemberWarming((NODE,))

    route = WarmingRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    # refused before any node command: no command-ledger terminal is owed (same as a quarantine refusal)
    monkeypatch.setattr(port, "_record_command_terminal",
                        lambda *a, **k: pytest.fail("admission refusal must not CAS a command terminal"))

    class Sink:
        def emit(self, _index, _token):
            return None

    admitted = port.admit(request_factory("warming-refusal"), Sink())
    deadline = time.monotonic() + 3
    while port.request_status(admitted) != "FAILED" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert port.request_status(admitted) == "FAILED"
    assert port.request_error_code(admitted) == "member_warming"
    port.release_request(admitted)
    port.close()
