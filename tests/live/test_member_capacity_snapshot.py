from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "capture_member_capacity_snapshot.py"
)
SPEC = importlib.util.spec_from_file_location(
    "capture_member_capacity_snapshot", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _observations() -> dict:
    return {
        "endpoint_id": "local-endpoint",
        "observations": [
            {
                "remote_endpoint_id": "remote-endpoint",
                "connection_generation": 1,
                "path_class": "direct",
                "relay_identity": None,
                "relay_region": None,
                "cold_rtt_ms": 12.0,
                "warm_rtt_ms": 8.0,
                "observed_goodput_bps": 1000.0,
                "jitter_ms": 0.5,
                "loss_ratio": 0.0,
                "sample_count": 9,
                "connections_opened": 1,
                "frames_sent": 8,
                "reconnect_count": 0,
                "selected_path_changes": 1,
                "measured_at_unix_ms": 1_000,
            }
        ],
    }


def test_transport_projection_is_directed_closed_and_model_free() -> None:
    result = MODULE._transport_paths(
        _observations(),
        local_node_id="node-a",
        remote_nodes={"remote-endpoint": "node-b"},
    )

    assert result == [
        {
            "protocol": "mycelium.transport_path_observation.v1",
            "local_node_id": "node-a",
            "local_endpoint_id": "local-endpoint",
            "remote_node_id": "node-b",
            "remote_endpoint_id": "remote-endpoint",
            "connection_generation": 1,
            "path_class": "direct",
            "relay_identity": None,
            "relay_region": None,
            "cold_rtt_ms": 12.0,
            "warm_rtt_ms": 8.0,
            "observed_goodput_Bps": 1000.0,
            "jitter_ms": 0.5,
            "loss_ratio": 0.0,
            "sample_count": 9,
            "connections_opened": 1,
            "frames_sent": 8,
            "reconnect_count": 0,
            "selected_path_changes": 1,
            "measurement_source": "iroh_activation_plane",
            "measured_at_unix_ms": 1_000,
            "fresh_until_unix_ms": 7_201_000,
            "exclusions": [],
        }
    ]


def test_transport_projection_rejects_missing_or_duplicate_peer() -> None:
    with pytest.raises(MODULE.MemberCapacityError, match="matrix_incomplete"):
        MODULE._transport_paths(
            _observations(),
            local_node_id="node-a",
            remote_nodes={
                "remote-endpoint": "node-b",
                "unobserved-endpoint": "node-c",
            },
        )

    duplicated = _observations()
    duplicated["observations"].append(dict(duplicated["observations"][0]))
    with pytest.raises(MODULE.MemberCapacityError, match="observations_invalid"):
        MODULE._transport_paths(
            duplicated,
            local_node_id="node-a",
            remote_nodes={"remote-endpoint": "node-b"},
        )


def test_resource_lease_is_bounded_for_long_running_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE.time, "time", lambda: 1.0)
    monkeypatch.setattr(MODULE, "_host_available_memory_bytes", lambda: 1_000_000)
    monkeypatch.setattr(MODULE, "_process_rss_bytes", lambda: 1)
    monkeypatch.setattr(MODULE, "_host_swap_used_bytes", lambda: 0)
    monkeypatch.setattr(MODULE, "_host_thermal_state", lambda: "nominal")
    monkeypatch.setattr(MODULE, "_host_power_state", lambda: "external")

    resource = MODULE._host_resources(
        backend="numpy", artifact_root=tmp_path, valid_for_ms=3_600_000
    )

    assert resource["observed_at_unix_ms"] == 1_000
    assert resource["valid_until_unix_ms"] == 3_601_000
    with pytest.raises(MODULE.MemberCapacityError, match="capacity_runtime_invalid"):
        MODULE._host_resources(
            backend="numpy", artifact_root=tmp_path, valid_for_ms=3_600_001
        )
