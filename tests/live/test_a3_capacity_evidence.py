from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

from mycelium_qualification.signing import generate_ed25519_signer


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_a3_capacity_evidence.py"
SPEC = importlib.util.spec_from_file_location("build_a3_capacity_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _digest(value: object) -> str:
    return MODULE._digest(value)


def _path(src: str, dst: str, now: int) -> dict:
    return {
        "protocol": "mycelium.transport_path_observation.v1",
        "local_node_id": src,
        "local_endpoint_id": f"endpoint-{src}",
        "remote_node_id": dst,
        "remote_endpoint_id": f"endpoint-{dst}",
        "connection_generation": 1,
        "path_class": "direct",
        "relay_identity": None,
        "relay_region": None,
        "cold_rtt_ms": 10.0,
        "warm_rtt_ms": 5.0 if src < dst else 7.0,
        "observed_goodput_Bps": 10_000_000.0,
        "jitter_ms": 0.25,
        "loss_ratio": 0.0,
        "sample_count": 8,
        "connections_opened": 1,
        "frames_sent": 8,
        "reconnect_count": 0,
        "selected_path_changes": 1,
        "measurement_source": "iroh_activation_plane",
        "measured_at_unix_ms": now - 1_000,
        "fresh_until_unix_ms": now + 60_000,
        "exclusions": [],
    }


def _snapshot(node_id: str, peers: tuple[str, ...], now: int, *, thermal=None) -> dict:
    signer = generate_ed25519_signer(endpoint_id=f"endpoint-{node_id}")
    resources = {
        "protocol": "mycelium.host_resource_snapshot.v1",
        "observed_at_unix_ms": now - 500,
        "valid_until_unix_ms": now + 60_000,
        "backend": "mlx",
        "supported_architectures": ["qwen2", "qwen3"],
        "supported_dtypes": ["float32"],
        "supported_quantizations": ["int8-weight-only"],
        "supported_decode_modes": ["complete_context_replay"],
        "decode_modes_by_architecture": {
            "qwen2": ["complete_context_replay"],
            "qwen3": ["complete_context_replay"],
        },
        "runtime_build_digest": "sha256:" + "1" * 64,
        "available_memory_bytes": 8_000_000_000,
        "rss_bytes": 100_000_000,
        "swap_used_bytes": 0,
        "disk_free_bytes": 20_000_000_000,
        "disk_total_bytes": 40_000_000_000,
        "cached_content_digests": [],
        "thermal_state": thermal,
        "power_state": "external",
        "route_ready": False,
    }
    resources["resource_digest"] = _digest(resources)
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "snapshot",
        "monotonic_ns": 1,
        "node_id": node_id,
        "details": {
            "capture_protocol": "mycelium.member_capacity_snapshot.v1",
            "host_resources": resources,
            "transport": {
                "transport_path_observations": [
                    _path(node_id, peer, now) for peer in peers
                ]
            },
        },
    }
    return {
        "observation": observation,
        "signature": signer.sign(observation),
        "verification_key": signer.public_key_record(),
    }


def _calibration(nodes: tuple[str, ...]) -> dict:
    return {
        "protocol": "mycelium.a3_capacity_calibration.v1",
        "records": [
            {
                "node_id": node,
                "source_evidence_digest": "sha256:" + str(index + 2) * 64,
                "prefill_ms_per_layer_token": 0.01 + index,
                "decode_ms_per_layer_token": 0.02 + index,
                "memory_bandwidth_Bps": 1_000_000_000.0,
                "spill_bandwidth_Bps": 1_000_000_000.0,
            }
            for index, node in enumerate(nodes)
        ],
    }


def test_dynamic_two_node_capacity_input_binds_signed_resources_and_links() -> None:
    now = 1_000_000
    nodes = ("node-a", "node-b")
    snapshots = [
        _snapshot(node, tuple(peer for peer in nodes if peer != node), now)
        for node in nodes
    ]

    live, summary = MODULE.build_live_observations(
        snapshots=snapshots,
        calibration=_calibration(nodes),
        entry_node_id="node-b",
        now_unix_ms=now,
    )

    assert summary["eligible_node_ids"] == ["node-a", "node-b"]
    assert summary["opened_order"] == ["node-b", "node-a"]
    assert live["placement"]["snapshot_generation"] == now - 500
    assert len(live["signed_snapshots"]) == 2
    assert live["topology"]["measurement_source"] == "iroh_activation_plane"
    assert live["placement"]["route_ready"] is False


def test_pressure_exclusion_is_recorded_and_remaining_set_is_dynamic() -> None:
    now = 1_000_000
    nodes = ("node-a", "node-b", "node-c")
    snapshots = [
        _snapshot(
            node,
            tuple(peer for peer in nodes if peer != node),
            now,
            thermal="serious" if node == "node-c" else None,
        )
        for node in nodes
    ]

    live, summary = MODULE.build_live_observations(
        snapshots=snapshots,
        calibration=_calibration(nodes),
        entry_node_id="node-a",
        now_unix_ms=now,
    )

    assert summary["eligible_node_ids"] == ["node-a", "node-b"]
    assert summary["excluded_nodes"] == [
        {"node_id": "node-c", "reason": "thermal_pressure:serious"}
    ]
    assert {item["observation"]["node_id"] for item in live["signed_snapshots"]} == {
        "node-a",
        "node-b",
    }
    evidence = MODULE.assemble(live)
    assert {
        (edge["src"], edge["dst"]) for edge in evidence["directed_edges"]
    } == {("node-a", "node-b"), ("node-b", "node-a")}


def test_signed_snapshot_cannot_claim_another_nodes_transport_observation() -> None:
    now = 1_000_000
    snapshot = _snapshot("node-a", ("node-b",), now)
    path = snapshot["observation"]["details"]["transport"][
        "transport_path_observations"
    ][0]
    path["local_node_id"] = "node-b"
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    snapshot["signature"] = signer.sign(snapshot["observation"])
    snapshot["verification_key"] = signer.public_key_record()

    with pytest.raises(ValueError, match="not owned by its signer"):
        MODULE._verified_node(snapshot)


def test_invalid_signature_or_stale_path_fails_closed() -> None:
    now = 1_000_000
    nodes = ("node-a", "node-b")
    snapshots = [
        _snapshot(node, tuple(peer for peer in nodes if peer != node), now)
        for node in nodes
    ]
    bad_signature = copy.deepcopy(snapshots)
    bad_signature[0]["observation"]["monotonic_ns"] = 2
    with pytest.raises(MODULE.A3CapacityEvidenceError, match="snapshot_invalid"):
        MODULE.build_live_observations(
            snapshots=bad_signature,
            calibration=_calibration(nodes),
            entry_node_id="node-a",
            now_unix_ms=now,
        )

    stale = [copy.deepcopy(item) for item in snapshots]
    for item in stale:
        for path in item["observation"]["details"]["transport"][
            "transport_path_observations"
        ]:
            path["fresh_until_unix_ms"] = now - 1
        signer = generate_ed25519_signer(
            endpoint_id=f"endpoint-{item['observation']['node_id']}"
        )
        # A fresh valid signer is not the declared key, so retain this case solely for
        # topology staleness by rebuilding the signed envelope.
        item["signature"] = signer.sign(item["observation"])
        item["verification_key"] = signer.public_key_record()
    with pytest.raises(MODULE.A3CapacityEvidenceError, match="topology_invalid"):
        MODULE.build_live_observations(
            snapshots=stale,
            calibration=_calibration(nodes),
            entry_node_id="node-a",
            now_unix_ms=now,
        )
