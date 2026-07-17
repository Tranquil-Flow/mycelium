import unittest
from dataclasses import replace

from mycelium_router.contracts import HopHeader, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy
from mycelium_router.scoring import RouteScorer
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


def locked_path():
   graph = graph_fixture()
   request = request_fixture()
   builder = ProgressivePathBuilder(
      policy=RoutePolicy(RouteScorer(RouterConfig())),
      capacity=FakeCapacityPort(),
      id_source=SequenceIdSource(),
   )
   build = builder.start(request, graph, path_attempt=0)
   while not builder.is_complete(build):
      build = builder.advance(build, state_table(), now=0.0)
   return graph, request, builder.lock(build, now=0.0)


def first_header(request, manifest, *, phase="DECODE", token_index=0):
   first = manifest.ordered_hops[0]
   return HopHeader(
      request_id=request.request_id,
      path_id=manifest.path_id,
      path_attempt=manifest.path_attempt,
      phase=phase,
      token_index=token_index,
      hop_index=0,
      source_placement_id="",
      destination_placement_id=first.placement_id,
      topology_version=manifest.topology_version,
      idempotency_key=hop_idempotency_key(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase=phase,
         token_index=token_index,
         hop_index=0,
      ),
   )


class ReceiveHopTests(unittest.TestCase):
   def setUp(self):
      self.graph, self.request, self.manifest = locked_path()
      placement_map = {
         placement.placement_id: placement
         for stage in self.graph.stages
         for placement in stage.placements
      }
      self.routers = []
      self.runtimes = []
      self.transports = []
      for hop in self.manifest.ordered_hops:
         runtime = FakeRuntimePort()
         transport = FakeTransportPort()
         router = Router(
            node_id=placement_map[hop.placement_id].node_id,
            topology=FakeTopologyProvider(self.graph),
            device_states=FakeDeviceStateProvider(state_table()),
            capacity=FakeCapacityPort(),
            runtime=runtime,
            transport=transport,
            clock=ManualClock(),
            id_source=SequenceIdSource(),
            config=RouterConfig(),
         )
         self.assertTrue(
            router.register_path(self.request, self.manifest, self.graph)
         )
         self.routers.append(router)
         self.runtimes.append(runtime)
         self.transports.append(transport)

   def test_each_router_executes_only_its_local_hop_then_forwards(self):
      header = first_header(self.request, self.manifest)
      payload = self.request.prompt_token_ids

      for index, router in enumerate(self.routers):
         result = router.receive_hop(header, payload)
         self.assertEqual(len(self.runtimes[index].executed), 1)
         self.assertEqual(
            self.runtimes[index].executed[0].placement_id,
            self.manifest.ordered_hops[index].placement_id,
         )
         batch_key = self.runtimes[index].executed[0].batch_key
         self.assertIsNotNone(batch_key)
         self.assertEqual(batch_key.deployment_id, self.graph.deployment_id)
         self.assertEqual(batch_key.deployment_epoch, self.graph.deployment_epoch)
         self.assertEqual(batch_key.model_commit, self.graph.resolved_commit)
         self.assertEqual(batch_key.manifest_digest, self.graph.manifest_digest)
         self.assertEqual(batch_key.phase, "DECODE")
         self.assertEqual(batch_key.token_span, 1)
         for other in range(index + 1, len(self.runtimes)):
            self.assertFalse(self.runtimes[other].executed)

         if index + 1 < len(self.routers):
            self.assertEqual(result.disposition, "FORWARDED")
            self.assertEqual(len(self.transports[index].hops), 1)
            header, payload = self.transports[index].hops[0]
         else:
            self.assertEqual(result.disposition, "COMPLETED")
            self.assertIsNotNone(result.token_event)
            self.assertEqual(len(self.transports[index].token_events), 1)

   def test_duplicate_hop_returns_cached_result_without_side_effects(self):
      header = first_header(self.request, self.manifest)
      first = self.routers[0].receive_hop(header, (1, 2))
      second = self.routers[0].receive_hop(header, (1, 2))

      self.assertEqual(second, first)
      self.assertEqual(len(self.runtimes[0].executed), 1)
      self.assertEqual(len(self.transports[0].hops), 1)

   def test_misdirected_hop_is_rejected_without_execution(self):
      runtime = FakeRuntimePort()
      router = Router(
         node_id="node-without-selected-placement",
         topology=FakeTopologyProvider(self.graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=runtime,
         transport=FakeTransportPort(),
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      router.register_path(self.request, self.manifest, self.graph)

      result = router.receive_hop(
         first_header(self.request, self.manifest),
         (1, 2),
      )

      self.assertEqual(result.disposition, "REJECTED")
      self.assertEqual(result.reason, "destination_not_local")
      self.assertFalse(runtime.executed)

   def test_stale_attempt_is_rejected_without_execution(self):
      header = first_header(self.request, self.manifest)
      header = replace(
         header,
         path_attempt=header.path_attempt + 1,
         idempotency_key=hop_idempotency_key(
            request_id=header.request_id,
            path_id=header.path_id,
            path_attempt=header.path_attempt + 1,
            phase=header.phase,
            token_index=header.token_index,
            hop_index=header.hop_index,
         ),
      )

      result = self.routers[0].receive_hop(header, (1, 2))

      self.assertEqual(result.disposition, "REJECTED")
      self.assertEqual(result.reason, "stale_path_attempt")
      self.assertFalse(self.runtimes[0].executed)

   def test_runtime_failure_reports_scope_and_does_not_forward(self):
      first = self.manifest.ordered_hops[0]
      self.runtimes[0].fail_once(
         placement_id=first.placement_id,
         phase="DECODE",
         token_index=0,
         scope="PLACEMENT",
      )

      result = self.routers[0].receive_hop(
         first_header(self.request, self.manifest),
         (1, 2),
      )

      self.assertEqual(result.disposition, "FAILED")
      self.assertIsNotNone(result.failure_report)
      self.assertFalse(self.transports[0].hops)
      self.assertEqual(len(self.transports[0].failure_reports), 1)


if __name__ == "__main__":
   unittest.main()
