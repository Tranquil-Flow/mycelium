from dataclasses import replace
import unittest

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class StageLocalFakeRuntime(FakeRuntimePort):
   decode_mode = "stage_local_kv"

   def execute(self, item):
      result = super().execute(item)
      if item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
         return replace(result, token_id=self.token_base)
      return result


class RelayIdempotencyTests(unittest.TestCase):
   def build_router(self, runtime=None):
      runtime = runtime or FakeRuntimePort()
      transport = FakeTransportPort()
      router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(graph_fixture()),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=runtime,
         transport=transport,
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      request = request_fixture()
      router.admit(
         request,
         InMemoryClientSink(),
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      return router, runtime, transport, router.get_request(request.request_id)

   def test_duplicate_manifest_execution_returns_cached_outcome(self):
      router, runtime, transport, record = self.build_router()
      kwargs = {
         "graph": record.graph,
         "manifest": record.manifest,
         "request": record.request,
         "phase": "DECODE",
         "token_index": 0,
         "payload": record.request.prompt_token_ids,
      }
      before = len(runtime.executed)
      first = router.relay.execute_manifest(**kwargs)
      after_first = len(runtime.executed)
      token_events_after_first = len(transport.token_events)
      second = router.relay.execute_manifest(**kwargs)

      self.assertEqual(first, second)
      self.assertEqual(after_first - before, 3)
      self.assertEqual(len(runtime.executed), after_first)
      self.assertEqual(len(transport.token_events), token_events_after_first)

   def test_stage_local_duplicate_manifest_execution_returns_cached_outcome(self):
      router, runtime, transport, record = self.build_router(
         StageLocalFakeRuntime()
      )
      kwargs = {
         "graph": record.graph,
         "manifest": record.manifest,
         "request": record.request,
         "phase": "DECODE",
         "token_index": 0,
         "payload": record.request.prompt_token_ids,
      }
      first = router.relay.execute_manifest(**kwargs)
      before = (
         len(runtime.executed),
         len(transport.hops),
         len(transport.token_events),
      )
      second = router.relay.execute_manifest(**kwargs)

      self.assertEqual(first, second)
      self.assertEqual(
         (
            len(runtime.executed),
            len(transport.hops),
            len(transport.token_events),
         ),
         before,
      )

   def test_conflicting_manifest_payload_rejects_without_side_effects(self):
      router, runtime, transport, record = self.build_router()
      kwargs = {
         "graph": record.graph,
         "manifest": record.manifest,
         "request": record.request,
         "phase": "DECODE",
         "token_index": 7,
      }
      router.relay.execute_manifest(payload=(1, 2), **kwargs)
      before = (
         len(runtime.executed),
         len(transport.hops),
         len(transport.token_events),
      )

      conflict = router.relay.execute_manifest(payload=(1, 3), **kwargs)

      report = conflict.failure_report
      self.assertIsNotNone(report)
      assert report is not None
      self.assertEqual(
         report.reason,
         "idempotency_payload_mismatch",
      )
      self.assertEqual(
         (
            len(runtime.executed),
            len(transport.hops),
            len(transport.token_events),
         ),
         before,
      )

   def test_forwarded_headers_use_same_attempt_scoped_key_schema(self):
      router, _, transport, record = self.build_router()
      transport.hops.clear()

      router.relay.execute_manifest(
         graph=record.graph,
         manifest=record.manifest,
         request=record.request,
         phase="DECODE",
         token_index=3,
         payload=(1, 2),
      )

      self.assertTrue(transport.hops)
      for header, _ in transport.hops:
         self.assertEqual(
            header.idempotency_key,
            (
               f"{header.request_id}:{header.path_id}:"
               f"{header.path_attempt}:{header.phase}:"
               f"{header.token_index}:{header.hop_index}"
            ),
         )


if __name__ == "__main__":
   unittest.main()
