from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping

from mycelium_gossip.evidence_bundle import (
    EvidenceBundle,
    evidence_bundle_from_dict,
    evidence_bundle_to_dict,
)

from .contracts import RoutePlanV2
from .planner import plan_snapshot

PLANNER_SNAPSHOT_PROTOCOL = "mycelium.layer_planner_snapshot.v1"
_PERFORMANCE_FIELDS = (
    "prefill_ms_per_layer_token",
    "decode_ms_per_layer_token",
    "memory_bandwidth_Bps",
    "spill_bandwidth_Bps",
)


def planner_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("planner snapshot must be canonical JSON") from exc
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _validated_bundle(value: Mapping[str, Any] | EvidenceBundle) -> dict[str, Any]:
    if isinstance(value, EvidenceBundle):
        wire = evidence_bundle_to_dict(value)
    elif isinstance(value, Mapping):
        wire = copy.deepcopy(dict(value))
    else:
        raise ValueError("evidence_bundle must be a mapping or EvidenceBundle")
    return evidence_bundle_to_dict(evidence_bundle_from_dict(wire))


def _require_finite_nonnegative(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "finite and non-negative"
        raise ValueError(f"{name} must be {qualifier}")
    return result


def _status_by_node(bundle: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in bundle["records"]:
        if record["kind"] != "status":
            continue
        node_id = record["origin_node_id"]
        if node_id in result:
            raise ValueError(f"duplicate status evidence for {node_id}")
        result[node_id] = record["payload"]
    return result


def _planner_nodes(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    statuses = _status_by_node(bundle)
    nodes = []
    eligible_allocator_nodes = [
        node for node in bundle["allocator_view"]["nodes"] if node.get("eligible") is True
    ]
    for allocator_node in sorted(eligible_allocator_nodes, key=lambda item: item["node_id"]):
        node_id = allocator_node.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("allocator node_id is invalid")
        status = statuses.get(node_id)
        if status is None:
            raise ValueError(f"missing status evidence for allocator-eligible node {node_id}")
        performance = status.get("performance")
        if not isinstance(performance, Mapping):
            raise ValueError(f"missing performance calibration for allocator-eligible node {node_id}")
        calibrated = {
            field: _require_finite_nonnegative(
                performance.get(field),
                f"{node_id} performance.{field}",
                positive=field in {"memory_bandwidth_Bps", "spill_bandwidth_Bps"},
            )
            for field in _PERFORMANCE_FIELDS
        }
        confidence = _require_finite_nonnegative(
            performance.get("calibration_confidence", 1.0),
            f"{node_id} performance.calibration_confidence",
        )
        if confidence > 1:
            raise ValueError(f"{node_id} performance.calibration_confidence must be in [0, 1]")
        total_allocatable = allocator_node.get("total_allocatable_bytes")
        if isinstance(total_allocatable, bool) or not isinstance(total_allocatable, int) or total_allocatable <= 0:
            raise ValueError(f"missing allocatable memory for allocator-eligible node {node_id}")
        nodes.append({
            "node_id": node_id,
            "prefill_ms_per_layer_token": calibrated["prefill_ms_per_layer_token"],
            "decode_ms_per_layer_token": calibrated["decode_ms_per_layer_token"],
            # Gossip reports net memory after reservations. Treat that net value as
            # both fast and total usable capacity; the adapter forbids a second reserve.
            "fast_memory_bytes": total_allocatable,
            "total_memory_bytes": total_allocatable,
            "memory_bandwidth_Bps": calibrated["memory_bandwidth_Bps"],
            "spill_bandwidth_Bps": calibrated["spill_bandwidth_Bps"],
            "calibration_confidence": confidence,
        })
    if not nodes:
        raise ValueError("evidence bundle contains no allocator-eligible calibrated nodes")
    return nodes


def _planner_links(bundle: Mapping[str, Any], eligible_node_ids: set[str]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in bundle["records"]:
        if record["kind"] != "link":
            continue
        payload = record["payload"]
        src = payload.get("src_node_id")
        dst = payload.get("dst_node_id")
        if src not in eligible_node_ids or dst not in eligible_node_ids:
            continue
        if payload.get("reachable") is not True:
            continue
        key = (src, dst)
        if key in seen:
            raise ValueError(f"multiple reachable link observations for {src}->{dst}")
        seen.add(key)
        rtt_raw = payload.get("rtt_p95_ms")
        if rtt_raw is None:
            rtt_raw = payload.get("connect_rtt_ema_ms")
        rtt_ms = _require_finite_nonnegative(rtt_raw, f"{src}->{dst} rtt")
        jitter_ms = _require_finite_nonnegative(payload.get("jitter_ms"), f"{src}->{dst} jitter")
        loss_ratio = _require_finite_nonnegative(payload.get("loss_ratio"), f"{src}->{dst} loss_ratio")
        if loss_ratio > 1:
            raise ValueError(f"{src}->{dst} loss_ratio must be in [0, 1]")
        goodput_mbps = _require_finite_nonnegative(
            payload.get("goodput_mbps"), f"{src}->{dst} goodput_mbps", positive=True
        )
        links.append({
            "src": src,
            "dst": dst,
            "rtt_ms": rtt_ms,
            "jitter_ms": jitter_ms,
            "bandwidth_Bps": goodput_mbps * 1_000_000.0 / 8.0,
            "loss_ratio": loss_ratio,
            "inferred": False,
            "stale": False,
        })
    return sorted(links, key=lambda item: (item["src"], item["dst"]))


def planner_snapshot_from_evidence_bundle(
    evidence_bundle: Mapping[str, Any] | EvidenceBundle,
    *,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    bundle = _validated_bundle(evidence_bundle)
    model_wire = copy.deepcopy(dict(model))
    expected_model = bundle["model"]
    bindings = {
        "model_id": expected_model["model_id"],
        "num_layers": expected_model["num_layers"],
        "revision": expected_model["resolved_commit"],
        "weight_digest": expected_model["manifest_digest"],
    }
    for field, expected in bindings.items():
        if model_wire.get(field) != expected:
            raise ValueError(f"planner model {field} does not match evidence bundle")

    policy_wire = copy.deepcopy(dict(policy))
    reserve = policy_wire.get("memory_reserve_fraction")
    if reserve != 0 and reserve != 0.0:
        raise ValueError(
            "policy memory_reserve_fraction must be 0 because Gossip memory is already net of reservations"
        )

    nodes = _planner_nodes(bundle)
    node_ids = {node["node_id"] for node in nodes}
    links = _planner_links(bundle, node_ids)
    snapshot = {
        "protocol": PLANNER_SNAPSHOT_PROTOCOL,
        "swarm_id": bundle["swarm_id"],
        "deployment": copy.deepcopy(bundle["deployment"]),
        "snapshot_generation": bundle["snapshot_generation"],
        "evidence_bundle_digest": bundle["evidence_bundle_digest"],
        "model": model_wire,
        "nodes": nodes,
        "links": links,
        "workload": copy.deepcopy(dict(workload)),
        "policy": policy_wire,
    }
    # Prove canonicalizability before passing to the planner.
    planner_snapshot_digest(snapshot)
    return snapshot


def plan_evidence_bundle(
    evidence_bundle: Mapping[str, Any] | EvidenceBundle,
    *,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> RoutePlanV2:
    snapshot = planner_snapshot_from_evidence_bundle(
        evidence_bundle,
        model=model,
        workload=workload,
        policy=policy,
    )
    plan = plan_snapshot(snapshot)
    expected_digest = planner_snapshot_digest(snapshot)
    if plan.snapshot_digest != expected_digest:
        raise ValueError("Planner returned a route not bound to the supplied evidence snapshot")
    return plan
