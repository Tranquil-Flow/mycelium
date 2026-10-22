# SPDX-License-Identifier: AGPL-3.0-only
"""PV34: the active-liveness monitor must not flood a polled member with health commands.

Measured on the two-Mac demo (2026-10-21): while a request was active the route sent one ``health`` managed-control
command per node about every 0.56 s.  The member claims commands one at a time over HTTPS, so every health queued
ahead of the next ``infer_decode`` and cost it ~0.75 s of claim time (claim wait ~0.35 s + 0.75 s x backlog; 72 of
173 probes expired unclaimed).  Per-token time was ~3.7 s for ~0.1 s of compute.

Contract pinned here:

* a node that acknowledged progress (a completed infer_start / infer_decode, which proves every stage of the pipeline
  computed) inside the progress window is live: no health is issued to it;
* never a second health to a node while the first is outstanding, and a polled member whose health missed is not
  probed again before its declared wire life (``idle_probe_floor_seconds``) has passed;
* loss detection is unchanged: an exited process is settled in the same tick, a silent node is probed once the
  progress window lapses and is still declared lost inside the liveness active-failure budget (2000 ms).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import pytest

import mycelium_deadlines
from mycelium_live import route as route_module
from mycelium_live.liveness import (
    LivenessSubject,
    SubjectKind,
    TrafficAwareLivenessDetector,
)
from mycelium_live.route import PhysicalLiveRoute


class _LocalSession:
    """A session whose health answers at once (a local subprocess / owner node)."""

    returncode: int | None = None

    def __init__(self, details: dict[str, Any] | None = None) -> None:
        self.details = details or {}
        self.sent: list[str] = []

    def send_before(self, *, command_id: str, command: str, payload: Any, deadline_monotonic_s: float) -> dict:
        self.sent.append(command)
        if self.returncode is not None:
            raise RuntimeError("node_process_exited")
        return {"details": self.details}


def _route(sessions: dict[str, Any], *, participants=None) -> tuple[PhysicalLiveRoute, dict[str, Any]]:
    clock = {"ms": 1_000_000}
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._open = True
    route._closed = False
    route._sessions = sessions
    route._last_health = {}
    route._last_snapshots = {}
    route._peer_exit_successor_authority = None
    route._peer_exit_claim_lock = threading.Lock()
    nodes = frozenset(participants or sessions)
    route._active_route_requests = {
        "request-1": {
            "participating_node_ids": nodes,
            "cancellation_requested": False,
            "terminal": False,
        }
    }
    route._liveness = TrafficAwareLivenessDetector()
    route._liveness_subjects = {}
    for node_id in sessions:
        subject = LivenessSubject(node_id, SubjectKind.PEER, 1)
        route._liveness.register_subject(subject, observed_at_ms=1)
        route._liveness_subjects[node_id] = subject
    log: dict[str, Any] = {"cancelled": [], "scoped": []}
    route._pv34_log = log
    route.cancel_request = lambda request_id, *, deadline_monotonic_s=None: log["cancelled"].append(request_id) or True
    route._record_incident = lambda **_kwargs: None
    route._record_scoped_runtime_incident = lambda **kwargs: log["scoped"].append(kwargs)
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"
    route._send_before = lambda node_id, **kwargs: sessions[node_id].send_before(**kwargs)
    route._verify_observation = lambda node_id, response, *, event, **_k: dict(response)
    route._monotonic_ms = lambda: clock["ms"]
    return route, clock


def _route3(sessions: dict[str, Any]) -> tuple[PhysicalLiveRoute, dict[str, Any], dict[str, Any]]:
    route, clock = _route(sessions)
    return route, clock, route._pv34_log


def _health_count(sessions: dict[str, Any]) -> dict[str, int]:
    return {node_id: session.sent.count("health") for node_id, session in sessions.items()}


def test_progress_window_is_derived_from_the_active_failure_budget() -> None:
    window = route_module._PROGRESS_LIVENESS_WINDOW_MS
    assert 0 < window <= mycelium_deadlines.LIVENESS_ACTIVE_FAILURE_DETECTION_MS
    # window + one health probe + the monitor tick must still fit inside the 2000 ms budget
    assert window + int(route_module._ACTIVE_HEALTH_PROBE_DEADLINE_SECONDS * 1_000) + 100 <= (
        mycelium_deadlines.LIVENESS_ACTIVE_FAILURE_DETECTION_MS
    )


def test_a_node_with_fresh_progress_receipts_gets_no_health_command() -> None:
    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock = _route(sessions)

    for _ in range(40):  # 2 s of 50 ms ticks, a decode receipt every 500 ms
        if _ % 10 == 0:
            route._note_progress_receipts(("entry", "member"))
        route._monitor_active_liveness_once()
        clock["ms"] += 50

    assert _health_count(sessions) == {"entry": 0, "member": 0}


def test_health_resumes_once_the_progress_window_lapses() -> None:
    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock = _route(sessions)
    route._note_progress_receipts(("entry", "member"))

    clock["ms"] += route_module._PROGRESS_LIVENESS_WINDOW_MS - 1
    route._monitor_active_liveness_once()
    assert _health_count(sessions) == {"entry": 0, "member": 0}

    clock["ms"] += 2
    route._monitor_active_liveness_once()
    assert _health_count(sessions) == {"entry": 1, "member": 1}


def test_progress_of_one_node_does_not_vouch_for_another() -> None:
    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock = _route(sessions)
    route._note_progress_receipts(("entry",))

    route._monitor_active_liveness_once()

    assert _health_count(sessions) == {"entry": 0, "member": 1}


def test_a_second_health_is_never_enqueued_while_the_first_is_outstanding() -> None:
    release = threading.Event()
    entered = threading.Event()

    class _Slow(_LocalSession):
        def send_before(self, **kwargs: Any) -> dict:
            self.sent.append(kwargs["command"])
            entered.set()
            release.wait(5.0)
            return {"details": {}}

    sessions = {"entry": _Slow()}
    route, clock = _route(sessions)
    first = threading.Thread(target=route._monitor_active_liveness_once)
    first.start()
    assert entered.wait(5.0)

    route._monitor_active_liveness_once()  # a concurrent tick while the first probe is unanswered
    release.set()
    first.join(5.0)

    assert _health_count(sessions) == {"entry": 1}


def test_a_polled_member_whose_health_missed_is_not_probed_again_inside_its_wire_life() -> None:
    class _Polled(_LocalSession):
        idle_probe_floor_seconds = 4.0

        def send_before(self, **kwargs: Any) -> dict:
            self.sent.append(kwargs["command"])
            raise TimeoutError("command_deadline_exceeded")

    sessions = {"entry": _Polled()}
    route, clock = _route(sessions)

    route._monitor_active_liveness_once()
    for _ in range(60):  # 3 s of 50 ms ticks: the first probe may still be queued at the member
        clock["ms"] += 50
        route._monitor_active_liveness_once()
    assert _health_count(sessions) == {"entry": 1}

    clock["ms"] += 1_100  # past the member's 4.0 s wire life
    route._monitor_active_liveness_once()
    assert _health_count(sessions) == {"entry": 2}


def test_an_exited_process_is_settled_in_the_same_tick_even_with_fresh_progress() -> None:
    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock, log = _route3(sessions)
    route._note_progress_receipts(("entry", "member"))
    sessions["member"].returncode = -9

    route._monitor_active_liveness_once()

    assert route._active_route_requests["request-1"]["lost_peer_node_ids"] == frozenset({"member"})
    assert log["cancelled"] == ["request-1"]


def test_a_silent_node_is_still_declared_lost_inside_the_active_failure_budget() -> None:
    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock = _route(sessions)
    route._note_progress_receipts(("entry", "member"))
    last_progress_ms = clock["ms"]
    # The member then stops making progress and its health reports a dead transport.
    sessions["member"].details = {"transport_fatal_error": {"code": "sidecar_closed"}}

    declared_at_ms = None
    for _ in range(80):  # 4 s of 50 ms ticks
        clock["ms"] += 50
        route._monitor_active_liveness_once()
        incidents = route._liveness.incidents()
        if incidents and declared_at_ms is None:
            declared_at_ms = clock["ms"]
            break

    assert declared_at_ms is not None, "a silent node with a dead transport was never declared lost"
    assert declared_at_ms - last_progress_ms <= mycelium_deadlines.LIVENESS_ACTIVE_FAILURE_DETECTION_MS
    assert route._liveness.incidents()[0].subject.subject_id == "member"
    assert route._liveness.incidents()[0].affected_track_ids == ("request-1",)


def test_progress_does_not_hide_a_node_that_stops_answering() -> None:
    """Silence after progress ends the free pass: health goes out again and its miss reaches the handler."""

    sessions = {"entry": _LocalSession(), "member": _LocalSession()}
    route, clock = _route(sessions)
    route._note_progress_receipts(("entry", "member"))
    handled: list[bool] = []
    route._handle_lost_peer_processes = lambda: handled.append(True)
    sessions["member"].send_before = lambda **_k: (_ for _ in ()).throw(TimeoutError("command_deadline_exceeded"))

    clock["ms"] += route_module._PROGRESS_LIVENESS_WINDOW_MS + 1
    route._monitor_active_liveness_once()

    assert handled == [True]


# ---------------------------------------------------------------------------------------------------------------------
# The measured queueing, reproduced: one-at-a-time claims, 0.7 s per command.
# ---------------------------------------------------------------------------------------------------------------------

_COMMAND_SECONDS = 0.7
_TOKENS = 3


class _FakeMailbox:
    """A polled member: claims ONE command at a time, 0.7 s each; a command past its deadline is dropped unclaimed."""

    idle_probe_floor_seconds = 4.0
    returncode: int | None = None

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue()
        self.claimed: list[tuple[str, float]] = []
        self.dropped: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._member, daemon=True)
        self.thread.start()

    def _member(self) -> None:
        while not self.stop.is_set():
            try:
                command, deadline, done = self.queue.get(timeout=0.02)
            except queue.Empty:
                continue
            if time.monotonic() >= deadline:
                self.dropped.append(command)  # expired unclaimed: costs the member nothing
                continue
            self.claimed.append((command, time.monotonic()))
            time.sleep(_COMMAND_SECONDS)
            done.set()

    def send_before(self, *, command_id: str, command: str, payload: Any, deadline_monotonic_s: float) -> dict:
        done = threading.Event()
        self.queue.put((command, deadline_monotonic_s, done))
        if not done.wait(max(0.0, deadline_monotonic_s - time.monotonic())):
            raise TimeoutError("command_deadline_exceeded")
        return {"details": {}}


def _drive_request(route: PhysicalLiveRoute, mailbox: _FakeMailbox, *, credit_progress: bool) -> list[float]:
    stop = threading.Event()
    if credit_progress:
        route._note_progress_receipts(("entry", "owner"))  # the infer_start receipt that opened the request

    def monitor() -> None:
        while not stop.wait(0.05):
            route._monitor_active_liveness_once()

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    waits: list[float] = []
    try:
        for _ in range(_TOKENS):
            started = time.monotonic()
            mailbox.send_before(
                command_id="decode",
                command="infer_decode",
                payload={},
                deadline_monotonic_s=started + 60.0,
            )
            waits.append(time.monotonic() - started)
            if credit_progress:
                route._note_progress_receipts(("entry", "owner"))
            time.sleep(0.05)  # the owner's turnaround between tokens
    finally:
        stop.set()
        thread.join(5.0)
    return waits


def _storm_route() -> tuple[PhysicalLiveRoute, _FakeMailbox]:
    mailbox = _FakeMailbox()
    sessions: dict[str, Any] = {"entry": mailbox, "owner": _LocalSession()}
    route, _clock = _route(sessions)
    route._monotonic_ms = lambda: int(time.monotonic() * 1_000)
    return route, mailbox


def test_per_token_wait_is_constant_instead_of_backlog_dependent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Today's behaviour: every tick probes every node whatever progress it acknowledged (no progress window, no
    # hold-off for a missed probe).
    with monkeypatch.context() as legacy:
        legacy.setattr(route_module, "_PROGRESS_LIVENESS_WINDOW_MS", 0, raising=False)
        legacy_route, legacy_mailbox = _storm_route()
        legacy_mailbox.idle_probe_floor_seconds = 0.0
        legacy_waits = _drive_request(legacy_route, legacy_mailbox, credit_progress=False)
    legacy_mailbox.stop.set()

    fixed_route, fixed_mailbox = _storm_route()
    fixed_waits = _drive_request(fixed_route, fixed_mailbox, credit_progress=True)
    fixed_mailbox.stop.set()

    legacy_health = [c for c, _ in legacy_mailbox.claimed if c == "health"]
    fixed_health = [c for c, _ in fixed_mailbox.claimed if c == "health"]
    print(f"legacy waits={legacy_waits} health_claimed={len(legacy_health)}")
    print(f"fixed  waits={fixed_waits} health_claimed={len(fixed_health)}")

    # with progress flowing no health is claimed ahead of a decode, so every token costs one command (~0.7 s)
    assert not fixed_health
    assert max(fixed_waits) < 1.0 * _COMMAND_SECONDS + 0.4
    assert max(fixed_waits) - min(fixed_waits) < 0.4
    # the storm queues health ahead of the decode: the legacy wait is backlog-dependent and much larger
    assert legacy_health
    assert max(legacy_waits) > 1.5 * max(fixed_waits)
    assert sum(legacy_waits) > 1.3 * sum(fixed_waits)
