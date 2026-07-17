import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   HopWorkItem,
   RouterConfig,
   RuntimeBatch,
   RuntimeBatchKey,
)
from mycelium_router.fakes import FakeRuntimePort
from mycelium_router.scheduler import HopScheduler


def batch_key(**overrides) -> RuntimeBatchKey:
   values = {
      "deployment_id": "deployment-1",
      "deployment_epoch": 7,
      "model_commit": "commit-abc",
      "manifest_digest": "manifest-abc",
      "placement_id": "placement-a",
      "assignment_id": "assignment-a",
      "stage_signature": "stage-signature-a",
      "load_proof_digest": "load-proof-a",
      "runtime_backend": "mlx",
      "phase": "DECODE",
      "hidden_size": 4096,
      "activation_bytes": 2,
      "token_span": 1,
      "speculative_role": "NONE",
      "speculative_width": 0,
   }
   values.update(overrides)
   return RuntimeBatchKey(**values)


def work(
   request_id: str,
   *,
   key: RuntimeBatchKey | None,
   qos: str = "batch",
   enqueued_at: float = 0.0,
   token_index: int = 0,
   payload: bytes = b"1234",
) -> HopWorkItem:
   return HopWorkItem(
      request_id=request_id,
      path_id=f"path-{request_id}",
      path_attempt=0,
      phase=key.phase if key is not None else "DECODE",
      token_index=token_index,
      hop_index=0,
      placement_id=key.placement_id if key is not None else "placement-a",
      qos_class=qos,
      deficit_ratio=0.0,
      enqueued_at=enqueued_at,
      idempotency_key=f"{request_id}:0:{token_index}:0",
      payload=payload,
      batch_key=key,
   )


class RuntimeBatchSchedulerTests(unittest.TestCase):
   def scheduler(self, maximum: int = 2) -> HopScheduler:
      return HopScheduler(
         RouterConfig(
            maximum_runtime_batch_size=maximum,
            maximum_pending_hops=20,
            maximum_pending_bytes=1_000,
         )
      )

   def test_compatible_decode_items_form_deterministic_bounded_batch(self):
      scheduler = self.scheduler(maximum=2)
      key = batch_key()
      for request_id in ("request-c", "request-a", "request-b"):
         scheduler.enqueue(work(request_id, key=key))

      batch = scheduler.pop_batch(now=0.0)

      self.assertEqual(
         [item.request_id for item in batch.items],
         ["request-a", "request-b"],
      )
      self.assertEqual(batch.compatibility_key, key)
      self.assertEqual(scheduler.queue_depth(), 1)
      self.assertEqual(scheduler.queued_payload_bytes(), 4)

   def test_highest_priority_item_anchors_batch_before_compatible_fill(self):
      scheduler = self.scheduler(maximum=4)
      low_key = batch_key(placement_id="placement-low")
      high_key = batch_key(placement_id="placement-high")
      scheduler.enqueue(work("batch-a", key=low_key))
      scheduler.enqueue(work("batch-b", key=low_key))
      scheduler.enqueue(work("interactive", key=high_key, qos="interactive"))

      batch = scheduler.pop_batch(now=0.0)

      self.assertEqual([item.request_id for item in batch.items], ["interactive"])
      self.assertEqual(batch.compatibility_key, high_key)
      self.assertEqual(scheduler.queue_depth(), 2)

   def test_phase_identity_and_prefill_shape_mismatches_do_not_coalesce(self):
      scheduler = self.scheduler(maximum=8)
      anchor = batch_key(phase="PREFILL_CHUNK", token_span=16)
      incompatible = (
         replace(anchor, phase="DECODE", token_span=1),
         replace(anchor, token_span=8),
         replace(anchor, deployment_epoch=8),
         replace(anchor, stage_signature="other-stage"),
         replace(anchor, speculative_role="TARGET_VERIFY", speculative_width=4),
      )
      scheduler.enqueue(work("anchor", key=anchor))
      for index, key in enumerate(incompatible):
         scheduler.enqueue(work(f"other-{index}", key=key))

      batch = scheduler.pop_batch(now=0.0)

      self.assertEqual([item.request_id for item in batch.items], ["anchor"])
      self.assertEqual(scheduler.queue_depth(), len(incompatible))

   def test_missing_compatibility_identity_fails_closed_to_singleton(self):
      scheduler = self.scheduler(maximum=8)
      scheduler.enqueue(work("request-a", key=None))
      scheduler.enqueue(work("request-b", key=None))

      batch = scheduler.pop_batch(now=0.0)

      self.assertEqual([item.request_id for item in batch.items], ["request-a"])
      self.assertIsNone(batch.compatibility_key)
      self.assertEqual(scheduler.queue_depth(), 1)

   def test_runtime_batch_rejects_mixed_items(self):
      first_key = batch_key()
      second_key = replace(first_key, activation_bytes=4)

      with self.assertRaisesRegex(ValueError, "incompatible_runtime_batch"):
         RuntimeBatch(
            compatibility_key=first_key,
            items=(
               work("request-a", key=first_key),
               work("request-b", key=second_key),
            ),
         )

   def test_fake_runtime_returns_one_isolated_result_per_batch_member(self):
      key = batch_key()
      batch = RuntimeBatch(
         compatibility_key=key,
         items=(
            work("request-a", key=key, token_index=3),
            work("request-b", key=key, token_index=9),
         ),
      )
      runtime = FakeRuntimePort(token_base=100)

      results = runtime.execute_batch(batch)

      self.assertEqual([result.token_id for result in results], [104, 110])
      self.assertEqual(runtime.executed_batches, [batch])
      self.assertEqual(runtime.executed, list(batch.items))


if __name__ == "__main__":
   unittest.main()
