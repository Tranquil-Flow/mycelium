# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from mycelium_live.route import InferenceResult, RouteCounters, RouteIdentity
from mycelium_live.router_port import LiveRouterPort
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    COLD_ROUTE_RESTART_EXHAUSTED_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)


class _Sink:
    def __init__(self) -> None:
        self.tokens: list[tuple[int, int]] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


class _Route:
    is_simulated = False
    startup_challenge = ((1,), (2,))

    def __init__(
        self,
        name: str,
        *,
        tokens: tuple[int, ...] = (7, 8, 9),
        fail_on_infer: bool = False,
    ) -> None:
        self.name = name
        self.tokens = tokens
        self.fail_on_infer = fail_on_infer
        self.open_count = 0
        self.close_count = 0
        self.cleanup_count = 0
        self.release_calls: list[str] = []
        self.dead = False
        self.stop_token_ids: tuple[int, ...] | None = None
        self.runtime_source = None
        self.model_operation = None
        self.execution_graph = SimpleNamespace(stages=())

    def open(self) -> RouteIdentity:
        self.open_count += 1
        self.dead = False
        return RouteIdentity(
            deployment_id="deployment",
            model_id="model",
            resolved_commit="commit",
            endpoint_ids=(f"endpoint-{self.name}",),
        )

    def infer(
        self,
        token_ids,
        *,
        max_new_tokens,
        request_id,
        sink,
        cancel_requested=None,
        **_options,
    ) -> InferenceResult:
        if self.fail_on_infer:
            self.dead = True
            raise RuntimeError("route_not_open")
        emitted = self.tokens[:max_new_tokens]
        for index, token_id in enumerate(emitted):
            sink.emit(index, token_id)
        return InferenceResult(request_id=request_id, token_ids=emitted)

    def release_request(self, request_id: str) -> None:
        self.release_calls.append(request_id)

    def counters(self) -> RouteCounters:
        return RouteCounters(
            frames_sent=len(self.tokens),
            frames_received=len(self.tokens),
            applied_operation_count=len(self.tokens),
        )

    def public_status(self):
        return {
            "protocol": "mycelium.live_route_status.v1",
            "route_alive": self.is_alive(),
            "route_name": self.name,
        }

    def is_alive(self) -> bool:
        return self.open_count > self.close_count and not self.dead

    def close(self) -> None:
        self.close_count += 1

    def cleanup(self) -> None:
        self.cleanup_count += 1

    def set_stop_token_ids(self, token_ids) -> None:
        self.stop_token_ids = tuple(token_ids)

    def set_m16_runtime_source(self, source) -> None:
        self.runtime_source = source

    def set_m17_model_operation(self, document) -> None:
        self.model_operation = document


def _owner_for(routes: list[_Route], *, restart_budget: int = 1) -> ColdRouteRestartOwner:
    created: list[_Route] = []

    def factory() -> _Route:
        route = routes[len(created)]
        created.append(route)
        return route

    def qualify(route: _Route, generation: int):
        identity = route.open()
        return identity, f"qualification-{generation}-{route.name}"

    return ColdRouteRestartOwner(
        route_factory=factory,
        open_and_qualify=qualify,
        restart_budget=restart_budget,
    )


def test_owner_restarts_once_and_publishes_fresh_qualification() -> None:
    first = _Route("first", fail_on_infer=True)
    second = _Route("second", tokens=(21, 34))
    owner = _owner_for([first, second])

    assert owner.open().endpoint_ids == ("endpoint-first",)
    assert owner.current() == "qualification-1-first"

    with pytest.raises(ColdRouteRestartError) as caught:
        owner.infer((1, 2), max_new_tokens=2, request_id="request-1", sink=_Sink())
    assert caught.value.code == COLD_ROUTE_RESTART_ERROR_CODE
    owner.release_request("request-1")

    assert first.close_count == 1
    assert first.cleanup_count == 0  # the replacement reopens the same staging
    assert first.release_calls == ["request-1"]
    assert second.open_count == 1
    assert owner.current() == "qualification-2-second"

    status = owner.public_status()
    assert status["route_ready"] is True
    assert status["route_name"] == "second"
    assert status["cold_route_restart"]["attempts"] == 1
    assert status["cold_route_restart"]["generation"] == 2

    sink = _Sink()
    result = owner.infer((3, 5), max_new_tokens=2, request_id="request-2", sink=sink)
    assert result.token_ids == (21, 34)
    assert sink.tokens == [(0, 21), (1, 34)]


def test_router_port_terminal_uses_restart_error_and_successor_serves(
    live_graph,
    request_factory,
) -> None:
    first = _Route("first", fail_on_infer=True)
    second = _Route("second", tokens=(55, 89, 144))
    owner = _owner_for([first, second])
    owner.open()
    port = LiveRouterPort(route=owner, execution_graph=live_graph)

    failed_sink = _Sink()
    failed_request = port.admit(request_factory("cold-route-failed"), failed_sink)
    assert port.decode_one(failed_request) is False
    assert port.request_status(failed_request) == "FAILED"
    assert port.request_error_code(failed_request) == COLD_ROUTE_RESTART_ERROR_CODE
    assert failed_sink.tokens == []
    port.release_request(failed_request)

    success_sink = _Sink()
    success_request = port.admit(request_factory("cold-route-successor"), success_sink)
    while port.decode_one(success_request):
        pass
    assert port.request_status(success_request) == "COMPLETED"
    assert success_sink.tokens == [(0, 55), (1, 89), (2, 144)]


def test_status_reports_restarting_while_requalification_blocks() -> None:
    first = _Route("first", fail_on_infer=True)
    second = _Route("second")
    created: list[_Route] = []
    restart_factory_entered = threading.Event()
    allow_restart = threading.Event()

    def factory() -> _Route:
        if created:
            restart_factory_entered.set()
            assert allow_restart.wait(timeout=5.0)
        route = [first, second][len(created)]
        created.append(route)
        return route

    def qualify(route: _Route, generation: int):
        identity = route.open()
        return identity, f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory,
        open_and_qualify=qualify,
        restart_budget=1,
    )
    owner.open()
    outcome: dict[str, str] = {}

    def trigger_restart() -> None:
        try:
            owner.infer((1,), max_new_tokens=1, request_id="request-restart", sink=_Sink())
        except ColdRouteRestartError as exc:
            outcome["code"] = exc.code

    thread = threading.Thread(target=trigger_restart)
    thread.start()
    assert restart_factory_entered.wait(timeout=5.0)

    status = owner.public_status()
    assert owner.current() is None
    assert status["route_ready"] is False
    assert status["route_restarting"] is True
    assert status["cold_route_restart"]["state"] == "restarting"

    allow_restart.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome == {"code": COLD_ROUTE_RESTART_ERROR_CODE}
    assert owner.current() == "qualification-2-second"
    assert owner.public_status()["route_ready"] is True


def test_restart_budget_exhaustion_is_observable_and_named() -> None:
    route = _Route("only", fail_on_infer=True)
    owner = _owner_for([route], restart_budget=0)
    owner.open()

    with pytest.raises(ColdRouteRestartError) as caught:
        owner.infer((1,), max_new_tokens=1, request_id="request-exhausted", sink=_Sink())
    assert caught.value.code == COLD_ROUTE_RESTART_EXHAUSTED_CODE
    assert route.close_count == 1
    assert route.cleanup_count == 1

    status = owner.public_status()
    assert status["route_ready"] is False
    assert status["restart_failed"] is True
    assert status["cold_route_restart"]["budget_exhausted"] is True
    assert status["cold_route_restart"]["last_error_code"] == COLD_ROUTE_RESTART_EXHAUSTED_CODE

    with pytest.raises(ColdRouteRestartError) as refused:
        owner.infer((2,), max_new_tokens=1, request_id="request-refused", sink=_Sink())
    assert refused.value.code == COLD_ROUTE_RESTART_EXHAUSTED_CODE
