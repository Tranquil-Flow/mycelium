"""Request admission, checkpointing, decode, and failure recovery."""

from dataclasses import dataclass, field

from mycelium_router.contracts import (
   ExecutionGraph,
   FailureReport,
   HopHeader,
   ManifestDelta,
   ManifestLocked,
   PathBuildState,
   PathCancellation,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   RequestContext,
   TokenEvent,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.payloads import encode_token_ids
from mycelium_router.routing import RoutingError
from mycelium_router.state import RequestStateMachine
from mycelium_router.validation import validate_manifest


@dataclass
class RequestRecord:
   request: RequestContext
   graph: ExecutionGraph
   manifest: PathManifest
   client_sink: object
   state_machine: RequestStateMachine
   generated_token_ids: list[int] = field(default_factory=list)
   prefill_chunks: tuple[tuple[int, ...], ...] = ()
   completed_prefill_chunks: int = 0
   excluded_placements: frozenset[str] = frozenset()
   excluded_edges: frozenset[str] = frozenset()
   excluded_devices: frozenset[str] = frozenset()
   cleaned_up: bool = False

   @property
   def status(self) -> str:
      return self.state_machine.state


@dataclass
class PendingPrefill:
   request: RequestContext
   graph: ExecutionGraph
   build: PathBuildState
   client_sink: object
   state_machine: RequestStateMachine
   prefill_chunks: tuple[tuple[int, ...], ...]


class EntryCoordinator:
   def __init__(
      self,
      *,
      node_id,
      topology,
      device_states,
      capacity,
      runtime,
      transport,
      relay,
      builder,
      clock,
      config,
   ):
      self.node_id = node_id
      self.topology = topology
      self.device_states = device_states
      self.capacity = capacity
      self.runtime = runtime
      self.transport = transport
      self.relay = relay
      self.builder = builder
      self.clock = clock
      self.config = config
      self._requests: dict[str, RequestRecord] = {}
      self._pending_prefills: dict[str, PendingPrefill] = {}

   def admit(
      self,
      request: RequestContext,
      client_sink,
      *,
      excluded_placements: frozenset[str] = frozenset(),
      excluded_edges: frozenset[str] = frozenset(),
      excluded_devices: frozenset[str] = frozenset(),
   ) -> str:
      if (
         request.request_id in self._requests
         or request.request_id in self._pending_prefills
      ):
         raise ValueError("duplicate_request_id")
      graph, manifest, build = self._build_path(
         request,
         path_attempt=0,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      state_machine = RequestStateMachine(path_attempt=manifest.path_attempt)
      state_machine.transition("PREFILL", path_attempt=manifest.path_attempt)
      record = RequestRecord(
         request=request,
         graph=graph,
         manifest=manifest,
         client_sink=client_sink,
         state_machine=state_machine,
         excluded_placements=build.excluded_placements,
         excluded_edges=build.excluded_edges,
         excluded_devices=build.excluded_devices,
      )
      self._requests[request.request_id] = record
      outcome = self.relay.execute_manifest(
         graph=graph,
         manifest=manifest,
         request=request,
         phase="PREFILL",
         token_index=-1,
         payload=request.prompt_token_ids,
      )
      if outcome.failure_report is not None:
         if not self.receive_failure_report(outcome.failure_report):
            raise RoutingError("prefill_failed", outcome.failure_report.reason)
      else:
         state_machine.transition("LOCKED", path_attempt=manifest.path_attempt)
         state_machine.transition("DECODING", path_attempt=manifest.path_attempt)
         if outcome.token_event is not None:
            prefill_accepted = self.receive_token_event(outcome.token_event)
         else:
            prefill_accepted = self.relay.decode_mode == "complete_context_replay"
         if not prefill_accepted:
            state_machine.transition("FAILED", path_attempt=manifest.path_attempt)
            self._cleanup_record(record)
            raise RoutingError("prefill_missing_token")
      return request.request_id

   def start_distributed_prefill(
      self,
      request: RequestContext,
      client_sink,
      *,
      excluded_placements: frozenset[str] = frozenset(),
      excluded_edges: frozenset[str] = frozenset(),
      excluded_devices: frozenset[str] = frozenset(),
   ) -> str:
      if (
         request.request_id in self._requests
         or request.request_id in self._pending_prefills
      ):
         raise ValueError("duplicate_request_id")
      self.transport.remember_entry(request.request_id, self.node_id)
      graph = self.topology.snapshot()
      build = self.builder.start(
         request,
         graph,
         path_attempt=0,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      build = self.builder.advance(
         build,
         self.device_states.snapshot(),
         now=self.clock.now(),
      )
      prefill_chunks = self._prefill_chunks(request.prompt_token_ids)
      state_machine = RequestStateMachine(path_attempt=0)
      state_machine.transition("PREFILL", path_attempt=0)
      self._pending_prefills[request.request_id] = PendingPrefill(
         request=request,
         graph=graph,
         build=build,
         client_sink=client_sink,
         state_machine=state_machine,
         prefill_chunks=prefill_chunks,
      )
      first = build.ordered_hops[0]
      self.transport.send_manifest_delta(
         ManifestDelta(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            hop_index=0,
            hop=first,
         )
      )
      header = HopHeader(
         request_id=request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=0,
         source_placement_id="",
         destination_placement_id=first.placement_id,
         topology_version=graph.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            phase="PREFILL",
            token_index=-1,
            hop_index=0,
         ),
         prefill_chunk_token_count=len(prefill_chunks[0]),
      )
      context = ProgressivePrefillContext(
         graph=graph,
         request=request,
         build=build,
         payload=encode_token_ids(prefill_chunks[0]),
      )
      self.transport.send_hop(header, context)
      return request.request_id

   def receive_manifest_locked(
      self,
      locked: ManifestLocked,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      pending = self._pending_prefills.get(locked.request_id)
      if pending is None or pending.state_machine.state != "PREFILL":
         return False
      if source_node_id is not None:
         if not source_node_id or not locked.manifest.ordered_hops:
            return False
         final_placement_id = locked.manifest.ordered_hops[-1].placement_id
         if not any(
            placement.placement_id == final_placement_id
            and placement.node_id == source_node_id
            for stage in pending.graph.stages
            for placement in stage.placements
         ):
            return False
      if (
         locked.path_id != pending.build.path_id
         or locked.path_attempt != pending.build.path_attempt
         or locked.manifest.request_id != pending.request.request_id
         or not self.builder.is_complete(locked.build)
         or locked.build.ordered_hops[:1] != pending.build.ordered_hops[:1]
      ):
         return False
      validate_manifest(locked.manifest, pending.graph)
      if locked.manifest.ordered_hops != locked.build.ordered_hops:
         return False
      pending.state_machine.transition(
         "LOCKED",
         path_attempt=locked.path_attempt,
      )
      record = RequestRecord(
         request=pending.request,
         graph=pending.graph,
         manifest=locked.manifest,
         client_sink=pending.client_sink,
         state_machine=pending.state_machine,
         prefill_chunks=pending.prefill_chunks,
         completed_prefill_chunks=1,
         excluded_placements=locked.build.excluded_placements,
         excluded_edges=locked.build.excluded_edges,
         excluded_devices=locked.build.excluded_devices,
      )
      self._requests[locked.request_id] = record
      self._pending_prefills.pop(locked.request_id, None)
      self.relay.register_path(record.request, record.manifest, record.graph)
      if len(record.prefill_chunks) == 1:
         record.state_machine.transition(
            "DECODING",
            path_attempt=locked.path_attempt,
         )
      else:
         self._send_prefill_chunk(record, chunk_index=1)
      return True

   def receive_prefill_chunk_completed(
      self,
      event: PrefillChunkCompleted,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      record = self._requests.get(event.request_id)
      if record is None or record.status != "LOCKED":
         return False
      if source_node_id is not None and not self._final_hop_origin_matches_locked_path(
         record,
         source_node_id,
      ):
         return False
      manifest = record.manifest
      expected_index = record.completed_prefill_chunks
      if (
         event.path_id != manifest.path_id
         or event.path_attempt != manifest.path_attempt
         or event.chunk_index != expected_index
         or event.chunk_index >= len(record.prefill_chunks)
         or event.token_count != len(record.prefill_chunks[event.chunk_index])
      ):
         return False
      record.completed_prefill_chunks += 1
      if record.completed_prefill_chunks == len(record.prefill_chunks):
         record.state_machine.transition(
            "DECODING",
            path_attempt=manifest.path_attempt,
         )
      else:
         self._send_prefill_chunk(
            record,
            chunk_index=record.completed_prefill_chunks,
         )
      return True

   def _send_prefill_chunk(
      self,
      record: RequestRecord,
      *,
      chunk_index: int,
   ) -> None:
      manifest = record.manifest
      first = manifest.ordered_hops[0]
      chunk = record.prefill_chunks[chunk_index]
      header = HopHeader(
         request_id=record.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="PREFILL_CHUNK",
         token_index=chunk_index,
         hop_index=0,
         source_placement_id="",
         destination_placement_id=first.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="PREFILL_CHUNK",
            token_index=chunk_index,
            hop_index=0,
         ),
         prefill_chunk_token_count=len(chunk),
      )
      self.transport.send_hop(header, encode_token_ids(chunk))

   def _prefill_chunks(
      self,
      prompt_token_ids: tuple[int, ...],
   ) -> tuple[tuple[int, ...], ...]:
      size = self.config.prefill_chunk_size_tokens
      if size <= 0 or size >= len(prompt_token_ids):
         return (prompt_token_ids,)
      return tuple(
         prompt_token_ids[start : start + size]
         for start in range(0, len(prompt_token_ids), size)
      )

   def request_status(self, request_id: str) -> str:
      record = self._requests.get(request_id)
      if record is not None:
         return record.status
      pending = self._pending_prefills.get(request_id)
      if pending is not None:
         return pending.state_machine.state
      raise KeyError(request_id)

   def get_request(self, request_id: str) -> RequestRecord:
      return self._requests[request_id]

   def cancel(self, request_id: str) -> bool:
      pending = self._pending_prefills.get(request_id)
      if pending is not None:
         if pending.state_machine.state != "PREFILL":
            return False
         pending.state_machine.transition(
            "CANCELLED",
            path_attempt=pending.build.path_attempt,
         )
         cancellation = PathCancellation(
            request_id=request_id,
            path_id=pending.build.path_id,
            path_attempt=pending.build.path_attempt,
            topology_version=pending.graph.topology_version,
         )
         try:
            self.transport.send_path_cancellation(cancellation)
         finally:
            self.builder.abort(pending.build)
            self.relay.release_path(pending.build.path_id)
         return True
      record = self._requests.get(request_id)
      if record is None or record.status in {
         "COMPLETED",
         "FAILED",
         "CANCELLED",
      }:
         return False
      record.state_machine.transition(
         "CANCELLED",
         path_attempt=record.manifest.path_attempt,
      )
      cancellation = PathCancellation(
         request_id=request_id,
         path_id=record.manifest.path_id,
         path_attempt=record.manifest.path_attempt,
         topology_version=record.manifest.topology_version,
      )
      try:
         self.transport.send_path_cancellation(cancellation)
      finally:
         self._cleanup_record(record)
      return True

   def generate(self, request_id: str, *, token_count: int) -> tuple[int, ...]:
      record = self.get_request(request_id)
      starting = len(record.generated_token_ids)
      target = min(starting + token_count, record.request.max_new_tokens)
      while len(record.generated_token_ids) < target:
         if not self.decode_one(request_id):
            break
      return tuple(record.generated_token_ids[starting:])

   def decode_one_distributed(self, request_id: str) -> bool:
      if request_id in self._pending_prefills:
         return False
      record = self.get_request(request_id)
      if record.status != "DECODING":
         return False
      token_index = len(record.generated_token_ids)
      manifest = record.manifest
      first = manifest.ordered_hops[0]
      final = manifest.ordered_hops[-1]
      header = HopHeader(
         request_id=record.request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="DECODE",
         token_index=token_index,
         hop_index=0,
         source_placement_id=final.placement_id,
         destination_placement_id=first.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=record.request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="DECODE",
            token_index=token_index,
            hop_index=0,
         ),
      )
      if self.relay.decode_mode == "stage_local_kv":
         if not record.generated_token_ids:
            return False
         decode_tokens = (record.generated_token_ids[-1],)
      else:
         decode_tokens = (
            record.request.prompt_token_ids + tuple(record.generated_token_ids)
         )
      self.transport.send_hop(
         header,
         encode_token_ids(decode_tokens),
      )
      return True

   def decode_one(self, request_id: str) -> bool:
      if request_id in self._pending_prefills:
         return False
      record = self.get_request(request_id)
      if record.status != "DECODING":
         return False
      token_index = len(record.generated_token_ids)
      while record.status == "DECODING":
         if self.relay.decode_mode == "stage_local_kv":
            if not record.generated_token_ids:
               return False
            decode_tokens = (record.generated_token_ids[-1],)
         else:
            decode_tokens = (
               record.request.prompt_token_ids + tuple(record.generated_token_ids)
            )
         outcome = self.relay.execute_manifest(
            graph=record.graph,
            manifest=record.manifest,
            request=record.request,
            phase="DECODE",
            token_index=token_index,
            payload=decode_tokens,
         )
         if outcome.failure_report is not None:
            if not self.receive_failure_report(outcome.failure_report):
               return False
            record = self.get_request(request_id)
            continue
         if outcome.token_event is None:
            record.state_machine.transition(
               "FAILED",
               path_attempt=record.manifest.path_attempt,
            )
            self._cleanup_record(record)
            return False
         return self.receive_token_event(outcome.token_event)
      return False

   def receive_token_event(
      self,
      event: TokenEvent,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      record = self._requests.get(event.request_id)
      if record is None or record.status != "DECODING":
         return False
      manifest = record.manifest
      if (
         event.path_id != manifest.path_id
         or event.path_attempt != manifest.path_attempt
         or event.token_index != len(record.generated_token_ids)
         or event.sampling_counter != event.token_index + 1
      ):
         return False
      if source_node_id is not None and not self._final_hop_origin_matches_locked_path(
         record,
         source_node_id,
      ):
         return False
      record.generated_token_ids.append(event.token_id)
      record.client_sink.emit(event.token_index, event.token_id)
      if len(record.generated_token_ids) >= record.request.max_new_tokens:
         record.state_machine.transition(
            "COMPLETED",
            path_attempt=record.manifest.path_attempt,
         )
         self._cleanup_record(record)
      return True

   @staticmethod
   def _final_hop_origin_matches_locked_path(
      record: RequestRecord,
      source_node_id: str,
   ) -> bool:
      if not source_node_id:
         return False
      final_placement_id = record.manifest.ordered_hops[-1].placement_id
      return any(
         placement.placement_id == final_placement_id
         and placement.node_id == source_node_id
         for stage in record.graph.stages
         for placement in stage.placements
      )

   def receive_failure_report(
      self,
      report: FailureReport,
      *,
      source_node_id: str | None = None,
   ) -> bool:
      record = self._requests.get(report.request_id)
      if record is None or record.status in {
         "COMPLETED",
         "FAILED",
         "CANCELLED",
      }:
         return False
      manifest = record.manifest
      if (
         report.path_id != manifest.path_id
         or report.path_attempt != manifest.path_attempt
      ):
         return False
      if (
         record.status == "DECODING"
         and report.token_index != len(record.generated_token_ids)
      ):
         return False
      if not self._failure_identity_matches_locked_path(record, report):
         return False
      if source_node_id is not None and not self._failure_origin_matches_locked_path(
         record,
         report,
         source_node_id,
      ):
         return False
      if report.scope == "DEVICE" and report.node_id == self.node_id:
         record.state_machine.transition(
            "FAILED",
            path_attempt=manifest.path_attempt,
         )
         self._cleanup_record(record)
         return False
      if manifest.path_attempt >= self.config.maximum_recovery_attempts:
         record.state_machine.transition(
            "FAILED",
            path_attempt=manifest.path_attempt,
         )
         self._cleanup_record(record)
         return False

      excluded_placements = record.excluded_placements
      excluded_edges = record.excluded_edges
      excluded_devices = record.excluded_devices
      if report.scope == "EDGE" and report.edge_id:
         excluded_edges |= frozenset({report.edge_id})
      elif report.scope == "DEVICE" and report.node_id:
         excluded_devices |= frozenset({report.node_id})
      elif report.placement_id:
         excluded_placements |= frozenset({report.placement_id})

      old_reservations = tuple(
         hop.reservation_id for hop in manifest.ordered_hops
      )
      self.capacity.release(old_reservations)
      self.relay.release_path(manifest.path_id)
      new_attempt = manifest.path_attempt + 1
      record.state_machine.begin_recovery(path_attempt=new_attempt)
      try:
         graph, new_manifest, build = self._build_path(
            record.request,
            path_attempt=new_attempt,
            excluded_placements=excluded_placements,
            excluded_edges=excluded_edges,
            excluded_devices=excluded_devices,
         )
      except RoutingError:
         record.state_machine.transition(
            "FAILED",
            path_attempt=new_attempt,
         )
         record.cleaned_up = True
         return False

      record.graph = graph
      record.manifest = new_manifest
      record.excluded_placements = build.excluded_placements
      record.excluded_edges = build.excluded_edges
      record.excluded_devices = build.excluded_devices
      replay_tokens = (
         record.request.prompt_token_ids + tuple(record.generated_token_ids)
      )
      outcome = self.relay.execute_manifest(
         graph=graph,
         manifest=new_manifest,
         request=record.request,
         phase="RECOVERY_PREFILL",
         token_index=(
            len(record.generated_token_ids)
            if self.relay.decode_mode == "stage_local_kv"
            else len(record.generated_token_ids) - 1
         ),
         payload=replay_tokens,
      )
      if outcome.failure_report is not None or (
         self.relay.decode_mode == "stage_local_kv"
         and outcome.token_event is None
      ):
         record.state_machine.transition(
            "FAILED",
            path_attempt=new_manifest.path_attempt,
         )
         self._cleanup_record(record)
         return False
      record.state_machine.transition(
         "LOCKED",
         path_attempt=new_manifest.path_attempt,
      )
      record.state_machine.transition(
         "DECODING",
         path_attempt=new_manifest.path_attempt,
      )
      if outcome.token_event is not None:
         return self.receive_token_event(outcome.token_event)
      return True

   def _failure_identity_matches_locked_path(
      self,
      record: RequestRecord,
      report: FailureReport,
   ) -> bool:
      manifest = record.manifest
      selected_placement_ids = {
         hop.placement_id for hop in manifest.ordered_hops
      }
      if report.scope == "PLACEMENT":
         return report.placement_id in selected_placement_ids

      placement_by_id = {
         placement.placement_id: placement
         for stage in record.graph.stages
         for placement in stage.placements
      }
      if report.scope == "DEVICE":
         return report.node_id == self.node_id or any(
            placement_by_id[placement_id].node_id == report.node_id
            for placement_id in selected_placement_ids
         )
      if report.scope != "EDGE":
         return False

      selected_pairs = {
         (left.placement_id, right.placement_id)
         for left, right in zip(
            manifest.ordered_hops,
            manifest.ordered_hops[1:],
         )
      }
      selected_edge_ids = {
         edge.edge_id
         for edge in record.graph.edges
         if (edge.from_placement_id, edge.to_placement_id) in selected_pairs
      }
      selected_edge_ids.add(manifest.loopback_edge_id)
      return report.edge_id in selected_edge_ids

   @staticmethod
   def _failure_origin_matches_locked_path(
      record: RequestRecord,
      report: FailureReport,
      source_node_id: str,
   ) -> bool:
      if not source_node_id:
         return False
      placement_by_id = {
         placement.placement_id: placement
         for stage in record.graph.stages
         for placement in stage.placements
      }
      if report.scope == "DEVICE":
         return report.node_id == source_node_id
      placement = placement_by_id.get(report.placement_id)
      if placement is None or placement.node_id != source_node_id:
         return False
      if report.scope == "EDGE":
         edge = next(
            (
               edge
               for edge in (*record.graph.edges, *record.graph.loopback_edges)
               if edge.edge_id == report.edge_id
            ),
            None,
         )
         if edge is None or report.placement_id not in {
            edge.from_placement_id,
            edge.to_placement_id,
         }:
            return False
      return not report.node_id or report.node_id == source_node_id

   def _cleanup_record(self, record: RequestRecord) -> None:
      if record.cleaned_up:
         return
      self.capacity.release(
         tuple(hop.reservation_id for hop in record.manifest.ordered_hops)
      )
      try:
         self.relay.release_path(record.manifest.path_id)
      finally:
         record.cleaned_up = True

   def _build_path(
      self,
      request: RequestContext,
      *,
      path_attempt: int,
      excluded_placements: frozenset[str],
      excluded_edges: frozenset[str],
      excluded_devices: frozenset[str],
   ) -> tuple[ExecutionGraph, PathManifest, PathBuildState]:
      graph = self.topology.snapshot()
      build = self.builder.start(
         request,
         graph,
         path_attempt=path_attempt,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )
      while not self.builder.is_complete(build):
         build = self.builder.advance(
            build,
            self.device_states.snapshot(),
            now=self.clock.now(),
         )
         self.transport.send_manifest_delta(
            ManifestDelta(
               request_id=request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               hop_index=len(build.ordered_hops) - 1,
               hop=build.ordered_hops[-1],
            )
         )
      manifest = self.builder.lock(build, now=self.clock.now())
      return graph, manifest, build
