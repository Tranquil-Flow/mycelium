# SPDX-License-Identifier: AGPL-3.0-only
"""The A5 replica readers of a live stack follow the route the owner serves.

``build_live_stack`` handed the request gateway ``route.replica_track_qualifications``
and ``route.replica_loss_placement_ids`` as values bound at build time. On a
``ColdRouteRestartOwner`` the attribute lookup resolves through the owner to the
first concrete route, so after a cold restart the replica dispatcher kept reading
the RETIRED route's qualifications and loss set. These are the stale-binding
variant of the product readers ``_follow`` already fixed: every call must read
the replacement, and while the owner has no route the reader must refuse with
the owner's bounded code instead of reading anything.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
from typing import Any

import pytest

from mycelium_live import supervisor
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    COLD_ROUTE_RESTART_EXHAUSTED_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)
from mycelium_request_gateway.contracts import AdmissionError
from tests.live.test_cold_route_restart import _Route, _Sink


class _ReplicaRoute(_Route):
    """A cold-restartable route that serves the A5 replica readers."""

    def replica_track_qualifications(self):
        return ({"protocol": "mycelium.replica_qualification.v1", "route_name": self.name},)

    def replica_loss_placement_ids(self):
        return frozenset({f"lost-{self.name}"})


def _qualify(route: _ReplicaRoute, generation: int):
    return route.open(), f"qualification-{generation}-{route.name}"


class _RouterProbe:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def is_idle(self) -> bool:
        return True

    def runtime_status(self) -> dict[str, object]:
        return {}


def _replica_factories(monkeypatch, tmp_path: Path, owner: Any):
    """Build the live stack around ``owner``; return the two replica factories.

    Only the collaborators the readers do not depend on are probes; the
    readers themselves are what ``build_live_stack`` hands the product stack.
    """

    captured: dict[str, object] = {}

    def build_stack_probe(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        supervisor,
        "prompt_codec_from_deployment",
        lambda _path: SimpleNamespace(stop_token_ids=frozenset()),
    )
    monkeypatch.setattr(supervisor, "_placement_projection", lambda _path: ())
    monkeypatch.setattr(supervisor, "_workload_comparison", lambda _path: ())
    monkeypatch.setattr(supervisor, "_m16_performance_budget", lambda _path: None)
    monkeypatch.setattr(supervisor, "build_live_m16_runtime", lambda *_a, **_k: object())
    monkeypatch.setattr(supervisor, "LiveRouterPort", _RouterProbe)
    monkeypatch.setattr(supervisor, "build_loopback_product_stack", build_stack_probe)
    monkeypatch.setattr(supervisor, "LiveSwarmCoordinator", lambda *_a, **_k: object())
    monkeypatch.setattr(
        supervisor,
        "_build_live_observatory_gateway",
        lambda **_kwargs: SimpleNamespace(app=object()),
    )
    supervisor.build_live_stack(
        route=owner,
        deployment_dir=tmp_path / "deployment",
        execution_graph=SimpleNamespace(deployment_id="deployment"),
        bearer_token="test-token",
        product_state_root=tmp_path / "product-state",
    )
    qualifications = captured["replica_qualifications_factory"]
    losses = captured["replica_loss_placement_ids_factory"]
    assert callable(qualifications) and callable(losses)
    return qualifications, losses


def _route_names(qualifications) -> list[str]:
    return [document["route_name"] for document in qualifications()]


def test_replica_readers_read_the_replacement_after_a_cold_restart(
    monkeypatch, tmp_path: Path
) -> None:
    first = _ReplicaRoute("first", fail_on_infer=True)
    second = _ReplicaRoute("second")
    routes = iter((first, second))
    owner = ColdRouteRestartOwner(
        route_factory=lambda: next(routes), open_and_qualify=_qualify, restart_budget=1)
    owner.open()
    try:
        qualifications, losses = _replica_factories(monkeypatch, tmp_path, owner)
        assert (_route_names(qualifications), losses()) == (["first"], {"lost-first"})

        with pytest.raises(ColdRouteRestartError) as restarted:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        assert restarted.value.code == COLD_ROUTE_RESTART_ERROR_CODE
        assert owner.current() == "qualification-2-second"
        assert first.close_count == 1  # the first route is retired

        assert (_route_names(qualifications), losses()) == (["second"], {"lost-second"})
    finally:
        owner.close()


def test_replica_readers_refuse_with_the_restart_code_while_the_owner_restarts(
    monkeypatch, tmp_path: Path
) -> None:
    first = _ReplicaRoute("first", fail_on_infer=True)
    second = _ReplicaRoute("second")
    created: list[_ReplicaRoute] = []
    restarting = threading.Event()
    allow_restart = threading.Event()

    def factory() -> _ReplicaRoute:
        if created:
            restarting.set()
            assert allow_restart.wait(timeout=10.0)
        route = [first, second][len(created)]
        created.append(route)
        return route

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=_qualify, restart_budget=1)
    owner.open()
    qualifications, losses = _replica_factories(monkeypatch, tmp_path, owner)
    outcome: dict[str, str] = {}

    def trigger_restart() -> None:
        try:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        except ColdRouteRestartError as exc:
            outcome["code"] = exc.code

    thread = threading.Thread(target=trigger_restart, daemon=True)
    try:
        thread.start()
        assert restarting.wait(timeout=10.0)
        assert owner.public_status()["route_restarting"] is True
        # No route to read: a bounded admission refusal (the request gateway's
        # failed terminal names it), never the retired route's documents.
        for reader in (qualifications, losses):
            with pytest.raises(AdmissionError) as refused:
                reader()
            assert refused.value.code == COLD_ROUTE_RESTART_ERROR_CODE
    finally:
        allow_restart.set()
        thread.join(timeout=10.0)
    try:
        assert not thread.is_alive()
        assert outcome == {"code": COLD_ROUTE_RESTART_ERROR_CODE}
        assert (_route_names(qualifications), losses()) == (["second"], {"lost-second"})
    finally:
        owner.close()


def test_replica_readers_refuse_with_the_failed_restart_code(
    monkeypatch, tmp_path: Path
) -> None:
    route = _ReplicaRoute("only", fail_on_infer=True)
    owner = ColdRouteRestartOwner(
        route_factory=lambda: route, open_and_qualify=_qualify, restart_budget=0)
    owner.open()
    try:
        qualifications, losses = _replica_factories(monkeypatch, tmp_path, owner)
        with pytest.raises(ColdRouteRestartError) as raised:
            owner.infer((1,), max_new_tokens=1, request_id="request-exhaust", sink=_Sink())
        assert raised.value.code == COLD_ROUTE_RESTART_EXHAUSTED_CODE
        for reader in (qualifications, losses):
            with pytest.raises(AdmissionError) as refused:
                reader()
            assert refused.value.code == COLD_ROUTE_RESTART_EXHAUSTED_CODE
    finally:
        owner.close()


def test_a_plain_route_keeps_its_replica_readers(monkeypatch, tmp_path: Path) -> None:
    """Characterization: a concrete route (no owner) is read directly as before."""

    route = _ReplicaRoute("plain")
    route.open()
    qualifications, losses = _replica_factories(monkeypatch, tmp_path, route)
    assert (_route_names(qualifications), losses()) == (["plain"], {"lost-plain"})
