from __future__ import annotations

import threading

import pytest

from mycelium_request_gateway.contracts import AdmissionError, InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification


def _submission(qualification) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="tab private prompt",
        max_new_tokens=2,
        qualification=qualification_binding(qualification),
    )


def test_mid_request_reconnect_replays_without_duplicate_or_cancel() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def __init__(self) -> None:
            self.first = threading.Event()
            self.release = threading.Event()
            self.cancelled: list[str] = []
            self.publisher_generations: list[tuple[int, int]] = []

        def run(self, request_id, submission, emit_token, is_cancelled):
            emit_token(0, "first")
            self.first.set()
            self.release.wait(timeout=2.0)
            emit_token(1, "second")
            return "completed"

        def cancel(self, request_id):
            self.cancelled.append(request_id)
            self.release.set()

        def update_publisher_generation(
            self,
            request_id,
            *,
            expected_generation,
            new_generation,
        ):
            assert request_id == "reconnect-a4"
            self.publisher_generations.append(
                (expected_generation, new_generation)
            )
            return True

    backend = Backend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "reconnect-a4",
    )
    try:
        request_id = service.submit(_submission(qualification), owner_token="a" * 32)
        assert backend.first.wait(timeout=1.0)
        first = service.subscribe(request_id, last_event_id=None, owner_token="a" * 32)
        accepted = first.next_event(timeout=1.0)
        assert accepted is not None
        first.ack(accepted.sequence)
        token = first.next_event(timeout=1.0)
        assert token is not None
        first.ack(token.sequence)
        first.close()

        second = service.subscribe(
            request_id,
            last_event_id=(first.publisher_generation, token.sequence),
            owner_token="a" * 32,
        )
        assert second.publisher_generation == first.publisher_generation + 1
        backend.release.set()
        replayed = []
        while True:
            event = second.next_event(timeout=1.0)
            if event is None:
                break
            replayed.append(event)
            second.ack(event.sequence)
        second.close()
        assert [event.text for event in replayed if event.kind == "token"] == ["second"]
        assert replayed[-1].kind == "completed"
        assert all(
            event.publisher_generation == second.publisher_generation
            for event in replayed
        )
        assert backend.cancelled == []
        assert backend.publisher_generations == [(0, 1), (1, 2)]
        with pytest.raises(AdmissionError, match="stale_publisher_generation"):
            service.subscribe(
                request_id,
                last_event_id=(first.publisher_generation, token.sequence),
                owner_token="a" * 32,
            )
    finally:
        backend.release.set()
        service.close()


def test_second_session_cannot_observe_or_resume_private_request() -> None:
    qualification = _synthetic_qualification()

    class Backend:
        def run(self, request_id, submission, emit_token, is_cancelled):
            emit_token(0, "private response")
            return "completed"

        def cancel(self, request_id):
            return None

    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=Backend(),
        request_id_source=lambda: "private-a4",
    )
    try:
        request_id = service.submit(_submission(qualification), owner_token="a" * 32)
        with pytest.raises(AdmissionError, match="session_owner_mismatch"):
            service.subscribe(request_id, last_event_id=None, owner_token="b" * 32)
        with pytest.raises(AdmissionError, match="session_owner_mismatch"):
            service.cancel(request_id, owner_token="b" * 32)
        with service.subscribe(
            request_id,
            last_event_id=None,
            owner_token="a" * 32,
        ) as owned:
            assert owned.next_event(timeout=1.0) is not None
    finally:
        service.close()
