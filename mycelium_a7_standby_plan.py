# SPDX-License-Identifier: AGPL-3.0-only
"""A7 operator planning: a stage standby placement and its per-node A7 files.

Owner ruling R7 topology: the stage-1 source runs on the laptop (node-2) and the
stage-1 standby is a second MLX placement on node-0, beside node-0's ordinary
stage-0 placement.  This module produces, deterministically and offline:

* the standby's own assignment for the covered stage's exact layer range,
  provisioned, compiled into a stage pack and verified (the standby acquires and
  loads its own artifacts; it never borrows the source's);
* the execution graph extended with that STANDBY placement after the existing
  placements of its stage (so ordinary routing and the route's default
  placement selection never pick it), plus the forward and loopback edges the
  successor path needs; and
* the closed ``a7/node.json`` configurations for the standby/entry node and the
  source node, with the standby's staged owner-only key material and the
  public records pinned on the other roles.

The standby qualification record is produced separately by the qualifier from
measured identities and an executed parity challenge; this module never invents
one.
"""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from layer_assignment import compile_layer_assignments
from mycelium_a7_node_wiring import (
   A7_NODE_CONFIG_PROTOCOL,
   STANDBY_SECRET_BYTES,
   standby_public_records,
)
from mycelium_router.contracts import ExecutionGraph, PlacementEdge
from mycelium_router.validation import validate_execution_graph
from stage_pack import artifact_report_for_loader, compile_stage_pack, verify_stage_pack
from weight_provisioning import artifact_report_errors, provision_assignment

STANDBY_PLAN_PROTOCOL = "mycelium.a7_standby_plan.v1"


class StandbyPlanError(ValueError):
   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def standby_placement_digest(placement_id: str) -> str:
   return _digest(placement_id)


def compile_standby_artifacts(
   *,
   manifest: Mapping[str, Any],
   route_plan: Mapping[str, Any],
   stage_index: int,
   standby_node_id: str,
   deployment_id: str,
   deployment_epoch: int,
   cache_root: Path,
   runtime: Mapping[str, str],
   control_plane_binding: Mapping[str, Any],
   fetch_file: Any,
) -> dict[str, Any]:
   """The standby's own assignment, stage pack and loader report for one stage."""

   route = copy.deepcopy(dict(route_plan))
   hops = route.get("route")
   if not isinstance(hops, list) or not 0 <= stage_index < len(hops):
      raise StandbyPlanError("a7_standby_stage_invalid")
   # Compile the deployment's exact ranges with the covered stage bound to the
   # standby node; the other stages get unused placeholder nodes.
   nodes = [f"a7-unused-{index}" for index in range(len(hops))]
   nodes[stage_index] = standby_node_id
   route["route"] = [dict(hop, node_id=node) for hop, node in zip(hops, nodes, strict=True)]
   route["node_order"] = nodes
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=dict(manifest),
      deployment_id=deployment_id,
      deployment_epoch=deployment_epoch,
      cache_roots={node: str(cache_root) for node in nodes},
      runtime_by_node={node: dict(runtime) for node in nodes},
      control_plane_binding=copy.deepcopy(dict(control_plane_binding)),
   )
   assignment = assignments[stage_index]
   if assignment.get("node_id") != standby_node_id:
      raise StandbyPlanError("a7_standby_assignment_invalid")
   report = provision_assignment(assignment, fetch_file=fetch_file, local_files_only=True)
   errors = artifact_report_errors(assignment, report)
   if errors:
      raise StandbyPlanError("a7_standby_artifacts_invalid")
   pack = compile_stage_pack(assignment, dict(manifest), report)
   verification = verify_stage_pack(pack, assignment=assignment, manifest=dict(manifest))
   loader_report = artifact_report_for_loader(pack, verification, assignment=assignment, manifest=dict(manifest))
   return {"assignment": assignment, "stage_pack": pack, "loader_report": loader_report}


def extend_graph_with_standby(
   graph: ExecutionGraph,
   *,
   stage_index: int,
   placement_id: str,
   node_id: str,
   assignment_id: str,
   load_proof_digest: str,
   link_scheme: str = "iroh",
) -> ExecutionGraph:
   """Append the STANDBY placement to its stage with the edges a successor needs."""

   if not 0 <= stage_index < len(graph.stages):
      raise StandbyPlanError("a7_standby_stage_invalid")
   stage = graph.stages[stage_index]
   if not stage.placements or any(p.placement_id == placement_id for s in graph.stages for p in s.placements):
      raise StandbyPlanError("a7_standby_placement_conflict")
   if any(p.runtime_backend.startswith("mobile-rust-") for s in graph.stages for p in s.placements
          if s.stage_id == stage.stage_id or p.node_id == node_id):
      raise StandbyPlanError("a7_phone_kv_unsupported")
   template = stage.placements[0]
   standby = replace(
      template,
      placement_id=placement_id,
      node_id=node_id,
      assignment_id=assignment_id,
      load_proof_digest=load_proof_digest,
      runtime_endpoint=f"{link_scheme}://{node_id}/{assignment_id}",
      lifecycle_state="STANDBY",
   )
   stages = list(graph.stages)
   stages[stage_index] = replace(stage, placements=stage.placements + (standby,))
   edges = list(graph.edges)
   if stage_index > 0:
      for previous in graph.stages[stage_index - 1].placements:
         if previous.lifecycle_state == "STANDBY":
            continue
         edges.append(
            PlacementEdge(
               edge_id=f"forward:{previous.placement_id}->{placement_id}",
               from_placement_id=previous.placement_id,
               to_placement_id=placement_id,
               link_id=f"{link_scheme}:{previous.node_id}->{node_id}",
            )
         )
   if stage_index + 1 < len(graph.stages):
      for following in graph.stages[stage_index + 1].placements:
         if following.lifecycle_state == "STANDBY":
            continue
         edges.append(
            PlacementEdge(
               edge_id=f"forward:{placement_id}->{following.placement_id}",
               from_placement_id=placement_id,
               to_placement_id=following.placement_id,
               link_id=f"{link_scheme}:{node_id}->{following.node_id}",
            )
         )
   loopbacks = list(graph.loopback_edges)
   if stage_index == len(graph.stages) - 1:
      for entry in graph.stages[0].placements:
         if entry.lifecycle_state == "STANDBY":
            continue
         loopbacks.append(
            PlacementEdge(
               edge_id=f"loopback:{placement_id}->{entry.placement_id}",
               from_placement_id=placement_id,
               to_placement_id=entry.placement_id,
               link_id=f"{link_scheme}:{node_id}->{entry.node_id}",
            )
         )
   extended = replace(graph, stages=tuple(stages), edges=tuple(edges), loopback_edges=tuple(loopbacks))
   return validate_execution_graph(extended)


def write_standby_secret(path: Path) -> bytes:
   """Create the standby's owner-only key material once (never overwritten)."""

   path.parent.mkdir(parents=True, exist_ok=True)
   secret = os.urandom(STANDBY_SECRET_BYTES)
   descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
   try:
      os.write(descriptor, secret)
      os.fsync(descriptor)
   finally:
      os.close(descriptor)
   return secret


def node_configs(
   *,
   deployment_id: str,
   graph: ExecutionGraph,
   stage_index: int,
   standby_placement_id: str,
   standby_secret: bytes,
   standby_incarnation: str,
   standby_files: Mapping[str, str],
   standby_load_generation: int,
   qualification_file: str,
   fence_directory: str = "a7-fences",
   max_checkpoint_bytes: int = 64 * 1024 * 1024,
   max_retained_checkpoints: int = 512,
   ack_timeout_seconds: float = 2.0,
   receipt_timeout_seconds: float = 5.0,
   a6_available: bool = True,
) -> dict[str, dict[str, Any]]:
   """Closed ``a7/node.json`` documents keyed by node id (entry/standby and source)."""

   stage = graph.stages[stage_index]
   standby = next((p for p in stage.placements if p.placement_id == standby_placement_id), None)
   sources = [p for p in stage.placements if p.lifecycle_state == "ACTIVE"]
   if standby is None or standby.lifecycle_state != "STANDBY" or len(sources) != 1:
      raise StandbyPlanError("a7_standby_plan_graph_invalid")
   source = sources[0]
   entry_node = graph.stages[0].placements[0].node_id
   placement_digest = standby_placement_digest(standby_placement_id)
   records = standby_public_records(standby_secret, node_id=standby.node_id, placement_digest=placement_digest, incarnation=standby_incarnation)
   configs: dict[str, dict[str, Any]] = {}

   def config_for(node_id: str) -> dict[str, Any]:
      return configs.setdefault(
         node_id,
         {"protocol": A7_NODE_CONFIG_PROTOCOL, "deployment_id": deployment_id, "node_id": node_id, "standby": None, "source": None, "entry": None},
      )

   config_for(standby.node_id)["standby"] = {
      "placement_id": standby_placement_id,
      "placement_digest": placement_digest,
      "incarnation": standby_incarnation,
      "assignment_file": standby_files["assignment_file"],
      "manifest_file": standby_files["manifest_file"],
      "stage_pack_file": standby_files["stage_pack_file"],
      "load_generation": standby_load_generation,
      "secret_file": standby_files["secret_file"],
      "max_checkpoint_bytes": max_checkpoint_bytes,
      "max_retained_checkpoints": max_retained_checkpoints,
   }
   config_for(source.node_id)["source"] = {
      "source_placement_id": source.placement_id,
      "source_placement_digest": _digest(source.placement_id),
      "stage_id": stage.stage_id,
      "standby_node_id": standby.node_id,
      "standby_placement_id": standby_placement_id,
      "standby_placement_digest": placement_digest,
      "standby_incarnation": standby_incarnation,
      "standby_signer_record": records["signer_record"],
      "standby_key_record": records["key_record"],
      "max_checkpoint_bytes": max_checkpoint_bytes,
   }
   config_for(entry_node)["entry"] = {
      "fence_directory": fence_directory,
      "coverage": [
         {
            "stage_id": stage.stage_id,
            "source_placement_id": source.placement_id,
            "source_node_id": source.node_id,
            "standby_placement_id": standby_placement_id,
            "standby_node_id": standby.node_id,
            "standby_incarnation": standby_incarnation,
            "standby_signer_record": records["signer_record"],
            "qualification_file": qualification_file,
         }
      ],
      "a6_available": a6_available,
      "ack_timeout_seconds": ack_timeout_seconds,
      "receipt_timeout_seconds": receipt_timeout_seconds,
   }
   return configs


def plan_summary(graph: ExecutionGraph, *, stage_index: int, standby_placement_id: str) -> dict[str, Any]:
   """Privacy-reduced standby intent for the Plans workspace and the gate plan."""

   stage = graph.stages[stage_index]
   standby = next(p for p in stage.placements if p.placement_id == standby_placement_id)
   source = next(p for p in stage.placements if p.lifecycle_state == "ACTIVE")
   return {
      "protocol": STANDBY_PLAN_PROTOCOL,
      "stage_id": stage.stage_id,
      "layer_range": [stage.layer_range.start_layer, stage.layer_range.end_layer_exclusive],
      "covered_failure": "source_placement",
      "source_placement_digest": _digest(source.placement_id),
      "standby_placement_digest": standby_placement_digest(standby_placement_id),
      "same_failure_domain_as_source": standby.node_id == source.node_id,
      "runtime_backend": standby.runtime_backend,
      "route_ready": False,
   }


def update_plan_graphs(plan: Mapping[str, Any], graph_document: Mapping[str, Any]) -> dict[str, Any]:
   """Replace every node's configure graph in a route run plan with the extended graph."""

   updated = copy.deepcopy(dict(plan))
   nodes = updated.get("nodes")
   if not isinstance(nodes, list) or not nodes:
      raise StandbyPlanError("a7_plan_nodes_invalid")
   for node in nodes:
      configure = node.get("configure") if isinstance(node, dict) else None
      if not isinstance(configure, dict) or "graph" not in configure:
         raise StandbyPlanError("a7_plan_nodes_invalid")
      configure["graph"] = copy.deepcopy(dict(graph_document))
   return updated


def require_distinct_failure_domains(graph: ExecutionGraph, *, stage_index: int, standby_placement_id: str) -> None:
   if any(p.runtime_backend.startswith("mobile-rust-") for p in graph.stages[stage_index].placements):
      raise StandbyPlanError("a7_phone_kv_unsupported")
   summary = plan_summary(graph, stage_index=stage_index, standby_placement_id=standby_placement_id)
   if summary["same_failure_domain_as_source"]:
      raise StandbyPlanError("a7_standby_shares_source_failure_domain")


__all__: Sequence[str] = (
   "StandbyPlanError",
   "compile_standby_artifacts",
   "extend_graph_with_standby",
   "node_configs",
   "plan_summary",
   "require_distinct_failure_domains",
   "standby_placement_digest",
   "update_plan_graphs",
   "write_standby_secret",
)
