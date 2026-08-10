"""Versioned immutable contracts for the standalone Mycelium Router."""

from dataclasses import dataclass, replace


EXECUTION_GRAPH_PROTOCOL = "mycelium.execution_graph.v1"
PATH_MANIFEST_PROTOCOL = "mycelium.path_manifest.v1"


@dataclass(frozen=True)
class LayerRange:
   start_layer: int
   end_layer_exclusive: int
   layer_count: int


@dataclass(frozen=True)
class StageCost:
   prefill_work_units_per_prompt_token: float
   decode_work_units_per_token: float
   kv_bytes_per_context_token: int


@dataclass(frozen=True)
class Placement:
   placement_id: str
   node_id: str
   replica_group_id: str
   assignment_id: str
   stage_signature: str
   load_proof_digest: str
   runtime_backend: str
   runtime_endpoint: str
   lifecycle_state: str = "ACTIVE"


@dataclass(frozen=True)
class Stage:
   stage_id: str
   layer_range: LayerRange
   component_roles: tuple[str, ...]
   stage_cost: StageCost
   placements: tuple[Placement, ...]


@dataclass(frozen=True)
class PlacementEdge:
   edge_id: str
   from_placement_id: str
   to_placement_id: str
   link_id: str


@dataclass(frozen=True)
class ExecutionGraph:
   deployment_id: str
   deployment_epoch: int
   topology_version: int
   model_id: str
   resolved_commit: str
   manifest_digest: str
   entry_stage_id: str
   final_stage_id: str
   hidden_size: int
   activation_bytes: int
   token_envelope_bytes: int
   stages: tuple[Stage, ...]
   edges: tuple[PlacementEdge, ...]
   loopback_edges: tuple[PlacementEdge, ...]
   protocol: str = EXECUTION_GRAPH_PROTOCOL

   def with_stages(self, stages: tuple[Stage, ...]) -> "ExecutionGraph":
      return replace(self, stages=stages)

   def with_edges(self, edges: tuple[PlacementEdge, ...]) -> "ExecutionGraph":
      return replace(self, edges=edges)

   def with_loopback_edges(
      self, loopback_edges: tuple[PlacementEdge, ...]
   ) -> "ExecutionGraph":
      return replace(self, loopback_edges=loopback_edges)


@dataclass(frozen=True)
class PathHop:
   stage_id: str
   placement_id: str
   reservation_id: str
   reservation_expires_at: float = float("inf")
   reservation_epoch: int = -1


@dataclass(frozen=True)
class PathManifest:
   path_id: str
   path_attempt: int
   request_id: str
   deployment_id: str
   deployment_epoch: int
   topology_version: int
   manifest_digest: str
   ordered_hops: tuple[PathHop, ...]
   loopback_edge_id: str
   protocol: str = PATH_MANIFEST_PROTOCOL


@dataclass(frozen=True)
class DeviceState:
   node_id: str
   state_seq: int
   last_updated: float
   availability: str
   compute_units_per_second: float
   free_compute_fraction: float
   available_kv_bytes: int
   pending_hop_queue_depth: int
   neighbor_rtt_ms: dict[str, float]
   neighbor_bandwidth_bytes_per_second: dict[str, float]

   def with_availability(self, availability: str) -> "DeviceState":
      return replace(self, availability=availability)


@dataclass(frozen=True)
class RequestContext:
   request_id: str
   prompt_token_ids: tuple[int, ...]
   max_new_tokens: int
   expected_new_tokens: int
   qos_class: str
   admitted_at: float
   target_ttft_ms: float
   target_tpot_ms: float
   target_tokens_per_second: float
   sampling_seed: int
   generation_config_digest: str


@dataclass(frozen=True)
class RouterConfig:
   interactive_alpha: float = 0.35
   batch_alpha: float = 0.10
   stale_after_seconds: float = 10.0
   confidence_half_life_seconds: float = 10.0
   minimum_confidence: float = 0.25
   conservative_compute_fraction: float = 0.10
   conservative_queue_depth: int = 20
   default_bandwidth_bytes_per_second: float = 1_000_000.0
   default_rtt_ms: float = 100.0
   interactive_base_priority: float = 100.0
   batch_base_priority: float = 10.0
   aging_priority_per_second: float = 1.0
   batch_aging_multiplier: float = 4.0
   maximum_deficit_boost: float = 50.0
   maximum_recovery_attempts: int = 3
   reservation_lease_seconds: float = 30.0
   idempotency_retention_seconds: float = 300.0
   maximum_idempotency_entries: int = 10_000
   maximum_pending_hops: int = 1_024
   maximum_pending_bytes: int = 268_435_456
   backpressure_retry_after_seconds: float = 0.01
   prefill_chunk_size_tokens: int = 0
   maximum_runtime_batch_size: int = 20
   decode_runtime_batch_size: int = 8
   prefill_runtime_batch_size: int = 20
   maximum_runtime_batch_bytes: int = 2_400_000
   prefill_collection_window_seconds: float = 0.002
   batch_deadline_guard_seconds: float = 0.001
   batch_bdp_multiplier: float = 2.0
   batch_stats_stale_seconds: float = 10.0
   batch_observation_ewma_alpha: float = 0.25
   maximum_batch_decision_history: int = 1_024

   def alpha_for(self, qos_class: str) -> float:
      return self.batch_alpha if qos_class == "batch" else self.interactive_alpha


@dataclass(frozen=True)
class ReservationRequest:
   request_id: str
   path_id: str
   path_attempt: int
   placement_id: str
   kv_bytes: int
   deployment_epoch: int
   lease_expires_at: float


@dataclass(frozen=True)
class ReservationResult:
   accepted: bool
   reservation_id: str = ""
   reason: str = ""
   deployment_epoch: int = -1
   expires_at: float = 0.0


@dataclass(frozen=True)
class ReservationCommitResult:
   accepted: bool
   reason: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
   ttft_ms: float
   tpot_ms: float
   prefill_transfer_ms: float
   decode_transfer_ms: float
   total_score: float
   confidence: float
   fallback_nodes: tuple[str, ...]


@dataclass(frozen=True)
class BranchDecision:
   placement_id: str
   complete_route: tuple[str, ...]
   score: ScoreBreakdown


@dataclass(frozen=True)
class PathBuildState:
   request: RequestContext
   graph: ExecutionGraph
   path_id: str
   path_attempt: int
   ordered_hops: tuple[PathHop, ...] = ()
   excluded_placements: frozenset[str] = frozenset()
   excluded_edges: frozenset[str] = frozenset()
   excluded_devices: frozenset[str] = frozenset()

   def with_excluded_devices(self, devices: frozenset[str]) -> "PathBuildState":
      return replace(self, excluded_devices=devices)


@dataclass(frozen=True)
class RuntimeBatchKey:
   deployment_id: str
   deployment_epoch: int
   model_commit: str
   manifest_digest: str
   placement_id: str
   assignment_id: str
   stage_signature: str
   load_proof_digest: str
   runtime_backend: str
   phase: str
   hidden_size: int
   activation_bytes: int
   token_span: int
   speculative_role: str = "NONE"
   speculative_width: int = 0


@dataclass(frozen=True)
class BatchNetworkStats:
   """Recent directed-link observations used only for batch sizing."""

   one_way_p95_ms: float
   goodput_bytes_per_second: float
   loss_rate: float
   receiver_queue_ms: float
   observed_at: float


@dataclass(frozen=True)
class BatchExecutionObservation:
   """One runtime batch sample; activation content is never retained."""

   phase: str
   batch_size: int
   payload_bytes: int
   execution_ms: float
   successful: bool


@dataclass(frozen=True)
class BatchDecision:
   action: str
   phase: str
   batch_size: int
   target_items: int
   available_items: int
   predicted_payload_bytes: int
   predicted_transfer_ms: float
   ready_at: float
   reason: str


@dataclass(frozen=True)
class HopWorkItem:
   request_id: str
   path_id: str
   path_attempt: int
   phase: str
   token_index: int
   hop_index: int
   placement_id: str
   qos_class: str
   deficit_ratio: float
   enqueued_at: float
   idempotency_key: str
   payload: object
   prefill_chunk_token_count: int = 0
   batch_key: RuntimeBatchKey | None = None
   deadline_at: float = float("inf")
   position: int = 0
   terminal: bool = False
   lease_expires_at: float = float("inf")


@dataclass(frozen=True)
class RuntimeBatch:
   compatibility_key: RuntimeBatchKey | None
   items: tuple[HopWorkItem, ...]
   decision: BatchDecision | None = None

   def __post_init__(self) -> None:
      if not self.items:
         raise ValueError("empty_runtime_batch")
      if any(item.batch_key != self.compatibility_key for item in self.items):
         raise ValueError("incompatible_runtime_batch")
      if self.compatibility_key is None and len(self.items) != 1:
         raise ValueError("incompatible_runtime_batch")


@dataclass(frozen=True)
class HopHeader:
   request_id: str
   path_id: str
   path_attempt: int
   phase: str
   token_index: int
   hop_index: int
   source_placement_id: str
   destination_placement_id: str
   topology_version: int
   idempotency_key: str
   prefill_chunk_token_count: int = 0


@dataclass(frozen=True)
class ManifestDelta:
   request_id: str
   path_id: str
   path_attempt: int
   hop_index: int
   hop: PathHop


@dataclass(frozen=True)
class PathCancellation:
   request_id: str
   path_id: str
   path_attempt: int
   topology_version: int


@dataclass(frozen=True)
class TokenEvent:
   request_id: str
   path_id: str
   path_attempt: int
   token_index: int
   token_id: int
   sampling_counter: int


@dataclass(frozen=True)
class PrefillChunkCompleted:
   request_id: str
   path_id: str
   path_attempt: int
   chunk_index: int
   token_count: int


@dataclass(frozen=True)
class FailureReport:
   request_id: str
   path_id: str
   path_attempt: int
   token_index: int
   scope: str
   reason: str
   placement_id: str = ""
   edge_id: str = ""
   node_id: str = ""


@dataclass(frozen=True)
class RuntimeResult:
   success: bool
   payload: object = None
   token_id: int | None = None
   failure_scope: str = ""
   failure_reason: str = ""


@dataclass(frozen=True)
class RelayOutcome:
   token_event: TokenEvent | None = None
   failure_report: FailureReport | None = None


@dataclass(frozen=True)
class HopReceiveResult:
   disposition: str
   reason: str = ""
   retry_after_seconds: float = 0.0
   forwarded_header: HopHeader | None = None
   token_event: TokenEvent | None = None
   failure_report: FailureReport | None = None
   prefill_chunk_completed: PrefillChunkCompleted | None = None


@dataclass(frozen=True)
class ProgressivePrefillContext:
   graph: ExecutionGraph
   request: RequestContext
   build: PathBuildState
   payload: object


@dataclass(frozen=True)
class ProgressivePrefillMessage:
   header: HopHeader
   graph: ExecutionGraph
   request: RequestContext
   ordered_hops: tuple[PathHop, ...]
   excluded_placements: frozenset[str] = frozenset()
   excluded_edges: frozenset[str] = frozenset()
   excluded_devices: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ManifestLocked:
   request_id: str
   path_id: str
   path_attempt: int
   manifest: PathManifest
   build: PathBuildState


@dataclass(frozen=True)
class ProgressivePrefillResult:
   disposition: str
   reason: str = ""
   retry_after_seconds: float = 0.0
   forwarded_header: HopHeader | None = None
   context: ProgressivePrefillContext | None = None
   confirmation: ManifestLocked | None = None
   failure_report: FailureReport | None = None
