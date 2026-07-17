#!/usr/bin/env python3
"""Compatibility contract for ordered manual provisioning routes.

The product Planner owns ``mycelium.route_plan.v2``. This module accepts only the
legacy compact ``mycelium.route_plan.v1`` input and the distinct manual
provisioning wire contract ``mycelium.manual_provisioning_route.v1``.
"""
from __future__ import annotations

import copy
import re
from typing import Any


LEGACY_ROUTE_PLAN_PROTOCOL = "mycelium.route_plan.v1"
MANUAL_PROVISIONING_ROUTE_PROTOCOL = "mycelium.manual_provisioning_route.v1"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def validate_layer_range(layer_range: dict[str, Any], *, num_layers: int) -> None:
   start = layer_range.get("start_layer")
   end = layer_range.get("end_layer_exclusive")
   count = layer_range.get("layer_count")
   if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end, count)):
      raise ValueError("layer range fields must be integers")
   if start < 0:
      raise ValueError("start_layer must be non-negative")
   if end <= start:
      raise ValueError("end_layer_exclusive must be greater than start_layer")
   if end > num_layers:
      raise ValueError("end_layer_exclusive exceeds num_layers")
   if end - start != count:
      raise ValueError("layer_count does not match half-open range")


def validate_manual_provisioning_route_v1(plan: dict[str, Any]) -> None:
   if not isinstance(plan, dict) or plan.get("protocol") != MANUAL_PROVISIONING_ROUTE_PROTOCOL:
      raise ValueError(f"expected {MANUAL_PROVISIONING_ROUTE_PROTOCOL}")
   if plan.get("ok") is not True:
      raise ValueError("route plan is not successful")
   model = plan.get("model")
   if not isinstance(model, dict):
      raise ValueError("model must be an object")
   if not isinstance(model.get("model_id"), str) or not model["model_id"]:
      raise ValueError("model_id required")
   num_layers = model.get("num_layers")
   if isinstance(num_layers, bool) or not isinstance(num_layers, int) or num_layers <= 0:
      raise ValueError("num_layers must be positive")
   if not _SHA256_REF_RE.fullmatch(str(model.get("manifest_digest", ""))):
      raise ValueError("manifest_digest must be sha256:<64 lowercase hex>")
   if not _COMMIT_RE.fullmatch(str(model.get("resolved_commit", ""))):
      raise ValueError("resolved_commit must be immutable 40-hex")
   claim_boundary = plan.get("claim_boundary")
   if not isinstance(claim_boundary, str) or not claim_boundary.strip():
      raise ValueError("claim_boundary must be a non-empty string")
   route = plan.get("route")
   if not isinstance(route, list) or not route:
      raise ValueError("route must contain at least one stage")

   expected_start = 0
   seen_nodes: set[str] = set()
   for stage in route:
      node_id = stage.get("node_id")
      if not isinstance(node_id, str) or not node_id:
         raise ValueError("every route stage requires node_id")
      if node_id in seen_nodes:
         raise ValueError(f"duplicate primary route node: {node_id}")
      seen_nodes.add(node_id)
      layer_range = stage.get("range")
      if not isinstance(layer_range, dict):
         raise ValueError(f"route stage {node_id} requires range")
      validate_layer_range(layer_range, num_layers=num_layers)
      start = layer_range["start_layer"]
      if start > expected_start:
         raise ValueError(f"route gap before layer {start}")
      if start < expected_start:
         raise ValueError(f"route overlap at layer {start}")
      expected_start = layer_range["end_layer_exclusive"]

   if expected_start < num_layers:
      raise ValueError(f"route gap after layer {expected_start - 1}")
   if expected_start > num_layers:
      raise ValueError("route exceeds model layer count")

   node_order = plan.get("node_order")
   if node_order is not None and node_order != [stage["node_id"] for stage in route]:
      raise ValueError("node_order does not match route")


def upgrade_legacy_route_plan_v1(plan_v1: dict[str, Any]) -> dict[str, Any]:
   """Convert the legacy inclusive compact route exactly once."""
   if not isinstance(plan_v1, dict) or plan_v1.get("protocol") != LEGACY_ROUTE_PLAN_PROTOCOL:
      raise ValueError(f"expected {LEGACY_ROUTE_PLAN_PROTOCOL}")
   upgraded = copy.deepcopy(plan_v1)
   upgraded["protocol"] = MANUAL_PROVISIONING_ROUTE_PROTOCOL
   upgraded["source_protocol"] = LEGACY_ROUTE_PLAN_PROTOCOL
   upgraded.setdefault(
      "claim_boundary",
      "legacy route conversion for manual provisioning only; not product Planner output",
   )
   converted = []
   for stage in upgraded.get("route") or []:
      legacy = stage.pop("layers", None)
      if not isinstance(legacy, list) or len(legacy) != 2:
         raise ValueError("v1 stage requires inclusive layers pair")
      start, end_inclusive = legacy
      if not isinstance(start, int) or not isinstance(end_inclusive, int):
         raise ValueError("v1 layers must be integers")
      stage["range"] = {
         "start_layer": start,
         "end_layer_exclusive": end_inclusive + 1,
         "layer_count": stage.get("layer_count"),
      }
      converted.append(stage)
   upgraded["route"] = converted
   validate_manual_provisioning_route_v1(upgraded)
   return upgraded
