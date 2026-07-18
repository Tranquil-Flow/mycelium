"""Default observability privacy tests; local fixtures only."""
from __future__ import annotations

import json
import logging

import pytest

from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    StreamEvent,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification
from test_stream import ScriptedBackend, _drain


def test_default_logs_and_metrics_never_contain_private_inference_material(caplog):
    qualification = _synthetic_qualification()
    prompt = "PROMPT-PRIVATE activation=ACT-PRIVATE kv=KV-PRIVATE endpoint=10.9.8.7"
    token_text = "TOKEN-CONTENT-PRIVATE"
    token_id_marker = "TOKEN-ID-424242"
    credential = "BEARER-CREDENTIAL-PRIVATE"
    backend = ScriptedBackend((token_text,))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "privacy-001",
        max_buffered_events=4,
    )
    caplog.set_level(logging.DEBUG, logger="mycelium.request_gateway")
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt=prompt,
                max_new_tokens=1,
                qualification=qualification_binding(qualification),
            )
        )
        _drain(service.subscribe(request_id, last_event_id=None))
        snapshot = service.metrics_snapshot()
        observable = caplog.text + json.dumps(snapshot, sort_keys=True)

        for private_value in (
            prompt,
            token_text,
            token_id_marker,
            "ACT-PRIVATE",
            "KV-PRIVATE",
            "10.9.8.7",
            credential,
        ):
            assert private_value not in observable
        assert snapshot["requests_admitted_total"] == 1
        assert snapshot["token_events_total"] == 1
        assert snapshot["requests_completed_total"] == 1
        assert all(isinstance(value, int) for value in snapshot.values())
        assert all(
            forbidden not in key
            for key in snapshot
            for forbidden in ("prompt", "token_id", "token_content", "activation", "kv", "credential", "endpoint")
        )
    finally:
        service.close()


def test_backend_exception_detail_is_not_logged_or_returned(caplog):
    qualification = _synthetic_qualification()
    secret = "RAW-BACKEND-PRIVATE-DETAIL"

    class SecretFailureBackend:
        def run(self, request_id, submission, emit_token, is_cancelled):
            raise RuntimeError(secret)

        def cancel(self, request_id):
            return None

    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=SecretFailureBackend(),
        request_id_source=lambda: "privacy-failure-001",
        max_buffered_events=4,
    )
    caplog.set_level(logging.DEBUG, logger="mycelium.request_gateway")
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="private",
                max_new_tokens=1,
                qualification=qualification_binding(qualification),
            )
        )
        events = _drain(service.subscribe(request_id, last_event_id=None))

        assert events[-1].kind == "failed"
        assert events[-1].code == "backend_failed"
        assert secret not in caplog.text
        assert secret not in json.dumps(events[-1].to_dict())
    finally:
        service.close()


def test_wire_error_codes_cannot_embed_private_endpoint_details():
    with pytest.raises(ValueError, match="invalid_admission_error_code"):
        AdmissionError("endpoint_unreachable:10.9.8.7")
    with pytest.raises(AdmissionError) as invalid_event:
        StreamEvent(
            request_id="privacy-error-001",
            sequence=0,
            kind="failed",
            code="endpoint_unreachable:10.9.8.7",
        )
    assert invalid_event.value.code == "invalid_stream_event"
