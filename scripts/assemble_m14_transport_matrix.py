#!/usr/bin/env python3
"""Normalize per-host sidecar telemetry into the frozen M14 matrix contract."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_topology_evidence import complete_directed_observation_matrix
from runtime_loader import canonical_json


def _object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(code) from exc
    if not isinstance(value, dict):
        raise RuntimeError(code)
    return value


def assemble(args: argparse.Namespace) -> dict[str, Any]:
    topology = _object(args.topology, "m14_probe_topology_invalid")
    nodes = topology.get("nodes")
    if topology.get("protocol") != "mycelium.qwen_live_topology.v1" or not isinstance(nodes, list) or len(nodes) < 3:
        raise RuntimeError("m14_probe_topology_invalid")
    endpoint_to_node = {
        node.get("endpoint_id"): node.get("node_id")
        for node in nodes
        if isinstance(node, dict)
    }
    if len(endpoint_to_node) != len(nodes) or not all(
        isinstance(endpoint, str) and isinstance(node, str)
        for endpoint, node in endpoint_to_node.items()
    ):
        raise RuntimeError("m14_probe_topology_invalid")
    observations = []
    seen_local: set[str] = set()
    for path in args.sidecar_observation:
        envelope = _object(path, "m14_probe_observation_invalid")
        local_endpoint = envelope.get("endpoint_id")
        local_node = endpoint_to_node.get(local_endpoint)
        raw = envelope.get("observations")
        if not isinstance(local_node, str) or local_node in seen_local or not isinstance(raw, list):
            raise RuntimeError("m14_probe_observation_invalid")
        seen_local.add(local_node)
        for item in raw:
            if not isinstance(item, dict):
                raise RuntimeError("m14_probe_observation_invalid")
            remote_endpoint = item.get("remote_endpoint_id")
            remote_node = endpoint_to_node.get(remote_endpoint)
            if not isinstance(remote_node, str):
                raise RuntimeError("m14_probe_observation_peer_invalid")
            measured_at = item.get("measured_at_unix_ms")
            if type(measured_at) is not int:
                raise RuntimeError("m14_probe_observation_time_invalid")
            observations.append(
                {
                    "protocol": "mycelium.transport_path_observation.v1",
                    "local_node_id": local_node,
                    "local_endpoint_id": local_endpoint,
                    "remote_node_id": remote_node,
                    "remote_endpoint_id": remote_endpoint,
                    "connection_generation": item.get("connection_generation"),
                    "path_class": item.get("path_class"),
                    "relay_identity": item.get("relay_identity"),
                    "relay_region": item.get("relay_region"),
                    "cold_rtt_ms": item.get("cold_rtt_ms"),
                    "warm_rtt_ms": item.get("warm_rtt_ms"),
                    "observed_goodput_Bps": item.get("observed_goodput_bps"),
                    "jitter_ms": item.get("jitter_ms"),
                    "loss_ratio": item.get("loss_ratio"),
                    "sample_count": item.get("sample_count"),
                    "connections_opened": item.get("connections_opened"),
                    "frames_sent": item.get("frames_sent"),
                    "reconnect_count": item.get("reconnect_count"),
                    "selected_path_changes": item.get("selected_path_changes"),
                    "measurement_source": "iroh_activation_plane",
                    "measured_at_unix_ms": measured_at,
                    "fresh_until_unix_ms": measured_at + 7_200_000,
                    "exclusions": ["path_transition_not_observed_within_budget"],
                }
            )
    node_ids = tuple(node["node_id"] for node in nodes)
    endpoint_ids = {node["node_id"]: node["endpoint_id"] for node in nodes}
    captured = int(time.time() * 1_000)
    complete_directed_observation_matrix(
        observations,
        node_ids=node_ids,
        endpoint_ids_by_node=endpoint_ids,
        now_unix_ms=captured,
    )
    document = {
        "protocol": "mycelium.m14_transport_matrix_input.v1",
        "captured_at_unix_ms": captured,
        "observations": sorted(
            observations,
            key=lambda item: (item["local_node_id"], item["remote_node_id"]),
        ),
    }
    args.output.write_text(canonical_json(document), encoding="utf-8")
    return {
        "output": str(args.output),
        "nodes": len(node_ids),
        "directed_edges": len(observations),
        "minimum_samples": min(item["sample_count"] for item in observations),
        "connections_opened": sum(item["connections_opened"] for item in observations),
        "frames_sent": sum(item["frames_sent"] for item in observations),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--sidecar-observation", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(assemble(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
