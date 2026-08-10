from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import Any, Mapping

from .contracts import (
    DirectedLinkObservation,
    LegalTrack,
    Loopback,
    ModelIdentity,
    NodeCapability,
    PlanEdge,
    PlanningPolicy,
    RoutePlanV2,
    SpeculativeConfig,
    WorkloadScenario,
)
from .physical_graph import build_physical_graph
from .primary_plan import admit_primary_nodes, plan_primary
from .replication import replicate_stages
from .speculative import score_speculative
from .workload import WorkloadProfile, empirical_interactive_chat, mlperf_qa_stress


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_policy(data: Mapping[str, Any]) -> PlanningPolicy:
    allowed = {field.name for field in fields(PlanningPolicy)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown planning policy fields: {sorted(unknown)}")
    return PlanningPolicy(**data)


def _parse_workload(data: Mapping[str, Any]) -> WorkloadProfile:
    preset = data.get("preset")
    if preset == "interactive_chat_v1":
        return empirical_interactive_chat(
            user_scale=float(data.get("user_scale", 1.0)),
            system_prefix_tokens=int(data.get("system_prefix_tokens", 0)),
            history_tokens=int(data.get("history_tokens", 0)),
            concurrency_points=tuple(int(value) for value in data.get("concurrency_points", (1, 2, 4, 8, 16, 32))),
        )
    if preset == "qa_benchmark_v1":
        return mlperf_qa_stress(user_scale=float(data.get("user_scale", 1.0)))
    if preset is not None:
        raise ValueError(f"unknown workload preset: {preset}")
    scenarios = tuple(WorkloadScenario(**scenario) for scenario in data.get("scenarios", ()))
    return WorkloadProfile(
        name=str(data.get("name", "custom")),
        scenarios=scenarios,
        mode=str(data.get("mode", "sensitivity_grid")),
        source=str(data.get("source", "user_configured")),
        arrival_process=str(data.get("arrival_process", "poisson_capacity_sweep")),
        average_turns=data.get("average_turns"),
    )


def _parse_speculative(data: Mapping[str, Any] | None) -> SpeculativeConfig | None:
    if not data:
        return None
    distribution = tuple(float(value) for value in data["accepted_count_distribution"])
    return SpeculativeConfig(
        draft_model_id=str(data["draft_model_id"]),
        draft_revision=str(data["draft_revision"]),
        proposal_width=int(data["proposal_width"]),
        accepted_count_distribution=distribution,
    )


def plan_snapshot(snapshot: Mapping[str, Any]) -> RoutePlanV2:
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a mapping")
    try:
        model = ModelIdentity(**snapshot["model"])
        nodes = tuple(NodeCapability(**node) for node in snapshot["nodes"])
        links = tuple(DirectedLinkObservation(**link) for link in snapshot["links"])
        policy = _parse_policy(snapshot.get("policy", {}))
        profile = _parse_workload(snapshot.get("workload", {"preset": "interactive_chat_v1"}))
    except KeyError as exc:
        raise ValueError(f"snapshot missing required field: {exc.args[0]}") from exc
    graph = build_physical_graph(nodes, links, policy)
    explicit_admission = snapshot.get("admitted_node_ids")
    if explicit_admission is None:
        # Compatibility path for research fixtures that predate explicit admission.
        admitted_node_ids = admit_primary_nodes(graph, model, profile, policy)
    else:
        if (
            not isinstance(explicit_admission, (list, tuple))
            or not explicit_admission
            or any(not isinstance(node_id, str) for node_id in explicit_admission)
            or len(set(explicit_admission)) != len(explicit_admission)
        ):
            raise ValueError("admitted_node_ids is invalid")
        admitted_node_ids = tuple(explicit_admission)
    primary = plan_primary(
        graph,
        model,
        profile,
        policy,
        admitted_node_ids=admitted_node_ids,
    )
    representative = max(
        profile.scenarios,
        key=lambda scenario: (scenario.total_context_tokens * scenario.concurrency, scenario.name),
    )
    replicated = replicate_stages(primary, graph, model, representative, policy)
    legal_tracks = tuple(
        LegalTrack(
            track_id=f"track-{index:03}",
            placement_ids=track.placement_ids,
            traffic_fraction=track.traffic_fraction,
            cost_ms=track.cost_ms,
        )
        for index, track in enumerate(replicated.flow.tracks)
    )
    forward_edges = tuple(
        PlanEdge(edge.src, edge.dst, edge.capacity, edge.cost_ms, "forward")
        for edge in replicated.forward_edges
    )
    loopbacks = tuple(
        Loopback(edge.src, edge.dst, 16, edge.cost_ms)
        for edge in replicated.loopback_edges
    )
    scenario_metrics = [
        {
            "name": score.scenario_name,
            "ttft_ms": score.ttft_ms,
            "tpot_ms": score.tpot_ms,
            "single_request_tps": score.single_request_tps,
            "output_goodput_tps": score.output_goodput_tps,
            "prefill_compute_ms": score.prefill_compute_ms,
            "prefill_transfer_ms": score.prefill_network_ms,
            "decode_compute_ms": score.decode_compute_ms,
            "decode_transfer_ms": score.decode_network_ms + score.decode_loopback_ms,
            "prefill_payload_bytes": score.prefill_payload_bytes,
            "decode_payload_bytes": score.decode_payload_bytes,
            "expected_response_ms": score.expected_response_ms,
            "required_memory_bytes": score.required_memory_bytes,
            "confidence": score.confidence,
        }
        for score in primary.scenario_scores
    ]
    metrics: dict[str, Any] = {
        "scenarios": scenario_metrics,
        "replicated_request_capacity_rps": replicated.flow.admitted,
        "replicated_output_capacity_tps": replicated.flow.admitted * representative.output_tokens,
        "unmet_capacity_sweep_demand_rps": replicated.flow.unmet_demand,
    }
    speculative = _parse_speculative(snapshot.get("speculative"))
    speculative_diagnostics: dict[str, Any] = {"enabled": False, "target_fallback": True}
    if speculative is not None:
        timing = snapshot.get("speculative_timing", {})
        result = score_speculative(
            speculative,
            float(timing.get("target_decode_ms", primary.scenario_scores[0].tpot_ms)),
            float(timing.get("draft_decode_ms", primary.scenario_scores[0].tpot_ms)),
            float(timing.get("verification_ms", primary.scenario_scores[0].tpot_ms)),
            float(timing.get("proposal_transfer_ms", 0.0)),
            runtime_supported=bool(timing.get("runtime_supported", False)),
            proposal_payload_bytes=int(timing.get("proposal_payload_bytes", 16)),
        )
        speculative_diagnostics = {
            "enabled": result.enabled,
            "use_speculative": result.use_speculative,
            "reason": result.reason,
            "target_fallback": result.target_fallback,
            "expected_accepted_tokens": result.expected_accepted_tokens,
            "draft_kv_owner": result.draft_kv_owner,
            "target_kv_owner": result.target_kv_owner,
        }
    diagnostics = {
        "placement_provenance": snapshot.get("placement_provenance", "planner_v2"),
        "decode_mode": snapshot.get("decode_mode", "unknown"),
        "node_runtime": dict(snapshot.get("node_runtime", {})),
        "topology": "ordered_stage_groups_with_independent_loops_and_cross_edges",
        "primary_order": list(primary.order),
        "frozen_primary_order": list(primary.frozen_primary_order),
        "candidate_node_ids": list(primary.candidate_node_ids),
        "admitted_node_ids": list(admitted_node_ids),
        "unplaced_node_ids": list(primary.unplaced_node_ids),
        "accepted_replica_nodes": list(replicated.accepted_replica_nodes),
        "excluded_nodes": dict(graph.exclusions),
        "workload": {
            "name": profile.name,
            "source": profile.source,
            "mode": profile.mode,
            "arrival_process": profile.arrival_process,
            "user_scale": profile.scenarios[0].user_scale,
            "network_size_changes_capacity_not_demand": True,
        },
        "path_cost": "phase_sensitive_latency_plus_payload_over_bandwidth",
        "primary_search": list(primary.diagnostics),
        "speculative": speculative_diagnostics,
    }
    plan = RoutePlanV2(
        model=model,
        snapshot_digest=_snapshot_digest(snapshot),
        placements=replicated.placements,
        legal_tracks=legal_tracks,
        forward_edges=forward_edges,
        loopbacks=loopbacks,
        provenance=primary.provenance,
        workload_name=profile.name,
        metrics=metrics,
        diagnostics=diagnostics,
    )
    from .validation import validate_route_plan

    validate_route_plan(plan)
    return plan
