"""Public-interface lifecycle, replay-window, and capacity conformance tests."""

from __future__ import annotations

import threading

from mycelium_request_gateway.service import RequestGatewayService

from .support import CountingBackend, drain, submission, synthetic_qualification


class PausingRevalidationSource:
    """Allow capture, then pause the worker's first continuous revalidation."""

    def __init__(self, qualification) -> None:
        self.value = qualification
        self.blocked = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._reads = 0

    def current(self):
        with self._lock:
            self._reads += 1
            reads = self._reads
        if reads == 2:
            self.blocked.set()
            self.release.wait(timeout=2)
        return self.value


def test_cancel_before_backend_start_has_no_runtime_capacity_or_kv_effects():
    qualification = synthetic_qualification()
    source = PausingRevalidationSource(qualification)

    def script(_backend, _request_id, _submission, _emit_token, _is_cancelled):
        raise AssertionError("cancelled request reached backend runtime")

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-cancel-before-start",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert source.blocked.wait(timeout=2)

        assert service.cancel(request_id) is True
        events = drain(service, request_id)
        source.release.set()
        service.close()

        assert [event.kind for event in events] == ["accepted", "cancelled"]
        assert service.terminal_event_count(request_id) == 1
        assert backend.finished.is_set() is False
        assert backend.counters() == (0, 1, 0, 0, 0, 0, 0, 0)
    finally:
        source.release.set()
        service.close()
