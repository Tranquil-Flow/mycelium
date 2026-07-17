"""Deterministic control-plane to Router execution-graph Layer Builder.

The builder consumes one already validated atomic control-plane tranche plus one
local load proof per assignment.  Its output is a *candidate* execution graph:
load proofs deliberately retain ``route_ready: false`` until a later route
challenge proves the complete distributed path.  This module does not publish
or execute the graph.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from mycelium_gossip.evidence_bundle import (
   evidence_bundle_from_dict,
   evidence_bundle_to_dict,
)
from mycelium_router.contracts import (
   DeviceState,
   ExecutionGraph,
   LayerRange,
   Placement,
   PlacementEdge,
   Stage,
   StageCost,
)
from mycelium_router.validation import validate_execution_graph
from planner_assignment import validate_control_plane_tranche


LAYER_LOAD_PROOF_PROTOCOL = "mycelium.layer_load_proof.v1"
_SHA256_PREFIX = "sha256:"


class LayerBuildError(ValueError):
   """Fail-closed Layer Builder error carrying a stable code."""

   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
   if not condition:
      raise LayerBuildError(code, detail)


def _canonical_json(value: Any) -> bytes:
   try:
      return json.dumps(
         value,
         sort_keys=True,
         separators=(",", ":"),
         ensure_ascii=False,
         allow_nan=False,
      ).encode("utf-8")
   except (TypeError, ValueError) as exc:
      raise LayerBuildError("non_canonical_json", str(exc)) from exc


def _canonical_equal(left: Any, right: Any) -> bool:
   return _canonical_json(left) == _canonical_json(right)


def _sha256_ref(value: Any) -> bool:
   if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
      return False
   digest = value[len(_SHA256_PREFIX) :]
   return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def layer_load_proof_digest(proof: Mapping[str, Any]) -> str:
   """Return canonical digest of the complete immutable load-proof document."""
   _require(isinstance(proof, Mapping), "invalid_load_proof")
   return _SHA256_PREFIX + hashlib.sha256(_canonical_json(dict(proof))).hexdigest()


def _validate_endpoint(value: Any, assignment_id: str) -> str:
   _require(isinstance(value, str) and value == value.strip() and value, "missing_runtime_endpoint", assignment_id)
   _require(not any(ord(character) < 32 for character in value), "unsafe_runtime_endpoint", assignment_id)
   try:
      parsed = urlsplit(value)
   except ValueError as exc:
      raise LayerBuildError("unsafe_runtime_endpoint", assignment_id) from exc
   _require(bool(parsed.scheme), "unsafe_runtime_endpoint", assignment_id)
   _require(
      parsed.username is None
      and parsed.password is None
      and not parsed.query
      and not parsed.fragment,
      "unsafe_runtime_endpoint",
      assignment_id,
   )
   return value


def _validate_load_proof(
   assignment: Mapping[str, Any], proof: Mapping[str, Any]
) -> str:
   _require(
      proof.get("protocol") == LAYER_LOAD_PROOF_PROTOCOL,
      "unsupported_load_proof_protocol",
   )
   for field in (
      "deployment_id",
      "deployment_epoch",
      "assignment_id",
      "node_id",
      "model_id",
      "manifest_digest",
      "resolved_commit",
   ):
      _require(
         proof.get(field) == assignment.get(field),
         f"load_proof_{field}_mismatch",
      )
   for proof_field, assignment_field in (
      ("loaded_range", "range"),
      ("loaded_components", "components"),
      ("loaded_tensor_keys", "expected_tensor_keys"),
      ("runtime", "runtime"),
      ("control_plane_binding", "control_plane_binding"),
   ):
      _require(
         _canonical_equal(proof.get(proof_field), assignment.get(assignment_field)),
         f"load_proof_{proof_field}_mismatch",
      )
   _require(
      _sha256_ref(proof.get("loaded_tensor_digest")),
      "invalid_loaded_tensor_digest",
   )
   _require(_sha256_ref(proof.get("probe_digest")), "invalid_probe_digest")
   _require(
      isinstance(proof.get("load_generation"), int)
      and not isinstance(proof.get("load_generation"), bool)
      and proof["load_generation"] >= 0,
      "invalid_load_generation",
   )
   _require(
      isinstance(proof.get("probe_shape"), list)
      and bool(proof["probe_shape"])
      and all(
         isinstance(dimension, int)
         and not isinstance(dimension, bool)
         and dimension > 0
         for dimension in proof["probe_shape"]
      ),
      "invalid_probe_shape",
   )
   runtime_identity = proof.get("runtime_identity")
   _require(isinstance(runtime_identity, Mapping), "invalid_runtime_identity")
   _require(
      runtime_identity.get("backend") == assignment["runtime"].get("backend"),
      "runtime_identity_backend_mismatch",
   )
   _require(
      proof.get("route_ready") is False
      and isinstance(proof.get("claim_boundary"), str)
      and bool(proof["claim_boundary"].strip()),
      "load_proof_claim_boundary_violation",
   )
   _canonical_json(dict(proof))
   return layer_load_proof_digest(proof)


def _status_payloads(evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
   statuses: dict[str, dict[str, Any]] = {}
   for record in evidence["records"]:
      payload = record["payload"]
      if payload.get("protocol") != "mycelium.device_status.v1":
         continue
      node_id = payload.get("node_id")
      _require(isinstance(node_id, str) and node_id, "invalid_device_status_node")
      _require(node_id not in statuses, "duplicate_device_status", node_id)
      statuses[node_id] = payload
   return statuses


def _status_record_times(evidence: Mapping[str, Any]) -> dict[str, float]:
   result: dict[str, float] = {}
   for record in evidence["records"]:
      payload = record["payload"]
      if payload.get("protocol") == "mycelium.device_status.v1":
         result[payload["node_id"]] = float(record["generated_at_unix_ms"]) / 1000.0
   return result


def _performance(status: Mapping[str, Any], node_id: str) -> tuple[float, float]:
   performance = status.get("performance")
   _require(isinstance(performance, Mapping), "missing_device_performance", node_id)
   prefill = performance.get("prefill_ms_per_layer_token")
   decode = performance.get("decode_ms_per_layer_token")
   for name, value in (("prefill", prefill), ("decode", decode)):
      _require(
         isinstance(value, (int, float))
         and not isinstance(value, bool)
         and math.isfinite(float(value))
         and float(value) > 0,
         f"invalid_{name}_performance",
         node_id,
      )
   return float(prefill), float(decode)


def device_states_from_evidence_bundle(
   document: Mapping[str, Any],
) -> dict[str, DeviceState]:
   """Compile Router DeviceState values from one atomic evidence generation."""
   try:
      bundle = evidence_bundle_from_dict(dict(document))
      evidence = evidence_bundle_to_dict(bundle)
   except (TypeError, ValueError) as exc:
      raise LayerBuildError("invalid_evidence_bundle", str(exc)) from exc

   statuses = _status_payloads(evidence)
   updated_at = _status_record_times(evidence)
   nodes = evidence["router_view"]["nodes"]
   edges = evidence["router_view"]["edges"]
   states: dict[str, DeviceState] = {}
   for node in nodes:
      node_id = node["node_id"]
      _require(node_id in statuses, "missing_device_status", node_id)
      status = statuses[node_id]
      prefill_ms, _ = _performance(status, node_id)
      concurrency = status.get("concurrency_limit")
      in_flight = status.get("in_flight")
      _require(
         isinstance(concurrency, int)
         and not isinstance(concurrency, bool)
         and concurrency > 0,
         "invalid_concurrency_limit",
         node_id,
      )
      _require(
         isinstance(in_flight, int)
         and not isinstance(in_flight, bool)
         and 0 <= in_flight,
         "invalid_in_flight",
         node_id,
      )
      memory_domains = status.get("memory_domains")
      _require(isinstance(memory_domains, list), "invalid_memory_domains", node_id)
      available_kv = 0
      for memory in memory_domains:
         amount = memory.get("allocatable_after_reservations_bytes")
         _require(
            isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0,
            "invalid_allocatable_memory",
            node_id,
         )
         available_kv += amount

      neighbor_rtt: dict[str, float] = {}
      neighbor_bandwidth: dict[str, float] = {}
      for edge in edges:
         if edge["src_node_id"] != node_id or not edge["eligible"]:
            continue
         destination = edge["dst_node_id"]
         _require(destination not in neighbor_rtt, "ambiguous_router_link", f"{node_id}->{destination}")
         neighbor_rtt[destination] = float(edge["rtt_p95_ms"])
         neighbor_bandwidth[destination] = float(edge["goodput_mbps"]) * 1_000_000.0 / 8.0

      states[node_id] = DeviceState(
         node_id=node_id,
         state_seq=int(node["status_version"]["sequence"]),
         last_updated=updated_at[node_id],
         availability="ALIVE" if node["eligible"] and node["peer_state"] == "alive" else "DEAD",
         compute_units_per_second=1000.0 / prefill_ms,
         free_compute_fraction=max(0.0, min(1.0, (concurrency - in_flight) / concurrency)),
         available_kv_bytes=available_kv,
         pending_hop_queue_depth=int(status["queue_depth"]),
         neighbor_rtt_ms=neighbor_rtt,
         neighbor_bandwidth_bytes_per_second=neighbor_bandwidth,
      )
   return states


def _physical_link_id(
   evidence: Mapping[str, Any], source_node: str, destination_node: str
) -> str:
   if source_node == destination_node:
      return f"local:{source_node}"
   matches = [
      edge
      for edge in evidence["router_view"]["edges"]
      if edge["src_node_id"] == source_node
      and edge["dst_node_id"] == destination_node
      and edge["eligible"]
   ]
   _require(matches, "missing_eligible_router_link", f"{source_node}->{destination_node}")
   _require(len(matches) == 1, "ambiguous_router_link", f"{source_node}->{destination_node}")
   edge = matches[0]
   payload_hash = edge["version"]["payload_hash"]
   _require(_sha256_ref(payload_hash), "invalid_router_link_digest")
   return (
      f"link:{source_node}/{edge['src_endpoint_id']}->"
      f"{destination_node}/{edge['dst_endpoint_id']}@{payload_hash}"
   )


def _stage_signature(
   assignment: Mapping[str, Any], stage_id: str, model: Mapping[str, Any]
) -> str:
   material = {
      "protocol": "mycelium.stage_signature.v1",
      "deployment_id": assignment["deployment_id"],
      "deployment_epoch": assignment["deployment_epoch"],
      "model_id": assignment["model_id"],
      "resolved_commit": assignment["resolved_commit"],
      "manifest_digest": assignment["manifest_digest"],
      "stage_id": stage_id,
      "range": assignment["range"],
      "components": assignment["components"],
      "hidden_size": model["hidden_size"],
      "dtype_bytes": model["dtype_bytes"],
   }
   return _SHA256_PREFIX + hashlib.sha256(_canonical_json(material)).hexdigest()


def _edge(
   *,
   kind: str,
   source_placement: Mapping[str, Any],
   destination_placement: Mapping[str, Any],
   evidence: Mapping[str, Any],
) -> PlacementEdge:
   source_id = source_placement["placement_id"]
   destination_id = destination_placement["placement_id"]
   return PlacementEdge(
      edge_id=f"{kind}:{source_id}->{destination_id}",
      from_placement_id=source_id,
      to_placement_id=destination_id,
      link_id=_physical_link_id(
         evidence,
         source_placement["node_id"],
         destination_placement["node_id"],
      ),
   )


def build_execution_graph(
   tranche: Mapping[str, Any],
   load_proofs: Sequence[Mapping[str, Any]],
   *,
   manifest: dict[str, Any],
   runtime_endpoints: Mapping[str, str],
   topology_version: int,
   token_envelope_bytes: int,
) -> ExecutionGraph:
   """Build a deterministic, unqualified candidate Router execution graph."""
   _require(
      isinstance(topology_version, int)
      and not isinstance(topology_version, bool)
      and topology_version >= 0,
      "invalid_topology_version",
   )
   _require(
      isinstance(token_envelope_bytes, int)
      and not isinstance(token_envelope_bytes, bool)
      and token_envelope_bytes >= 0,
      "invalid_token_envelope_bytes",
   )
   _require(isinstance(tranche, Mapping), "invalid_control_plane_tranche")
   try:
      validate_control_plane_tranche(dict(tranche), manifest=manifest)
      evidence_bundle = evidence_bundle_from_dict(dict(tranche["evidence_bundle"]))
      evidence = evidence_bundle_to_dict(evidence_bundle)
   except (KeyError, TypeError, ValueError) as exc:
      raise LayerBuildError("invalid_control_plane_tranche", str(exc)) from exc

   assignments = list(tranche["assignments"])
   assignments_by_id = {assignment["assignment_id"]: assignment for assignment in assignments}
   _require(len(assignments_by_id) == len(assignments), "duplicate_assignment_id")
   _require(
      isinstance(load_proofs, Sequence)
      and not isinstance(load_proofs, (str, bytes, bytearray)),
      "invalid_load_proofs",
   )
   proofs_by_assignment: dict[str, Mapping[str, Any]] = {}
   for proof in load_proofs:
      _require(isinstance(proof, Mapping), "invalid_load_proof")
      assignment_id = proof.get("assignment_id")
      _require(
         assignment_id in assignments_by_id,
         "unknown_load_proof_assignment",
         str(assignment_id),
      )
      _require(
         assignment_id not in proofs_by_assignment,
         "duplicate_load_proof",
         str(assignment_id),
      )
      proofs_by_assignment[assignment_id] = proof
   missing = sorted(set(assignments_by_id) - set(proofs_by_assignment))
   _require(not missing, "missing_load_proof", ",".join(missing))

   _require(isinstance(runtime_endpoints, Mapping), "invalid_runtime_endpoints")
   endpoint_keys = set(runtime_endpoints)
   assignment_keys = set(assignments_by_id)
   _require(not (assignment_keys - endpoint_keys), "missing_runtime_endpoint")
   _require(not (endpoint_keys - assignment_keys), "unknown_runtime_endpoint")

   route = tranche["route_plan"]
   placement_by_id = {item["placement_id"]: item for item in route["placements"]}
   track = route["legal_tracks"][0]
   track_placements = [placement_by_id[item] for item in track["placement_ids"]]
   assignment_by_placement: dict[str, Mapping[str, Any]] = {}
   for assignment in assignments:
      matches = [
         placement
         for placement in track_placements
         if placement["node_id"] == assignment["node_id"]
         and placement["layer_range"]["start"] == assignment["range"]["start_layer"]
         and placement["layer_range"]["end"]
         == assignment["range"]["end_layer_exclusive"]
      ]
      _require(
         len(matches) == 1,
         "assignment_route_placement_mismatch",
         assignment["assignment_id"],
      )
      placement_id = matches[0]["placement_id"]
      _require(
         placement_id not in assignment_by_placement,
         "duplicate_assignment_route_placement",
         placement_id,
      )
      assignment_by_placement[placement_id] = assignment
   model = tranche["planner_snapshot"]["model"]
   statuses = _status_payloads(evidence)
   stages: list[Stage] = []
   router_placement_by_id: dict[str, dict[str, str]] = {}

   for route_placement in track_placements:
      placement_id = route_placement["placement_id"]
      assignment = assignment_by_placement[placement_id]
      proof = proofs_by_assignment[assignment["assignment_id"]]
      proof_digest = _validate_load_proof(assignment, proof)
      endpoint = _validate_endpoint(
         runtime_endpoints[assignment["assignment_id"]], assignment["assignment_id"]
      )
      stage_id = route_placement["replica_group_id"]
      layer_range = assignment["range"]
      layer_count = layer_range["layer_count"]
      prefill_ms, decode_ms = _performance(statuses[assignment["node_id"]], assignment["node_id"])
      kv_bytes = (
         2
         * layer_count
         * int(model["kv_heads"])
         * int(model["head_dim"])
         * int(model["dtype_bytes"])
      )
      placement = Placement(
         placement_id=placement_id,
         node_id=assignment["node_id"],
         replica_group_id=route_placement["replica_group_id"],
         assignment_id=assignment["assignment_id"],
         stage_signature=_stage_signature(assignment, stage_id, model),
         load_proof_digest=proof_digest,
         runtime_backend=proof["runtime_identity"]["backend"],
         runtime_endpoint=endpoint,
         lifecycle_state="ACTIVE",
      )
      stages.append(
         Stage(
            stage_id=stage_id,
            layer_range=LayerRange(
               start_layer=layer_range["start_layer"],
               end_layer_exclusive=layer_range["end_layer_exclusive"],
               layer_count=layer_count,
            ),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
               prefill_work_units_per_prompt_token=float(layer_count),
               decode_work_units_per_token=float(layer_count) * decode_ms / prefill_ms,
               kv_bytes_per_context_token=kv_bytes,
            ),
            placements=(placement,),
         )
      )
      router_placement_by_id[placement_id] = {
         "placement_id": placement_id,
         "node_id": assignment["node_id"],
      }

   expected_forward_pairs = {
      (left["placement_id"], right["placement_id"])
      for left, right in zip(track_placements, track_placements[1:])
   }
   route_forward_pairs = {
      (item["src_placement_id"], item["dst_placement_id"])
      for item in route["forward_edges"]
   }
   _require(
      expected_forward_pairs <= route_forward_pairs,
      "missing_route_forward_edge",
   )
   edges = tuple(
      _edge(
         kind="forward",
         source_placement=router_placement_by_id[source],
         destination_placement=router_placement_by_id[destination],
         evidence=evidence,
      )
      for source, destination in sorted(expected_forward_pairs)
   )

   loopback_pair = (
      track_placements[-1]["placement_id"],
      track_placements[0]["placement_id"],
   )
   route_loopback_pairs = {
      (item["src_placement_id"], item["dst_placement_id"])
      for item in route["loopbacks"]
   }
   _require(loopback_pair in route_loopback_pairs, "missing_route_loopback")
   loopbacks = (
      _edge(
         kind="loopback",
         source_placement=router_placement_by_id[loopback_pair[0]],
         destination_placement=router_placement_by_id[loopback_pair[1]],
         evidence=evidence,
      ),
   )

   first_assignment = assignment_by_placement[track_placements[0]["placement_id"]]
   graph = ExecutionGraph(
      deployment_id=first_assignment["deployment_id"],
      deployment_epoch=first_assignment["deployment_epoch"],
      topology_version=topology_version,
      model_id=first_assignment["model_id"],
      resolved_commit=first_assignment["resolved_commit"],
      manifest_digest=first_assignment["manifest_digest"],
      entry_stage_id=stages[0].stage_id,
      final_stage_id=stages[-1].stage_id,
      hidden_size=int(model["hidden_size"]),
      activation_bytes=int(model["dtype_bytes"]),
      token_envelope_bytes=token_envelope_bytes,
      stages=tuple(stages),
      edges=edges,
      loopback_edges=loopbacks,
   )
   return validate_execution_graph(graph)
