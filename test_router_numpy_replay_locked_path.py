# SPDX-License-Identifier: AGPL-3.0-only
"""Governed locked-path inference in complete-context replay mode.

Product requests enter through ``start_locked_distributed_prefill``: the owner
has already locked the exact path, and the entry streams the prompt as
``PREFILL_CHUNK`` hops.  Complete-context replay keeps no stage-local KV, so a
multi-chunk prefill cannot be continued; the node pins replay routers to
``prefill_chunk_size_tokens=0`` so the whole prompt travels as one chunk at
position zero.  The runtimes accept exactly that full-context chunk in replay
mode, count it as prefill work, and retain no KV; any other chunk is still
rejected with ``prefill_chunk_requires_kv_continuity``.
Every generated token (including the first) is then produced by a decode hop
that replays the complete context, so the tokens must equal the tokens the
stage-local KV path produces for the same prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_router.contracts import (
   PathHop,
   PathManifest,
   RequestContext,
   RouterConfig,
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
from test_router_numpy_qwen_kv import _case, _reference


_LEASE = 1_000_000_000_000.0
_PROMPT = (1, 7, 11, 5, 3)
_MAX_NEW_TOKENS = 3


def _ports(case, decode_mode: str) -> dict[str, NumpyRuntimePort]:
   return {
      assignment["node_id"]: NumpyRuntimePort(
         assignment["node_id"],
         case.graph,
         {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
         decode_mode=decode_mode,
      )
      for index, assignment in enumerate(case.assignments)
   }


def _mesh(case, ports, *, chunk_size: int) -> dict[str, Router]:
   mesh = InProcessMesh()
   clock = ManualClock()
   capacity = FakeCapacityPort()
   routers = {}
   for node_id, port in ports.items():
      router = Router(
         node_id=node_id,
         topology=FakeTopologyProvider(case.graph),
         device_states=FakeDeviceStateProvider({}),
         capacity=capacity,
         runtime=port,
         transport=mesh.transport_for(node_id),
         clock=clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(prefill_chunk_size_tokens=chunk_size),
      )
      mesh.register_router(node_id, router)
      routers[node_id] = router
   return routers


def _request(request_id: str) -> RequestContext:
   return RequestContext(
      request_id=request_id,
      prompt_token_ids=_PROMPT,
      max_new_tokens=_MAX_NEW_TOKENS,
      expected_new_tokens=_MAX_NEW_TOKENS,
      qos_class="interactive",
      admitted_at=0.0,
      target_ttft_ms=1_000.0,
      target_tpot_ms=100.0,
      target_tokens_per_second=10.0,
      sampling_seed=0,
      generation_config_digest="sha256:" + "c" * 64,
   )


def _manifest(case, request_id: str) -> PathManifest:
   graph = case.graph
   return PathManifest(
      path_id=f"path:{request_id}",
      path_attempt=0,
      request_id=request_id,
      deployment_id=graph.deployment_id,
      deployment_epoch=graph.deployment_epoch,
      topology_version=graph.topology_version,
      manifest_digest=graph.manifest_digest,
      ordered_hops=tuple(
         PathHop(
            stage.stage_id,
            stage.placements[0].placement_id,
            f"reservation-{request_id}-{index}",
            _LEASE,
            graph.deployment_epoch,
         )
         for index, stage in enumerate(graph.stages)
      ),
      loopback_edge_id=graph.loopback_edges[0].edge_id,
   )


def _run_locked(case, decode_mode: str, *, chunk_size: int | None = None):
   if chunk_size is None:
      # Mirror PhysicalNodeService._bind_router_config_to_runtime (replay: 0).
      chunk_size = 2 if decode_mode == "stage_local_kv" else 0
   ports = _ports(case, decode_mode)
   routers = _mesh(case, ports, chunk_size=chunk_size)
   request_id = f"request-locked-{decode_mode}"
   sink = InMemoryClientSink()
   entry = routers["node-a"]
   entry.start_locked_distributed_prefill(
      _request(request_id),
      sink,
      manifest=_manifest(case, request_id),
      pinned_deployment=case.graph,
   )
   status = entry.request_status(request_id)
   for _ in range(_MAX_NEW_TOKENS + 1):
      if status != "DECODING":
         break
      if not entry.decode_one_distributed(request_id):
         break
      status = entry.request_status(request_id)
   return status, tuple(sink.token_ids), ports


def _expected_tokens(case) -> tuple[int, ...]:
   context = list(_PROMPT)
   tokens = []
   for _ in range(_MAX_NEW_TOKENS):
      token = _reference(case, tuple(context))
      tokens.append(token)
      context.append(token)
   return tuple(tokens)


@pytest.fixture(scope="module")
def qwen_case(tmp_path_factory):
   return _case(tmp_path_factory.mktemp("replay-locked"), "qwen2")


def test_replay_locked_path_yields_the_same_tokens_as_stage_local_kv(qwen_case):
   kv_status, kv_tokens, _ = _run_locked(qwen_case, "stage_local_kv")
   replay_status, replay_tokens, _ = _run_locked(qwen_case, "complete_context_replay")

   assert kv_status == "COMPLETED"
   assert len(kv_tokens) == _MAX_NEW_TOKENS
   assert kv_tokens == _expected_tokens(qwen_case)
   assert replay_status == "COMPLETED"
   assert replay_tokens == kv_tokens


def test_replay_locked_path_counts_one_full_context_prefill_and_replayed_decodes(
   qwen_case,
):
   status, tokens, ports = _run_locked(qwen_case, "complete_context_replay")

   assert status == "COMPLETED"
   assert len(tokens) == _MAX_NEW_TOKENS
   # Every decode replays the whole context: prompt, then prompt + 1, ...
   replayed_context = sum(len(_PROMPT) + index for index in range(_MAX_NEW_TOKENS))
   for port in ports.values():
      snapshot = port.kv_snapshot()
      assert snapshot["mode"] == "complete_context_replay"
      # Replay keeps no stage-local KV at any point.
      assert snapshot["active_state_count"] == 0
      assert snapshot["peak_kv_bytes"] == 0
      assert snapshot["prefill_operation_count"] == 1
      assert snapshot["prefill_input_token_count"] == len(_PROMPT)
      assert snapshot["decode_operation_count"] == _MAX_NEW_TOKENS
      assert snapshot["decode_input_token_count"] == replayed_context
      assert snapshot["applied_operation_count"] == (
         snapshot["prefill_operation_count"] + snapshot["decode_operation_count"]
      )
      (placement,) = snapshot["placement_counters"].values()
      assert placement["prefill_operation_count"] == 1
      assert placement["decode_operation_count"] == _MAX_NEW_TOKENS


def test_stage_local_kv_locked_path_still_streams_prefill_chunks(qwen_case):
   """Negative control: kv mode keeps chunked prefill with KV continuity."""

   status, tokens, ports = _run_locked(qwen_case, "stage_local_kv", chunk_size=2)

   assert status == "COMPLETED"
   assert len(tokens) == _MAX_NEW_TOKENS
   expected_chunks = -(-len(_PROMPT) // 2)
   for port in ports.values():
      snapshot = port.kv_snapshot()
      assert snapshot["prefill_operation_count"] == expected_chunks
      assert snapshot["decode_operation_count"] == _MAX_NEW_TOKENS - 1
      assert snapshot["peak_kv_bytes"] > 0


def test_replay_locked_path_still_rejects_a_partial_prefill_chunk(qwen_case):
   """Negative control: replay never continues a chunked prefill.

   A replay router configured to split the prompt sends a first chunk that is
   not the whole prompt; the runtime must refuse it (no KV continuity), and
   the request must end FAILED with no tokens and no applied work.
   """

   status, tokens, ports = _run_locked(
      qwen_case, "complete_context_replay", chunk_size=2
   )

   assert status == "FAILED"
   assert tokens == ()
   for port in ports.values():
      snapshot = port.kv_snapshot()
      assert snapshot["applied_operation_count"] == 0
      assert snapshot["prefill_operation_count"] == 0
      assert snapshot["decode_operation_count"] == 0
