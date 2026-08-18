#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Capture one signed model-free member resource and directed-link snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_adapters import ADAPTERS
from mycelium_node.identity import load_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from physical_inference_node import (
    NODE_OBSERVATION_PROTOCOL,
    _host_available_memory_bytes,
    _host_power_state,
    _host_swap_used_bytes,
    _host_thermal_state,
    _process_rss_bytes,
    _runtime_build_digest,
)


PROTOCOL = "mycelium.member_capacity_snapshot.v1"


class MemberCapacityError(RuntimeError):
    """A bounded capacity-snapshot input or capture failure."""


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemberCapacityError("capacity_input_invalid") from exc
    if not isinstance(value, dict):
        raise MemberCapacityError("capacity_input_invalid")
    return value


def _write_private(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _transport_paths(
    observations: Mapping[str, Any],
    *,
    local_node_id: str,
    remote_nodes: Mapping[str, Any],
) -> list[dict[str, Any]]:
    local_endpoint = observations.get("endpoint_id")
    raw = observations.get("observations")
    if (
        not isinstance(local_endpoint, str)
        or not local_endpoint
        or not isinstance(raw, list)
        or not raw
    ):
        raise MemberCapacityError("capacity_transport_observations_invalid")
    result = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise MemberCapacityError("capacity_transport_observations_invalid")
        remote_endpoint = item.get("remote_endpoint_id")
        remote_node = remote_nodes.get(remote_endpoint)
        measured_at = item.get("measured_at_unix_ms")
        goodput = item.get("observed_goodput_bps")
        if (
            not isinstance(remote_endpoint, str)
            or not isinstance(remote_node, str)
            or not remote_node
            or remote_node == local_node_id
            or remote_node in seen
            or type(measured_at) is not int
            or measured_at <= 0
            or not isinstance(goodput, (int, float))
            or isinstance(goodput, bool)
            or float(goodput) <= 0
        ):
            raise MemberCapacityError("capacity_transport_observations_invalid")
        seen.add(remote_node)
        result.append(
            {
                "protocol": "mycelium.transport_path_observation.v1",
                "local_node_id": local_node_id,
                "local_endpoint_id": local_endpoint,
                "remote_node_id": remote_node,
                "remote_endpoint_id": remote_endpoint,
                "connection_generation": item.get("connection_generation"),
                "path_class": item.get("path_class"),
                "relay_identity": item.get("relay_identity"),
                "relay_region": item.get("relay_region"),
                "cold_rtt_ms": item.get("cold_rtt_ms"),
                "warm_rtt_ms": item.get("warm_rtt_ms"),
                "observed_goodput_Bps": float(goodput),
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
                "exclusions": [],
            }
        )
    if set(seen) != set(remote_nodes.values()):
        raise MemberCapacityError("capacity_transport_matrix_incomplete")
    return sorted(result, key=lambda item: item["remote_node_id"])


def _host_resources(
    *, backend: str, artifact_root: Path, valid_for_ms: int = 120_000
) -> dict[str, Any]:
    if (
        backend not in {"mlx", "numpy"}
        or not artifact_root.is_dir()
        or type(valid_for_ms) is not int
        or not 120_000 <= valid_for_ms <= 3_600_000
    ):
        raise MemberCapacityError("capacity_runtime_invalid")
    architectures = sorted(
        adapter.architecture
        for adapter in ADAPTERS.values()
        if backend in adapter.runtime_backends
    )
    decode = {
        architecture: (
            ["complete_context_replay", "stage_local_kv"]
            if backend == "mlx" or architecture in {"qwen2", "qwen3"}
            else ["complete_context_replay"]
        )
        for architecture in architectures
    }
    object_root = artifact_root / ".mycelium" / "objects" / "sha256"
    cached = (
        sorted(
            f"sha256:{path.name}"
            for path in object_root.iterdir()
            if path.is_file()
            and len(path.name) == 64
            and all(character in "0123456789abcdef" for character in path.name)
        )
        if object_root.is_dir()
        else []
    )
    if len(cached) > 4096:
        raise MemberCapacityError("capacity_cache_inventory_too_large")
    disk = shutil.disk_usage(artifact_root)
    now = int(time.time() * 1_000)
    value: dict[str, Any] = {
        "protocol": "mycelium.host_resource_snapshot.v1",
        "observed_at_unix_ms": now,
        "valid_until_unix_ms": now + valid_for_ms,
        "backend": backend,
        "supported_architectures": architectures,
        "supported_dtypes": ["bfloat16", "float16", "float32"],
        "supported_quantizations": [
            "bfloat16",
            "float16",
            "float32",
            "int8-weight-only",
            "none",
        ],
        "supported_decode_modes": sorted(
            {mode for modes in decode.values() for mode in modes}
        ),
        "decode_modes_by_architecture": decode,
        "runtime_build_digest": _runtime_build_digest(backend),
        "available_memory_bytes": _host_available_memory_bytes(),
        "rss_bytes": _process_rss_bytes(),
        "swap_used_bytes": _host_swap_used_bytes(),
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "cached_content_digests": cached,
        "thermal_state": _host_thermal_state(),
        "power_state": _host_power_state(),
        "route_ready": False,
    }
    value["resource_digest"] = _digest(value)
    return value


def capture(
    *,
    node_id: str,
    identity_file: Path,
    endpoint_id: str,
    backend: str,
    artifact_root: Path,
    observations: Mapping[str, Any],
    remote_nodes: Mapping[str, Any],
    resource_valid_for_ms: int = 120_000,
) -> dict[str, Any]:
    if not isinstance(node_id, str) or not node_id:
        raise MemberCapacityError("capacity_node_identity_invalid")
    signer = load_node_signer(identity_file, endpoint_id=endpoint_id)
    statement = {
        "protocol": NODE_OBSERVATION_PROTOCOL,
        "event": "snapshot",
        "monotonic_ns": time.monotonic_ns(),
        "node_id": node_id,
        "details": {
            "capture_protocol": PROTOCOL,
            "host_resources": _host_resources(
                backend=backend,
                artifact_root=artifact_root,
                valid_for_ms=resource_valid_for_ms,
            ),
            "transport": {
                "transport_path_observations": _transport_paths(
                    observations,
                    local_node_id=node_id,
                    remote_nodes=remote_nodes,
                )
            },
        },
    }
    return {
        "observation": statement,
        "signature": signer.sign(statement),
        "verification_key": signer.public_key_record(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--backend", choices=("mlx", "numpy"), required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--remote-nodes", type=Path, required=True)
    parser.add_argument(
        "--resource-valid-for-ms",
        type=int,
        default=120_000,
        help="signed resource lease (120000..3600000 ms)",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value = capture(
        node_id=args.node_id,
        identity_file=args.identity_file,
        endpoint_id=args.endpoint_id,
        backend=args.backend,
        artifact_root=args.artifact_root,
        observations=_read_object(args.observations),
        remote_nodes=_read_object(args.remote_nodes),
        resource_valid_for_ms=args.resource_valid_for_ms,
    )
    _write_private(args.output, value)
    print(
        json.dumps(
            {
                "node_id": args.node_id,
                "output": str(args.output),
                "resource_digest": value["observation"]["details"]["host_resources"][
                    "resource_digest"
                ],
                "directed_edge_count": len(
                    value["observation"]["details"]["transport"][
                        "transport_path_observations"
                    ]
                ),
                "route_ready": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
