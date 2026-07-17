"""Fail-closed structural validation for Router contracts."""

from mycelium_router.contracts import (
   EXECUTION_GRAPH_PROTOCOL,
   PATH_MANIFEST_PROTOCOL,
   ExecutionGraph,
   LayerRange,
   PathManifest,
)


class ContractError(ValueError):
   """Contract validation failure carrying a stable machine-readable code."""

   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      message = code if not detail else f"{code}: {detail}"
      super().__init__(message)


def _require(condition: bool, code: str, detail: str = "") -> None:
   if not condition:
      raise ContractError(code, detail)


def validate_layer_range(layer_range: LayerRange) -> LayerRange:
   _require(layer_range.start_layer >= 0, "negative_layer_start")
   _require(
      layer_range.end_layer_exclusive > layer_range.start_layer,
      "empty_or_reversed_layer_range",
   )
   _require(
      layer_range.end_layer_exclusive - layer_range.start_layer
      == layer_range.layer_count,
      "range_count_mismatch",
   )
   return layer_range


def validate_execution_graph(graph: ExecutionGraph) -> ExecutionGraph:
   _require(graph.protocol == EXECUTION_GRAPH_PROTOCOL, "unsupported_graph_protocol")
   _require(bool(graph.deployment_id), "missing_deployment_id")
   _require(graph.deployment_epoch >= 0, "invalid_deployment_epoch")
   _require(graph.topology_version >= 0, "invalid_topology_version")
   _require(bool(graph.manifest_digest), "missing_manifest_digest")
   _require(bool(graph.resolved_commit), "missing_resolved_commit")
   _require(graph.hidden_size > 0, "invalid_hidden_size")
   _require(graph.activation_bytes > 0, "invalid_activation_bytes")
   _require(graph.token_envelope_bytes >= 0, "invalid_token_envelope_bytes")
   _require(bool(graph.stages), "missing_stages")

   stage_ids: set[str] = set()
   placement_ids: set[str] = set()
   placement_stage: dict[str, int] = {}
   for stage_index, stage in enumerate(graph.stages):
      _require(stage.stage_id not in stage_ids, "duplicate_stage_id", stage.stage_id)
      stage_ids.add(stage.stage_id)
      validate_layer_range(stage.layer_range)
      _require(bool(stage.placements), "stage_without_placements", stage.stage_id)
      _require(
         stage.stage_cost.prefill_work_units_per_prompt_token > 0,
         "invalid_prefill_cost",
         stage.stage_id,
      )
      _require(
         stage.stage_cost.decode_work_units_per_token > 0,
         "invalid_decode_cost",
         stage.stage_id,
      )
      _require(
         stage.stage_cost.kv_bytes_per_context_token >= 0,
         "invalid_kv_cost",
         stage.stage_id,
      )
      if stage_index:
         previous = graph.stages[stage_index - 1].layer_range
         _require(
            stage.layer_range.start_layer == previous.end_layer_exclusive,
            "layer_gap_or_overlap",
            stage.stage_id,
         )
      for placement in stage.placements:
         _require(
            placement.placement_id not in placement_ids,
            "duplicate_placement_id",
            placement.placement_id,
         )
         placement_ids.add(placement.placement_id)
         placement_stage[placement.placement_id] = stage_index
         _require(bool(placement.node_id), "missing_node_id", placement.placement_id)
         _require(
            bool(placement.assignment_id),
            "missing_assignment_id",
            placement.placement_id,
         )
         _require(
            bool(placement.stage_signature),
            "missing_stage_signature",
            placement.placement_id,
         )
         _require(
            bool(placement.load_proof_digest),
            "missing_load_proof",
            placement.placement_id,
         )
         _require(
            placement.lifecycle_state in {"ACTIVE", "RETIRING"},
            "invalid_placement_lifecycle",
            placement.placement_id,
         )

   _require(
      graph.entry_stage_id == graph.stages[0].stage_id,
      "invalid_entry_stage",
   )
   _require(
      graph.final_stage_id == graph.stages[-1].stage_id,
      "invalid_final_stage",
   )

   edge_ids: set[str] = set()
   edge_pairs: set[tuple[str, str]] = set()
   for edge in graph.edges:
      _require(edge.edge_id not in edge_ids, "duplicate_edge_id", edge.edge_id)
      edge_ids.add(edge.edge_id)
      _require(
         edge.from_placement_id in placement_stage
         and edge.to_placement_id in placement_stage,
         "unknown_edge_placement",
         edge.edge_id,
      )
      _require(
         placement_stage[edge.to_placement_id]
         == placement_stage[edge.from_placement_id] + 1,
         "non_adjacent_stage_edge",
         edge.edge_id,
      )
      pair = (edge.from_placement_id, edge.to_placement_id)
      _require(pair not in edge_pairs, "duplicate_edge_pair", edge.edge_id)
      edge_pairs.add(pair)

   loopback_ids: set[str] = set()
   loopback_sources: set[str] = set()
   entry_placements = {
      placement.placement_id for placement in graph.stages[0].placements
   }
   final_placements = {
      placement.placement_id for placement in graph.stages[-1].placements
   }
   for edge in graph.loopback_edges:
      _require(
         edge.edge_id not in edge_ids and edge.edge_id not in loopback_ids,
         "duplicate_edge_id",
         edge.edge_id,
      )
      loopback_ids.add(edge.edge_id)
      _require(
         edge.from_placement_id in final_placements
         and edge.to_placement_id in entry_placements,
         "invalid_loopback",
         edge.edge_id,
      )
      loopback_sources.add(edge.from_placement_id)
   for final_placement_id in final_placements:
      _require(
         final_placement_id in loopback_sources,
         "missing_loopback",
         final_placement_id,
      )
   return graph


def validate_manifest(
   manifest: PathManifest, graph: ExecutionGraph
) -> PathManifest:
   validate_execution_graph(graph)
   _require(manifest.protocol == PATH_MANIFEST_PROTOCOL, "unsupported_manifest_protocol")
   _require(manifest.deployment_id == graph.deployment_id, "deployment_id_mismatch")
   _require(
      manifest.deployment_epoch == graph.deployment_epoch,
      "deployment_epoch_mismatch",
   )
   _require(
      manifest.topology_version == graph.topology_version,
      "topology_version_mismatch",
   )
   _require(manifest.manifest_digest == graph.manifest_digest, "manifest_digest_mismatch")
   _require(
      len(manifest.ordered_hops) == len(graph.stages),
      "manifest_stage_count_mismatch",
   )

   placement_by_id = {
      placement.placement_id: placement
      for stage in graph.stages
      for placement in stage.placements
   }
   legal_edges = {
      (edge.from_placement_id, edge.to_placement_id) for edge in graph.edges
   }
   for stage, hop in zip(graph.stages, manifest.ordered_hops):
      _require(hop.stage_id == stage.stage_id, "manifest_stage_order_mismatch")
      _require(hop.placement_id in placement_by_id, "unknown_manifest_placement")
      _require(
         hop.placement_id in {item.placement_id for item in stage.placements},
         "placement_stage_mismatch",
      )
      _require(bool(hop.reservation_id), "missing_reservation_id", hop.placement_id)
   for left, right in zip(manifest.ordered_hops, manifest.ordered_hops[1:]):
      _require(
         (left.placement_id, right.placement_id) in legal_edges,
         "illegal_manifest_edge",
         f"{left.placement_id}->{right.placement_id}",
      )

   first = manifest.ordered_hops[0].placement_id
   last = manifest.ordered_hops[-1].placement_id
   legal_loopback_ids = {
      edge.edge_id
      for edge in graph.loopback_edges
      if edge.from_placement_id == last and edge.to_placement_id == first
   }
   _require(
      manifest.loopback_edge_id in legal_loopback_ids,
      "illegal_manifest_loopback",
   )
   return manifest
