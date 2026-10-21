# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import mycelium_request_gateway.service as gateway_service
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification


def _submission(qualification, *, tokens: int = 2) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="tab private prompt",
        max_new_tokens=tokens,
        qualification=qualification_binding(qualification),
    )


def _hold_session_eviction_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Bind session creation and capacity eviction to one held timeline.

    The injected service clock does not cover either site: ``_Session``
    captured real monotonic as its dataclass default factory, whereas
    ``_make_session_room_locked`` calls the service module's ``time``.
    Override those construction/lookup seams, not process-global time.
    """
    now = [time.monotonic()]
    original_session = gateway_service._Session

    def create_session(**kwargs):
        return original_session(**kwargs, created_at=now[0])

    monkeypatch.setattr(gateway_service, "_Session", create_session)
    monkeypatch.setattr(
        gateway_service, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    return now


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


def test_fresh_request_inside_subscribe_window_is_not_cancelled(monkeypatch) -> None:
    """A never-subscribed, nonterminal session is protected only inside grace.

    A second submit must be refused without evicting A; advancing the same
    clock beyond grace must then admit a replacement by evicting A (A4 §5).
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    now = _hold_session_eviction_clock(monkeypatch)
    request_id_source = iter(["stuck-a", "after-grace"])
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
        # Deliberately NO subscribe: prove the actual state before admission.
        session = service._sessions[stuck_id]
        with session.condition:
            assert session.created_at == now[0]
            assert not session.active_subscription
            assert not session.ever_subscribed
            assert session.terminal_event is None
        grace = RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS
        assert 0 <= now[0] - session.created_at < grace
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
        assert RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS > 0
        assert RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS <= 0.5, (
            "grace must be bounded so a truly stuck session cannot pin "
            "capacity forever"
        )
        # Negative control: the real eviction path must fire after crossing
        # this guard. A static held clock cannot conceal a broken backstop.
        now[0] = session.created_at + grace + 0.001
        assert now[0] - session.created_at > grace
        replacement_id = service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert replacement_id == "after-grace"
        assert stuck_id not in service._sessions
    finally:
        service.close()


def test_stuck_session_evicted_after_grace_elapses(monkeypatch) -> None:
    """Never-subscribed nonterminal sessions are reclaimable after grace."""
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    now = _hold_session_eviction_clock(monkeypatch)
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
        session = service._sessions[stuck_id]
        with session.condition:
            assert session.created_at == now[0]
            assert not session.active_subscription
            assert not session.ever_subscribed
            assert session.terminal_event is None
        now[0] = (
            session.created_at
            + RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS
            + 0.001
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


def test_ever_subscribed_then_vanished_is_evicted_immediately(monkeypatch) -> None:
    """An attached-then-vanished consumer is observably abandoned (A4 §5).

    The held clock is still inside grace when the replacement arrives: the
    ever-subscribed path must reclaim it without relying on elapsed time.
    """
    qualification = _synthetic_qualification()
    backend = _EmitterBackend()
    now = _hold_session_eviction_clock(monkeypatch)
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
        sub = service.subscribe(
            stuck_id, last_event_id=None, owner_token="a" * 32
        )
        backend.start.set()
        first = sub.next_event(timeout=1.0)
        assert first is not None
        sub.ack(first.sequence)
        sub.close()

        session = service._sessions[stuck_id]
        with session.condition:
            assert session.created_at == now[0]
            assert session.ever_subscribed
            assert not session.active_subscription
            assert session.terminal_event is None
        assert now[0] - session.created_at < RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS
        replacement_id = service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert replacement_id == "fresh-replacement"
        assert stuck_id not in service._sessions
    finally:
        service.close()
