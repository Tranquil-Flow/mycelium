# SPDX-License-Identifier: AGPL-3.0-only
"""Closing the route owner during a cold restart must not leak or wait out the restart.

A6 browser matrix on hardware (1bc4d342): the serve got SIGTERM while the
``ColdRouteRestartOwner`` was restarting. ``close()`` only closed the current
route (none while restarting); the restart kept opening and qualifying the
replacement in the request worker, and once done it parked the open
replacement as the startup-failed route that nobody closes any more. Its node
processes (and their non-daemon reader threads) kept the serve alive until the
receiver factory SIGKILLed it 120 s later.
"""
from __future__ import annotations

import threading
import time

from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_FAILED_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)
from tests.live.test_cold_route_restart import _Route, _Sink

_BOUND_S = 2.0


class _HeldRoute(_Route):
    """A replacement whose open blocks until released (or aborted)."""

    def __init__(self, name: str, *, abortable: bool) -> None:
        super().__init__(name)
        self.opening = threading.Event()
        self.release = threading.Event()
        self.abort_calls = 0
        self._aborted = False
        if abortable:
            self.abort_open = self._abort_open

    def _abort_open(self) -> None:
        self.abort_calls += 1
        self._aborted = True
        self.release.set()

    def open(self):
        self.opening.set()
        assert self.release.wait(timeout=10.0)
        if self._aborted:
            raise RuntimeError("route_open_aborted")
        return super().open()


def _restarting_owner(replacement: _Route, *, hold_factory: threading.Event | None = None):
    first = _Route("first", fail_on_infer=True)
    created: list[_Route] = []
    factory_entered = threading.Event()

    def factory() -> _Route:
        if created:
            factory_entered.set()
            if hold_factory is not None:
                assert hold_factory.wait(timeout=10.0)
        route = [first, replacement][len(created)]
        created.append(route)
        return route

    def qualify(route: _Route, generation: int):
        return route.open(), f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=qualify, restart_budget=1)
    owner.open()
    outcome: dict[str, object] = {}

    def request() -> None:
        try:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        except ColdRouteRestartError as exc:
            outcome["code"] = exc.code
        outcome["done_at"] = time.monotonic()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    return owner, worker, outcome, factory_entered


def test_closing_mid_restart_retires_the_replacement_instead_of_parking_it() -> None:
    replacement = _HeldRoute("replacement", abortable=False)
    owner, worker, outcome, _entered = _restarting_owner(replacement)
    try:
        assert replacement.opening.wait(timeout=5.0)
        owner.close()
    finally:
        replacement.release.set()
        worker.join(timeout=10.0)
    assert not worker.is_alive()

    assert outcome["code"] == COLD_ROUTE_RESTART_FAILED_CODE
    assert replacement.close_count == 1, "the opened replacement was left running"
    # Same as an ordinary shutdown: close only, the staging stays.
    assert replacement.cleanup_count == 0
    assert owner.current() is None
    assert owner.public_status()["cold_route_restart"]["state"] == "closed"


def test_closing_mid_restart_aborts_the_replacement_open_within_a_bound() -> None:
    replacement = _HeldRoute("replacement", abortable=True)
    owner, worker, outcome, _entered = _restarting_owner(replacement)
    try:
        assert replacement.opening.wait(timeout=5.0)
        closed_at = time.monotonic()
        owner.close()
        worker.join(timeout=_BOUND_S)
        finished = not worker.is_alive()
    finally:
        replacement.release.set()
        worker.join(timeout=10.0)

    assert finished, f"the restart kept opening the replacement for >{_BOUND_S} s after close"
    assert outcome["done_at"] - closed_at < _BOUND_S
    assert replacement.abort_calls == 1
    assert outcome["code"] == COLD_ROUTE_RESTART_FAILED_CODE
    assert replacement.close_count == 1
    assert owner.public_status()["cold_route_restart"]["state"] == "closed"


def test_closing_before_the_replacement_opens_never_opens_it() -> None:
    hold = threading.Event()
    replacement = _HeldRoute("replacement", abortable=True)
    replacement.release.set()
    owner, worker, outcome, entered = _restarting_owner(replacement, hold_factory=hold)
    try:
        assert entered.wait(timeout=5.0)
        owner.close()
    finally:
        hold.set()
        worker.join(timeout=10.0)
    assert not worker.is_alive()

    assert outcome["code"] == COLD_ROUTE_RESTART_FAILED_CODE
    assert replacement.open_count == 0
    assert not replacement.opening.is_set()
    assert replacement.close_count == 1
    assert owner.public_status()["cold_route_restart"]["state"] == "closed"
