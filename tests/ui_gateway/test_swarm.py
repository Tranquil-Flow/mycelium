from __future__ import annotations

import json

from conftest import json_body, request, session_headers


def post(app, bootstrap, path: str, document: dict):
    return request(
        app,
        path,
        method="POST",
        body=json_body(document),
        headers=session_headers(bootstrap, csrf=True)
        + [(b"content-type", b"application/json")],
    )


def test_swarm_status_uses_injected_coordinator_and_preserves_capability_boundary(
    app, bootstrap, coordinator
) -> None:
    result = request(app, "/api/v1/swarm/status", headers=session_headers(bootstrap))

    assert result.status == 200
    assert result.json()["protocol"] == "mycelium.product_ui.swarm.v1"
    node = result.json()["native_nodes"][0]
    assert node["capability"] == "native_inference_node"
    assert node["membership_state"] == "reachable"
    assert "route_ready" not in json.dumps(result.json())
    assert coordinator.calls == [("status", None)]


def test_swarm_invite_join_and_leave_are_csrf_protected_strict_adapters(
    app, bootstrap, coordinator
) -> None:
    invite_request = {
        "protocol": "mycelium.product_ui.swarm.v1",
        "action": "create_invite",
        "capability": "native_inference_node",
        "expires_in_seconds": 60,
    }
    invite = post(app, bootstrap, "/api/v1/swarm/invites", invite_request)
    join_request = {
        "protocol": "mycelium.product_ui.swarm.v1",
        "action": "join",
        "invite_code": invite.json()["invite_code"],
        "display_name": "Local node",
    }
    joined = post(app, bootstrap, "/api/v1/swarm/join", join_request)
    leave_request = {
        "protocol": "mycelium.product_ui.swarm.v1",
        "action": "leave",
        "member_id": joined.json()["member_id"],
    }
    left = post(app, bootstrap, "/api/v1/swarm/leave", leave_request)

    assert invite.status == 201
    assert invite.json()["capability"] == "native_inference_node"
    assert joined.status == 200
    assert joined.json()["joined"] is True
    assert left.status == 200
    assert left.json() == {
        "protocol": "mycelium.product_ui.swarm.v1",
        "member_id": "node-joined",
        "left": True,
    }
    assert coordinator.calls == [
        ("create_invite", invite_request),
        ("join", join_request),
        ("leave", leave_request),
    ]


def test_swarm_unknown_fields_and_private_endpoint_projection_fail_closed(
    app, bootstrap, coordinator
) -> None:
    invalid = post(
        app,
        bootstrap,
        "/api/v1/swarm/invites",
        {
            "protocol": "mycelium.product_ui.swarm.v1",
            "action": "create_invite",
            "capability": "native_inference_node",
            "expires_in_seconds": 60,
            "unexpected": True,
        },
    )
    assert invalid.status == 400
    assert invalid.json()["code"] == "invalid_swarm_request"

    original = coordinator.status
    coordinator.status = lambda: {
        "protocol": "mycelium.product_ui.swarm.v1",
        "native_nodes": [
            {
                "member_id": "node-1",
                "capability": "native_inference_node",
                "membership_state": "reachable",
                "connectivity": "direct",
                "endpoint_id": "192.168.1.20",
            }
        ],
        "browser_workers": [],
    }
    try:
        status = request(app, "/api/v1/swarm/status", headers=session_headers(bootstrap))
    finally:
        coordinator.status = original

    assert status.status == 502
    assert status.json()["code"] == "invalid_coordinator_response"
    assert b"192.168.1.20" not in status.body
