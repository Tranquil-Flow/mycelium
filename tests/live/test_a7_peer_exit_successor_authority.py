# SPDX-License-Identifier: AGPL-3.0-only
"""Verification of the A7 peer-exit successor authority hook in route.py.

These tests drive the real ``PhysicalLiveRoute`` methods added by the
owner-approved A7 route change (``set_peer_exit_successor_authority``,
``_bound_participants``, ``_bind_covered_peer_exit`` and the claim step in
``_handle_lost_peer_processes``) on a minimal route fixture.  They check:

* the authority's blocking RPC to the entry never runs under the route lock,
  from either detection caller (status read or the active liveness monitor);
* with no authority installed, the lost-peer and participant paths behave
  exactly as before, and the monitor settles a partial participant loss in the
  same tick (A5: a lost remote pipeline stage), not at the next status read;
* every refusal shape keeps today's interruption;
* concurrent detection callers cannot cancel a request that one of them
  handed to its successor (the serialised peer-exit claim).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

import pytest

from mycelium_live.liveness import (
    LivenessSubject,
    SubjectKind,
    TrafficAwareLivenessDetector,
)
from mycelium_live.route import PhysicalLiveRoute

_CONTROL_FIELDS = {
    "deployment_id": "deployment-a7",
    "deployment_epoch": 1,
    "qualification_digest": "sha256:" + "a" * 64,
    "request_attempt": 1,
    "path_id": "path-a7",
    "path_attempt": 1,
    "path_digest": "sha256:" + "b" * 64,
    "topology_generation": 1,
    "command_id": "command-a7",
    "cancellation_generation": 0,
    "publisher_generation": 0,
    "absolute_deadline_ms": 10**15,
}


class _Session:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode


def _route(
    *,
    lost_returncode: int | None = -9,
    authority: Callable[..., bool] | None = None,
) -> tuple[PhysicalLiveRoute, dict[str, list[Any]]]:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._open = True
    route._closed = False
    route._sessions = {
        "node-0": _Session(None),
        "node-2": _Session(lost_returncode),
    }
    route._active_route_requests = {
        "request-a7": {
            **_CONTROL_FIELDS,
            "entry_node_id": "node-0",
            "participating_node_ids": frozenset({"node-0", "node-2"}),
            "terminal": False,
            "cancellation_requested": False,
            "control_bound": True,
        },
        "request-other": {
            **_CONTROL_FIELDS,
            "entry_node_id": "node-0",
            "participating_node_ids": frozenset({"node-0"}),
            "terminal": False,
            "cancellation_requested": False,
            "control_bound": True,
        },
    }
    route._liveness = TrafficAwareLivenessDetector()
    subject = LivenessSubject("node-2", SubjectKind.PEER, 9)
    route._liveness.register_subject(subject, observed_at_ms=1)
    route._liveness_subjects = {"node-2": subject}
    route._last_health = {}
    route._last_snapshots = {}
    log: dict[str, list[Any]] = {
        "cancelled": [],
        "incidents": [],
        "scoped": [],
        "sent": [],
        "lock_owned_during_send": [],
    }
    route.cancel_request = lambda request_id, *, deadline_monotonic_s=None: (
        log["cancelled"].append(request_id) or True
    )
    route._record_incident = lambda *, state, reason, request_id: log[
        "incidents"
    ].append((state, reason, request_id))

    def record_scoped(**kwargs: Any) -> None:
        log["scoped"].append(kwargs)

    route._record_scoped_runtime_incident = record_scoped
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"

    def send_before(node_id: str, **kwargs: Any) -> dict[str, Any]:
        # The blocking command to the entry must never hold the route lock.
        log["lock_owned_during_send"].append(route._lock._is_owned())
        log["sent"].append((node_id, kwargs["command"], kwargs["payload"]))
        if kwargs["command"] == "health":
            if route._sessions[node_id].returncode is not None:
                raise RuntimeError("node_process_exited")
            return {"node_id": node_id, "details": {}}
        return {"node_id": node_id, "accepted": True}

    route._send_before = send_before
    route._verify_observation = lambda node_id, response, *, event, **_k: dict(response)
    if authority is not None:
        route.set_peer_exit_successor_authority(authority)
    return route, log


def _accepting_authority(calls: list[Mapping[str, Any]]) -> Callable[..., bool]:
    def authority(
        *,
        request_id: str,
        lost_node_id: str,
        control: Mapping[str, Any],
        send_to_entry: Callable[..., Mapping[str, Any]],
    ) -> bool:
        calls.append(
            {"request_id": request_id, "lost_node_id": lost_node_id, **control}
        )
        observation = send_to_entry(
            "a7_source_lost",
            {"request_id": request_id, "control": dict(control)},
            time.monotonic() + 1.0,
        )
        return observation.get("accepted") is True

    return authority


def test_authority_runs_outside_the_route_lock_and_route_stays_usable() -> None:
    other_thread_acquired: list[bool] = []
    calls: list[Mapping[str, Any]] = []
    inner = _accepting_authority(calls)

    def authority(**kwargs: Any) -> bool:
        assert not route._lock._is_owned()

        def probe() -> None:
            acquired = route._lock.acquire(timeout=0.5)
            other_thread_acquired.append(acquired)
            if acquired:
                route._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(2.0)
        return inner(**kwargs)

    route, log = _route()
    route.set_peer_exit_successor_authority(authority)

    worker = threading.Thread(target=route._handle_lost_peer_processes)
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive(), "lost-peer handling deadlocked"
    assert other_thread_acquired == [True]
    assert log["lock_owned_during_send"] == [False]
    assert [call["request_id"] for call in calls] == ["request-a7"]
    assert calls[0]["lost_node_id"] == "node-2"
    assert log["sent"] == [
        (
            "node-0",
            "a7_source_lost",
            {
                "request_id": "request-a7",
                "control": {field: calls[0][field] for field in _CONTROL_FIELDS},
            },
        )
    ]
    assert log["cancelled"] == []
    assert route._bound_participants("request-a7", frozenset()) == frozenset({"node-0"})
    assert [item["subject_id"] for item in log["scoped"]] == ["node-2"]
    assert log["scoped"][0]["reason"] == "node_process_exited"
    assert log["incidents"] == [("peer_process_lost", "route_peer_process_lost", None)]
    # A4 still owns the observed failure for the covered track.
    incident = route._liveness.incidents()[0]
    assert incident.scope == "peer"
    assert incident.affected_track_ids == ("request-a7",)


def test_monitor_tick_hands_the_exit_to_the_successor_without_the_route_lock() -> None:
    calls: list[Mapping[str, Any]] = []
    route, log = _route(authority=_accepting_authority(calls))

    worker = threading.Thread(target=route._monitor_active_liveness_once)
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive(), "monitor deadlocked"
    assert log["lock_owned_during_send"] == [False, False, False]
    assert [call["request_id"] for call in calls] == ["request-a7"]
    assert log["cancelled"] == []
    assert route._active_route_requests["request-a7"][
        "participating_node_ids"
    ] == frozenset({"node-0"})


def test_lost_peer_handling_is_reentrant_for_a_caller_holding_the_route_lock() -> None:
    # RLock: a same-thread caller holding the lock cannot deadlock the claim.
    route, log = _route(authority=_accepting_authority([]))
    finished = threading.Event()

    def held() -> None:
        with route._lock:
            route._handle_lost_peer_processes()
            finished.set()

    thread = threading.Thread(target=held)
    thread.start()
    thread.join(5.0)
    assert finished.is_set()
    # No route.py caller holds the lock (see the two tests above); under this
    # artificial caller the RPC would run under it, so callers must not.
    assert log["lock_owned_during_send"] == [True]


def test_no_authority_keeps_todays_cancel_and_participants() -> None:
    route, log = _route()
    route._handle_lost_peer_processes()

    assert log["cancelled"] == ["request-a7"]
    assert log["sent"] == []
    assert log["scoped"] == []
    registered = route._active_route_requests["request-a7"]["participating_node_ids"]
    assert route._bound_participants("request-a7", frozenset()) == registered
    assert route._bound_participants("request-a7", frozenset()) == frozenset(
        {"node-0", "node-2"}
    )
    other = route._active_route_requests["request-other"]["participating_node_ids"]
    assert route._bound_participants("request-other", frozenset({"x"})) == other
    fallback = frozenset({"node-0", "node-2"})
    assert route._bound_participants("gone", fallback) is fallback


def test_monitor_without_authority_reaches_the_handler_on_a_partial_failure() -> None:
    route, log = _route()
    handled: list[bool] = []
    route._handle_lost_peer_processes = lambda: handled.append(True)

    route._monitor_active_liveness_once()

    # One of two health probes failed: a lost pipeline stage must be settled in
    # this tick (A5), whether or not an A7 authority is installed.
    assert handled == [True]
    assert log["cancelled"] == []


def test_monitor_without_authority_interrupts_a_lost_remote_stage_in_the_tick() -> None:
    route, log = _route()

    route._monitor_active_liveness_once()

    assert log["cancelled"] == ["request-a7"]
    assert route._active_route_requests["request-a7"]["lost_peer_node_ids"] == frozenset(
        {"node-2"}
    )


def test_monitor_with_authority_reaches_the_handler_on_a_partial_failure() -> None:
    route, _log = _route(authority=_accepting_authority([]))
    handled: list[bool] = []
    route._handle_lost_peer_processes = lambda: handled.append(True)

    route._monitor_active_liveness_once()

    assert handled == [True]


@pytest.mark.parametrize(
    "outcome",
    ("refused", "raised", "non_bool_truthy"),
)
def test_authority_refusal_keeps_todays_cancel(outcome: str) -> None:
    def authority(**_kwargs: Any) -> Any:
        if outcome == "raised":
            raise RuntimeError("entry_unreachable")
        if outcome == "non_bool_truthy":
            return 1
        return False

    route, log = _route(authority=authority)
    route._handle_lost_peer_processes()

    assert log["cancelled"] == ["request-a7"]
    assert log["scoped"] == []
    assert route._bound_participants("request-a7", frozenset()) == frozenset(
        {"node-0", "node-2"}
    )


def test_dead_entry_is_never_claimed() -> None:
    calls: list[Mapping[str, Any]] = []
    route, log = _route(authority=_accepting_authority(calls))
    route._sessions["node-0"].returncode = -9
    route._sessions["node-2"].returncode = None

    route._handle_lost_peer_processes()

    assert calls == []
    assert sorted(log["cancelled"]) == ["request-a7", "request-other"]


def test_unbound_control_is_never_claimed() -> None:
    calls: list[Mapping[str, Any]] = []
    route, log = _route(authority=_accepting_authority(calls))
    route._active_route_requests["request-a7"]["control_bound"] = False

    route._handle_lost_peer_processes()

    assert calls == []
    assert log["cancelled"] == ["request-a7"]


def test_identity_change_during_the_claim_refuses_the_rebind() -> None:
    def authority(**_kwargs: Any) -> bool:
        with route._lock:
            route._active_route_requests["request-a7"]["publisher_generation"] = 1
        return True

    route, log = _route(authority=authority)
    route._handle_lost_peer_processes()

    assert log["cancelled"] == ["request-a7"]
    assert log["scoped"] == []
    assert route._bound_participants("request-a7", frozenset()) == frozenset(
        {"node-0", "node-2"}
    )


def test_covered_exit_is_not_reclaimed_by_a_later_status_read() -> None:
    calls: list[Mapping[str, Any]] = []
    route, log = _route(authority=_accepting_authority(calls))

    route._handle_lost_peer_processes()
    route._handle_lost_peer_processes()

    assert len(calls) == 1
    assert log["cancelled"] == []


def test_concurrent_detection_callers_never_cancel_a_covered_request() -> None:
    first_in_authority = threading.Event()
    second_in_authority = threading.Event()
    first_done = threading.Event()
    calls: list[str] = []

    def authority(**_kwargs: Any) -> bool:
        if not first_in_authority.is_set():
            calls.append("first")
            first_in_authority.set()
            # Unserialised callers reach the authority concurrently; a
            # serialised claim makes the second caller wait instead.
            second_in_authority.wait(1.0)
            return True
        calls.append("second")
        second_in_authority.set()
        # The second caller's entry answer arrives after the first caller
        # has already rebound the request (same accepted ownership).
        assert first_done.wait(2.0)
        return True

    route, log = _route(authority=authority)

    def first() -> None:
        route._handle_lost_peer_processes()
        first_done.set()

    a = threading.Thread(target=first)
    a.start()
    assert first_in_authority.wait(2.0)
    b = threading.Thread(target=route._handle_lost_peer_processes)
    b.start()
    a.join(5.0)
    b.join(5.0)

    assert not a.is_alive() and not b.is_alive()
    assert calls[0] == "first"
    assert route._bound_participants("request-a7", frozenset()) == frozenset({"node-0"})
    assert log["cancelled"] == []
