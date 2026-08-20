"""End-to-end and fail-closed tests for the assignment-bound NumPy RuntimePort.

The NumPy port is ``complete_context_replay``: every call carries the entire
context (token ids for the entry stage, hidden-state envelope for every other
stage) and never retains KV state.  Tests therefore exercise graph, load proof,
placement, batch key, and payload validation together with the public
``execute``/``execute_batch``/``cancel``/``close``/``kv_snapshot`` surface and
verify the port does not pull in MLX at any point.
"""

from __future__ import annotations

import importlib
import json
import struct
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from layer_assignment import compile_layer_assignments
from mycelium_router.contracts import (
   ExecutionGraph,
   HopWorkItem,
   LayerRange,
   Placement,
   PlacementEdge,
   RuntimeBatch,
   RuntimeBatchKey,
   Stage,
   StageCost,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.numpy_runtime import (
   NumpyRouterRuntimeError,
   NumpyRuntimePort,
)
from mycelium_router.payloads import (
   decode_activation,
   decode_token_ids,
   encode_activation,
   encode_token_ids,
)
from runtime_contracts import validate_normalized_numpy_runtime
from runtime_loader import LoadedStage, canonical_json, load_assignment_stage
from two_process_runtime_qualification import (
   DEPLOYMENT_EPOCH,
   DEPLOYMENT_ID,
   LOAD_GENERATION,
   _LocalOnlyFetcher,
   _build_local_model,
   _control_plane_binding,
   _route_for_manifest,
)
from weight_provisioning import provision_assignment


def _plain(value):
   return json.loads(canonical_json(value))


def _build_graph(assignments, loaded_stages) -> ExecutionGraph:
   stages = []
   placements = []
   for index, (assignment, loaded) in enumerate(zip(assignments, loaded_stages)):
      stage_id = f"stage-{index:03d}"
      placement = Placement(
         placement_id=f"placement-{index:03d}",
         node_id=assignment["node_id"],
         replica_group_id=f"{stage_id}-replicas",
         assignment_id=assignment["assignment_id"],
         stage_signature="pending-stage-signature",
         load_proof_digest=layer_load_proof_digest(_plain(loaded.proof)),
         runtime_backend=assignment["runtime"]["backend"],
         runtime_endpoint=f"memory://{assignment['node_id']}/{stage_id}",
      )
      layer_range = assignment["range"]
      placements.append(placement)
      stages.append(
         Stage(
            stage_id=stage_id,
            layer_range=LayerRange(
               start_layer=layer_range["start_layer"],
               end_layer_exclusive=layer_range["end_layer_exclusive"],
               layer_count=layer_range["layer_count"],
            ),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
               prefill_work_units_per_prompt_token=1.0,
               decode_work_units_per_token=1.0,
               kv_bytes_per_context_token=32,
            ),
            placements=(placement,),
         )
      )

   graph = ExecutionGraph(
      deployment_id=assignments[0]["deployment_id"],
      deployment_epoch=assignments[0]["deployment_epoch"],
      topology_version=1,
      model_id=assignments[0]["model_id"],
      resolved_commit=assignments[0]["resolved_commit"],
      manifest_digest=assignments[0]["manifest_digest"],
      entry_stage_id=stages[0].stage_id,
      final_stage_id=stages[-1].stage_id,
      hidden_size=assignments[0]["runtime"]["model_config"]["n_embd"],
      activation_bytes=4,
      token_envelope_bytes=9,
      stages=tuple(stages),
      edges=(
         PlacementEdge(
            edge_id="forward:placement-000->placement-001",
            from_placement_id=placements[0].placement_id,
            to_placement_id=placements[1].placement_id,
            link_id="test:node-a->node-b",
         ),
      ),
      loopback_edges=(
         PlacementEdge(
            edge_id="loopback:placement-001->placement-000",
            from_placement_id=placements[1].placement_id,
            to_placement_id=placements[0].placement_id,
            link_id="test:node-b->node-a",
         ),
      ),
   )
   # Stage signatures use the same recipe the router layer_builder relies on
   # for the NumPy backend.  We re-derive them locally to avoid a hard import
   # cycle on the MLX helper.
   from mycelium_router.numpy_runtime import _stage_signature

   signed_stages = tuple(
      replace(
         stage,
         placements=(
            replace(
               stage.placements[0],
               stage_signature=_stage_signature(graph, stage, loaded.proof),
            ),
         ),
      )
      for stage, loaded in zip(graph.stages, loaded_stages)
   )
   return replace(graph, stages=signed_stages)


@pytest.fixture(scope="module")
def runtime_case():
   temporary = tempfile.TemporaryDirectory(prefix="mycelium-numpy-runtime-test-")
   root = Path(temporary.name)
   manifest, _ = _build_local_model(root, n_positions=16)
   route = _route_for_manifest(manifest)
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=manifest,
      deployment_id=DEPLOYMENT_ID,
      deployment_epoch=DEPLOYMENT_EPOCH,
      cache_roots={node: str(root) for node in route["node_order"]},
      runtime_by_node={
         node: {"backend": "numpy", "dtype": "float32", "quantization": "none"}
         for node in route["node_order"]
      },
      control_plane_binding=_control_plane_binding(),
   )
   fetcher = _LocalOnlyFetcher(root)
   reports = [
      provision_assignment(
         assignment,
         fetch_file=fetcher,
         local_files_only=True,
      )
      for assignment in assignments
   ]
   loaded = tuple(
      load_assignment_stage(
         assignment,
         report,
         load_generation=LOAD_GENERATION,
      )
      for assignment, report in zip(assignments, reports)
   )
   graph = _build_graph(assignments, loaded)
   ports = tuple(
      NumpyRuntimePort(
         assignment["node_id"],
         graph,
         {graph.stages[index].placements[0].placement_id: loaded[index]},
      )
      for index, assignment in enumerate(assignments)
   )
   yield SimpleNamespace(
      root=root,
      manifest=manifest,
      assignments=tuple(assignments),
      loaded=loaded,
      graph=graph,
      ports=ports,
   )
   temporary.cleanup()


def _batch_key(case, stage_index, phase, token_span):
   placement = case.graph.stages[stage_index].placements[0]
   return RuntimeBatchKey(
      deployment_id=case.graph.deployment_id,
      deployment_epoch=case.graph.deployment_epoch,
      model_commit=case.graph.resolved_commit,
      manifest_digest=case.graph.manifest_digest,
      placement_id=placement.placement_id,
      assignment_id=placement.assignment_id,
      stage_signature=placement.stage_signature,
      load_proof_digest=placement.load_proof_digest,
      runtime_backend=placement.runtime_backend,
      phase=phase,
      hidden_size=case.graph.hidden_size,
      activation_bytes=case.graph.activation_bytes,
      token_span=token_span,
   )


def _work_item(
   case,
   stage_index,
   payload,
   *,
   phase="PREFILL",
   token_span=3,
   request_id="request-1",
   path_id="path-1",
   batch_key=None,
   token_index=None,
   position=None,
   emits_token=False,
   terminal=False,
   lease_expires_at=None,
   idempotency_key=None,
):
   key = batch_key or _batch_key(case, stage_index, phase, token_span)
   if token_index is None:
      token_index = -1 if phase in {"PREFILL", "RECOVERY_PREFILL"} else 0
   if position is None:
      position = 0
   return HopWorkItem(
      request_id=request_id,
      path_id=path_id,
      path_attempt=0,
      phase=phase,
      token_index=token_index,
      hop_index=stage_index,
      placement_id=case.graph.stages[stage_index].placements[0].placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key=(
         idempotency_key
         if idempotency_key is not None
         else f"{request_id}:{phase}:{token_index}:{stage_index}"
      ),
      payload=payload,
      batch_key=key,
      position=position,
      emits_token=emits_token,
      terminal=terminal,
      lease_expires_at=lease_expires_at,
   )


def _reference_token(case, token_ids):
   """Compute the reference hidden state and argmax token via the loader."""

   from numpy_runtime import execute_loaded_stage

   hidden = execute_loaded_stage(case.loaded[0], token_ids=np.array((token_ids,), dtype=np.int64))
   logits = execute_loaded_stage(case.loaded[1], hidden_states=hidden)
   return np.asarray(hidden), quantized_greedy_token_id(logits[0, -1, :].tolist())


def _run_two_stage(
   case,
   token_ids,
   *,
   phase,
   path_id,
   request_id=None,
   token_index=None,
   position=None,
   emits_token=False,
   terminal=False,
   ports=None,
):
   token_span = (
      len(token_ids)
      if phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}
      else 1
   )
   request_id = request_id or f"request:{path_id}"
   ports = ports or case.ports
   first = ports[0].execute(
      _work_item(
         case,
         0,
         encode_token_ids(token_ids),
         phase=phase,
         token_span=token_span,
         request_id=request_id,
         path_id=path_id,
         token_index=token_index,
         position=position,
         emits_token=emits_token,
         terminal=terminal,
      )
   )
   assert first.success, first.failure_reason
   second = ports[1].execute(
      _work_item(
         case,
         1,
         first.payload,
         phase=phase,
         token_span=token_span,
         request_id=request_id,
         path_id=path_id,
         token_index=token_index,
         position=position,
         emits_token=emits_token,
         terminal=terminal,
      )
   )
   assert second.success, second.failure_reason
   return first.payload, second.token_id


def _fresh_ports(case, *, clock=None):
   return tuple(
      NumpyRuntimePort(
         assignment["node_id"],
         case.graph,
         {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
         clock=clock,
      )
      for index, assignment in enumerate(case.assignments)
   )


def _assert_failure(result, reason):
   assert not result.success
   assert result.failure_scope == "PLACEMENT"
   assert result.failure_reason == reason
   assert result.payload is None
   assert result.token_id is None


# ---------------------------------------------------------------------------
# Determinism + public surface
# ---------------------------------------------------------------------------


def test_numpy_runtime_port_does_not_pull_mlx(runtime_case):
   """A clean process can import the NumPy port without importing MLX."""

   code = (
      "import sys; "
      "import mycelium_router.numpy_runtime; "
      "assert not any(n == 'mlx' or n.startswith('mlx.') for n in sys.modules)"
   )
   completed = subprocess.run(
      [sys.executable, "-c", code],
      cwd=Path(__file__).parent,
      capture_output=True,
      text=True,
      check=False,
   )
   assert completed.returncode == 0, completed.stderr
   fresh = importlib.import_module("mycelium_router.numpy_runtime")
   assert fresh.NumpyRuntimePort is NumpyRuntimePort


def test_decode_mode_and_backend_are_pinned(runtime_case):
   for port in runtime_case.ports:
      assert port.decode_mode == "complete_context_replay"
      assert port.backend == "numpy"


def test_stage_local_kv_rejects_unqualified_architecture(runtime_case):
   placement = runtime_case.graph.stages[0].placements[0]
   with pytest.raises(
      NumpyRouterRuntimeError,
      match="^stage_local_kv_unsupported_architecture$",
   ):
      NumpyRuntimePort(
         placement.node_id,
         runtime_case.graph,
         {placement.placement_id: runtime_case.loaded[0]},
         decode_mode="stage_local_kv",
      )


def test_constructor_rejects_unknown_decode_mode(runtime_case):
   placement = runtime_case.graph.stages[0].placements[0]
   with pytest.raises(NumpyRouterRuntimeError, match="^invalid_runtime_decode_mode$"):
      NumpyRuntimePort(
         placement.node_id,
         runtime_case.graph,
         {placement.placement_id: runtime_case.loaded[0]},
         decode_mode="backend_specific_guess",
      )


def test_complete_context_replay_prefill_matches_reference(runtime_case):
   prompt = (1, 2, 3)
   reference_hidden, reference_prefill_token = _reference_token(runtime_case, prompt)

   activation_payload, prefill_token = _run_two_stage(
      runtime_case,
      prompt,
      phase="PREFILL",
      path_id="path:ccr-prefill",
      request_id="request:ccr-prefill",
      token_index=-1,
      position=0,
   )

   assert prefill_token == reference_prefill_token

   envelope = decode_activation(activation_payload)
   assert envelope.dtype == "float32"
   assert envelope.shape == tuple(int(value) for value in reference_hidden.shape)
   assert envelope.data == bytes(np.ascontiguousarray(reference_hidden))

   for port in runtime_case.ports:
      snapshot = port.kv_snapshot()
      assert snapshot["mode"] == "complete_context_replay"
      assert snapshot["backend"] == "numpy"
      assert snapshot["active_state_count"] == 0
      assert snapshot["closed"] is False


def test_complete_context_replay_decode_produces_deterministic_bytes_and_token(
   runtime_case,
):
   """Every decode call replays the full context; output must be deterministic."""

   context = (1, 2, 3)
   reference_hidden, reference_prefill_token = _reference_token(runtime_case, context)
   _, prefill_token = _run_two_stage(
      runtime_case,
      context,
      phase="PREFILL",
      path_id="path:ccr-decode",
      request_id="request:ccr-decode",
   )
   assert prefill_token == reference_prefill_token

   next_token = (prefill_token,)
   next_context = context + next_token
   _, expected_next_token = _reference_token(runtime_case, next_context)

   payload_a, actual_token_a = _run_two_stage(
      runtime_case,
      next_context,
      phase="DECODE",
      path_id="path:ccr-decode",
      request_id="request:ccr-decode",
      token_index=1,
      position=len(next_context) - 1,
      terminal=True,
   )
   payload_b, actual_token_b = _run_two_stage(
      runtime_case,
      next_context,
      phase="DECODE",
      path_id="path:ccr-decode-dup",
      request_id="request:ccr-decode-dup",
      token_index=1,
      position=len(next_context) - 1,
      terminal=True,
   )
   assert actual_token_a == expected_next_token
   assert actual_token_a == actual_token_b
   assert payload_a == payload_b

   envelope = decode_activation(payload_a)
   reference_intermediate = np.asarray(
      _reference_token(runtime_case, next_context)[0]
   )
   assert envelope.data == bytes(np.ascontiguousarray(reference_intermediate))


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_graph_and_placement_proof_bindings(runtime_case):
   placement_id = runtime_case.graph.stages[0].placements[0].placement_id
   cases = (
      (
         "load_proof_model_id_mismatch",
         replace(runtime_case.graph, model_id="other/model"),
      ),
      (
         "load_proof_deployment_epoch_mismatch",
         replace(
            runtime_case.graph,
            deployment_epoch=runtime_case.graph.deployment_epoch + 1,
         ),
      ),
      (
         "load_proof_digest_mismatch",
         replace(
            runtime_case.graph,
            stages=(
               replace(
                  runtime_case.graph.stages[0],
                  placements=(
                     replace(
                        runtime_case.graph.stages[0].placements[0],
                        load_proof_digest="sha256:" + "f" * 64,
                     ),
                  ),
               ),
               runtime_case.graph.stages[1],
            ),
         ),
      ),
   )

   for reason, graph in cases:
      with pytest.raises(NumpyRouterRuntimeError, match=f"^{reason}"):
         NumpyRuntimePort(
            runtime_case.assignments[0]["node_id"],
            graph,
            {placement_id: runtime_case.loaded[0]},
         )


def test_constructor_rejects_tampered_load_proof(runtime_case):
   proof = _plain(runtime_case.loaded[0].proof)
   proof["assignment_id"] = "attacker-assignment"
   tampered = LoadedStage(
      tensors=runtime_case.loaded[0].tensors,
      resolved_aliases=runtime_case.loaded[0].resolved_aliases,
      probe_output=runtime_case.loaded[0].probe_output,
      proof=proof,
   )
   placement_id = runtime_case.graph.stages[0].placements[0].placement_id

   with pytest.raises(
      NumpyRouterRuntimeError, match="^load_proof_assignment_id_mismatch"
   ):
      NumpyRuntimePort(
         runtime_case.assignments[0]["node_id"],
         runtime_case.graph,
         {placement_id: tampered},
      )


def test_constructor_rejects_non_numpy_backend_marker(runtime_case):
   """A placement that claims 'mlx' on a NumPy-bound port must fail closed."""

   placement_id = runtime_case.graph.stages[0].placements[0].placement_id
   bad_graph = replace(
      runtime_case.graph,
      stages=(
         replace(
            runtime_case.graph.stages[0],
            placements=(
               replace(
                  runtime_case.graph.stages[0].placements[0],
                  runtime_backend="mlx",
               ),
            ),
         ),
         runtime_case.graph.stages[1],
      ),
   )
   with pytest.raises(
      NumpyRouterRuntimeError, match="^runtime_backend_mismatch"
   ):
      NumpyRuntimePort(
         runtime_case.assignments[0]["node_id"],
         bad_graph,
         {placement_id: runtime_case.loaded[0]},
      )


def test_execute_rejects_every_batch_binding_dimension(runtime_case):
   payload = encode_token_ids((1, 2, 3))
   valid = _batch_key(runtime_case, 0, "PREFILL", 3)
   mismatches = {
      "deployment_id": "other-deployment",
      "deployment_epoch": valid.deployment_epoch + 1,
      "model_commit": "other-commit",
      "manifest_digest": "sha256:" + "0" * 64,
      "placement_id": "other-placement",
      "assignment_id": "other-assignment",
      "stage_signature": "sha256:" + "1" * 64,
      "load_proof_digest": "sha256:" + "2" * 64,
      "runtime_backend": "other-backend",
      "phase": "DECODE",
      "hidden_size": valid.hidden_size + 1,
      "activation_bytes": 2,
      "speculative_role": "DRAFT",
      "speculative_width": 1,
   }

   for field, value in mismatches.items():
      key = replace(valid, **{field: value})
      result = runtime_case.ports[0].execute(
         _work_item(runtime_case, 0, payload, batch_key=key)
      )
      _assert_failure(result, f"batch_key_{field}_mismatch")

   wrong_span = replace(valid, token_span=2)
   result = runtime_case.ports[0].execute(
      _work_item(runtime_case, 0, payload, batch_key=wrong_span)
   )
   _assert_failure(result, "batch_key_token_span_mismatch")

   invalid_span = replace(valid, token_span=0)
   result = runtime_case.ports[0].execute(
      _work_item(runtime_case, 0, payload, batch_key=invalid_span)
   )
   _assert_failure(result, "invalid_batch_key_token_span")


def test_execute_rejects_invalid_payloads(runtime_case):
   reference_payload, _ = _run_two_stage(
      runtime_case,
      (1, 2, 3),
      phase="PREFILL",
      path_id="path:payload-validation",
   )

   cases = (
      ("empty_token_sequence", encode_token_ids(())),
      ("position_bounds_exceeded", encode_token_ids(tuple(range(20)))),
      ("token_bounds_exceeded", encode_token_ids((1, 2, 99))),
   )
   for reason, payload in cases:
      result = runtime_case.ports[0].execute(
         _work_item(
            runtime_case,
            0,
            payload,
            phase="PREFILL",
            token_span=len(decode_token_ids(payload)) or 1,
         )
      )
      _assert_failure(result, reason)

   # Activation envelope corruption paths on the intermediate stage.
   malformed = b"NOPE" + reference_payload[4:]
   truncated = reference_payload[:20]
   oversize = bytearray(reference_payload)
   struct.pack_into(">Q", oversize, 8, 268_435_457)
   wrong_dtype = encode_activation(
      dtype="float16",
      shape=(1, 3, 4),
      data=b"\x00" * (1 * 3 * 4 * 2),
   )
   wrong_rank = encode_activation(
      dtype="float32",
      shape=(1, 3),
      data=b"\x00" * (1 * 3 * 4),
   )
   wrong_batch = encode_activation(
      dtype="float32",
      shape=(2, 3, 4),
      data=b"\x00" * (2 * 3 * 4 * 4),
   )
   nonfinite = encode_activation(
      dtype="float32",
      shape=(1, 3, 4),
      data=struct.pack(
         "<12f",
         *[0.0 if index != 5 else float("nan") for index in range(12)]
      ),
   )

   bad_payloads = {
      "invalid_activation_payload_magic": malformed,
      "truncated_activation_shape": truncated,
      "activation_payload_too_large": bytes(oversize),
      "activation_dtype_mismatch": wrong_dtype,
      "activation_rank_mismatch": wrong_rank,
      "activation_shape_mismatch": wrong_batch,
      "nonfinite_activation": nonfinite,
   }
   for reason, payload in bad_payloads.items():
      token_span = 3
      if reason == "activation_rank_mismatch":
         token_span = 3
      key = _batch_key(runtime_case, 1, "PREFILL", token_span)
      result = runtime_case.ports[1].execute(
         HopWorkItem(
            request_id="request:payload-bad",
            path_id="path:payload-bad",
            path_attempt=0,
            phase="PREFILL",
            token_index=-1,
            hop_index=1,
            placement_id=runtime_case.graph.stages[1].placements[0].placement_id,
            qos_class="interactive",
            deficit_ratio=0.0,
            enqueued_at=0.0,
            idempotency_key=f"request:payload-bad:PREFILL:{reason}",
            payload=payload,
            batch_key=key,
            position=0,
         )
      )
      _assert_failure(result, reason)


def test_execute_rejects_prefill_chunk_phase(runtime_case):
   payload = encode_token_ids((1, 2, 3))
   key = _batch_key(runtime_case, 0, "PREFILL", 3)
   item = HopWorkItem(
      request_id="request:prefill-chunk",
      path_id="path:prefill-chunk",
      path_attempt=0,
      phase="PREFILL_CHUNK",
      token_index=-1,
      hop_index=0,
      placement_id=runtime_case.graph.stages[0].placements[0].placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key="request:prefill-chunk:PREFILL_CHUNK:0",
      payload=payload,
      batch_key=key,
      position=0,
   )
   result = runtime_case.ports[0].execute(item)
   _assert_failure(result, "prefill_chunk_requires_kv_continuity")


def test_execute_rejects_decode_position_and_span_anomalies(runtime_case):
   reference_payload, _ = _run_two_stage(
      runtime_case,
      (1, 2, 3),
      phase="PREFILL",
      path_id="path:decode-anomaly",
   )
   key = _batch_key(runtime_case, 1, "DECODE", 1)
   bad_position = HopWorkItem(
      request_id="request:decode-anomaly",
      path_id="path:decode-anomaly",
      path_attempt=0,
      phase="DECODE",
      token_index=1,
      hop_index=1,
      placement_id=runtime_case.graph.stages[1].placements[0].placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key="request:decode-anomaly:DECODE:1:position",
      payload=reference_payload,
      batch_key=key,
      position=0,
   )
   _assert_failure(runtime_case.ports[1].execute(bad_position), "decode_position_mismatch")

   bad_token_index = HopWorkItem(
      request_id="request:decode-anomaly",
      path_id="path:decode-anomaly",
      path_attempt=0,
      phase="DECODE",
      token_index=-1,
      hop_index=1,
      placement_id=runtime_case.graph.stages[1].placements[0].placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key="request:decode-anomaly:DECODE:1:index",
      payload=reference_payload,
      batch_key=key,
      position=2,
   )
   _assert_failure(
      runtime_case.ports[1].execute(bad_token_index), "decode_sequence_mismatch"
   )


# ---------------------------------------------------------------------------
# Lifecycle: cancel, close, kv_snapshot, execute_batch, replay
# ---------------------------------------------------------------------------


def test_cancel_idempotently_rejects_subsequent_work(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   port.cancel("path:cancel-then-execute")
   result = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
         path_id="path:cancel-then-execute",
         request_id="request:cancel-then-execute",
      )
   )
   _assert_failure(result, "path_cancelled")

   # Idempotent: a second cancel must not raise.
   port.cancel("path:cancel-then-execute")
   port.cancel("not-a-string-passes-silently")  # type: ignore[arg-type]


def test_close_idempotently_releases_state_and_rejects_work(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   port.close(reason="worker_shutdown")
   port.close(reason="worker_shutdown")  # idempotent
   snapshot = port.kv_snapshot()
   assert snapshot["closed"] is True
   assert snapshot["release_counts"]["worker_shutdown"] == 1

   result = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
      )
   )
   _assert_failure(result, "runtime_closed")

   with pytest.raises(NumpyRouterRuntimeError, match="^invalid_runtime_close_reason"):
      port.close(reason="")


def test_execute_batch_serializes_items_in_order(runtime_case):
   items = (
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
         path_id="path:batch-1",
         request_id="request:batch-1",
         idempotency_key="request:batch-1:PREFILL:-1:0",
      ),
      _work_item(
         runtime_case,
         0,
         encode_token_ids((3, 4)),
         phase="PREFILL",
         token_span=2,
         path_id="path:batch-2",
         request_id="request:batch-2",
         idempotency_key="request:batch-2:PREFILL:-1:0",
      ),
   )
   batch = RuntimeBatch(compatibility_key=items[0].batch_key, items=items)
   results = _fresh_ports(runtime_case)[0].execute_batch(batch)
   assert len(results) == 2
   assert all(result.success for result in results)
   assert all(results[index].payload is not None for index in range(2))
   # Bytes must differ: the inputs differ.
   assert results[0].payload != results[1].payload

   with pytest.raises(NumpyRouterRuntimeError, match="^invalid_runtime_batch"):
      _fresh_ports(runtime_case)[0].execute_batch("not-a-batch")  # type: ignore[arg-type]


def test_kv_snapshot_reports_lifecycle_counters(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL",
         token_span=2,
         path_id="path:snapshot",
         request_id="request:snapshot",
         idempotency_key="request:snapshot:PREFILL:-1:0",
      )
   )
   snapshot = port.kv_snapshot()
   assert snapshot["mode"] == "complete_context_replay"
   assert snapshot["backend"] == "numpy"
   assert snapshot["closed"] is False
   assert snapshot["active_state_count"] == 0
   assert snapshot["applied_operation_count"] == 1
   assert snapshot["cancelled_path_count"] == 0


def test_replay_rejects_inconsistent_payloads(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   path_id = "path:replay-conflict"
   first = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2, 3)),
         phase="PREFILL",
         token_span=3,
         path_id=path_id,
         request_id="request:replay-conflict",
      )
   )
   assert first.success
   conflict = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((4, 5, 6)),
         phase="PREFILL",
         token_span=3,
         path_id=path_id,
         request_id="request:replay-conflict",
      )
   )
   _assert_failure(conflict, "replay_fingerprint_mismatch")


# ---------------------------------------------------------------------------
# numpy_runtime canonical-contract smoke checks
# ---------------------------------------------------------------------------


def test_validate_normalized_numpy_runtime_accepts_numpy_manifest() -> None:
   runtime = {
      "backend": "numpy",
      "dtype": "float32",
      "quantization": "none",
      "architecture": "gpt2",
      "model_config": {
         "n_layer": 1,
         "n_embd": 4,
         "n_head": 2,
         "n_inner": 16,
         "vocab_size": 7,
         "n_positions": 8,
         "layer_norm_epsilon": 1e-5,
         "activation_function": "gelu_new",
         "scale_attn_weights": True,
         "scale_attn_by_inverse_layer_idx": False,
         "reorder_and_upcast_attn": False,
         "add_cross_attention": False,
      },
   }
   assert validate_normalized_numpy_runtime(runtime)["backend"] == "numpy"
