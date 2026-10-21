#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first acceptance: live hop/timing telemetry accuracy + measured-RTT routing.

Base c59920a gaps this module pins, one reason per test:

  G1  ``mycelium_router.instrument.HopRecorder`` / ``trace_request`` were
      imported only by tests.  The live router (``mycelium_live/route.py``) and
      the transports never called them, so a real run emitted ZERO hop traces
      unless a test wrapped the call sites by hand.
  G2  ``instrument._Hop.send()`` / ``recv()`` stamped ``start == end == now``:
      a point-in-time stamp, not a socket measurement.  The serialize and
      deserialize spans were neither captured nor labelled, so no caller could
      separate them from the network span.
  G3  No live RTT sample was ever filed.  The periodic keepalive/health probe
      (``mycelium_live/route.py::_idle_probe_peer``) discarded its round-trip
      duration, ``scoring.py`` silently substituted ``RouterConfig.default_rtt_ms``
      (100.0) for a missing neighbour RTT, and ``mycelium_m16_runtime.py``
      hardcoded ``1.0`` ms for every neighbour.
  G4  Because of G3 the route cost model could not consume a measurement.

Every assertion below is written so that a fabricated or defaulted number cannot
satisfy it: a "measured" claim must be backed by a span that brackets a real
socket write/read, and a missing measurement must be labelled as such.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

# The repository under test, never a foreign checkout.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_router import instrument  # noqa: E402
from mycelium_router.contracts import (  # noqa: E402
   HopHeader,
   RouterConfig,
)
from mycelium_router.fakes import (  # noqa: E402
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router  # noqa: E402
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy  # noqa: E402
from mycelium_router.scoring import RouteScorer  # noqa: E402
from mycelium_router.transports.loopback_socket import (  # noqa: E402
   LoopbackSocketMesh,
)
from test_router_contracts import graph_fixture  # noqa: E402
from test_router_inprocess_mesh import three_device_graph  # noqa: E402
from test_router_policy import request_fixture, state_table  # noqa: E402


def _socket_request(request_id):
   return replace(request_fixture(), request_id=request_id)


def _run_real_socket_mesh(*, request_id, rtt_table=None, hop_recorder=None):
   """Drive one real TCP prefill+decode across three routers on loopback.

   No test-side wrapping of ``send``/``recv``: the mesh is handed the recorder
   and the RTT table as construction arguments, exactly as a live caller would.
   Returns the mesh (already closed), the routers, the sink and the request.
   """

   graph = three_device_graph()
   states = state_table(slow_b_bandwidth=True)
   states["node-d"] = replace(states["node-a"], node_id="node-d")
   capacity = FakeCapacityPort()
   clock = ManualClock()
   mesh = LoopbackSocketMesh(rtt_table=rtt_table, hop_recorder=hop_recorder)
   routers = {}
   request = _socket_request(request_id)
   sink = InMemoryClientSink()
   for node_id in ("node-a", "node-c", "node-d"):
      router = Router(
         node_id=node_id,
         topology=FakeTopologyProvider(graph),
         device_states=FakeDeviceStateProvider(states),
         capacity=capacity,
         runtime=FakeRuntimePort(),
         transport=mesh.transport_for(node_id),
         clock=clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(prefill_chunk_size_tokens=2),
      )
      mesh.register_router(node_id, router)
      routers[node_id] = router
   mesh.start()
   try:
      entry = routers["node-a"]
      entry.start_distributed_prefill(
         request,
         sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      entry.decode_one_distributed(request.request_id)
   finally:
      mesh.close()
   return mesh, routers, sink, request


def _trace_lines(path):
   with open(path) as handle:
      return [json.loads(line) for line in handle if line.strip()]


def _all_hops(path):
   hops = []
   for trace in _trace_lines(path):
      hops.extend(trace.get("hops", []))
   return hops


class LiveHopTelemetryEmission(unittest.TestCase):
   """G1 — a real transport run must emit hop traces with no test-side wrapping."""

   def test_real_socket_run_emits_hop_traces(self):
      handle = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
      handle.close()
      sink_path = Path(handle.name)
      try:
         recorder = instrument.HopRecorder(enabled=True, sink_path=sink_path)
         from mycelium_router.rtt_measurement import MeasuredRttTable

         table = MeasuredRttTable()
         mesh, _routers, sink, request = _run_real_socket_mesh(
            request_id="request-measure-emission",
            rtt_table=table,
            hop_recorder=recorder,
         )
         # The run really moved frames over loopback TCP.
         self.assertGreater(mesh.connection_count, 0)
         self.assertTrue(sink.token_ids)
         traces = _trace_lines(sink_path)
         self.assertTrue(
            traces,
            "a real socket run emitted zero hop traces: the transport never "
            "reached HopRecorder",
         )
         hops = [hop for trace in traces for hop in trace["hops"]]
         self.assertTrue(hops, "hop traces carried no hops")
         # Every emitted hop must be attributable to the real request, and every
         # one of them must be a measured span (this run has no point stamps).
         self.assertEqual(
            {trace["request_id"] for trace in traces}, {request.request_id}
         )
         self.assertEqual(
            {hop["request_id"] for hop in hops}, {request.request_id}
         )
         self.assertEqual(
            {hop["transport_source"] for hop in hops}, {"socket_spans"}
         )
         # The same run must feed the RTT table used by the route cost model.
         self.assertGreater(table.measured_link_count(), 0)
         measured = table.snapshot()["links"]
         self.assertIsInstance(measured, dict)
         self.assertTrue(
            all(link["sample_count"] > 0 for link in measured.values()),
            f"a link was filed with no samples: {measured}",
         )
      finally:
         sink_path.unlink(missing_ok=True)

   def test_live_router_module_reaches_the_recorder(self):
      """The live route module must reference HopRecorder/trace_request itself.

      G1 was "imported only by tests".  A wiring that lives in the test file is
      not a wiring, so the live module is checked for the call.
      """

      source = (ROOT / "mycelium_live" / "route.py").read_text()
      self.assertIn("HopRecorder", source)
      self.assertIn("trace_request", source)

   def test_recorder_is_reachable_from_the_environment(self):
      """A real run must be able to turn instrumentation on without code edits."""

      factory = getattr(instrument.HopRecorder, "from_environment", None)
      self.assertIsNotNone(
         factory,
         "HopRecorder.from_environment is missing: the live path has no env "
         "switch for instrumentation",
      )
      with tempfile.TemporaryDirectory() as directory:
         sink_path = Path(directory) / "live-hop-traces.jsonl"
         environment = {
            "MYCELIUM_ROUTER_INSTRUMENT": "1",
            "MYCELIUM_ROUTER_INSTRUMENT_SINK": str(sink_path),
         }
         previous = {key: os.environ.get(key) for key in environment}
         os.environ.update(environment)
         try:
            recorder = instrument.HopRecorder.from_environment()
            self.assertTrue(recorder.enabled)
            self.assertEqual(recorder.sink_path, sink_path)
            with recorder.trace_request("request-env"):
               pass
            self.assertTrue(sink_path.exists())
            self.assertEqual(len(_trace_lines(sink_path)), 1)
         finally:
            for key, value in previous.items():
               if value is None:
                  os.environ.pop(key, None)
               else:
                  os.environ[key] = value


class TrueSocketTimestamps(unittest.TestCase):
   """G2 — real spans: pre-write, post-read, and separate labelled serialize."""

   def test_hop_carries_true_socket_spans(self):
      handle = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
      handle.close()
      sink_path = Path(handle.name)
      try:
         recorder = instrument.HopRecorder(enabled=True, sink_path=sink_path)
         _run_real_socket_mesh(
            request_id="request-measure-spans",
            hop_recorder=recorder,
         )
         hops = _all_hops(sink_path)
         measured = [
            hop for hop in hops
            if hop.get("transport_source") == "socket_spans"
         ]
         self.assertTrue(
            measured,
            "no hop carried transport_source='socket_spans': the transport "
            "never recorded a real socket write/read span",
         )
         for hop in measured:
            with self.subTest(hop=hop.get("hop_id")):
               # True monotonic points either side of the socket write/read.
               self.assertGreater(hop["socket_write_at"], 0.0)
               self.assertGreater(hop["socket_read_end_at"], hop["socket_write_at"])
               self.assertAlmostEqual(
                  hop["transport_ms"],
                  (hop["socket_read_end_at"] - hop["socket_write_at"]) * 1000.0,
                  places=3,
               )
               self.assertGreater(hop["transport_ms"], 0.0)
               # Serialize and deserialize are separate, explicitly labelled spans.
               self.assertIn(hop["serialize_source"], ("encode_frame",))
               self.assertAlmostEqual(
                  hop["serialize_ms"],
                  (hop["serialize_end_at"] - hop["serialize_start_at"]) * 1000.0,
                  places=3,
               )
               self.assertIn(hop["deserialize_source"], ("response_frame",))
               self.assertAlmostEqual(
                  hop["deserialize_ms"],
                  (hop["deserialize_end_at"] - hop["deserialize_start_at"]) * 1000.0,
                  places=3,
               )
               # The network span must not be the merged serialize/deserialize
               # blob.  transport_ms is pinned to its own socket span above; the
               # not-equal check only applies when that span and the merged sum
               # differ beyond rounding, so a sub-millisecond coincidence (two
               # independent spans landing on the same 0.001 ms) is not a failure.
               socket_span_ms = round(
                  (hop["socket_read_end_at"] - hop["socket_write_at"]) * 1000.0, 3
               )
               merged_ms = round(hop["serialize_ms"] + hop["deserialize_ms"], 3)
               if abs(socket_span_ms - merged_ms) > 0.001:
                  self.assertNotEqual(
                     hop["transport_ms"],
                     merged_ms,
                     "transport_ms looks merged with serialize/deserialize",
                  )
               # Ordered, disjoint spans: serialize -> write -> read -> deserialize.
               self.assertLessEqual(hop["serialize_start_at"], hop["serialize_end_at"])
               self.assertLessEqual(hop["serialize_end_at"], hop["socket_write_at"])
               self.assertLessEqual(
                  hop["socket_read_end_at"], hop["deserialize_start_at"]
               )
               # A measured round trip is stated as such, with its source named.
               self.assertEqual(hop["measured_rtt_ms"], hop["transport_ms"])
               self.assertEqual(
                  hop["measured_rtt_source"], "socket_round_trip"
               )
               self.assertGreater(hop["bytes_sent"], 0)
      finally:
         sink_path.unlink(missing_ok=True)

   def test_point_stamp_fallback_is_labelled_not_silent(self):
      """The old point-stamp path must announce itself instead of looking measured."""

      handle = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
      handle.close()
      sink_path = Path(handle.name)
      try:
         recorder = instrument.HopRecorder(enabled=True, sink_path=sink_path)
         with recorder.trace_request("request-point-stamp") as request:
            with request.hop("stage-0", "node-0") as hop:
               hop.queue_in()
               hop.compute_start()
               hop.compute_end()
               hop.send(bytes_out=128)
               hop.recv(bytes_in=64)
         recorded = _all_hops(sink_path)[0]
         self.assertEqual(recorded["transport_source"], "point_stamp_fallback")
         self.assertIsNone(recorded["measured_rtt_ms"])
         self.assertIsNone(recorded["serialize_ms"])
         self.assertIsNone(recorded["deserialize_ms"])
      finally:
         sink_path.unlink(missing_ok=True)


class MeasuredRttTableContract(unittest.TestCase):
   """G3 — measured RTT with a documented fallback, never a silent default."""

   def test_module_exists_and_exposes_the_fallback(self):
      from mycelium_router import rtt_measurement

      self.assertTrue(hasattr(rtt_measurement, "MeasuredRttTable"))
      self.assertEqual(rtt_measurement.DEFAULT_FALLBACK_RTT_MS, 100.0)
      self.assertEqual(
         rtt_measurement.PROVENANCE_FALLBACK_DEFAULT, "fallback_default_ms"
      )
      self.assertEqual(rtt_measurement.PROVENANCE_MEASURED, "measured")

   def test_measured_round_trips_become_measured_samples(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      self.assertIsNone(table.rtt_ms("node-a", "node-c").last_measured_at_monotonic)
      table.record_round_trip_ms(
         "node-a", "node-c", 21.5, measurement_source="socket_round_trip"
      )
      table.record_round_trip_ms(
         "node-a", "node-c", 18.5, measurement_source="socket_round_trip"
      )
      sample = table.rtt_ms("node-a", "node-c")
      self.assertEqual(sample.provenance, "measured")
      self.assertTrue(sample.is_measured)
      self.assertEqual(sample.sample_count, 2)
      self.assertEqual(sample.latest_ms, 18.5)
      self.assertEqual(sample.mean_ms, 20.0)
      self.assertEqual(sample.min_ms, 18.5)
      self.assertEqual(sample.measurement_source, "socket_round_trip")
      self.assertIsNotNone(sample.last_measured_at_monotonic)
      # Direction matters: the reverse link was never measured.
      reverse = table.rtt_ms("node-c", "node-a")
      self.assertEqual(reverse.provenance, "fallback_default_ms")
      self.assertEqual(reverse.sample_count, 0)
      self.assertFalse(reverse.is_measured)
      self.assertEqual(reverse.rtt_ms, 100.0)
      self.assertIsNone(reverse.last_measured_at_monotonic)

   def test_configured_fallback_is_named_as_configured(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      sample = table.rtt_ms("node-a", "node-b", fallback_ms=250.0)
      self.assertEqual(sample.rtt_ms, 250.0)
      self.assertEqual(sample.provenance, "fallback_configured_ms")
      self.assertFalse(sample.is_measured)

   def test_invalid_samples_are_rejected(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      for bad in (-1.0, float("nan"), float("inf")):
         with self.subTest(value=bad):
            with self.assertRaises(ValueError):
               table.record_round_trip_ms("node-a", "node-b", bad)

   def test_link_observation_is_planner_shaped(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      table.record_round_trip_ms(
         "node-a", "node-b", 12.0, measurement_source="socket_round_trip"
      )
      observation = table.link_observation(
         "node-a", "node-b", bandwidth_Bps=1_000_000_000.0
      )
      self.assertEqual(observation.src, "node-a")
      self.assertEqual(observation.dst, "node-b")
      self.assertEqual(observation.rtt_ms, 12.0)
      self.assertFalse(observation.inferred)
      self.assertFalse(observation.stale)
      # An unmeasured pair must not be exportable as a measured link.
      with self.assertRaises(ValueError):
         table.link_observation("node-b", "node-a")


class MeasuredRttFeedsRouteCost(unittest.TestCase):
   """G4 — the route cost model must consume the measurement."""

   def test_measured_rtt_flips_route_selection(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      graph = graph_fixture()
      request = request_fixture()
      # Equal bandwidths, so the choice is decided by latency alone.
      states = state_table(slow_b_bandwidth=False)
      # Static state: node-a->node-b is 2.0 ms, node-a->node-c is 20.0 ms, so
      # the latency-only choice is the node-b path.
      static_choice = _selected_middle_placement(
         RoutePolicy(RouteScorer(RouterConfig())), request, graph, states
      )
      self.assertEqual(static_choice, "node-b-stage-001")

      table = MeasuredRttTable()
      for _ in range(5):
         table.record_round_trip_ms(
            "node-a", "node-b", 400.0, measurement_source="socket_round_trip"
         )
         table.record_round_trip_ms(
            "node-b", "node-a", 400.0, measurement_source="socket_round_trip"
         )
         table.record_round_trip_ms(
            "node-a", "node-c", 4.0, measurement_source="socket_round_trip"
         )
         table.record_round_trip_ms(
            "node-c", "node-a", 4.0, measurement_source="socket_round_trip"
         )
      measured_choice = _selected_middle_placement(
         RoutePolicy(RouteScorer(RouterConfig(), rtt_table=table)),
         request,
         graph,
         states,
      )
      self.assertEqual(
         measured_choice,
         "node-c-stage-001",
         "measured RTT did not change route selection",
      )

   def test_local_host_probe_samples_reach_route_selection(self):
      """Close the loop: a live-route measurement must reach the cost model.

      The route files its own probes under LOCAL_HOST_KEY (it does not name its
      own stage node).  Binding that key to the stage node the plan calls local
      must make the measured value win over the stated one in route selection.
      """

      from mycelium_router.rtt_measurement import LOCAL_HOST_KEY, MeasuredRttTable

      table = MeasuredRttTable()
      # What mycelium_live.route writes for its own keepalive/infer round trips.
      table.record_round_trip_ms(
         LOCAL_HOST_KEY,
         "node-c",
         8.0,
         measurement_source="live_session_round_trip",
      )
      states = state_table()
      unbound = RouteScorer(RouterConfig(), rtt_table=table)
      # Unbound: the local host's own sample is addressed by the sentinel, so
      # node-a keeps its stated 20.0 ms — nothing is silently cross-applied.
      self.assertEqual(
         unbound.transfer_provenance("node-a", "node-c", states)["provenance"],
         "state_neighbor_rtt_ms",
      )
      table.bind_local_source_alias("node-a")
      bound = RouteScorer(RouterConfig(), rtt_table=table)
      resolved = bound.transfer_provenance("node-a", "node-c", states)
      self.assertEqual(resolved["provenance"], "measured")
      self.assertEqual(resolved["rtt_ms"], 8.0)
      self.assertEqual(resolved["sample_count"], 1)
      # And it moves the score: the measured 8 ms link is cheaper one-way than
      # the stated 20 ms one, so the via-c prefill transfer must shrink by
      # (20 - 8) / 2 = 6 ms.
      graph = graph_fixture()
      request = request_fixture()
      stated_score = RouteScorer(RouterConfig()).score_route(
         request,
         graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         states,
         now=0.0,
      )
      measured_score = bound.score_route(
         request,
         graph,
         ("node-a-stage-000", "node-c-stage-001", "node-a-stage-002"),
         states,
         now=0.0,
      )
      self.assertAlmostEqual(
         stated_score.prefill_transfer_ms - measured_score.prefill_transfer_ms,
         6.0,
         places=6,
      )

   def test_measured_rtt_is_visible_in_scorer_provenance(self):
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      table.record_round_trip_ms(
         "node-a", "node-c", 30.0, measurement_source="socket_round_trip"
      )
      scorer = RouteScorer(RouterConfig(), rtt_table=table)
      measured = scorer.transfer_provenance("node-a", "node-c", state_table())
      self.assertEqual(measured["provenance"], "measured")
      self.assertEqual(measured["rtt_ms"], 30.0)
      unmeasured = scorer.transfer_provenance("node-b", "node-c", state_table())
      self.assertNotEqual(unmeasured["provenance"], "measured")
      self.assertEqual(unmeasured["sample_count"], 0)

   def test_layer_planner_cost_uses_measured_rtt(self):
      from mycelium_layer_planner.contracts import PlanningPolicy
      from mycelium_layer_planner.network_cost import transfer_time_ms
      from mycelium_router.rtt_measurement import MeasuredRttTable

      table = MeasuredRttTable()
      table.record_round_trip_ms(
         "node-a", "node-b", 40.0, measurement_source="socket_round_trip"
      )
      observation = table.link_observation(
         "node-a", "node-b", bandwidth_Bps=1_000_000_000.0
      )
      cost = transfer_time_ms(
         observation,
         payload_bytes=1_000_000,
         policy=PlanningPolicy(),
      )
      # 40 ms round trip -> 20 ms one way, no geolocation floor.
      self.assertAlmostEqual(cost.base_one_way_ms, 20.0, places=6)
      self.assertEqual(observation.rtt_ms, 40.0)

   def test_route_scorer_without_a_table_is_unchanged(self):
      """The default (no measurement) path must keep — and now name — its sources."""

      scorer = RouteScorer(RouterConfig())
      # node-a states node-c at 20.0 ms: a stated value, not a measurement.
      stated = scorer.transfer_provenance("node-a", "node-c", state_table())
      self.assertEqual(stated["provenance"], "state_neighbor_rtt_ms")
      self.assertEqual(stated["rtt_ms"], 20.0)
      self.assertEqual(stated["sample_count"], 0)
      self.assertFalse(stated["is_measured"])
      # node-b states nothing for node-c: the configured default applies, and
      # the caller can now see that it was a default.
      defaulted = scorer.transfer_provenance("node-b", "node-c", state_table())
      self.assertEqual(defaulted["provenance"], "fallback_default_rtt_ms")
      self.assertEqual(defaulted["rtt_ms"], 100.0)
      self.assertFalse(defaulted["is_measured"])

   def test_live_keepalive_probe_files_a_measured_round_trip(self):
      """The shipped periodic probe must file what it measures (G3 wiring)."""

      from mycelium_live.route import PhysicalLiveRoute
      from mycelium_router.rtt_measurement import MeasuredRttTable

      route = PhysicalLiveRoute.__new__(PhysicalLiveRoute)
      route._lock = threading.RLock()
      route._command_sequence = 0
      route._plan = {"run_id": "run-1", "deployment_id": "deployment-1"}
      route._last_health = {}
      route._sessions = {"node-c": object()}
      table = MeasuredRttTable()
      route._measured_rtt = table
      route._verify_observation = lambda node_id, response, *, event: {
         "node_id": node_id,
         "event": event,
      }
      delay = 0.02

      def _probe(node_id, *, command_id, command, payload, deadline_monotonic_s):
         self.assertEqual(command, "health")
         time.sleep(delay)
         return {"protocol": "mycelium.physical_node_control.v1", "ok": True}

      route._send_before = _probe
      self.assertTrue(route._idle_probe_peer("node-c"))
      sample = table.rtt_ms(
         rtt_measurement_local_host_key(), "node-c"
      )
      self.assertEqual(
         sample.provenance,
         "measured",
         "the shipped keepalive probe discarded its round-trip duration",
      )
      self.assertEqual(sample.sample_count, 1)
      self.assertEqual(sample.measurement_source, "live_session_round_trip")
      self.assertGreaterEqual(sample.latest_ms, delay * 1000.0 * 0.5)
      self.assertLess(sample.latest_ms, delay * 1000.0 * 50.0)


def _selected_middle_placement(policy, request, graph, states):
   """Run the real progressive route selection and return stage-001's placement.

   ``ProgressivePathBuilder.advance`` is the shipped selection entry point: it
   calls ``RoutePolicy.choose_next`` -> ``RouteScorer.score_route`` for every
   legal suffix.  Returning the chosen middle placement therefore reports what
   route selection actually did, not what a test computed by hand.
   """

   builder = ProgressivePathBuilder(
      policy=policy,
      capacity=FakeCapacityPort(),
      id_source=SequenceIdSource(),
   )
   build = builder.start(
      request,
      graph,
      path_attempt=0,
      excluded_placements=frozenset({"node-b-stage-000"}),
   )
   while not builder.is_complete(build):
      build = builder.advance(build, states, now=0.0)
   manifest = builder.lock(build)
   return manifest.ordered_hops[1].placement_id


def rtt_measurement_local_host_key():
   from mycelium_router.rtt_measurement import LOCAL_HOST_KEY

   return LOCAL_HOST_KEY


if __name__ == "__main__":
   unittest.main(verbosity=2)
