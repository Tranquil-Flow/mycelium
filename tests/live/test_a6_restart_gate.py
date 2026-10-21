# SPDX-License-Identifier: AGPL-3.0-only
"""A6 recovery gates the cold restart; teardown of a restart is deterministic.

Hardware (candidates 5b and 6): with an M16 recovery runtime attached, the
background cold restart replaced the degraded route while a recovery (whose
successor has the *same* request id as the released source) still needed it,
and a restart still opening nodes at SIGINT left node processes for the
post-teardown census.  A5's property -- a failed terminal does not wait for a
slow replacement route (tests/live/test_a5_remote_stage_loss_terminal.py) --
holds because the background restart stays unless the A6 fault control is
mounted; the outstanding-recovery gate applies either way.
"""
from __future__ import annotations

import threading
import time

import pytest

from mycelium_live import supervisor as supervisor_module
from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live.test_cold_restart_background_race import (
    _EXPECTED,
    _Degraded,
    _Nodes,
    _restart_threads,
    _wait_generation,
)
from tests.live.test_cold_route_restart import _Sink

RID = "source"


class _RecoveringRoute(_Degraded):
    """Entry peer lost; the source attempt fails with a scoped, M16-owned incident."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.attempts = 0

    def a4_scoped_runtime_incidents(self):
        return [{"request_id": RID, "fatal_accepted": False}]

    def infer(self, token_ids, **options):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("peer_lost")
        return super().infer(token_ids, **options)


class _Runtime:
    """The M16 runtime document the owner reads (active ids + request records)."""

    def __init__(self) -> None:
        self.active: list[str] = [RID]
        self.phase = "successor_pending"
        self.terminal: str | None = None

    def __call__(self):
        return {
            "queue": {"active_request_ids": list(self.active)},
            "requests": [
                {"request_id": RID, "phase": self.phase, "terminal_state": self.terminal}
            ],
        }

    def settle(self, state: str = "completed") -> None:
        self.active, self.phase, self.terminal = [], state, state


def _owner(routes):
    created: list = []

    def factory():
        route = routes[len(created)]
        created.append(route)
        return route

    return ColdRouteRestartOwner(
        route_factory=factory,
        open_and_qualify=lambda route, generation: (
            route.open(), f"qualification-{generation}-{route.name}"),
        restart_budget=1,
    )


@pytest.mark.parametrize("a6_fault_control", [True, False])
def test_recovering_request_id_gates_the_restart_until_the_recovery_settles(
    a6_fault_control,
) -> None:
    route, spare = _RecoveringRoute("first"), _Degraded("spare")
    owner = _owner([route, spare])
    if a6_fault_control:
        owner.attach_recovery_guard()
    runtime = _Runtime()
    owner.set_m16_runtime_source(runtime)
    owner.open()
    route.lost = True
    try:
        # The source attempt fails; M16 recovery owns it.
        try:
            owner.infer((1, 2, 3), max_new_tokens=4, request_id=RID, sink=_Sink())
        except RuntimeError as error:
            assert str(error) == "peer_lost"
        else:  # pragma: no cover
            raise AssertionError("the source attempt must fail")
        # The router releases the source while its recovery still runs.
        owner.release_request(RID)
        time.sleep(0.2)
        assert _restart_threads() == []
        assert owner._current is route
        assert owner._state == "ready" and route.close_count == 0
        # The successor reuses the request id on the surviving route.
        result = owner.infer((1, 2, 3), max_new_tokens=4, request_id=RID, sink=_Sink())
        assert tuple(result.token_ids) == _EXPECTED
        assert owner._current is route and spare.open_count == 0
        # Recovery still unsettled: its release must not restart either.
        owner.release_request(RID)
        assert owner._current is route and spare.open_count == 0
        # Settled: the next release restarts. With the A6 fault control that
        # is inline on the releasing thread; otherwise in the background.
        runtime.settle()
        owner.release_request(RID)
        if a6_fault_control:
            assert owner.public_status()["cold_route_restart"]["generation"] == 2
            assert _restart_threads() == []
        else:
            _wait_generation(owner, 2)
        assert route.close_count == 1 and spare.open_count == 1
    finally:
        owner.close()


def test_a_terminal_released_request_does_not_block_the_restart() -> None:
    route, spare = _Degraded("first"), _Degraded("spare")
    owner = _owner([route, spare])
    runtime = _Runtime()
    runtime.settle("failed")
    owner.set_m16_runtime_source(runtime)
    owner.open()
    route.lost = True
    try:
        owner._request_routes[RID] = route
        owner.release_request(RID)
        _wait_generation(owner, 2)
    finally:
        owner.close()


def test_without_the_a6_fault_control_the_restart_stays_in_the_background() -> None:
    """The A5 property: no A6 fault control, so the release returns at once."""

    gate = threading.Event()
    route, spare = _Degraded("first"), _Degraded("spare")
    created: list = []

    def factory():
        if created:
            assert gate.wait(timeout=10.0)
        created.append(1)
        return [route, spare][len(created) - 1]

    owner = ColdRouteRestartOwner(
        route_factory=factory,
        open_and_qualify=lambda r, g: (r.open(), f"q-{g}"),
        restart_budget=1,
    )
    owner.open()
    route.lost = True
    try:
        owner._request_routes[RID] = route
        started = time.monotonic()
        owner.release_request(RID)
        assert time.monotonic() - started < 2.0
        assert len(_restart_threads()) == 1
    finally:
        gate.set()
        owner.close()


# -- deterministic teardown ------------------------------------------------------


class _SpawningRoute(_Degraded):
    """open() starts a node, then blocks and ignores abort until close()."""

    def __init__(self, name: str, nodes: _Nodes) -> None:
        super().__init__(name)
        self.nodes = nodes
        self.gate = threading.Event()
        self.mid_open = threading.Event()

    def open(self):
        self.nodes.spawn(f"{self.name}/node-0")
        self.mid_open.set()
        self.gate.wait(timeout=15.0)
        self.nodes.spawn(f"{self.name}/node-2")  # a spawn after close() began
        return super().open()

    def close(self) -> None:
        super().close()
        self.nodes.kill_all(f"{self.name}/")
        self.gate.set()


def test_close_during_a_spawning_restart_leaves_nothing_alive() -> None:
    nodes = _Nodes()
    first = _Degraded("first")
    replacement = _SpawningRoute("replacement", nodes)
    created: list = []

    def factory():
        created.append(1)
        if len(created) == 1:
            return first
        nodes.spawn("replacement/factory-node")  # spawned before open() starts
        return replacement

    owner = ColdRouteRestartOwner(
        route_factory=factory,
        open_and_qualify=lambda r, g: (r.open(), f"q-{g}"),
        restart_budget=1,
    )
    owner.open()
    first.lost = True
    owner._request_routes[RID] = first
    owner.release_request(RID)  # no A6 fault control: background restart
    assert replacement.mid_open.wait(timeout=5.0)
    assert supervisor_module.COLD_ROUTE_RESTART_CLOSE_JOIN_SECONDS <= 8.0
    started = time.monotonic()
    owner.close()
    assert time.monotonic() - started < 12.0
    assert nodes.alive == set(), nodes.alive
    assert replacement.close_count >= 1
    assert _restart_threads() == []
    assert owner._replacing is None
