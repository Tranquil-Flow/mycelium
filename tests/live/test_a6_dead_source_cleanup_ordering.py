# SPDX-License-Identifier: AGPL-3.0-only
"""A killed source's cleanup proof must not be decided before its facts exist.

Same production composition and real local node processes as
``test_a6_local_physical_recovery`` (run-day single-path topology: the source
``node-a`` carries stage-000 and is the request's entry, the survivor
``node-b`` carries stage-001). The product seam SIGKILLs the source after the
first token. The router may seal the source command's cleanup from the source's
observed process exit only when (1) the route recorded that request-scoped
``node_process_exited`` incident and (2) every surviving participant proved its
own exact cleanup. Each test forces one interleaving in which one of those two
facts arrives after the route's first look, without sleeping:

* the survivor's cleanup receipt arrives after the dead source's control channel
  has already failed (a busy survivor answers the cancellation later than the
  route's inline-receipt grace); the route must keep waiting for the survivor
  inside the owner's cleanup deadline instead of giving up on the first
  unanswerable snapshot of a dead peer;
* a status read (``public_status``: operator, Observatory, owner) observes the
  exited process and linearizes the request's cancellation before the source's
  response reader delivers EOF; the in-flight command is then interrupted as
  ``request_cancelled``, and the route must still record the participant's
  process exit as the request's runtime failure, exactly as when the reader
  wins.

Before the fix both end ``failed/request_cleanup_unproven`` and the A6 seam
refuses ``a6_fault_source_failure_unobserved``: the rare rehearsal-#2 failure of
``test_single_path_source_kill_is_observed_then_route_cold_restarts``.

Local multi-process session only; never physical or browser qualification.
"""
from __future__ import annotations

import signal
import threading
import time

import pytest

from mycelium_live import route as route_module
from mycelium_live.router_port import LiveRouterPort
from mycelium_node import process as process_module
from mycelium_request_gateway.backend import RouterSessionBackend
from tests.live.test_a6_local_physical_recovery import (
    _compose,
    _run_case,
    _run_without_fault,
    _wait_ready,
    watchdog,  # noqa: F401 - fixture
)
from tests.physical_qualification.test_node_service import SIDECAR_BINARY

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

_CASE = "ordinary_browser_source_failure_recovery"
_HOLD_BOUND_S = 6.0


def _record_failures(monkeypatch) -> list:
    """Collect every failure the router and the backend raise while a test runs.

    The router retains a request's bounded error code only until the gateway
    releases the request, which is before the assertion below can ask for it. A
    recorder installed up front keeps what actually ended a request: the router
    command's exception (with its cause) and any exception out of the backend run.
    """

    failures: list = []
    original_code = LiveRouterPort._bounded_error_code

    def bounded_error_code(error):
        failures.append({"router_command": repr(error), "cause": repr(error.__cause__)})
        return original_code(error)

    original_run = RouterSessionBackend.run

    def run(self, request_id, submission, emit_token, is_cancelled):
        try:
            return original_run(self, request_id, submission, emit_token, is_cancelled)
        except BaseException as error:
            failures.append({"backend_run": repr(error), "request_id": request_id})
            raise

    monkeypatch.setattr(LiveRouterPort, "_bounded_error_code", staticmethod(bounded_error_code))
    monkeypatch.setattr(RouterSessionBackend, "run", run)
    return failures


def _router_state_of(router, owner, request_id, failures) -> dict:
    """What the router and the owner say about a request that did not complete.

    Evaluated only when the assertion above fails: a failed second request names
    its router status, recovery reason, the failures recorded while the test ran
    and the owner's restart state instead of only the gateway's generic
    ``backend_failed``.
    """

    state: dict = {"recorded_failures": list(failures)}
    for name, read in (
        ("request_status", lambda: router.request_status(request_id)),
        ("request_error_code", lambda: router.request_error_code(request_id)),
        ("recovery_failure_reason", lambda: router.recovery_failure_reason(request_id)),
        ("scoped_incidents", lambda: owner._current.a4_scoped_runtime_incidents()),
        ("owner_status", owner.public_status),
    ):
        try:
            state[name] = read()
        except Exception as error:  # noqa: BLE001 - diagnostic only
            state[name] = f"unreadable: {error!r}"
    return state


def _assert_source_failure_recovered_then_cold_restarted(
    local, owner, router, service, request_id, outcome, first_route, failures,
) -> None:
    assert "refusal" not in outcome, outcome
    assert outcome["post_state"]["source_failure_observed"] is True
    assert outcome["post_state"]["actual_incident"]["subject_id"] == "node-a"
    assert outcome["kinds"] == ["failed"], outcome
    assert ("failed", "recovery_successor_unavailable") in outcome["events"], outcome
    assert router.recovery_failure_reason(request_id) == "recovery_successor_unavailable"
    status = _wait_ready(owner, 120)
    assert status["state"] == "ready" and status["generation"] == 2, status
    assert owner._current is not first_route
    second_id, second = _run_without_fault(service, owner)
    assert second["kinds"] == ["completed"], (
        second, _router_state_of(router, owner, second_id, failures))
    assert local.session_factory.launches == ["node-a", "node-b", "node-a", "node-b"]


def test_survivor_cleanup_proven_after_the_dead_source_channel_failed(
        tmp_path, watchdog, monkeypatch) -> None:  # noqa: F811
    """The survivor's receipt arrives after the dead source's snapshot failed.

    The survivor's cancellation (``infer_cancel_wait``) is held until the
    request's own cleanup wait has taken one snapshot round of the survivor
    while its cancellation was still undelivered (so its receipt is provably
    incomplete), and then either asked again (it kept waiting) or the router
    already attempted the source command's FAILED terminal (it gave up).
    """

    failures = _record_failures(monkeypatch)
    local, owner, router, service, target = _compose(tmp_path, "single")
    lock = threading.Lock()
    release = threading.Event()
    state: dict = {"armed": False, "held": False, "worker": None, "rounds": 0,
                   "released_by": None, "source_exited_at_hold": None,
                   "survivor_proven_at_terminal": None}

    def release_once(cause: str) -> None:
        with lock:
            if state["released_by"] is None:
                state["released_by"] = cause
        release.set()

    original_send_before = route_module._ConcurrentNodeSession.send_before

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        hold = False
        with lock:
            if (state["armed"] and not state["held"] and self.node_id == "node-b"
                    and command == "infer_cancel_wait"):
                state["held"] = True
                hold = True
                source = local.session_factory.sessions["node-a"]
                state["source_exited_at_hold"] = source.returncode is not None
        if hold:
            release.wait(min(_HOLD_BOUND_S, max(0.0, deadline_monotonic_s - time.monotonic() - 1.0)))
            release_once("hold_bound")
        return original_send_before(
            self, command_id=command_id, command=command, payload=payload,
            deadline_monotonic_s=deadline_monotonic_s)

    original_authorize = route_module.PhysicalLiveRoute._authorize_failed_request_cleanup

    def authorize(self, request_id, **kwargs):
        with lock:
            if state["armed"] and state["worker"] is None:
                state["worker"] = threading.get_ident()
        return original_authorize(self, request_id, **kwargs)

    original_snapshot = route_module.PhysicalLiveRoute._snapshot_nodes_before

    def snapshot_nodes_before(self, node_ids, **kwargs):
        counted = False
        with lock:
            counted = (state["held"] and not release.is_set()
                       and threading.get_ident() == state["worker"] and "node-b" in node_ids)
        try:
            return original_snapshot(self, node_ids, **kwargs)
        finally:
            if counted:
                with lock:
                    state["rounds"] += 1
                    rounds = state["rounds"]
                if rounds >= 2:
                    release_once("survivor_asked_again")

    original_terminal = LiveRouterPort._record_command_terminal

    def record_terminal(self, pending, terminal, **kwargs):
        try:
            if terminal == "FAILED" and state["held"] and state["survivor_proven_at_terminal"] is None:
                try:
                    route = owner._current
                    request_id = pending.request.request_id
                    state["survivor_proven_at_terminal"] = route._cancellation_cleanup_complete(
                        frozenset({"node-b"}),
                        cleanup_subject=route._request_cleanup_subject(request_id))
                except Exception as error:  # noqa: BLE001 - asserted below
                    state["survivor_proven_at_terminal"] = repr(error)
            return original_terminal(self, pending, terminal, **kwargs)
        finally:
            if terminal == "FAILED" and state["rounds"] >= 1:
                release_once("router_terminal_attempted")

    monkeypatch.setattr(route_module._ConcurrentNodeSession, "send_before", send_before)
    monkeypatch.setattr(
        route_module.PhysicalLiveRoute, "_authorize_failed_request_cleanup", authorize)
    monkeypatch.setattr(
        route_module.PhysicalLiveRoute, "_snapshot_nodes_before", snapshot_nodes_before)
    monkeypatch.setattr(LiveRouterPort, "_record_command_terminal", record_terminal)
    try:
        first_route = owner._current
        with lock:
            state["armed"] = True
        request_id, outcome = _run_case(service, owner, target, _CASE)
        # The forced interleaving really happened: the survivor's cancellation
        # was held while the source process had already exited, and the
        # request's cleanup wait snapshotted the incomplete survivor first.
        assert state["held"] is True, state
        assert state["source_exited_at_hold"] is True, state
        assert state["rounds"] >= 1, state
        assert "refusal" not in outcome, (outcome, state)
        assert state["released_by"] == "survivor_asked_again", (state, outcome)
        assert state["survivor_proven_at_terminal"] is True, state
        _assert_source_failure_recovered_then_cold_restarted(
            local, owner, router, service, request_id, outcome, first_route, failures)
    finally:
        release.set()
        service.close()
        owner.close()
        local.close()


def test_status_read_cancelling_before_the_reader_sees_eof_still_records_the_exit(
        tmp_path, watchdog, monkeypatch) -> None:  # noqa: F811
    """A status read linearizes cancellation before the source's reader EOF.

    The source's stdout reader is held at EOF until an ordinary
    ``owner.public_status()`` read has run to completion; that read finds the
    exited process and cancels the request, so the in-flight decode command is
    interrupted as ``request_cancelled`` instead of failing with
    ``node_process_exited``.
    """

    failures = _record_failures(monkeypatch)
    local, owner, router, service, target = _compose(tmp_path, "single")
    state = _hold_source_eof_for_a_status_read(monkeypatch, owner)
    try:
        first_route = owner._current
        state["armed"] = True
        request_id, outcome = _run_case(service, owner, target, _CASE)
        # The forced interleaving really happened: the status read cancelled
        # the request while the source's EOF was still undelivered.
        assert state["eof_held"] is True, state
        assert state["source_exit_status"] == -signal.SIGKILL, state
        assert state["observer_error"] is None, state
        assert state["lost_peer_cancelled"] is True, state
        assert "refusal" not in outcome, (outcome, state)
        _assert_source_failure_recovered_then_cold_restarted(
            local, owner, router, service, request_id, outcome, first_route, failures)
    finally:
        service.close()
        owner.close()
        local.close()


def test_status_read_ordering_still_recovers_on_the_disjoint_successor(
        tmp_path, watchdog, monkeypatch) -> None:  # noqa: F811
    """The run-day A6 case 1 shape: whole model on node-a and again on node-b.

    The source's only participant is the killed node, so there is no survivor
    to prove; the status-read ordering above must still yield the source's
    ``node_process_exited`` incident, and the request continues on the
    successor with the uninterrupted token sequence.
    """

    local, owner, router, service, target = _compose(tmp_path, "full_replica")
    state = _hold_source_eof_for_a_status_read(monkeypatch, owner)
    try:
        _baseline_id, baseline = _run_without_fault(service, owner)
        assert baseline["kinds"] == ["completed"], baseline
        state["armed"] = True
        request_id, outcome = _run_case(service, owner, target, _CASE)
        assert state["eof_held"] is True, state
        assert state["source_exit_status"] == -signal.SIGKILL, state
        assert state["observer_error"] is None, state
        assert state["lost_peer_cancelled"] is True, state
        assert "refusal" not in outcome, (outcome, state)
        assert outcome["post_state"]["source_failure_observed"] is True
        assert outcome["post_state"]["recovery_required"] is True
        assert outcome["post_state"]["actual_incident"]["subject_id"] == "node-a"
        assert outcome["kinds"] == ["completed"], outcome
        assert outcome["tokens"] == baseline["tokens"]
        assert router.recovery_failure_reason(request_id) is None
        status = _wait_ready(owner, 120)
        assert status["state"] == "ready" and status["generation"] == 2, status
        _after_id, after = _run_without_fault(service, owner)
        assert after["kinds"] == ["completed"], after
    finally:
        service.close()
        owner.close()
        local.close()


def _hold_source_eof_for_a_status_read(monkeypatch, owner) -> dict:
    """Hold node-a's reader EOF (once, when armed) until a status read finished."""

    lock = threading.Lock()
    state: dict = {"armed": False, "eof_held": False, "eof_released": False,
                   "observer": None, "lost_peer_cancelled": False,
                   "cancelled_by": None, "observer_error": None,
                   "source_exit_status": None}

    original_cancel = route_module.PhysicalLiveRoute.cancel_request

    def cancel_request(self, request_id, **kwargs):
        accepted = original_cancel(self, request_id, **kwargs)
        # The ordering under test is "the route cancels while the source's EOF is
        # still undelivered". Either the status read or the product's own liveness
        # monitor (50 ms) may be the one that finds the exited process first; both
        # take the same lost-peer path, so either counts.
        with lock:
            in_hold_window = state["eof_held"] and not state["eof_released"]
        if accepted and in_hold_window and not state["lost_peer_cancelled"]:
            state["lost_peer_cancelled"] = True
            state["cancelled_by"] = ("status_read" if threading.get_ident() == state["observer"]
                                     else threading.current_thread().name)
        return accepted

    def observe() -> None:
        state["observer"] = threading.get_ident()
        try:
            owner.public_status()
        except BaseException as error:  # noqa: BLE001 - asserted by the caller
            state["observer_error"] = repr(error)

    original_fail_waiters = process_module.PhysicalNodeProcess._fail_waiters

    def fail_waiters(self, item):
        hold = False
        with lock:
            if (state["armed"] and not state["eof_held"] and item is process_module._EOF
                    and self.node_id == "node-a"):
                state["eof_held"] = True
                hold = True
        if hold:
            # The pipe closes while the killed process exits; its exit status
            # can become observable a moment later. Let the status read run
            # only once the exit is observable, as it is when it wins for real.
            try:
                state["source_exit_status"] = self._process.wait(timeout=10)
            except Exception as error:  # noqa: BLE001 - asserted by the caller
                state["source_exit_status"] = repr(error)
            observer = threading.Thread(target=observe, name="a6-status-observer", daemon=True)
            observer.start()
            observer.join(30)
            with lock:
                state["eof_released"] = True
        return original_fail_waiters(self, item)

    monkeypatch.setattr(route_module.PhysicalLiveRoute, "cancel_request", cancel_request)
    monkeypatch.setattr(process_module.PhysicalNodeProcess, "_fail_waiters", fail_waiters)
    return state
