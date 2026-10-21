# SPDX-License-Identifier: AGPL-3.0-only
"""Live-unit coverage for the ``a7_source_lost`` node command dispatch."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from physical_inference_node import NODE_CONTROL_PROTOCOL, PhysicalNodeService

_CONTROL = {
    "deployment_id": "deployment-a7",
    "deployment_epoch": 3,
    "qualification_digest": "sha256:" + "a" * 64,
    "command_id": "command-a7",
    "publisher_generation": 1,
    "absolute_deadline_ms": 10**12,
    "request_attempt": 1,
    "path_id": "path-a7",
    "path_attempt": 0,
    "path_digest": "sha256:" + "b" * 64,
    "topology_generation": 5,
    "cancellation_generation": 0,
}


class _Wiring:
    def __init__(self) -> None:
        self.claims: list[tuple[str, str]] = []

    def accept_source_lost(self, request_id: str, lost_node_id: str) -> dict[str, Any]:
        self.claims.append((request_id, lost_node_id))
        return {
            "accepted": True,
            "reason": "source_lost_accepted",
            "cutover_pending": False,
        }

    def cut_over_lost_source(self, _router: Any, _request_id: str, _lost_node_id: str) -> bool:
        raise AssertionError("no cutover should be deferred when cutover_pending is false")


def _service(wiring: _Wiring) -> PhysicalNodeService:
    service = object.__new__(PhysicalNodeService)
    service.node_id = "node-0"
    service.run_id = "run-a7"
    service.deployment_id = "deployment-a7"
    service.state = "RUNNING"
    service.router = SimpleNamespace(name="router")
    service.graph = SimpleNamespace(
        deployment_id="deployment-a7",
        deployment_epoch=3,
        topology_version=5,
    )
    service._a7_wiring = wiring
    service._control_lock = threading.RLock()
    service._request_controls = {"request-a7": dict(_CONTROL)}
    service._after_response = threading.local()
    service._signed_result = lambda event, details: {"event": event, "details": dict(details)}
    return service


def test_dispatch_a7_source_lost_calls_wiring_and_returns_signed_details() -> None:
    wiring = _Wiring()
    service = _service(wiring)

    result = service.dispatch(
        {
            "protocol": NODE_CONTROL_PROTOCOL,
            "command_id": "command-a7-source-lost",
            "run_id": "run-a7",
            "deployment_id": "deployment-a7",
            "command": "a7_source_lost",
            "payload": {
                "request_id": "request-a7",
                "lost_node_id": "node-2",
                "control": dict(_CONTROL),
            },
        }
    )

    assert wiring.claims == [("request-a7", "node-2")]
    assert result == {
        "event": "a7_source_lost",
        "details": {
            "request_id": "request-a7",
            "lost_node_id": "node-2",
            "control": dict(_CONTROL),
            "accepted": True,
            "reason": "source_lost_accepted",
            "cutover_pending": False,
        },
    }
