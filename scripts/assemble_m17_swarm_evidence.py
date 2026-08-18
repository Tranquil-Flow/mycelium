#!/usr/bin/env python3
"""Verify live node signatures and freeze one M17 feasibility evidence generation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mycelium_model_catalog import (  # noqa: E402
    DirectedEdgeFeasibilityEvidence,
    NodeFeasibilityEvidence,
    SwarmFeasibilityEvidence,
)
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_qualification.signing import build_ed25519_verifier  # noqa: E402


PROTOCOL = "mycelium.swarm_feasibility_evidence.v1"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _verified_node(
    record: object,
) -> tuple[
    NodeFeasibilityEvidence,
    str,
    tuple[DirectedEdgeFeasibilityEvidence, ...],
]:
    signed = _mapping(record, "signed snapshot")
    if set(signed) != {"observation", "signature", "verification_key"}:
        raise ValueError("signed snapshot shape is invalid")
    observation = _mapping(signed["observation"], "snapshot observation")
    signature = _mapping(signed["signature"], "snapshot signature")
    verification_key = _mapping(signed["verification_key"], "snapshot verification key")
    verifier = build_ed25519_verifier((verification_key,))
    if not verifier(canonical_json_bytes(observation), dict(signature)):
        raise ValueError("snapshot signature verification failed")
    if observation.get("event") != "snapshot":
        raise ValueError("signed observation is not a snapshot")
    details = _mapping(observation.get("details"), "snapshot details")
    resources = _mapping(details.get("host_resources"), "host resources")
    if resources.get("protocol") != "mycelium.host_resource_snapshot.v1":
        raise ValueError("host resource protocol is invalid")
    detached_resources = dict(resources)
    resource_digest = detached_resources.pop("resource_digest", None)
    if resource_digest != _digest(detached_resources):
        raise ValueError("host resource digest mismatch")
    node_id = observation.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("snapshot node identity is invalid")
    evidence = NodeFeasibilityEvidence(
        node_id=node_id,
        observation_digest=str(signature["signed_statement_digest"]),
        signature_digest=_digest(signature),
        observed_at_unix_ms=int(resources["observed_at_unix_ms"]),
        valid_until_unix_ms=int(resources["valid_until_unix_ms"]),
        backend=str(resources["backend"]),
        supported_architectures=tuple(resources["supported_architectures"]),
        supported_dtypes=tuple(resources["supported_dtypes"]),
        supported_quantizations=tuple(resources["supported_quantizations"]),
        supported_decode_modes=tuple(resources["supported_decode_modes"]),
        decode_modes_by_architecture={
            str(architecture): tuple(modes)
            for architecture, modes in _mapping(
                resources.get("decode_modes_by_architecture", {}),
                "architecture decode modes",
            ).items()
        },
        runtime_build_digest=str(resources["runtime_build_digest"]),
        available_memory_bytes=int(resources["available_memory_bytes"]),
        rss_bytes=int(resources["rss_bytes"]),
        swap_used_bytes=int(resources["swap_used_bytes"]),
        disk_free_bytes=int(resources["disk_free_bytes"]),
        cached_content_digests=tuple(resources["cached_content_digests"]),
        thermal_state=resources["thermal_state"],
        power_state=resources["power_state"],
    )
    transport = _mapping(details.get("transport"), "transport evidence")
    path_observations = transport.get("transport_path_observations")
    if not isinstance(path_observations, list):
        raise ValueError("signed snapshot lacks directed transport observations")
    edges = []
    for value in path_observations:
        path = _mapping(value, "transport path observation")
        if path.get("protocol") != "mycelium.transport_path_observation.v1":
            raise ValueError("transport path observation protocol is invalid")
        if path.get("local_node_id") != node_id:
            raise ValueError("transport path observation is not owned by its signer")
        if (
            int(path["measured_at_unix_ms"]) <= 0
            or int(path["fresh_until_unix_ms"]) <= int(path["measured_at_unix_ms"])
            or float(path["observed_goodput_Bps"]) <= 0
        ):
            continue
        edges.append(
            DirectedEdgeFeasibilityEvidence(
                src=str(path["local_node_id"]),
                dst=str(path["remote_node_id"]),
                observation_digest=_digest(path),
                observed_at_unix_ms=int(path["measured_at_unix_ms"]),
                valid_until_unix_ms=int(path["fresh_until_unix_ms"]),
                goodput_Bps=float(path["observed_goodput_Bps"]),
                rtt_ms=float(path["warm_rtt_ms"]),
                jitter_ms=float(path["jitter_ms"]),
                loss_ratio=float(path["loss_ratio"]),
            )
        )
    return (
        evidence,
        str(verification_key["verification_key_digest"]),
        tuple(edges),
    )


def assemble(source: Mapping[str, Any]) -> dict[str, object]:
    if source.get("protocol") != "mycelium.live_swarm_resource_observations.v1":
        raise ValueError("live swarm observation protocol is invalid")
    placement = _mapping(source.get("placement"), "placement")
    topology = _mapping(source.get("topology"), "topology")
    snapshots = source.get("signed_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("live swarm observations require signed snapshots")
    verified = [_verified_node(record) for record in snapshots]
    nodes = tuple(sorted((item[0] for item in verified), key=lambda item: item.node_id))
    node_ids = {node.node_id for node in nodes}
    verification_keys = sorted(item[1] for item in verified)
    edges = tuple(
        sorted(
            (
                edge
                for item in verified
                for edge in item[2]
                if edge.src in node_ids and edge.dst in node_ids
            ),
            key=lambda item: (item.src, item.dst),
        )
    )
    decision = _mapping(topology.get("decision"), "topology decision")
    opened_order = decision.get("opened_order")
    if (
        not isinstance(opened_order, list)
        or not opened_order
        or not all(isinstance(node_id, str) and node_id for node_id in opened_order)
        or len(opened_order) != len(set(opened_order))
        or set(opened_order) != {node.node_id for node in nodes}
    ):
        raise ValueError("topology opened order does not bind the signed node set")
    required_pairs = {
        (opened_order[index], opened_order[index + 1])
        for index in range(len(opened_order) - 1)
    }
    if len(opened_order) > 1:
        required_pairs.add((opened_order[-1], opened_order[0]))
    observed_pairs = {(edge.src, edge.dst) for edge in edges}
    missing_pairs = sorted(required_pairs - observed_pairs)
    if missing_pairs:
        rendered = ",".join(f"{src}->{dst}" for src, dst in missing_pairs)
        raise ValueError(f"signed snapshots lack required route edges: {rendered}")
    placement_snapshot_generation = placement.get("snapshot_generation")
    if (
        not isinstance(placement_snapshot_generation, int)
        or isinstance(placement_snapshot_generation, bool)
        or placement_snapshot_generation <= 0
    ):
        raise ValueError("placement snapshot generation is invalid")
    observed_at = max(
        [node.observed_at_unix_ms for node in nodes]
        + [edge.observed_at_unix_ms for edge in edges]
    )
    valid_until = min(
        [node.valid_until_unix_ms for node in nodes]
        + [edge.valid_until_unix_ms for edge in edges]
    )
    generation = observed_at
    signature_set_digest = _digest(sorted(node.signature_digest for node in nodes))
    verification_key_set_digest = _digest(verification_keys)
    body = {
        "protocol": PROTOCOL,
        "generation": generation,
        "signature_set_digest": signature_set_digest,
        "verification_key_set_digest": verification_key_set_digest,
        "placement_snapshot_generation": placement_snapshot_generation,
        "observed_at_unix_ms": observed_at,
        "valid_until_unix_ms": valid_until,
        "nodes": [node.projection() for node in nodes],
        "directed_edges": [edge.projection() for edge in edges],
        "placement_digest": _digest(placement),
        "topology_digest": _digest(topology),
        "route_ready": False,
    }
    evidence_digest = _digest(body)
    evidence = SwarmFeasibilityEvidence(
        generation=generation,
        evidence_digest=evidence_digest,
        signature_set_digest=signature_set_digest,
        verification_key_set_digest=verification_key_set_digest,
        placement_snapshot_generation=placement_snapshot_generation,
        placement_digest=str(body["placement_digest"]),
        topology_digest=str(body["topology_digest"]),
        observed_at_unix_ms=observed_at,
        valid_until_unix_ms=valid_until,
        nodes=nodes,
        directed_edges=edges,
    )
    return {
        **body,
        "evidence_digest": evidence.evidence_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = assemble(_load(args.live_observations))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "protocol": document["protocol"],
        "generation": document["generation"],
        "node_count": len(document["nodes"]),
        "edge_count": len(document["directed_edges"]),
        "valid_until_unix_ms": document["valid_until_unix_ms"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
