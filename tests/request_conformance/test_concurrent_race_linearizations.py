"""Concurrent request-gateway race outcomes checked against serial model linearizations."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable

from mycelium_request_conformance.model import Authority, GatewayModel, Phase
from mycelium_request_conformance.trace import generate_race_traces, run_trace, trace_to_json
from mycelium_request_gateway.contracts import AdmissionError, InferenceSubmission
from mycelium_request_gateway.service import RequestGatewayService

from .support import (
    MutableQualificationSource,
    clone_qualification,
    drain,
    submission,
    synthetic_qualification,
)


CURRENT = Authority(
    deployment="deploy-a",
    epoch=7,
    path="path-a",
    evidence="evidence-a",
    qualification="qualification-a",
    ready=True,
)


class ConcurrentRaceBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._finish = threading.Event()
        self._emit_token = None
        self._outcome: str | None = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self.runtime_starts = 0
        self.backend_cancels = 0
        self.capacity_acquires = 0
        self.capacity_releases = 0
        self.kv_acquires = 0
        self.kv_cleanups = 0

    def run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        del request_id, submission
        with self._lock:
            self.runtime_starts += 1
            self.capacity_acquires += 1
            self.kv_acquires += 1
            self._emit_token = emit_token
        self.started.set()
        self._finish.wait()
        try:
            with self._lock:
                outcome = self._outcome
            if outcome is not None:
                return outcome
            return "cancelled" if is_cancelled() else "completed"
        finally:
            with self._lock:
                self.capacity_releases += 1
                self.kv_cleanups += 1
            self.finished.set()

    def emit(self) -> str:
        with self._lock:
            callback = self._emit_token
        assert callback is not None
        try:
            callback(0, "token-0")
        except AdmissionError as error:
            return error.code
        return "token_accepted"

    def complete(self) -> None:
        with self._lock:
            if self._outcome is None:
                self._outcome = "completed"
        self._finish.set()

    def cancel(self, request_id: str) -> None:
        del request_id
        with self._lock:
            self.backend_cancels += 1
            if self._outcome is None:
                self._outcome = "cancelled"
        self._finish.set()

    def counters(self) -> tuple[int, ...]:
        with self._lock:
            return (
                self.runtime_starts,
                self.backend_cancels,
                self.capacity_acquires,
                self.capacity_releases,
                self.kv_acquires,
                self.kv_cleanups,
                self.capacity_acquires - self.capacity_releases,
                self.kv_acquires - self.kv_cleanups,
            )


def _event_projection(event) -> tuple[object, ...]:
    text_digest = getattr(event, "text_digest", None)
    if not hasattr(event, "text_digest") and event.text is not None:
        text_digest = hashlib.sha256(event.text.encode("utf-8")).hexdigest()
    return (event.sequence, event.kind, event.token_index, text_digest, event.code)


def _counter_projection(state) -> tuple[int, ...]:
    counters = state.counters
    return (
        counters.runtime_starts,
        counters.backend_cancels,
        counters.capacity_acquires,
        counters.capacity_releases,
        counters.kv_acquires,
        counters.kv_cleanups,
        counters.capacity_acquires - counters.capacity_releases,
        counters.kv_acquires - counters.kv_cleanups,
    )


def _permitted_outcomes() -> set[tuple[object, ...]]:
    outcomes: set[tuple[object, ...]] = set()
    for trace in generate_race_traces(CURRENT):
        result = run_trace(GatewayModel(current=CURRENT), trace)
        state = result.state
        outcomes.add(
            (
                tuple(_event_projection(event) for event in state.events),
                _counter_projection(state),
                state.phase.value,
            )
        )
    return outcomes


def test_simultaneous_token_cancel_revoke_disconnect_and_complete_linearize_to_model():
    permitted = _permitted_outcomes()
    assert permitted

    for iteration in range(25):
        qualification = synthetic_qualification()
        source = MutableQualificationSource(qualification)
        backend = ConcurrentRaceBackend()
        service = RequestGatewayService(
            qualification_source=source,
            backend=backend,
            request_id_source=lambda: f"concurrent-race-{iteration}",
        )
        request_id = service.submit(submission(qualification))
        assert backend.started.wait(timeout=2)
        stream = service.subscribe(request_id, last_event_id=-1)
        barrier = threading.Barrier(6)
        results_lock = threading.Lock()
        operation_results: dict[str, tuple[str, object]] = {}

        def run(name: str, operation) -> None:
            barrier.wait()
            try:
                result = operation()
            except BaseException as error:
                outcome = ("exception", getattr(error, "code", type(error).__name__))
            else:
                outcome = ("return", result)
            with results_lock:
                operation_results[name] = outcome

        operations = (
            ("token", backend.emit),
            ("cancel", lambda: service.cancel(request_id)),
            (
                "revoke",
                lambda: setattr(
                    source,
                    "value",
                    clone_qualification(source.value, route_ready=False),
                ),
            ),
            ("disconnect", stream.close),
            ("complete", backend.complete),
        )
        workers = [
            threading.Thread(target=run, args=(name, operation))
            for name, operation in operations
        ]
        try:
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=2)
                assert not worker.is_alive()
            assert set(operation_results) == {
                "token",
                "cancel",
                "revoke",
                "disconnect",
                "complete",
            }
            assert all(kind == "return" for kind, _value in operation_results.values())
            assert operation_results["cancel"][1] in {True, False}
            assert operation_results["token"][1] in {
                "token_accepted",
                "request_cancelled",
                "request_state_released",
                "readiness_revoked",
            }
            assert operation_results["revoke"][1] is None
            assert operation_results["disconnect"][1] is None
            assert operation_results["complete"][1] is None
            assert backend.finished.wait(timeout=2)
            stream.close()
            events = drain(service, request_id)
            terminal_kind = events[-1].kind
            phase = {
                "completed": Phase.COMPLETED.value,
                "cancelled": Phase.CANCELLED.value,
                "failed": Phase.FAILED.value,
            }[terminal_kind]
            observed = (
                tuple(_event_projection(event) for event in events),
                backend.counters(),
                phase,
            )
            assert observed in permitted, {
                "iteration": iteration,
                "observed": observed,
                "operation_results": operation_results,
                "permitted_traces": [
                    trace_to_json(trace) for trace in generate_race_traces(CURRENT)
                ],
            }
            assert service.terminal_event_count(request_id) == 1
        finally:
            backend.complete()
            service.close()
