"""Token-stream lifecycle tests using local synthetic qualification only.

These tests prove deterministic local session semantics. They do not prove a
distributed route, physical transport, or accepted route_ready evidence.
"""
from __future__ import annotations

from dataclasses import fields
import threading
import time
from typing import Any

import pytest

from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification


class ScriptedBackend:
    def __init__(self, tokens: tuple[str, ...]) -> None:
        self.tokens = tokens
        self.cancelled: list[str] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        for index, text in enumerate(self.tokens):
            if is_cancelled():
                return "cancelled"
            emit_token(index, text)
        return "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)


class ControlledBackend:
    def __init__(self) -> None:
        self.first_emitted = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[str] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        emit_token(0, "first")
        self.first_emitted.set()
        self.release.wait(timeout=2)
        if is_cancelled():
            return "cancelled"
        emit_token(1, "second")
        return "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        self.release.set()


class BurstBackend:
    def __init__(self, count: int) -> None:
        self.count = count
        self.emitted = 0
        self.cancelled: list[str] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        for index in range(self.count):
            emit_token(index, f"piece-{index}")
            self.emitted += 1
        return "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)


class SlowCancelBackend:
    def __init__(self, *, release_worker_before_return: bool = False) -> None:
        self.cancel_entered = threading.Event()
        self.allow_cancel_return = threading.Event()
        self.worker_release = threading.Event()
        self.release_worker_before_return = release_worker_before_return
        self.cancelled: list[str] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        self.worker_release.wait(timeout=2)
        return "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        self.cancel_entered.set()
        if self.release_worker_before_return:
            self.worker_release.set()
        self.allow_cancel_return.wait(timeout=2)
        self.worker_release.set()


def _submission(qualification, max_new_tokens: int = 8) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="sensitive prompt never log",
        max_new_tokens=max_new_tokens,
        qualification=qualification_binding(qualification),
    )


def _clone_qualification(qualification, **changes: Any):
    clone = object.__new__(type(qualification))
    for item in fields(qualification):
        object.__setattr__(clone, item.name, changes.get(item.name, getattr(qualification, item.name)))
    return clone


def _drain(subscription, *, timeout: float = 2.0):
    events = []
    while True:
        event = subscription.next_event(timeout=timeout)
        if event is None:
            break
        events.append(event)
        subscription.ack(event.sequence)
    return events


def test_token_events_are_deterministic_and_terminal_is_exactly_once():
    qualification = _synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = ScriptedBackend(("moon", "lit", " path"))
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "stream-001",
        max_buffered_events=8,
    )
    try:
        request_id = service.submit(_submission(qualification, max_new_tokens=3))
        subscription = service.subscribe(request_id, last_event_id=None)
        events = _drain(subscription)

        assert [event.sequence for event in events] == [0, 1, 2, 3, 4]
        assert [event.kind for event in events] == [
            "accepted",
            "token",
            "token",
            "token",
            "completed",
        ]
        assert [event.text for event in events if event.kind == "token"] == [
            "moon",
            "lit",
            " path",
        ]
        assert sum(event.terminal for event in events) == 1
        assert service.cancel(request_id) is False
        assert service.terminal_event_count(request_id) == 1
    finally:
        service.close()


def test_disconnect_reconnect_resume_has_no_duplicate_output():
    qualification = _synthetic_qualification()
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "resume-001",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(_submission(qualification, max_new_tokens=2))
        first = service.subscribe(request_id, last_event_id=None)
        accepted = first.next_event(timeout=1)
        assert accepted is not None and accepted.kind == "accepted"
        first.ack(accepted.sequence)
        token = first.next_event(timeout=1)
        assert token is not None and token.kind == "token" and token.text == "first"
        first.ack(token.sequence)
        first.close()

        backend.release.set()
        resumed = service.subscribe(request_id, last_event_id=token.sequence)
        remaining = _drain(resumed)

        assert [(event.kind, event.text) for event in remaining] == [
            ("token", "second"),
            ("completed", None),
        ]
        assert all(event.sequence > token.sequence for event in remaining)
        assert service.cancel(request_id) is False
    finally:
        service.close()


def test_reconnect_can_replay_last_server_acked_event_if_client_did_not_apply_it():
    qualification = _synthetic_qualification()
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "resume-inflight-001",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(_submission(qualification, max_new_tokens=2))
        first = service.subscribe(request_id, last_event_id=None)
        accepted = first.next_event(timeout=1)
        assert accepted is not None
        first.ack(accepted.sequence)
        token = first.next_event(timeout=1)
        assert token is not None and token.kind == "token" and token.text == "first"
        first.ack(token.sequence)
        first.close()

        resumed = service.subscribe(request_id, last_event_id=accepted.sequence)
        replayed = resumed.next_event(timeout=1)
        assert replayed == token
        resumed.ack(replayed.sequence)
        backend.release.set()
        remaining = _drain(resumed)

        assert [event.text for event in (replayed, *remaining) if event.kind == "token"] == [
            "first",
            "second",
        ]
    finally:
        backend.release.set()
        service.close()


def test_bounded_backpressure_pauses_backend_until_consumer_acks():
    qualification = _synthetic_qualification()
    backend = BurstBackend(5)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "pressure-001",
        max_buffered_events=3,
    )
    try:
        request_id = service.submit(_submission(qualification, max_new_tokens=5))
        deadline = time.monotonic() + 1
        while backend.emitted < 1 and time.monotonic() < deadline:
            time.sleep(0.005)

        assert backend.emitted == 1
        assert service.buffered_event_count(request_id) == 2
        subscription = service.subscribe(request_id, last_event_id=None)
        events = _drain(subscription)

        assert [event.kind for event in events].count("token") == 5
        assert events[-1].kind == "completed"
        assert service.maximum_observed_buffered_events(request_id) <= 3
    finally:
        service.close()


def test_cancellation_emits_one_terminal_and_cleans_backend_once():
    qualification = _synthetic_qualification()
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "cancel-001",
        max_buffered_events=4,
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.first_emitted.wait(timeout=1)

        assert service.cancel(request_id) is True
        assert service.cancel(request_id) is False
        events = _drain(service.subscribe(request_id, last_event_id=None))

        assert events[-1].kind == "cancelled"
        assert sum(event.terminal for event in events) == 1
        assert backend.cancelled == [request_id]
        assert service.terminal_event_count(request_id) == 1
    finally:
        service.close()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("epoch", "deployment_epoch_changed"),
        ("path", "path_changed"),
        ("dropped", "route_dropped"),
        ("revoked", "readiness_revoked"),
    ],
)
def test_midstream_route_change_stops_output_and_cleans_once(mutation, expected_code):
    qualification = _synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: f"change-{mutation}",
        max_buffered_events=5,
    )
    try:
        request_id = service.submit(_submission(qualification))
        assert backend.first_emitted.wait(timeout=1)
        if mutation == "epoch":
            source.value = _clone_qualification(
                qualification,
                deployment_epoch=qualification.deployment_epoch + 1,
            )
        elif mutation == "path":
            source.value = _clone_qualification(
                qualification,
                path_manifest_digest="sha256:" + "9" * 64,
            )
        elif mutation == "dropped":
            source.value = None
        else:
            source.value = _clone_qualification(
                qualification,
                route_ready=False,
                reason_codes=("readiness_revoked",),
                qualified_by=None,
            )
        backend.release.set()

        events = _drain(service.subscribe(request_id, last_event_id=None))

        assert [event.text for event in events if event.kind == "token"] == ["first"]
        assert events[-1].kind == "failed"
        assert events[-1].code == expected_code
        assert backend.cancelled == [request_id]
        assert service.terminal_event_count(request_id) == 1
    finally:
        service.close()


def test_rejects_duplicate_or_out_of_order_backend_tokens():
    qualification = _synthetic_qualification()

    class DisorderBackend(ScriptedBackend):
        def run(self, request_id, submission, emit_token, is_cancelled):
            emit_token(0, "once")
            emit_token(0, "duplicate")
            emit_token(2, "gap")
            return "completed"

    backend = DisorderBackend(())
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "order-001",
        max_buffered_events=6,
    )
    try:
        request_id = service.submit(_submission(qualification))
        events = _drain(service.subscribe(request_id, last_event_id=None))

        assert [event.text for event in events if event.kind == "token"] == ["once"]
        assert events[-1].kind == "failed"
        assert events[-1].code == "token_order_violation"
        assert backend.cancelled == [request_id]
    finally:
        service.close()


def test_terminal_releases_prompt_and_qualification_state():
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=ScriptedBackend(("done",)),
        request_id_source=lambda: "cleanup-sensitive-001",
    )
    try:
        request_id = service.submit(_submission(qualification))
        _drain(service.subscribe(request_id, last_event_id=None))
        session = service._get_session(request_id)
        deadline = time.monotonic() + 1
        while not session.worker_done and time.monotonic() < deadline:
            time.sleep(0.005)

        assert session.worker_done
        assert session.submission is None
        assert session.captured is None
    finally:
        service.close()


def test_session_table_is_bounded_by_evicting_oldest_terminal_session():
    qualification = _synthetic_qualification()
    identifiers = iter(("bounded-001", "bounded-002", "bounded-003"))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=ScriptedBackend(()),
        request_id_source=lambda: next(identifiers),
        max_sessions=2,
    )
    try:
        first = service.submit(_submission(qualification))
        second = service.submit(_submission(qualification))
        deadline = time.monotonic() + 1
        while (
            service.terminal_event_count(first) != 1
            or service.terminal_event_count(second) != 1
        ) and time.monotonic() < deadline:
            time.sleep(0.005)

        third = service.submit(_submission(qualification))

        with pytest.raises(Exception) as evicted:
            service.terminal_event_count(first)
        assert getattr(evicted.value, "code", None) == "unknown_request"
        assert service.terminal_event_count(second) == 1
        deadline = time.monotonic() + 1
        while service.terminal_event_count(third) != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        service.close()


def test_active_session_capacity_rejects_without_starting_another_worker():
    qualification = _synthetic_qualification()
    backend = ControlledBackend()
    identifiers = iter(("active-001", "active-002"))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(identifiers),
        max_sessions=1,
    )
    try:
        service.submit(_submission(qualification))
        assert backend.first_emitted.wait(timeout=1)
        with pytest.raises(Exception) as full:
            service.submit(_submission(qualification))
        assert getattr(full.value, "code", None) == "gateway_capacity_exhausted"
    finally:
        backend.release.set()
        service.close()


def test_only_one_concurrent_cancellation_is_accepted():
    qualification = _synthetic_qualification()
    backend = SlowCancelBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "cancel-race-001",
    )
    results: list[bool] = []
    try:
        request_id = service.submit(_submission(qualification))
        first = threading.Thread(target=lambda: results.append(service.cancel(request_id)))
        first.start()
        assert backend.cancel_entered.wait(timeout=1)

        results.append(service.cancel(request_id))
        backend.allow_cancel_return.set()
        first.join(timeout=1)

        assert sorted(results) == [False, True]
        assert backend.cancelled == [request_id]
        events = _drain(service.subscribe(request_id, last_event_id=None))
        assert events[-1].kind == "cancelled"
        assert service.terminal_event_count(request_id) == 1
    finally:
        backend.allow_cancel_return.set()
        service.close()


def test_accepted_cancellation_wins_completion_race():
    qualification = _synthetic_qualification()
    backend = SlowCancelBackend(release_worker_before_return=True)
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "cancel-wins-001",
    )
    result: list[bool] = []
    try:
        request_id = service.submit(_submission(qualification))
        cancellation = threading.Thread(target=lambda: result.append(service.cancel(request_id)))
        cancellation.start()
        assert backend.cancel_entered.wait(timeout=1)

        deadline = time.monotonic() + 1
        while service.terminal_event_count(request_id) == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        backend.allow_cancel_return.set()
        cancellation.join(timeout=1)

        events = _drain(service.subscribe(request_id, last_event_id=None))
        assert result == [True]
        assert events[-1].kind == "cancelled"
    finally:
        backend.allow_cancel_return.set()
        service.close()


def test_request_id_must_be_url_safe():
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=ScriptedBackend(()),
        request_id_source=lambda: "unsafe/request",
    )
    try:
        with pytest.raises(Exception) as invalid:
            service.submit(_submission(qualification))
        assert getattr(invalid.value, "code", None) == "invalid_request_id"
    finally:
        service.close()
