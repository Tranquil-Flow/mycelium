from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .contracts import LayerRange, ModelIdentity, NodeCapability, PlanningPolicy, WorkloadScenario


@dataclass(frozen=True)
class StageCost:
    feasible: bool
    prefill_ms: float
    decode_ms: float
    spill_penalty_ms: float
    required_memory_bytes: float
    spill_bytes: float
    service_work_ms: float
    diagnostic: str = ""

    @property
    def effective_prefill_ms(self) -> float:
        return self.prefill_ms + self.spill_penalty_ms

    @property
    def effective_decode_ms(self) -> float:
        return self.decode_ms + self.spill_penalty_ms


@dataclass(frozen=True)
class AllocatedStage:
    stage_index: int
    node_id: str
    layer_range: LayerRange
    cost: StageCost


@dataclass(frozen=True)
class AllocationResult:
    stages: tuple[AllocatedStage, ...]
    bottleneck_service_work_ms: float
    feasible: bool
    diagnostics: tuple[str, ...] = ()


def stage_cost(
    node: NodeCapability,
    layer_count: int,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
) -> StageCost:
    if layer_count <= 0:
        raise ValueError("layer_count must be positive")
    weight_bytes = model.weight_bytes_per_layer * layer_count
    kv_bytes = (
        model.kv_bytes_per_layer_token
        * layer_count
        * workload.total_context_tokens
        * workload.concurrency
    )
    required = weight_bytes + kv_bytes + node.workspace_bytes
    usable_total = node.total_memory_bytes
    if required > usable_total:
        return StageCost(False, math.inf, math.inf, math.inf, required, 0, math.inf, "total_memory_exceeded")
    fast_limit = node.fast_memory_bytes * (1.0 - policy.memory_reserve_fraction)
    spill_bytes = max(0.0, required - fast_limit)
    if spill_bytes and node.spill_bandwidth_Bps <= 0:
        return StageCost(False, math.inf, math.inf, math.inf, required, spill_bytes, math.inf, "spill_unavailable")
    spill_penalty = spill_bytes / node.spill_bandwidth_Bps * 1000.0 if spill_bytes else 0.0
    prefill = node.prefill_ms_per_layer_token * layer_count * workload.effective_prompt_tokens
    decode = node.decode_ms_per_layer_token * layer_count
    service_work = prefill + workload.output_tokens * decode + (workload.output_tokens + 1) * spill_penalty
    return StageCost(True, prefill, decode, spill_penalty, required, spill_bytes, service_work)


def allocate_layers(
    ordered_nodes: Sequence[NodeCapability],
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
) -> AllocationResult:
    nodes = tuple(ordered_nodes)
    if not nodes:
        return AllocationResult((), math.inf, False, ("no nodes",))
    if len(nodes) > model.num_layers:
        return AllocationResult((), math.inf, False, ("stage count exceeds layer count",))

    # State: (stage_count, assigned_layers) -> (bottleneck, counts)
    dp: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {(0, 0): (0.0, ())}
    for stage_index, node in enumerate(nodes, start=1):
        remaining_stages = len(nodes) - stage_index
        for assigned in range(stage_index, model.num_layers - remaining_stages + 1):
            best: tuple[float, tuple[int, ...]] | None = None
            minimum_previous = stage_index - 1
            for previous_layers in range(minimum_previous, assigned):
                prior = dp.get((stage_index - 1, previous_layers))
                if prior is None:
                    continue
                take = assigned - previous_layers
                cost = stage_cost(node, take, model, workload, policy)
                if not cost.feasible:
                    continue
                candidate = (max(prior[0], cost.service_work_ms), prior[1] + (take,))
                if best is None or candidate < best:
                    best = candidate
            if best is not None:
                dp[(stage_index, assigned)] = best

    state = dp.get((len(nodes), model.num_layers))
    if state is None:
        return AllocationResult((), math.inf, False, ("no feasible contiguous allocation",))
    stages: list[AllocatedStage] = []
    cursor = 0
    for index, (node, count) in enumerate(zip(nodes, state[1])):
        cost = stage_cost(node, count, model, workload, policy)
        stages.append(AllocatedStage(index, node.node_id, LayerRange(cursor, cursor + count), cost))
        cursor += count
    return AllocationResult(tuple(stages), state[0], True)
