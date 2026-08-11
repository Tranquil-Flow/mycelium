# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closed M18 replica-plan and request-track runtime projections.

The planner projection is intent only.  The runtime ledger can admit requests only to
independently qualified complete tracks and never migrates an admitted request.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from typing import Any, Mapping

from mycelium_layer_planner.contracts import RoutePlanV2
from mycelium_layer_planner.validation import validate_route_plan


PLAN_PROTOCOL = "mycelium.replica_plan.v1"
RUNTIME_PROTOCOL = "mycelium.replica_runtime.v1"
_SHA256_LENGTH = 71
_PRIVATE_FIELDS = frozenset(
    {
        "prompt",
        "prompt_text",
        "response",
        "response_text",
        "token_ids",
        "tokens",
        "activation",
        "activations",
        "kv",
        "kv_content",
        "tensor",
        "tensors",
        "credential",
        "secret",
        "runtime_endpoint",
        "private_address",
        "artifact_root",
        "cache_path",
    }
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256_ref(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 reference")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and non-negative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _reject_private(value: object) -> None:
    if isinstance(value, Mapping):
        if _PRIVATE_FIELDS.intersection(str(key).lower() for key in value):
            raise ValueError("M18 projection contains private request or runtime content")
        for child in value.values():
            _reject_private(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_private(child)


def _json_copy(value: object) -> Any:
    _reject_private(value)
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("M18 projection must be finite JSON") from exc


_DEPLOYMENT_FIELDS = frozenset(
    {
        "deployment_id",
        "deployment_epoch",
        "model_id",
        "model_revision",
        "representation_digest",
        "manifest_digest",
        "qualification_id",
        "qualification_digest",
        "decode_mode",
        "quantization",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "generation",
        "evidence_digest",
        "evaluated_at_unix_ms",
        "valid_until_unix_ms",
    }
)
_THROUGHPUT_FIELDS = frozenset(
    {
        "evidence_digest",
        "mode",
        "baseline_request_count",
        "baseline_throughput_rps",
        "replicated_request_count",
        "replicated_throughput_rps",
        "gain_fraction",
        "minimum_required_fraction",
        "passed",
    }
)


def _deployment_binding(value: Mapping[str, Any], plan: RoutePlanV2) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DEPLOYMENT_FIELDS:
        raise ValueError("M18 deployment binding shape is invalid")
    output = {
        "deployment_id": _nonempty(value["deployment_id"], "deployment_id"),
        "deployment_epoch": value["deployment_epoch"],
        "model_id": _nonempty(value["model_id"], "model_id"),
        "model_revision": _nonempty(value["model_revision"], "model_revision"),
        "representation_digest": _sha256_ref(
            value["representation_digest"], "representation_digest"
        ),
        "manifest_digest": _sha256_ref(value["manifest_digest"], "manifest_digest"),
        "qualification_id": _nonempty(value["qualification_id"], "qualification_id"),
        "qualification_digest": _sha256_ref(
            value["qualification_digest"], "qualification_digest"
        ),
        "decode_mode": _nonempty(value["decode_mode"], "decode_mode"),
        "quantization": _nonempty(value["quantization"], "quantization"),
    }
    if type(output["deployment_epoch"]) is not int or output["deployment_epoch"] <= 0:
        raise ValueError("deployment_epoch must be a positive integer")
    if output["model_id"] != plan.model.model_id or output["model_revision"] != plan.model.revision:
        raise ValueError("M18 deployment does not match the planned model")
    return output


def _evidence_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EVIDENCE_FIELDS:
        raise ValueError("M18 evidence binding shape is invalid")
    output = {
        "generation": value["generation"],
        "evidence_digest": _sha256_ref(value["evidence_digest"], "evidence_digest"),
        "evaluated_at_unix_ms": value["evaluated_at_unix_ms"],
        "valid_until_unix_ms": value["valid_until_unix_ms"],
    }
    if type(output["generation"]) is not int or output["generation"] <= 0:
        raise ValueError("evidence generation must be a positive integer")
    if any(
        type(output[name]) is not int or output[name] <= 0
        for name in ("evaluated_at_unix_ms", "valid_until_unix_ms")
    ) or output["valid_until_unix_ms"] <= output["evaluated_at_unix_ms"]:
        raise ValueError("M18 evidence window is invalid")
    return output


def _roles(start: int, end: int, total_layers: int) -> list[str]:
    roles = ["decoder_layers"]
    if start == 0:
        roles.insert(0, "token_embeddings")
    if end == total_layers:
        roles.extend(("final_norm", "lm_head"))
    return roles


def build_replica_plan(
    plan: RoutePlanV2,
    *,
    deployment_binding: Mapping[str, Any],
    evidence_binding: Mapping[str, Any],
    generated_at_unix_ms: int,
) -> dict[str, Any]:
    """Project one planner-v2 result into the closed M18 intent contract."""

    validate_route_plan(plan)
    if type(generated_at_unix_ms) is not int or generated_at_unix_ms <= 0:
        raise ValueError("generated_at_unix_ms must be a positive integer")
    deployment = _deployment_binding(deployment_binding, plan)
    evidence = _evidence_binding(evidence_binding)
    if generated_at_unix_ms != evidence["evaluated_at_unix_ms"]:
        raise ValueError("M18 plan generation must equal the evidence evaluation time")

    ordered_placements = sorted(
        plan.placements,
        key=lambda item: (item.layer_range.start, item.replica_group_id, item.placement_id),
    )
    placements = [
        {
            "placement_id": item.placement_id,
            "replica_group_id": item.replica_group_id,
            "node_id": item.node_id,
            "layer_range": item.layer_range.to_dict(),
            "component_roles": _roles(
                item.layer_range.start, item.layer_range.end, plan.model.num_layers
            ),
            "primary": item.primary,
            "service_capacity_rps": item.service_capacity_rps,
        }
        for item in ordered_placements
    ]
    groups: list[dict[str, Any]] = []
    for group_id in sorted(
        {item.replica_group_id for item in ordered_placements},
        key=lambda candidate: min(
            item.layer_range.start
            for item in ordered_placements
            if item.replica_group_id == candidate
        ),
    ):
        members = [item for item in placements if item["replica_group_id"] == group_id]
        groups.append(
            {
                "replica_group_id": group_id,
                "layer_range": copy.deepcopy(members[0]["layer_range"]),
                "component_roles": list(members[0]["component_roles"]),
                "primary_placement_id": next(
                    item["placement_id"] for item in members if item["primary"]
                ),
                "placement_ids": sorted(item["placement_id"] for item in members),
            }
        )
    forward = [
        {
            "src_placement_id": edge.src_placement_id,
            "dst_placement_id": edge.dst_placement_id,
            "kind": "forward",
            "capacity_rps": edge.capacity_rps,
            "cost_ms": edge.cost_ms,
        }
        for edge in sorted(plan.forward_edges, key=lambda item: (item.src_placement_id, item.dst_placement_id))
    ]
    loopbacks = [
        {
            "src_placement_id": edge.src_placement_id,
            "dst_placement_id": edge.dst_placement_id,
            "kind": "decode_closure",
            "capacity_rps": None,
            "cost_ms": edge.cost_ms,
        }
        for edge in sorted(plan.loopbacks, key=lambda item: (item.src_placement_id, item.dst_placement_id))
    ]
    edge_identity = {
        (item["src_placement_id"], item["dst_placement_id"], item["kind"]): _digest(item)
        for item in [*forward, *loopbacks]
    }
    tracks = []
    for track in plan.legal_tracks:
        bound_edges = [
            edge_identity[(src, dst, "forward")]
            for src, dst in zip(track.placement_ids, track.placement_ids[1:])
        ]
        if len(track.placement_ids) > 1:
            bound_edges.append(
                edge_identity[(track.placement_ids[-1], track.placement_ids[0], "decode_closure")]
            )
        immutable_track_id = _digest(
            {
                "placement_ids": list(track.placement_ids),
                "edge_digests": bound_edges,
                "deployment_epoch": deployment["deployment_epoch"],
            }
        )
        tracks.append(
            {
                "track_id": immutable_track_id,
                "planner_track_id": track.track_id,
                "placement_ids": list(track.placement_ids),
                "edge_digests": bound_edges,
                "traffic_fraction": track.traffic_fraction,
                "cost_ms": track.cost_ms,
            }
        )
    replication = plan.diagnostics.get("replication", {})
    decisions = copy.deepcopy(replication.get("candidate_decisions", []))
    warnings = sorted(
        {
            decision["failure_domain_warning"]
            for decision in decisions
            if isinstance(decision, Mapping) and decision.get("failure_domain_warning")
        }
    )
    document = {
        "protocol": PLAN_PROTOCOL,
        "generated_at_unix_ms": generated_at_unix_ms,
        "deployment": deployment,
        "evidence": evidence,
        "planner_snapshot_digest": _sha256_ref(plan.snapshot_digest, "planner_snapshot_digest"),
        "workload_name": _nonempty(plan.workload_name, "workload_name"),
        "parallelism": "data_parallel_request_routing",
        "groups": groups,
        "placements": placements,
        "edges": [*forward, *loopbacks],
        "tracks": tracks,
        "flow": {
            "primary_capacity_rps": _nonnegative_number(
                plan.metrics.get("primary_request_capacity_rps"),
                "primary_request_capacity_rps",
            ),
            "replicated_capacity_rps": _nonnegative_number(
                plan.metrics.get("replicated_request_capacity_rps"),
                "replicated_request_capacity_rps",
            ),
            "predicted_gain_rps": _nonnegative_number(
                plan.metrics.get("replica_capacity_gain_rps"),
                "replica_capacity_gain_rps",
            ),
            "unmet_demand_rps": _nonnegative_number(
                plan.metrics.get("unmet_capacity_sweep_demand_rps"),
                "unmet_capacity_sweep_demand_rps",
            ),
        },
        "candidate_decisions": decisions,
        "zero_flow_removed_placement_ids": copy.deepcopy(
            replication.get("zero_flow_removed_placement_ids", [])
        ),
        "failure_domain_warnings": warnings,
        "claim_boundary": (
            "planner intent and predicted cross-request flow only; qualification and "
            "physical throughput remain runtime authorities"
        ),
        "route_ready": False,
    }
    document["plan_digest"] = _digest(document)
    return validate_replica_plan(document)


_PLAN_FIELDS = frozenset(
    {
        "protocol",
        "generated_at_unix_ms",
        "deployment",
        "evidence",
        "planner_snapshot_digest",
        "workload_name",
        "parallelism",
        "groups",
        "placements",
        "edges",
        "tracks",
        "flow",
        "candidate_decisions",
        "zero_flow_removed_placement_ids",
        "failure_domain_warnings",
        "claim_boundary",
        "route_ready",
        "plan_digest",
    }
)


def validate_replica_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _PLAN_FIELDS:
        raise ValueError("M18 replica plan shape is invalid")
    normalized = _json_copy(document)
    if normalized["protocol"] != PLAN_PROTOCOL or normalized["route_ready"] is not False:
        raise ValueError("M18 Planner cannot claim route readiness")
    if normalized["parallelism"] != "data_parallel_request_routing":
        raise ValueError("M18 plan must identify request-level data parallelism")
    _sha256_ref(normalized["planner_snapshot_digest"], "planner_snapshot_digest")
    _sha256_ref(normalized["plan_digest"], "plan_digest")
    body = dict(normalized)
    actual_digest = body.pop("plan_digest")
    if _digest(body) != actual_digest:
        raise ValueError("M18 replica plan digest mismatch")
    placements = normalized["placements"]
    groups = normalized["groups"]
    tracks = normalized["tracks"]
    if not isinstance(placements, list) or not placements or len(placements) > 512:
        raise ValueError("M18 replica placements must be bounded and non-empty")
    placement_ids = [item.get("placement_id") for item in placements if isinstance(item, Mapping)]
    if len(placement_ids) != len(placements) or len(set(placement_ids)) != len(placement_ids):
        raise ValueError("M18 replica placement identities are invalid")
    if not isinstance(groups, list) or not groups or len(groups) > 256:
        raise ValueError("M18 replica groups must be bounded and non-empty")
    group_order = [item.get("replica_group_id") for item in groups if isinstance(item, Mapping)]
    if len(group_order) != len(groups) or len(set(group_order)) != len(group_order):
        raise ValueError("M18 replica group identities are invalid")
    group_by_placement = {
        item["placement_id"]: item.get("replica_group_id") for item in placements
    }
    edge_digests = {_digest(item) for item in normalized["edges"]}
    if not isinstance(tracks, list) or not tracks or len(tracks) > 512:
        raise ValueError("M18 complete tracks must be bounded and non-empty")
    track_ids: set[str] = set()
    fraction = 0.0
    for track in tracks:
        if not isinstance(track, Mapping) or set(track) != {
            "track_id",
            "planner_track_id",
            "placement_ids",
            "edge_digests",
            "traffic_fraction",
            "cost_ms",
        }:
            raise ValueError("M18 track shape is invalid")
        track_id = _sha256_ref(track["track_id"], "track_id")
        if track_id in track_ids:
            raise ValueError("M18 track identities must be unique")
        track_ids.add(track_id)
        if len(track["placement_ids"]) != len(group_order):
            raise ValueError("M18 track is incomplete")
        if [group_by_placement.get(item) for item in track["placement_ids"]] != group_order:
            raise ValueError("M18 track mixes or reorders replica groups")
        if any(item not in edge_digests for item in track["edge_digests"]):
            raise ValueError("M18 track references an unknown directed edge")
        fraction += _nonnegative_number(track["traffic_fraction"], "traffic_fraction")
    if abs(fraction - 1.0) > 1e-9:
        raise ValueError("M18 track fractions must sum to one")
    return normalized


class ReplicaRuntimeLedger:
    """Admit each request to one immutable qualified complete track."""

    def __init__(
        self,
        replica_plan: Mapping[str, Any],
        *,
        qualified_tracks: Mapping[str, Mapping[str, str]],
        throughput_evidence: Mapping[str, Any] | None = None,
        clock: object | None = None,
    ) -> None:
        self._plan = validate_replica_plan(replica_plan)
        known = {item["track_id"] for item in self._plan["tracks"]}
        if not isinstance(qualified_tracks, Mapping) or not qualified_tracks:
            raise ValueError("M18 runtime requires qualified tracks")
        if not set(qualified_tracks) <= known:
            raise ValueError("M18 runtime qualification references an unknown track")
        self._qualified: dict[str, dict[str, str]] = {}
        for track_id, binding in qualified_tracks.items():
            if not isinstance(binding, Mapping) or set(binding) != {
                "qualification_id",
                "qualification_digest",
            }:
                raise ValueError("M18 track qualification shape is invalid")
            self._qualified[track_id] = {
                "qualification_id": _nonempty(binding["qualification_id"], "qualification_id"),
                "qualification_digest": _sha256_ref(
                    binding["qualification_digest"], "qualification_digest"
                ),
            }
        self._clock = clock or time
        self._throughput = _normalize_throughput_evidence(throughput_evidence)
        self._lock = threading.RLock()
        self._removed: dict[str, str] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._incidents: list[dict[str, Any]] = []
        self._sequence = 0

    def _now(self) -> float:
        source = getattr(self._clock, "monotonic", None) or getattr(self._clock, "now", None)
        if not callable(source):
            raise ValueError("M18 runtime clock is invalid")
        return float(source())

    def admit(self, request_id: str, *, path_id: str, requested_track_id: str | None = None) -> str:
        with self._lock:
            _nonempty(request_id, "request_id")
            _nonempty(path_id, "path_id")
            if request_id in self._requests:
                raise ValueError("m18_duplicate_request_id")
            available = [
                item
                for item in self._plan["tracks"]
                if item["track_id"] in self._qualified and item["track_id"] not in self._removed
            ]
            if requested_track_id is not None:
                available = [item for item in available if item["track_id"] == requested_track_id]
            if not available:
                raise ValueError("m18_qualified_track_unavailable")
            active_counts = {
                item["track_id"]: sum(
                    record["track_id"] == item["track_id"] and record["terminal_state"] is None
                    for record in self._requests.values()
                )
                for item in available
            }
            selected = min(
                available,
                key=lambda item: (
                    (active_counts[item["track_id"]] + 1)
                    / max(float(item["traffic_fraction"]), 1e-12),
                    item["track_id"],
                ),
            )
            qualification = self._qualified[selected["track_id"]]
            self._requests[request_id] = {
                "request_id": request_id,
                "path_id": path_id,
                "track_id": selected["track_id"],
                "placement_ids": list(selected["placement_ids"]),
                "qualification_id": qualification["qualification_id"],
                "qualification_digest": qualification["qualification_digest"],
                "phase": "admitted",
                "admitted_at_monotonic_s": self._now(),
                "terminal_at_monotonic_s": None,
                "terminal_state": None,
                "placement_work": {
                    placement_id: {"frames_sent": 0, "frames_received": 0, "work_items": 0}
                    for placement_id in selected["placement_ids"]
                },
                "kv_locality": "request_track_pinned_no_migration",
            }
            return selected["track_id"]

    def mark_phase(self, request_id: str, phase: str) -> None:
        if phase not in {"prefill", "first_token", "decode", "cleanup"}:
            raise ValueError("m18_phase_invalid")
        with self._lock:
            record = self._active(request_id)
            record["phase"] = phase

    def record_placement_work(
        self,
        request_id: str,
        placement_id: str,
        *,
        frames_sent: int,
        frames_received: int,
        work_items: int = 1,
    ) -> None:
        if any(type(item) is not int or item < 0 for item in (frames_sent, frames_received, work_items)):
            raise ValueError("m18_placement_work_invalid")
        with self._lock:
            record = self._active(request_id)
            if placement_id not in record["placement_work"]:
                raise ValueError("m18_placement_not_on_bound_track")
            work = record["placement_work"][placement_id]
            work["frames_sent"] += frames_sent
            work["frames_received"] += frames_received
            work["work_items"] += work_items

    def complete(self, request_id: str, *, state: str = "completed") -> None:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("m18_terminal_state_invalid")
        with self._lock:
            record = self._active(request_id)
            record["phase"] = state
            record["terminal_state"] = state
            record["terminal_at_monotonic_s"] = self._now()

    def remove_track(self, track_id: str, *, reason: str) -> None:
        with self._lock:
            if track_id not in self._qualified or track_id in self._removed:
                raise ValueError("m18_track_removal_invalid")
            self._removed[track_id] = _nonempty(reason, "reason")
            for record in self._requests.values():
                if record["track_id"] == track_id and record["terminal_state"] is None:
                    record["phase"] = "failed"
                    record["terminal_state"] = "replica_lost_no_migration"
                    record["terminal_at_monotonic_s"] = self._now()
            self._sequence += 1
            self._incidents.append(
                {
                    "incident_id": f"m18-incident-{self._sequence}",
                    "kind": "replica_track_removed",
                    "track_id": track_id,
                    "reason": self._removed[track_id],
                    "observed_at_monotonic_s": self._now(),
                    "recovery_claimed": False,
                }
            )

    def _active(self, request_id: str) -> dict[str, Any]:
        record = self._requests.get(request_id)
        if record is None or record["terminal_state"] is not None:
            raise ValueError("m18_request_not_active")
        return record

    def status(self) -> dict[str, Any]:
        with self._lock:
            qualified = []
            for track in self._plan["tracks"]:
                track_id = track["track_id"]
                if track_id not in self._qualified:
                    continue
                qualified.append(
                    {
                        "track_id": track_id,
                        "placement_ids": list(track["placement_ids"]),
                        "traffic_fraction": track["traffic_fraction"],
                        "qualification_id": self._qualified[track_id]["qualification_id"],
                        "qualification_digest": self._qualified[track_id]["qualification_digest"],
                        "admission_state": "removed" if track_id in self._removed else "qualified",
                        "active_request_count": sum(
                            record["track_id"] == track_id and record["terminal_state"] is None
                            for record in self._requests.values()
                        ),
                    }
                )
            document = {
                "protocol": RUNTIME_PROTOCOL,
                "generated_at_monotonic_s": self._now(),
                "deployment": copy.deepcopy(self._plan["deployment"]),
                "replica_plan_digest": self._plan["plan_digest"],
                "parallelism": "data_parallel_request_routing",
                "qualified_tracks": qualified,
                "requests": copy.deepcopy(list(self._requests.values())[-1024:]),
                "incidents": copy.deepcopy(self._incidents[-256:]),
                "throughput": copy.deepcopy(self._throughput),
                "claim_boundary": (
                    "immutable cross-request track binding and per-placement work; no tensor "
                    "parallelism, in-flight migration, or M19 recovery"
                ),
            }
            return validate_replica_runtime(document)


_RUNTIME_FIELDS = frozenset(
    {
        "protocol",
        "generated_at_monotonic_s",
        "deployment",
        "replica_plan_digest",
        "parallelism",
        "qualified_tracks",
        "requests",
        "incidents",
        "throughput",
        "claim_boundary",
    }
)


def validate_replica_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _RUNTIME_FIELDS:
        raise ValueError("M18 replica runtime shape is invalid")
    normalized = _json_copy(document)
    if normalized["protocol"] != RUNTIME_PROTOCOL:
        raise ValueError("M18 replica runtime protocol is invalid")
    if normalized["parallelism"] != "data_parallel_request_routing":
        raise ValueError("M18 runtime parallelism claim is invalid")
    _sha256_ref(normalized["replica_plan_digest"], "replica_plan_digest")
    if len(normalized["qualified_tracks"]) > 512 or len(normalized["requests"]) > 1024:
        raise ValueError("M18 runtime projection exceeds its bound")
    if len(normalized["incidents"]) > 256:
        raise ValueError("M18 incident projection exceeds its bound")
    normalized["throughput"] = _normalize_throughput_evidence(
        normalized["throughput"]
    )
    track_ids = {item.get("track_id") for item in normalized["qualified_tracks"]}
    for request in normalized["requests"]:
        if request.get("track_id") not in track_ids:
            raise ValueError("M18 request is not bound to a projected qualified track")
        if request.get("kv_locality") != "request_track_pinned_no_migration":
            raise ValueError("M18 request KV locality claim is invalid")
        if set(request.get("placement_work", {})) != set(request.get("placement_ids", [])):
            raise ValueError("M18 placement work does not match the immutable track")
    return normalized


def _normalize_throughput_evidence(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _THROUGHPUT_FIELDS:
        raise ValueError("M18 throughput evidence shape is invalid")
    output = {
        "evidence_digest": _sha256_ref(
            value["evidence_digest"], "throughput_evidence_digest"
        ),
        "mode": _nonempty(value["mode"], "throughput_mode"),
        "baseline_request_count": value["baseline_request_count"],
        "baseline_throughput_rps": _nonnegative_number(
            value["baseline_throughput_rps"], "baseline_throughput_rps"
        ),
        "replicated_request_count": value["replicated_request_count"],
        "replicated_throughput_rps": _nonnegative_number(
            value["replicated_throughput_rps"], "replicated_throughput_rps"
        ),
        "gain_fraction": _nonnegative_number(value["gain_fraction"], "gain_fraction"),
        "minimum_required_fraction": _nonnegative_number(
            value["minimum_required_fraction"], "minimum_required_fraction"
        ),
        "passed": value["passed"],
    }
    if any(
        type(output[name]) is not int or output[name] <= 0
        for name in ("baseline_request_count", "replicated_request_count")
    ) or type(output["passed"]) is not bool:
        raise ValueError("M18 throughput evidence counters are invalid")
    measured_gain = (
        output["replicated_throughput_rps"]
        / max(output["baseline_throughput_rps"], 1e-12)
        - 1.0
    )
    if abs(measured_gain - output["gain_fraction"]) > 1e-9 or output["passed"] != (
        measured_gain >= output["minimum_required_fraction"]
    ):
        raise ValueError("M18 throughput evidence result is inconsistent")
    return output


__all__ = [
    "PLAN_PROTOCOL",
    "RUNTIME_PROTOCOL",
    "ReplicaRuntimeLedger",
    "build_replica_plan",
    "validate_replica_plan",
    "validate_replica_runtime",
]
