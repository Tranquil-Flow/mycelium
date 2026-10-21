# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime batching on the live hop path (owner ruling 2026-10-01).

The live iroh transport used to hand every inbound hop to ``Router.receive_hop``,
which executes it alone, so concurrent requests never shared a runtime batch on
a live route.  Hops now enter ``Router.enqueue_hop`` and are drained by
``Router.pump_ready_batches``.  The receiver takes the next frame only after the
current one is acknowledged, so while the batch cap admits batching the
dispatcher acknowledges a queued hop at once and keeps collecting for a bounded
window; under a cap of one every hop still executes inside its own dispatch,
before its frame is acknowledged.

These cases drive the production ``IrohTransport`` dispatcher (with an in-memory
sidecar client), the real ``Router`` and, where numbers matter, the real MLX
stage runtime over a tiny model.

Failure modes this suite catches (one per test, by name):

  test_concurrent_live_decode_hops_form_one_runtime_batch_the_gate_sees
      Concurrent decode hops on the live path served one at a time, or a batch
      whose numbers differ from the sequential reference, or a publication the
      gate's mode observer cannot read as a candidate window.
  test_baseline_cap_one_serves_each_live_hop_alone_and_the_gate_sees_no_batch
      The baseline policy still forming a batch, or a baseline window the gate
      would read as batched.
  test_baseline_hop_executes_before_its_frame_is_acknowledged
      The baseline changing the incumbent path: a hop must still run inside its
      own dispatch, before its frame is acknowledged.
  test_candidate_hop_is_acknowledged_then_executed_when_its_window_closes
      A candidate hop executed before the receiver can collect the next frame,
      or never executed once nothing else arrives.
  test_cancellation_during_a_batch_drops_only_the_cancelled_member
      A member cancelled while its batch executes still being forwarded, or its
      cancellation taking the rest of the batch with it.
  test_one_member_completion_failure_is_scoped_to_that_member
      A forwarding failure for one member failing the whole batch, killing the
      transport, or going unreported.
  test_collection_is_bounded_by_the_cap_and_the_window
      A full batch, or a cap of one, waiting for the window; a dispatcher wait
      that outlives the window.
  test_a_router_without_the_batch_surface_keeps_the_synchronous_hop_path
      Routers that do not offer the batch surface losing their hop path.
"""

from __future__ import annotations

import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_router.contracts import (
   HopHeader,
   ManifestLocked,
   PathBuildState,
   PathHop,
   PathManifest,
   RouterConfig,
   TokenEvent,
)
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.payloads import encode_token_ids
from mycelium_router.router import Router
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.wire import decode_frame, encode_frame
from scripts.run_a10_physical_gate import mode_observer
from tests.a10_deterministic.test_a10_batch_policy import _command, _service
from test_router_iroh_transport import _Hub
from test_router_mlx_runtime import _fresh_ports, _work_item, runtime_case  # noqa: F401
from test_router_policy import request_fixture


LEASE = 1_000_000_000_000.0
PROMPT = (1, 2)
DECODE_TOKENS = {"a": 5, "b": 6}


class _GatedRouter(Router):
   """A real Router whose token sink can hold the dispatcher at a known point."""

   def __init__(self, **kwargs):
      super().__init__(**kwargs)
      self.hold = threading.Event()
      self.held = threading.Event()

   def receive_token_event(self, event, *, source_node_id=None):
      self.held.set()
      assert self.hold.wait(5)
      return True


class _StatusClient:
   def __init__(self, publication):
      self._publication = publication

   def live_status(self):
      return {"peers": [{"a10_publication": self._publication}]}


def _nodes(case):
   """(entry node, final-stage node): this suite's transport is the final stage."""

   return case.assignments[0]["node_id"], case.assignments[1]["node_id"]


def _transport(hub, case) -> IrohTransport:
   entry_node, final_node = _nodes(case)
   return IrohTransport(
      node_id=final_node,
      socket_path="/unused",
      bootstrap_secret=b"s" * 32,
      peer=PeerBinding(
         node_id=entry_node,
         endpoint_id="peer-endpoint",
         endpoint_addr={"id": "peer-endpoint", "addrs": ["127.0.0.1:1"]},
         generation=7,
      ),
      expected_endpoint_id="local-endpoint",
      queue_capacity=16,
      delivery_timeout_seconds=1.0,
      poll_interval_seconds=0.01,
      client_factory=hub.client,
   )


#: Long enough that the test's frames always meet inside one window, however
#: loaded the host; production uses the RouterConfig default.
TEST_CONFIG = RouterConfig(live_hop_collection_window_seconds=1.0)


def _router(case, runtime, transport) -> _GatedRouter:
   _, final_node = _nodes(case)
   return _GatedRouter(
      node_id=final_node,
      topology=FakeTopologyProvider(case.graph),
      device_states=FakeDeviceStateProvider({}),
      capacity=FakeCapacityPort(),
      runtime=runtime,
      transport=transport,
      clock=ManualClock(),
      id_source=SequenceIdSource(),
      config=TEST_CONFIG,
   )


def _policy(router, policy):
   _service(router.relay, config=TEST_CONFIG).dispatch(_command({"policy": policy}))


def _locked(case, name) -> ManifestLocked:
   graph = case.graph
   request = request_fixture(
      request_id=f"request-{name}",
      prompt_token_ids=PROMPT,
      max_new_tokens=4,
      expected_new_tokens=4,
   )
   hops = tuple(
      PathHop(
         stage.stage_id,
         stage.placements[0].placement_id,
         f"reservation-{name}-{index}",
         reservation_expires_at=LEASE,
         reservation_epoch=graph.deployment_epoch,
      )
      for index, stage in enumerate(graph.stages)
   )
   manifest = PathManifest(
      path_id=f"path-{name}",
      path_attempt=0,
      request_id=request.request_id,
      deployment_id=graph.deployment_id,
      deployment_epoch=graph.deployment_epoch,
      topology_version=graph.topology_version,
      manifest_digest=graph.manifest_digest,
      ordered_hops=hops,
      loopback_edge_id=graph.loopback_edges[0].edge_id,
   )
   return ManifestLocked(
      request_id=request.request_id,
      path_id=manifest.path_id,
      path_attempt=0,
      manifest=manifest,
      build=PathBuildState(
         request=request,
         graph=graph,
         path_id=manifest.path_id,
         path_attempt=0,
         ordered_hops=hops,
      ),
   )


def _decode_header(case, name) -> HopHeader:
   graph = case.graph
   return HopHeader(
      request_id=f"request-{name}",
      path_id=f"path-{name}",
      path_attempt=0,
      phase="DECODE",
      token_index=1,
      hop_index=1,
      source_placement_id=graph.stages[0].placements[0].placement_id,
      destination_placement_id=graph.stages[1].placements[0].placement_id,
      topology_version=graph.topology_version,
      idempotency_key=hop_idempotency_key(
         request_id=f"request-{name}",
         path_id=f"path-{name}",
         path_attempt=0,
         phase="DECODE",
         token_index=1,
         hop_index=1,
      ),
   )


def _item(case, stage, name, payload, *, phase):
   return _work_item(
      case,
      stage,
      payload,
      phase=phase,
      token_span=len(PROMPT) if phase == "PREFILL" else 1,
      request_id=f"request-{name}",
      path_id=f"path-{name}",
      token_index=None if phase == "PREFILL" else 1,
      position=0 if phase == "PREFILL" else len(PROMPT),
      lease_expires_at=LEASE,
   )


def _stage_inputs(case, final_port):
   """Prefill both requests on both stages; return each request's decode input.

   The entry stage runs here; its decode activation is what the entry node would
   send to the final stage as the live decode hop.
   """

   entry_port = _fresh_ports(case)[0]
   decode_inputs = {}
   for name, token in DECODE_TOKENS.items():
      hidden = entry_port.execute(
         _item(case, 0, name, encode_token_ids(PROMPT), phase="PREFILL")
      )
      assert hidden.success, hidden.failure_reason
      prefilled = final_port.execute(_item(case, 1, name, hidden.payload, phase="PREFILL"))
      assert prefilled.success, prefilled.failure_reason
      decoded = entry_port.execute(
         _item(case, 0, name, encode_token_ids((token,)), phase="DECODE")
      )
      assert decoded.success, decoded.failure_reason
      decode_inputs[name] = decoded.payload
   return decode_inputs


def _sequential_reference(case):
   final_port = _fresh_ports(case)[1]
   decode_inputs = _stage_inputs(case, final_port)
   reference = {}
   for name in DECODE_TOKENS:
      result = final_port.execute(_item(case, 1, name, decode_inputs[name], phase="DECODE"))
      assert result.success, result.failure_reason
      reference[f"request-{name}"] = (result.token_id, _digest(result.payload))
   return reference


def _event_frame_for(index):
   return encode_frame(
      TokenEvent(
         request_id=f"request-event-{index}",
         path_id=f"path-event-{index}",
         path_attempt=0,
         token_index=0,
         token_id=1,
         sampling_counter=1,
      )
   )


def _hold_dispatcher(hub, router):
   """Park the dispatcher inside a token event so later frames queue behind it."""

   hub.deliver(
      b"t" * 16,
      encode_frame(
         TokenEvent(
            request_id="request-hold",
            path_id="path-hold",
            path_attempt=0,
            token_index=0,
            token_id=1,
            sampling_counter=1,
         )
      ),
   )
   assert router.held.wait(5)


def _wait(predicate, timeout=10.0):
   deadline = time.monotonic() + timeout
   while not predicate():
      if time.monotonic() >= deadline:
         raise AssertionError("condition not reached")
      time.sleep(0.005)


def _sent(hub, kind):
   return [
      decode_frame(frame).message
      for _message_id, frame, _timeout, _generation in list(hub.sent)
      if type(decode_frame(frame).message).__name__ == kind
   ]


def _tokens(hub):
   """Token events the final stage sent back to the entry node, by request."""

   return {event.request_id: event for event in _sent(hub, "TokenEvent")}


def _digest(payload) -> str | None:
   return None if payload is None else hashlib.sha256(bytes(payload)).hexdigest()


class _RecordingPort:
   """Pass-through to the MLX stage port that records what the relay dispatched.

   It keeps the port's whole A10 dispatch surface, so the relay's batch producer
   treats it exactly as the port itself.
   """

   def __init__(self, port):
      self._port = port
      self.batch_sizes: list[int] = []
      self.results: dict[str, object] = {}

   def __getattr__(self, name):
      return getattr(self._port, name)

   def offer_batch_member(self, item, *, member_ref):
      return self._port.offer_batch_member(item, member_ref=member_ref)

   def dispatch_mode(self, *args, **kwargs):
      return self._port.dispatch_mode(*args, **kwargs)

   def execution_interval(self):
      return self._port.execution_interval()

   def execute(self, item):
      result = self._port.execute(item)
      self.batch_sizes.append(1)
      self.results[item.request_id] = result
      return result

   def execute_batch(self, batch):
      outcome = self._port.execute_batch(batch)
      member_refs = getattr(batch, "member_refs", None)
      if member_refs is None:
         for item, result in zip(batch.items, outcome):
            self.batch_sizes.append(1)
            self.results[item.request_id] = result
      else:
         self.batch_sizes.append(len(member_refs))
         for execution in outcome.executed:
            # member refs are "a10-member:<request>:<path>:...": see the producer.
            request_id = execution.member_ref.split(":", 2)[1]
            self.results[request_id] = execution.result
      return outcome


def _start(case, runtime, names):
   hub = _Hub()
   transport = _transport(hub, case)
   router = _router(case, runtime, transport)
   transport.bind_router(router)
   transport.start()
   for index, name in enumerate(names):
      hub.deliver(bytes([0x40 + index]) * 16, encode_frame(_locked(case, name)))
   _wait(lambda: all(router.relay._paths.get(f"path-{name}") for name in names))
   return hub, transport, router


def _deliver_decode(case, hub, name, payload, message_byte):
   hub.deliver(message_byte * 16, encode_frame(_decode_header(case, name), payload))


def _serve_two_concurrent_decodes(case, policy):
   """Two requests' decode hops arrive together on the live path of one node."""

   final_port = _fresh_ports(case)[1]
   decode_inputs = _stage_inputs(case, final_port)
   runtime = _RecordingPort(final_port)
   hub, transport, router = _start(case, runtime, tuple(DECODE_TOKENS))
   _policy(router, policy)
   try:
      _hold_dispatcher(hub, router)
      for index, name in enumerate(DECODE_TOKENS):
         _deliver_decode(case, hub, name, decode_inputs[name], bytes([index + 1]))
      router.hold.set()
      _wait(lambda: len(_tokens(hub)) == 2)
      assert transport.fatal_error is None
      return runtime, final_port.a10_publication_snapshot(), _tokens(hub)
   finally:
      router.hold.set()
      transport.close()


def _assert_matches_reference(case, runtime, tokens):
   reference = _sequential_reference(case)
   assert set(tokens) == set(reference)
   for request_id, (token_id, payload_digest) in reference.items():
      assert tokens[request_id].token_id == token_id
      assert runtime.results[request_id].token_id == token_id
      assert _digest(runtime.results[request_id].payload) == payload_digest


def test_concurrent_live_decode_hops_form_one_runtime_batch_the_gate_sees(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime, publication, tokens = _serve_two_concurrent_decodes(
      runtime_case,
      "candidate",
   )

   assert runtime.batch_sizes == [2], runtime.batch_sizes
   _assert_matches_reference(runtime_case, runtime, tokens)
   observe = mode_observer(_StatusClient(publication))
   assert observe("candidate", 0.0) == "verified"
   assert observe("baseline", 0.0) == "contradicted"


def test_baseline_cap_one_serves_each_live_hop_alone_and_the_gate_sees_no_batch(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime, publication, tokens = _serve_two_concurrent_decodes(
      runtime_case,
      "baseline",
   )

   assert runtime.batch_sizes == [1, 1], runtime.batch_sizes
   _assert_matches_reference(runtime_case, runtime, tokens)
   observe = mode_observer(_StatusClient(publication))
   assert observe("baseline", 0.0) == "verified"
   assert observe("candidate", 0.0) == "contradicted"


class _GateRuntime(FakeRuntimePort):
   """Fake stage runtime whose batches can be held mid-execution."""

   def __init__(self):
      super().__init__()
      self.release = threading.Event()
      self.entered = threading.Event()
      self.block = False

   def execute_batch(self, batch):
      if self.block:
         self.entered.set()
         assert self.release.wait(5)
      return super().execute_batch(batch)


def test_baseline_hop_executes_before_its_frame_is_acknowledged(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, "baseline")
   runtime.block = True
   try:
      _deliver_decode(runtime_case, hub, "a", b"hidden", b"a")
      assert runtime.entered.wait(5)
      time.sleep(0.05)
      # The hop is executing inside its own dispatch: not yet acknowledged.
      assert b"a" * 16 not in hub.acks
      runtime.release.set()
      _wait(lambda: b"a" * 16 in hub.acks)
      _wait(lambda: set(_tokens(hub)) == {"request-a"})
      assert [len(batch.items) for batch in runtime.executed_batches] == [1]
   finally:
      runtime.release.set()
      transport.close()


def test_candidate_hop_is_acknowledged_then_executed_when_its_window_closes(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a", "b"))
   _policy(router, "candidate")
   runtime.block = True
   # Request b was just seen on this node, so it may still join a's window.
   router.relay._hop_arrivals.record(
      "path-b", at=router.relay.clock.now(), terminal=False
   )
   try:
      queued_at = time.monotonic()
      _deliver_decode(runtime_case, hub, "a", b"hidden", b"a")
      # Acknowledged first, so the receiver can take the next frame ...
      _wait(lambda: b"a" * 16 in hub.acks)
      assert not runtime.entered.is_set()
      # ... and executed once the window closes with nothing else arriving.
      assert runtime.entered.wait(5)
      assert time.monotonic() - queued_at >= TEST_CONFIG.live_hop_collection_window_seconds * 0.9
      runtime.release.set()
      _wait(lambda: set(_tokens(hub)) == {"request-a"})
      assert [len(batch.items) for batch in runtime.executed_batches] == [1]
      assert transport.fatal_error is None
   finally:
      runtime.release.set()
      transport.close()


def test_cancellation_during_a_batch_drops_only_the_cancelled_member(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a", "b"))
   _policy(router, "candidate")
   try:
      _hold_dispatcher(hub, router)
      _deliver_decode(runtime_case, hub, "a", b"hidden-a", b"\x01")
      _deliver_decode(runtime_case, hub, "b", b"hidden-b", b"\x02")
      runtime.block = True
      router.hold.set()
      assert runtime.entered.wait(5)
      # Cancel b while the batch holding both members is executing.
      router.relay.release_path("path-b")
      runtime.release.set()
      _wait(lambda: "request-a" in _tokens(hub))
      time.sleep(0.1)
      assert [len(batch.items) for batch in runtime.executed_batches] == [2]
      assert set(_tokens(hub)) == {"request-a"}
      assert "path-b" in runtime.cancelled_path_ids
      assert router.relay.pending_batch_hops() == 0
      assert transport.fatal_error is None
   finally:
      router.hold.set()
      runtime.release.set()
      transport.close()


def test_one_member_completion_failure_is_scoped_to_that_member(
   runtime_case,  # noqa: F811 - pytest fixture
):
   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a", "b"))
   original = router.relay._complete_queued_hop

   def complete(header, item, result, digest, generation):
      if header.request_id == "request-b":
         raise RuntimeError("injected_member_completion_failure")
      return original(header, item, result, digest, generation)

   router.relay._complete_queued_hop = complete
   _policy(router, "candidate")
   try:
      _hold_dispatcher(hub, router)
      _deliver_decode(runtime_case, hub, "a", b"hidden-a", b"\x01")
      _deliver_decode(runtime_case, hub, "b", b"hidden-b", b"\x02")
      router.hold.set()
      _wait(lambda: "request-a" in _tokens(hub))
      _wait(
         lambda: any(
            event.request_id == "request-b" and event.code == "router_dispatch_failed"
            for event in transport._scoped_events
         )
      )
      assert [len(batch.items) for batch in runtime.executed_batches] == [2]
      assert set(_tokens(hub)) == {"request-a"}
      # b's sender receives a failure report for exactly that hop.
      _wait(lambda: _sent(hub, "FailureReport"))
      assert [
         (report.request_id, report.reason) for report in _sent(hub, "FailureReport")
      ] == [("request-b", "router_dispatch_failed")]
      assert transport.fatal_error is None
   finally:
      router.hold.set()
      transport.close()


class _ThreadRecordingRuntime(_GateRuntime):
   def __init__(self):
      super().__init__()
      self.threads = []

   def execute_batch(self, batch):
      self.threads.append(threading.current_thread())
      return super().execute_batch(batch)


@pytest.mark.parametrize("policy", ["candidate", "baseline"])
def test_a_hop_dispatched_off_the_dispatcher_thread_is_drained_by_the_dispatcher(
   runtime_case,  # noqa: F811 - pytest fixture
   policy,
):
   """A local hop dispatched by another thread (an entry's request worker, say)
   is only queued there.  The drain, and every member it executes and forwards,
   runs on the dispatcher, so the caller is never trapped executing other
   requests' hops or waiting on their remote deliveries."""

   runtime = _ThreadRecordingRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, policy)
   entry_node, _ = _nodes(runtime_case)
   runtime.block = True
   try:
      started = time.monotonic()
      transport._dispatch(
         decode_frame(encode_frame(_decode_header(runtime_case, "a"), b"hidden")),
         source_node_id=entry_node,
      )
      assert time.monotonic() - started < 0.5
      assert runtime.entered.wait(5)
      assert runtime.threads == [transport._dispatcher_thread]
      runtime.release.set()
      _wait(lambda: set(_tokens(hub)) == {"request-a"})
      assert transport.fatal_error is None
   finally:
      runtime.release.set()
      transport.close()


def test_candidate_hop_alone_on_the_node_executes_without_waiting_the_window(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """No other request can join, so the window is never paid."""

   runtime = _GateRuntime()
   hub, transport, router = _start(runtime_case, runtime, ("a",))
   _policy(router, "candidate")
   try:
      queued_at = time.monotonic()
      _deliver_decode(runtime_case, hub, "a", b"hidden", b"a")
      _wait(lambda: set(_tokens(hub)) == {"request-a"})
      assert (
         time.monotonic() - queued_at
         < TEST_CONFIG.live_hop_collection_window_seconds * 0.5
      )
      assert [len(batch.items) for batch in runtime.executed_batches] == [1]
      assert transport.fatal_error is None
   finally:
      transport.close()


class _CountingRouter:
   def __init__(self, pending, cap, window=0.5):
      self.pending = pending
      self.cap = cap
      self.window = window
      self.pumps = 0

   def pending_batch_hops(self):
      return self.pending

   def runtime_batch_cap(self):
      return self.cap

   def hop_collection_window_seconds(self):
      return self.window

   def pump_ready_batches(self, *, on_item_error=None):
      self.pumps += 1
      return ()


def test_collection_is_bounded_by_the_cap_and_the_window(
   runtime_case,  # noqa: F811 - pytest fixture
):
   transport = _transport(_Hub(), runtime_case)

   # A cap of one never collects: the hop runs inside its own dispatch.
   router = _CountingRouter(pending=1, cap=1)
   transport._queue_hop_for_batch(router)
   assert (router.pumps, transport._hop_collection_deadline) == (1, None)

   # A full batch never waits for the window.
   router = _CountingRouter(pending=4, cap=4)
   transport._queue_hop_for_batch(router)
   assert (router.pumps, transport._hop_collection_deadline) == (1, None)

   # Otherwise one window opens at the first queued hop and later hops join it.
   router = _CountingRouter(pending=1, cap=4, window=0.5)
   opened = time.monotonic()
   transport._queue_hop_for_batch(router)
   deadline = transport._hop_collection_deadline
   assert router.pumps == 0
   assert opened + 0.5 <= deadline <= time.monotonic() + 0.5
   router.pending = 2
   transport._queue_hop_for_batch(router)
   assert transport._hop_collection_deadline == deadline
   # The dispatcher never blocks past the window or past its own poll.
   assert 0.0 <= transport._dispatch_wait_seconds() <= transport.poll_interval_seconds
   transport._hop_collection_deadline = time.monotonic() - 1.0
   assert transport._dispatch_wait_seconds() == 0.0


def test_a_router_without_the_batch_surface_keeps_the_synchronous_hop_path(
   runtime_case,  # noqa: F811 - pytest fixture
):
   received = []

   class _SyncRouter:
      def receive_hop(self, header, payload, *, source_node_id=None):
         received.append((header.request_id, source_node_id))

   assert IrohTransport._batching_router(_SyncRouter()) is None
   assert IrohTransport._batching_router(SimpleNamespace()) is None
   hub = _Hub()
   transport = _transport(hub, runtime_case)
   transport.bind_router(_SyncRouter())
   transport.start()
   try:
      _deliver_decode(runtime_case, hub, "a", b"hidden", b"s")
      _wait(lambda: received == [("request-a", _nodes(runtime_case)[0])])
   finally:
      transport.close()


def test_a_switched_window_is_the_one_the_dispatcher_collects_for(
   runtime_case,  # noqa: F811 - pytest fixture
):
   transport = _transport(_Hub(), runtime_case)
   router = _router(runtime_case, FakeRuntimePort(), None)
   _service(router.relay, config=RouterConfig()).dispatch(
      _command({"policy": "candidate@120"}))
   assert router.hop_collection_window_seconds() == 0.12
   opened = time.monotonic()
   transport._queue_hop_for_batch(SimpleNamespace(
      runtime_batch_cap=router.runtime_batch_cap,
      pending_batch_hops=lambda: 1,
      hop_collection_window_seconds=router.hop_collection_window_seconds,
   ))
   assert opened + 0.12 <= transport._hop_collection_deadline <= time.monotonic() + 0.12


def test_router_facade_exposes_the_batch_surface(runtime_case):  # noqa: F811
   router = _router(runtime_case, FakeRuntimePort(), None)
   assert router.runtime_batch_cap() == RouterConfig().maximum_runtime_batch_size
   assert router.batch_clock_now() == 0.0
   assert router.pump_ready_batches() == ()
