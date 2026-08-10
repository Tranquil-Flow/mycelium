from __future__ import annotations

import asyncio

import pytest

from conftest import (
    ASGIResult,
    REQUEST_TOKEN,
    cookie_pair,
    request,
    session_headers,
)

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def assert_private_headers(result: ASGIResult) -> None:
    assert result.header("cache-control") == "no-store"
    assert result.header("access-control-allow-origin") is None
    assert result.header("access-control-allow-credentials") is None


def test_configuration_defaults_to_loopback_and_external_mode_fails_closed(upstreams, coordinator) -> None:
    observatory, request_gateway = upstreams
    config = GatewayConfig()
    assert config.bind_host == "127.0.0.1"
    assert config.is_loopback

    with pytest.raises(ValueError, match="non_loopback_requires_tls"):
        GatewayConfig(bind_host="0.0.0.0")
    with pytest.raises(ValueError, match="non_loopback_requires_https_origin"):
        GatewayConfig(bind_host="0.0.0.0", tls_enabled=True)

    external = GatewayConfig(
        bind_host="0.0.0.0",
        tls_enabled=True,
        public_origin="https://mycelium.example.test",
    )
    with pytest.raises(ValueError, match="non_loopback_requires_access_policy"):
        create_product_gateway_application(
            config=external,
            observatory_app=observatory,
            request_gateway_app=request_gateway,
            swarm_coordinator=coordinator,
            request_gateway_bearer_token=REQUEST_TOKEN,
        )


def test_bootstrap_creates_bounded_http_only_strict_session(app) -> None:
    result = request(app, "/api/v1/bootstrap")

    assert result.status == 200
    assert_private_headers(result)
    cookie = result.header("set-cookie")
    assert cookie is not None
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie
    assert "Secure" not in cookie
    payload = result.json()
    assert payload == {
        "protocol": "mycelium.product_ui.bootstrap.v1",
        "source_mode": "live",
        "session": {
            "csrf_header": "X-Mycelium-CSRF",
            "csrf_token": payload["session"]["csrf_token"],
            "expires_at_unix_ms": payload["session"]["expires_at_unix_ms"],
        },
        "api": {
            "observatory_snapshot": "/api/v1/observatory/snapshot",
            "observatory_events": "/api/v1/observatory/events",
            "qualification_current": "/api/v1/qualification/current",
            "inference_submit": "/api/v1/inference",
            "swarm_status": "/api/v1/swarm/status",
            "swarm_invites": "/api/v1/swarm/invites",
            "swarm_join": "/api/v1/swarm/join",
                "swarm_leave": "/api/v1/swarm/leave",
                "product_snapshot": "/api/v1/product/snapshot",
                "product_events": "/api/v1/product/events",
                "product_export": "/api/v1/product/export",
            },
        "limits": {"max_prompt_utf8_bytes": 131_072, "max_new_tokens": 4_096},
        "qualification_authority": "mycelium_qualification.qualifier:RouteQualificationV1",
    }
    serialized = result.body.decode("utf-8") + cookie
    assert REQUEST_TOKEN not in serialized
    assert "Bearer " not in serialized


def test_bootstrap_reuses_valid_session_and_capacity_does_not_evict_live_session(upstreams, coordinator) -> None:
    observatory, request_gateway = upstreams
    now = [1_000.0]
    app = create_product_gateway_application(
        config=GatewayConfig(max_sessions=1, session_ttl_seconds=10),
        observatory_app=observatory,
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
        clock=lambda: now[0],
    )
    first = request(app, "/api/v1/bootstrap")
    reused = request(
        app,
        "/api/v1/bootstrap",
        headers=[(b"cookie", cookie_pair(first))],
    )
    denied = request(app, "/api/v1/bootstrap")

    assert reused.status == 200
    assert cookie_pair(reused) == cookie_pair(first)
    assert reused.json()["session"] == first.json()["session"]
    assert denied.status == 503
    assert denied.json()["code"] == "session_capacity"

    now[0] += 11
    replaced = request(app, "/api/v1/bootstrap")
    assert replaced.status == 200
    assert cookie_pair(replaced) != cookie_pair(first)


def test_loopback_mode_rejects_remote_clients_and_host_rebinding(app) -> None:
    remote = request(app, "/api/v1/bootstrap", client=("203.0.113.9", 4100))
    rebound = request(app, "/api/v1/bootstrap", server=("example.test", 8080))

    assert remote.status == 403
    assert remote.json()["code"] == "loopback_required"
    assert rebound.status == 403
    assert rebound.json()["code"] == "invalid_origin"


def test_non_loopback_bootstrap_requires_injected_access_policy(upstreams, coordinator) -> None:
    observatory, request_gateway = upstreams
    decisions: list[str] = []

    def policy(scope):
        decisions.append(scope["path"])
        return any(
            name.lower() == b"authorization" and value == b"Product local-access"
            for name, value in scope["headers"]
        )

    app = create_product_gateway_application(
        config=GatewayConfig(
            bind_host="0.0.0.0",
            tls_enabled=True,
            public_origin="https://mycelium.example.test",
        ),
        observatory_app=observatory,
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
        access_policy=policy,
    )
    denied = request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        server=("mycelium.example.test", 443),
        client=("203.0.113.9", 4100),
    )
    accepted = request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        server=("mycelium.example.test", 443),
        client=("203.0.113.9", 4100),
        headers=[(b"authorization", b"Product local-access")],
    )

    assert denied.status == 403
    assert accepted.status == 200
    assert "Secure" in (accepted.header("set-cookie") or "")
    assert decisions == ["/api/v1/bootstrap", "/api/v1/bootstrap"]


def test_private_endpoints_require_session_and_state_changes_require_csrf(app, bootstrap) -> None:
    no_session = request(app, "/api/v1/qualification/current")
    no_csrf = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=b"{}",
        headers=session_headers(bootstrap),
    )
    wrong_origin = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=b"{}",
        headers=session_headers(bootstrap, csrf=True, origin=b"https://attacker.invalid"),
    )

    assert no_session.status == 401
    assert no_session.json()["code"] == "session_required"
    assert no_csrf.status == 403
    assert no_csrf.json()["code"] == "csrf_required"
    assert wrong_origin.status == 403
    assert wrong_origin.json()["code"] == "origin_mismatch"
    for result in (no_session, no_csrf, wrong_origin):
        assert_private_headers(result)


def test_duplicate_cookie_or_csrf_headers_fail_closed(app, bootstrap) -> None:
    duplicate_cookie = request(
        app,
        "/api/v1/qualification/current",
        headers=[
            (b"cookie", cookie_pair(bootstrap)),
            (b"cookie", cookie_pair(bootstrap)),
        ],
    )
    headers = session_headers(bootstrap, csrf=True)
    duplicate_csrf = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=b"{}",
        headers=headers + [(b"x-mycelium-csrf", b"another-token")],
    )

    assert duplicate_cookie.status == 401
    assert duplicate_csrf.status == 403


def test_queries_websockets_unknown_routes_and_methods_fail_closed(app, bootstrap) -> None:
    query = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(bootstrap),
        query_string=b"token=must-not-be-accepted",
    )
    wrong_method = request(app, "/api/v1/bootstrap", method="POST")
    missing = request(app, "/api/v1/not-real")

    assert query.status == 400
    assert query.json()["code"] == "query_not_allowed"
    assert wrong_method.status == 405
    assert wrong_method.header("allow") == "GET"
    assert missing.status == 404

    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "websocket", "path": "/api/v1/bootstrap"}, receive, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_all_json_and_stream_responses_are_no_store(app, bootstrap) -> None:
    for path in (
        "/api/v1/bootstrap",
        "/api/v1/observatory/snapshot",
        "/api/v1/observatory/events",
        "/api/v1/qualification/current",
        "/api/v1/swarm/status",
    ):
        headers = [] if path.endswith("bootstrap") else session_headers(bootstrap)
        result = request(app, path, headers=headers)
        assert result.header("cache-control") == "no-store"
        assert result.header("access-control-allow-origin") is None
