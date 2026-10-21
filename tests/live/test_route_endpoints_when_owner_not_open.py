# SPDX-License-Identifier: AGPL-3.0-only
"""Every route-backed ``/__mycelium/`` endpoint answers while the owner is not open.

Before ``open()`` and after ``close()`` the ``ColdRouteRestartOwner`` has no
route. Its ``__getattr__`` raised a plain ``RuntimeError("route_not_open")`` /
``RuntimeError("route_closed")``, which the live HTTP handlers do not catch (a
``getattr`` default does not, and the restart fix only catches
``ColdRouteRestartError``). A request racing serve start-up or shutdown then
killed its handler thread and the connection was dropped with no response.
Each endpoint must answer a well-formed refusal naming the owner's existing
code instead: ``503`` for a read, ``409`` for a refused mutation (the same
statuses the cold-restart refusal uses).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live.test_status_endpoints_during_cold_restart import (
    _ENDPOINTS,
    _Served,
    _StatusRoute,
    _ids,
    _qualify,
)


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("ok", encoding="utf-8")
    return root


def _owner(route: _StatusRoute) -> ColdRouteRestartOwner:
    return ColdRouteRestartOwner(
        route_factory=lambda: route, open_and_qualify=_qualify, restart_budget=1)


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=_ids)
def test_endpoint_answers_route_not_open_before_the_owner_opens(
    endpoint, static_root: Path
) -> None:
    method, path, body, refusal = endpoint
    route = _StatusRoute("only")
    owner = _owner(route)
    served = _Served(owner, static_root)
    try:
        assert served.request(method, path, body) == (refusal, {"error": "route_not_open"})
        status, live = served.request("GET", "/__mycelium/live-status", None)
        assert (status, live["route_alive"]) == (200, False)
        assert route.open_count == 0  # nothing was opened or applied
        assert not hasattr(route, "installed")
        # Once open, the same endpoint serves the route.
        owner.open()
        assert served.request(method, path, body)[0] == 200
    finally:
        served.close()
        owner.close()


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=_ids)
def test_endpoint_answers_route_closed_after_the_owner_closes(
    endpoint, static_root: Path
) -> None:
    method, path, body, refusal = endpoint
    route = _StatusRoute("only")
    owner = _owner(route)
    owner.open()
    served = _Served(owner, static_root)
    try:
        assert served.request(method, path, body)[0] == 200
        owner.close()
        assert route.close_count == 1
        assert served.request(method, path, body) == (refusal, {"error": "route_closed"})
        status, live = served.request("GET", "/__mycelium/live-status", None)
        assert (status, live["route_alive"]) == (200, False)
    finally:
        served.close()
        owner.close()
