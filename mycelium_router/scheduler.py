"""Device-wide fair scheduler for local inter-layer work."""

from collections import Counter

from mycelium_router.contracts import (
   BatchDecision,
   HopWorkItem,
   RouterConfig,
   RuntimeBatch,
)


class DuplicateHopError(ValueError):
   pass


class BackpressureError(RuntimeError):
   def __init__(self, reason: str, retry_after_seconds: float):
      self.reason = reason
      self.retry_after_seconds = retry_after_seconds
      super().__init__(f"backpressure:{reason}")


class HopScheduler:
   def __init__(self, config: RouterConfig):
      self.config = config
      self._queue: list[HopWorkItem] = []
      self._queued_payload_bytes = 0
      self._seen_keys: dict[str, tuple[float, str]] = {}

   def enqueue(self, item: HopWorkItem) -> None:
      self._evict_seen(now=item.enqueued_at)
      if item.idempotency_key in self._seen_keys:
         raise DuplicateHopError(item.idempotency_key)
      retry_after = max(0.001, self.config.backpressure_retry_after_seconds)
      if len(self._queue) >= max(0, self.config.maximum_pending_hops):
         raise BackpressureError("work_limit", retry_after)
      payload_bytes = self._payload_size(item.payload)
      if (
         self._queued_payload_bytes + payload_bytes
         > max(0, self.config.maximum_pending_bytes)
      ):
         raise BackpressureError("byte_limit", retry_after)
      self._seen_keys[item.idempotency_key] = (
         item.enqueued_at,
         item.path_id,
      )
      self._queue.append(item)
      self._queued_payload_bytes += payload_bytes
      self._evict_seen(now=item.enqueued_at)

   def effective_priority(self, item: HopWorkItem, *, now: float) -> float:
      base = (
         self.config.batch_base_priority
         if item.qos_class == "batch"
         else self.config.interactive_base_priority
      )
      age = max(0.0, now - item.enqueued_at)
      aging_multiplier = (
         self.config.batch_aging_multiplier
         if item.qos_class == "batch"
         else 1.0
      )
      aging = age * self.config.aging_priority_per_second * aging_multiplier
      deficit = max(0.0, min(1.0, item.deficit_ratio))
      deficit_boost = deficit * self.config.maximum_deficit_boost
      return base + aging + deficit_boost

   def pop_next(self, *, now: float) -> HopWorkItem:
      return self.pop_batch(now=now, maximum_items=1).items[0]

   def batch_candidates(self, *, now: float) -> tuple[HopWorkItem, ...]:
      """Return priority-ordered work compatible with the next anchor."""
      if not self._queue:
         return ()
      anchor_index = min(
         range(len(self._queue)),
         key=lambda offset: self._priority_key(offset, now=now),
      )
      anchor = self._queue[anchor_index]
      if anchor.batch_key is None:
         return (anchor,)
      compatible_indices = sorted(
         (
            offset
            for offset, item in enumerate(self._queue)
            if item.batch_key == anchor.batch_key
         ),
         key=lambda offset: self._priority_key(offset, now=now),
      )
      return tuple(self._queue[offset] for offset in compatible_indices)

   def pop_batch(
      self,
      *,
      now: float,
      maximum_items: int | None = None,
      maximum_bytes: int | None = None,
      decision: BatchDecision | None = None,
   ) -> RuntimeBatch:
      if not self._queue:
         raise IndexError("empty_hop_queue")
      maximum = max(
         1,
         (
            self.config.maximum_runtime_batch_size
            if maximum_items is None
            else maximum_items
         ),
      )
      anchor_index = min(
         range(len(self._queue)),
         key=lambda offset: self._priority_key(offset, now=now),
      )
      anchor = self._queue[anchor_index]
      if anchor.batch_key is None:
         selected_indices = [anchor_index]
      else:
         compatible_indices = [
            offset
            for offset, item in enumerate(self._queue)
            if item.batch_key == anchor.batch_key
         ]
         selected_indices = sorted(
            compatible_indices,
            key=lambda offset: self._priority_key(offset, now=now),
         )[:maximum]
      byte_limit = None if maximum_bytes is None else max(1, maximum_bytes)
      if byte_limit is not None:
         bounded_indices = []
         bounded_bytes = 0
         for offset in selected_indices:
            payload_bytes = self._payload_size(self._queue[offset].payload)
            if bounded_indices and bounded_bytes + payload_bytes > byte_limit:
               break
            bounded_indices.append(offset)
            bounded_bytes += payload_bytes
         selected_indices = bounded_indices
      items = tuple(self._queue[offset] for offset in selected_indices)
      for offset in sorted(selected_indices, reverse=True):
         item = self._queue.pop(offset)
         self._queued_payload_bytes -= self._payload_size(item.payload)
      return RuntimeBatch(
         compatibility_key=anchor.batch_key,
         items=items,
         decision=decision,
      )

   def _priority_key(self, offset: int, *, now: float) -> tuple:
      item = self._queue[offset]
      return (
         -self.effective_priority(item, now=now),
         item.request_id,
         item.path_id,
         item.token_index,
         item.hop_index,
         offset,
      )

   def release_path(self, path_id: str) -> None:
      self._queue = [item for item in self._queue if item.path_id != path_id]
      self._queued_payload_bytes = sum(
         self._payload_size(item.payload) for item in self._queue
      )
      self._seen_keys = {
         key: value
         for key, value in self._seen_keys.items()
         if value[1] != path_id
      }

   def queue_depth(self) -> int:
      return len(self._queue)

   def placement_depths(self) -> dict[str, int]:
      return dict(Counter(item.placement_id for item in self._queue))

   def queued_payload_bytes(self) -> int:
      return self._queued_payload_bytes

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
      raise TypeError("unsupported_hop_payload_for_byte_accounting")

   def seen_key_count(self) -> int:
      return len(self._seen_keys)

   def _evict_seen(self, *, now: float) -> None:
      active_keys = {item.idempotency_key for item in self._queue}
      retention = max(0.0, self.config.idempotency_retention_seconds)
      expired = [
         key
         for key, (seen_at, _) in self._seen_keys.items()
         if key not in active_keys and now - seen_at > retention
      ]
      for key in expired:
         self._seen_keys.pop(key, None)

      maximum = max(1, self.config.maximum_idempotency_entries)
      inactive = sorted(
         (
            (seen_at, key)
            for key, (seen_at, _) in self._seen_keys.items()
            if key not in active_keys
         )
      )
      while len(self._seen_keys) > maximum and inactive:
         _, key = inactive.pop(0)
         self._seen_keys.pop(key, None)
