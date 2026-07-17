import math
import unittest

from mycelium_router.contracts import (
   DeviceState,
   RequestContext,
   RouterConfig,
)
from mycelium_router.fakes import FakeCapacityPort, SequenceIdSource
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy, RoutingError
from mycelium_router.scoring import RouteScorer
from test_router_contracts import graph_fixture


def request_fixture(**overrides):
   values = {
      "request_id": "request-1",
      "prompt_token_ids": tuple(range(100)),
      "max_new_tokens": 20,
      "expected_new_tokens": 10,
      "qos_class": "interactive",
      "admitted_at": 0.0,
      "target_ttft_ms": 1_000.0,
      "target_tpot_ms": 100.0,
      "target_tokens_per_second": 10.0,
      "sampling_seed": 7,
      "generation_config_digest": "sha256:generation-config",
   }
   values.update(overrides)
   return RequestContext(**values)


def state(
   node_id,
   *,
   free=1.0,
   updated=0.0,
   available_kv=1_000_000,
   rtt=None,
   bandwidth=None,
   queue=0,
):
   return DeviceState(
      node_id=node_id,
      state_seq=1,
      last_updated=updated,
      availability="ALIVE",
      compute_units_per_second=1_000.0,
      free_compute_fraction=free,
      available_kv_bytes=available_kv,
      pending_hop_queue_depth=queue,
      neighbor_rtt_ms=dict(rtt or {}),
      neighbor_bandwidth_bytes_per_second=dict(bandwidth or {}),
   )


def state_table(*, slow_b_bandwidth=True, stale_c=False):
   return {
      "node-a": state(
         "node-a",
         rtt={"node-b": 2.0, "node-c": 20.0, "node-a": 0.0},
         bandwidth={
            "node-b": 100_000.0 if slow_b_bandwidth else 1_000_000_000.0,
            "node-c": 1_000_000_000.0,
            "node-a": 1_000_000_000.0,
         },
      ),
      "node-b": state(
         "node-b",
         rtt={"node-a": 2.0},
         bandwidth={"node-a": 100_000_000.0},
      ),
      "node-c": state(
         "node-c",
         updated=-100.0 if stale_c else 0.0,
         free=1.0,
         rtt={"node-a": 20.0},
         bandwidth={"node-a": 1_000_000_000.0},
      ),
   }


class RouteScoringTests(unittest.TestCase):
   def setUp(self):
      self.graph = graph_fixture()
      self.request = request_fixture()
      self.config = RouterConfig(
         interactive_alpha=0.35,
         batch_alpha=0.10,
         stale_after_seconds=10.0,
         confidence_half_life_seconds=10.0,
         minimum_confidence=0.25,
         conservative_compute_fraction=0.10,
         conservative_queue_depth=20,
         default_bandwidth_bytes_per_second=1_000_000.0,
      )
      self.scorer = RouteScorer(self.config)

   def test_prefill_serialization_can_reverse_latency_only_choice(self):
      states = state_table(slow_b_bandwidth=True)
      via_b = self.scorer.score_route(
         self.request,
         self.graph,
         ("node-a-stage-000", "node-b-stage-001", "node-a-stage-002"),
         states,
         now=0.0,
      )
      via_c = self.scorer.score_route(
         self.request,
         self.graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         states,
         now=0.0,
      )
      self.assertLess(via_c.total_score, via_b.total_score)
      self.assertGreater(via_b.prefill_transfer_ms, via_c.prefill_transfer_ms)

   def test_score_uses_sla_normalized_terms(self):
      score = self.scorer.score_route(
         self.request,
         self.graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         state_table(),
         now=0.0,
      )
      expected = 0.35 * (score.ttft_ms / self.request.target_ttft_ms) + 0.65 * (
         score.tpot_ms / self.request.target_tpot_ms
      )
      self.assertAlmostEqual(score.total_score, expected)

   def test_stale_optimistic_state_uses_conservative_fallback(self):
      score = self.scorer.score_route(
         self.request,
         self.graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         state_table(stale_c=True),
         now=0.0,
      )
      self.assertIn("node-c", score.fallback_nodes)
      self.assertLess(score.confidence, self.config.minimum_confidence)

   def test_dead_device_makes_route_infeasible(self):
      states = state_table()
      states["node-c"] = states["node-c"].with_availability("DEAD")
      score = self.scorer.score_route(
         self.request,
         self.graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         states,
         now=0.0,
      )
      self.assertTrue(math.isinf(score.total_score))


class ProgressiveRouteTests(unittest.TestCase):
   def setUp(self):
      self.graph = graph_fixture()
      self.request = request_fixture()
      self.capacity = FakeCapacityPort()
      self.policy = RoutePolicy(RouteScorer(RouterConfig()))
      self.builder = ProgressivePathBuilder(
         policy=self.policy,
         capacity=self.capacity,
         id_source=SequenceIdSource(),
      )

   def test_progressive_build_selects_one_placement_per_stage_and_locks(self):
      build = self.builder.start(
         self.request,
         self.graph,
         path_attempt=0,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      while not self.builder.is_complete(build):
         build = self.builder.advance(build, state_table(), now=0.0)
      manifest = self.builder.lock(build)
      self.assertEqual(
         tuple(hop.stage_id for hop in manifest.ordered_hops),
         ("stage-000", "stage-001", "stage-002"),
      )
      self.assertEqual(manifest.ordered_hops[1].placement_id, "node-c-stage-001")
      self.assertEqual(len(self.capacity.committed_ids), 3)

   def test_reservation_rejection_excludes_candidate_and_rescores(self):
      self.capacity.reject_placements.add("node-c-stage-001")
      build = self.builder.start(
         self.request,
         self.graph,
         path_attempt=0,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      while not self.builder.is_complete(build):
         build = self.builder.advance(build, state_table(), now=0.0)
      manifest = self.builder.lock(build)
      self.assertEqual(manifest.ordered_hops[1].placement_id, "node-b-stage-001")
      self.assertIn("node-c-stage-001", build.excluded_placements)

   def test_kv_reservation_uses_max_context_not_expected_output(self):
      build = self.builder.start(
         self.request,
         self.graph,
         path_attempt=0,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      build = self.builder.advance(build, state_table(), now=0.0)
      expected = (100 + 20) * 32
      self.assertEqual(self.capacity.requests[0].kv_bytes, expected)

   def test_no_legal_suffix_raises_typed_routing_error(self):
      build = self.builder.start(
         self.request,
         self.graph,
         path_attempt=0,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      build = build.with_excluded_devices(frozenset({"node-a", "node-b", "node-c"}))
      with self.assertRaisesRegex(RoutingError, "no_feasible_route"):
         self.builder.advance(build, state_table(), now=0.0)


if __name__ == "__main__":
   unittest.main()
