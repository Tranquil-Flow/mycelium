from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from conftest import (
    ERROR_CANARY,
    REQUEST_TOKEN,
    FakeUpstream,
    json_body,
    observatory_snapshot,
    request,
    session_headers,
)

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from mycelium_ui_gateway.validation import (
    GatewayValidationError,
    validate_observatory_envelope,
    validate_swarm_status,
)


def _ready_observatory() -> dict:
    payload = observatory_snapshot()
    binding = {
        "qualification_id": "qualification-1",
        "qualification_digest": "sha256:" + "a" * 64,
        "deployment_id": "deployment-1",
        "deployment_epoch": 1,
        "topology_version": 2,
        "model_id": "model-1",
        "resolved_commit": "revision-1",
        "manifest_digest": "sha256:" + "b" * 64,
        "path_manifest_digest": "sha256:" + "c" * 64,
        "stage_load_proof_digests": ["sha256:" + "d" * 64],
    }
    payload["bundle"]["snapshot"]["qualification"] = {
        "protocol": "mycelium.route_qualification.v1",
        "qualification_id": "qualification-1",
        "issued_at_unix_ms": 1_000,
        "evidence_class": "physical_qualification",
        "route_ready": True,
        "reason_codes": [],
        "binding": binding,
    }
    payload["bundle"]["provisioning"]["route_ready"] = True
    return payload


@pytest.mark.parametrize(
    ("section", "field"),
    (("snapshot", "user_prompt"), ("provisioning", "private_endpoint")),
)
def test_observatory_projection_rejects_unmodelled_nested_private_fields(
    section: str, field: str
) -> None:
    payload = observatory_snapshot()
    payload["bundle"][section][field] = "must-never-cross-product-gateway"

    with pytest.raises(GatewayValidationError) as raised:
        validate_observatory_envelope(payload)
    assert raised.value.code == "invalid_observatory_response"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload["bundle"]["snapshot"]["qualification"]["binding"].update(
            stage_load_proof_digests=[]
        ),
        lambda payload: payload["bundle"]["snapshot"].update(
            protocol="mycelium.observatory.request_projection.v2"
        ),
        lambda payload: payload["bundle"]["provisioning"].update(buffered_sessions="0"),
        lambda payload: payload["bundle"]["snapshot"]["qualification"]["binding"].update(
            deployment_id="10.0.0.1"
        ),
    ),
)
def test_observatory_projection_rejects_unbacked_mistyped_or_private_state(mutate) -> None:
    payload = _ready_observatory()
    mutate(payload)

    with pytest.raises(GatewayValidationError) as raised:
        validate_observatory_envelope(payload)
    assert raised.value.code == "invalid_observatory_response"


def test_swarm_projection_rejects_private_network_identifiers() -> None:
    payload = {
        "protocol": "mycelium.product_ui.swarm.v1",
        "native_nodes": [
            {
                "member_id": "10.0.0.1",
                "capability": "native_inference_node",
                "membership_state": "trusted",
                "connectivity": "local",
                "endpoint_id": None,
            }
        ],
        "browser_workers": [],
    }
    with pytest.raises(GatewayValidationError):
        validate_swarm_status(payload)


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
