# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json

import pytest

from ui_gateway_support import (
    FakeUpstream,
    default_request,
    json_body,
    request as asgi_request,
    session_headers,
    submission,
)

from mycelium_request_gateway.contracts import (
    REQUEST_EVENT_PROTOCOL,
    REQUEST_EVENT_PROTOCOL_V2,
    StreamEvent,
)
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from mycelium_ui_gateway.validation import (
    GatewayValidationError,
    MAX_SAFE_INTEGER,
    validate_stream_event,
)


REQUEST_ID = "request-fixture"


def _producer_token_with_id(request_id: str = REQUEST_ID, sequence: int = 6) -> dict[str, object]:
    return StreamEvent(
        request_id=request_id,
        sequence=sequence,
        kind="token",
        token_index=0,
        text="Hallo",
        token_id=9707,
        publisher_generation=1,
        protocol=REQUEST_EVENT_PROTOCOL_V2,
    ).to_dict()


def _producer_frame(event: dict[str, object]) -> bytes:
    return (
        f"id: {event['publisher_generation']}:{event['sequence']}\n"
        f"event: {event['type']}\n"
        "data: "
    ).encode("ascii") + json.dumps(
        event, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n\n"


def _rejects(document: dict[str, object], expected_id: str = REQUEST_ID) -> None:
    with pytest.raises(GatewayValidationError) as exc_info:
        validate_stream_event(document, expected_id)
    assert exc_info.value.code == "invalid_upstream_event"


def test_validate_stream_event_accepts_producer_v2_token_id_and_keeps_strict_controls() -> None:
    event = _producer_token_with_id()
    assert validate_stream_event(event, REQUEST_ID) == (6, "token")

    v2_without_id = StreamEvent(
        request_id=REQUEST_ID,
        sequence=7,
        kind="token",
        token_index=1,
        text="ohne-id",
        publisher_generation=1,
        protocol=REQUEST_EVENT_PROTOCOL_V2,
    ).to_dict()
    assert "token_id" not in v2_without_id
    assert validate_stream_event(v2_without_id, REQUEST_ID) == (7, "token")

    v1_legacy = StreamEvent(
        request_id=REQUEST_ID,
        sequence=8,
        kind="token",
        token_index=2,
        text="legacy",
        publisher_generation=1,
        protocol=REQUEST_EVENT_PROTOCOL,
    ).to_dict()
    assert "token_id" not in v1_legacy
    assert validate_stream_event(v1_legacy, REQUEST_ID) == (8, "token")

    lifecycle = StreamEvent(
        request_id=REQUEST_ID,
        sequence=9,
        kind="lifecycle",
        phase="prefill",
        publisher_generation=1,
        protocol=REQUEST_EVENT_PROTOCOL_V2,
    ).to_dict()
    invalid_documents = [
        {**event, "protocol": REQUEST_EVENT_PROTOCOL},
        {**event, "token_id": True},
        {**event, "token_id": -1},
        {**event, "token_id": MAX_SAFE_INTEGER + 1},
        {**event, "unexpected": "field"},
        {**lifecycle, "token_id": 9707},
    ]
    for invalid in invalid_documents:
        _rejects(invalid)


def test_inference_stream_forwards_producer_v2_token_id_through_validated_sse(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams

    async def v2_token_stream(scope, body):
        if scope["path"] == "/v1/inference/request-1/events":
            events = [
                StreamEvent(
                    request_id="request-1",
                    sequence=0,
                    kind="accepted",
                    publisher_generation=1,
                    protocol=REQUEST_EVENT_PROTOCOL_V2,
                ).to_dict(),
                _producer_token_with_id(request_id="request-1", sequence=1),
                StreamEvent(
                    request_id="request-1",
                    sequence=2,
                    kind="completed",
                    publisher_generation=1,
                    protocol=REQUEST_EVENT_PROTOCOL_V2,
                ).to_dict(),
            ]
            return (
                200,
                [(b"content-type", b"text/event-stream; charset=utf-8")],
                [_producer_frame(event) for event in events],
            )
        return await default_request(scope, body)

    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=FakeUpstream(v2_token_stream),
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="private-request-bearer",
    )
    bootstrap = asgi_request(app, "/api/v1/bootstrap")
    accepted = asgi_request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(submission()),
        headers=session_headers(bootstrap, csrf=True)
        + [(b"content-type", b"application/json")],
    )
    assert accepted.status == 202

    result = asgi_request(
        app,
        "/api/v1/inference/request-1/events",
        headers=session_headers(bootstrap),
    )

    assert result.status == 200
    assert b"event: accepted" in result.body
    assert b"event: token" in result.body
    assert b'"token_id":9707' in result.body
    assert b'"text":"Hallo"' in result.body
    assert b"event: completed" in result.body
