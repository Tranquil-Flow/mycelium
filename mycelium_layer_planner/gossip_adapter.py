from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping

from mycelium_capacity_profiles import CapacityProfile, placement_calibration_digest
from mycelium_gossip.evidence_bundle import (
    EvidenceBundle,
    evidence_bundle_from_dict,
    evidence_bundle_to_dict,
)
from mycelium_gossip.signed_bundle import validate_signed_evidence_bundle

from .contracts import RoutePlanV2
from .planner import plan_snapshot

PLANNER_SNAPSHOT_PROTOCOL = "mycelium.layer_planner_snapshot.v1"
_PERFORMANCE_FIELDS = (
    "prefill_ms_per_layer_token",
    "decode_ms_per_layer_token",
    "memory_bandwidth_Bps",
    "spill_bandwidth_Bps",
)
_QUALIFIED_DECODE_MODES = {
    "mlx": frozenset({"stage_local_kv", "complete_context_replay"}),
    "numpy": frozenset({"complete_context_replay"}),
    "pixel-stdlib": frozenset({"complete_context_replay"}),
}


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


def _membership_by_node(bundle: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in bundle["records"]:
        if record["kind"] != "membership":
            continue
        payload = record["payload"]
        node_id = record["origin_node_id"]
        if payload.get("subject_node_id") != node_id:
            continue
        if node_id in result:
            raise ValueError(f"duplicate self-membership evidence for {node_id}")
        result[node_id] = record
    return result


def _capacity_profile_binding(
    *,
    node_id: str,
    status: Mapping[str, Any],
    performance: Mapping[str, Any],
    profile: CapacityProfile,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    quantization: str,
) -> dict[str, Any]:
    extensions = status.get("extensions")
    reference = extensions.get("capacity_profile") if isinstance(extensions, Mapping) else None
    expected_reference = {
        "protocol": "mycelium.capacity_profile_ref.v1",
        "profile_digest": profile.profile_digest,
        "max_safe_concurrency": profile.max_safe_concurrency,
        "interactive_concurrency_limit": profile.interactive_concurrency_limit,
        "batch_concurrency_limit": profile.batch_concurrency_limit,
        "evidence_scope": "bounded_local_samples",
        "route_ready": False,
    }
    if reference != expected_reference:
        raise ValueError(f"{node_id} capacity profile reference is missing or mismatched")
    if status.get("concurrency_limit") != profile.interactive_concurrency_limit:
        raise ValueError(f"{node_id} capacity profile concurrency limit is mismatched")
    key = profile.key
    expected_key = {
        "model_digest": model.get("weight_digest"),
        "quantization": quantization,
        "backend": performance.get("backend"),
        "runtime_build": performance.get("runtime_build"),
        "hardware_class": performance.get("hardware_class"),
        "power_mode": performance.get("power_mode"),
        "context_bucket": workload.get("context_bucket"),
        "kv_mode": performance.get("decode_mode"),
    }
    actual_key = key.to_document()
    for field, expected in expected_key.items():
        if not isinstance(expected, str) or not expected or actual_key.get(field) != expected:
            raise ValueError(f"{node_id} capacity profile {field} is mismatched")
    expected_source = placement_calibration_digest(node_id, performance)
    if key.source_evidence_digest != expected_source:
        raise ValueError(f"{node_id} capacity profile source evidence is mismatched")
    return {
        "profile_digest": profile.profile_digest,
        "source_evidence_digest": key.source_evidence_digest,
        "key": actual_key,
        "max_safe_concurrency": profile.max_safe_concurrency,
        "interactive_concurrency_limit": profile.interactive_concurrency_limit,
        "batch_concurrency_limit": profile.batch_concurrency_limit,
    }


def _planner_nodes(
    bundle: Mapping[str, Any],
    *,
    capacity_profiles: Mapping[str, CapacityProfile] | None = None,
    model: Mapping[str, Any] | None = None,
    workload: Mapping[str, Any] | None = None,
    quantization: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    statuses = _status_by_node(bundle)
    memberships = _membership_by_node(bundle)
    nodes = []
    profile_bindings: dict[str, dict[str, Any]] = {}
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
        membership = memberships.get(node_id)
        if membership is None:
            raise ValueError(
                f"missing self-membership evidence for allocator-eligible node {node_id}"
            )
        membership_payload = membership["payload"]
        if (
            membership_payload.get("state") != "alive"
            or membership_payload.get("reporter_node_id") != node_id
            or membership_payload.get("subject_incarnation")
            != allocator_node.get("incarnation")
            or membership.get("incarnation") != allocator_node.get("incarnation")
        ):
            raise ValueError(f"stale self-membership evidence for allocator-eligible node {node_id}")
        performance = status.get("performance")
        if not isinstance(performance, Mapping):
            raise ValueError(f"missing performance calibration for allocator-eligible node {node_id}")
        if capacity_profiles is not None:
            profile = capacity_profiles.get(node_id)
            if profile is None:
                raise ValueError(f"missing capacity profile for allocator-eligible node {node_id}")
            assert model is not None and workload is not None
            profile_bindings[node_id] = _capacity_profile_binding(
                node_id=node_id,
                status=status,
                performance=performance,
                profile=profile,
                model=model,
                workload=workload,
                quantization=quantization or "",
            )
        calibrated = {
            field: _require_finite_nonnegative(
                performance.get(field),
                f"{node_id} performance.{field}",
                positive=field in {"memory_bandwidth_Bps", "spill_bandwidth_Bps"},
            )
            for field in _PERFORMANCE_FIELDS
        }
        backend = performance.get("backend")
        decode_mode = performance.get("decode_mode")
        if (
            not isinstance(backend, str)
            or backend not in _QUALIFIED_DECODE_MODES
            or decode_mode not in _QUALIFIED_DECODE_MODES[backend]
        ):
            raise ValueError(f"{node_id} performance decode mode is not qualified")
        confidence = _require_finite_nonnegative(
            performance.get("calibration_confidence"),
            f"{node_id} performance.calibration_confidence",
        )
        if confidence > 1:
            raise ValueError(f"{node_id} performance.calibration_confidence must be in [0, 1]")
        total_allocatable = allocator_node.get("total_allocatable_bytes")
        if isinstance(total_allocatable, bool) or not isinstance(total_allocatable, int) or total_allocatable <= 0:
            raise ValueError(f"missing allocatable memory for allocator-eligible node {node_id}")
        fast_allocatable = allocator_node.get("fast_allocatable_bytes")
        if (
            isinstance(fast_allocatable, bool)
            or not isinstance(fast_allocatable, int)
            or fast_allocatable <= 0
            or fast_allocatable > total_allocatable
        ):
            raise ValueError(f"missing fast allocatable memory for allocator-eligible node {node_id}")
        nodes.append({
            "node_id": node_id,
            "prefill_ms_per_layer_token": calibrated["prefill_ms_per_layer_token"],
            "decode_ms_per_layer_token": calibrated["decode_ms_per_layer_token"],
            "fast_memory_bytes": fast_allocatable,
            "total_memory_bytes": total_allocatable,
            "memory_bandwidth_Bps": calibrated["memory_bandwidth_Bps"],
            "spill_bandwidth_Bps": calibrated["spill_bandwidth_Bps"],
            "calibration_confidence": confidence,
            "backend": backend,
            "decode_mode": decode_mode,
        })
    if not nodes:
        raise ValueError("evidence bundle contains no allocator-eligible calibrated nodes")
    return nodes, profile_bindings


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
        key = (src, dst)
        if key in seen:
            raise ValueError(f"multiple link observations for {src}->{dst}")
        seen.add(key)
        if payload.get("reachable") is not True:
            raise ValueError(f"required directed link is unreachable for {src}->{dst}")
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
    required = {
        (src, dst)
        for src in eligible_node_ids
        for dst in eligible_node_ids
        if src != dst
    }
    missing = sorted(required - seen)
    if missing:
        src, dst = missing[0]
        raise ValueError(f"required directed link is missing for {src}->{dst}")
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

    nodes, profile_bindings = _planner_nodes(bundle)
    decode_modes = {node.pop("decode_mode") for node in nodes}
    if len(decode_modes) != 1:
        raise ValueError("planner candidate nodes have incompatible decode modes")
    decode_mode = next(iter(decode_modes))
    node_runtime = {
        node["node_id"]: {"backend": node.pop("backend"), "decode_mode": decode_mode}
        for node in nodes
    }
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
        "placement_provenance": "planner_v2",
        "decode_mode": decode_mode,
        "node_runtime": node_runtime,
        "capacity_profile_bindings": profile_bindings,
    }
    # Prove canonicalizability before passing to the planner.
    planner_snapshot_digest(snapshot)
    return snapshot


def planner_snapshot_from_signed_evidence(
    signed_evidence: Mapping[str, Any],
    *,
    expected_verification_key_digest: str,
    now_unix_ms: int,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    policy: Mapping[str, Any],
    quantization: str,
    admitted_node_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build the serving candidate only from trusted, current, atomic evidence."""

    validated = validate_signed_evidence_bundle(
        signed_evidence,
        expected_verification_key_digest=expected_verification_key_digest,
        now_unix_ms=now_unix_ms,
    )
    bundle = evidence_bundle_to_dict(validated.bundle)
    snapshot = planner_snapshot_from_evidence_bundle(
        bundle,
        model=model,
        workload=workload,
        policy=policy,
    )
    nodes, profile_bindings = _planner_nodes(
        bundle,
        capacity_profiles=validated.capacity_profiles,
        model=model,
        workload=workload,
        quantization=quantization,
    )
    decode_modes = {node.pop("decode_mode") for node in nodes}
    if len(decode_modes) != 1:
        raise ValueError("planner candidate nodes have incompatible decode modes")
    decode_mode = next(iter(decode_modes))
    node_runtime = {
        node["node_id"]: {"backend": node.pop("backend"), "decode_mode": decode_mode}
        for node in nodes
    }
    snapshot["nodes"] = nodes
    snapshot["node_runtime"] = node_runtime
    snapshot["capacity_profile_bindings"] = profile_bindings
    snapshot["quantization"] = quantization
    snapshot["evidence_authority"] = {
        "authority_generation": validated.statement["authority_generation"],
        "signer_endpoint_id": validated.statement["signer_endpoint_id"],
        "verification_key_digest": expected_verification_key_digest,
        "captured_at_unix_ms": validated.statement["captured_at_unix_ms"],
        "valid_until_unix_ms": validated.statement["valid_until_unix_ms"],
        "capacity_profiles_digest": validated.statement["capacity_profiles_digest"],
    }
    if admitted_node_ids is not None:
        known = {node["node_id"] for node in nodes}
        if (
            not admitted_node_ids
            or len(set(admitted_node_ids)) != len(admitted_node_ids)
            or not set(admitted_node_ids) <= known
        ):
            raise ValueError("admitted_node_ids must be a unique non-empty evidence subset")
        snapshot["admitted_node_ids"] = list(admitted_node_ids)
    planner_snapshot_digest(snapshot)
    return snapshot


def plan_signed_evidence(
    signed_evidence: Mapping[str, Any],
    *,
    expected_verification_key_digest: str,
    now_unix_ms: int,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    policy: Mapping[str, Any],
    quantization: str,
    admitted_node_ids: tuple[str, ...] | None = None,
) -> RoutePlanV2:
    snapshot = planner_snapshot_from_signed_evidence(
        signed_evidence,
        expected_verification_key_digest=expected_verification_key_digest,
        now_unix_ms=now_unix_ms,
        model=model,
        workload=workload,
        policy=policy,
        quantization=quantization,
        admitted_node_ids=admitted_node_ids,
    )
    plan = plan_snapshot(snapshot)
    if plan.snapshot_digest != planner_snapshot_digest(snapshot):
        raise ValueError("Planner returned a route not bound to signed evidence")
    return plan


def plan_evidence_bundle(
    bundle_value: EvidenceBundle | Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    workload: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> RoutePlanV2:
    snapshot = planner_snapshot_from_evidence_bundle(
        bundle_value,
        model=model,
        workload=workload,
        policy=policy,
    )
    plan = plan_snapshot(snapshot)
    expected_digest = planner_snapshot_digest(snapshot)
    if plan.snapshot_digest != expected_digest:
        raise ValueError("Planner returned a route not bound to the supplied evidence snapshot")
    return plan


def validate_planner_snapshot_binding(
    snapshot: Mapping[str, Any], bundle_value: EvidenceBundle | Mapping[str, Any]
) -> None:
    """Rebuild and compare a Planner snapshot against its sealed Gossip source."""
    for field in ("model", "workload", "policy"):
        if not isinstance(snapshot.get(field), Mapping):
            raise ValueError(f"planner snapshot {field} is required")
    expected = planner_snapshot_from_evidence_bundle(
        bundle_value,
        model=snapshot["model"],
        workload=snapshot["workload"],
        policy=snapshot["policy"],
    )
    if json.loads(json.dumps(snapshot)) != expected:
        raise ValueError("planner snapshot does not equal deterministic evidence-bundle projection")
