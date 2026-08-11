from __future__ import annotations

import copy
import threading
import time

import pytest

from mycelium_m16_runtime import (
    M16AdmissionError,
    M16RuntimeCoordinator,
    validate_m16_runtime_status,
)
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    Stage,
    StageCost,
)
from mycelium_router.fakes import ManualClock, SequenceIdSource
from mycelium_live.route import InferenceCancelled
from mycelium_live.router_port import LiveRouterPort


def _placement(identifier: str, node_id: str) -> Placement:
    return Placement(
        placement_id=identifier,
        node_id=node_id,
        replica_group_id=f"group-{identifier}",
        assignment_id=f"assignment-{identifier}",
        stage_signature=f"signature-{identifier}",
        load_proof_digest="sha256:" + ("1" if identifier == "placement-a" else "2") * 64,
        runtime_backend="mlx",
        runtime_endpoint=f"private-{identifier}",
    )


def _graph() -> ExecutionGraph:
    first = _placement("placement-a", "node-a")
    second = _placement("placement-b", "node-b")
    return ExecutionGraph(
        deployment_id="deployment-m16",
        deployment_epoch=16,
        topology_version=7,
        model_id="org/model",
        resolved_commit="a" * 40,
        manifest_digest="sha256:" + "3" * 64,
        entry_stage_id="stage-a",
        final_stage_id="stage-b",
        hidden_size=256,
        activation_bytes=512,
        token_envelope_bytes=8,
        stages=(
            Stage("stage-a", LayerRange(0, 4, 4), ("input_embedding", "decoder"), StageCost(1.0, 1.0, 64), (first,)),
            Stage("stage-b", LayerRange(4, 8, 4), ("decoder", "lm_head"), StageCost(1.0, 1.0, 64), (second,)),
        ),
        edges=(PlacementEdge("edge-forward", "placement-a", "placement-b", "link-ab"),),
        loopback_edges=(PlacementEdge("edge-loopback", "placement-b", "placement-a", "link-ba"),),
    )


def _states() -> dict[str, DeviceState]:
    return {
        node_id: DeviceState(node_id, 1, 100.0, "ALIVE", 1_000.0, 1.0, 1_000_000, 0, {}, {})
        for node_id in ("node-a", "node-b")
    }


def _request(identifier: str, qos: str = "interactive") -> RequestContext:
    return RequestContext(
        request_id=identifier,
        prompt_token_ids=tuple(range(16)),
        max_new_tokens=8,
        expected_new_tokens=8,
        qos_class=qos,
        admitted_at=100.0,
        target_ttft_ms=5_000.0,
        target_tpot_ms=2_000.0,
        target_tokens_per_second=0.5,
        sampling_seed=0,
        generation_config_digest="sha256:" + "4" * 64,
    )


def _coordinator(*, queue_items: int = 4, capacity_b: int = 1_000_000) -> tuple[M16RuntimeCoordinator, ManualClock]:
    clock = ManualClock(100.0)
    coordinator = M16RuntimeCoordinator(
        graph=_graph(),
        device_states=_states(),
        placement_capacities={
            "placement-a": {"memory_capacity_bytes": 1_000_000, "kv_capacity_bytes": 100_000, "workspace_capacity_bytes": 100_000, "maximum_reservations": 4},
            "placement-b": {"memory_capacity_bytes": capacity_b, "kv_capacity_bytes": 100_000, "workspace_capacity_bytes": 100_000, "maximum_reservations": 4},
        },
        workload_profiles={"interactive_chat_v1": "interactive", "sustained_batch_v1": "batch"},
        clock=clock,
        id_source=SequenceIdSource(),
        config=RouterConfig(
            maximum_pending_hops=queue_items,
            maximum_pending_bytes=1_024,
            reservation_lease_seconds=300.0,
        ),
    )
    return coordinator, clock


def test_admission_commits_complete_bound_path_before_dispatch() -> None:
    coordinator, _ = _coordinator()
    manifest = coordinator.admit(_request("request-one"), workload_profile_id="interactive_chat_v1")

    assert manifest.topology_version == 7
    assert [hop.placement_id for hop in manifest.ordered_hops] == ["placement-a", "placement-b"]
    status = coordinator.status()
    request = status["requests"][0]
    assert request["phase"] == "queued"
    assert request["path_manifest_digest"].startswith("sha256:")
    assert request["reservation_count"] == 2
    assert all(item["reserved_memory_bytes"] > 0 for item in status["placements"])


def test_infeasible_complete_path_rejects_before_queue_and_releases_partial_reservations() -> None:
    coordinator, _ = _coordinator(capacity_b=1)
    with pytest.raises(M16AdmissionError, match="resource_unavailable"):
        coordinator.admit(_request("request-rejected"), workload_profile_id="interactive_chat_v1")

    status = coordinator.status()
    assert status["queue"]["depth"] == 0
    assert all(item["reserved_memory_bytes"] == 0 for item in status["placements"])
    assert status["incidents"][0]["kind"] == "admission_rejected"


def test_qos_priority_protects_interactive_and_aging_prevents_batch_starvation() -> None:
    coordinator, clock = _coordinator()
    coordinator.admit(_request("batch-old", "batch"), workload_profile_id="sustained_batch_v1")
    coordinator.admit(_request("interactive-new"), workload_profile_id="interactive_chat_v1")
    assert coordinator.next_dispatch() == "interactive-new"
    coordinator.complete("interactive-new")

    coordinator.admit(_request("interactive-later"), workload_profile_id="interactive_chat_v1")
    clock.advance(101.0)
    assert coordinator.next_dispatch() == "batch-old"
    coordinator.complete("batch-old")
    assert coordinator.next_dispatch() == "interactive-later"


def test_queue_backpressure_and_cancel_release_every_resource() -> None:
    coordinator, _ = _coordinator(queue_items=1)
    coordinator.admit(_request("batch-one", "batch"), workload_profile_id="sustained_batch_v1")
    with pytest.raises(M16AdmissionError, match="queue_full"):
        coordinator.admit(_request("interactive-two"), workload_profile_id="interactive_chat_v1")
    assert coordinator.cancel("batch-one") is True

    status = coordinator.status()
    assert status["queue"]["depth"] == 0
    assert all(item["reserved_memory_bytes"] == 0 for item in status["placements"])
    assert any(item["kind"] == "backpressure" for item in status["incidents"])
    assert any(item["kind"] == "cancellation_cleanup" for item in status["incidents"])


def test_status_contract_is_closed_privacy_reduced_and_detached() -> None:
    coordinator, _ = _coordinator()
    coordinator.admit(_request("request-private"), workload_profile_id="interactive_chat_v1")
    document = coordinator.status()
    assert validate_m16_runtime_status(copy.deepcopy(document)) == document
    assert "prompt" not in repr(document).lower()
    assert "private-placement" not in repr(document)

    with pytest.raises(ValueError, match="shape"):
        validate_m16_runtime_status({**document, "surprise": True})
    private = copy.deepcopy(document)
    private["requests"][0]["prompt"] = "secret"
    with pytest.raises(ValueError, match="private|shape"):
        validate_m16_runtime_status(private)


def test_live_router_dispatches_interactive_ahead_of_queued_batch_and_cleans_cancel() -> None:
    coordinator, _ = _coordinator()

    class ControlledRoute:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.selected: dict[str, tuple[str, ...]] = {}
            self.gates: dict[str, threading.Event] = {}
            self.released: list[str] = []
            self.route_lock = threading.Lock()

        def is_alive(self) -> bool:
            return True

        def infer(
            self,
            _tokens,
            *,
            request_id,
            sink,
            cancel_requested,
            selected_placement_ids,
            **_kwargs,
        ):
            with self.route_lock:
                self.started.append(request_id)
                self.selected[request_id] = tuple(selected_placement_ids)
                gate = self.gates.setdefault(request_id, threading.Event())
                while not gate.wait(0.01):
                    if cancel_requested():
                        raise InferenceCancelled("cancelled")
                sink.emit(0, 1)

        def release_request(self, request_id: str) -> None:
            with self.route_lock:
                self.released.append(request_id)

    class Sink:
        def emit(self, _index: int, _token: int) -> None:
            return None

    route = ControlledRoute()
    port = LiveRouterPort(
        route=route,
        execution_graph=_graph(),
        runtime_coordinator=coordinator,
    )

    def wait_for(predicate) -> None:
        deadline = time.monotonic() + 2.0
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for live M16 dispatch")
            time.sleep(0.01)

    port.admit(_request("batch-active", "batch"), Sink(), workload_profile_id="sustained_batch_v1")
    wait_for(lambda: route.started == ["batch-active"])
    assert route.selected["batch-active"] == ("placement-a", "placement-b")
    port.admit(_request("batch-queued", "batch"), Sink(), workload_profile_id="sustained_batch_v1")
    port.admit(_request("interactive-queued"), Sink(), workload_profile_id="interactive_chat_v1")

    assert port.cancel("batch-queued") is True
    cleanup = threading.Thread(target=port.release_request, args=("batch-queued",))
    cleanup.start()

    route.gates["batch-active"].set()
    wait_for(lambda: route.started == ["batch-active", "interactive-queued"])
    status = port.runtime_status()
    assert status is not None
    cancelled = next(item for item in status["requests"] if item["request_id"] == "batch-queued")
    assert cancelled["terminal_state"] == "cancelled"
    assert cancelled["reservation_count"] == 0

    route.gates["interactive-queued"].set()
    wait_for(lambda: port.decode_one("interactive-queued"))
    wait_for(lambda: port.request_status("interactive-queued") == "COMPLETED")
    cleanup.join(timeout=1.0)
    assert not cleanup.is_alive()
