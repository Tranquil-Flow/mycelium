"""End-to-end and fail-closed tests for the assignment-bound MLX RuntimePort."""

from __future__ import annotations

import json
import math
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
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
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.mlx_runtime import MLXRuntimeError, MLXRuntimePort, _stage_signature
from mycelium_router.payloads import decode_activation, encode_activation, encode_token_ids
from runtime_loader import LoadedStage, canonical_json, execute_loaded_stage, load_assignment_stage
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
   temporary = tempfile.TemporaryDirectory(prefix="mycelium-mlx-runtime-test-")
   root = Path(temporary.name)
   manifest, _ = _build_local_model(root)
   route = _route_for_manifest(manifest)
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=manifest,
      deployment_id=DEPLOYMENT_ID,
      deployment_epoch=DEPLOYMENT_EPOCH,
      cache_roots={node: str(root) for node in route["node_order"]},
      runtime_by_node={
         node: {"backend": "mlx", "dtype": "float32", "quantization": "none"}
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
      MLXRuntimePort(
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
):
   key = batch_key or _batch_key(case, stage_index, phase, token_span)
   return HopWorkItem(
      request_id=request_id,
      path_id=path_id,
      path_attempt=0,
      phase=phase,
      token_index=0,
      hop_index=stage_index,
      placement_id=case.graph.stages[stage_index].placements[0].placement_id,
      qos_class="interactive",
      deficit_ratio=0.0,
      enqueued_at=0.0,
      idempotency_key=f"{request_id}:{phase}:{stage_index}",
      payload=payload,
      batch_key=key,
   )


def _reference_token(case, token_ids):
   tokens = mx.array((token_ids,), dtype=mx.uint32)
   hidden = execute_loaded_stage(case.loaded[0], token_ids=tokens)
   logits = execute_loaded_stage(case.loaded[1], hidden_states=hidden)
   return hidden, int(mx.argmax(logits[0, -1, :]).item())


def _run_two_stage(case, token_ids, *, phase, path_id):
   token_span = len(token_ids) if phase == "PREFILL" else 1
   first = case.ports[0].execute(
      _work_item(
         case,
         0,
         encode_token_ids(token_ids),
         phase=phase,
         token_span=token_span,
         path_id=path_id,
      )
   )
   assert first.success, first.failure_reason
   second = case.ports[1].execute(
      _work_item(
         case,
         1,
         first.payload,
         phase=phase,
         token_span=token_span,
         path_id=path_id,
      )
   )
   assert second.success, second.failure_reason
   return first.payload, second.token_id


def _assert_failure(result, reason):
   assert not result.success
   assert result.failure_scope == "PLACEMENT"
   assert result.failure_reason == reason
   assert result.payload is None
   assert result.token_id is None


def test_two_stage_prefill_and_decode_match_concatenated_reference_and_wire_bytes(runtime_case):
   prompt = (1, 2, 3)
   reference_hidden, reference_prefill_token = _reference_token(runtime_case, prompt)

   activation_payload, prefill_token = _run_two_stage(
      runtime_case,
      prompt,
      phase="PREFILL",
      path_id="path-prefill",
   )

   assert prefill_token == reference_prefill_token
   envelope = decode_activation(activation_payload)
   assert envelope.dtype == "float32"
   assert envelope.shape == tuple(int(value) for value in reference_hidden.shape)
   contiguous_reference = mx.contiguous(reference_hidden)
   mx.eval(contiguous_reference)
   assert envelope.data == bytes(contiguous_reference)

   # Exercise the host's concrete bytes -> MLX and MLX -> bytes paths, not a mock.
   reconstructed = (
      mx.array(memoryview(envelope.data), dtype=mx.uint8)
      .view(mx.float32)
      .reshape(envelope.shape)
   )
   mx.eval(reconstructed)
   assert bool(mx.allclose(reconstructed, reference_hidden, rtol=0.0, atol=0.0).item())
   assert decode_activation(encode_activation(contiguous_reference)).data == envelope.data

   decode_context = prompt + (prefill_token,)
   _, reference_decode_token = _reference_token(runtime_case, decode_context)
   _, decode_token = _run_two_stage(
      runtime_case,
      decode_context,
      phase="DECODE",
      path_id="path-decode",
   )
   assert decode_token == reference_decode_token


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
      with pytest.raises(MLXRuntimeError, match=f"^{reason}"):
         MLXRuntimePort(
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

   with pytest.raises(MLXRuntimeError, match="^load_proof_assignment_id_mismatch"):
      MLXRuntimePort(
         runtime_case.assignments[0]["node_id"],
         runtime_case.graph,
         {placement_id: tampered},
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


def test_malformed_truncated_oversize_rank_dtype_shape_and_nonfinite_activation_reject(runtime_case):
   reference_payload, _ = _run_two_stage(
      runtime_case,
      (1, 2, 3),
      phase="PREFILL",
      path_id="path-activation-reference",
   )
   malformed = b"NOPE" + reference_payload[4:]
   truncated = reference_payload[:20]
   oversize = bytearray(reference_payload)
   struct.pack_into(">Q", oversize, 8, 268_435_457)
   rank_two = encode_activation(
      dtype="float32",
      shape=(3, 4),
      data=b"\x00" * (3 * 4 * 4),
   )
   wrong_dtype = encode_activation(
      dtype="float16",
      shape=(1, 3, 4),
      data=b"\x00" * (1 * 3 * 4 * 2),
   )
   wrong_shape = encode_activation(
      dtype="float32",
      shape=(2, 3, 4),
      data=b"\x00" * (2 * 3 * 4 * 4),
   )
   nonfinite_values = [0.0] * 12
   nonfinite_values[5] = math.nan
   nonfinite = encode_activation(
      dtype="float32",
      shape=(1, 3, 4),
      data=struct.pack("<12f", *nonfinite_values),
   )
   cases = (
      ("invalid_activation_payload_magic", malformed),
      ("truncated_activation_shape", truncated),
      ("activation_payload_too_large", bytes(oversize)),
      ("activation_rank_mismatch", rank_two),
      ("activation_dtype_mismatch", wrong_dtype),
      ("activation_shape_mismatch", wrong_shape),
      ("nonfinite_activation", nonfinite),
   )

   for reason, payload in cases:
      result = runtime_case.ports[1].execute(
         _work_item(
            runtime_case,
            1,
            payload,
            request_id=f"request-{reason}",
            path_id=f"path-{reason}",
         )
      )
      _assert_failure(result, reason)


def test_token_and_position_bounds_fail_closed_on_entry_and_activation(runtime_case):
   entry_cases = (
      ("empty_token_sequence", (), 1),
      ("token_bounds_exceeded", (7,), 1),
      ("position_bounds_exceeded", (0,) * 9, 9),
   )
   for reason, token_ids, token_span in entry_cases:
      result = runtime_case.ports[0].execute(
         _work_item(
            runtime_case,
            0,
            encode_token_ids(token_ids),
            token_span=token_span,
            request_id=f"request-{reason}",
            path_id=f"path-{reason}",
         )
      )
      _assert_failure(result, reason)

   over_position_activation = encode_activation(
      dtype="float32",
      shape=(1, 9, 4),
      data=b"\x00" * (1 * 9 * 4 * 4),
   )
   result = runtime_case.ports[1].execute(
      _work_item(
         runtime_case,
         1,
         over_position_activation,
         token_span=9,
         path_id="path-activation-position-bound",
      )
   )
   _assert_failure(result, "position_bounds_exceeded")


def test_prefill_chunk_is_explicitly_unsupported_without_kv_continuity(runtime_case):
   result = runtime_case.ports[0].execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL_CHUNK",
         token_span=2,
         path_id="path-prefill-chunk",
      )
   )
   _assert_failure(result, "prefill_chunk_requires_kv_continuity")


def test_cancellation_is_idempotent_and_isolated_by_path(runtime_case):
   cancelled_item = _work_item(
      runtime_case,
      0,
      encode_token_ids((1, 2, 3)),
      path_id="path-cancelled",
   )
   runtime_case.ports[0].cancel("path-cancelled")
   runtime_case.ports[0].cancel("path-cancelled")

   _assert_failure(runtime_case.ports[0].execute(cancelled_item), "path_cancelled")
   other = runtime_case.ports[0].execute(
      replace(cancelled_item, path_id="path-not-cancelled", request_id="request-other")
   )
   assert other.success


def test_execute_batch_preserves_order_and_isolates_malformed_item(runtime_case):
   key = _batch_key(runtime_case, 0, "PREFILL", 3)
   payloads = (
      encode_token_ids((1, 2, 3)),
      b"malformed-token-payload",
      encode_token_ids((3, 2, 1)),
   )
   items = tuple(
      _work_item(
         runtime_case,
         0,
         payload,
         request_id=f"request-batch-{index}",
         path_id=f"path-batch-{index}",
         batch_key=key,
      )
      for index, payload in enumerate(payloads)
   )
   expected_first = runtime_case.ports[0].execute(items[0])
   expected_last = runtime_case.ports[0].execute(items[2])
   batch = RuntimeBatch(compatibility_key=key, items=items)

   results = runtime_case.ports[0].execute_batch(batch)

   assert len(results) == 3
   assert results[0].success and results[0].payload == expected_first.payload
   _assert_failure(results[1], "invalid_token_payload_magic")
   assert results[2].success and results[2].payload == expected_last.payload
   assert results[0].payload != results[2].payload


def test_execute_batch_rejects_non_batch_input(runtime_case):
   with pytest.raises(MLXRuntimeError, match="^invalid_runtime_batch"):
      runtime_case.ports[0].execute_batch(object())
