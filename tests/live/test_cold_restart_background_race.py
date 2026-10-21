# SPDX-License-Identifier: AGPL-3.0-only
"""The background cold restart must not race A6 recovery or shutdown.

cb13ab64 moved the idle cold restart of a route that lost a peer into a
``cold-route-restart`` thread that starts after the released request's
terminal.  On hardware (candidate 5b) that regressed A6:

* the successor qualifier's probe (``a6-qual-*``) is released through the
  owner; a detached restart then retires the degraded route the running
  recovery still needs, so the recovery never reaches a terminal;
* SIGINT during the background restart left a half-opened replacement whose
  node processes tripped the post-teardown census.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_live import supervisor as supervisor_module
from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live.test_cold_route_restart import _Route, _Sink

_EXPECTED = (7, 8, 9, 10)
_RESTART_THREAD = "cold-route-restart"


class _Degraded(_Route):
    """A route whose entry peer is lost while a surviving peer still serves."""

    startup_challenge = ((1, 2, 3), _EXPECTED)

    def __init__(self, name: str) -> None:
        super().__init__(name, tokens=_EXPECTED)
        self.lost = False

    def is_alive(self) -> bool:
        return super().is_alive() and not self.lost

    def a4_liveness_status(self):
        return {
            "protocol": "mycelium.traffic_liveness.v1",
            "deployment_fatal_reason": None,
            "subjects": [
                {"subject_id": "entry", "state": "failed" if self.lost else "fresh"},
                {"subject_id": "survivor", "state": "fresh"},
            ],
            "incidents": [],
        }


def _owner(routes, *, factory_gate: threading.Event | None = None):
    created: list = []

    def factory():
        if created and factory_gate is not None:
            assert factory_gate.wait(timeout=10.0)
        route = routes[len(created)]
        created.append(route)
        return route

    def qualify(route, generation):
        return route.open(), f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=qualify, restart_budget=1)
    return owner, created


def _qualifier(tmp_path, owner) -> LiveA6RecoveryQualifier:
    graph = SimpleNamespace(
        deployment_id="d", deployment_epoch=1, topology_version=1, model_id="m",
        resolved_commit="r", manifest_digest="sha256:" + "a" * 64)
    return LiveA6RecoveryQualifier(
        graph=graph, deployment_dir=tmp_path, tokenizer_digest="sha256:" + "9" * 64,
        route=owner, clock_ms=lambda: 1_000)


def _probe(qualifier):
    return qualifier._run_physical_challenge(
        request_id="req-1", placement_ids=("placement-001",),
        absolute_deadline_unix_ms=60_000)


def _restart_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _RESTART_THREAD and t.is_alive()]


def _wait_generation(owner, generation: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while owner.public_status()["cold_route_restart"]["generation"] != generation:
        assert time.monotonic() < deadline, owner.public_status()["cold_route_restart"]
        time.sleep(0.01)


def _degraded_owner(*, factory_gate=None):
    route, spare = _Degraded("first"), _Degraded("spare")
    owner, created = _owner([route, spare], factory_gate=factory_gate)
    owner.open()
    route.lost = True
    return owner, route, spare


def _assert_untouched(owner, route) -> None:
    assert _restart_threads() == []
    assert owner._restart_threads == []
    assert owner._current is route
    assert owner._state == "ready"
    assert route.close_count == 0


def test_probe_release_while_the_source_is_mapped_never_restarts(tmp_path) -> None:
    owner, route, spare = _degraded_owner()
    try:
        owner._request_routes["source"] = route  # the recovering request
        assert _probe(_qualifier(tmp_path, owner))["passed"] is True
        _assert_untouched(owner, route)
        owner.release_request("source")  # the final real release restarts
        _wait_generation(owner, 2)
        assert route.close_count == 1 and spare.open_count == 1
    finally:
        owner.close()


def test_probe_release_never_restarts_even_when_the_source_is_not_mapped(tmp_path) -> None:
    owner, route, spare = _degraded_owner()
    try:
        assert _probe(_qualifier(tmp_path, owner))["passed"] is True
        _assert_untouched(owner, route)
        assert spare.open_count == 0
    finally:
        owner.close()


def test_release_waits_for_the_recovery_runtime_to_finish_the_request() -> None:
    owner, route, spare = _degraded_owner()
    active = {"ids": ["source", "other"]}
    owner.set_m16_runtime_source(
        lambda: {"queue": {"active_request_ids": list(active["ids"])}})
    try:
        owner.infer((1,), max_new_tokens=1, request_id="other", sink=_Sink())
        owner.release_request("other")  # "source" still recovers
        _assert_untouched(owner, route)
        active["ids"] = []  # the recovery ended
        owner.release_request("source")
        _wait_generation(owner, 2)
        assert route.close_count == 1
    finally:
        owner.close()


def test_the_runtime_listing_the_released_request_terminal_does_not_block_the_restart() -> None:
    owner, route, _spare = _degraded_owner()
    owner.set_m16_runtime_source(
        lambda: {
            "queue": {"active_request_ids": ["source"]},
            "requests": [{"request_id": "source", "terminal_state": "failed"}],
        })
    try:
        owner._request_routes["source"] = route
        owner.release_request("source")
        _wait_generation(owner, 2)
    finally:
        owner.close()


def test_a_failing_runtime_source_never_blocks_the_restart() -> None:
    owner, route, _spare = _degraded_owner()

    def broken():
        raise RuntimeError("status unavailable")

    owner.set_m16_runtime_source(broken)
    try:
        owner._request_routes["source"] = route
        owner.release_request("source")
        _wait_generation(owner, 2)
    finally:
        owner.close()


def test_a_recovery_successor_still_runs_after_the_qualifier_probe(tmp_path) -> None:
    """a6n591: the recovery's successor needs the degraded route after the probe.

    The restart's replacement is held in the factory (a real 7B route takes
    minutes), so a detached restart leaves the route retired and admission
    refused for as long as the recovery waits.
    """

    gate = threading.Event()
    owner, route, _spare = _degraded_owner(factory_gate=gate)
    try:
        assert _probe(_qualifier(tmp_path, owner))["passed"] is True
        result = owner.infer(
            (1, 2, 3), max_new_tokens=len(_EXPECTED), request_id="req-1", sink=_Sink())
        assert tuple(result.token_ids) == _EXPECTED
    finally:
        gate.set()
        owner.close()


# -- shutdown determinism ------------------------------------------------------


class _Nodes:
    """Fake node processes: ``alive`` is what the post-teardown census counts."""

    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.lock = threading.Lock()

    def spawn(self, name: str) -> None:
        with self.lock:
            self.alive.add(name)

    def kill_all(self, prefix: str) -> None:
        with self.lock:
            self.alive = {n for n in self.alive if not n.startswith(prefix)}


class _NodeRoute(_Route):
    """open() starts nodes one at a time; only close() kills them."""

    def __init__(self, name: str, nodes: _Nodes, *, abortable: bool, gate: threading.Event):
        super().__init__(name)
        self.nodes = nodes
        self.gate = gate
        self.mid_open = threading.Event()
        self._aborted = False
        if abortable:
            self.abort_open = self._abort_open

    def _abort_open(self) -> None:
        self._aborted = True
        self.gate.set()

    def open(self):
        self.nodes.spawn(f"{self.name}/node-0")
        self.mid_open.set()
        assert self.gate.wait(timeout=20.0)
        if self._aborted:
            raise RuntimeError("route_open_aborted")
        self.nodes.spawn(f"{self.name}/node-2")
        return super().open()

    def close(self) -> None:
        super().close()
        self.nodes.kill_all(f"{self.name}/")
        self.gate.set()


def _restarting_after_release(replacement, *, factory_gate=None, old_close_gate=None):
    """A degraded route whose final real release starts the background restart."""

    first = _Degraded("first")
    created: list = []
    factory_calls: list[int] = []

    if old_close_gate is not None:
        original_close = first.close

        def slow_close() -> None:
            assert old_close_gate.wait(timeout=10.0)
            original_close()

        first.close = slow_close

    def factory():
        factory_calls.append(1)
        if factory_gate is not None:
            assert factory_gate.wait(timeout=10.0)
        route = [first, replacement][len(created)]
        created.append(route)
        return route

    def qualify(route, generation):
        return route.open(), f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=qualify, restart_budget=1)
    owner.open()
    first.lost = True
    owner._request_routes["source"] = first
    owner.release_request("source")
    return owner, factory_calls


def test_close_mid_open_leaves_no_replacement_node_alive() -> None:
    nodes = _Nodes()
    replacement = _NodeRoute("replacement", nodes, abortable=True, gate=threading.Event())
    owner, _calls = _restarting_after_release(replacement)
    assert replacement.mid_open.wait(timeout=5.0)
    assert nodes.alive == {"replacement/node-0"}
    owner.close()
    assert nodes.alive == set(), "a half-opened replacement node outlived close()"
    assert replacement.close_count >= 1
    assert _restart_threads() == []


def test_close_while_the_old_route_retires_never_builds_a_replacement() -> None:
    nodes = _Nodes()
    replacement = _NodeRoute("replacement", nodes, abortable=True, gate=threading.Event())
    old_close_gate = threading.Event()
    owner, calls = _restarting_after_release(replacement, old_close_gate=old_close_gate)
    time.sleep(0.2)  # the restart thread is retiring the old route
    threading.Timer(0.3, old_close_gate.set).start()
    owner.close()
    assert calls == [1], "the factory ran for a replacement after the owner was closed"
    assert nodes.alive == set()
    assert replacement.open_count == 0


def test_close_returns_with_the_replacement_closed_when_its_open_ignores_abort(
    monkeypatch,
) -> None:
    nodes = _Nodes()
    replacement = _NodeRoute("replacement", nodes, abortable=False, gate=threading.Event())
    monkeypatch.setattr(supervisor_module, "COLD_ROUTE_RESTART_CLOSE_JOIN_SECONDS", 0.5)
    owner, _calls = _restarting_after_release(replacement)
    assert replacement.mid_open.wait(timeout=5.0)
    started = time.monotonic()
    owner.close()
    assert time.monotonic() - started < 5.0
    assert nodes.alive == set()
    assert replacement.close_count >= 1
