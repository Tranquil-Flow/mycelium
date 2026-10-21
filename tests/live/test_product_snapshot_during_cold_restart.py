# SPDX-License-Identifier: AGPL-3.0-only
"""The product snapshot stays available while the route owner cold-restarts.

A6 browser matrix on hardware (1bc4d342): after the source fault the
``ColdRouteRestartOwner`` was ``restarting``; every product reader composed by
``_product_application`` follows the owner, whose ``__getattr__`` then raises
``ColdRouteRestartError``. The internet-native reader's error escaped
``ProductEvidenceApplication._publish`` and the browser's recovery wait got a
fatal ``503 product_snapshot_unavailable`` mid-recovery. A transient restart
must instead publish a 200 snapshot that names the restart per source.
"""
from __future__ import annotations

import asyncio
import json
import threading

from mycelium_internet.contracts import compatibility_fixtures as internet_fixtures
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
    _product_application,
)
from tests.live.test_cold_route_restart import _Route, _Sink
from tests.product_spine.test_application import _sources


class _ProductRoute(_Route):
    """A cold-restartable route that also serves the product readers."""

    def __init__(self, name: str, **options) -> None:
        super().__init__(name, **options)
        self._members, self._status, _qualification = _sources()

    def public_status(self):
        return {**self._status, "route_alive": self.is_alive()}

    def product_membership_records(self):
        return list(self._members)

    def product_assignment_records(self):
        return []

    def product_pseudonym_salt(self) -> bytes:
        return b"p" * 32

    def product_internet_native_snapshot(self) -> dict:
        fixtures = internet_fixtures()
        activation = fixtures["internet-activation-observation-v1.json"]
        return {
            "bootstrap_status": fixtures["internet-bootstrap-status-v1.json"],
            "activation_observation": activation,
            "activation_history": [activation],
            "relay_projection": None,
            "qualification": None,
        }


class _NoQualification:
    def current(self):
        return None


async def _get(app, path: str = "/v1/product/snapshot") -> tuple[int, dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], json.loads(body)


def _source(snapshot: dict, source_id: str) -> dict:
    return next(item for item in snapshot["source_states"] if item["source_id"] == source_id)


def test_snapshot_is_served_with_restarting_sources_during_a_cold_restart() -> None:
    first = _ProductRoute("first", fail_on_infer=True)
    second = _ProductRoute("second")
    created: list[_ProductRoute] = []
    restarting = threading.Event()
    allow_restart = threading.Event()

    def factory() -> _ProductRoute:
        if created:
            restarting.set()
            assert allow_restart.wait(timeout=10.0)
        route = [first, second][len(created)]
        created.append(route)
        return route

    def qualify(route: _ProductRoute, generation: int):
        return route.open(), f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=qualify, restart_budget=1)
    owner.open()
    app = _product_application(
        owner, _NoQualification(), recovery_source=lambda: None)
    outcome: dict[str, str] = {}

    def trigger_restart() -> None:
        try:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        except ColdRouteRestartError as exc:
            outcome["code"] = exc.code

    before_status, before = asyncio.run(_get(app))
    assert before_status == 200
    thread = threading.Thread(target=trigger_restart, daemon=True)
    thread.start()
    try:
        assert restarting.wait(timeout=10.0)
        assert owner.public_status()["route_restarting"] is True

        export_status, _export = asyncio.run(_get(app, "/v1/product/export"))
        status, during = asyncio.run(_get(app))
        assert (status, export_status) == (200, 200), during
        assert during["publication"]["cursor"] > before["publication"]["cursor"]
        for source_id in ("membership-source", "assignment-source"):
            state = _source(during, source_id)
            assert (state["status"], state["reason_code"]) == (
                "missing", COLD_ROUTE_RESTART_ERROR_CODE), state
        internet = during["internet_native"]
        assert internet["bootstrap_status"]["route_state"] == "unavailable"
        assert internet["activation_observation"]["path_class"] == "unknown"
        # The direct observation published before the restart is retained.
        assert "fixture-observation" in [
            item["observation_id"] for item in internet["activation_history"]]
    finally:
        allow_restart.set()
        thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert outcome == {"code": COLD_ROUTE_RESTART_ERROR_CODE}

    after_status, after = asyncio.run(_get(app))
    assert after_status == 200
    assert _source(after, "membership-source")["status"] == "current"
    assert after["internet_native"]["activation_observation"]["observation_id"] == (
        "fixture-observation")
