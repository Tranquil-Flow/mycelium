from collections import deque
import threading
from types import SimpleNamespace

import pytest

import mycelium_live.route as live_route_module
from mycelium_live.route import FakeLiveRoute, PhysicalLiveRoute


class RecordingSink:
    def __init__(self):
        self.tokens = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


def test_route_is_not_alive_before_open():
    route = FakeLiveRoute(scripted_tokens=(1, 2, 3))
    assert route.is_alive() is False


def test_infer_emits_each_token_and_route_stays_alive():
    route = FakeLiveRoute(scripted_tokens=(1, 2, 3))
    route.open()
    sink = RecordingSink()
    result = route.infer(
        (15496, 11), max_new_tokens=3, request_id="request-a", sink=sink
    )
    assert sink.tokens == [(0, 1), (1, 2), (2, 3)]
    assert result.token_ids == (1, 2, 3)
    assert route.is_alive() is True


def test_second_request_reuses_open_route_and_advances_counters():
    route = FakeLiveRoute(scripted_tokens=(1, 2, 3))
    route.open()
    sink = RecordingSink()
    route.infer((1,), max_new_tokens=3, request_id="request-a", sink=sink)
    first = route.counters().frames_sent
    route.infer((2,), max_new_tokens=3, request_id="request-b", sink=sink)
    assert route.counters().frames_sent > first


def test_public_status_is_prompt_free_and_reports_counter_delta() -> None:
    route = FakeLiveRoute(scripted_tokens=(1, 2, 3))
    route.open()
    private_prompt_tokens = (999_001, 999_002)
    route.infer(
        private_prompt_tokens,
        max_new_tokens=3,
        request_id="private-request-id",
        sink=RecordingSink(),
    )

    status = route.public_status()
    assert status["protocol"] == "mycelium.live_route_status.v1"
    assert status["counters"]["frames_sent"] == 3
    assert status["recent_inferences"][-1]["context_tokens"] == 2
    assert status["recent_inferences"][-1]["peer_counter_deltas"] == [
        {
            "node_id": "fake-node",
            "frames_sent": 3,
            "frames_received": 3,
            "applied_operation_count": 3,
        }
    ]
    serialized = repr(status)
    assert "private-request-id" not in serialized
    assert "999001" not in serialized


def test_infer_before_open_is_rejected():
    route = FakeLiveRoute(scripted_tokens=(1,))
    with pytest.raises(RuntimeError, match="route_not_open"):
        route.infer((1,), max_new_tokens=1, request_id="r", sink=RecordingSink())


def test_close_makes_route_permanently_not_alive():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    route.close()


def test_physical_route_initializes_optional_public_projections(monkeypatch) -> None:
    graph = SimpleNamespace(topology_version=1)
    monkeypatch.setattr(
        live_route_module,
        "execution_graph_from_document",
        lambda _document: graph,
    )
    route = PhysicalLiveRoute(
        controller=SimpleNamespace(peers=()),
        endpoints={},
        run_plan={
            "nodes": [
                {"node_id": "node-0", "configure": {"graph": {}}},
            ],
        },
    )

    assert route._placement_projection is None
    assert route._topology_projection is None
    assert route._workload_comparison is None
    assert route._model_operation is None
    assert route.is_alive() is False
    route.close()


def test_physical_cancellation_cleanup_requires_every_peer_release() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._sessions = {"node-0": object(), "node-1": object()}
    released = {
        "details": {
            "runtime": {"active_state_count": 0},
            "transport_pending_delivery_count": 0,
            "transport_cancellation_cleanup_complete": True,
        }
    }
    still_active = {
        "details": {
            "runtime": {"active_state_count": 1},
            "transport_pending_delivery_count": 0,
            "transport_cancellation_cleanup_complete": False,
        }
    }
    route._last_snapshots = {"node-0": released, "node-1": still_active}
    assert route._cancellation_cleanup_complete() is False
    assert route._cancellation_cleanup_complete(frozenset({"node-0"})) is True

    route._last_snapshots["node-1"] = released
    assert route._cancellation_cleanup_complete() is True


def test_physical_route_accepts_eos_completion_without_visible_token() -> None:
    class Session:
        def send(self, *, command_id, command, payload):
            del command_id, payload
            if command == "infer_start":
                return {
                    "details": {
                        "status": "DECODING",
                        "output": {"token_indexes": [0], "token_ids": [101]},
                    }
                }
            assert command == "infer_decode"
            return {
                "details": {
                    "status": "COMPLETED",
                    "output": {"token_indexes": [0], "token_ids": [101]},
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.Lock()
    route._fatal = None
    route._plan = {
        "entry_node_id": "node-0",
        "request": {
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "a" * 64,
        },
    }
    route._sessions = {"node-0": Session()}
    route._stop_token_ids = frozenset()
    route._request_outputs = {}
    route._request_inputs = {}
    route._request_limits = {}
    route._request_entry_nodes = {}
    route._recent_inferences = deque(maxlen=16)
    route.is_alive = lambda: True
    route._command_id = lambda node_id, action: f"{node_id}-{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._peer_counters = lambda: {
        "node-0": {
            "frames_sent": 0,
            "frames_received": 0,
            "applied_operation_count": 0,
        }
    }
    route._snapshot_all = lambda: None
    sink = RecordingSink()

    result = route.infer((7, 8), max_new_tokens=4, request_id="request-eos", sink=sink)

    assert result.token_ids == (101,)
    assert sink.tokens == [(0, 101)]
    assert route._fatal is None


def test_physical_replica_qualification_proves_distinct_overlapping_tracks() -> None:
    class Session:
        def __init__(self, *, placement_id: str, excluded_placement_id: str) -> None:
            self.started = 0
            self.placement_id = placement_id
            self.excluded_placement_id = excluded_placement_id

        def send(self, *, command_id, command, payload):
            del command_id
            if command == "infer_start":
                assert payload["excluded_placement_ids"] == [
                    self.excluded_placement_id
                ]
                index = self.started
                self.started += 1
                return {
                    "details": {
                        "status": "DECODING",
                        "output": {"token_indexes": [0], "token_ids": [10 + index]},
                        "path": {
                            "path_id": f"path-{self.placement_id}-{index}",
                            "path_attempt": 0,
                            "placement_ids": [self.placement_id],
                        },
                    }
                }
            assert command == "infer_decode"
            index = int(payload["request_id"].rsplit("-", 1)[1])
            return {
                "details": {
                    "status": "COMPLETED",
                    "output": {
                        "token_indexes": [0, 1],
                        "token_ids": [10 + index, 20 + index],
                    },
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.Lock()
    route._plan = {
        "entry_node_id": "node-0",
        "request": {
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "a" * 64,
        },
    }
    route._graph = SimpleNamespace(
        deployment_id="deployment-m18",
        stages=(
            SimpleNamespace(
                placements=(
                    SimpleNamespace(node_id="node-0", placement_id="p0"),
                    SimpleNamespace(node_id="node-1", placement_id="p1"),
                )
            ),
        ),
    )
    route._sessions = {
        "node-0": Session(placement_id="p0", excluded_placement_id="p1"),
        "node-1": Session(placement_id="p1", excluded_placement_id="p0"),
    }
    route._request_outputs = {}
    route._request_inputs = {}
    route._request_limits = {}
    route._request_entry_nodes = {}
    route.is_alive = lambda: True
    route._command_id = lambda node_id, action: f"{node_id}-{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._peer_counters = lambda: {
        "node-0": {
            "frames_sent": 0,
            "frames_received": 0,
            "applied_operation_count": 0,
        },
        "node-1": {
            "frames_sent": 0,
            "frames_received": 0,
            "applied_operation_count": 0,
        },
    }
    route._snapshot_all = lambda: None

    evidence = route.qualify_replica_concurrency(
        ((7, 8), (9, 10)),
        max_new_tokens=2,
        request_id_prefix="m18-physical",
    )

    assert evidence["protocol"] == "mycelium.physical_replica_concurrency.v1"
    assert evidence["overlapping_at_admission"] is True
    assert evidence["distinct_tracks"] is True
    assert [item["placement_ids"] for item in evidence["requests"]] == [
        ["p0"],
        ["p1"],
    ]
    assert all("output_digest" in item for item in evidence["requests"])
