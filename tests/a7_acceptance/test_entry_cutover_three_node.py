# SPDX-License-Identifier: AGPL-3.0-only
"""A7 fenced cutover on a three-node path, including a final-decode-step source loss.

The two-node slice (``test_entry_cutover.py``) only has a surviving stage on the
entry node itself.  Here every A7 message crosses three in-process Routers with
real NumPy stage runtimes over a three-layer model:

* node-a is the entry (stage 0) and holds the STANDBY for the source's stage;
* node-c is a surviving stage on a node that is neither entry, source nor
  standby, so its fence-install is authorised only through its own relay's
  registration of the old path's entry;
* node-b is the source and is lost.

``source_last`` puts the source on the final stage: during the final decode
step node-c has already executed the terminal hop, so the cutover can only
succeed by rekeying node-c's retained terminal KV, which requires node-c's relay
to still know the old path's entry.  ``source_middle`` puts node-c after the
source (it never sees the final step, so its KV is still live).  Both run with a
mid-request loss and a final-step loss; the output must equal the uninterrupted
reference with every index delivered once, and every port (retained terminal KV
included) must be empty afterwards.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
import numpy as np
import pytest

import test_router_numpy_qwen_kv as kv_fixture
from layer_assignment import compile_layer_assignments
from model_manifest import compile_model_manifest, manifest_digest_ref
from mycelium_a7_checkpoint_channel import StandbyCheckpointKey
from mycelium_a7_entry_controller import A7EntryController, StandbyCoverage, stage_digest
from mycelium_a7_fence_store import DurableFenceStore
from mycelium_a7_node_agent import A7NodeAgent, SourceCoverage
from mycelium_a7_node_wiring import enable_terminal_kv_retention
from mycelium_a7_standby_qualification import parity_challenge_result, port_placement_identity, qualify_standby
from mycelium_a7_standby_store import StandbyShadowStore
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_qualification.signing import build_ed25519_verifier, generate_ed25519_signer
from mycelium_router.contracts import PathHop, PathManifest, PlacementEdge, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeTopologyProvider,
   InMemoryClientSink,
   InProcessMesh,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.router import Router
from mycelium_router.standby_dispatch import StandbyDispatchRuntime
from numpy_runtime import execute_loaded_stage
from runtime_loader import load_assignment_stage
from test_router_numpy_runtime import _build_graph, _plain
from two_process_runtime_qualification import _control_plane_binding
from weight_provisioning import provision_assignment, sha256_file

from tests.a7_acceptance.test_entry_cutover import _A7Net, _Killable, _request

_LEASE = 1_000_000_000_000.0
_MAX = 1 << 20
_PROMPT = (1, 7, 11, 5)
_MAX_NEW = 6
_NOW_MS = 1_800_000_000_000
_LAYERS = 3
_SHARDS = ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")
_STANDBY_ID = "placement-standby"
_ENTRY_ID = "placement-000"


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _three_layer_tensors() -> dict[str, np.ndarray]:
   tensors = kv_fixture._tensors("qwen2")
   rng = np.random.default_rng(20261007)
   for key, value in list(tensors.items()):
      if key.startswith("model.layers.1."):
         suffix = key[len("model.layers.1.") :]
         if suffix.endswith("norm.weight"):
            layer2 = rng.normal(1.0, 0.02, value.shape)
         elif suffix.endswith(".bias"):
            layer2 = rng.normal(0.0, 0.005, value.shape)
         else:
            layer2 = rng.normal(0.0, 0.04, value.shape)
         tensors[f"model.layers.2.{suffix}"] = layer2.astype(np.float32)
   return tensors


def _route(model_id: str, manifest, nodes: tuple[str, ...]) -> dict:
   return {
      "ok": True,
      "protocol": "mycelium.manual_provisioning_route.v1",
      "claim_boundary": "offline A7 three-node NumPy KV fixture",
      "model": {
         "model_id": model_id,
         "num_layers": _LAYERS,
         "manifest_digest": manifest_digest_ref(manifest),
         "resolved_commit": kv_fixture._COMMIT,
      },
      "route": [
         {"node_id": node, "range": {"start_layer": index, "end_layer_exclusive": index + 1, "layer_count": 1}}
         for index, node in enumerate(nodes)
      ],
      "node_order": list(nodes),
   }


def _three_node_case(root: Path, *, source_stage: int) -> SimpleNamespace:
   """Three single-layer stages; ``source_stage`` is on node-b, its standby on node-a."""

   model_id = "local/tiny-qwen2-kv"
   tensors = _three_layer_tensors()
   shards = (
      {
         "model.embed_tokens.weight": tensors["model.embed_tokens.weight"],
         **{key: value for key, value in tensors.items() if key.startswith("model.layers.0.")},
      },
      {
         **{key: value for key, value in tensors.items() if key.startswith(("model.layers.1.", "model.layers.2."))},
         "model.norm.weight": tensors["model.norm.weight"],
         "lm_head.weight": tensors["lm_head.weight"],
      },
   )
   for name, shard in zip(_SHARDS, shards):
      mx.save_safetensors(str(root / name), {key: mx.array(value) for key, value in shard.items()})
   manifest = compile_model_manifest(
      model_id=model_id,
      requested_revision="offline-generated",
      resolved_commit=kv_fixture._COMMIT,
      config=dict(kv_fixture._config("qwen2"), num_hidden_layers=_LAYERS),
      checkpoint_index={"weight_map": {key: name for name, shard in zip(_SHARDS, shards) for key in sorted(shard)}},
      file_metadata={name: {"size_bytes": (root / name).stat().st_size, "sha256": sha256_file(root / name)} for name in _SHARDS},
   )
   nodes = tuple("node-b" if index == source_stage else ("node-a" if index == 0 else "node-c") for index in range(_LAYERS))
   binding = {**_control_plane_binding(), "deployment_id": kv_fixture._DEPLOYMENT_ID}
   runtime = {"backend": "numpy", "dtype": "float32", "quantization": "none"}

   def compile_for(route_nodes: tuple[str, ...]):
      return compile_layer_assignments(
         route_plan=_route(model_id, manifest, route_nodes),
         manifest=manifest,
         deployment_id=kv_fixture._DEPLOYMENT_ID,
         deployment_epoch=1,
         cache_roots={node: str(root) for node in route_nodes},
         runtime_by_node={node: runtime for node in route_nodes},
         control_plane_binding=binding,
      )

   fetcher = kv_fixture._LocalFetcher(root, model_id)

   def load(assignment):
      report = provision_assignment(assignment, fetch_file=fetcher, local_files_only=True)
      return load_assignment_stage(assignment, report, load_generation=23)

   assignments = compile_for(nodes)
   loaded = tuple(load(assignment) for assignment in assignments)
   # The standby is independently provisioned for the source's layer, bound to node-a.
   standby_nodes = tuple("node-a" if index == source_stage else f"node-standby-{index}" for index in range(_LAYERS))
   standby_assignment = compile_for(standby_nodes)[source_stage]
   standby_loaded = load(standby_assignment)

   graph = _build_graph(assignments, loaded)
   placements = [stage.placements[0] for stage in graph.stages]
   edges = tuple(
      PlacementEdge(
         edge_id=f"forward:{a.placement_id}->{b.placement_id}",
         from_placement_id=a.placement_id,
         to_placement_id=b.placement_id,
         link_id=f"test:{a.node_id}->{b.node_id}",
      )
      for a, b in zip(placements, placements[1:])
   )
   last, first = placements[-1], placements[0]
   loopbacks = (
      PlacementEdge(
         edge_id=f"loopback:{last.placement_id}->{first.placement_id}",
         from_placement_id=last.placement_id,
         to_placement_id=first.placement_id,
         link_id=f"test:{last.node_id}->{first.node_id}",
      ),
   )
   source = placements[source_stage]
   standby = replace(
      source,
      placement_id=_STANDBY_ID,
      node_id="node-a",
      assignment_id=standby_assignment["assignment_id"],
      load_proof_digest=layer_load_proof_digest(_plain(standby_loaded.proof)),
      lifecycle_state="STANDBY",
      runtime_endpoint="memory://node-a/standby",
   )
   before = placements[source_stage - 1]
   edges += (
      PlacementEdge(
         edge_id=f"forward:{before.placement_id}->{_STANDBY_ID}",
         from_placement_id=before.placement_id,
         to_placement_id=_STANDBY_ID,
         link_id=f"test:{before.node_id}->node-a",
      ),
   )
   if source_stage == _LAYERS - 1:
      loopbacks += (
         PlacementEdge(
            edge_id=f"loopback:{_STANDBY_ID}->{first.placement_id}",
            from_placement_id=_STANDBY_ID,
            to_placement_id=first.placement_id,
            link_id="test:node-a->node-a",
         ),
      )
   else:
      after = placements[source_stage + 1]
      edges += (
         PlacementEdge(
            edge_id=f"forward:{_STANDBY_ID}->{after.placement_id}",
            from_placement_id=_STANDBY_ID,
            to_placement_id=after.placement_id,
            link_id=f"test:node-a->{after.node_id}",
         ),
      )
   stages = tuple(
      replace(stage, placements=(stage.placements[0], standby)) if index == source_stage else stage
      for index, stage in enumerate(graph.stages)
   )
   graph = replace(graph, stages=stages, edges=edges, loopback_edges=loopbacks)
   return SimpleNamespace(graph=graph, loaded=loaded, standby_loaded=standby_loaded, nodes=nodes)


def _reference_tokens(case) -> list[int]:
   generated: list[int] = []
   for _ in range(_MAX_NEW):
      hidden = None
      tokens = np.array((_PROMPT + tuple(generated),), dtype=np.int64)
      for index, stage in enumerate(case.loaded):
         hidden = execute_loaded_stage(stage, token_ids=tokens) if index == 0 else execute_loaded_stage(stage, hidden_states=hidden)
      generated.append(int(np.argmax(hidden[0, -1])))
   return generated


class _ThreeNodeHarness:
   def __init__(self, tmp_path: Path, *, source_stage: int) -> None:
      case = _three_node_case(tmp_path, source_stage=source_stage)
      graph = case.graph
      self.case, self.graph = case, graph
      self.source_stage = source_stage
      self.source_id = graph.stages[source_stage].placements[0].placement_id
      self.survivor_stage = next(index for index in range(1, _LAYERS) if index != source_stage)
      self.survivor_id = graph.stages[self.survivor_stage].placements[0].placement_id
      clock = ManualClock()

      def port(node_id: str, placement_id: str, loaded):
         return NumpyRuntimePort(node_id, graph, {placement_id: loaded}, decode_mode="stage_local_kv", clock=clock.now)

      self.entry_port = port("node-a", _ENTRY_ID, case.loaded[0])
      self.source_port = _Killable(port("node-b", self.source_id, case.loaded[source_stage]))
      self.survivor_port = port("node-c", self.survivor_id, case.loaded[self.survivor_stage])
      self.standby_port = port("node-a", _STANDBY_ID, case.standby_loaded)
      # As A7NodeWiring.attach does on every A7-wired node.
      assert enable_terminal_kv_retention((self.entry_port, self.source_port.port, self.survivor_port, self.standby_port)) == 4

      mesh = InProcessMesh()
      self.net = _A7Net()
      self.routers: dict[str, Router] = {}
      runtimes = {
         "node-a": StandbyDispatchRuntime(self.entry_port, {_STANDBY_ID: self.standby_port}),
         "node-b": self.source_port,
         "node-c": self.survivor_port,
      }
      for node_id, runtime in runtimes.items():
         router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider({}),
            capacity=FakeCapacityPort(),
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
      placement_digest = _digest(f"placement:{_STANDBY_ID}")
      incarnation = _digest("incarnation:node-a:1")
      self.shadow = StandbyShadowStore(max_entry_bytes=_MAX, max_entries=64)
      standby = StandbySide(
         port=self.standby_port,
         placement_id=_STANDBY_ID,
         placement_digest=placement_digest,
         incarnation=incarnation,
         signer=signer,
         key=StandbyCheckpointKey(signer=signer, standby_placement_id=placement_digest, standby_membership_incarnation=incarnation),
         store=self.shadow,
         max_checkpoint_bytes=_MAX,
      )
      self.agents = {node: A7NodeAgent(node, send=self.net.sender(node), clock_ms=lambda: _NOW_MS) for node in runtimes}
      self.agents["node-a"].register_standby(standby, session_digest=_digest("session"))
      self.agents["node-a"].register_port(_ENTRY_ID, self.entry_port)
      self.agents["node-c"].register_port(self.survivor_id, self.survivor_port)
      stage_id = graph.stages[source_stage].stage_id
      self.agents["node-b"].register_source_coverage(
         self.source_id,
         SourceCoverage(
            stage_id=stage_digest(stage_id),
            port=self.source_port.port,
            standby_node_id="node-a",
            standby_placement_id=_STANDBY_ID,
            standby_placement_digest=placement_digest,
            standby_incarnation=incarnation,
            standby_key_record=standby.key.public_record(),
            verify_standby_signature=verifier,
            source_placement_digest=_digest(f"placement:{self.source_id}"),
            max_bytes=_MAX,
         ),
      )
      self.agents["node-b"].register_port(self.source_id, self.source_port.port)
      for node_id, agent in self.agents.items():
         self.routers[node_id].a7_agent = agent

      qualification = qualify_standby(
         source_identity=port_placement_identity(self.source_port.port, self.source_id),
         standby_identity=port_placement_identity(self.standby_port, _STANDBY_ID),
         parity_challenge=parity_challenge_result(challenge_id="setup", next_position=len(_PROMPT), source_output=b"same", standby_output=b"same"),
         reserved_kv_bytes=64 * 1024,
         available_bytes=1 << 30,
         issued_at_unix_ms=_NOW_MS - 1_000,
         ttl_ms=600_000,
      )
      self.fences = DurableFenceStore(tmp_path / "fences")
      self.controller = A7EntryController(
         node_id="node-a",
         agent=self.agents["node-a"],
         send=self.net.sender("node-a"),
         fence_store=self.fences,
         coverage=[
            StandbyCoverage(
               stage_id=stage_id,
               source_placement_id=self.source_id,
               source_node_id="node-b",
               standby_placement_id=_STANDBY_ID,
               standby_node_id="node-a",
               standby_incarnation=incarnation,
               verify_standby_signature=verifier,
               qualification=qualification,
            )
         ],
         a6_available=True,
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


def _retention(port) -> dict:
   return port.kv_snapshot()["terminal_kv_retention"]


@pytest.mark.parametrize("source_stage", [pytest.param(2, id="source_last"), pytest.param(1, id="source_middle")])
@pytest.mark.parametrize("kill_after", [pytest.param(3, id="mid_request"), pytest.param(_MAX_NEW - 1, id="final_decode_step")])
def test_three_node_source_loss_cuts_over_through_a_non_entry_survivor(tmp_path: Path, source_stage: int, kill_after: int) -> None:
   harness = _ThreeNodeHarness(tmp_path, source_stage=source_stage)
   request_id = f"request:a7-three-node-{source_stage}-{kill_after}"
   reference = _reference_tokens(harness.case)

   sink = harness.run(request_id, kill_after=kill_after)

   record = harness.entry.entry.get_request(request_id)
   result = record.kv_successor_cutover
   assert result is not None and result["outcome"] == "fenced_kv_successor", result
   assert harness.entry.request_status(request_id) == "COMPLETED"
   assert result["kv_continuity_claimed"] is True and result["recovery_prefill"] is False
   assert result["gateway_committed_token_count"] == kill_after == result["acknowledged_token_count"]
   assert record.kv_successor_recovery is None  # the A6 replay seam never ran
   assert sink.token_ids == reference
   assert sink.token_indexes == list(range(_MAX_NEW))
   assert record.manifest.ordered_hops[source_stage].placement_id == _STANDBY_ID
   assert record.manifest.ordered_hops[harness.survivor_stage].placement_id == harness.survivor_id
   standby = harness.standby_port.kv_snapshot()
   assert standby["prefill_operation_count"] == 0 and standby["decode_operation_count"] == _MAX_NEW - kill_after
   # node-c (neither entry, source nor standby) installed its fence and kept
   # decoding on the successor path from its own KV: only the original
   # chunked prefill (chunk size 2), no recovery prefill.
   assert harness.survivor_port.kv_snapshot()["prefill_operation_count"] == len(_PROMPT) // 2
   assert harness.net.delivered.count("KVSuccessorCutoverReceipt") == 3
   final_step = kill_after == _MAX_NEW - 1
   # Survivors before the source had already executed the final step's terminal
   # hop: the rekey could only take their retained terminal KV.
   assert _retention(harness.entry_port)["reclaimed_by_rekey_total"] == (1 if final_step else 0)
   assert _retention(harness.survivor_port)["reclaimed_by_rekey_total"] == (1 if final_step and source_stage == 2 else 0)
   # Exact cleanup on every surviving node, retained terminal KV included.
   assert harness.shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
   assert harness.fences.current(request_id) is None
   for port in (harness.entry_port, harness.survivor_port, harness.standby_port):
      assert port.kv_snapshot()["active_state_count"] == 0
      assert _retention(port)["retained_count"] == 0


def test_three_node_survivor_relay_still_knows_the_old_path_entry_at_the_final_step(tmp_path: Path) -> None:
   """The precondition the hardening relies on, observed directly on node-c."""

   harness = _ThreeNodeHarness(tmp_path, source_stage=2)
   request_id = "request:a7-three-node-relay-entry"
   seen: list[str | None] = []
   relay = harness.routers["node-c"].relay
   original = harness.agents["node-c"].receive_cutover

   def observe(cutover, **kwargs):
      seen.append(relay.entry_node_for_path(cutover.old_path_id))
      assert kwargs.get("path_entry_node_id") == "node-a"
      return original(cutover, **kwargs)

   harness.agents["node-c"].receive_cutover = observe
   harness.run(request_id, kill_after=_MAX_NEW - 1)
   assert seen == ["node-a"]
   assert harness.entry.entry.get_request(request_id).kv_successor_cutover["outcome"] == "fenced_kv_successor"
   # The retained KV went to that authenticated cutover and nothing is left.
   assert _retention(harness.survivor_port)["reclaimed_by_rekey_total"] == 1
   assert _retention(harness.survivor_port)["retained_count"] == 0
   assert harness.survivor_port.kv_snapshot()["active_state_count"] == 0
