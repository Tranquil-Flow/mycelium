"""Deterministic local harnesses for PathCancellation adversarial tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from typing import Any, Callable

from mycelium_router.contracts import RequestContext, RouterConfig, RuntimeResult
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   InMemoryClientSink,
   InProcessMesh,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table


NODE_IDS = ("node-a", "node-c", "node-d")
ENTRY_NODE = "node-a"


class BlockingRuntime(FakeRuntimePort):
   """Block only decode execution; cancellation remains callable."""

   def __init__(self):
      super().__init__()
      self.decode_entered = threading.Event()
      self.release_decode = threading.Event()

   def execute(self, item):
      if item.phase == "DECODE":
         self.executed.append(item)
         self.decode_entered.set()
         if not self.release_decode.wait(timeout=1.0):
            return RuntimeResult(success=False, failure_reason="test_decode_timeout")
         token_id = self.token_base + item.token_index + 1
         return RuntimeResult(success=True, payload=item.payload, token_id=token_id)
      return super().execute(item)


class BlockingSink(InMemoryClientSink):
   """Expose deterministic cancellation-vs-completion interleaving."""

   def __init__(self):
      super().__init__()
      self.emit_entered = threading.Event()
      self.release_emit = threading.Event()

   def emit(self, token_index: int, token_id: int) -> None:
      super().emit(token_index, token_id)
      self.emit_entered.set()
      if not self.release_emit.wait(timeout=1.0):
         raise TimeoutError("test_sink_emit_timeout")


@dataclass
class MeshCase:
   mesh: Any
   routers: dict[str, Router]
   runtimes: dict[str, FakeRuntimePort]
   capacity: FakeCapacityPort
   clock: ManualClock
   sink: InMemoryClientSink
   request: RequestContext

   @property
   def entry(self) -> Router:
      return self.routers[ENTRY_NODE]

   @property
   def record(self):
      return self.entry.get_request(self.request.request_id)

   @property
   def cancellation(self):
      from mycelium_router.contracts import PathCancellation

      manifest = self.record.manifest
      return PathCancellation(
         request_id=self.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         topology_version=manifest.topology_version,
      )


def build_mesh_case(
   *,
   mesh=None,
   request_id: str = "request-path-cancellation-adversarial",
   max_new_tokens: int = 2,
   sink=None,
   runtime_factory: Callable[[str], FakeRuntimePort] | None = None,
) -> MeshCase:
   """Build and lock one three-participant path through public Router APIs."""

   graph = three_device_graph()
   states = state_table(slow_b_bandwidth=True)
   states["node-d"] = replace(states["node-a"], node_id="node-d")
   capacity = FakeCapacityPort()
   clock = ManualClock()
   mesh = InProcessMesh() if mesh is None else mesh
   sink = InMemoryClientSink() if sink is None else sink
   request = replace(
      request_fixture(),
      request_id=request_id,
      max_new_tokens=max_new_tokens,
   )
   routers: dict[str, Router] = {}
   runtimes: dict[str, FakeRuntimePort] = {}
   for node_id in NODE_IDS:
      runtime = runtime_factory(node_id) if runtime_factory else FakeRuntimePort()
      router = Router(
         node_id=node_id,
         topology=FakeTopologyProvider(graph),
         device_states=FakeDeviceStateProvider(states),
         capacity=capacity,
         runtime=runtime,
         transport=mesh.transport_for(node_id),
         clock=clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(prefill_chunk_size_tokens=0),
      )
      mesh.register_router(node_id, router)
      routers[node_id] = router
      runtimes[node_id] = runtime
   start = getattr(mesh, "start", None)
   if callable(start):
      start()
   routers[ENTRY_NODE].start_distributed_prefill(
      request,
      sink,
      excluded_placements=frozenset({"node-b-stage-000"}),
   )
   assert routers[ENTRY_NODE].request_status(request.request_id) == "DECODING"
   return MeshCase(mesh, routers, runtimes, capacity, clock, sink, request)


def run_in_thread(callable_, *, daemon: bool = False):
   results: list[object] = []
   errors: list[BaseException] = []

   def target() -> None:
      try:
         results.append(callable_())
      except BaseException as error:  # deliberately surface worker exceptions to test
         errors.append(error)

   thread = threading.Thread(
      target=target,
      name="path-cancellation-adversarial",
      daemon=daemon,
   )
   thread.start()
   return thread, results, errors


def join_bounded(thread: threading.Thread, *, timeout: float = 1.0) -> None:
   thread.join(timeout=timeout)
   assert not thread.is_alive(), "test worker leaked past deterministic deadline"
