#!/usr/bin/env python3
"""Versioned route contracts for Mycelium runtime-facing layer ranges."""
from __future__ import annotations

import copy
from typing import Any


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


def validate_route_plan_v2(plan: dict[str, Any]) -> None:
   if plan.get("protocol") != "mycelium.route_plan.v2":
      raise ValueError("expected mycelium.route_plan.v2")
   if plan.get("ok") is not True:
      raise ValueError("route plan is not successful")
   model = plan.get("model")
   if not isinstance(model, dict):
      raise ValueError("route plan model must be an object")
   num_layers = model.get("num_layers")
   if not isinstance(num_layers, int) or isinstance(num_layers, bool) or num_layers <= 0:
      raise ValueError("model num_layers must be a positive integer")
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


def upgrade_route_plan_v1(plan: dict[str, Any]) -> dict[str, Any]:
   """Convert legacy inclusive display ranges to explicit half-open ranges once."""
   if plan.get("protocol") != "mycelium.route_plan.v1":
      raise ValueError("expected mycelium.route_plan.v1")
   upgraded = copy.deepcopy(plan)
   upgraded["protocol"] = "mycelium.route_plan.v2"
   upgraded["source_protocol"] = "mycelium.route_plan.v1"
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
   validate_route_plan_v2(upgraded)
   return upgraded
