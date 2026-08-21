from __future__ import annotations

import threading
import time

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


def test_silent_active_subscriber_cannot_pin_worker() -> None:
    """A consumer that attaches and then stops acknowledging WITHOUT closing
    must not pin the dispatch worker (spec A4 §5 slow-consumer).

    This is the physical wedge shape: the SSE loop is parked (blocked send
    to a dead socket) so ``active_subscription`` stays True while no further
    acknowledgement can ever arrive. Once the bounded buffer fills, an
    independent second request must still reach dispatch.
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
        # The consumer goes silent WITHOUT closing: byte-identical to an
        # SSE loop parked in a blocked send. active_subscription stays True.

        deadline = time.monotonic() + 2.0
        while backend.emit_count < 59 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert backend.emit_count >= 59, "producer never filled the buffer"

        service.submit(
            _submission(qualification, tokens=2), owner_token="b" * 32
        )
        assert backend.probe_started.wait(
            timeout=3.0
        ), "second request never reached dispatch: worker pinned by silent subscriber"
        # The stall budget is a bounded product constant, not a hidden timer.
        assert 0 < RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS <= 1.0
    finally:
        service.close()
