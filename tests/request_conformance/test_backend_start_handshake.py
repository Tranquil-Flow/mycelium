"""Backend start/cancel handshake conformance at the public seam."""

from __future__ import annotations

import threading
from typing import Callable, cast

from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission
from mycelium_request_gateway.service import InferenceBackend, RequestGatewayService

from .support import MutableQualificationSource, drain, submission, synthetic_qualification


class StickyStartGapBackend:
    """Expose the service's logical-start to run-entry gap deterministically."""

    def __init__(self) -> None:
        self.run_lookup = threading.Event()
        self.release_lookup = threading.Event()
        self.finished = threading.Event()
        self._cancelled = threading.Event()
        self.cancel_calls = 0
        self.run_entries = 0
        self.runtime_acquires = 0

    @property
    def run(self) -> Callable[..., str]:
        self.run_lookup.set()
        self.release_lookup.wait()
        return self._run

    def _run(
        self,
        _request_id: str,
        _submission: InferenceSubmission,
        _emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        self.run_entries += 1
        try:
            if self._cancelled.is_set() or is_cancelled():
                return "cancelled"
            self.runtime_acquires += 1
            return "completed"
        finally:
            self.finished.set()

    def cancel(self, request_id: str) -> None:
        del request_id
        self.cancel_calls += 1
        self._cancelled.set()


class RecordingRouter:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.admit_calls = 0

    def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True

    def admit(self, *_args: object, **_kwargs: object) -> str:
        self.admit_calls += 1
        raise AssertionError("pre-cancelled request reached Router admission")

    def request_status(self, request_id: str) -> str:
        del request_id
        raise AssertionError("pre-cancelled request reached Router status")

    def decode_one(self, request_id: str) -> bool:
        del request_id
        raise AssertionError("pre-cancelled request reached Router decode")


class RejectEncodeCodec:
    def encode(self, prompt: str) -> tuple[int, ...]:
        del prompt
        raise AssertionError("pre-cancelled request reached prompt encoding")

    def decode_token(self, token_id: int) -> str:
        del token_id
        raise AssertionError("pre-cancelled request reached token decoding")


def test_cancel_in_logical_start_to_run_entry_gap_is_sticky_and_resource_free():
    qualification = synthetic_qualification()
    backend = StickyStartGapBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=cast(InferenceBackend, backend),
        request_id_source=lambda: "request-sticky-start-gap",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert backend.run_lookup.wait(timeout=2)

        assert service.cancel(request_id) is True
        events = drain(service, request_id)
        backend.release_lookup.set()
        assert backend.finished.wait(timeout=2)

        assert [event.kind for event in events] == ["accepted", "cancelled"]
        assert service.terminal_event_count(request_id) == 1
        assert backend.cancel_calls == 1
        assert backend.run_entries == 1
        assert backend.runtime_acquires == 0
    finally:
        backend.release_lookup.set()
        service.close()


def test_router_backend_cancel_is_sticky_before_run_entry():
    router = RecordingRouter()
    backend = RouterSessionBackend(
        router=router,
        codec=RejectEncodeCodec(),
        clock=lambda: 0.0,
    )
    qualification = synthetic_qualification()
    request = submission(qualification)

    backend.cancel("request-router-sticky")
    outcome = backend.run(
        "request-router-sticky",
        request,
        lambda _index, _text: None,
        lambda: False,
    )

    assert outcome == "cancelled"
    assert router.cancelled == ["request-router-sticky"]
    assert router.admit_calls == 0
