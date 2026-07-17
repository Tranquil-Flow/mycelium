"""Strict JSON-compatible parsing for the planner-to-Router graph contract."""

from typing import Any

from mycelium_router.contracts import (
   EXECUTION_GRAPH_PROTOCOL,
   ExecutionGraph,
   LayerRange,
   Placement,
   PlacementEdge,
   Stage,
   StageCost,
)
from mycelium_router.validation import ContractError, validate_execution_graph


def execution_graph_from_dict(payload: dict[str, Any]) -> ExecutionGraph:
   if payload.get("protocol") != EXECUTION_GRAPH_PROTOCOL:
      raise ContractError("unsupported_graph_protocol")
   try:
      stages = tuple(
         Stage(
            stage_id=item["stage_id"],
            layer_range=LayerRange(
               start_layer=item["range"]["start_layer"],
               end_layer_exclusive=item["range"]["end_layer_exclusive"],
               layer_count=item["range"]["layer_count"],
            ),
            component_roles=tuple(item["component_roles"]),
            stage_cost=StageCost(
               prefill_work_units_per_prompt_token=item["stage_cost"][
                  "prefill_work_units_per_prompt_token"
               ],
               decode_work_units_per_token=item["stage_cost"][
                  "decode_work_units_per_token"
               ],
               kv_bytes_per_context_token=item["stage_cost"][
                  "kv_bytes_per_context_token"
               ],
            ),
            placements=tuple(
               Placement(
                  placement_id=placement["placement_id"],
                  node_id=placement["node_id"],
                  replica_group_id=placement["replica_group_id"],
                  assignment_id=placement["assignment_id"],
                  stage_signature=placement["stage_signature"],
                  load_proof_digest=placement["load_proof_digest"],
                  runtime_backend=placement["runtime_backend"],
                  runtime_endpoint=placement["runtime_endpoint"],
                  lifecycle_state=placement.get("lifecycle_state", "ACTIVE"),
               )
               for placement in item["placements"]
            ),
         )
         for item in payload["stages"]
      )
      edges = tuple(_edge_from_dict(item) for item in payload["edges"])
      loopbacks = tuple(
         _edge_from_dict(item) for item in payload["loopback_edges"]
      )
      graph = ExecutionGraph(
         protocol=payload["protocol"],
         deployment_id=payload["deployment_id"],
         deployment_epoch=payload["deployment_epoch"],
         topology_version=payload["topology_version"],
         model_id=payload["model_id"],
         resolved_commit=payload["resolved_commit"],
         manifest_digest=payload["manifest_digest"],
         entry_stage_id=payload["entry_stage_id"],
         final_stage_id=payload["final_stage_id"],
         hidden_size=payload["hidden_size"],
         activation_bytes=payload["activation_bytes"],
         token_envelope_bytes=payload["token_envelope_bytes"],
         stages=stages,
         edges=edges,
         loopback_edges=loopbacks,
      )
   except (KeyError, TypeError, ValueError) as error:
      raise ContractError("missing_contract_field", str(error)) from error
   return validate_execution_graph(graph)


def execution_graph_to_dict(graph: ExecutionGraph) -> dict[str, Any]:
   validate_execution_graph(graph)
   return {
      "protocol": graph.protocol,
      "deployment_id": graph.deployment_id,
      "deployment_epoch": graph.deployment_epoch,
      "topology_version": graph.topology_version,
      "model_id": graph.model_id,
      "resolved_commit": graph.resolved_commit,
      "manifest_digest": graph.manifest_digest,
      "entry_stage_id": graph.entry_stage_id,
      "final_stage_id": graph.final_stage_id,
      "hidden_size": graph.hidden_size,
      "activation_bytes": graph.activation_bytes,
      "token_envelope_bytes": graph.token_envelope_bytes,
      "stages": [
         {
            "stage_id": stage.stage_id,
            "range": {
               "start_layer": stage.layer_range.start_layer,
               "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
               "layer_count": stage.layer_range.layer_count,
            },
            "component_roles": list(stage.component_roles),
            "stage_cost": {
               "prefill_work_units_per_prompt_token": (
                  stage.stage_cost.prefill_work_units_per_prompt_token
               ),
               "decode_work_units_per_token": (
                  stage.stage_cost.decode_work_units_per_token
               ),
               "kv_bytes_per_context_token": (
                  stage.stage_cost.kv_bytes_per_context_token
               ),
            },
            "placements": [
               {
                  "placement_id": placement.placement_id,
                  "node_id": placement.node_id,
                  "replica_group_id": placement.replica_group_id,
                  "assignment_id": placement.assignment_id,
                  "stage_signature": placement.stage_signature,
                  "load_proof_digest": placement.load_proof_digest,
                  "runtime_backend": placement.runtime_backend,
                  "runtime_endpoint": placement.runtime_endpoint,
                  "lifecycle_state": placement.lifecycle_state,
               }
               for placement in stage.placements
            ],
         }
         for stage in graph.stages
      ],
      "edges": [_edge_to_dict(edge) for edge in graph.edges],
      "loopback_edges": [_edge_to_dict(edge) for edge in graph.loopback_edges],
   }


def _edge_from_dict(payload: dict[str, Any]) -> PlacementEdge:
   return PlacementEdge(
      edge_id=payload["edge_id"],
      from_placement_id=payload["from_placement_id"],
      to_placement_id=payload["to_placement_id"],
      link_id=payload["link_id"],
   )


def _edge_to_dict(edge: PlacementEdge) -> dict[str, str]:
   return {
      "edge_id": edge.edge_id,
      "from_placement_id": edge.from_placement_id,
      "to_placement_id": edge.to_placement_id,
      "link_id": edge.link_id,
   }
