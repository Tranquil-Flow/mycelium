from __future__ import annotations

import inspect
import json
from pathlib import Path

from conftest import (
    ERROR_CANARY,
    REQUEST_TOKEN,
    FakeUpstream,
    json_body,
    request,
    session_headers,
)

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def test_upstream_exception_and_error_bodies_never_echo_private_material(
    upstreams, coordinator, caplog
) -> None:
    observatory, _request_gateway = upstreams

    async def explode(_scope, _body):
        raise RuntimeError(ERROR_CANARY + " " + REQUEST_TOKEN)

    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=FakeUpstream(explode),
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
    )
    boot = request(app, "/api/v1/bootstrap")
    result = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(boot),
    )

    assert result.status == 502
    assert result.json() == {
        "protocol": "mycelium.product_ui.error.v1",
        "code": "upstream_unavailable",
        "retryable": True,
    }
    surface = result.body.decode() + caplog.text
    assert ERROR_CANARY not in surface
    assert REQUEST_TOKEN not in surface


def test_upstream_error_is_reduced_to_a_stable_public_code(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams

    async def error(_scope, _body):
        return (
            409,
            [(b"content-type", b"application/json")],
            [json_body({"error": "path_changed", "private": ERROR_CANARY})],
        )

    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=FakeUpstream(error),
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
    )
    boot = request(app, "/api/v1/bootstrap")
    result = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(boot),
    )

    assert result.status == 409
    assert result.json()["code"] == "path_changed"
    assert ERROR_CANARY.encode() not in result.body


def test_browser_authorization_cookie_csrf_and_upstream_tokens_are_never_forwarded_together(
    app, bootstrap, upstreams
) -> None:
    browser_bearer = b"Bearer browser-only-product-auth"
    result = request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers(bootstrap) + [(b"authorization", browser_bearer)],
    )
    _observatory, request_gateway = upstreams
    scope, _body = request_gateway.calls[-1]
    serialized_headers = b"\n".join(name + b":" + value for name, value in scope["headers"])

    assert result.status == 200
    assert browser_bearer not in serialized_headers
    cookie = bootstrap.header("set-cookie")
    assert cookie is not None
    assert cookie.split(";", 1)[0].encode() not in serialized_headers
    assert bootstrap.json()["session"]["csrf_token"].encode() not in serialized_headers
    assert REQUEST_TOKEN.encode() not in result.body


def test_owned_source_has_no_forbidden_backend_or_ui_imports_and_no_readiness_promotion() -> None:
    import mycelium_ui_gateway

    package = Path(inspect.getfile(mycelium_ui_gateway)).parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    lowered = source.lower()

    for forbidden_import in (
        "mycelium_gateway.event_adapter",
        "mycelium_router",
        "mycelium_qualification.qualifier",
        "mycelium_iroh",
    ):
        assert forbidden_import not in source
    assert "route_ready=true" not in lowered.replace(" ", "")
    assert "route_ready = true" not in lowered
    assert "tailscale" not in lowered


def test_error_documents_are_small_closed_and_no_store(app, bootstrap) -> None:
    result = request(
        app,
        "/api/v1/inference/unknown/events",
        headers=session_headers(bootstrap),
    )

    assert result.status == 404
    assert set(result.json()) == {"protocol", "code", "retryable"}
    assert len(result.body) < 256
    assert result.header("cache-control") == "no-store"
    assert result.header("access-control-allow-origin") is None
    assert "traceback" not in result.body.decode().lower()
