from __future__ import annotations

import json

from conftest import (
    PROMPT_CANARY,
    REQUEST_TOKEN,
    FakeUpstream,
    default_request,
    json_body,
    qualification,
    request,
    session_headers,
    submission,
)

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def auth_values(scope) -> list[bytes]:
    return [value for name, value in scope["headers"] if name.lower() == b"authorization"]


def test_qualification_is_exact_allowlisted_request_gateway_projection(app, bootstrap, upstreams) -> None:
    result = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(bootstrap),
    )
    _observatory, request_gateway = upstreams

    assert result.status == 200
    assert result.json() == qualification(route_ready=False)
    assert result.json()["route_ready"] is False
    scope, body = request_gateway.calls[-1]
    assert scope["path"] == "/v1/qualification/current"
    assert scope["method"] == "GET"
    assert body == b""
    assert auth_values(scope) == [b"Bearer " + REQUEST_TOKEN.encode("ascii")]
    assert REQUEST_TOKEN.encode() not in result.body


def test_invalid_positive_qualification_is_not_reconstructed_or_forwarded(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams
    invalid = qualification(route_ready=True)
    invalid["evidence_class"] = "synthetic_test_fixture"

    async def responder(_scope, _body):
        return 200, [(b"content-type", b"application/json")], [json_body(invalid)]

    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=FakeUpstream(responder),
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="private-request-bearer",
    )
    boot = request(app, "/api/v1/bootstrap")
    result = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(boot),
    )

    assert result.status == 502
    assert result.json()["code"] == "invalid_upstream_qualification"
    assert b"route_ready" not in result.body


def test_inference_submit_adapts_only_paths_and_binds_request_to_session(
    app, bootstrap, upstreams
) -> None:
    response = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(submission()),
        headers=session_headers(bootstrap, csrf=True)
        + [(b"content-type", b"application/json")],
    )
    _observatory, request_gateway = upstreams

    assert response.status == 202
    assert response.json() == {
        "protocol": "mycelium.request_gateway.v1",
        "request_id": "request-1",
        "accepted": True,
        "event_path": "/api/v1/inference/request-1/events",
        "cancel_path": "/api/v1/inference/request-1/cancel",
    }
    assert PROMPT_CANARY.encode() not in response.body
    scope, body = request_gateway.calls[-1]
    assert scope["path"] == "/v1/inference"
    assert scope["method"] == "POST"
    assert json.loads(body) == submission()
    assert auth_values(scope) == [b"Bearer " + REQUEST_TOKEN.encode("ascii")]

    other_bootstrap = request(app, "/api/v1/bootstrap")
    denied = request(
        app,
        "/api/v1/inference/request-1/events",
        headers=session_headers(other_bootstrap),
    )
    assert denied.status == 404
    assert denied.json()["code"] == "request_not_found"


def test_inference_body_content_type_duplicate_fields_and_size_are_bounded(
    app, bootstrap, upstreams
) -> None:
    _observatory, request_gateway = upstreams
    before = len(request_gateway.calls)
    headers = session_headers(bootstrap, csrf=True)
    wrong_type = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(submission()),
        headers=headers + [(b"content-type", b"text/plain")],
    )
    duplicate = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=b'{"protocol":"mycelium.request_gateway.v1","protocol":"mycelium.request_gateway.v1"}',
        headers=headers + [(b"content-type", b"application/json")],
    )
    oversized = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=b"x" * 262_145,
        headers=headers
        + [
            (b"content-type", b"application/json"),
            (b"content-length", b"262145"),
        ],
    )

    assert wrong_type.status == 415
    assert duplicate.status == 400
    assert duplicate.json()["code"] == "invalid_json_body"
    assert oversized.status == 413
    assert oversized.json()["code"] == "request_body_too_large"
    assert len(request_gateway.calls) == before


def _submit(app, bootstrap) -> None:
    response = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(submission()),
        headers=session_headers(bootstrap, csrf=True)
        + [(b"content-type", b"application/json")],
    )
    assert response.status == 202


def test_inference_stream_forwards_resume_and_validated_events(app, bootstrap, upstreams) -> None:
    _submit(app, bootstrap)
    response = request(
        app,
        "/api/v1/inference/request-1/events",
        headers=session_headers(bootstrap) + [(b"last-event-id", b"1:0")],
    )
    _observatory, request_gateway = upstreams

    assert response.status == 200
    assert response.header("content-type") == "text/event-stream; charset=utf-8"
    assert b"event: accepted" in response.body
    assert b"event: token" in response.body
    assert b'"text":"hello"' in response.body
    assert b"event: completed" in response.body
    scope, _body = request_gateway.calls[-1]
    assert scope["path"] == "/v1/inference/request-1/events"
    assert [value for name, value in scope["headers"] if name.lower() == b"last-event-id"] == [
        b"1:0"
    ]


def test_inference_stream_rejects_request_id_mismatch(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams

    async def mismatch(scope, body):
        if scope["path"] == "/v1/inference/request-1/events":
            event = {
                "protocol": "mycelium.request_event.v1",
                "request_id": "other-request",
                "sequence": 0,
                "publisher_generation": 1,
                "type": "accepted",
            }
            frame = b"id: 1:0\nevent: accepted\ndata: " + json_body(event) + b"\n\n"
            return 200, [(b"content-type", b"text/event-stream; charset=utf-8")], [frame]
        return await default_request(scope, body)

    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=FakeUpstream(mismatch),
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="private-request-bearer",
    )
    boot = request(app, "/api/v1/bootstrap")
    _submit(app, boot)
    result = request(
        app,
        "/api/v1/inference/request-1/events",
        headers=session_headers(boot),
    )
    assert result.status == 200
    assert result.body == b""


def test_inference_cancel_uses_frozen_same_origin_cancel_route(app, bootstrap, upstreams) -> None:
    _submit(app, bootstrap)
    response = request(
        app,
        "/api/v1/inference/request-1/cancel",
        method="DELETE",
        headers=session_headers(bootstrap, csrf=True),
    )
    _observatory, request_gateway = upstreams

    assert response.status == 202
    assert response.json() == {
        "protocol": "mycelium.request_gateway.v1",
        "request_id": "request-1",
        "cancelled": True,
    }
    scope, body = request_gateway.calls[-1]
    assert scope["path"] == "/v1/inference/request-1"
    assert scope["method"] == "DELETE"
    assert body == b""


def test_per_session_request_history_is_bounded(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams
    request_number = [0]

    async def different_ids(scope, body):
        if scope["path"] == "/v1/inference" and scope["method"] == "POST":
            request_number[0] += 1
            request_id = f"request-{request_number[0]}"
            return (
                202,
                [(b"content-type", b"application/json")],
                [
                    json_body(
                        {
                            "request_id": request_id,
                            "stream_path": f"/v1/inference/{request_id}/events",
                            "cancel_path": f"/v1/inference/{request_id}",
                            "session_token": (
                                "session-owner-token-0000000000000002"
                            ),
                        }
                    )
                ],
            )
        return await default_request(scope, body)

    upstream = FakeUpstream(different_ids)
    app = create_product_gateway_application(
        config=GatewayConfig(max_requests_per_session=1),
        observatory_app=observatory,
        request_gateway_app=upstream,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="private-request-bearer",
    )
    boot = request(app, "/api/v1/bootstrap")
    _submit(app, boot)
    second = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(submission()),
        headers=session_headers(boot, csrf=True)
        + [(b"content-type", b"application/json")],
    )

    assert second.status == 429
    assert second.json()["code"] == "request_capacity"
    assert request_number[0] == 1
