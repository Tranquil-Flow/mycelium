# SPDX-License-Identifier: AGPL-3.0-only
"""A11: the browser gateway's optional speculation preference and its bootstrap advertisement."""

from __future__ import annotations

import json

import pytest

from ui_gateway_support import (
    OBSERVATORY_TOKEN,
    REQUEST_TOKEN,
    json_body,
    request,
    session_headers,
    submission,
)

from mycelium_demo.product_stack import build_loopback_product_stack
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def _v2(**extra):
    return {
        **submission(),
        "protocol": "mycelium.request_gateway.v2",
        "workload_profile_id": "interactive_chat_v1",
        "qos_class": "interactive",
        **extra,
    }


def _app(upstreams, coordinator, *, speculation_preference: bool):
    observatory, request_gateway = upstreams
    return create_product_gateway_application(
        config=GatewayConfig(speculation_preference=speculation_preference),
        observatory_app=observatory,
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
        observatory_bearer_token=OBSERVATORY_TOKEN,
    )


@pytest.mark.parametrize("advertised", [True, False])
def test_bootstrap_advertises_the_preference_only_when_configured(upstreams, coordinator, advertised) -> None:
    document = request(_app(upstreams, coordinator, speculation_preference=advertised), "/api/v1/bootstrap").json()
    if advertised:
        assert document["features"] == {"speculation_preference": True}
    else:
        assert "features" not in document


def test_config_refuses_a_non_boolean_advertisement() -> None:
    with pytest.raises(ValueError, match="invalid_speculation_preference"):
        GatewayConfig(speculation_preference="yes")


@pytest.mark.parametrize("preference", ["target_only", "speculative_requested"])
def test_a_stated_preference_is_forwarded_exactly(upstreams, coordinator, preference) -> None:
    app = _app(upstreams, coordinator, speculation_preference=True)
    boot = request(app, "/api/v1/bootstrap")
    body = _v2(speculation_preference=preference)
    response = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(body),
        headers=session_headers(boot, csrf=True) + [(b"content-type", b"application/json")],
    )
    assert response.status == 202
    _observatory, request_gateway = upstreams
    scope, forwarded = request_gateway.calls[-1]
    assert scope["path"] == "/v1/inference"
    assert json.loads(forwarded) == body


@pytest.mark.parametrize("preference", ["always", None, 1, True, ""])
def test_an_invalid_preference_is_refused_before_any_upstream_call(upstreams, coordinator, preference) -> None:
    app = _app(upstreams, coordinator, speculation_preference=True)
    boot = request(app, "/api/v1/bootstrap")
    _observatory, request_gateway = upstreams
    before = len(request_gateway.calls)
    response = request(
        app,
        "/api/v1/inference",
        method="POST",
        body=json_body(_v2(speculation_preference=preference)),
        headers=session_headers(boot, csrf=True) + [(b"content-type", b"application/json")],
    )
    assert response.status == 400
    assert response.json()["code"] == "invalid_inference_request"
    assert len(request_gateway.calls) == before


def test_product_stack_advertises_only_for_a_router_that_takes_the_preference(upstreams, coordinator) -> None:
    observatory, _request_gateway = upstreams

    class _Router:
        supports_speculation_preference = True

    class _Codec:
        pass

    for router, advertised in ((_Router(), True), (object(), False)):
        app = build_loopback_product_stack(
            observatory_app=observatory,
            qualification_source=None,
            router=router,
            codec=_Codec(),
            request_bearer_token=REQUEST_TOKEN,
            swarm_coordinator=coordinator,
        )
        document = request(app, "/api/v1/bootstrap").json()
        assert ("features" in document) is advertised
