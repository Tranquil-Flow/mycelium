# SPDX-License-Identifier: AGPL-3.0-only
"""A lost remote pipeline stage mid-request is an honest, bounded failed terminal.

A5 hardware (two-stage 7B route: node-0 entry stage, node-2 remote stage):
the harness SIGKILLed the remote stage worker mid-decode.  The request had
streamed four correct tokens, then the gateway SSE went silent for 90 s with
no terminal; only the owner's cancel produced a (cleanup-unproven) terminal
and the serve then crashed on ``router_port_worker_shutdown_timeout``.

This reproduces it on the run-day single-path topology with real local node
processes (stage-000 on ``node-a`` is the entry, stage-001 on ``node-b`` is
the remote stage), through the production composition: ordinary gateway ->
``LiveRouterPort`` -> ``ColdRouteRestartOwner`` -> ``PhysicalLiveRoute``.  The
remote stage's process is SIGKILLed mid-decode; the gateway must publish
``failed`` within the product's own bounds, with no token after the loss and no
completion, and the owner must bring the route back instead of crashing.

Local multi-process session only; never physical or browser qualification.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from mycelium_deadlines import (
    CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    LIVENESS_ACTIVE_FAILURE_DETECTION_MS,
)
from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live._a6_local_physical import build_local_physical
from tests.live.test_a6_local_physical_recovery import (
    _TERMINALS,
    SIDECAR_BINARY,
    _open_and_qualify,
    _stack,
    _run_without_fault,
    _submit,
    _wait_ready,
    no_route_threads_survive,  # noqa: F401 - autouse fixture
    watchdog,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

REMOTE_STAGE = "node-b"
#: Detection window + cancel/cleanup budget, plus scheduling margin.
TERMINAL_BOUND_SECONDS = (
    LIVENESS_ACTIVE_FAILURE_DETECTION_MS / 1000.0
    + CANCEL_AND_CLEANUP_BUDGET_SECONDS
    + 5.0
)


def _compose(tmp_path, step_delay_s=0.15):
    local = build_local_physical(tmp_path, shape="single")
    local.session_factory.step_delay_s = step_delay_s
    owner = ColdRouteRestartOwner(
        route_factory=local.new_route,
        open_and_qualify=_open_and_qualify,
        restart_budget=1,
    )
    try:
        owner.open()
        return (local, owner, *_stack(tmp_path, owner))
    except BaseException:
        owner.close()
        owner.cleanup()
        local.close()
        raise


def _collect(service, request_id, events, first_token, finished):
    subscription = service.subscribe(request_id, last_event_id=None)
    try:
        while True:
            event = subscription.next_event(timeout=TERMINAL_BOUND_SECONDS + 60)
            if event is None:
                return
            events.append((time.monotonic(), event))
            subscription.ack(event.sequence)
            if event.kind == "token":
                first_token.set()
            if event.kind in _TERMINALS:
                return
    finally:
        first_token.set()
        finished.set()
        subscription.close()


def test_lost_remote_stage_mid_decode_is_a_bounded_failed_terminal(
    tmp_path, watchdog
) -> None:
    local, owner, router, service, _target = _compose(tmp_path)
    try:
        first_route = owner._current
        request_id = _submit(service, owner)
        events: list = []
        first_token = threading.Event()
        finished = threading.Event()
        reader = threading.Thread(
            target=_collect,
            args=(service, request_id, events, first_token, finished),
            daemon=True,
        )
        reader.start()
        assert first_token.wait(60), "request never produced a token"

        os.kill(first_route.process_id(REMOTE_STAGE), signal.SIGKILL)
        lost_at = time.monotonic()
        tokens_at_loss = sum(1 for _t, e in events if e.kind == "token")

        # Bounded wait: a hang must fail this test, not hang the suite.
        assert finished.wait(TERMINAL_BOUND_SECONDS), (
            f"no terminal within {TERMINAL_BOUND_SECONDS:.0f}s of the remote "
            f"stage loss; events={[(e.kind, e.code) for _t, e in events]}"
        )
        reader.join(5)
        terminals = [(t, e) for t, e in events if e.kind in _TERMINALS]
        assert [e.kind for _t, e in terminals] == ["failed"], [
            (e.kind, e.code) for _t, e in events
        ]
        failed_at, failed = terminals[0]
        assert failed_at - lost_at <= TERMINAL_BOUND_SECONDS
        assert failed.code, "the failed terminal must name an honest code"
        # No token after the terminal, none beyond the in-flight one, no completion.
        assert all(t <= failed_at for t, _e in events)
        tokens_total = sum(1 for _t, e in events if e.kind == "token")
        assert tokens_total < 8
        assert tokens_total - tokens_at_loss <= 1
        assert not any(e.kind == "completed" for _t, e in events)

        # The degraded route is honest about needing a restart and recovers:
        # the owner replaces it and the next ordinary request completes.
        status = _wait_ready(owner, 120)
        assert status["state"] == "ready" and status["generation"] == 2, status
        assert owner._current is not first_route
        _next_id, after = _run_without_fault(service, owner)
        assert after["kinds"] == ["completed"], after
    finally:
        _teardown(local, owner, router, service)


def _teardown(local, owner, router, service) -> None:
    service.close()
    try:
        router.close()
    finally:
        owner.close()
        local.close()


def _start(service, owner):
    request_id = _submit(service, owner)
    events: list = []
    first_token = threading.Event()
    finished = threading.Event()
    reader = threading.Thread(
        target=_collect,
        args=(service, request_id, events, first_token, finished),
        daemon=True,
    )
    reader.start()
    return events, first_token, finished, reader


def _wait_for_event(events, kind, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(e.kind == kind for _t, e in events):
            return True
        time.sleep(0.02)
    return False


def test_lost_remote_stage_during_prefill_is_a_bounded_failed_terminal(
    tmp_path, watchdog
) -> None:
    # Each stage execution takes 2 s, so the remote stage is lost while the
    # entry stage is still prefilling: no token has been produced yet.
    local, owner, router, service, _target = _compose(tmp_path, step_delay_s=2.0)
    local.session_factory.step_delay_s = 0.15  # only the replacement route
    try:
        first_route = owner._current
        events, _first, finished, reader = _start(service, owner)
        assert _wait_for_event(events, "accepted", 60)
        time.sleep(0.5)
        os.kill(first_route.process_id(REMOTE_STAGE), signal.SIGKILL)
        assert finished.wait(TERMINAL_BOUND_SECONDS), (
            f"no terminal within {TERMINAL_BOUND_SECONDS:.0f}s; "
            f"events={[(e.kind, e.code) for _t, e in events]}"
        )
        reader.join(5)
        kinds = [e.kind for _t, e in events]
        assert kinds.count("failed") == 1 and "completed" not in kinds, kinds
        assert "token" not in kinds, kinds
    finally:
        _teardown(local, owner, router, service)


def test_lost_remote_stage_while_idle_is_reported_not_ready(
    tmp_path, watchdog
) -> None:
    local, owner, router, service, _target = _compose(tmp_path)
    try:
        first_route = owner._current
        _id, baseline = _run_without_fault(service, owner)
        assert baseline["kinds"] == ["completed"], baseline
        os.kill(first_route.process_id(REMOTE_STAGE), signal.SIGKILL)
        # No request is running: the route must say within the bound that it
        # is not serving (never keep advertising a qualification it cannot
        # honour), and admission must be refused instead of hanging.
        deadline = time.monotonic() + TERMINAL_BOUND_SECONDS
        while time.monotonic() < deadline and owner.current() is not None:
            time.sleep(0.1)
        assert owner.current() is None
        assert not owner.public_status().get("route_ready")
    finally:
        _teardown(local, owner, router, service)


def test_failed_terminal_does_not_wait_for_a_slow_replacement_route(
    tmp_path, watchdog
) -> None:
    # A real model route reloads for minutes.  The gateway releases the
    # backend before it publishes the request's terminal, so a replacement
    # opened inside that release held the failed terminal back for the whole
    # reload and the client saw silence.  The replacement here cannot open
    # until the test lets it, i.e. it is slower than any bound.
    local = build_local_physical(tmp_path, shape="single")
    local.session_factory.step_delay_s = 0.15
    replacement_may_open = threading.Event()
    replacement_requested = threading.Event()
    factory_calls = []

    def new_route():
        factory_calls.append(time.monotonic())
        if len(factory_calls) > 1:
            replacement_requested.set()
            assert replacement_may_open.wait(180), "replacement never released"
        return local.new_route()

    owner = ColdRouteRestartOwner(
        route_factory=new_route,
        open_and_qualify=_open_and_qualify,
        restart_budget=1,
    )
    try:
        owner.open()
        router, service, _target = _stack(tmp_path, owner)
    except BaseException:
        replacement_may_open.set()
        owner.close()
        owner.cleanup()
        local.close()
        raise
    try:
        first_route = owner._current
        events, first_token, finished, reader = _start(service, owner)
        assert first_token.wait(60), "request never produced a token"

        os.kill(first_route.process_id(REMOTE_STAGE), signal.SIGKILL)
        lost_at = time.monotonic()
        assert finished.wait(TERMINAL_BOUND_SECONDS), (
            f"no terminal within {TERMINAL_BOUND_SECONDS:.0f}s of the remote "
            f"stage loss while the replacement route was still opening; "
            f"events={[(e.kind, e.code) for _t, e in events]}"
        )
        reader.join(5)
        terminals = [(t, e) for t, e in events if e.kind in _TERMINALS]
        assert [e.kind for _t, e in terminals] == ["failed"], [
            (e.kind, e.code) for _t, e in events
        ]
        failed_at, failed = terminals[0]
        assert failed_at - lost_at <= TERMINAL_BOUND_SECONDS
        # This request's own cleanup was proven, so the terminal carries the
        # bounded recovery code (the same one it carries when the restart is
        # fast), never request_cleanup_unproven.
        assert failed.code == "recovery_successor_unavailable", failed.code

        # Recovery is deferred, not dropped.  Until the replacement is ready
        # admission is refused rather than hanging or serving a dead route.
        assert owner.current() is None
        assert replacement_requested.wait(60), "the route was never restarted"
        assert owner.public_status()["cold_route_restart"]["state"] == "restarting"
        replacement_may_open.set()
        status = _wait_ready(owner, 120)
        assert status["state"] == "ready" and status["generation"] == 2, status
        assert owner._current is not first_route
        _next_id, after = _run_without_fault(service, owner)
        assert after["kinds"] == ["completed"], after
    finally:
        replacement_may_open.set()
        _teardown(local, owner, router, service)
