"""PV20: entry/relay/wire seam of the prefix cache (fake runtime, no tensors)."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from mycelium_router.contracts import (
   ManifestLocked,
   PathBuildState,
   RouterConfig,
   RuntimeResult,
)
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
from mycelium_router.prefix_cache import (
   PrefixCacheConfig,
   PrefixPlanner,
   chain_digests,
   route_scope_digest,
)
from mycelium_router.router import Router
from mycelium_router.wire import decode_frame, encode_frame
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class _KVFakeRuntime(FakeRuntimePort):
   """Stage-local-KV flavoured fake: every prefill hop yields a (dummy) first token."""

   decode_mode = "stage_local_kv"

   def execute(self, item):
      result = super().execute(item)
      if item.phase in {"PREFILL", "PREFILL_CHUNK"} and result.success:
         return RuntimeResult(success=True, payload=result.payload, token_id=7)
      return result


class _HoldingProbe:
   """Every placement holds the first ``held`` blocks of whatever chain it is asked about."""

   def __init__(self, held: int) -> None:
      self.held = held
      self.pins: list[tuple[str, str]] = []
      self.settles: list[tuple[str, str, int]] = []

   def prefix_pin(self, placement_id, chain, owner):
      self.pins.append((placement_id, owner))
      return min(self.held, len(chain))

   def prefix_settle(self, placement_id, owner, keep_blocks):
      self.settles.append((placement_id, owner, keep_blocks))


class PrefixEntrySeamTests(unittest.TestCase):
   def setUp(self):
      self.graph = graph_fixture()
      self.runtime = _KVFakeRuntime(token_base=100)
      self.router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(self.graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=self.runtime,
         transport=FakeTransportPort(),
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      self.entry_exclusions = frozenset({"node-b-stage-000"})
      self.config = PrefixCacheConfig(enabled=True, block_tokens=16, max_bytes=1)
      self.probe = _HoldingProbe(held=99)
      self.probes = {
         placement.placement_id: self.probe
         for stage in self.graph.stages
         for placement in stage.placements
      }

   def _planner(self):
      return PrefixPlanner(
         self.config,
         route_scope=route_scope_digest(self.graph),
         probes=self.probes,
      )

   def _admit(self, request):
      return self.router.admit(
         request,
         InMemoryClientSink(),
         excluded_placements=self.entry_exclusions,
      )

   def test_default_is_a_cold_full_prefill_and_reports_zero(self):
      request = request_fixture()
      self._admit(request)
      first = next(item for item in self.runtime.executed if item.phase == "PREFILL")
      self.assertEqual(first.payload, request.prompt_token_ids)
      self.assertEqual(first.position, 0)
      self.assertEqual(first.prefix_reuse_tokens, 0)
      self.assertEqual(first.prefix_chain, ())
      self.assertEqual(self.router.prefix_reused_tokens(request.request_id), 0)

   def test_planned_hit_prefills_only_the_suffix_at_the_offset_on_every_stage(self):
      self.router.prefix_planner = self._planner()
      request = request_fixture()  # 100 tokens, block 16 -> 6 usable blocks -> 96 reused

      self._admit(request)

      prefills = [item for item in self.runtime.executed if item.phase == "PREFILL"]
      self.assertEqual(len(prefills), 3)  # one per stage on the path
      for item in prefills:
         self.assertEqual(item.position, 96)
         self.assertEqual(item.prefix_reuse_tokens, 96)
         self.assertEqual(len(item.prefix_chain), 6)
      self.assertEqual(prefills[0].payload, request.prompt_token_ids[96:])
      self.assertEqual(self.router.prefix_reused_tokens(request.request_id), 96)

   def test_every_stage_was_probed_and_pins_were_trimmed_to_the_agreed_length(self):
      self.router.prefix_planner = self._planner()
      self._admit(request_fixture())
      self.assertEqual(len(self.probe.pins), 3)
      self.assertTrue(all(keep == 6 for _p, _o, keep in self.probe.settles[:3]))

   def test_shorter_agreement_moves_the_offset_back(self):
      self.probe.held = 2
      self.router.prefix_planner = self._planner()
      request = request_fixture()
      self._admit(request)
      first = next(item for item in self.runtime.executed if item.phase == "PREFILL")
      self.assertEqual(first.position, 32)
      self.assertEqual(first.payload, request.prompt_token_ids[32:])

   def test_cold_planned_request_still_carries_the_chain_for_storing(self):
      self.probe.held = 0
      self.router.prefix_planner = self._planner()
      request = request_fixture()
      self._admit(request)
      first = next(item for item in self.runtime.executed if item.phase == "PREFILL")
      self.assertEqual(first.position, 0)
      self.assertEqual(first.payload, request.prompt_token_ids)
      self.assertEqual(
         first.prefix_chain,
         chain_digests(route_scope_digest(self.graph), request.prompt_token_ids, 16),
      )

   def test_replay_mode_never_plans(self):
      self.runtime.decode_mode = "complete_context_replay"
      router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(self.graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=self.runtime,
         transport=FakeTransportPort(),
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      router.prefix_planner = self._planner()
      router.admit(
         request_fixture(),
         InMemoryClientSink(),
         excluded_placements=self.entry_exclusions,
      )
      self.assertEqual(self.probe.pins, [])
      self.assertEqual(router.prefix_reused_tokens("request-1"), 0)

   def test_recovery_prefill_stays_at_position_zero_with_no_plan(self):
      relay = self.router.relay
      request = replace(request_fixture(), prefix_reuse_tokens=96, prefix_chain=("sha256:" + "a" * 64,))
      self.assertEqual(relay._kv_position(request, "RECOVERY_PREFILL", 1), 0)
      self.assertEqual(relay._prefix_plan_fields(request, "RECOVERY_PREFILL", 1), {})
      self.assertEqual(relay._prefix_plan_fields(request, "DECODE", 3), {})
      self.assertEqual(relay._kv_position(request, "PREFILL", -1), 96)
      self.assertEqual(relay._kv_position(request, "DECODE", 3), len(request.prompt_token_ids) + 2)


class PrefixWireTests(unittest.TestCase):
   def _locked(self, request):
      graph = graph_fixture()
      router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(graph),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=FakeRuntimePort(),
         transport=FakeTransportPort(),
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      router.admit(
         request,
         InMemoryClientSink(),
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      record = router.get_request(request.request_id)
      build = PathBuildState(
         request=request,
         graph=graph,
         path_id=record.manifest.path_id,
         path_attempt=record.manifest.path_attempt,
         ordered_hops=record.manifest.ordered_hops,
      )
      return ManifestLocked(
         request_id=request.request_id,
         path_id=record.manifest.path_id,
         path_attempt=record.manifest.path_attempt,
         manifest=record.manifest,
         build=build,
      )

   def test_cold_default_adds_no_field_to_the_wire(self):
      frame = encode_frame(self._locked(request_fixture()))
      self.assertNotIn(b"prefix_reuse_tokens", frame)
      self.assertNotIn(b"prefix_chain", frame)

   def test_planned_request_round_trips_exactly(self):
      chain = chain_digests("route", tuple(range(100)), 16)
      request = replace(request_fixture(), prefix_reuse_tokens=96, prefix_chain=chain)
      locked = self._locked(request)
      decoded = decode_frame(encode_frame(locked)).message
      self.assertEqual(decoded.build.request.prefix_reuse_tokens, 96)
      self.assertEqual(decoded.build.request.prefix_chain, chain)
      self.assertEqual(decoded.build.request, request)

   def test_malformed_prefix_fields_fail_closed(self):
      from mycelium_router.wire import WireError, _request_from_dict
      from dataclasses import asdict

      body = asdict(request_fixture())
      body["prefix_chain"] = "not-a-list"
      with self.assertRaises(WireError):
         _request_from_dict(body)
      body = asdict(request_fixture())
      body["prefix_reuse_tokens"] = -1
      with self.assertRaises(WireError):
         _request_from_dict(body)


if __name__ == "__main__":
   unittest.main()
