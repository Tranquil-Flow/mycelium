#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Build fresh A3 topology and model-capacity evidence without provisioning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.model_capacity import recompute_model_operation
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_topology_evidence import (
    complete_directed_observation_matrix,
    select_measured_topology,
)
from scripts.assemble_m17_swarm_evidence import _verified_node, assemble


PROTOCOL = "mycelium.a3_capacity_evidence.v1"
CALIBRATION_PROTOCOL = "mycelium.a3_capacity_calibration.v1"
_CALIBRATION_FIELDS = {
    "node_id",
    "source_evidence_digest",
    "prefill_ms_per_layer_token",
    "decode_ms_per_layer_token",
    "memory_bandwidth_Bps",
    "spill_bandwidth_Bps",
}
_PRESSURE_THERMAL_STATES = {"serious", "critical", "emergency"}
_PRESSURE_POWER_STATES = {"critical_battery", "shutdown_imminent"}


class A3CapacityEvidenceError(RuntimeError):
    """One bounded A3 capacity input or evidence failure."""


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise A3CapacityEvidenceError("a3_capacity_input_invalid") from exc
    if not isinstance(value, dict):
        raise A3CapacityEvidenceError("a3_capacity_input_invalid")
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


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise A3CapacityEvidenceError(code)
    return value


def _calibrations(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    records = document.get("records")
    if document.get("protocol") != CALIBRATION_PROTOCOL or not isinstance(records, list):
        raise A3CapacityEvidenceError("a3_capacity_calibration_invalid")
    result: dict[str, dict[str, Any]] = {}
    for value in records:
        if not isinstance(value, Mapping) or set(value) != _CALIBRATION_FIELDS:
            raise A3CapacityEvidenceError("a3_capacity_calibration_invalid")
        node_id = value.get("node_id")
        source_digest = value.get("source_evidence_digest")
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in result
            or not isinstance(source_digest, str)
            or not source_digest.startswith("sha256:")
            or len(source_digest) != 71
        ):
            raise A3CapacityEvidenceError("a3_capacity_calibration_invalid")
        for field in (
            "prefill_ms_per_layer_token",
            "decode_ms_per_layer_token",
            "memory_bandwidth_Bps",
            "spill_bandwidth_Bps",
        ):
            number = value.get(field)
            if (
                not isinstance(number, (int, float))
                or isinstance(number, bool)
                or float(number) <= 0
            ):
                raise A3CapacityEvidenceError("a3_capacity_calibration_invalid")
        result[node_id] = dict(value)
    if not result:
        raise A3CapacityEvidenceError("a3_capacity_calibration_invalid")
    return result


def _resource(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    observation = _mapping(snapshot.get("observation"), "a3_capacity_snapshot_invalid")
    details = _mapping(observation.get("details"), "a3_capacity_snapshot_invalid")
    return _mapping(details.get("host_resources"), "a3_capacity_snapshot_invalid")


def _paths(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    observation = _mapping(snapshot.get("observation"), "a3_capacity_snapshot_invalid")
    details = _mapping(observation.get("details"), "a3_capacity_snapshot_invalid")
    transport = _mapping(details.get("transport"), "a3_capacity_snapshot_invalid")
    values = transport.get("transport_path_observations")
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise A3CapacityEvidenceError("a3_capacity_snapshot_invalid")
    return [dict(value) for value in values]


def _automatic_exclusion(resource: Mapping[str, Any]) -> str | None:
    thermal = resource.get("thermal_state")
    power = resource.get("power_state")
    if thermal in _PRESSURE_THERMAL_STATES:
        return f"thermal_pressure:{thermal}"
    if power in _PRESSURE_POWER_STATES:
        return f"power_pressure:{power}"
    if type(resource.get("available_memory_bytes")) is not int or int(
        resource["available_memory_bytes"]
    ) <= 0:
        return "memory_unavailable"
    if type(resource.get("disk_free_bytes")) is not int or int(
        resource["disk_free_bytes"]
    ) <= 0:
        return "disk_unavailable"
    return None


def build_live_observations(
    *,
    snapshots: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    entry_node_id: str,
    now_unix_ms: int,
    excluded_node_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify and bind a dynamic eligible host set to one measured topology."""

    if not snapshots:
        raise A3CapacityEvidenceError("a3_capacity_snapshots_required")
    calibrated = _calibrations(calibration)
    requested_exclusions = set(excluded_node_ids)
    if len(requested_exclusions) != len(tuple(excluded_node_ids)):
        raise A3CapacityEvidenceError("a3_capacity_exclusions_invalid")
    verified: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    exclusion_reasons: dict[str, str] = {}
    for snapshot in snapshots:
        try:
            node, _, _ = _verified_node(snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise A3CapacityEvidenceError("a3_capacity_snapshot_invalid") from exc
        if node.node_id in verified:
            raise A3CapacityEvidenceError("a3_capacity_snapshot_duplicate")
        resources = _resource(snapshot)
        reason = _automatic_exclusion(resources)
        if node.node_id in requested_exclusions:
            reason = "operator_excluded"
        if reason is not None:
            exclusion_reasons[node.node_id] = reason
        verified[node.node_id] = (snapshot, resources)
    unknown_exclusions = requested_exclusions - set(verified)
    if unknown_exclusions:
        raise A3CapacityEvidenceError("a3_capacity_exclusions_invalid")
    eligible = tuple(sorted(set(verified) - set(exclusion_reasons)))
    if len(eligible) < 2:
        raise A3CapacityEvidenceError("a3_capacity_requires_two_eligible_nodes")
    if entry_node_id not in eligible:
        raise A3CapacityEvidenceError("a3_capacity_entry_node_ineligible")
    if not set(eligible) <= set(calibrated):
        raise A3CapacityEvidenceError("a3_capacity_calibration_missing")

    all_paths = [
        path
        for node_id in eligible
        for path in _paths(verified[node_id][0])
        if path.get("remote_node_id") in eligible
    ]
    endpoints: dict[str, str] = {}
    for path in all_paths:
        local_node = path.get("local_node_id")
        local_endpoint = path.get("local_endpoint_id")
        if not isinstance(local_node, str) or not isinstance(local_endpoint, str):
            raise A3CapacityEvidenceError("a3_capacity_topology_invalid")
        previous = endpoints.setdefault(local_node, local_endpoint)
        if previous != local_endpoint:
            raise A3CapacityEvidenceError("a3_capacity_topology_invalid")
    try:
        matrix = complete_directed_observation_matrix(
            all_paths,
            node_ids=eligible,
            endpoint_ids_by_node=endpoints,
            now_unix_ms=now_unix_ms,
            minimum_node_count=2,
        )
        decision = select_measured_topology(
            matrix,
            node_ids=eligible,
            entry_node_id=entry_node_id,
        )
    except ValueError as exc:
        raise A3CapacityEvidenceError("a3_capacity_topology_invalid") from exc

    placement_nodes = []
    for node_id in eligible:
        resources = verified[node_id][1]
        record = calibrated[node_id]
        available = int(resources["available_memory_bytes"])
        placement_nodes.append(
            {
                "node_id": node_id,
                "prefill_ms_per_layer_token": float(
                    record["prefill_ms_per_layer_token"]
                ),
                "decode_ms_per_layer_token": float(
                    record["decode_ms_per_layer_token"]
                ),
                "fast_allocatable_bytes": available,
                "total_allocatable_bytes": available,
                "memory_bandwidth_Bps": float(record["memory_bandwidth_Bps"]),
                "spill_bandwidth_Bps": float(record["spill_bandwidth_Bps"]),
                "calibration_source_digest": record["source_evidence_digest"],
                "resource_observation_digest": node_observation_digest(
                    verified[node_id][0]
                ),
            }
        )
    generation = max(
        int(verified[node_id][1]["observed_at_unix_ms"]) for node_id in eligible
    )
    placement = {
        "protocol": "mycelium.a3_capacity_placement.v1",
        "snapshot_generation": generation,
        "nodes": placement_nodes,
        "excluded_nodes": [
            {"node_id": node_id, "reason": exclusion_reasons[node_id]}
            for node_id in sorted(exclusion_reasons)
        ],
        "route_ready": False,
    }
    topology = {
        "protocol": "mycelium.a3_capacity_topology.v1",
        "measurement_source": "iroh_activation_plane",
        "decision": decision,
        "observation_digests": sorted(
            _digest(item) for item in matrix.values()
        ),
        "excluded_nodes": placement["excluded_nodes"],
        "route_ready": False,
    }
    live = {
        "protocol": "mycelium.live_swarm_resource_observations.v1",
        "placement": placement,
        "topology": topology,
        "signed_snapshots": [verified[node_id][0] for node_id in eligible],
    }
    return live, {
        "eligible_node_ids": list(eligible),
        "excluded_nodes": placement["excluded_nodes"],
        "selected_cycle": decision["selected_cycle"],
        "opened_order": decision["opened_order"],
        "selected_cost_ms": decision["selected_cost_ms"],
    }


def node_observation_digest(snapshot: Mapping[str, Any]) -> str:
    observation = _mapping(snapshot.get("observation"), "a3_capacity_snapshot_invalid")
    return _digest(observation)


def build(args: argparse.Namespace) -> dict[str, Any]:
    now = args.evaluated_at_unix_ms or int(time.time() * 1_000)
    snapshots = [_read_object(path) for path in args.snapshot]
    calibration = _read_object(args.calibration)
    live, topology_summary = build_live_observations(
        snapshots=snapshots,
        calibration=calibration,
        entry_node_id=args.entry_node_id,
        now_unix_ms=now,
        excluded_node_ids=args.exclude_node,
    )
    swarm_evidence = assemble(live)
    operation = recompute_model_operation(
        cache_root=args.hf_cache,
        live_observations=live,
        evaluated_at_unix_ms=now,
        prompt_tokens=args.prompt_tokens,
        output_tokens=args.output_tokens,
        concurrency=args.concurrency,
        workspace_bytes=args.workspace_bytes,
        required_decode_mode=args.required_decode_mode,
        representation_authorization=(
            _read_object(args.representation_authorization)
            if args.representation_authorization is not None
            else None
        ),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.output_dir, 0o700)
    _write_private(args.output_dir / "live-observations.json", live)
    _write_private(args.output_dir / "swarm-feasibility-evidence.json", swarm_evidence)
    _write_private(args.output_dir / "model-operation.json", operation)
    reports = operation.get("feasibility_reports")
    if not isinstance(reports, list):
        raise A3CapacityEvidenceError("a3_capacity_operation_invalid")
    selected_reports = [
        report
        for report in reports
        if isinstance(report, Mapping)
        and report.get("model_id") in set(args.focus_model_id)
    ]
    summary = {
        "protocol": PROTOCOL,
        "evaluated_at_unix_ms": now,
        "live_observations_digest": _digest(live),
        "swarm_evidence_digest": swarm_evidence["evidence_digest"],
        "model_operation_digest": operation["operation_digest"],
        "download_authorized": False,
        "provisioning_started": False,
        **topology_summary,
        "models": [
            {
                "model_id": report.get("model_id"),
                "revision": report.get("revision"),
                "state": report.get("state"),
                "reasons": report.get("reasons"),
                "provisioning_authorized": report.get("provisioning_authorized"),
                "stages": report.get("stages"),
            }
            for report in selected_reports
        ],
    }
    _write_private(args.output_dir / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, action="append", required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--entry-node-id", required=True)
    parser.add_argument("--exclude-node", action="append", default=[])
    parser.add_argument("--hf-cache", type=Path, required=True)
    parser.add_argument("--focus-model-id", action="append", default=[])
    parser.add_argument("--prompt-tokens", type=int, default=256)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--workspace-bytes", type=int, default=536_870_912)
    parser.add_argument("--required-decode-mode", default="complete_context_replay")
    parser.add_argument("--evaluated-at-unix-ms", type=int)
    parser.add_argument("--representation-authorization", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
