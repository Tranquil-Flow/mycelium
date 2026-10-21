# SPDX-License-Identifier: AGPL-3.0-only
"""A6 recovery burst through the real gateway publisher must never skip sequences.

Counted attempt a6fin1 (ordinary_browser_source_failure_recovery): the browser
received seq 0..7, then seq 70; seq 8..69 (the recovered continuation) were
dropped by the gateway and the UI failed closed on the sequence gap.  Cause:
the successor output is buffered and appended all at once at cutover, after a
multi-second silence.  The bounded event buffer fills during that burst while
the healthy consumer has not drained yet, and the full-buffer stall check
measured "time since the last acknowledgement" -- stale by the whole recovery
silence -- so a healthy idle consumer was treated as stalled, its prefix was
dropped, and the still-attached subscription kept serving the gapped tail.

Everything here is clock-injected and event-synchronised: no sleeps, no retries.
"""

from __future__ import annotations

import sys
import threading

import pytest

from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification

CAPACITY = 16
BURST_TOKENS = 100
PRE_TOKENS = 3
WAIT = 10.0


class _GatewayClock:
    """Manual clock that tells the test when the producer hit the full-buffer
    stall check (the only ``_clock()`` caller that runs inside ``_accept_token``)."""

    def __init__(self) -> None:
        self._now = 1_000.0
        self._lock = threading.Lock()
        self.producer_at_full_buffer = threading.Event()
        self.watch_full_buffer = False

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def __call__(self) -> float:
        if (
            self.watch_full_buffer
            and sys._getframe(1).f_code.co_name == "_accept_token"
        ):
            self.producer_at_full_buffer.set()
        with self._lock:
            return self._now


class _RecoveryBurstBackend:
    """Emits a few tokens, goes silent (source fenced, successor buffering), then
    publishes the whole recovered continuation in one burst (router cutover)."""

    def __init__(self, clock: _GatewayClock) -> None:
        self._clock = clock
        self.pre_tokens_emitted = threading.Event()
        self.recovery_cutover = threading.Event()
        self.burst_published = threading.Event()

    def run(self, request_id, submission, emit_token, is_cancelled):
        for index in range(PRE_TOKENS):
            emit_token(index, f"t{index}")
        self.pre_tokens_emitted.set()
        assert self.recovery_cutover.wait(timeout=WAIT)
        self._clock.watch_full_buffer = True
        for index in range(PRE_TOKENS, BURST_TOKENS):
            emit_token(index, f"t{index}")
        self.burst_published.set()
        return "completed"

    def cancel(self, request_id):
        return None


def _open(clock: _GatewayClock, backend: _RecoveryBurstBackend):
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a6-burst",
        max_buffered_events=CAPACITY,
        clock=clock,
    )
    request_id = service.submit(
        InferenceSubmission(
            prompt="tab private prompt",
            max_new_tokens=BURST_TOKENS,
            qualification=qualification_binding(qualification),
        ),
        owner_token="a" * 32,
    )
    subscription = service.subscribe(
        request_id, last_event_id=None, owner_token="a" * 32
    )
    return service, subscription


def _drain_through_pre_tokens(subscription, backend):
    assert backend.pre_tokens_emitted.wait(timeout=WAIT)
    seen = []
    while sum(1 for event in seen if event.kind == "token") < PRE_TOKENS:
        event = subscription.next_event(timeout=WAIT)
        assert event is not None
        subscription.ack(event.sequence)
        seen.append(event)
    return seen


def test_recovery_burst_after_idle_gap_reaches_healthy_consumer_without_gap() -> None:
    clock = _GatewayClock()
    backend = _RecoveryBurstBackend(clock)
    service, subscription = _open(clock, backend)
    try:
        events = _drain_through_pre_tokens(subscription, backend)
        # The recovery silence: the consumer is healthy and idle, nothing is
        # owed to it, and far longer than the stall budget elapses.
        clock.advance(
            RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS * 40
        )
        backend.recovery_cutover.set()
        # The producer has filled the bounded buffer with the burst while the
        # consumer has not yet drained a single event of it.
        assert clock.producer_at_full_buffer.wait(timeout=WAIT)

        while True:
            event = subscription.next_event(timeout=WAIT)
            if event is None:
                break
            subscription.ack(event.sequence)
            events.append(event)

        sequences = [event.sequence for event in events]
        assert sequences == list(range(len(sequences))), "gap in published sequence"
        assert [e.text for e in events if e.kind == "token"] == [
            f"t{index}" for index in range(BURST_TOKENS)
        ]
        assert events[-1].kind == "completed"
        assert backend.burst_published.is_set()
    finally:
        backend.recovery_cutover.set()
        service.close()


def test_genuinely_stalled_consumer_never_receives_a_gapped_tail() -> None:
    """A consumer that really stops acknowledging may have its prefix dropped,
    but the attached stream must then fail closed instead of serving the tail."""
    clock = _GatewayClock()
    backend = _RecoveryBurstBackend(clock)
    service, subscription = _open(clock, backend)
    try:
        _drain_through_pre_tokens(subscription, backend)
        backend.recovery_cutover.set()
        # Consumer reads nothing: the buffer fills and the producer blocks at
        # the stall check with the stall clock running from the first owed ack.
        assert clock.producer_at_full_buffer.wait(timeout=WAIT)
        clock.advance(
            RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS * 2
        )
        assert backend.burst_published.wait(timeout=WAIT)

        with pytest.raises(AdmissionError, match="resume_cursor_expired"):
            subscription.next_event(timeout=WAIT)
    finally:
        backend.recovery_cutover.set()
        service.close()
