from __future__ import annotations

import threading
import time

import pytest

from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification


def _submission(qualification, *, tokens: int = 2) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="tab private prompt",
        max_new_tokens=tokens,
        qualification=qualification_binding(qualification),
    )


class _EmitterBackend:
    """Backend whose first request emits a long token stream on demand.

    The stream is deliberately longer than the gateway's bounded event
    buffer. ``emit_count`` stalls when the producer wedges inside
    ``emit_token``; ``probe_started`` records whether an independent
    second request ever reached dispatch.
    """

    def __init__(self, *, token_count: int = 80) -> None:
        self.start = threading.Event()
        self.emit_count = 0
        self.probe_started = threading.Event()
        self.cancelled: list[str] = []
        self._token_count = token_count

    def run(self, request_id, submission, emit_token, is_cancelled):
        if request_id == "probe-b":
            self.probe_started.set()
            return "completed"
        self.start.wait(timeout=2.0)
        for index in range(self._token_count):
            emit_token(index, f"tok{index}")
            self.emit_count += 1
        return "completed"

    def cancel(self, request_id):
        self.cancelled.append(request_id)


def test_abandoned_subscriber_cannot_wedge_dispatcher() -> None:
    """A vanished consumer must not pin a worker (spec A4 §5).

    The subscriber reads two events, then closes without acknowledging the
    rest — byte-identical to a browser tab closing mid-stream. Once the
    bounded event buffer fills, an independent second request must still
    reach dispatch.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["wedge-a", "probe-b"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
    )
    try:
        request_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        subscription = service.subscribe(
            request_id, last_event_id=None, owner_token="a" * 32
        )
        backend.start.set()
        first = subscription.next_event(timeout=1.0)
        assert first is not None
        subscription.ack(first.sequence)
        second = subscription.next_event(timeout=1.0)
        assert second is not None
        subscription.ack(second.sequence)
        subscription.close()  # abandoned: no further acknowledgements

        deadline = time.monotonic() + 2.0
        while backend.emit_count < 59 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert backend.emit_count >= 59, "producer never filled the buffer"
        time.sleep(0.3)  # let the producer reach the full-buffer wait

        service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert backend.probe_started.wait(
            timeout=2.0
        ), "second request never reached dispatch: worker pinned by abandoned subscriber"
    finally:
        service.close()


def test_stuck_nonterminal_session_is_evictable_under_pressure() -> None:
    """A wedged, non-terminal session must not be permanently un-evictable.

    With a single-slot session table, a new submission must be admitted even
    when the only resident session is stuck non-terminal with no consumer.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["wedge-a", "probe-b"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
        max_sessions=1,
    )
    try:
        request_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        subscription = service.subscribe(
            request_id, last_event_id=None, owner_token="a" * 32
        )
        backend.start.set()
        first = subscription.next_event(timeout=1.0)
        assert first is not None
        subscription.ack(first.sequence)
        subscription.close()  # abandoned

        deadline = time.monotonic() + 2.0
        while backend.emit_count < 59 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert backend.emit_count >= 59, "producer never filled the buffer"
        time.sleep(0.3)

        fresh_id = service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert fresh_id == "probe-b"
    finally:
        service.close()


def test_dropped_events_never_advance_committed_watermark() -> None:
    """A6 seam 8: drops must move the replay boundary only.

    When the vanished-consumer path discards buffered events, the committed
    delivery watermark (acknowledged_through) must stay at the last
    acknowledged sequence; only the replay boundary (discarded_through) may
    advance, so a later reconnect replay of the dropped prefix fails closed
    via resume_cursor_expired instead of being treated as delivered.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["wedge-a", "probe-b"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
    )
    try:
        request_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        subscription = service.subscribe(
            request_id, last_event_id=None, owner_token="a" * 32
        )
        backend.start.set()
        first = subscription.next_event(timeout=1.0)
        assert first is not None
        subscription.ack(first.sequence)
        second = subscription.next_event(timeout=1.0)
        assert second is not None
        subscription.ack(second.sequence)
        last_acknowledged = second.sequence
        subscription.close()  # abandoned

        deadline = time.monotonic() + 5.0
        session = service._sessions[request_id]
        while session.terminal_event is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session.terminal_event is not None, "stream never reached terminal"
        assert backend.emit_count == backend._token_count, "stream did not complete"
        assert session.acknowledged_through == last_acknowledged, (
            "committed delivery watermark advanced past the last acknowledgement"
        )
        assert session.discarded_through >= last_acknowledged, (
            "replay boundary never advanced past the dropped prefix"
        )
        # The dropped prefix must fail closed on reconnect.
        assert session.discarded_through > last_acknowledged
    finally:
        service.close()


def test_fresh_request_inside_subscribe_window_is_not_cancelled() -> None:
    """A fresh request inside the submit-then-subscribe window must not be
    cancelled by its own kind under capacity pressure (spec A4 §5).

    Setup:
      - resident session A: admitted, its SSE subscriber never attached,
        its backend never reached terminal — exactly the shape of a
        browser mid-handshake. State: ``active_subscription=False``,
        ``terminal_event=None``.
      - fresh session B: submitted moments later, while A is still in the
        subscribe window. By the time B's submit reaches
        ``_make_session_room_locked`` the table is at capacity and A
        looks eligible for eviction.

    Expected (with minimum-age grace enabled):
      - A is OLDER than the grace; B is YOUNGER. The grace protects B
        even though A matches the stuck condition.
      - B's submit raises ``gateway_capacity_exhausted`` — the gate
        rejects a second request rather than cancelling B (or A) while
        A's subscribe is still in flight.
      - A remains in ``_sessions`` (the eviction path was bounded, not
        lossy).
      - Once A's grace elapses with no subscribe, a subsequent submit is
        admitted by evicting A normally — see the
        ``eviction_age_grace_constant`` reflection below for the bound.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["stuck-a", "fresh-b"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
        max_sessions=1,
    )
    try:
        # A: admitted but never subscribed.
        stuck_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        # Deliberately NO service.subscribe(stuck_id) — the browser mid-
        # handshake is byte-identical to this state.
        # Drive the producer a little so A is clearly stuck, not idle.
        time.sleep(0.02)
        # B: a second submit inside the grace window. This must NOT be
        # cancelled. With the grace, B is rejected instead.
        with pytest.raises(Exception) as exc_info:
            service.submit(
                _submission(qualification, tokens=2), owner_token="b" * 32
            )
        assert getattr(exc_info.value, "code", None) == (
            "gateway_capacity_exhausted"
        ), (
            "fresh request inside the subscribe window must be rejected "
            "(capacity-exhausted), never cancelled by eviction"
        )
        assert stuck_id in service._sessions, (
            "resident session was unexpectedly evicted during the grace "
            "window — the eviction path must respect the minimum-age "
            "guard"
        )
        # The grace is a bounded product constant, not a hidden timer:
        # verify the documented bound is exposed and positive.
        assert (
            RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS > 0
        )
        assert (
            RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS <= 0.5
        ), (
            "grace must be bounded so a truly stuck session cannot pin "
            "capacity forever"
        )
    finally:
        service.close()


def test_stuck_session_evicted_after_grace_elapses() -> None:
    """Symmetric companion: once the grace elapses with no subscribe, a
    genuinely stuck session is evicted normally on the next submit.

    This guard exists so the grace cannot be used to wedge capacity
    permanently — the preceding test ensures the race is closed; this
    one ensures the wedge is not widened by the close.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["aged-stuck", "replacement"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
        max_sessions=1,
    )
    try:
        stuck_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        # Sleep just past the grace, then submit the replacement.
        time.sleep(
            RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS + 0.1
        )
        replacement_id = service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert replacement_id == "replacement"
        assert stuck_id not in service._sessions, (
            "stuck session past its grace was not evicted — the eviction "
            "path must still fire after the minimum-age guard elapses"
        )
    finally:
        service.close()


def test_ever_subscribed_then_vanished_is_evicted_immediately() -> None:
    """An attached-then-vanished consumer is observably abandoned (spec A4 §5).

    When a session's subscriber has attached and then detached, the slot is
    demonstrably orphaned; no further acks can ever arrive. The eviction
    path must reclaim that slot without waiting for any grace period,
    because ``ever_subscribed`` is a stronger signal than a timer.

    The companion case — a session that has never subscribed — is protected
    by the backstop grace in
    ``test_fresh_request_inside_subscribe_window_is_not_cancelled``.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    request_id_source = iter(["stuck-then-vanished", "fresh-replacement"])
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_id_source),
        max_concurrent_requests=1,
        max_sessions=1,
    )
    try:
        stuck_id = service.submit(
            _submission(qualification, tokens=backend._token_count),
            owner_token="a" * 32,
        )
        # Attach a subscriber, ack one event, then close. The session is
        # now observably ``ever_subscribed=True`` and ``active_subscription
        # =False``: a definitive abandoned slot, not a never-subscribed
        # pending request.
        sub = service.subscribe(
            stuck_id, last_event_id=None, owner_token="a" * 32
        )
        backend.start.set()
        first = sub.next_event(timeout=1.0)
        assert first is not None
        sub.ack(first.sequence)
        sub.close()  # detached after attaching

        # Even with a backstop grace, the ever-subscribed case evicts
        # immediately. We do not sleep any grace at all here.
        t0 = time.monotonic()
        replacement_id = service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        elapsed = time.monotonic() - t0
        assert replacement_id == "fresh-replacement"
        # No grace waited: the new submit returns well under the
        # backstop's worth of wall clock.
        assert elapsed < RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS, (
            f"eviction waited {elapsed:.3f}s but ever_subscribed should "
            "evict immediately without grace"
        )
        assert stuck_id not in service._sessions
    finally:
        service.close()
