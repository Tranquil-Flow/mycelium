from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Optional, Tuple

from .contracts import NUMERIC_EPSILON, RoutePlanV2
from .planner import plan_snapshot
from .serialization import dumps_route_plan


EVENT_KINDS = frozenset(
    {
        "device_unavailable",
        "edge_unavailable",
        "device_joined",
        "measurement_drift",
    }
)
ASSESSMENT_ACTIONS = frozenset(
    {"existing_track_intent", "full_replan", "candidate_replan", "no_action"}
)
URGENCIES = frozenset({"immediate", "deferred", "none"})
RECOMMENDATIONS = frozenset(
    {
        "router_builder_validate_existing_track",
        "prefer_route_ready_successor_standby_else_provision_candidate",
        "provision_replanned_intent",
        "provision_candidate",
        "retain_current_plan",
        "no_viable_plan",
    }
)


@dataclass(frozen=True)
class TopologyEvent:
    event_id: str
    snapshot_generation: int
    kind: str
    node_ids: Tuple[str, ...] = ()
    edges: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.snapshot_generation, int) or self.snapshot_generation < 0:
            raise ValueError("snapshot_generation must be a non-negative integer")
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unsupported topology event kind: {self.kind}")

        node_ids = tuple(self.node_ids)
        if any(not isinstance(node_id, str) or not node_id for node_id in node_ids):
            raise ValueError("node_ids must contain non-empty strings")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_ids must be unique")
        object.__setattr__(self, "node_ids", tuple(sorted(node_ids)))

        raw_edges = tuple(self.edges)
        normalized_edges = []
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, (tuple, list)):
                raise ValueError("each edge must be a two-item tuple or list")
            edge = tuple(raw_edge)
            if len(edge) != 2:
                raise ValueError("each edge must contain src and dst")
            src, dst = edge
            if not isinstance(src, str) or not src or not isinstance(dst, str) or not dst:
                raise ValueError("edge endpoints must be non-empty strings")
            if src == dst:
                raise ValueError("topology-event edges must be directed between distinct nodes")
            normalized_edges.append((src, dst))
        if len(set(normalized_edges)) != len(normalized_edges):
            raise ValueError("edges must be unique")
        object.__setattr__(self, "edges", tuple(sorted(normalized_edges)))

        if self.kind == "device_unavailable":
            if not self.node_ids:
                raise ValueError("device_unavailable requires at least one node_id")
            if self.edges:
                raise ValueError("device_unavailable cannot carry directed edges")
        if self.kind == "edge_unavailable":
            if not self.edges:
                raise ValueError("edge_unavailable requires at least one directed edge")
            if self.node_ids:
                raise ValueError("edge_unavailable cannot carry node_ids")
        if self.kind == "device_joined":
            if not self.node_ids:
                raise ValueError("device_joined requires at least one node_id")
            if self.edges:
                raise ValueError("device_joined link facts belong in the snapshot")
        if self.kind == "measurement_drift" and not (self.node_ids or self.edges):
            raise ValueError("measurement_drift requires a node_id or directed edge")


@dataclass(frozen=True)
class ReplanAssessment:
    action: str
    urgency: str
    surviving_track_ids: Tuple[str, ...]
    affected_group_ids: Tuple[str, ...]
    escalation_order: Tuple[str, ...]
    external_readiness_required: bool
    reason: str

    protocol: ClassVar[str] = "mycelium.layer_replan_assessment.v1"

    def __post_init__(self) -> None:
        if self.action not in ASSESSMENT_ACTIONS:
            raise ValueError(f"unsupported replan action: {self.action}")
        if self.urgency not in URGENCIES:
            raise ValueError(f"unsupported replan urgency: {self.urgency}")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("assessment reason must be non-empty")
        allowed_escalations = {
            (),
            ("full_replan",),
            ("successor_standby_candidate", "full_replan"),
        }
        if self.escalation_order not in allowed_escalations:
            raise ValueError("unsupported replan escalation order")
        if self.action == "existing_track_intent" and not self.surviving_track_ids:
            raise ValueError("existing_track_intent requires a surviving track")
        if self.action == "full_replan" and self.surviving_track_ids:
            raise ValueError("full_replan cannot retain a surviving track")


@dataclass(frozen=True)
class ReplanOutcome:
    event: TopologyEvent
    assessment: ReplanAssessment
    previous_plan_digest: str
    candidate_plan_digest: Optional[str]
    candidate_plan: Optional[RoutePlanV2]
    capacity_gain_fraction: Optional[float]
    recommendation: str
    reason: str
    handoff_state: str = "placement_intent_only"

    protocol: ClassVar[str] = "mycelium.layer_replan.v1"

    def __post_init__(self) -> None:
        if self.recommendation not in RECOMMENDATIONS:
            raise ValueError(f"unsupported replan recommendation: {self.recommendation}")
        if self.handoff_state != "placement_intent_only":
            raise ValueError("replan outcomes are placement intent only")
        if not self.previous_plan_digest.startswith("sha256:"):
            raise ValueError("previous_plan_digest must be SHA-256")
        if (self.candidate_plan is None) != (self.candidate_plan_digest is None):
            raise ValueError("candidate plan and digest must be present together")
        if self.candidate_plan_digest is not None and not self.candidate_plan_digest.startswith("sha256:"):
            raise ValueError("candidate_plan_digest must be SHA-256")
        if self.candidate_plan is not None and self.candidate_plan.handoff_state != "placement_intent_only":
            raise ValueError("candidate plan must remain placement intent only")
        if self.capacity_gain_fraction is not None and not math.isfinite(self.capacity_gain_fraction):
            raise ValueError("capacity_gain_fraction must be finite")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("outcome reason must be non-empty")


def replan_outcome_to_dict(outcome: ReplanOutcome) -> dict[str, Any]:
    candidate_plan = (
        json.loads(dumps_route_plan(outcome.candidate_plan))
        if outcome.candidate_plan is not None
        else None
    )
    return {
        "assessment": {
            "action": outcome.assessment.action,
            "affected_group_ids": list(outcome.assessment.affected_group_ids),
            "escalation_order": list(outcome.assessment.escalation_order),
            "external_readiness_required": outcome.assessment.external_readiness_required,
            "protocol": outcome.assessment.protocol,
            "reason": outcome.assessment.reason,
            "surviving_track_ids": list(outcome.assessment.surviving_track_ids),
            "urgency": outcome.assessment.urgency,
        },
        "candidate_plan": candidate_plan,
        "candidate_plan_digest": outcome.candidate_plan_digest,
        "capacity_gain_fraction": outcome.capacity_gain_fraction,
        "event": {
            "edges": [list(edge) for edge in outcome.event.edges],
            "event_id": outcome.event.event_id,
            "kind": outcome.event.kind,
            "node_ids": list(outcome.event.node_ids),
            "snapshot_generation": outcome.event.snapshot_generation,
        },
        "handoff_state": outcome.handoff_state,
        "previous_plan_digest": outcome.previous_plan_digest,
        "protocol": outcome.protocol,
        "reason": outcome.reason,
        "recommendation": outcome.recommendation,
    }


def dumps_replan_outcome(outcome: ReplanOutcome, *, pretty: bool = False) -> str:
    payload = replan_outcome_to_dict(outcome)
    if pretty:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _plan_digest(plan: RoutePlanV2) -> str:
    encoded = dumps_route_plan(plan).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _track_node_edges(plan: RoutePlanV2, placement_ids: Tuple[str, ...]) -> Tuple[Tuple[str, str], ...]:
    placement_nodes = {placement.placement_id: placement.node_id for placement in plan.placements}
    nodes = tuple(placement_nodes[placement_id] for placement_id in placement_ids)
    if len(nodes) < 2:
        return ()
    return tuple(zip(nodes, nodes[1:])) + ((nodes[-1], nodes[0]),)


def assess_topology_event(plan: RoutePlanV2, event: TopologyEvent) -> ReplanAssessment:
    all_track_ids = tuple(track.track_id for track in plan.legal_tracks)
    if event.kind in {"device_joined", "measurement_drift"}:
        return ReplanAssessment(
            action="candidate_replan",
            urgency="deferred",
            surviving_track_ids=all_track_ids,
            affected_group_ids=(),
            escalation_order=("full_replan",),
            external_readiness_required=True,
            reason="new or changed fleet facts merit a hysteresis-gated candidate plan",
        )

    unavailable_nodes = set(event.node_ids)
    unavailable_edges = set(event.edges)
    unavailable_placement_ids = {
        placement.placement_id
        for placement in plan.placements
        if placement.node_id in unavailable_nodes
    }
    affected_group_ids = tuple(
        sorted(
            {
                placement.replica_group_id
                for placement in plan.placements
                if placement.placement_id in unavailable_placement_ids
            }
        )
    )

    surviving_track_ids = []
    event_used_by_route = bool(unavailable_placement_ids)
    for track in plan.legal_tracks:
        track_placements = set(track.placement_ids)
        uses_unavailable_node = bool(track_placements & unavailable_placement_ids)
        node_edges = set(_track_node_edges(plan, track.placement_ids))
        uses_unavailable_edge = bool(node_edges & unavailable_edges)
        event_used_by_route = event_used_by_route or uses_unavailable_edge
        if not uses_unavailable_node and not uses_unavailable_edge:
            surviving_track_ids.append(track.track_id)

    surviving = tuple(surviving_track_ids)
    if not event_used_by_route:
        return ReplanAssessment(
            action="no_action",
            urgency="none",
            surviving_track_ids=all_track_ids,
            affected_group_ids=affected_group_ids,
            escalation_order=(),
            external_readiness_required=False,
            reason="topology event does not affect any current legal track",
        )
    if surviving:
        return ReplanAssessment(
            action="existing_track_intent",
            urgency="immediate",
            surviving_track_ids=surviving,
            affected_group_ids=affected_group_ids,
            escalation_order=(),
            external_readiness_required=True,
            reason="an unaffected legal placement-intent track survives; runtime readiness remains external",
        )

    groups_by_start = {}
    for placement in plan.placements:
        current = groups_by_start.get(placement.replica_group_id)
        layer_start = placement.layer_range.start
        if current is None or layer_start < current:
            groups_by_start[placement.replica_group_id] = layer_start
    ordered_groups = tuple(
        group_id for group_id, _ in sorted(groups_by_start.items(), key=lambda item: (item[1], item[0]))
    )
    standby_eligible = False
    if event.kind == "device_unavailable" and len(event.node_ids) == 1 and len(affected_group_ids) == 1:
        group_index = ordered_groups.index(affected_group_ids[0])
        standby_eligible = 0 < group_index < len(ordered_groups) - 1
    escalation = (
        ("successor_standby_candidate", "full_replan")
        if standby_eligible
        else ("full_replan",)
    )
    return ReplanAssessment(
        action="full_replan",
        urgency="immediate",
        surviving_track_ids=(),
        affected_group_ids=affected_group_ids,
        escalation_order=escalation,
        external_readiness_required=True,
        reason="no complete legal placement-intent track survives the topology event",
    )


def _capacity(plan: RoutePlanV2) -> float:
    try:
        value = float(plan.metrics["replicated_request_capacity_rps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("plan lacks finite replicated_request_capacity_rps") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("replicated_request_capacity_rps must be finite and non-negative")
    return value


def _snapshot_for_event(snapshot: Mapping[str, Any], event: TopologyEvent) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(snapshot))
    nodes = candidate.get("nodes")
    links = candidate.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("snapshot nodes and links must be arrays")

    node_ids = {
        node.get("node_id")
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("node_id"), str)
    }
    if event.kind == "device_joined":
        missing = sorted(set(event.node_ids) - node_ids)
        if missing:
            raise ValueError(f"joined nodes missing from candidate snapshot: {', '.join(missing)}")

    unavailable_nodes = set(event.node_ids) if event.kind == "device_unavailable" else set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("snapshot nodes must be objects")
        if node.get("node_id") in unavailable_nodes:
            node["eligible"] = False
            node["exclusion_reason"] = f"topology_event:{event.event_id}:device_unavailable"

    unavailable_edges = set(event.edges) if event.kind == "edge_unavailable" else set()
    if unavailable_edges:
        filtered_links = []
        for link in links:
            if not isinstance(link, dict):
                raise ValueError("snapshot links must be objects")
            edge = (link.get("src"), link.get("dst"))
            if edge not in unavailable_edges:
                filtered_links.append(link)
        candidate["links"] = filtered_links
    return candidate


def replan_for_event(
    previous_plan: RoutePlanV2,
    snapshot: Mapping[str, Any],
    event: TopologyEvent,
    *,
    min_capacity_gain_fraction: float = 0.05,
) -> ReplanOutcome:
    threshold = float(min_capacity_gain_fraction)
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("min_capacity_gain_fraction must be finite and non-negative")

    assessment = assess_topology_event(previous_plan, event)
    previous_digest = _plan_digest(previous_plan)
    if assessment.action == "no_action":
        return ReplanOutcome(
            event=event,
            assessment=assessment,
            previous_plan_digest=previous_digest,
            candidate_plan_digest=None,
            candidate_plan=None,
            capacity_gain_fraction=None,
            recommendation="retain_current_plan",
            reason=assessment.reason,
        )
    if assessment.action == "existing_track_intent":
        return ReplanOutcome(
            event=event,
            assessment=assessment,
            previous_plan_digest=previous_digest,
            candidate_plan_digest=None,
            candidate_plan=None,
            capacity_gain_fraction=None,
            recommendation="router_builder_validate_existing_track",
            reason=assessment.reason,
        )

    current_capacity = _capacity(previous_plan)
    candidate_snapshot = _snapshot_for_event(snapshot, event)
    try:
        candidate_plan = plan_snapshot(candidate_snapshot)
    except ValueError as exc:
        return ReplanOutcome(
            event=event,
            assessment=assessment,
            previous_plan_digest=previous_digest,
            candidate_plan_digest=None,
            candidate_plan=None,
            capacity_gain_fraction=None,
            recommendation="no_viable_plan",
            reason=f"planner found no viable candidate: {exc}",
        )

    candidate_capacity = _capacity(candidate_plan)
    gain = (candidate_capacity - current_capacity) / max(current_capacity, NUMERIC_EPSILON)
    if assessment.action == "full_replan":
        if "successor_standby_candidate" in assessment.escalation_order:
            recommendation = "prefer_route_ready_successor_standby_else_provision_candidate"
        else:
            recommendation = "provision_replanned_intent"
    else:
        recommendation = "provision_candidate" if gain >= threshold else "retain_current_plan"

    return ReplanOutcome(
        event=event,
        assessment=assessment,
        previous_plan_digest=previous_digest,
        candidate_plan_digest=_plan_digest(candidate_plan),
        candidate_plan=candidate_plan,
        capacity_gain_fraction=gain,
        recommendation=recommendation,
        reason="fresh placement intent computed from the supplied topology snapshot",
    )
