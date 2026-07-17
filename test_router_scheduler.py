import unittest

from mycelium_router.contracts import HopWorkItem, RouterConfig
from mycelium_router.scheduler import DuplicateHopError, HopScheduler


def work(
   request_id,
   *,
   qos="batch",
   enqueued_at=0.0,
   deficit=0.0,
   placement="node-a-stage-000",
   token_index=0,
):
   return HopWorkItem(
      request_id=request_id,
      path_id=f"path-{request_id}",
      path_attempt=0,
      phase="DECODE",
      token_index=token_index,
      hop_index=0,
      placement_id=placement,
      qos_class=qos,
      deficit_ratio=deficit,
      enqueued_at=enqueued_at,
      idempotency_key=f"{request_id}:0:{token_index}:0",
      payload=None,
   )


class HopSchedulerTests(unittest.TestCase):
   def setUp(self):
      self.config = RouterConfig(
         interactive_base_priority=100.0,
         batch_base_priority=10.0,
         aging_priority_per_second=1.0,
         maximum_deficit_boost=50.0,
      )
      self.scheduler = HopScheduler(self.config)

   def test_interactive_wins_at_equal_age(self):
      self.scheduler.enqueue(work("batch", qos="batch"))
      self.scheduler.enqueue(work("interactive", qos="interactive"))
      self.assertEqual(self.scheduler.pop_next(now=0.0).request_id, "interactive")

   def test_deficit_boost_is_capped(self):
      normal = work("normal", qos="batch", deficit=0.0)
      extreme = work("extreme", qos="batch", deficit=1_000_000.0)
      difference = self.scheduler.effective_priority(
         extreme, now=0.0
      ) - self.scheduler.effective_priority(normal, now=0.0)
      self.assertEqual(difference, self.config.maximum_deficit_boost)

   def test_aging_eventually_prevents_batch_starvation(self):
      self.scheduler.enqueue(work("old-batch", qos="batch", enqueued_at=0.0))
      self.scheduler.enqueue(
         work("new-interactive", qos="interactive", enqueued_at=100.0)
      )
      self.assertEqual(self.scheduler.pop_next(now=100.0).request_id, "old-batch")

   def test_duplicate_idempotency_key_is_rejected(self):
      item = work("request")
      self.scheduler.enqueue(item)
      with self.assertRaises(DuplicateHopError):
         self.scheduler.enqueue(item)
      self.assertEqual(self.scheduler.queue_depth(), 1)

   def test_one_queue_tracks_multiple_local_placements(self):
      self.scheduler.enqueue(work("a", placement="placement-a"))
      self.scheduler.enqueue(work("b", placement="placement-b"))
      self.assertEqual(
         self.scheduler.placement_depths(),
         {"placement-a": 1, "placement-b": 1},
      )
      self.scheduler.pop_next(now=0.0)
      self.assertEqual(sum(self.scheduler.placement_depths().values()), 1)

   def test_ties_are_deterministic_by_request_identity(self):
      self.scheduler.enqueue(work("request-z"))
      self.scheduler.enqueue(work("request-a"))
      self.assertEqual(self.scheduler.pop_next(now=0.0).request_id, "request-a")


if __name__ == "__main__":
   unittest.main()
