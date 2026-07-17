"""Phase-aware, latency-first runtime microbatch policy.

The controller is transport-neutral.  It consumes recent directed-link and runtime
observations, but never activation content.  Callers own timers: a WAIT decision
includes ``ready_at`` so an event loop can schedule the next drain without a
controller thread.
"""

from math import ceil, isfinite

from mycelium_router.contracts import (
   BatchDecision,
   BatchExecutionObservation,
   BatchNetworkStats,
   HopWorkItem,
   RouterConfig,
)


_PREFILL_PHASES = {"PREFILL", "PREFILL_CHUNK"}


class PhaseAwareBatchController:
   def __init__(self, config: RouterConfig):
      self.config = config
      self._network: dict[str, BatchNetworkStats] = {}
      self._execution_ms: dict[tuple[str, int], float] = {}
      self._decisions: list[BatchDecision] = []

   def update_network_stats(
      self,
      placement_id: str,
      stats: BatchNetworkStats,
   ) -> None:
      if not placement_id:
         raise ValueError("missing_batch_stats_placement")
      if stats.goodput_bytes_per_second <= 0:
         raise ValueError("invalid_batch_goodput")
      if stats.one_way_p95_ms < 0 or stats.receiver_queue_ms < 0:
         raise ValueError("invalid_batch_latency")
      if stats.loss_rate < 0 or stats.loss_rate > 1:
         raise ValueError("invalid_batch_loss_rate")
      self._network[placement_id] = stats

   def record_execution(self, observation: BatchExecutionObservation) -> None:
      if not observation.successful:
         return
      if observation.batch_size <= 0 or observation.execution_ms < 0:
         raise ValueError("invalid_batch_execution_observation")
      key = (observation.phase, observation.batch_size)
      previous = self._execution_ms.get(key)
      alpha = max(0.0, min(1.0, self.config.batch_observation_ewma_alpha))
      self._execution_ms[key] = (
         observation.execution_ms
         if previous is None
         else alpha * observation.execution_ms + (1.0 - alpha) * previous
      )

   def decide(
      self,
      candidates: tuple[HopWorkItem, ...],
      *,
      now: float,
      force: bool = False,
   ) -> BatchDecision:
      if not candidates:
         raise ValueError("empty_batch_candidates")
      anchor = candidates[0]
      phase = anchor.phase
      byte_limited = self._byte_limited_count(candidates)
      phase_limit = self._phase_limit(phase)
      available = min(len(candidates), byte_limited, phase_limit)
      available = max(1, available)
      target, target_reason = self._target_items(
         anchor,
         available_capacity=min(self._byte_capacity(anchor), phase_limit),
         now=now,
      )
      batch_size = min(available, target)
      predicted_payload = sum(
         self._payload_size(item.payload) for item in candidates[:batch_size]
      )
      predicted_transfer = self._predicted_transfer_ms(
         anchor,
         predicted_payload,
         now=now,
      )
      ready_at = anchor.enqueued_at
      reason = target_reason
      action = "DISPATCH"

      if phase == "DECODE":
         batch_size = available
         predicted_payload = sum(
            self._payload_size(item.payload) for item in candidates[:batch_size]
         )
         predicted_transfer = self._predicted_transfer_ms(
            anchor,
            predicted_payload,
            now=now,
         )
         reason = (
            "decode_singleton_immediate"
            if batch_size == 1
            else "decode_ready_batch"
         )
      elif phase in _PREFILL_PHASES:
         ready_at = anchor.enqueued_at + max(
            0.0,
            self.config.prefill_collection_window_seconds,
         )
         earliest_deadline = min(item.deadline_at for item in candidates)
         deadline_guard = max(0.0, self.config.batch_deadline_guard_seconds)
         deadline_cost = predicted_transfer / 1_000.0 + deadline_guard
         ready_at = min(ready_at, earliest_deadline - deadline_cost)
         if force:
            reason = "forced_prefill_flush"
         elif anchor.qos_class != "batch":
            reason = "interactive_prefill_immediate"
         elif earliest_deadline <= now + deadline_cost:
            reason = "earliest_deadline"
         elif available >= target:
            reason = target_reason
         elif now >= ready_at:
            batch_size = available
            predicted_payload = sum(
               self._payload_size(item.payload)
               for item in candidates[:batch_size]
            )
            predicted_transfer = self._predicted_transfer_ms(
               anchor,
               predicted_payload,
               now=now,
            )
            reason = "prefill_collection_expired"
         else:
            action = "WAIT"
            batch_size = 0
            predicted_payload = 0
            reason = "collecting_prefill"
      else:
         batch_size = 1
         predicted_payload = self._payload_size(anchor.payload)
         predicted_transfer = self._predicted_transfer_ms(
            anchor,
            predicted_payload,
            now=now,
         )
         reason = "unbatchable_phase"

      decision = BatchDecision(
         action=action,
         phase=phase,
         batch_size=batch_size,
         target_items=target,
         available_items=len(candidates),
         predicted_payload_bytes=predicted_payload,
         predicted_transfer_ms=predicted_transfer,
         ready_at=ready_at,
         reason=reason,
      )
      self._remember(decision)
      return decision

   def decisions(self) -> tuple[BatchDecision, ...]:
      return tuple(self._decisions)

   def execution_profiles(self) -> dict[tuple[str, int], float]:
      return dict(self._execution_ms)

   def network_stats(self) -> dict[str, BatchNetworkStats]:
      return dict(self._network)

   def _target_items(
      self,
      anchor: HopWorkItem,
      *,
      available_capacity: int,
      now: float,
   ) -> tuple[int, str]:
      capacity = max(1, available_capacity)
      profiled = self._profiled_target(anchor, capacity=capacity, now=now)
      if profiled is not None:
         return profiled, "profiled_prefill_target"
      if anchor.phase not in _PREFILL_PHASES:
         return min(capacity, self._phase_limit(anchor.phase)), "phase_target_ready"
      item_bytes = max(1, self._payload_size(anchor.payload))
      stats = self._fresh_stats(anchor.placement_id, now=now)
      if stats is None:
         return min(capacity, self._phase_limit(anchor.phase)), "prefill_target_ready"
      bdp_bytes = (
         stats.goodput_bytes_per_second
         * max(0.0, stats.one_way_p95_ms)
         / 1_000.0
      )
      loss_factor = max(0.25, 1.0 - min(0.75, stats.loss_rate * 10.0))
      target_bytes = (
         bdp_bytes
         * max(0.25, self.config.batch_bdp_multiplier)
         * loss_factor
      )
      count = max(1, ceil(target_bytes / item_bytes))
      return min(capacity, count), "prefill_target_ready"

   def _profiled_target(
      self,
      anchor: HopWorkItem,
      *,
      capacity: int,
      now: float,
   ) -> int | None:
      choices = []
      item_bytes = max(1, self._payload_size(anchor.payload))
      for (phase, size), runtime_ms in self._execution_ms.items():
         if phase != anchor.phase or size > capacity:
            continue
         transfer_ms = self._predicted_transfer_ms(
            anchor,
            item_bytes * size,
            now=now,
         )
         choices.append((runtime_ms + transfer_ms, size))
      if len(choices) < 2:
         return None
      return min(choices)[1]

   def _phase_limit(self, phase: str) -> int:
      common = max(1, self.config.maximum_runtime_batch_size)
      if phase == "DECODE":
         return min(common, max(1, self.config.decode_runtime_batch_size))
      if phase in _PREFILL_PHASES:
         return min(common, max(1, self.config.prefill_runtime_batch_size))
      return 1

   def _byte_capacity(self, anchor: HopWorkItem) -> int:
      maximum = max(1, self.config.maximum_runtime_batch_bytes)
      item_bytes = max(1, self._payload_size(anchor.payload))
      return max(1, maximum // item_bytes)

   def _byte_limited_count(self, candidates: tuple[HopWorkItem, ...]) -> int:
      maximum = max(1, self.config.maximum_runtime_batch_bytes)
      count = 0
      total = 0
      for item in candidates:
         size = self._payload_size(item.payload)
         if count and total + size > maximum:
            break
         count += 1
         total += size
      return max(1, count)

   def _predicted_transfer_ms(
      self,
      anchor: HopWorkItem,
      payload_bytes: int,
      *,
      now: float,
   ) -> float:
      stats = self._fresh_stats(anchor.placement_id, now=now)
      if stats is None:
         one_way_ms = max(0.0, self.config.default_rtt_ms / 2.0)
         goodput = max(1.0, self.config.default_bandwidth_bytes_per_second)
         loss_rate = 0.0
         receiver_queue_ms = 0.0
      else:
         one_way_ms = stats.one_way_p95_ms
         goodput = max(1.0, stats.goodput_bytes_per_second)
         loss_rate = stats.loss_rate
         receiver_queue_ms = stats.receiver_queue_ms
      serialization_ms = payload_bytes / goodput * 1_000.0
      loss_tail_ms = one_way_ms * min(2.0, max(0.0, loss_rate) * 10.0)
      value = one_way_ms + serialization_ms + loss_tail_ms + receiver_queue_ms
      return value if isfinite(value) else float("inf")

   def _fresh_stats(
      self,
      placement_id: str,
      *,
      now: float,
   ) -> BatchNetworkStats | None:
      stats = self._network.get(placement_id)
      if stats is None:
         return None
      if now - stats.observed_at > max(0.0, self.config.batch_stats_stale_seconds):
         return None
      return stats

   def _remember(self, decision: BatchDecision) -> None:
      self._decisions.append(decision)
      maximum = max(1, self.config.maximum_batch_decision_history)
      if len(self._decisions) > maximum:
         del self._decisions[: len(self._decisions) - maximum]

   @staticmethod
   def _payload_size(payload: object) -> int:
      if payload is None:
         return 0
      if isinstance(payload, (bytes, bytearray, memoryview)):
         return len(payload)
      if isinstance(payload, (tuple, list)) and all(
         isinstance(item, int) and not isinstance(item, bool) for item in payload
      ):
         return len(payload) * 4
      raise TypeError("unsupported_hop_payload_for_batching")
