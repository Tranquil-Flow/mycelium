"""Model-to-production conformance through request-gateway public interfaces."""

from __future__ import annotations

import pytest

from mycelium_qualification.evidence import sha256_bytes
from mycelium_request_conformance.model import Action, Authority, GatewayModel, Phase
from mycelium_request_gateway.service import RequestGatewayService

from .support import (
    CountingBackend,
    MutableQualificationSource,
    clone_qualification,
    drain,
    submission,
    synthetic_qualification,
)


MODEL_AUTHORITY = Authority(
    deployment="deployment",
    epoch=1,
    path="path",
    evidence="evidence",
    qualification="qualification",
    ready=True,
)


def _new_service(qualification, backend, request_id="request-conformance"):
    return RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: request_id,
        max_buffered_events=8,
        max_sessions=4,
    )


def _public_side_effects(service, backend, request_id):
    metrics = service.metrics_snapshot()
    return (
        backend.counters(),
        service.buffered_event_count(request_id),
        service.maximum_observed_buffered_events(request_id),
        service.terminal_event_count(request_id),
        metrics.get("token_events_total", 0),
        metrics.get("requests_failed_total", 0),
        metrics.get("requests_completed_total", 0),
        metrics.get("requests_cancelled_total", 0),
    )


def test_exact_backend_token_replay_has_zero_side_effect_delta():
    qualification = synthetic_qualification()
    holder = {}

    def script(backend, request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "alpha")
        before = _public_side_effects(holder["service"], backend, request_id)
        emit_token(0, "alpha")
        after = _public_side_effects(holder["service"], backend, request_id)
        holder["replay_delta"] = (before, after)
        return "completed"

    backend = CountingBackend(script)
    service = _new_service(qualification, backend)
    holder["service"] = service
    try:
        request_id = service.submit(submission(qualification))
        assert backend.finished.wait(timeout=2)
        events = drain(service, request_id)

        assert holder["replay_delta"][0] == holder["replay_delta"][1]
        assert [event.text for event in events if event.kind == "token"] == ["alpha"]
        assert events[-1].kind == "completed"
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 0, 1, 1, 1, 1, 0, 0)
    finally:
        service.close()


def test_minimal_conflicting_token_replay_matches_reference_failure_and_cleanup():
    qualification = synthetic_qualification()

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        emit_token(0, "alpha")
        emit_token(0, "different")
        return "completed"

    backend = CountingBackend(script)
    service = _new_service(qualification, backend)
    try:
        request_id = service.submit(submission(qualification))
        events = drain(service, request_id)

        model = GatewayModel(current=MODEL_AUTHORITY)
        state = model.apply(Action.admit(MODEL_AUTHORITY, payload="prompt")).state
        state = model.apply(Action.token(0, "alpha"), state=state).state
        expected = model.apply(Action.token(0, "different"), state=state).state

        assert expected.phase is Phase.FAILED
        assert expected.outcome == "conflicting_token_replay"
        assert [event.kind for event in events] == ["accepted", "token", "failed"]
        assert events[-1].code == expected.outcome
        assert service.terminal_event_count(request_id) == expected.terminal_count == 1
        assert backend.runtime_starts == expected.counters.runtime_starts == 1
        assert backend.backend_cancels == expected.counters.backend_cancels == 1
        assert backend.capacity_releases == expected.counters.capacity_releases == 1
        assert backend.kv_cleanups == expected.counters.kv_cleanups == 1
        assert backend.active_capacity == backend.active_kv == 0
    finally:
        service.close()


@pytest.mark.parametrize(
    ("mutation", "model_action", "expected_code"),
    (
        (
            lambda value: clone_qualification(
                value,
                route_ready=False,
                reason_codes=("synthetic_revocation",),
            ),
            Action.change_authority("ready", False),
            "readiness_revoked",
        ),
        (
            lambda value: clone_qualification(value, deployment_epoch=value.deployment_epoch + 1),
            Action.change_authority("epoch", 2),
            "deployment_epoch_changed",
        ),
        (
            lambda value: clone_qualification(value, topology_version=value.topology_version + 1),
            Action.change_authority("path", "changed"),
            "path_changed",
        ),
        (
            lambda value: clone_qualification(
                value,
                path_manifest_digest=sha256_bytes(b"changed-path"),
            ),
            Action.change_authority("path", "changed"),
            "path_changed",
        ),
        (
            lambda value: clone_qualification(
                value,
                evidence_manifest_digest=sha256_bytes(b"changed-evidence"),
            ),
            Action.change_authority("evidence", "changed"),
            "qualification_mismatch",
        ),
        (
            lambda value: clone_qualification(value, qualification_id="changed-qualification"),
            Action.change_authority("qualification", "changed"),
            "stale_qualification",
        ),
    ),
)
def test_completion_revalidates_every_qualification_dimension(
    mutation, model_action, expected_code
):
    qualification = synthetic_qualification()
    source = MutableQualificationSource(qualification)

    def script(backend, _request_id, _submission, _emit_token, _is_cancelled):
        backend.release.wait(timeout=2)
        return "completed"

    backend = CountingBackend(script)
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-revalidation",
        max_buffered_events=8,
    )
    try:
        request_id = service.submit(submission(qualification))
        assert backend.started.wait(timeout=2)
        source.value = mutation(qualification)
        backend.release.set()
        events = drain(service, request_id)

        model = GatewayModel(current=MODEL_AUTHORITY)
        state = model.apply(Action.admit(MODEL_AUTHORITY, payload="prompt")).state
        state = model.apply(model_action, state=state).state
        expected = model.apply(Action.complete(), state=state).state

        assert expected.phase is Phase.FAILED
        assert [event.kind for event in events] == ["accepted", "failed"]
        assert events[-1].code == expected_code
        assert service.terminal_event_count(request_id) == 1
        assert backend.counters() == (1, 1, 1, 1, 1, 1, 0, 0)
    finally:
        service.close()
