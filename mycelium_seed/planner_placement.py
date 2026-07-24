# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planner-derived placement source for measured-capacity seed decisions.

This module implements the full planner placement pipeline:

    Gossip EvidenceBundle
      -> planner_snapshot_from_evidence_bundle
      -> plan_snapshot (RoutePlanV2)
      -> compile_bound_layer_assignments
      -> PlacementDecision(provenance=planner_v2)

The output is placement *intent only* — never route readiness.  It drops into
the seed coordinator through the same :class:`PlacementSource` protocol as
:class:`~mycelium_seed.placement.FrozenPlacementSource`, requiring zero edits
to the coordinator, HTTP API, or node agent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    planner_snapshot_from_evidence_bundle,
)
from mycelium_layer_planner.serialization import route_plan_to_dict
from mycelium_seed.placement import (
    PlacementDecision,
    PlacementError,
    PlacementSource,
)
from planner_assignment import compile_bound_layer_assignments


class PlannerPlacementSource(PlacementSource):
    """Compile placement intent from measured gossip evidence.

    Caches the planner snapshot, route plan, and compiled assignments at
    construction time.  :meth:`compile` validates that the live member set
    is consistent with the cached evidence before returning the decision.
    """

    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        evidence_bundle: Mapping[str, Any],
        planner_model: Mapping[str, Any],
        workload: Mapping[str, Any],
        policy: Mapping[str, Any],
        runtime_by_node: dict[str, dict[str, Any]],
        deployment_epoch: int | None = None,
    ) -> None:
        self._manifest = manifest
        self._evidence_bundle = evidence_bundle
        self._planner_model = planner_model
        self._workload = workload
        self._policy = policy
        self._runtime_by_node = runtime_by_node
        self._deployment_id = evidence_bundle["deployment"]["deployment_id"]
        self._deployment_epoch = (
            deployment_epoch
            if deployment_epoch is not None
            else evidence_bundle["deployment"]["deployment_epoch"]
        )

        # Pin the evidence bundle digest at construction so we can detect
        # stale or mixed-generation evidence later.
        self._source_digest = evidence_bundle["evidence_bundle_digest"]

        # Run the planner pipeline eagerly so construction fails fast on
        # invalid evidence.
        self._planner_snapshot = planner_snapshot_from_evidence_bundle(
            evidence_bundle,
            model=dict(planner_model),
            workload=dict(workload),
            policy=dict(policy),
        )
        self._route_plan = route_plan_to_dict(
            plan_evidence_bundle(
                evidence_bundle,
                model=dict(planner_model),
                workload=dict(workload),
                policy=dict(policy),
            )
        )

        # Collect the node IDs that the planner actually placed.
        self._planned_node_ids = frozenset(
            placement["node_id"] for placement in self._route_plan["placements"]
        )

        # Compile bound assignments.
        self._assignments = compile_bound_layer_assignments(
            route_plan=self._route_plan,
            planner_snapshot=self._planner_snapshot,
            evidence_bundle=evidence_bundle,
            manifest=manifest,
            deployment_id=self._deployment_id,
            deployment_epoch=self._deployment_epoch,
            cache_roots={
                node_id: runtime_by_node.get(node_id, {}).get(
                    "cache_root", f"/var/lib/mycelium/{node_id}"
                )
                for node_id in self._planned_node_ids
            },
            runtime_by_node=runtime_by_node,
        )

    @property
    def source_digest(self) -> str:
        return self._source_digest

    def compile(self, members: Sequence) -> PlacementDecision:
        """Validate members against pinned evidence and return placement intent."""
        if isinstance(members, (str, bytes)) or not isinstance(members, Sequence):
            raise PlacementError("placement_members_invalid")

        # Verify the evidence bundle digest has not changed.
        current_digest = self._evidence_bundle.get("evidence_bundle_digest", "")
        if current_digest != self._source_digest:
            raise PlacementError("planner_evidence_stale")

        # Verify deployment epoch consistency.
        bundle_epoch = self._evidence_bundle.get("deployment", {}).get("deployment_epoch")
        if bundle_epoch is not None and bundle_epoch != self._deployment_epoch:
            raise PlacementError("planner_epoch_mismatch")

        # Validate that every planned node has a corresponding eligible member.
        member_node_ids: set[str] = set()
        for member in members:
            node_id = getattr(member, "node_id", None)
            if node_id is None:
                raise PlacementError("placement_member_invalid")
            member_node_ids.add(node_id)

        # Every planned node must be present in the member set.
        missing = self._planned_node_ids - member_node_ids
        if missing:
            raise PlacementError("planner_member_missing")

        # Build the assignment entries for the PlacementDecision.
        assignment_entries: list[dict[str, Any]] = []
        for assignment in self._assignments:
            node_id = assignment["node_id"]
            entry: dict[str, Any] = {
                "node_id": node_id,
                "assignment_id": assignment["assignment_id"],
                "range": {
                    "start_layer": assignment["range"]["start_layer"],
                    "end_layer_exclusive": assignment["range"]["end_layer_exclusive"],
                    "layer_count": assignment["range"]["layer_count"],
                },
            }
            assignment_entries.append(entry)

        # Verify there are no duplicate node IDs.
        seen: set[str] = set()
        for entry in assignment_entries:
            if entry["node_id"] in seen:
                raise PlacementError("placement_assignment_duplicate_member")
            seen.add(entry["node_id"])

        return PlacementDecision(
            placement_provenance="planner_v2",
            placement_id=f"planner-{self._source_digest[7:19]}",
            assignments=tuple(assignment_entries),
            source_digest=self._source_digest,
        )
