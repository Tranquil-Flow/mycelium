from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Tuple


NUMERIC_EPSILON = 1e-12


def _finite_nonnegative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, order=True)
class LayerRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("layer range must be positive and half-open")

    @property
    def count(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class ModelIdentity:
    model_id: str
    revision: str
    weight_digest: str
    architecture: str
    num_layers: int
    hidden_size: int
    dtype_bytes: int
    kv_heads: int
    head_dim: int
    weight_bytes: int

    def __post_init__(self) -> None:
        if not self.model_id or not self.revision or not self.weight_digest:
            raise ValueError("model identity requires id, immutable revision, and digest")
        if not self.weight_digest.startswith("sha256:") or len(self.weight_digest) != 71:
            raise ValueError("weight_digest must be sha256:<64 hex characters>")
        if any(c not in "0123456789abcdefABCDEF" for c in self.weight_digest[7:]):
            raise ValueError("weight_digest must contain hexadecimal characters")
        for name in ("num_layers", "hidden_size", "dtype_bytes", "kv_heads", "head_dim", "weight_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @property
    def weight_bytes_per_layer(self) -> float:
        return self.weight_bytes / self.num_layers

    @property
    def kv_bytes_per_layer_token(self) -> int:
        return 2 * self.kv_heads * self.head_dim * self.dtype_bytes

    def activation_bytes(self, tokens: int, batch: int = 1) -> int:
        if tokens < 0 or batch <= 0:
            raise ValueError("tokens must be non-negative and batch positive")
        return tokens * batch * self.hidden_size * self.dtype_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "revision": self.revision,
            "weight_digest": self.weight_digest,
            "architecture": self.architecture,
            "num_layers": self.num_layers,
            "hidden_size": self.hidden_size,
            "dtype_bytes": self.dtype_bytes,
            "kv_heads": self.kv_heads,
            "head_dim": self.head_dim,
            "weight_bytes": self.weight_bytes,
        }


@dataclass(frozen=True)
class NodeCapability:
    node_id: str
    prefill_ms_per_layer_token: float
    decode_ms_per_layer_token: float
    fast_memory_bytes: int
    total_memory_bytes: int
    memory_bandwidth_Bps: float
    spill_bandwidth_Bps: float
    eligible: bool = True
    exclusion_reason: Optional[str] = None
    region: str = "unknown"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    workspace_bytes: int = 0
    calibration_confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("node_id is required")
        for name in (
            "prefill_ms_per_layer_token",
            "decode_ms_per_layer_token",
            "fast_memory_bytes",
            "total_memory_bytes",
            "memory_bandwidth_Bps",
            "spill_bandwidth_Bps",
            "workspace_bytes",
        ):
            _finite_nonnegative(name, getattr(self, name))
        if self.total_memory_bytes < self.fast_memory_bytes:
            raise ValueError("total memory cannot be smaller than fast memory")
        if not 0 <= self.calibration_confidence <= 1:
            raise ValueError("calibration_confidence must be in [0, 1]")


@dataclass(frozen=True)
class DirectedLinkObservation:
    src: str
    dst: str
    rtt_ms: float
    jitter_ms: float
    bandwidth_Bps: Optional[float]
    loss_ratio: float = 0.0
    geolocation_floor_ms: float = 0.0
    measured_at: Optional[str] = None
    inferred: bool = False
    stale: bool = False
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.src or not self.dst or self.src == self.dst:
            raise ValueError("directed link requires distinct source and destination")
        for name in ("rtt_ms", "jitter_ms", "loss_ratio", "geolocation_floor_ms"):
            _finite_nonnegative(name, getattr(self, name))
        if self.bandwidth_Bps is not None and self.bandwidth_Bps <= 0:
            raise ValueError("bandwidth must be positive when present")
        if self.loss_ratio > 1 or not 0 <= self.confidence <= 1:
            raise ValueError("loss_ratio and confidence must be in [0, 1]")


@dataclass(frozen=True)
class WorkloadScenario:
    name: str
    prompt_tokens: int
    output_tokens: int
    concurrency: int
    probability: Optional[float] = None
    user_scale: float = 1.0
    arrival_rate_rps: Optional[float] = None
    system_prefix_tokens: int = 0
    history_tokens: int = 0
    prompt_p95_tokens: Optional[int] = None
    output_p95_tokens: Optional[int] = None
    batch_size: int = 1
    qos_class: str = "interactive"

    def __post_init__(self) -> None:
        if not self.name or self.prompt_tokens < 0 or self.output_tokens <= 0 or self.concurrency <= 0:
            raise ValueError("invalid workload scenario")
        if self.probability is not None and not 0 <= self.probability <= 1:
            raise ValueError("probability must be in [0, 1]")
        if self.user_scale <= 0 or self.system_prefix_tokens < 0 or self.history_tokens < 0:
            raise ValueError("invalid workload scaling")
        if self.arrival_rate_rps is not None and self.arrival_rate_rps < 0:
            raise ValueError("arrival rate must be non-negative")
        if self.prompt_p95_tokens is not None and self.prompt_p95_tokens < self.prompt_tokens:
            raise ValueError("prompt p95 must not be smaller than prompt p50")
        if self.output_p95_tokens is not None and self.output_p95_tokens < self.output_tokens:
            raise ValueError("output p95 must not be smaller than output p50")
        if self.batch_size <= 0 or self.qos_class not in {"interactive", "batch"}:
            raise ValueError("invalid workload batch or QoS class")

    @property
    def effective_prompt_tokens(self) -> int:
        return self.prompt_tokens + self.system_prefix_tokens + self.history_tokens

    @property
    def total_context_tokens(self) -> int:
        return self.effective_prompt_tokens + self.output_tokens


@dataclass(frozen=True)
class SpeculativeConfig:
    draft_model_id: str
    draft_revision: str
    proposal_width: int
    accepted_count_distribution: Tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.draft_model_id or not self.draft_revision or self.proposal_width <= 0:
            raise ValueError("invalid speculative configuration")
        if len(self.accepted_count_distribution) != self.proposal_width + 1:
            raise ValueError("acceptance distribution must cover counts 0..proposal_width")
        if any(p < 0 for p in self.accepted_count_distribution):
            raise ValueError("acceptance probabilities must be non-negative")
        if abs(sum(self.accepted_count_distribution) - 1.0) > NUMERIC_EPSILON:
            raise ValueError("acceptance probabilities must sum to one")


@dataclass(frozen=True)
class PlanningPolicy:
    exact_cycle_max_nodes: int = 7
    held_karp_max_nodes: int = 12
    local_search_max_nodes: int = 32
    clustered_max_nodes: int = 128
    search_candidate_budget: int = 100_000
    replica_budget: int = 32
    minimum_replica_gain_fraction: float = 0.05
    replica_uncertainty_fraction: float = 0.1
    conservative_bandwidth_Bps: Optional[float] = 1_000_000.0
    exclude_missing_bandwidth: bool = False
    jitter_guard_sigma: float = 2.0
    loss_penalty_ms: float = 10.0
    memory_reserve_fraction: float = 0.1
    objective: str = "slo_goodput"
    ttft_slo_ms: float = 2_000.0
    tpot_slo_ms: float = 200.0

    def __post_init__(self) -> None:
        thresholds = (
            self.exact_cycle_max_nodes,
            self.held_karp_max_nodes,
            self.local_search_max_nodes,
            self.clustered_max_nodes,
        )
        if any(x <= 0 for x in thresholds) or tuple(sorted(thresholds)) != thresholds:
            raise ValueError("search thresholds must be positive and nondecreasing")
        if self.search_candidate_budget <= 0 or self.replica_budget < 0:
            raise ValueError("invalid planning budget")
        if not 0 <= self.minimum_replica_gain_fraction <= 1:
            raise ValueError("minimum replica gain fraction must be in [0, 1]")
        if not 0 <= self.replica_uncertainty_fraction < 1:
            raise ValueError("replica uncertainty fraction must be in [0, 1)")
        if self.conservative_bandwidth_Bps is not None and self.conservative_bandwidth_Bps <= 0:
            raise ValueError("fallback bandwidth must be positive")
        if not 0 <= self.memory_reserve_fraction < 1:
            raise ValueError("memory reserve must be in [0, 1)")
        if self.objective not in {"slo_goodput", "balanced", "prefill_ttft", "decode_tpot"}:
            raise ValueError("unknown planning objective")


@dataclass(frozen=True)
class StagePlacement:
    placement_id: str
    replica_group_id: str
    node_id: str
    layer_range: LayerRange
    primary: bool
    service_capacity_rps: float

    def __post_init__(self) -> None:
        if not self.placement_id or not self.replica_group_id or not self.node_id:
            raise ValueError("placement identifiers are required")
        if self.service_capacity_rps < 0:
            raise ValueError("service capacity must be non-negative")


@dataclass(frozen=True)
class LegalTrack:
    track_id: str
    placement_ids: Tuple[str, ...]
    traffic_fraction: float
    cost_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.track_id or not self.placement_ids:
            raise ValueError("legal track must be non-empty")
        if not 0 <= self.traffic_fraction <= 1 or self.cost_ms < 0:
            raise ValueError("invalid legal track")


@dataclass(frozen=True)
class PlanEdge:
    src_placement_id: str
    dst_placement_id: str
    capacity_rps: float
    cost_ms: float
    kind: str = "forward"

    def __post_init__(self) -> None:
        if not self.src_placement_id or not self.dst_placement_id:
            raise ValueError("plan edge endpoints are required")
        if self.src_placement_id == self.dst_placement_id:
            raise ValueError("plan edge endpoints must be distinct")
        if self.capacity_rps < 0 or self.cost_ms < 0:
            raise ValueError("plan edge capacity and cost must be non-negative")
        if self.kind not in {"forward", "loopback"}:
            raise ValueError("unknown plan edge kind")


@dataclass(frozen=True)
class Loopback:
    src_placement_id: str
    dst_placement_id: str
    payload_bytes: int
    cost_ms: float

    def __post_init__(self) -> None:
        if self.payload_bytes <= 0 or self.cost_ms < 0:
            raise ValueError("invalid loopback")


@dataclass(frozen=True)
class SearchProvenance:
    mode: str
    globally_exact: bool
    explored_candidates: int
    candidate_node_count: int
    candidate_budget: int
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        if not self.mode or self.explored_candidates < 0 or self.candidate_node_count < 0:
            raise ValueError("invalid search provenance")
        if self.candidate_budget <= 0 or self.explored_candidates > self.candidate_budget:
            raise ValueError("invalid search candidate budget provenance")
        if self.globally_exact and self.budget_exhausted:
            raise ValueError("budget-exhausted search cannot claim global exactness")


@dataclass(frozen=True)
class RoutePlanV2:
    model: ModelIdentity
    snapshot_digest: str
    placements: Tuple[StagePlacement, ...]
    legal_tracks: Tuple[LegalTrack, ...]
    forward_edges: Tuple[PlanEdge, ...]
    loopbacks: Tuple[Loopback, ...]
    provenance: SearchProvenance
    workload_name: str
    metrics: Mapping[str, Any]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    handoff_state: str = "placement_intent_only"
    protocol: str = "mycelium.route_plan.v2"

    def __post_init__(self) -> None:
        if self.handoff_state != "placement_intent_only":
            raise ValueError("Planner may emit placement intent only")
        if not self.snapshot_digest:
            raise ValueError("snapshot digest is required")
