from __future__ import annotations

import json

from conftest import (
    OBSERVATORY_TOKEN,
    FakeUpstream,
    json_body,
    observatory_snapshot,
    request,
    session_headers,
)

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def header_values(scope, wanted: bytes) -> list[bytes]:
    return [value for name, value in scope["headers"] if name.lower() == wanted]


def test_observatory_snapshot_is_bounded_get_composition(app, bootstrap, upstreams) -> None:
    result = request(
        app,
        "/api/v1/observatory/snapshot",
        headers=session_headers(bootstrap) + [(b"authorization", b"Browser credential")],
    )
    observatory, _request_gateway = upstreams

    assert result.status == 200
    assert result.json() == observatory_snapshot()
    assert result.header("cache-control") == "no-store"
    assert result.header("x-mycelium-source-status") == "connected"
    scope, body = observatory.calls[-1]
    assert scope["method"] == "GET"
    assert scope["path"] == "/v1/observatory/snapshot"
    assert scope["query_string"] == b""
    assert body == b""
    assert header_values(scope, b"authorization") == [
        b"Bearer " + OBSERVATORY_TOKEN.encode("ascii")
    ]
    assert b"Browser credential" not in b"\n".join(value for _name, value in scope["headers"])


def test_observatory_response_bound_and_credential_leak_fail_closed(upstreams, coordinator) -> None:
    _old_observatory, request_gateway = upstreams

    async def oversized(_scope, _body):
        return 200, [(b"content-type", b"application/json")], [b"{" + b"x" * 1024 + b"}"]

    too_large = create_product_gateway_application(
        config=GatewayConfig(max_json_response_bytes=256),
        observatory_app=FakeUpstream(oversized),
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="request-token-not-in-response",
    )
    boot = request(too_large, "/api/v1/bootstrap")
    response = request(
        too_large,
        "/api/v1/observatory/snapshot",
        headers=session_headers(boot),
    )
    assert response.status == 502
    assert response.json()["code"] == "upstream_response_too_large"
    assert b"x" * 100 not in response.body

    bearer = "observatory-private-bearer-unique"

    async def leaking(_scope, _body):
        payload = observatory_snapshot()
        payload["bundle"]["snapshot"]["leak"] = bearer
        return 200, [(b"content-type", b"application/json")], [json_body(payload)]

    leaking_app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=FakeUpstream(leaking),
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="request-token-not-in-response",
        observatory_bearer_token=bearer,
    )
    boot = request(leaking_app, "/api/v1/bootstrap")
    response = request(
        leaking_app,
        "/api/v1/observatory/snapshot",
        headers=session_headers(boot),
    )
    assert response.status == 502
    assert response.json()["code"] == "credential_leak_blocked"
    assert bearer.encode() not in response.body


def test_observatory_sse_forwards_resume_cursor_and_validates_frames(app, bootstrap, upstreams) -> None:
    observatory, _request_gateway = upstreams

    async def events(scope, _body):
        if scope["path"] != "/v1/observatory/events":
            return 404, [(b"content-type", b"application/json")], [json_body({"error": "not_found"})]
        payload = json.dumps(observatory_snapshot(generation=8), separators=(",", ":"), sort_keys=True)
        frame = f"id: 8\nevent: snapshot\ndata: {payload}\n\n".encode()
        return (
            200,
            [(b"content-type", b"text/event-stream; charset=utf-8")],
            [b": heartbeat\n\n", frame[:30], frame[30:]],
        )

    observatory.responder = events
    result = request(
        app,
        "/api/v1/observatory/events",
        headers=session_headers(bootstrap) + [(b"last-event-id", b"7")],
    )

    assert result.status == 200
    assert result.header("content-type") == "text/event-stream; charset=utf-8"
    assert result.header("x-accel-buffering") == "no"
    assert result.header("cache-control") == "no-store"
    assert b": heartbeat\n\n" in result.body
    assert b"id: 8\nevent: snapshot\n" in result.body
    scope, _body = observatory.calls[-1]
    assert header_values(scope, b"last-event-id") == [b"7"]
    assert header_values(scope, b"authorization") == [
        b"Bearer " + OBSERVATORY_TOKEN.encode("ascii")
    ]


def test_observatory_stream_rejects_invalid_cursor_before_upstream(app, bootstrap, upstreams) -> None:
    observatory, _request_gateway = upstreams
    before = len(observatory.calls)
    result = request(
        app,
        "/api/v1/observatory/events",
        headers=session_headers(bootstrap)
        + [(b"last-event-id", b"7"), (b"last-event-id", b"8")],
    )

    assert result.status == 400
    assert result.json()["code"] == "invalid_last_event_id"
    assert len(observatory.calls) == before


def test_oversized_or_malformed_observatory_sse_frame_closes_without_echo(upstreams, coordinator) -> None:
    _observatory, request_gateway = upstreams

    async def malformed(_scope, _body):
        return (
            200,
            [(b"content-type", b"text/event-stream; charset=utf-8")],
            [b"data: " + b"private-canary-" * 32 + b"\n\n"],
        )

    app = create_product_gateway_application(
        config=GatewayConfig(max_sse_frame_bytes=64),
        observatory_app=FakeUpstream(malformed),
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token="request-token-not-in-response",
    )
    boot = request(app, "/api/v1/bootstrap")
    result = request(app, "/api/v1/observatory/events", headers=session_headers(boot))

    assert result.status == 200
    assert result.body == b""
    assert b"private-canary" not in result.body
