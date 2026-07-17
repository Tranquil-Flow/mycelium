#!/usr/bin/env python3
"""Compile route stages into immutable per-node layer assignments."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import PurePosixPath
from typing import Any

import model_manifest as mm
from route_contract import upgrade_legacy_route_plan_v1, validate_layer_range, validate_manual_provisioning_route_v1
from runtime_contracts import MLX_RUNTIME_BASE_FIELDS, validate_normalized_mlx_runtime


def _canonical(document: Any) -> str:
   return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


LAYER_ASSIGNMENT_PROTOCOL = "mycelium.layer_assignment.v2"

_ASSIGNMENT_ID_FIELDS = (
   "protocol",
   "deployment_id",
   "deployment_epoch",
   "node_id",
   "manifest_digest",
   "model_id",
   "resolved_commit",
   "range",
   "components",
   "component_tensor_keys",
   "component_aliases",
   "expected_tensor_prefixes",
   "expected_tensor_keys",
   "files",
   "artifact_cache_root",
   "runtime",
)


def assignment_id_for(assignment: dict[str, Any]) -> str:
   """Derive deterministic ID binding every runtime-relevant assignment field."""
   try:
      identity = {field: assignment[field] for field in _ASSIGNMENT_ID_FIELDS}
   except KeyError as exc:
      raise ValueError(f"assignment identity missing field: {exc.args[0]}") from exc
   control_plane_binding = assignment.get("control_plane_binding")
   if control_plane_binding is not None:
      identity["control_plane_binding"] = control_plane_binding
   try:
      namespace = uuid.UUID(str(identity["deployment_id"]))
   except (TypeError, ValueError) as exc:
      raise ValueError("assignment deployment_id must be a UUID") from exc
   if identity["deployment_id"] != str(namespace):
      raise ValueError("assignment deployment_id must be canonical")
   return str(uuid.uuid5(namespace, _canonical(identity)))


def validate_assignment_identity(assignment: dict[str, Any]) -> None:
   expected = assignment_id_for(assignment)
   actual = assignment.get("assignment_id")
   try:
      canonical = str(uuid.UUID(str(actual)))
   except (TypeError, ValueError) as exc:
      raise ValueError("assignment_id must be a canonical UUID") from exc
   if actual != canonical or actual != expected:
      raise ValueError("assignment_id does not bind assignment semantic identity")
   if assignment.get("route_ready") is not False:
      raise ValueError("assignment route_ready must be false until runtime activation")


def _normalize_runtime(
   runtime: Any, node_id: str, manifest: dict[str, Any]
) -> dict[str, Any]:
   if not isinstance(runtime, dict):
      raise ValueError(f"runtime identity missing for {node_id}")
   if set(runtime) != MLX_RUNTIME_BASE_FIELDS:
      raise ValueError(
         f"runtime fields for {node_id} must be backend, dtype, and quantization"
      )
   for field in ("backend", "dtype", "quantization"):
      if not isinstance(runtime.get(field), str) or not runtime[field]:
         raise ValueError(f"runtime {field} missing for {node_id}")
   normalized = copy.deepcopy(runtime)
   if runtime["backend"] == "mlx":
      runtime_model = manifest.get("runtime_model")
      if not isinstance(runtime_model, dict):
         raise ValueError(
            f"manifest lacks normalized MLX runtime model for {node_id}"
         )
      normalized.update(copy.deepcopy(runtime_model))
      normalized = validate_normalized_mlx_runtime(normalized)
   return normalized


def validate_target_cache_root(value: str, node_id: str = "peer") -> str:
   """Validate a target-owned POSIX path without resolving it on the coordinator."""
   if not isinstance(value, str) or not value or "\x00" in value:
      raise ValueError(f"artifact cache root for {node_id} must be an absolute POSIX path")
   path = PurePosixPath(value)
   if not path.is_absolute():
      raise ValueError(f"artifact cache root for {node_id} must be absolute")
   if str(path) != value or ".." in path.parts:
      raise ValueError(f"artifact cache root for {node_id} must be canonical")
   return value


def compile_layer_assignments(
   *,
   route_plan: dict[str, Any],
   manifest: dict[str, Any],
   deployment_id: str,
   deployment_epoch: int,
   cache_roots: dict[str, str],
   runtime_by_node: dict[str, dict[str, Any]],
   control_plane_binding: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
   if route_plan.get("protocol") == "mycelium.route_plan.v1":
      route_plan = upgrade_legacy_route_plan_v1(route_plan)
   validate_manual_provisioning_route_v1(route_plan)
   if not mm.verify_manifest_digest(manifest):
      raise ValueError("manifest digest mismatch")
   route_model = route_plan.get("model", {})
   manifest_identity = {
      "model_id": manifest.get("model_id"),
      "num_layers": manifest.get("num_layers"),
      "manifest_digest": mm.manifest_digest_ref(manifest),
      "resolved_commit": manifest.get("resolved_commit"),
   }
   for field, expected in manifest_identity.items():
      if route_model.get(field) != expected:
         raise ValueError(f"route model {field} does not match manifest")
   try:
      namespace = uuid.UUID(deployment_id)
   except (TypeError, ValueError) as exc:
      raise ValueError("deployment_id must be a UUID") from exc
   if not isinstance(deployment_epoch, int) or isinstance(deployment_epoch, bool) or deployment_epoch < 0:
      raise ValueError("deployment_epoch must be a non-negative integer")
   if control_plane_binding is not None and not isinstance(control_plane_binding, dict):
      raise ValueError("control_plane_binding must be an object or null")

   file_records = {item["path"]: item for item in manifest["files"]}
   assignments = []
   for stage in route_plan["route"]:
      node_id = stage["node_id"]
      layer_range = dict(stage["range"])
      validate_layer_range(layer_range, num_layers=manifest["num_layers"])
      cache_root_raw = cache_roots.get(node_id)
      cache_root = validate_target_cache_root(cache_root_raw, node_id)
      runtime = _normalize_runtime(runtime_by_node.get(node_id), node_id, manifest)

      layers = range(layer_range["start_layer"], layer_range["end_layer_exclusive"])
      static_tensor_keys = manifest.get("component_tensor_keys", {})
      components = ["decoder"]
      if layer_range["start_layer"] == 0 and static_tensor_keys.get("input_embedding"):
         components.insert(0, "input_embedding")
      if layer_range["end_layer_exclusive"] == manifest["num_layers"]:
         components.extend(
            component
            for component, tensor_keys in static_tensor_keys.items()
            if component != "input_embedding" and tensor_keys
         )
      tensor_prefixes = [manifest["block_prefix_template"].format(layer=layer) for layer in layers]
      decoder_tensor_keys = sorted({
         key
         for layer in layers
         for key in manifest["tensor_keys_by_layer"][str(layer)]
      })
      component_tensor_keys = {
         component: (
            decoder_tensor_keys
            if component == "decoder"
            else list(static_tensor_keys[component])
         )
         for component in components
      }
      component_aliases = {
         component: target
         for component, target in manifest.get("component_aliases", {}).items()
         if component in components
      }
      tensor_keys = sorted({
         key
         for keys in component_tensor_keys.values()
         for key in keys
      })
      covering_paths = {
         path
         for layer in layers
         for path in manifest["layer_files"][str(layer)]
      }
      for component in components:
         if component != "decoder":
            covering_paths.update(manifest["component_files"][component])
      covering_paths = sorted(covering_paths)
      files = []
      for path in covering_paths:
         record = file_records[path]
         files.append({
            "path": path,
            "size_bytes": record["size_bytes"],
            "content_digest": f"{record['content_digest']['algorithm']}:{record['content_digest']['value']}",
         })

      semantic_identity = {
         "protocol": LAYER_ASSIGNMENT_PROTOCOL,
         "deployment_id": str(namespace),
         "deployment_epoch": deployment_epoch,
         "node_id": node_id,
         "manifest_digest": mm.manifest_digest_ref(manifest),
         "model_id": manifest["model_id"],
         "resolved_commit": manifest["resolved_commit"],
         "range": layer_range,
         "components": components,
         "component_tensor_keys": component_tensor_keys,
         "component_aliases": component_aliases,
         "expected_tensor_prefixes": tensor_prefixes,
         "expected_tensor_keys": tensor_keys,
         "files": files,
         "artifact_cache_root": cache_root,
         "runtime": dict(runtime),
      }
      if control_plane_binding is not None:
         semantic_identity["control_plane_binding"] = copy.deepcopy(control_plane_binding)
      assignment_id = assignment_id_for(semantic_identity)
      assignment = {
         "assignment_id": assignment_id,
         **semantic_identity,
         "route_ready": False,
         "claim_boundary": "minimal upstream shards assigned; runtime has not loaded or probed layers",
      }
      assignments.append(assignment)
   return assignments
