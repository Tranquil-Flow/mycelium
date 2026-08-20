from __future__ import annotations

import threading
import time

import pytest

from mycelium_live.route import FakeLiveRoute, InferenceCancelled, InferenceResult
from mycelium_live.lock_order import LockOrderDetector
from mycelium_live.router_port import LiveRouterPort
from mycelium_m16_runtime import build_live_m16_runtime
from mycelium_router.contracts import RouterConfig


def _wait(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for concurrent progress")
        time.sleep(0.005)


def test_overlapping_requests_advance_independently(
    live_graph, request_factory
) -> None:
    first_started = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    released: list[str] = []

    class ControlledRoute(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
        ):
            if request_id == "blocked-request":
                first_started.set()
                while cancel_requested is None or not cancel_requested():
                    time.sleep(0.005)
                raise InferenceCancelled("inference_cancelled")
            second_started.set()
            assert release_second.wait(timeout=2.0)
            sink.emit(0, 17)
            return InferenceResult(request_id=request_id, token_ids=(17,))

        def release_request(self, request_id: str) -> None:
            released.append(request_id)

    route = ControlledRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)

    class Sink:
        def emit(self, _index: int, _token: int) -> None:
            return None

    blocked = port.admit(request_factory("blocked-request"), Sink())
    assert first_started.wait(timeout=1.0)
    healthy = port.admit(request_factory("healthy-request"), Sink())
    assert second_started.wait(timeout=1.0)
    assert port.cancel(blocked) is True
    port.release_request(blocked)
    release_second.set()
    _wait(lambda: port.decode_one(healthy))
    assert port.request_status(healthy) == "COMPLETED"
    port.release_request(healthy)
    _wait(lambda: set(released) == {blocked, healthy})
    assert port.is_idle()
    port.close()


@pytest.mark.parametrize(
    "blocked_boundary",
    ("stage_command", "browser_write", "cleanup"),
)
def test_every_blocking_boundary_allows_other_dispatch_and_cancel(
    live_graph, request_factory, blocked_boundary
) -> None:
    first_started = threading.Event()
    second_completed = threading.Event()
    release_first = threading.Event()

    class Route(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
            **_kwargs,
        ):
            if request_id == "boundary-blocked":
                first_started.set()
                while not release_first.wait(0.005):
                    if cancel_requested is not None and cancel_requested():
                        raise InferenceCancelled("inference_cancelled")
                if blocked_boundary == "browser_write":
                    sink.emit(0, 23)
                return InferenceResult(request_id=request_id, token_ids=())
            sink.emit(0, 29)
            second_completed.set()
            return InferenceResult(request_id=request_id, token_ids=(29,))

    route = Route(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)

    class Sink:
        def emit(self, _index, _token):
            return None

    blocked = port.admit(request_factory("boundary-blocked"), Sink())
    assert first_started.wait(timeout=1.0)
    healthy = port.admit(request_factory(f"healthy-{blocked_boundary}"), Sink())
    assert second_completed.wait(timeout=1.0)
    assert port.cancel(blocked) is True
    release_first.set()
    assert port.decode_one(healthy)
    _wait(lambda: port.request_status(healthy) == "COMPLETED")
    port.release_request(blocked)
    port.release_request(healthy)
    _wait(port.is_idle)
    port.close()


def test_terminal_is_published_only_after_exact_owner_cleanup_receipt(
    live_graph, request_factory
) -> None:
    class Qualification:
        qualification_digest = "sha256:" + "a" * 64

    class ReceiptRoute:
        is_simulated = False

        def __init__(self) -> None:
            self.receipts = {}

        def is_alive(self) -> bool:
            return True

        def infer(
            self,
            _tokens,
            *,
            request_id,
            sink,
            route_identity,
            locked_path_manifest,
            command_identity,
            **_kwargs,
        ):
            assert locked_path_manifest["path_id"] == route_identity["path_id"]
            assert command_identity["path_digest"] == route_identity[
                "path_manifest_digest"
            ]
            sink.emit(0, 17)
            self.receipts[request_id] = {
                "deployment_id": command_identity["deployment_id"],
                "deployment_epoch": command_identity["deployment_epoch"],
                "qualification_digest": command_identity["qualification_digest"],
                "request_id": request_id,
                "request_attempt": command_identity["request_attempt"],
                "path_id": command_identity["path_id"],
                "path_attempt": command_identity["path_attempt"],
                "path_digest": command_identity["path_digest"],
                "topology_generation": command_identity["topology_generation"],
                "command_id": command_identity["command_id"],
                "cancellation_generation": command_identity[
                    "cancellation_generation"
                ],
                "publisher_generation": command_identity["publisher_generation"],
                "cleanup_owner_id": f"physical-live-route:{live_graph.deployment_id}",
                "node_ids": ["node-a", "node-b"],
            }
            return InferenceResult(request_id=request_id, token_ids=(17,))

        def request_cleanup_receipt(self, request_id):
            return self.receipts.get(request_id)

        def release_request(self, request_id):
            self.receipts.pop(request_id, None)

    route = ReceiptRoute()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    request = request_factory("receipt-gated-terminal")
    port.admit(request, Sink(), qualification_binding=Qualification())
    _wait(lambda: request.request_id in route.receipts)
    assert port.decode_one(request.request_id)
    _wait(lambda: port.request_status(request.request_id) == "COMPLETED")
    snapshot = port._commands.snapshot(request.request_id, request_attempt=1)

    assert len(snapshot) == 1
    assert snapshot[0].cleanup_result is not None
    assert snapshot[0].cleanup_result.released_resource_count == 2
    assert snapshot[0].cleanup_completed_at_ms is not None
    assert snapshot[0].terminal is not None
    assert snapshot[0].cleanup_completed_at_ms <= snapshot[0].terminal.observed_at_ms
    port.release_request(request.request_id)
    assert port._commands.snapshot(request.request_id, request_attempt=1) == ()
    port.close()


def test_reconnect_publisher_generation_reaches_live_route_and_cleanup_receipt(
    live_graph, request_factory
) -> None:
    class Qualification:
        qualification_digest = "sha256:" + "d" * 64

    class GenerationRoute:
        is_simulated = False

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.control = None
            self.receipt = None

        def is_alive(self) -> bool:
            return True

        def infer(
            self,
            _tokens,
            *,
            request_id,
            command_identity,
            **_kwargs,
        ):
            self.control = dict(command_identity)
            self.started.set()
            assert self.release.wait(timeout=1.0)
            assert self.control is not None
            self.receipt = {
                **self.control,
                "cleanup_owner_id": (
                    f"physical-live-route:{live_graph.deployment_id}"
                ),
                "node_ids": ["node-a", "node-b"],
            }
            return InferenceResult(request_id=request_id, token_ids=())

        def update_publisher_generation(
            self,
            request_id,
            *,
            expected_generation,
            new_generation,
            route_identity,
        ):
            assert route_identity["request_id"] == request_id
            assert self.control is not None
            assert self.control["publisher_generation"] == expected_generation
            self.control["publisher_generation"] = new_generation
            return True

        def request_cleanup_receipt_scoped(self, _request_id, **_identity):
            return self.receipt

        def release_request_scoped(self, _request_id, **_identity):
            self.receipt = None

    route = GenerationRoute()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    request = request_factory("publisher-generation-route")
    port.admit(
        request,
        Sink(),
        qualification_binding=Qualification(),
        publisher_generation=1,
    )
    assert route.started.wait(timeout=1.0)
    assert port.update_publisher_generation(
        request.request_id,
        expected_generation=1,
        new_generation=2,
    ) is True
    route.release.set()
    _wait(lambda: port.request_status(request.request_id) == "COMPLETED")
    snapshot = port._commands.snapshot(request.request_id, request_attempt=1)
    assert snapshot[0].identity.publisher_generation == 2
    assert snapshot[0].cleanup_result is not None
    port.release_request(request.request_id)
    port.close()


def test_missing_cleanup_receipt_blocks_terminal_and_m16_completion(
    live_graph, request_factory
) -> None:
    class Qualification:
        qualification_digest = "sha256:" + "b" * 64

    class MissingReceiptRoute:
        is_simulated = False

        def is_alive(self) -> bool:
            return True

        def infer(self, _tokens, *, request_id, sink, **_kwargs):
            sink.emit(0, 19)
            return InferenceResult(request_id=request_id, token_ids=(19,))

        def request_cleanup_receipt(self, _request_id):
            return None

        def release_request(self, _request_id):
            raise AssertionError("unproven cleanup must retain route ownership")

    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=MissingReceiptRoute(),
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    request = request_factory("cleanup-proof-missing")
    port.admit(request, Sink(), qualification_binding=Qualification())
    assert port.decode_one(request.request_id)
    _wait(lambda: port.request_status(request.request_id) == "TERMINAL_BLOCKED")

    snapshot = port._commands.snapshot(request.request_id, request_attempt=1)
    assert len(snapshot) == 1
    assert snapshot[0].cleanup_result is None
    assert snapshot[0].terminal is None
    assert coordinator.phase(request.request_id) == "cleanup"

    port.release_request(request.request_id)
    assert not port.is_idle()
    with pytest.raises(RuntimeError, match="router_port_shutdown_cleanup_unproven"):
        port.close()


def test_normal_completion_authorizes_generation_before_cleanup_and_terminal(
    live_graph, request_factory
) -> None:
    class Qualification:
        qualification_digest = "sha256:" + "c" * 64

    class OwnerScopedRoute:
        is_simulated = False

        def __init__(self) -> None:
            self.receipt = None
            self.cleanup_observed = False

        def is_alive(self) -> bool:
            return True

        def infer(
            self,
            _tokens,
            *,
            request_id,
            sink,
            command_identity,
            authorize_cleanup,
            **_kwargs,
        ):
            sink.emit(0, 31)
            deadline = authorize_cleanup(time.monotonic() + 2.0)
            assert deadline > time.monotonic()
            self.cleanup_observed = True
            self.receipt = {
                **command_identity,
                "cancellation_generation": 1,
                "cleanup_owner_id": (
                    f"physical-live-route:{command_identity['deployment_id']}"
                ),
                "node_ids": ["node-a", "node-b"],
            }
            return InferenceResult(request_id=request_id, token_ids=(31,))

        def request_cleanup_receipt_scoped(self, _request_id, **_identity):
            assert self.cleanup_observed is True
            return self.receipt

        def release_request_scoped(self, _request_id, **_identity):
            self.receipt = None

    route = OwnerScopedRoute()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    request = request_factory("completion-cleanup-authority")
    port.admit(request, Sink(), qualification_binding=Qualification())
    assert port.decode_one(request.request_id)
    _wait(lambda: port.request_status(request.request_id) != "DECODING")
    assert port.request_status(request.request_id) == "COMPLETED", (
        port._pending[request.request_id].terminal_blocked_reason
    )
    snapshot = port._commands.snapshot(request.request_id, request_attempt=1)
    assert snapshot[0].identity.cancellation_generation == 1
    assert snapshot[0].cleanup_result is not None
    assert snapshot[0].terminal is not None
    assert coordinator.phase(request.request_id) == "completed"
    port.release_request(request.request_id)
    port.close()


def test_lock_inversion_fails_before_physical_command(
    live_graph, request_factory
) -> None:
    detector = LockOrderDetector()
    physical_commands: list[str] = []

    class Route(FakeLiveRoute):
        def infer(self, *args, request_id, **kwargs):
            physical_commands.append(request_id)
            return super().infer(*args, request_id=request_id, **kwargs)

    route = Route(scripted_tokens=(17,))
    route.open()
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=build_live_m16_runtime(live_graph),
        lock_order_detector=detector,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    with detector.scope("transport", owner_id="transport-owner"):
        with pytest.raises(Exception) as caught:
            port.admit(request_factory("inverted-request"), Sink())

    assert getattr(caught.value, "code", None) == "lock_order_inversion"
    assert physical_commands == []
    incidents = detector.incidents()
    assert len(incidents) == 1
    assert incidents[0].held_scope == "transport"
    assert incidents[0].requested_scope == "deployment"
    assert incidents[0].outcome == "rejected_before_physical_command"
    assert "transport-owner" not in repr(incidents[0])
    port.close()


def test_queue_saturation_returns_bounded_backpressure_without_leak(
    live_graph, request_factory
) -> None:
    config = RouterConfig(
        maximum_pending_hops=1,
        maximum_pending_bytes=1 << 20,
        reservation_lease_seconds=3_600.0,
    )
    coordinator = build_live_m16_runtime(live_graph, config=config)
    coordinator.admit(
        request_factory("queue-owner"),
        workload_profile_id="interactive_chat_v1",
    )
    with pytest.raises(Exception) as caught:
        coordinator.admit(
            request_factory("queue-rejected"),
            workload_profile_id="interactive_chat_v1",
        )

    assert getattr(caught.value, "code", None) == "queue_full"
    status = coordinator.status()
    assert status["queue"]["depth"] == 1
    assert all(
        item["request_id"] != "queue-rejected" for item in status["requests"]
    )
    assert any(
        item["kind"] == "backpressure"
        and item["request_id"] == "queue-rejected"
        for item in status["incidents"]
    )
    assert coordinator.cancel("queue-owner") is True
    assert all(item["active_reservations"] == 0 for item in coordinator.status()["placements"])


def test_worker_exit_fails_only_owned_request_and_releases_resources(
    live_graph, request_factory
) -> None:
    class Route(FakeLiveRoute):
        def infer(self, token_ids, *, request_id, sink, **kwargs):
            if request_id == "worker-crash":
                raise RuntimeError("bounded worker failure")
            sink.emit(0, 31)
            return InferenceResult(request_id=request_id, token_ids=(31,))

    route = Route(scripted_tokens=())
    route.open()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    failed = port.admit(request_factory("worker-crash"), Sink())
    healthy = port.admit(request_factory("worker-survivor"), Sink())
    _wait(lambda: port.request_status(failed) == "FAILED")
    assert port.decode_one(healthy)
    _wait(lambda: port.request_status(healthy) == "COMPLETED")
    port.release_request(failed)
    port.release_request(healthy)
    _wait(port.is_idle)
    assert all(item["active_reservations"] == 0 for item in coordinator.status()["placements"])
    port.close()


def test_shutdown_interrupts_joins_and_returns_all_counters_to_zero(
    live_graph, request_factory
) -> None:
    started = threading.Event()

    class Route(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            request_id,
            sink,
            cancel_requested=None,
            **kwargs,
        ):
            started.set()
            while cancel_requested is None or not cancel_requested():
                time.sleep(0.005)
            raise InferenceCancelled("inference_cancelled")

    route = Route(scripted_tokens=())
    route.open()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route,
        execution_graph=live_graph,
        runtime_coordinator=coordinator,
    )

    class Sink:
        def emit(self, _index, _token):
            return None

    port.admit(request_factory("shutdown-owned"), Sink())
    assert started.wait(timeout=1.0)
    port.close(timeout_seconds=4.0)

    assert port.is_idle()
    status = coordinator.status()
    assert status["queue"]["depth"] == 0
    assert status["queue"]["active_request_ids"] == []
    assert all(item["active_reservations"] == 0 for item in status["placements"])
