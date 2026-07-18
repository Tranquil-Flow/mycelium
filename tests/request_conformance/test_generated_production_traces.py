"""Replay every generated model trace through public production interfaces."""

from __future__ import annotations

import time
from collections.abc import Sequence
import hashlib

from mycelium_qualification.evidence import sha256_bytes
from mycelium_request_conformance.model import Action, Authority, GatewayModel, ModelState, Phase
from mycelium_request_conformance.trace import (
    generate_bounded_traces,
    generate_race_traces,
    materialize_action,
    trace_to_json,
)
from mycelium_request_gateway.contracts import AdmissionError
from mycelium_request_gateway.service import EventSubscription, RequestGatewayService

from .support import (
    ControlledBackend,
    MutableQualificationSource,
    clone_qualification,
    submission,
    synthetic_qualification,
)


CURRENT = Authority(
    deployment="deployment-a",
    epoch=1,
    path="path-a",
    evidence="evidence-a",
    qualification="qualification-a",
    ready=True,
)
TERMINAL_PHASES = {Phase.COMPLETED, Phase.CANCELLED, Phase.FAILED}


def _materialized_transitions(trace: Sequence[Action]):
    model = GatewayModel(current=CURRENT, buffer_capacity=4)
    state = model.initial_state
    transitions = []
    for symbolic in trace:
        action = materialize_action(symbolic, state)
        result = model.apply(action, state=state)
        transitions.append((action, state, result.state, result.code))
        state = result.state
    return tuple(transitions), state


def _change_source(source: MutableQualificationSource, action: Action) -> None:
    current = source.value
    assert current is not None
    if action.field == "ready":
        source.value = clone_qualification(
            current,
            route_ready=bool(action.value),
            reason_codes=() if action.value else ("synthetic_revocation",),
        )
    elif action.field == "epoch":
        source.value = clone_qualification(
            current,
            deployment_epoch=current.deployment_epoch + 1,
        )
    elif action.field == "path":
        source.value = clone_qualification(
            current,
            topology_version=current.topology_version + 1,
        )
    elif action.field == "evidence":
        source.value = clone_qualification(
            current,
            evidence_manifest_digest=sha256_bytes(str(action.value).encode("utf-8")),
        )
    else:
        raise AssertionError(f"unsupported_generated_authority_field:{action.field}")


def _wait_for_terminal(service: RequestGatewayService, backend: ControlledBackend, request_id: str) -> None:
    deadline = time.monotonic() + 2
    while service.terminal_event_count(request_id) != 1:
        if time.monotonic() >= deadline:
            raise TimeoutError("generated_trace_terminal_timeout")
        time.sleep(0.0005)
    if not backend.finished.wait(timeout=2):
        raise TimeoutError("generated_trace_cleanup_timeout")


def _event_projection(event) -> tuple[object, ...]:
    if hasattr(event, "text_digest"):
        text_digest = event.text_digest
    elif event.text is None:
        text_digest = None
    else:
        text_digest = hashlib.sha256(event.text.encode("utf-8")).hexdigest()
    return (event.sequence, event.kind, event.token_index, text_digest, event.code)


def _assert_generated_trace(trace: Sequence[Action], index: int) -> None:
    transitions, expected = _materialized_transitions(trace)
    qualification = synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: f"generated-trace-{index}",
        max_buffered_events=4,
        max_sessions=1,
    )
    request_id: str | None = None
    stream: EventSubscription | None = None
    trace_document = trace_to_json(trace)
    try:
        for action, before, after, _code in transitions:
            if action.kind == "admit":
                assert action.payload is not None and action.max_new_tokens is not None
                request_id = service.submit(
                    submission(
                        qualification,
                        prompt=action.payload,
                        tokens=action.max_new_tokens,
                    )
                )
            elif action.kind == "start":
                assert backend.started.wait(timeout=2), trace_document
            elif action.kind == "token":
                assert before.phase is Phase.STREAMING, trace_document
                assert action.token_index is not None and action.text is not None
                backend.emit(action.token_index, action.text)
            elif action.kind == "complete":
                assert before.phase is Phase.STREAMING, trace_document
                backend.complete()
            elif action.kind == "cancel":
                if request_id is None:
                    try:
                        service.cancel("generated-trace-missing")
                    except AdmissionError as exc:
                        assert exc.code == "unknown_request", trace_document
                    else:
                        raise AssertionError(f"missing_unknown_request:{trace_document}")
                else:
                    accepted = service.cancel(request_id)
                    assert accepted is (
                        before.phase in {Phase.ADMITTED, Phase.STREAMING}
                    ), trace_document
            elif action.kind == "change_authority":
                _change_source(source, action)
            elif action.kind == "reconnect":
                assert action.cursor is not None
                if request_id is None:
                    try:
                        service.subscribe(
                            "generated-trace-missing",
                            last_event_id=action.cursor,
                        )
                    except AdmissionError as exc:
                        assert exc.code == "unknown_request", trace_document
                    else:
                        raise AssertionError(f"missing_unknown_request:{trace_document}")
                elif before.attached:
                    try:
                        service.subscribe(request_id, last_event_id=action.cursor)
                    except AdmissionError as exc:
                        assert exc.code == "stream_already_attached", trace_document
                    else:
                        raise AssertionError(f"missing_stream_already_attached:{trace_document}")
                else:
                    stream = service.subscribe(request_id, last_event_id=action.cursor)
            elif action.kind == "disconnect" and stream is not None:
                stream.close()
                stream = None

            if before.phase not in TERMINAL_PHASES and after.phase in TERMINAL_PHASES:
                assert request_id is not None
                _wait_for_terminal(service, backend, request_id)

        if request_id is None:
            assert expected.phase is Phase.NEW, trace_document
            assert expected.events == (), trace_document
            assert expected.counters.runtime_starts == 0, trace_document
            assert expected.counters.total_events == 0, trace_document
            assert backend.counters() == (0, 0, 0, 0, 0, 0, 0, 0), trace_document
            assert all(value == 0 for value in service.metrics_snapshot().values()), trace_document
            return

        if expected.phase in TERMINAL_PHASES:
            _wait_for_terminal(service, backend, request_id)
        else:
            assert backend.finished.is_set() is False, trace_document

        counters = expected.counters
        expected_backend = (
            counters.runtime_starts,
            counters.backend_cancels,
            counters.capacity_acquires,
            counters.capacity_releases,
            counters.kv_acquires,
            counters.kv_cleanups,
            counters.capacity_acquires - counters.capacity_releases,
            counters.kv_acquires - counters.kv_cleanups,
        )
        assert backend.counters() == expected_backend, trace_document
        assert service.buffered_event_count(request_id) == len(expected.events), trace_document
        assert (
            service.maximum_observed_buffered_events(request_id)
            == counters.maximum_buffered
        ), trace_document
        assert service.terminal_event_count(request_id) == expected.terminal_count, trace_document

        metrics = service.metrics_snapshot()
        assert metrics.get("requests_admitted_total", 0) == 1, trace_document
        assert metrics.get("token_events_total", 0) == counters.token_events, trace_document
        assert metrics.get("requests_failed_total", 0) == counters.failures, trace_document
        assert metrics.get("requests_completed_total", 0) == int(
            expected.phase is Phase.COMPLETED
        ), trace_document
        assert metrics.get("requests_cancelled_total", 0) == int(
            expected.phase is Phase.CANCELLED
        ), trace_document

        if stream is None:
            stream = service.subscribe(request_id, last_event_id=None)
        observed = []
        for _ in expected.events:
            event = stream.next_event(timeout=2)
            assert event is not None, trace_document
            observed.append(event)
            stream.ack(event.sequence)
        assert tuple(map(_event_projection, observed)) == tuple(
            map(_event_projection, expected.events)
        ), trace_document
        if expected.phase in TERMINAL_PHASES:
            assert stream.next_event(timeout=0) is None, trace_document
    finally:
        if stream is not None:
            stream.close()
        service.close()


def test_all_generated_traces_match_production_events_counters_and_cleanup():
    traces = (*generate_bounded_traces(CURRENT), *generate_race_traces(CURRENT))
    assert len(traces) == len({trace_to_json(trace) for trace in traces})

    for index, trace in enumerate(traces):
        _assert_generated_trace(trace, index)
