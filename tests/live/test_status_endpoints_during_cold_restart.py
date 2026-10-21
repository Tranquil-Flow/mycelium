# SPDX-License-Identifier: AGPL-3.0-only
"""Every route-backed ``/__mycelium/`` endpoint answers while the owner restarts.

A6 browser matrix on hardware (1bc4d342, ``a6-serve-chromium.log`` 13:20:21):
while the ``ColdRouteRestartOwner`` was ``restarting`` its ``__getattr__``
raised ``ColdRouteRestartError``. The live HTTP handlers looked their route
reader up with ``getattr(self.server.route, name, None)``; a default does not
catch that error, so the handler thread died and the connection was dropped
with no response (``/__mycelium/runtime/admission-status`` and its m15/m17
siblings). Each endpoint must instead answer a well-formed refusal that names
the bounded restart code (the reason code the product snapshot publishes for
the same state): ``503`` for a read, ``409`` for a refused mutation.
"""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    COLD_ROUTE_RESTART_EXHAUSTED_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
    create_server,
)
from tests.live.test_cold_route_restart import _Route, _Sink

_OPERATOR_TOKEN = "o" * 40


class _StatusRoute(_Route):
    """A cold-restartable route that also serves every route-backed endpoint."""

    def _document(self, protocol: str) -> dict:
        return {"protocol": protocol, "route_name": self.name}

    def m15_plan_comparison(self):
        return self._document("mycelium.m15_plan_comparison.v1")

    def m16_runtime_status(self):
        return self._document("mycelium.m16_runtime_status.v1")

    def m17_model_operation(self):
        return self._document("mycelium.model_operation.v1")

    def m17_swarm_evidence(self):
        return self._document("mycelium.live_swarm_resource_observations.v1")

    def registry_status(self):
        return self._document("mycelium.live_deployment_registry.v1")

    def select(self, deployment_id: str):
        return {**self.registry_status(), "selected_deployment_id": deployment_id}

    def set_replica_track_qualification(self, documents) -> None:
        self.installed = list(documents)

    def promote_candidate(self, _report):
        return self.registry_status()

    def canary_candidate(self, candidate_id: str, **_options):
        return {"candidate_deployment_id": candidate_id, "route_name": self.name}

    def rollback_candidate(self, _candidate_id: str, *, reason: str):
        return {**self.registry_status(), "reason": reason}


# (method, path, body, refusal status while the owner has no route to read)
_ENDPOINTS = (
    ("GET", "/__mycelium/planning/workload-comparison", None, 503),
    ("GET", "/__mycelium/runtime/admission-status", None, 503),
    ("GET", "/__mycelium/models/operation", None, 503),
    ("GET", "/__mycelium/swarm/resource-observations", None, 503),
    ("GET", "/__mycelium/deployments", None, 503),
    ("POST", "/__mycelium/deployments/select", {"deployment_id": "deployment"}, 409),
    ("POST", "/__mycelium/replica-qualification/install", {"documents": []}, 409),
    (
        "POST",
        "/__mycelium/candidates/canary",
        {
            "candidate_deployment_id": "candidate",
            "case_id": "case-a",
            "prompt": "Arbitrary prompt",
            "max_new_tokens": 1,
        },
        409,
    ),
    ("POST", "/__mycelium/candidates/promote", {"report": "candidate"}, 409),
    (
        "POST",
        "/__mycelium/candidates/rollback",
        {"candidate_deployment_id": "candidate", "reason": "canary_regression"},
        409,
    ),
)


def _ids(endpoint) -> str:
    return f"{endpoint[0]} {endpoint[1]}"


async def _never_asgi(*_args):
    raise AssertionError("route_backed_endpoint_reached_asgi")


class _Served:
    def __init__(self, owner: ColdRouteRestartOwner, static_root: Path) -> None:
        self.server = create_server(
            app=_never_asgi,
            route=owner,
            static_root=static_root,
            host="127.0.0.1",
            port=0,
            replica_operator_token=_OPERATOR_TOKEN,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        """One request on a fresh connection; a dropped connection raises."""

        host = f"127.0.0.1:{self.server.server_port}"
        headers = {"Host": host}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers.update(
                {
                    "Origin": f"http://{host}",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {_OPERATOR_TOKEN}",
                }
            )
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("ok", encoding="utf-8")
    return root


def _qualify(route: _StatusRoute, generation: int):
    return route.open(), f"qualification-{generation}-{route.name}"


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=_ids)
def test_endpoint_answers_the_restart_code_while_the_owner_restarts(
    endpoint, static_root: Path
) -> None:
    method, path, body, refusal = endpoint
    first = _StatusRoute("first", fail_on_infer=True)
    second = _StatusRoute("second")
    created: list[_StatusRoute] = []
    restarting = threading.Event()
    allow_restart = threading.Event()

    def factory() -> _StatusRoute:
        if created:
            restarting.set()
            assert allow_restart.wait(timeout=10.0)
        route = [first, second][len(created)]
        created.append(route)
        return route

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=_qualify, restart_budget=1)
    owner.open()
    served = _Served(owner, static_root)
    outcome: dict[str, str] = {}

    def trigger_restart() -> None:
        try:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        except ColdRouteRestartError as exc:
            outcome["code"] = exc.code

    thread = threading.Thread(target=trigger_restart, daemon=True)
    try:
        assert served.request(method, path, body)[0] == 200
        thread.start()
        assert restarting.wait(timeout=10.0)
        assert owner.public_status()["route_restarting"] is True

        assert served.request(method, path, body) == (
            refusal, {"error": COLD_ROUTE_RESTART_ERROR_CODE})
        # The live status keeps naming the restart (characterization).
        status, live = served.request("GET", "/__mycelium/live-status", None)
        assert (status, live["route_restarting"]) == (200, True)
    finally:
        allow_restart.set()
        thread.join(timeout=10.0)
        try:
            assert not thread.is_alive()
            assert outcome == {"code": COLD_ROUTE_RESTART_ERROR_CODE}
            # The successor serves the endpoint again.
            status, document = served.request(method, path, body)
            assert status == 200
            if method == "GET":
                assert document["route_name"] == "second"
        finally:
            served.close()
            owner.close()


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=_ids)
def test_endpoint_answers_the_failed_restart_code(endpoint, static_root: Path) -> None:
    method, path, body, refusal = endpoint
    route = _StatusRoute("only", fail_on_infer=True)
    owner = ColdRouteRestartOwner(
        route_factory=lambda: route, open_and_qualify=_qualify, restart_budget=0)
    owner.open()
    with pytest.raises(ColdRouteRestartError) as raised:
        owner.infer((1,), max_new_tokens=1, request_id="request-exhaust", sink=_Sink())
    assert raised.value.code == COLD_ROUTE_RESTART_EXHAUSTED_CODE
    assert owner.public_status()["restart_failed"] is True
    served = _Served(owner, static_root)
    try:
        assert served.request(method, path, body) == (
            refusal, {"error": COLD_ROUTE_RESTART_EXHAUSTED_CODE})
    finally:
        served.close()
        owner.close()
