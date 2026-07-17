#!/usr/bin/env python3
"""Deterministic simulation harness for Mycelium route-planner strategies.

The simulator deliberately does not execute a real model. It estimates prefill,
decode, VRAM/RAM residency, and directed network transfer costs for hypothetical
clusters so planner algorithms can be compared before changing the runtime.

No disk/NVMe tier is modeled. On discrete-memory devices, weights that do not
fit in VRAM may either stream from RAM to the GPU or execute on the CPU; the
faster estimated path is used. Unified-memory devices have no copy penalty.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


EXACT_RING_MAX_NODES = 12
EXACT_SINGLE_REQUEST_RING_MAX_NODES = 7


@dataclass(frozen=True)
class NodeSpec:
   node_id: str
   gpu_tflops: float
   cpu_tflops: float
   vram_available_gb: float
   ram_available_gb: float
   gpu_memory_bandwidth_gbps: float
   ram_bandwidth_gbps: float
   vram_ram_bandwidth_gbps: float
   unified_memory: bool = False
   gpu_efficiency: float = 0.70
   cpu_efficiency: float = 0.55
   workspace_gb: float = 0.25


@dataclass(frozen=True)
class LinkSpec:
   src: str
   dst: str
   rtt_ms: float
   jitter_ms: float
   bandwidth_mbps: float
   loss_ratio: float = 0.0

   def transfer_ms(self, payload_bytes: float) -> float:
      if self.bandwidth_mbps <= 0 or self.loss_ratio >= 1.0:
         return math.inf
      serialization_ms = float(payload_bytes) * 8.0 / (self.bandwidth_mbps * 1_000_000.0) * 1000.0
      loss_penalty = self.rtt_ms * self.loss_ratio / max(1e-6, 1.0 - self.loss_ratio)
      return self.rtt_ms / 2.0 + self.jitter_ms + serialization_ms + loss_penalty


@dataclass(frozen=True)
class ModelSpec:
   model_id: str
   num_layers: int
   hidden_size: int
   layer_weight_gb: float
   decode_gflops_per_layer: float
   prefill_gflops_per_layer_per_token: float
   activation_bytes: int = 2
   kv_heads: int = 8
   head_dim: int = 128
   kv_bytes: int = 2
   token_envelope_bytes: int = 128

   def activation_bytes_decode(self) -> int:
      return int(self.hidden_size * self.activation_bytes)

   def activation_bytes_prefill(self, prompt_tokens: int) -> int:
      return int(prompt_tokens * self.hidden_size * self.activation_bytes)

   def kv_gb_per_token_per_layer(self) -> float:
      raw_bytes = 2 * self.kv_heads * self.head_dim * self.kv_bytes
      return raw_bytes / (1024**3)


@dataclass(frozen=True)
class WorkloadSpec:
   context_window: int
   context_fraction_per_request: float = 0.25
   kv_safety_multiplier: float = 1.25
   concurrent_requests: int = 1
   output_tokens: int = 128

   def prompt_tokens(self) -> int:
      return max(1, min(self.context_window, int(round(self.context_window * self.context_fraction_per_request))))

   def planned_kv_tokens(self) -> int:
      return max(1, min(self.context_window, int(math.ceil(self.prompt_tokens() * self.kv_safety_multiplier))))


@dataclass
class StageEstimate:
   layer_count: int
   resident_layer_count: int
   ram_layer_count: int
   ram_execution: str | None
   decode_compute_ms: float
   prefill_compute_ms: float
   vram_used_gb: float
   ram_used_gb: float
   weights_gb: float
   kv_cache_gb: float
   kv_cache_in_ram_gb: float


@dataclass
class Scenario:
   name: str
   model: ModelSpec
   workload: WorkloadSpec
   nodes: dict[str, NodeSpec]
   links: dict[tuple[str, str], LinkSpec]


def _stable_id(prefix: str, value: Any) -> str:
   raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
   return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:16]}"


def load_scenario(path: str | Path) -> Scenario:
   data = json.loads(Path(path).read_text())
   model = ModelSpec(**data["model"])
   workload = WorkloadSpec(**data["workload"])
   nodes = {item["node_id"]: NodeSpec(**item) for item in data["nodes"]}
   links: dict[tuple[str, str], LinkSpec] = {}
   for item in data["links"]:
      link = LinkSpec(
         src=item["src"],
         dst=item["dst"],
         rtt_ms=float(item["rtt_ms"]),
         jitter_ms=float(item["jitter_ms"]),
         bandwidth_mbps=float(item["bandwidth_mbps"]),
         loss_ratio=float(item.get("loss_ratio", 0.0)),
      )
      links[(link.src, link.dst)] = link
      if item.get("bidirectional", False):
         reverse = LinkSpec(
            src=link.dst,
            dst=link.src,
            rtt_ms=float(item.get("reverse_rtt_ms", link.rtt_ms)),
            jitter_ms=float(item.get("reverse_jitter_ms", link.jitter_ms)),
            bandwidth_mbps=float(item.get("reverse_bandwidth_mbps", link.bandwidth_mbps)),
            loss_ratio=float(item.get("reverse_loss_ratio", link.loss_ratio)),
         )
         links[(reverse.src, reverse.dst)] = reverse
   return Scenario(name=data["name"], model=model, workload=workload, nodes=nodes, links=links)


def estimate_stage(node: NodeSpec, model: ModelSpec, workload: WorkloadSpec, layer_count: int) -> StageEstimate | None:
   """Estimate a stage without a disk tier.

   Discrete devices reserve fast memory for workspace and KV first. Whole layer
   weights that remain are resident in VRAM; the rest live in RAM. RAM layers
   use the faster of CPU execution and GPU execution after a RAM->VRAM stream.
   """
   if layer_count <= 0:
      return None
   prompt_tokens = workload.prompt_tokens()
   planned_kv_tokens = workload.planned_kv_tokens()
   weights_gb = layer_count * model.layer_weight_gb
   kv_gb = (
      layer_count
      * model.kv_gb_per_token_per_layer()
      * planned_kv_tokens
      * workload.concurrent_requests
   )

   gpu_tflops = max(1e-9, node.gpu_tflops * node.gpu_efficiency)
   cpu_tflops = max(1e-9, node.cpu_tflops * node.cpu_efficiency)
   gpu_bw = max(1e-9, node.gpu_memory_bandwidth_gbps * node.gpu_efficiency)
   ram_bw = max(1e-9, node.ram_bandwidth_gbps * node.cpu_efficiency)

   gpu_decode_layer_ms = max(
      model.decode_gflops_per_layer / gpu_tflops,
      model.layer_weight_gb / gpu_bw * 1000.0,
   )
   gpu_prefill_layer_ms = max(
      model.prefill_gflops_per_layer_per_token * prompt_tokens / gpu_tflops,
      model.layer_weight_gb / gpu_bw * 1000.0,
   )

   if node.unified_memory:
      total_used = weights_gb + kv_gb + node.workspace_gb
      if total_used > node.ram_available_gb + 1e-9:
         return None
      return StageEstimate(
         layer_count=layer_count,
         resident_layer_count=layer_count,
         ram_layer_count=0,
         ram_execution=None,
         decode_compute_ms=layer_count * gpu_decode_layer_ms,
         prefill_compute_ms=layer_count * gpu_prefill_layer_ms,
         vram_used_gb=0.0,
         ram_used_gb=total_used,
         weights_gb=weights_gb,
         kv_cache_gb=kv_gb,
         kv_cache_in_ram_gb=0.0,
      )

   fast_after_workspace = max(0.0, node.vram_available_gb - node.workspace_gb)
   kv_fast_gb = min(kv_gb, fast_after_workspace)
   kv_ram_gb = max(0.0, kv_gb - kv_fast_gb)
   fast_for_weights_gb = max(0.0, fast_after_workspace - kv_fast_gb)
   resident_layers = min(layer_count, int((fast_for_weights_gb + 1e-12) // model.layer_weight_gb))
   ram_layers = layer_count - resident_layers
   ram_weight_gb = ram_layers * model.layer_weight_gb
   ram_used_gb = ram_weight_gb + kv_ram_gb
   if ram_used_gb > node.ram_available_gb + 1e-9:
      return None

   cpu_decode_layer_ms = max(
      model.decode_gflops_per_layer / cpu_tflops,
      model.layer_weight_gb / ram_bw * 1000.0,
   )
   cpu_prefill_layer_ms = max(
      model.prefill_gflops_per_layer_per_token * prompt_tokens / cpu_tflops,
      model.layer_weight_gb / ram_bw * 1000.0,
   )
   if node.vram_ram_bandwidth_gbps > 0:
      stream_ms = model.layer_weight_gb / node.vram_ram_bandwidth_gbps * 1000.0
      streamed_decode_layer_ms = gpu_decode_layer_ms + stream_ms
      streamed_prefill_layer_ms = gpu_prefill_layer_ms + stream_ms
   else:
      streamed_decode_layer_ms = math.inf
      streamed_prefill_layer_ms = math.inf

   cpu_blended = cpu_decode_layer_ms + cpu_prefill_layer_ms / prompt_tokens
   stream_blended = streamed_decode_layer_ms + streamed_prefill_layer_ms / prompt_tokens
   use_cpu = cpu_blended <= stream_blended
   ram_decode_layer_ms = cpu_decode_layer_ms if use_cpu else streamed_decode_layer_ms
   ram_prefill_layer_ms = cpu_prefill_layer_ms if use_cpu else streamed_prefill_layer_ms
   ram_execution = "cpu" if use_cpu else "gpu_stream"

   # KV in host RAM must be read during decode and written during prefill.
   kv_decode_penalty = 0.0
   kv_prefill_penalty = 0.0
   if kv_ram_gb > 0:
      if node.vram_ram_bandwidth_gbps <= 0:
         return None
      kv_decode_penalty = 2.0 * kv_ram_gb / node.vram_ram_bandwidth_gbps * 1000.0
      kv_prefill_penalty = kv_ram_gb / node.vram_ram_bandwidth_gbps * 1000.0

   return StageEstimate(
      layer_count=layer_count,
      resident_layer_count=resident_layers,
      ram_layer_count=ram_layers,
      ram_execution=ram_execution if ram_layers else None,
      decode_compute_ms=resident_layers * gpu_decode_layer_ms + ram_layers * ram_decode_layer_ms + kv_decode_penalty,
      prefill_compute_ms=resident_layers * gpu_prefill_layer_ms + ram_layers * ram_prefill_layer_ms + kv_prefill_penalty,
      vram_used_gb=node.workspace_gb + kv_fast_gb + resident_layers * model.layer_weight_gb,
      ram_used_gb=ram_used_gb,
      weights_gb=weights_gb,
      kv_cache_gb=kv_gb,
      kv_cache_in_ram_gb=kv_ram_gb,
   )


def _link(scenario: Scenario, src: str, dst: str) -> LinkSpec | None:
   return scenario.links.get((src, dst))


def _regular_edge_cost(scenario: Scenario, src: str, dst: str) -> float:
   link = _link(scenario, src, dst)
   if link is None:
      return math.inf
   prompt = scenario.workload.prompt_tokens()
   output = scenario.workload.output_tokens
   # Edge weight is network time for one expected request: one prefill plus
   # every autoregressive decode transfer in the expected generated response.
   return (
      link.transfer_ms(scenario.model.activation_bytes_prefill(prompt))
      + output * link.transfer_ms(scenario.model.activation_bytes_decode())
   )


def _closure_edge_cost(scenario: Scenario, src: str, dst: str) -> float:
   link = _link(scenario, src, dst)
   if link is None:
      return math.inf
   return scenario.workload.output_tokens * link.transfer_ms(scenario.model.token_envelope_bytes)


def ring_network_cost(scenario: Scenario, order: Iterable[str]) -> float:
   order = tuple(order)
   if len(order) <= 1:
      return 0.0
   total = sum(_regular_edge_cost(scenario, a, b) for a, b in zip(order, order[1:]))
   return total + _closure_edge_cost(scenario, order[-1], order[0])


def _exact_shortest_ring(scenario: Scenario, node_ids: tuple[str, ...]) -> tuple[str, ...]:
   """Held-Karp directed ring search with a special, small closure payload."""
   if len(node_ids) <= 1:
      return node_ids
   best_cost = math.inf
   best_path: tuple[str, ...] | None = None
   n = len(node_ids)
   for start_idx in range(n):
      start_bit = 1 << start_idx
      dp: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {
         (start_bit, start_idx): (0.0, (start_idx,))
      }
      for mask in range(1 << n):
         if not (mask & start_bit):
            continue
         for last in range(n):
            state = dp.get((mask, last))
            if state is None:
               continue
            cost, path = state
            for nxt in range(n):
               bit = 1 << nxt
               if mask & bit:
                  continue
               edge = _regular_edge_cost(scenario, node_ids[last], node_ids[nxt])
               new_cost = cost + edge
               key = (mask | bit, nxt)
               if key not in dp or new_cost < dp[key][0]:
                  dp[key] = (new_cost, path + (nxt,))
      full = (1 << n) - 1
      for last in range(n):
         state = dp.get((full, last))
         if state is None:
            continue
         cost, path = state
         cost += _closure_edge_cost(scenario, node_ids[last], node_ids[start_idx])
         if cost < best_cost:
            best_cost = cost
            best_path = tuple(node_ids[idx] for idx in path)
   if best_path is None or not math.isfinite(best_cost):
      raise ValueError("no_complete_directed_ring")
   return best_path


def _heuristic_shortest_ring(scenario: Scenario, node_ids: tuple[str, ...]) -> tuple[str, ...]:
   """Multi-start nearest-neighbour with swap improvement for large pools."""
   best: tuple[str, ...] | None = None
   best_cost = math.inf
   for start in node_ids:
      remaining = set(node_ids)
      remaining.remove(start)
      path = [start]
      while remaining:
         nxt = min(remaining, key=lambda item: _regular_edge_cost(scenario, path[-1], item))
         path.append(nxt)
         remaining.remove(nxt)
      improved = True
      while improved:
         improved = False
         current_cost = ring_network_cost(scenario, path)
         for i in range(len(path)):
            for j in range(i + 1, len(path)):
               candidate = list(path)
               candidate[i], candidate[j] = candidate[j], candidate[i]
               candidate_cost = ring_network_cost(scenario, candidate)
               if candidate_cost + 1e-9 < current_cost:
                  path = candidate
                  improved = True
                  current_cost = candidate_cost
      cost = ring_network_cost(scenario, path)
      if cost < best_cost:
         best = tuple(path)
         best_cost = cost
   if best is None or not math.isfinite(best_cost):
      raise ValueError("no_complete_directed_ring")
   return best


def shortest_ring(scenario: Scenario, node_ids: Iterable[str]) -> tuple[str, ...]:
   node_ids = tuple(dict.fromkeys(node_ids))
   if len(node_ids) <= EXACT_RING_MAX_NODES:
      return _exact_shortest_ring(scenario, node_ids)
   return _heuristic_shortest_ring(scenario, node_ids)


def _edge_times(scenario: Scenario, order: tuple[str, ...], index: int) -> tuple[float, float]:
   if len(order) <= 1:
      return 0.0, 0.0
   src = order[index]
   if index == len(order) - 1:
      link = _link(scenario, src, order[0])
      if link is None:
         return math.inf, math.inf
      return 0.0, link.transfer_ms(scenario.model.token_envelope_bytes)
   link = _link(scenario, src, order[index + 1])
   if link is None:
      return math.inf, math.inf
   return (
      link.transfer_ms(scenario.model.activation_bytes_prefill(scenario.workload.prompt_tokens())),
      link.transfer_ms(scenario.model.activation_bytes_decode()),
   )


def allocate_layers(scenario: Scenario, order: tuple[str, ...]) -> tuple[list[int], list[StageEstimate]] | None:
   """DP contiguous layer counts, minimizing blended prefill/decode bottleneck."""
   layers = scenario.model.num_layers
   if not order or len(order) > layers:
      return None
   prompt = scenario.workload.prompt_tokens()
   output = scenario.workload.output_tokens
   token_total = max(1, prompt + output)
   prefill_weight = prompt / token_total
   decode_weight = output / token_total

   estimates: list[dict[int, StageEstimate]] = []
   services: list[dict[int, float]] = []
   for index, node_id in enumerate(order):
      node = scenario.nodes[node_id]
      prefill_edge_ms, decode_edge_ms = _edge_times(scenario, order, index)
      node_estimates: dict[int, StageEstimate] = {}
      node_services: dict[int, float] = {}
      for count in range(1, layers + 1):
         estimate = estimate_stage(node, scenario.model, scenario.workload, count)
         if estimate is None:
            break
         node_estimates[count] = estimate
         node_services[count] = (
            prefill_weight * (estimate.prefill_compute_ms + prefill_edge_ms) / prompt
            + decode_weight * (estimate.decode_compute_ms + decode_edge_ms)
         )
      if not node_estimates:
         return None
      estimates.append(node_estimates)
      services.append(node_services)

   # total -> (bottleneck, summed service, counts)
   dp: dict[int, tuple[float, float, tuple[int, ...]]] = {0: (0.0, 0.0, ())}
   for index in range(len(order)):
      next_dp: dict[int, tuple[float, float, tuple[int, ...]]] = {}
      for assigned, (old_bottleneck, old_sum, counts) in dp.items():
         for count, service in services[index].items():
            total = assigned + count
            if total > layers:
               break
            candidate = (max(old_bottleneck, service), old_sum + service, counts + (count,))
            previous = next_dp.get(total)
            if previous is None or candidate[:2] < previous[:2]:
               next_dp[total] = candidate
      dp = next_dp
   result = dp.get(layers)
   if result is None:
      return None
   counts = list(result[2])
   return counts, [estimates[index][count] for index, count in enumerate(counts)]


def build_plan(scenario: Scenario, order: Iterable[str], *, strategy: str) -> dict[str, Any] | None:
   order = tuple(order)
   allocation = allocate_layers(scenario, order)
   if allocation is None:
      return None
   counts, stage_estimates = allocation
   prompt = scenario.workload.prompt_tokens()
   concurrency = scenario.workload.concurrent_requests
   route: list[dict[str, Any]] = []
   decode_stages: list[float] = []
   prefill_stages: list[float] = []
   start = 0
   for index, (node_id, count, estimate) in enumerate(zip(order, counts, stage_estimates)):
      end = start + count - 1
      prefill_edge_ms, decode_edge_ms = _edge_times(scenario, order, index)
      prefill_stage_ms = estimate.prefill_compute_ms + prefill_edge_ms
      decode_stage_ms = estimate.decode_compute_ms + decode_edge_ms
      prefill_stages.append(prefill_stage_ms)
      decode_stages.append(decode_stage_ms)
      route.append({
         "node_id": node_id,
         "layers": [start, end],
         "layer_count": count,
         "path_class": "primary",
         "path_priority": 0,
         "prefill_compute_ms": round(estimate.prefill_compute_ms, 6),
         "decode_compute_ms": round(estimate.decode_compute_ms, 6),
         "prefill_outgoing_ms": round(prefill_edge_ms, 6),
         "decode_outgoing_ms": round(decode_edge_ms, 6),
         "memory": {
            "vram_used_gb": round(estimate.vram_used_gb, 6),
            "ram_used_gb": round(estimate.ram_used_gb, 6),
            "weights_gb": round(estimate.weights_gb, 6),
            "kv_cache_gb": round(estimate.kv_cache_gb, 6),
            "kv_cache_in_ram_gb": round(estimate.kv_cache_in_ram_gb, 6),
            "resident_layer_count": estimate.resident_layer_count,
            "ram_layer_count": estimate.ram_layer_count,
            "ram_execution": estimate.ram_execution,
         },
      })
      start = end + 1

   # Prefill ends after the final stage samples the first token, so no closure edge.
   prefill_latency_ms = sum(item.prefill_compute_ms for item in stage_estimates)
   if len(order) > 1:
      prefill_latency_ms += sum(_edge_times(scenario, order, index)[0] for index in range(len(order) - 1))
   decode_latency_ms = sum(decode_stages)
   prefill_bottleneck_ms = max(prefill_stages)
   decode_bottleneck_ms = max(decode_stages)
   prefill_requests_s = min(
      1000.0 / max(1e-9, prefill_bottleneck_ms),
      1000.0 * concurrency / max(1e-9, prefill_latency_ms),
   )
   prefill_tokens_s = prefill_requests_s * prompt
   decode_tokens_s = min(
      1000.0 / max(1e-9, decode_bottleneck_ms),
      1000.0 * concurrency / max(1e-9, decode_latency_ms),
   )
   output = scenario.workload.output_tokens
   combined_tokens_s = (prompt + output) / (
      prompt / max(1e-9, prefill_tokens_s) + output / max(1e-9, decode_tokens_s)
   )
   ring_id = _stable_id("ring", {
      "model": scenario.model.model_id,
      "order": order,
      "counts": counts,
   })
   for item in route:
      item["ring_id"] = ring_id
      item["stage_signature"] = _stable_id("stage", {
         "model": scenario.model.model_id,
         "layers": item["layers"],
      })
   return {
      "ok": True,
      "strategy": strategy,
      "ring_id": ring_id,
      "path_class": "primary",
      "path_priority": 0,
      "node_order": list(order),
      "route": route,
      "network_workload_cost_ms": round(ring_network_cost(scenario, order), 6),
      "estimated_prefill_latency_ms": round(prefill_latency_ms, 6),
      "estimated_prefill_tokens_s": round(prefill_tokens_s, 6),
      "estimated_decode_latency_ms_per_token": round(decode_latency_ms, 6),
      "estimated_decode_tokens_s": round(decode_tokens_s, 6),
      "estimated_combined_tokens_s": round(combined_tokens_s, 6),
      "estimated_single_request_tokens_s": round(
         (prompt + output) * 1000.0 / max(1e-9, prefill_latency_ms + output * decode_latency_ms),
         6,
      ),
   }


def single_request_throughput_ring(scenario: Scenario, node_ids: Iterable[str]) -> tuple[str, ...]:
   """Refine the shortest ring using modeled single-request throughput.

   The small-pool path is exact. Larger pools start from the network-shortest
   ring and use swap improvement, so there is no configured device-count cap.
   Network workload cost breaks equal-throughput ties.
   """
   node_ids = tuple(dict.fromkeys(node_ids))
   if len(node_ids) <= 1:
      return node_ids

   def score(order: tuple[str, ...]) -> tuple[float, float]:
      plan = build_plan(scenario, order, strategy="single-request-ring-candidate")
      if plan is None:
         return (-math.inf, -math.inf)
      return (plan["estimated_single_request_tokens_s"], -plan["network_workload_cost_ms"])

   if len(node_ids) <= EXACT_SINGLE_REQUEST_RING_MAX_NODES:
      best_order: tuple[str, ...] | None = None
      best_score = (-math.inf, -math.inf)
      for permutation in itertools.permutations(node_ids):
         candidate_score = score(permutation)
         if candidate_score > best_score:
            best_order = permutation
            best_score = candidate_score
      if best_order is None:
         raise ValueError("no_feasible_single_request_ring")
      return best_order

   current = shortest_ring(scenario, node_ids)
   current_score = score(current)
   improved = True
   while improved:
      improved = False
      for i in range(len(current)):
         for j in range(i + 1, len(current)):
            candidate = list(current)
            candidate[i], candidate[j] = candidate[j], candidate[i]
            candidate_order = tuple(candidate)
            candidate_score = score(candidate_order)
            if candidate_score > current_score:
               current = candidate_order
               current_score = candidate_score
               improved = True
   return current


def _initial_stage_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
   return {item["node_id"]: item for item in plan["route"]}


def prune_throughput_nodes(
   scenario: Scenario,
   initial_plan: dict[str, Any],
   *,
   reoptimize_ring: bool,
   minimum_improvement: float = 0.05,
) -> dict[str, Any]:
   """Drop nodes only when the resulting primary plan improves throughput."""
   current = initial_plan
   initial_stages = _initial_stage_map(initial_plan)
   dropped: list[str] = []
   trace: list[dict[str, Any]] = []
   while len(current["node_order"]) > 1:
      best_candidate: dict[str, Any] | None = None
      best_drop: str | None = None
      best_improvement = minimum_improvement
      for node_id in current["node_order"]:
         remaining = tuple(item for item in current["node_order"] if item != node_id)
         if reoptimize_ring:
            try:
               candidate_order = shortest_ring(scenario, remaining)
            except ValueError:
               continue
         else:
            candidate_order = remaining
         candidate = build_plan(
            scenario,
            candidate_order,
            strategy="throughput-pruned-reoptimized" if reoptimize_ring else "throughput-pruned-local",
         )
         if candidate is None:
            continue
         improvement = candidate["estimated_combined_tokens_s"] / current["estimated_combined_tokens_s"] - 1.0
         if improvement > best_improvement + 1e-12:
            best_candidate = candidate
            best_drop = node_id
            best_improvement = improvement
      if best_candidate is None or best_drop is None:
         break
      trace.append({
         "dropped_node_id": best_drop,
         "throughput_before": current["estimated_combined_tokens_s"],
         "throughput_after": best_candidate["estimated_combined_tokens_s"],
         "relative_improvement": round(best_improvement, 9),
      })
      dropped.append(best_drop)
      current = best_candidate

   secondary_members = []
   current_stages = current["route"]

   def owner_of(layer_index: int) -> dict[str, Any] | None:
      for stage in current_stages:
         if stage["layers"][0] <= layer_index <= stage["layers"][1]:
            return stage
      return None

   for node_id in dropped:
      original = initial_stages[node_id]
      start_layer, end_layer = original["layers"]
      entry_owner = owner_of(start_layer - 1) if start_layer > 0 else None
      exit_owner = owner_of(end_layer + 1) if end_layer + 1 < scenario.model.num_layers else None
      exact_primary_match = any(
         item["stage_signature"] == original["stage_signature"] for item in current_stages
      )
      matching_primary_nodes = {
         item["node_id"]
         for item in current_stages
         if item["stage_signature"] == original["stage_signature"]
      }
      shared_primary_node_ids = [
         item["node_id"]
         for item in current_stages
         if item["node_id"] not in matching_primary_nodes
      ]
      secondary_members.append({
         "node_id": node_id,
         "role": "secondary_stage_member",
         "replica_mode": "exact_primary_stage" if exact_primary_match else "exact_layer_fragment",
         "path_class": "secondary",
         "path_priority": 1,
         "source_initial_ring_id": initial_plan["ring_id"],
         "original_layers": original["layers"],
         "layer_count": original["layer_count"],
         "stage_signature": original["stage_signature"],
         "hybrid_entry": {
            "after_layer": start_layer - 1 if start_layer > 0 else None,
            "from_node_id": entry_owner["node_id"] if entry_owner else "token_input",
         },
         "hybrid_exit": {
            "before_layer": end_layer + 1 if end_layer + 1 < scenario.model.num_layers else None,
            "to_node_id": exit_owner["node_id"] if exit_owner else "token_output",
         },
         "requires_partial_primary_stage_execution": not exact_primary_match,
         "shared_primary_node_ids": shared_primary_node_ids,
         "runtime_capacity_reserved": False,
      })
   covered_layers: set[int] = set()
   for member in secondary_members:
      covered_layers.update(range(member["original_layers"][0], member["original_layers"][1] + 1))
   complete_secondary_coverage = covered_layers == set(range(scenario.model.num_layers))
   shared_primary_bottlenecks = [
      {
         "node_id": stage["node_id"],
         "layers": stage["layers"],
         "estimated_prefill_stage_ms": round(
            stage["prefill_compute_ms"] + stage["prefill_outgoing_ms"], 6
         ),
         "estimated_decode_stage_ms": round(
            stage["decode_compute_ms"] + stage["decode_outgoing_ms"], 6
         ),
      }
      for stage in current_stages
   ]
   current["initial_ring_id"] = initial_plan["ring_id"]
   current["initial_node_order"] = initial_plan["node_order"]
   current["pruning_trace"] = trace
   current["secondary_structure"] = {
      "secondary_path_id": _stable_id("secondary", {
         "initial_ring": initial_plan["ring_id"],
         "dropped": dropped,
      }),
      "path_class": "secondary",
      "path_priority": 1,
      "kind": "complete_secondary_ring" if complete_secondary_coverage else "alternative_stage_routes",
      "executable_complete_ring": complete_secondary_coverage,
      "executable_hybrid_routes": bool(secondary_members),
      "activation_condition": (
         "router_may_schedule_pinned_requests"
         if complete_secondary_coverage
         else "router_may_splice_exact_layer_fragments_into_compatible_primary_chain"
      ),
      "covered_layer_count": len(covered_layers),
      "required_layer_count": scenario.model.num_layers,
      "runtime_capacity_reserved": False,
      "capacity_reservation_fraction": 0.0,
      "capacity_policy": "model_only_report_shared_primary_bottlenecks",
      "router_policy": "out_of_scope",
      "shared_primary_bottlenecks": shared_primary_bottlenecks,
      "members": secondary_members,
   }
   return current


def best_shortest_subset(scenario: Scenario) -> dict[str, Any]:
   """Global comparison baseline: shortest ring for every non-empty subset."""
   node_ids = tuple(scenario.nodes)
   best: dict[str, Any] | None = None
   evaluated = 0
   for size in range(1, len(node_ids) + 1):
      if size > scenario.model.num_layers:
         break
      for subset in itertools.combinations(node_ids, size):
         try:
            order = shortest_ring(scenario, subset)
         except ValueError:
            continue
         plan = build_plan(scenario, order, strategy="global-best-shortest-subset")
         if plan is None:
            continue
         evaluated += 1
         if best is None or plan["estimated_combined_tokens_s"] > best["estimated_combined_tokens_s"]:
            best = plan
   if best is None:
      raise ValueError("no_feasible_subset")
   best["evaluated_subsets"] = evaluated
   return best


def benchmark_scenario(scenario: Scenario, *, include_global_subset: bool = True, seed: int = 7) -> dict[str, Any]:
   eligible = []
   capacity_ineligible = []
   for node_id, node in scenario.nodes.items():
      if estimate_stage(node, scenario.model, scenario.workload, 1) is None:
         capacity_ineligible.append(node_id)
      else:
         eligible.append(node_id)
   if not eligible:
      raise ValueError("no_node_can_hold_one_layer_without_disk")
   if len(eligible) > scenario.model.num_layers:
      # This is a physical layer-count limit, not a configured device maximum.
      capacity_ineligible.extend(eligible[scenario.model.num_layers:])
      eligible = eligible[:scenario.model.num_layers]

   shortest_order = shortest_ring(scenario, eligible)
   shortest_initial = build_plan(scenario, shortest_order, strategy="shortest-ring-all-feasible")
   if shortest_initial is None:
      raise ValueError("all_feasible_ring_cannot_cover_model")
   initial_order = single_request_throughput_ring(scenario, eligible)
   initial = build_plan(scenario, initial_order, strategy="single-request-throughput-ring-all-feasible")
   if initial is None:
      raise ValueError("single_request_ring_cannot_cover_model")
   local_pruned = prune_throughput_nodes(scenario, initial, reoptimize_ring=False)
   reoptimized_pruned = prune_throughput_nodes(scenario, initial, reoptimize_ring=True)

   rng = random.Random(seed)
   random_order = list(eligible)
   rng.shuffle(random_order)
   random_plan = build_plan(scenario, random_order, strategy="random-ring")
   strategies = {
      "shortest_all": shortest_initial,
      "single_request_throughput_all": initial,
      "throughput_pruned_local": local_pruned,
      "throughput_pruned_reoptimized": reoptimized_pruned,
      "random_ring": random_plan,
   }
   if include_global_subset:
      strategies["global_best_shortest_subset"] = best_shortest_subset(scenario)

   ranked = sorted(
      (
         {
            "strategy": name,
            "combined_tokens_s": plan["estimated_combined_tokens_s"],
            "prefill_tokens_s": plan["estimated_prefill_tokens_s"],
            "decode_tokens_s": plan["estimated_decode_tokens_s"],
            "active_nodes": len(plan["node_order"]),
         }
         for name, plan in strategies.items()
         if plan is not None
      ),
      key=lambda item: item["combined_tokens_s"],
      reverse=True,
   )
   return {
      "ok": True,
      "protocol": "mycelium.planner_simulation.v1",
      "scenario": scenario.name,
      "model": asdict(scenario.model),
      "workload": {
         **asdict(scenario.workload),
         "assumed_prompt_tokens_per_request": scenario.workload.prompt_tokens(),
         "planned_kv_tokens_per_request": scenario.workload.planned_kv_tokens(),
      },
      "assumptions": {
         "disk_offloading": False,
         "ram_weight_execution": "minimum_of_cpu_execution_and_gpu_streaming",
         "prefill_and_decode_share_layer_allocation": True,
         "secondary_members_contribute_to_primary_throughput": False,
         "secondary_runtime_capacity_reserved": False,
         "secondary_router_policy": "out_of_scope",
         "node_drop_minimum_relative_throughput_improvement": 0.05,
         "shortest_ring_metric": "single_expected_request_network_time",
         "recommended_initial_ring_metric": "modeled_end_to_end_single_request_tokens_s",
         "exact_ring_search_at_or_below_nodes": EXACT_RING_MAX_NODES,
         "exact_single_request_ring_search_at_or_below_nodes": EXACT_SINGLE_REQUEST_RING_MAX_NODES,
      },
      "capacity_ineligible": capacity_ineligible,
      "ranking": ranked,
      "strategies": strategies,
      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
   }


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description="Benchmark Mycelium planner strategies on a hypothetical cluster")
   parser.add_argument("--scenario", required=True)
   parser.add_argument("--out")
   parser.add_argument("--seed", type=int, default=7)
   parser.add_argument("--skip-global-subset", action="store_true")
   args = parser.parse_args(argv)

   scenario = load_scenario(args.scenario)
   report = benchmark_scenario(
      scenario,
      include_global_subset=not args.skip_global_subset,
      seed=args.seed,
   )
   rendered = json.dumps(report, indent=2, sort_keys=True)
   if args.out:
      Path(args.out).write_text(rendered + "\n")
   print(rendered)
   return 0


if __name__ == "__main__":
   raise SystemExit(main())
