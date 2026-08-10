from __future__ import annotations

import math
from dataclasses import dataclass

from .allocation import AllocationResult
from .contracts import ModelIdentity, PlanningPolicy, WorkloadScenario
from .network_cost import EdgeCost, transfer_time_ms
from .physical_graph import PhysicalGraph
from .workload import expected_response_ms


@dataclass(frozen=True)
class PhaseScore:
    scenario_name: str
    ttft_ms: float
    tpot_ms: float
    single_request_tps: float
    output_goodput_tps: float
    prefill_compute_ms: float
    prefill_network_ms: float
    decode_compute_ms: float
    decode_network_ms: float
    prefill_loopback_ms: float
    decode_loopback_ms: float
    decode_bottleneck_ms: float
    prefill_payload_bytes: int
    decode_payload_bytes: int
    expected_response_ms: float
    required_memory_bytes: float
    confidence: float


def _cost(
    graph: PhysicalGraph,
    src: str,
    dst: str,
    payload: int,
    policy: PlanningPolicy,
) -> EdgeCost:
    link = graph.link(src, dst)
    if link is None:
        raise ValueError(f"missing directed link {src}->{dst}")
    return transfer_time_ms(link, payload, policy)


def score_phases(
    allocation: AllocationResult,
    graph: PhysicalGraph,
    model: ModelIdentity,
    workload: WorkloadScenario,
    policy: PlanningPolicy,
    *,
    loopback_payload_bytes: int = 16,
) -> PhaseScore:
    if not allocation.feasible or not allocation.stages:
        raise ValueError("cannot score infeasible allocation")
    stages = allocation.stages
    prefill_payload = model.activation_bytes(workload.effective_prompt_tokens)
    decode_payload = model.activation_bytes(1)
    prefill_edges: list[EdgeCost] = []
    decode_edges: list[EdgeCost] = []
    for left, right in zip(stages, stages[1:]):
        prefill_edges.append(_cost(graph, left.node_id, right.node_id, prefill_payload, policy))
        decode_edges.append(_cost(graph, left.node_id, right.node_id, decode_payload, policy))

    if len(stages) == 1:
        loopback = None
        decode_loopback = 0.0
        loopback_confidence = 1.0
    else:
        loopback = _cost(graph, stages[-1].node_id, stages[0].node_id, loopback_payload_bytes, policy)
        decode_loopback = loopback.total_ms
        loopback_confidence = loopback.confidence

    prefill_compute = sum(stage.cost.effective_prefill_ms for stage in stages)
    decode_compute = sum(stage.cost.effective_decode_ms for stage in stages)
    prefill_network = sum(edge.total_ms for edge in prefill_edges)
    decode_network = sum(edge.total_ms for edge in decode_edges)
    ttft = prefill_compute + prefill_network
    tpot = decode_compute + decode_network + decode_loopback
    resources = [stage.cost.effective_decode_ms for stage in stages]
    resources.extend(edge.total_ms for edge in decode_edges)
    if len(stages) > 1:
        resources.append(decode_loopback)
    bottleneck = max(resources)
    single_tps = 1000.0 / tpot if tpot > 0 else math.inf
    saturated_tps = 1000.0 / bottleneck if bottleneck > 0 else math.inf
    concurrency_limited = workload.concurrency * single_tps
    goodput = min(saturated_tps, concurrency_limited)
    if ttft > policy.ttft_slo_ms or tpot > policy.tpot_slo_ms:
        goodput = 0.0
    confidences = [graph.nodes[stage.node_id].calibration_confidence for stage in stages]
    confidences.extend(edge.confidence for edge in prefill_edges + decode_edges)
    confidences.append(loopback_confidence)
    return PhaseScore(
        scenario_name=workload.name,
        ttft_ms=ttft,
        tpot_ms=tpot,
        single_request_tps=single_tps,
        output_goodput_tps=goodput,
        prefill_compute_ms=prefill_compute,
        prefill_network_ms=prefill_network,
        decode_compute_ms=decode_compute,
        decode_network_ms=decode_network,
        prefill_loopback_ms=0.0,
        decode_loopback_ms=decode_loopback,
        decode_bottleneck_ms=bottleneck,
        prefill_payload_bytes=prefill_payload,
        decode_payload_bytes=decode_payload,
        expected_response_ms=expected_response_ms(ttft, tpot, workload.output_tokens),
        required_memory_bytes=sum(stage.cost.required_memory_bytes for stage in stages),
        confidence=min(confidences),
    )
