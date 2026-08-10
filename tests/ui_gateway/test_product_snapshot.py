from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    REQUEST_TOKEN,
    request,
    session_headers,
)

from mycelium_product_spine import ProductEvidenceApplication, ProductProjector
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


ROOT = Path(__file__).resolve().parents[2]


def test_product_snapshot_is_validated_and_proxied_same_origin(
    upstreams,
    coordinator,
) -> None:
    observatory, request_gateway = upstreams
    route = json.loads(
        (ROOT / "contracts/compatibility-fixtures/live-route-status-v1.json").read_text()
    )
    qualification = json.loads(
        (ROOT / "contracts/compatibility-fixtures/route-qualification-v1.json").read_text()
    )
    members = [
        {
            "node_id": node_id,
            "generation": 1,
            "incarnation": "incarnation-1",
            "lease_expires_at": 2_000.0,
            "last_liveness_at": 1_000.0,
            "lifecycle_state": "RUNNING",
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            "activation_eligible": True,
        }
        for node_id in ("node-a", "node-b")
    ]
    product_app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"g" * 32),
        membership_source=lambda: members,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        clock_unix_ms=lambda: 1_500_000,
    )
    app = create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=request_gateway,
        product_app=product_app,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
    )
    bootstrap = request(app, "/api/v1/bootstrap")

    result = request(
        app,
        "/api/v1/product/snapshot",
        headers=session_headers(bootstrap),
    )

    assert result.status == 200
    assert result.json()["protocol"] == "mycelium.product_snapshot.v1"
    assert result.header("cache-control") == "no-store"

    exported = request(
        app,
        "/api/v1/product/export",
        headers=session_headers(bootstrap),
    )
    assert exported.status == 200
    assert exported.json()["protocol"] == "mycelium.product_snapshot.v1"

    events = request(
        app,
        "/api/v1/product/events",
        headers=session_headers(bootstrap),
    )
    assert events.status == 200
    assert events.header("content-type") == "text/event-stream; charset=utf-8"
    assert b"event: product_snapshot\n" in events.body
    assert b'"protocol":"mycelium.product_event.v1"' in events.body


def test_product_snapshot_fails_closed_when_source_is_not_composed(app, bootstrap) -> None:
    result = request(
        app,
        "/api/v1/product/snapshot",
        headers=session_headers(bootstrap),
    )

    assert result.status == 503
    assert result.json() == {
        "protocol": "mycelium.product_ui.error.v1",
        "code": "product_snapshot_unavailable",
        "retryable": True,
    }
