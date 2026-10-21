# SPDX-License-Identifier: AGPL-3.0-only
"""A7 slice 3: the entry Router's fenced KV-successor cutover over real runtimes.

Three in-process Routers' worth of real NumPy stage runtimes on an in-process
mesh: node-a is the entry and stage 0, and also holds the stage-1 STANDBY on a
dedicated port; node-b is the stage-1 source.  Every A7 message is a real
encoded Router frame.  After each token is handed to the gateway the source
ships the KV for exactly that committed prefix and the standby's signed ack
reaches the entry before the next decode.  The source then fails mid-request:

* positive -- the entry fences the old attempt, installs the fence on the
  standby (import) and the surviving stage (rekey), grants one publication and
  resumes DECODE on the standby with no recovery prefill; the output equals the
  uninterrupted reference with no duplicate or missing token;
* negative -- a lagging standby (no ack), an expired qualification, or a
  refused import falls back honestly: no KV continuity is claimed, the A6 path
  owns the attempt, and every participant's A7 state is released;
* abort -- with no A6 authority the request ends with one explicit failure.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium_a7_checkpoint_channel import StandbyCheckpointKey
from mycelium_a7_entry_controller import A7EntryController, StandbyCoverage, stage_digest
from mycelium_a7_fence_store import DurableFenceStore
from mycelium_a7_node_agent import A7NodeAgent, SourceCoverage
from mycelium_a7_node_wiring import enable_terminal_kv_retention
from mycelium_a7_standby_qualification import parity_challenge_result, port_placement_identity, qualify_standby
from mycelium_a7_standby_store import StandbyShadowStore
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_qualification.signing import build_ed25519_verifier, generate_ed25519_signer
from mycelium_router.contracts import (
   KVCheckpointAck,
   KVCheckpointCommit,
   KVCheckpointShipment,
   KVShadowRelease,
   KVSuccessorCutover,
   PathHop,
   PathManifest,
   RequestContext,
   RouterConfig,
   RuntimeResult,
)
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeTopologyProvider,
   InMemoryClientSink,
   InProcessMesh,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.router import Router
from mycelium_router.standby_dispatch import StandbyDispatchRuntime
from mycelium_router.wire import decode_frame, encode_frame
from test_router_numpy_qwen_kv import _reference
from tests.a7_acceptance.test_standby_placement import STANDBY_ID, standby_case, standby_graph

_LEASE = 1_000_000_000_000.0
_MAX = 1 << 20
_PROMPT = (1, 7, 11, 5)
_MAX_NEW = 6
_NOW_MS = 1_800_000_000_000


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class _Killable:
   """The source runtime; once killed every operation fails like a dead placement."""

   def __init__(self, port) -> None:
      self.port = port
      self.killed = False

   def execute(self, item):
      if self.killed:
         return RuntimeResult(success=False, failure_scope="PLACEMENT", failure_reason="worker_runtime_unavailable")
      return self.port.execute(item)

   def __getattr__(self, name):
      return getattr(self.port, name)


class _A7Net:
   def __init__(self) -> None:
      self.routers: dict[str, Router] = {}
      self.delivered: list[str] = []
      self.drop: set[str] = set()

   def sender(self, origin: str):
      def send(destination: str, message, payload: bytes) -> None:
         decoded = decode_frame(encode_frame(message, payload))
         name = type(decoded.message).__name__
         if name in self.drop:
            return
         self.delivered.append(name)
         router = self.routers[destination]
         msg = decoded.message
         if isinstance(msg, KVCheckpointShipment):
            ok = router.receive_kv_checkpoint_shipment(msg, decoded.payload, source_node_id=origin)
         elif isinstance(msg, KVCheckpointAck):
            ok = router.receive_kv_checkpoint_ack(msg, source_node_id=origin)
         elif isinstance(msg, KVSuccessorCutover):
            ok = router.receive_kv_successor_cutover(msg, source_node_id=origin)
         elif isinstance(msg, KVCheckpointCommit):
            ok = router.receive_kv_checkpoint_commit(msg, source_node_id=origin)
         elif isinstance(msg, KVShadowRelease):
            ok = router.receive_kv_shadow_release(msg, source_node_id=origin)
         else:
            ok = router.receive_kv_successor_cutover_receipt(msg, source_node_id=origin)
         assert ok, name

      return send


def _request(request_id: str) -> RequestContext:
   return RequestContext(
      request_id=request_id,
      prompt_token_ids=_PROMPT,
      max_new_tokens=_MAX_NEW,
      expected_new_tokens=_MAX_NEW,
      qos_class="interactive",
      admitted_at=0.0,
      target_ttft_ms=1_000.0,
      target_tpot_ms=100.0,
      target_tokens_per_second=10.0,
      sampling_seed=0,
      generation_config_digest="sha256:" + "c" * 64,
   )


class _Harness:
   def __init__(self, tmp_path: Path, *, qualification_ttl_ms=600_000, a6_available=True, standby_dtype=None, retain_terminal_kv=True) -> None:
      case = standby_case(tmp_path, "qwen2")
      graph = standby_graph(case)
      self.case, self.graph = case, graph
      entry_id = graph.stages[0].placements[0].placement_id
      self.source_id = graph.stages[1].placements[0].placement_id
      # One protocol clock for routers and runtimes, as on a physical node.
      clock = ManualClock()
      self.entry_port = NumpyRuntimePort("node-a", graph, {entry_id: case.loaded[0]}, decode_mode="stage_local_kv", clock=clock.now)
      self.source_port = _Killable(NumpyRuntimePort("node-b", graph, {self.source_id: case.loaded[1]}, decode_mode="stage_local_kv", clock=clock.now))
      self.standby_port = NumpyRuntimePort("node-a", graph, {STANDBY_ID: case.standby_loaded}, decode_mode="stage_local_kv", clock=clock.now)
      node_a_runtime = StandbyDispatchRuntime(self.entry_port, {STANDBY_ID: self.standby_port})
      if retain_terminal_kv:
         # As A7NodeWiring.attach does on every A7-wired node.
         assert enable_terminal_kv_retention((self.entry_port, self.source_port.port, self.standby_port)) == 3

      mesh = InProcessMesh()
      self.capacity = FakeCapacityPort()
      self.net = _A7Net()
      self.routers: dict[str, Router] = {}
      for node_id, runtime in (("node-a", node_a_runtime), ("node-b", self.source_port)):
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider({}),
            capacity=self.capacity,
            runtime=runtime,
            transport=mesh.transport_for(node_id),
            clock=clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(prefill_chunk_size_tokens=2),
         )
         mesh.register_router(node_id, router)
         self.routers[node_id] = router
         self.net.routers[node_id] = router

      signer = generate_ed25519_signer(endpoint_id="node-a-standby")
      verifier = build_ed25519_verifier([signer.public_key_record()])
      placement_digest = _digest(f"placement:{STANDBY_ID}")
      incarnation = _digest("incarnation:node-a:1")
      self.shadow = StandbyShadowStore(max_entry_bytes=_MAX, max_entries=64)
      standby = StandbySide(
         port=self.standby_port,
         placement_id=STANDBY_ID,
         placement_digest=placement_digest,
         incarnation=incarnation,
         signer=signer,
         key=StandbyCheckpointKey(signer=signer, standby_placement_id=placement_digest, standby_membership_incarnation=incarnation),
         store=self.shadow,
         max_checkpoint_bytes=_MAX,
      )
      agent_a = A7NodeAgent("node-a", send=self.net.sender("node-a"), clock_ms=lambda: _NOW_MS)
      agent_b = A7NodeAgent("node-b", send=self.net.sender("node-b"), clock_ms=lambda: _NOW_MS)
      agent_a.register_standby(standby, session_digest=_digest("session"))
      agent_a.register_port(entry_id, self.entry_port)
      stage_id = graph.stages[1].stage_id
      agent_b.register_source_coverage(
         self.source_id,
         SourceCoverage(
            stage_id=stage_digest(stage_id),
            port=self.source_port.port,
            standby_node_id="node-a",
            standby_placement_id=STANDBY_ID,
            standby_placement_digest=placement_digest,
            standby_incarnation=incarnation,
            standby_key_record=standby.key.public_record(),
            verify_standby_signature=verifier,
            source_placement_digest=_digest(f"placement:{self.source_id}"),
            max_bytes=_MAX,
         ),
      )
      agent_b.register_port(self.source_id, self.source_port.port)
      self.routers["node-a"].a7_agent = agent_a
      self.routers["node-b"].a7_agent = agent_b
      self.agent_a, self.agent_b = agent_a, agent_b

      source_identity = port_placement_identity(self.source_port.port, self.source_id)
      standby_identity = port_placement_identity(self.standby_port, STANDBY_ID)
      if standby_dtype is not None:
         standby_identity = dict(standby_identity, **{"runtime.dtype": standby_dtype})
      self.qualification = qualify_standby(
         source_identity=source_identity,
         standby_identity=standby_identity,
         parity_challenge=parity_challenge_result(challenge_id="setup", next_position=len(_PROMPT), source_output=b"same", standby_output=b"same"),
         reserved_kv_bytes=64 * 1024,
         available_bytes=1 << 30,
         issued_at_unix_ms=_NOW_MS - 1_000,
         ttl_ms=qualification_ttl_ms,
      )
      self.fences = DurableFenceStore(tmp_path / "fences")
      self.controller = A7EntryController(
         node_id="node-a",
         agent=agent_a,
         send=self.net.sender("node-a"),
         fence_store=self.fences,
         coverage=[
            StandbyCoverage(
               stage_id=stage_id,
               source_placement_id=self.source_id,
               source_node_id="node-b",
               standby_placement_id=STANDBY_ID,
               standby_node_id="node-a",
               standby_incarnation=incarnation,
               verify_standby_signature=verifier,
               qualification=self.qualification,
            )
         ],
         a6_available=a6_available,
         ack_timeout_seconds=0.05,
         receipt_timeout_seconds=0.5,
         clock_ms=lambda: _NOW_MS,
      )
      self.entry = self.routers["node-a"]
      self.entry.entry.a7_controller = self.controller

   def manifest(self, request_id: str) -> PathManifest:
      graph = self.graph
      return PathManifest(
         path_id=f"path:{request_id}",
         path_attempt=0,
         request_id=request_id,
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         topology_version=graph.topology_version,
         manifest_digest=graph.manifest_digest,
         ordered_hops=tuple(
            PathHop(stage.stage_id, stage.placements[0].placement_id, f"reservation-{request_id}-{index}", _LEASE, graph.deployment_epoch)
            for index, stage in enumerate(graph.stages)
         ),
         loopback_edge_id=graph.loopback_edges[0].edge_id,
      )

   def run(self, request_id: str, *, kill_after: int | None):
      sink = InMemoryClientSink()
      self.entry.start_locked_distributed_prefill(_request(request_id), sink, manifest=self.manifest(request_id), pinned_deployment=self.graph)
      for _ in range(4 * _MAX_NEW):
         if self.entry.request_status(request_id) != "DECODING":
            break
         if kill_after is not None and len(sink.token_ids) >= kill_after:
            self.source_port.killed = True
         self.entry.decode_one_distributed(request_id)
      return sink


def _uninterrupted() -> list[int]:
   return []


def _reference_tokens(case) -> list[int]:
   generated: list[int] = []
   for _ in range(_MAX_NEW):
      generated.append(_reference(case, _PROMPT + tuple(generated)))
   return generated


def test_source_failure_cuts_over_to_the_standby_without_replay(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   sink = harness.run("request:a7-cutover", kill_after=3)
   record = harness.entry.entry.get_request("request:a7-cutover")
   assert harness.entry.request_status("request:a7-cutover") == "COMPLETED"
   result = record.kv_successor_cutover
   assert result["outcome"] == "fenced_kv_successor", result
   assert result["reason"] == "eligible"
   assert result["kv_continuity_claimed"] is True and result["recovery_prefill"] is False
   assert result["gateway_committed_token_count"] == 3 == result["acknowledged_token_count"]
   assert result["acknowledgement_lag"] == 0
   assert result["fencing_generation"] == 2 and result["request_attempt"] == 2
   # Exact delivery: the uninterrupted reference, each index exactly once.
   assert sink.token_ids == _reference_tokens(harness.case)
   assert sink.token_indexes == list(range(_MAX_NEW))
   # No recovery prefill anywhere; the standby only decoded.
   standby = harness.standby_port.kv_snapshot()
   assert standby["prefill_operation_count"] == 0
   assert standby["decode_operation_count"] == _MAX_NEW - 3
   assert harness.entry_port.kv_snapshot()["prefill_operation_count"] >= 1
   assert record.kv_successor_recovery is None  # the A6 replay seam never ran
   assert record.manifest.ordered_hops[1].placement_id == STANDBY_ID
   # One shipment + signed ack per committed (non-final) token before the failure.
   assert harness.net.delivered.count("KVCheckpointCommit") == 3
   assert harness.net.delivered.count("KVCheckpointShipment") == 3
   assert harness.net.delivered.count("KVSuccessorCutoverReceipt") == 2
   # Exact cleanup: shadow released, fence record released, no KV left anywhere.
   assert harness.shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
   assert harness.fences.current("request:a7-cutover") is None
   assert harness.standby_port.kv_snapshot()["active_state_count"] == 0
   assert harness.entry_port.kv_snapshot()["active_state_count"] == 0


def _retained(port) -> int:
   return len(port._terminal_retention)


def test_source_lost_during_the_final_decode_step_still_cuts_over(tmp_path: Path) -> None:
   """The surviving stage already executed the terminal hop: its retained KV is rekeyed."""

   harness = _Harness(tmp_path)
   request_id = "request:a7-final-step"
   sink = harness.run(request_id, kill_after=_MAX_NEW - 1)
   record = harness.entry.entry.get_request(request_id)
   assert harness.entry.request_status(request_id) == "COMPLETED"
   result = record.kv_successor_cutover
   assert result is not None and result["outcome"] == "fenced_kv_successor", result
   assert result["gateway_committed_token_count"] == _MAX_NEW - 1 == result["acknowledged_token_count"]
   assert sink.token_ids == _reference_tokens(harness.case)
   assert sink.token_indexes == list(range(_MAX_NEW))
   standby = harness.standby_port.kv_snapshot()
   assert standby["prefill_operation_count"] == 0 and standby["decode_operation_count"] == 1
   assert record.kv_successor_recovery is None
   # The terminal release freed everything, the retained terminal KV included.
   assert harness.shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
   assert harness.fences.current(request_id) is None
   for port in (harness.entry_port, harness.standby_port):
      assert port.kv_snapshot()["active_state_count"] == 0
      assert _retained(port) == 0


def test_without_terminal_retention_a_final_step_loss_falls_back_to_a6(tmp_path: Path) -> None:
   """The pre-fix behaviour, kept as the contrast: the rekey finds no KV."""

   harness = _Harness(tmp_path, retain_terminal_kv=False)
   request_id = "request:a7-final-step-off"
   harness.run(request_id, kill_after=_MAX_NEW - 1)
   result = harness.entry.entry.get_request(request_id).kv_successor_cutover
   outcome = result if result is not None else harness.controller.snapshot(request_id)["result"]
   assert outcome["outcome"] == "full_context_replay", outcome
   assert outcome["reason"] == "compatibility_incompatible"


def test_completed_requests_release_every_retained_terminal_kv(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   for index in range(3):
      harness.run(f"request:a7-retain-{index}", kill_after=None)
   for port in (harness.entry_port, harness.source_port.port, harness.standby_port):
      snapshot = port.kv_snapshot()
      assert snapshot["active_state_count"] == 0
      assert snapshot["terminal_kv_retention"]["retained_count"] == 0
      assert snapshot["terminal_kv_retention"]["retained_kv_bytes"] == 0
   assert harness.entry_port.kv_snapshot()["terminal_kv_retention"]["retained_total"] == 3
   assert harness.source_port.port.kv_snapshot()["terminal_kv_retention"]["retained_total"] == 3


def test_without_failure_coverage_ships_every_commit_and_releases(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   sink = harness.run("request:a7-clean", kill_after=None)
   assert sink.token_ids == _reference_tokens(harness.case)
   assert harness.net.delivered.count("KVCheckpointCommit") == _MAX_NEW - 1
   assert harness.net.delivered.count("KVShadowRelease") >= 1
   assert harness.shadow.snapshot()["shadow_entry_count"] == 0
   assert harness.fences.current("request:a7-clean") is None
   assert harness.entry.entry.get_request("request:a7-clean").kv_successor_cutover is None


@pytest.mark.parametrize(
   ("kwargs", "drop", "reason"),
   [
      ({}, {"KVCheckpointAck"}, "route_watermark_unavailable"),
      ({"qualification_ttl_ms": 10}, set(), "qualification_not_current"),
      ({"standby_dtype": "float16"}, set(), "compatibility_incompatible"),
   ],
)
def test_unqualified_or_lagging_standby_falls_back_without_continuity(tmp_path: Path, kwargs, drop, reason) -> None:
   harness = _Harness(tmp_path, **kwargs)
   harness.net.drop |= drop
   harness.run("request:a7-fallback", kill_after=3)
   record = harness.entry.entry.get_request("request:a7-fallback")
   result = record.kv_successor_cutover
   assert result["outcome"] == "full_context_replay"
   assert result["reason"] == reason
   assert result["kv_continuity_claimed"] is False
   # The A6 seam owned the attempt (this two-stage fixture has no other ACTIVE
   # stage-1 placement, so A6 itself ends the request without a successor).
   assert record.kv_successor_recovery is not None
   assert record.kv_successor_recovery["recovery_mode"] == "full_context_replay"
   assert harness.standby_port.kv_snapshot()["decode_operation_count"] == 0
   assert harness.standby_port.kv_snapshot()["active_state_count"] == 0
   assert harness.shadow.snapshot()["shadow_entry_count"] == 0
   assert STANDBY_ID not in {hop.placement_id for hop in record.manifest.ordered_hops}
   # The A6-ended request finishes its A7 coverage: no durable fence record stays.
   assert harness.fences.current("request:a7-fallback") is None
   assert harness.fences.record_count() == 0
   assert record.kv_successor_cutover["outcome"] == "full_context_replay"


def test_no_a6_authority_aborts_explicitly(tmp_path: Path) -> None:
   harness = _Harness(tmp_path, qualification_ttl_ms=10, a6_available=False)
   sink = harness.run("request:a7-abort", kill_after=2)
   record = harness.entry.entry.get_request("request:a7-abort")
   assert harness.entry.request_status("request:a7-abort") == "FAILED"
   assert record.kv_successor_cutover["outcome"] == "aborted"
   assert record.kv_successor_recovery is None
   assert sink.token_indexes == [0, 1]
   assert harness.standby_port.kv_snapshot()["active_state_count"] == 0
   assert harness.fences.current("request:a7-abort") is None


def test_old_attempt_output_and_checkpoints_are_rejected_after_the_fence(tmp_path: Path) -> None:
   from mycelium_router.contracts import TokenEvent

   harness = _Harness(tmp_path)
   request_id = "request:a7-stale"
   sink = InMemoryClientSink()
   harness.entry.start_locked_distributed_prefill(_request(request_id), sink, manifest=harness.manifest(request_id), pinned_deployment=harness.graph)
   for _ in range(2):
      harness.entry.decode_one_distributed(request_id)
   harness.source_port.killed = True
   harness.entry.decode_one_distributed(request_id)
   record = harness.entry.entry.get_request(request_id)
   assert record.kv_successor_cutover["outcome"] == "fenced_kv_successor"
   old_path = f"path:{request_id}"
   committed = len(sink.token_ids)
   # A late token from the fenced source attempt is never published.
   stale = TokenEvent(request_id=request_id, path_id=old_path, path_attempt=0, token_index=committed, token_id=1, sampling_counter=committed + 1)
   assert harness.entry.entry.receive_token_event(stale, source_node_id="node-b") is False
   assert len(sink.token_ids) == committed
   # The source can no longer checkpoint for the request: its role was released.
   commit = KVCheckpointCommit(request_id=request_id, path_id=old_path, path_attempt=0, request_attempt=1, source_placement_id=harness.source_id, committed_token_count=committed, committed_prefix_digest="sha256:" + "0" * 64)
   assert harness.agent_b.receive_commit(commit, source_node_id="node-a") is False
   # The fence store holds exactly one publisher: the successor generation.
   fence = harness.fences.current(request_id)
   assert fence["fencing_generation"] == 2 and fence["published_generation"] == 2
   # Repeating the cutover is impossible: coverage ended at the first one.
   assert harness.controller.failure_is_covered(request_id, record.manifest, type("R", (), {"scope": "PLACEMENT", "placement_id": STANDBY_ID})()) is None


def test_surviving_stage_rolls_back_only_the_one_uncommitted_position(tmp_path: Path) -> None:
   from mycelium_router.kv_checkpoint import KVCheckpointError

   harness = _Harness(tmp_path)
   request_id = "request:a7-rollback"
   sink = InMemoryClientSink()
   harness.entry.start_locked_distributed_prefill(_request(request_id), sink, manifest=harness.manifest(request_id), pinned_deployment=harness.graph)
   harness.entry.decode_one_distributed(request_id)
   state = harness.entry_port.kv_snapshot()["states"][f"path:{request_id}"]
   position = state["next_position"]
   with pytest.raises(KVCheckpointError) as excinfo:
      harness.entry_port.rekey_kv_path(f"path:{request_id}", new_path_id="p:x", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=position - 2)
   assert excinfo.value.code == "kv_checkpoint_position_mismatch"
   receipt = harness.entry_port.rekey_kv_path(f"path:{request_id}", new_path_id="p:y", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=position - 1)
   assert receipt["next_position"] == position - 1


def test_commit_and_release_frames_are_closed_and_bounded() -> None:
   from mycelium_router.wire import WireError

   good = KVCheckpointCommit(request_id="r", path_id="p", path_attempt=0, request_attempt=1, source_placement_id="s", committed_token_count=1, committed_prefix_digest="sha256:" + "a" * 64)
   assert decode_frame(encode_frame(good)).message == good
   for bad in (
      replace(good, committed_token_count=0),
      replace(good, request_attempt=0),
      replace(good, committed_prefix_digest="sha256:short"),
      replace(good, source_placement_id=""),
   ):
      with pytest.raises(WireError):
         encode_frame(bad)
   release = KVShadowRelease(request_id="r", path_id="p", path_attempt=0, reason="fenced")
   assert decode_frame(encode_frame(release)).message == release
   with pytest.raises(WireError):
      encode_frame(replace(release, reason="because"))


def test_standby_dispatch_routes_by_placement_and_merges_evidence(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   runtime = harness.routers["node-a"].relay.runtime
   assert isinstance(runtime, StandbyDispatchRuntime)
   assert runtime.port_for(STANDBY_ID) is harness.standby_port
   assert runtime.port_for(harness.graph.stages[0].placements[0].placement_id) is harness.entry_port
   with pytest.raises(ValueError):
      runtime.offer_batch_member(type("I", (), {"placement_id": STANDBY_ID})(), member_ref="m")
   with pytest.raises(ValueError):
      StandbyDispatchRuntime(harness.entry_port, {STANDBY_ID: harness.entry_port})
   snapshot = runtime.kv_snapshot()
   assert snapshot["standby_placement_count"] == 1 and snapshot["active_state_count"] == 0
   assert runtime.kv_subject_clean("r", "p", 0) is True


def test_owner_cancel_landing_before_the_terminal_release_still_frees_the_standby(tmp_path: Path) -> None:
   """The owner's controlled cancel can reach the entry/standby node just before
   the entry's terminal ``KVShadowRelease``.  The release must still be
   delivered through the real transport and free the standby's shadow chain."""

   from mycelium_router.contracts import PathCancellation
   from mycelium_router.transports.iroh import IrohTransport, PeerBinding
   from test_router_iroh_transport import _Hub

   harness = _Harness(tmp_path)
   transport = IrohTransport(
      node_id="node-a",
      socket_path="/unused",
      bootstrap_secret=b"s" * 32,
      peer=PeerBinding(node_id="node-b", endpoint_id="peer-endpoint", endpoint_addr={"id": "peer-endpoint", "addrs": ["127.0.0.1:1"]}, generation=7),
      expected_endpoint_id="local-endpoint",
      queue_capacity=8,
      delivery_timeout_seconds=0.5,
      poll_interval_seconds=0.01,
      client_factory=_Hub().client,
   )
   transport.bind_router(harness.routers["node-a"])
   net_send = harness.controller._send
   cancelled: list[str] = []

   def send(destination: str, message, payload: bytes) -> None:
      if isinstance(message, KVShadowRelease) and destination == "node-a":
         # Inject the ordering: the owner's fanned-out cancel wins first.
         with transport._state_lock:
            transport._remember_cancellation_locked(
               PathCancellation(message.request_id, message.path_id, message.path_attempt, harness.graph.topology_version)
            )
            transport._remember_controlled_cancellation_locked(message.request_id)
         cancelled.append(message.request_id)
         transport.send_a7_message(destination, message, payload)
         return
      net_send(destination, message, payload)

   harness.controller._send = send
   harness.net.drop.add("KVShadowRelease")  # node-b's copy is irrelevant here
   transport.start()
   try:
      harness.run("request:a7-cancel-first", kill_after=None)
      assert harness.entry.request_status("request:a7-cancel-first") == "COMPLETED"
      assert harness.net.delivered.count("KVCheckpointShipment") >= 1
      assert cancelled == ["request:a7-cancel-first"]
      assert harness.shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
      assert harness.controller.release_failures() == {}
   finally:
      transport.close()


def test_undelivered_remote_releases_are_retried_then_recorded(tmp_path: Path) -> None:
   import time as _time

   harness = _Harness(tmp_path)
   harness.controller._release_async = True
   net_send = harness.controller._send
   attempts: dict[str, int] = {"node-b": 0}
   done = __import__("threading").Event()

   class _Unreachable(Exception):
      code = "path_cancelled"

   def send(destination: str, message, payload: bytes) -> None:
      if isinstance(message, KVShadowRelease) and destination == "node-b":
         attempts["node-b"] += 1
         if attempts["node-b"] >= 3:
            done.set()
         raise _Unreachable()
      net_send(destination, message, payload)

   harness.controller._send = send
   harness.run("request:a7-release-unreachable", kill_after=None)
   assert done.wait(timeout=2.0)
   deadline = _time.monotonic() + 2.0
   while not harness.controller.release_failures() and _time.monotonic() < deadline:
      _time.sleep(0.01)
   assert attempts["node-b"] == 3
   assert harness.controller.release_failures() == {"path_cancelled": 1}
   # The standby's own copy still arrived: nothing is held locally.
   assert harness.shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
