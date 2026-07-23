"""Device-free tests for qualification-bound Router request admission.

All routes are local fakes.  These tests make no physical-run claim and keep
fixture device state ``route_ready=False``; only qualifier-owned records may
open the ordinary request path.
"""
from __future__ import annotations

import dataclasses

import pytest

from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from test_backend_cli import RecordingCodec, _runtime_stack
from test_core import _synthetic_qualification


class CurrentAuthority:
    def __init__(self, record):
        self.record = record

    def current(self):
        return self.record


class RecordingRouter:
    def __init__(self, router):
        self.router = router
        self.admit_calls = 0
        self.cancel_calls = 0

    def current_deployment(self):
        return self.router.entry.topology.snapshot()

    def admit(self, *args, **kwargs):
        self.admit_calls += 1
        return self.router.admit(*args, **kwargs)

    def decode_one(self, request_id):
        return self.router.decode_one(request_id)

    def request_status(self, request_id):
        return self.router.request_status(request_id)

    def cancel(self, request_id):
        self.cancel_calls += 1
        return self.router.cancel(request_id)


def submission(record, prompt="PROMPT-PRIVATE-3B4"):
    return InferenceSubmission(
        prompt=prompt,
        max_new_tokens=2,
        qualification=qualification_binding(record),
    )


def backend_stack(record=None):
    router, clock, capacity, runtime = _runtime_stack()
    port = RecordingRouter(router)
    codec = RecordingCodec()
    authority = CurrentAuthority(record or _synthetic_qualification())
    current = authority.record
    graph = port.current_deployment()
    for field in (
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
    ):
        object.__setattr__(current, field, getattr(graph, field))
    backend = RouterSessionBackend(
        router=port,
        codec=codec,
        clock=clock.now,
        qualification_source=authority,
    )
    return backend, authority, port, codec, capacity, runtime


def test_exact_current_qualification_streams_prompt_through_router_only():
    record = _synthetic_qualification()
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)
    emitted = []

    assert backend.run("physical-backend-ok", submission(record), lambda i, t: emitted.append((i, t)), lambda: False) == "completed"

    assert router.admit_calls == 1
    assert codec.encoded == ["PROMPT-PRIVATE-3B4"]
    assert emitted == [(0, "<101>"), (1, "<102>")]
    assert len(capacity.release_calls) == 1
    assert len(runtime.cancel_calls) == 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda authority, record, graph: object.__setattr__(record, "route_ready", False), "readiness_revoked"),
        (lambda authority, record, graph: object.__setattr__(record, "deployment_id", "wrong-deployment"), "qualification_mismatch"),
        (lambda authority, record, graph: object.__setattr__(record, "placement_provenance", "frozen_fixture" if record.placement_provenance == "planner_v2" else "planner_v2"), "qualification_mismatch"),
        (lambda authority, record, graph: object.__setattr__(record, "source_provenance_digest", "sha256:" + "f" * 64), "qualification_mismatch"),
    ],
)
def test_rejects_before_router_admission(mutate, code):
    record = _synthetic_qualification()
    backend, authority, router, codec, _capacity, _runtime = backend_stack(record)
    accepted = submission(record)
    graph = router.current_deployment()
    mutate(authority, record, graph)
    try:
        with pytest.raises(AdmissionError) as raised:
            backend.run(
                "physical-backend-reject",
                accepted,
                lambda *_: None,
                lambda: False,
            )
        assert raised.value.code == code
        assert router.admit_calls == 0
        assert codec.encoded == []
    finally:
        if authority.record is record:
            object.__setattr__(record, "route_ready", True)


def test_expired_authority_record_is_absent_and_rejects_before_prompt_encoding():
    record = _synthetic_qualification()
    backend, authority, router, codec, _capacity, _runtime = backend_stack(record)
    accepted = submission(record)
    authority.record = None

    with pytest.raises(AdmissionError) as raised:
        backend.run(
            "physical-backend-expired",
            accepted,
            lambda *_: None,
            lambda: False,
        )

    assert raised.value.code == "route_dropped"
    assert router.admit_calls == 0
    assert codec.encoded == []


def test_router_deployment_identity_must_match_accepted_record():
    record = _synthetic_qualification()
    backend, _authority, router, codec, _capacity, _runtime = backend_stack(record)
    original = router.current_deployment
    router.current_deployment = lambda: dataclasses.replace(original(), deployment_id="different")

    with pytest.raises(AdmissionError) as raised:
        backend.run("physical-backend-deployment", submission(record), lambda *_: None, lambda: False)

    assert raised.value.code == "qualification_mismatch"
    assert router.admit_calls == 0
    assert codec.encoded == []


def test_malformed_current_record_fails_closed_before_prompt_encoding():
    record = _synthetic_qualification()
    backend, authority, router, codec, _capacity, _runtime = backend_stack(record)
    accepted = submission(record)
    object.__setattr__(record, "source_provenance_digest", "not-a-digest")

    with pytest.raises(AdmissionError) as raised:
        backend.run(
            "physical-backend-malformed",
            accepted,
            lambda *_: None,
            lambda: False,
        )

    assert authority.record is record
    assert raised.value.code == "qualification_unavailable"
    assert router.admit_calls == 0
    assert codec.encoded == []


def test_post_rejection_cleanup_does_not_cancel_router_or_leave_pending_id():
    record = _synthetic_qualification()
    backend, authority, router, codec, _capacity, _runtime = backend_stack(record)
    accepted = submission(record)
    object.__setattr__(authority.record, "deployment_id", "changed-before-encoding")

    with pytest.raises(AdmissionError):
        backend.run(
            "physical-backend-rejected-cleanup",
            accepted,
            lambda *_: None,
            lambda: False,
        )
    backend.cancel("physical-backend-rejected-cleanup")

    assert router.admit_calls == 0
    assert router.cancel_calls == 0
    assert codec.encoded == []
    assert backend._pending_cancelled == set()
    assert backend._awaiting_cancel_ack == set()


class ReusableRouter:
    def __init__(self, deployment):
        self.deployment = deployment
        self.admit_calls = 0
        self.cancel_calls = 0
        self.status = "UNKNOWN"

    def current_deployment(self):
        return self.deployment

    def admit(self, request, client_sink, **kwargs):
        del client_sink, kwargs
        self.admit_calls += 1
        self.status = "DECODING"
        return request.request_id

    def decode_one(self, request_id):
        del request_id
        self.status = "COMPLETED"
        return True

    def request_status(self, request_id):
        del request_id
        return self.status

    def cancel(self, request_id):
        del request_id
        self.cancel_calls += 1
        self.status = "CANCELLED"
        return True


def test_cancel_state_is_request_local_and_does_not_poison_reused_id():
    record = _synthetic_qualification()
    _router, clock, _capacity, _runtime = _runtime_stack()
    graph = _router.entry.topology.snapshot()
    for field in (
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
    ):
        object.__setattr__(record, field, getattr(graph, field))
    router = ReusableRouter(graph)
    backend = RouterSessionBackend(
        router=router,
        codec=RecordingCodec(),
        clock=clock.now,
        qualification_source=CurrentAuthority(record),
    )
    checks = iter((False, False, True))

    assert (
        backend.run(
            "reused-request-id",
            submission(record),
            lambda *_: None,
            lambda: next(checks, True),
        )
        == "cancelled"
    )
    backend.cancel("reused-request-id")
    assert (
        backend.run(
            "reused-request-id",
            submission(record),
            lambda *_: None,
            lambda: False,
        )
        == "completed"
    )

    assert router.admit_calls == 2
    assert router.cancel_calls == 1
    assert backend._cancelled == set()
    assert backend._pending_cancelled == set()
    assert backend._internally_cancelled == set()
    assert backend._external_cancellation_observed == set()
    assert backend._awaiting_cancel_ack == set()


def test_client_cancellation_reaches_router_once_and_private_material_stays_out_of_error(caplog):
    record = _synthetic_qualification()
    backend, _authority, router, _codec, _capacity, _runtime = backend_stack(record)
    private = "PROMPT-PRIVATE-CANCEL-3B4"
    checks = iter((False, False, True))

    assert backend.run("physical-backend-cancel", submission(record, private), lambda *_: None, lambda: next(checks, True)) == "cancelled"
    backend.cancel("physical-backend-cancel")

    assert router.cancel_calls == 1
    assert backend._cancelled == set()
    assert backend._pending_cancelled == set()
    assert backend._internally_cancelled == set()
    assert backend._external_cancellation_observed == set()
    assert backend._awaiting_cancel_ack == set()
    assert private not in caplog.text
    assert private not in repr(backend.__dict__)
