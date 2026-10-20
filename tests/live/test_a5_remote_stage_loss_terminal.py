# SPDX-License-Identifier: AGPL-3.0-or-later
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
from tests.live._a6_local_physical import PROMPT  # noqa: F401 - fixture parity
from tests.live.test_a6_local_physical_recovery import (
    _TERMINALS,
    SIDECAR_BINARY,
    _compose,
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
    local, owner, router, service, _target = _compose(tmp_path, "single")
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
        service.close()
        try:
            router.close()
        finally:
            owner.close()
            local.close()
