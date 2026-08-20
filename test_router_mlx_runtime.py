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
from mycelium_router.decoding import quantized_greedy_token_id
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
   manifest, _ = _build_local_model(root, n_positions=16)
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
   token_index=None,
   position=None,
   emits_token=False,
   terminal=False,
   lease_expires_at=1_000_000_000_000.0,
   idempotency_key=None,
):
   key = batch_key or _batch_key(case, stage_index, phase, token_span)
   if token_index is None:
      token_index = -1 if phase == "PREFILL" else 0
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
   tokens = mx.array((token_ids,), dtype=mx.uint32)
   hidden = execute_loaded_stage(case.loaded[0], token_ids=tokens)
   logits = execute_loaded_stage(case.loaded[1], hidden_states=hidden)
   return hidden, quantized_greedy_token_id(logits[0, -1, :].tolist())


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
   lease_expires_at=1_000_000_000_000.0,
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
         lease_expires_at=lease_expires_at,
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
         lease_expires_at=lease_expires_at,
      )
   )
   assert second.success, second.failure_reason
   return first.payload, second.token_id


def _fresh_ports(case, *, clock=None):
   return tuple(
      MLXRuntimePort(
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


KV_NUMERIC_TOLERANCE = 1e-5


def test_final_prefill_chunk_emits_token_without_releasing_live_request_kv(
   runtime_case,
):
   ports = _fresh_ports(runtime_case)
   request_id = "request:chunk-kv-continuity"
   path_id = "path:chunk-kv-continuity"

   _run_two_stage(
      runtime_case,
      (1, 2),
      phase="PREFILL_CHUNK",
      path_id=path_id,
      request_id=request_id,
      token_index=0,
      position=0,
      ports=ports,
   )
   _, first_token = _run_two_stage(
      runtime_case,
      (3,),
      phase="PREFILL_CHUNK",
      path_id=path_id,
      request_id=request_id,
      token_index=1,
      position=2,
      emits_token=True,
      terminal=False,
      ports=ports,
   )

   assert first_token is not None
   assert all(port.kv_snapshot()["active_state_count"] == 1 for port in ports)
   _run_two_stage(
      runtime_case,
      (first_token,),
      phase="DECODE",
      path_id=path_id,
      request_id=request_id,
      token_index=1,
      position=3,
      terminal=True,
      ports=ports,
   )
   assert all(port.kv_snapshot()["active_state_count"] == 0 for port in ports)


def test_stage_local_kv_prefill_and_eight_single_token_decodes_match_reference(
   runtime_case,
):
   prompt = (1, 2, 3)
   request_id = "request:stage-local-kv-parity"
   path_id = "path:stage-local-kv-parity"
   reference_hidden, reference_prefill_token = _reference_token(runtime_case, prompt)

   activation_payload, prefill_token = _run_two_stage(
      runtime_case,
      prompt,
      phase="PREFILL",
      path_id=path_id,
      request_id=request_id,
      token_index=-1,
      position=0,
   )

   assert prefill_token == reference_prefill_token
   envelope = decode_activation(activation_payload)
   assert envelope.dtype == "float32"
   assert envelope.shape == tuple(int(value) for value in reference_hidden.shape)
   contiguous_reference = mx.contiguous(reference_hidden)
   mx.eval(contiguous_reference)
   assert envelope.data == bytes(contiguous_reference)

   for stage_index, port in enumerate(runtime_case.ports):
      snapshot = port.kv_snapshot()
      assert snapshot["mode"] == "stage_local_kv"
      assert snapshot["active_state_count"] == 1
      state = snapshot["states"][path_id]
      placement = runtime_case.graph.stages[stage_index].placements[0]
      assert state["request_id"] == request_id
      assert state["path_attempt"] == 0
      assert state["placement_id"] == placement.placement_id
      assert state["assignment_id"] == placement.assignment_id
      assert state["manifest_digest"] == runtime_case.graph.manifest_digest
      assert state["deployment_epoch"] == runtime_case.graph.deployment_epoch
      assert state["next_position"] == len(prompt)
      assert state["next_sequence"] == 1
      assert state["cached_context_tokens"] == len(prompt)
      assert state["kv_bytes"] > 0

   context = list(prompt)
   actual_tokens = [prefill_token]
   reference_tokens = [reference_prefill_token]
   max_hidden_abs_error = 0.0
   for token_index in range(1, 9):
      input_token = actual_tokens[-1]
      context.append(input_token)
      reference_hidden, reference_token = _reference_token(runtime_case, tuple(context))
      activation_payload, actual_token = _run_two_stage(
         runtime_case,
         (input_token,),
         phase="DECODE",
         path_id=path_id,
         request_id=request_id,
         token_index=token_index,
         position=len(prompt) + token_index - 1,
         terminal=token_index == 8,
      )
      envelope = decode_activation(activation_payload)
      assert envelope.shape == (1, 1, runtime_case.graph.hidden_size)
      actual_hidden = (
         mx.array(memoryview(envelope.data), dtype=mx.uint8)
         .view(mx.float32)
         .reshape(envelope.shape)
      )
      expected_hidden = reference_hidden[:, -1:, :]
      mx.eval(actual_hidden, expected_hidden)
      hidden_abs_error = float(mx.max(mx.abs(actual_hidden - expected_hidden)).item())
      max_hidden_abs_error = max(max_hidden_abs_error, hidden_abs_error)
      assert hidden_abs_error <= KV_NUMERIC_TOLERANCE
      assert actual_token == reference_token
      actual_tokens.append(actual_token)
      reference_tokens.append(reference_token)

   assert actual_tokens == reference_tokens
   assert len(actual_tokens) == 9
   assert max_hidden_abs_error <= KV_NUMERIC_TOLERANCE
   for port in runtime_case.ports:
      snapshot = port.kv_snapshot()
      assert snapshot["active_state_count"] == 0
      assert snapshot["release_counts"]["normal_completion"] >= 1


def test_recovery_prefill_rebuilds_kv_without_replaying_last_token(runtime_case):
   ports = _fresh_ports(runtime_case)
   recovered_context = (1, 2, 3, 4, 5)
   _, reference_recovery_token = _reference_token(runtime_case, recovered_context)
   _, recovery_token = _run_two_stage(
      runtime_case,
      recovered_context,
      phase="RECOVERY_PREFILL",
      path_id="path:recovery-kv",
      request_id="request:recovery-kv",
      token_index=2,
      position=0,
      ports=ports,
   )
   assert recovery_token == reference_recovery_token
   for port in ports:
      state = port.kv_snapshot()["states"]["path:recovery-kv"]
      assert state["next_position"] == len(recovered_context)
      assert state["next_sequence"] == 3

   expected_context = recovered_context + (recovery_token,)
   _, expected_next_token = _reference_token(runtime_case, expected_context)
   _, next_token = _run_two_stage(
      runtime_case,
      (recovery_token,),
      phase="DECODE",
      path_id="path:recovery-kv",
      request_id="request:recovery-kv",
      token_index=3,
      position=len(recovered_context),
      terminal=True,
      ports=ports,
   )
   assert next_token == expected_next_token
   for port in ports:
      assert port.kv_snapshot()["active_state_count"] == 0


def test_constructor_rejects_invalid_decode_mode(runtime_case):
   placement = runtime_case.graph.stages[0].placements[0]
   with pytest.raises(MLXRuntimeError, match="^invalid_runtime_decode_mode$"):
      MLXRuntimePort(
         placement.node_id,
         runtime_case.graph,
         {placement.placement_id: runtime_case.loaded[0]},
         decode_mode="backend_specific_guess",
      )


def test_constructor_rejects_non_little_endian_host(runtime_case, monkeypatch):
   placement_id = runtime_case.graph.stages[0].placements[0].placement_id
   monkeypatch.setattr("mycelium_router.mlx_runtime.sys.byteorder", "big")

   with pytest.raises(MLXRuntimeError, match="^unsupported_host_byte_order"):
      MLXRuntimePort(
         runtime_case.assignments[0]["node_id"],
         runtime_case.graph,
         {placement_id: runtime_case.loaded[0]},
      )


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
      ("position_bounds_exceeded", (0,) * 17, 17),
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
      shape=(1, 17, 4),
      data=b"\x00" * (1 * 17 * 4 * 4),
   )
   result = runtime_case.ports[1].execute(
      _work_item(
         runtime_case,
         1,
         over_position_activation,
         token_span=17,
         path_id="path-activation-position-bound",
      )
   )
   _assert_failure(result, "position_bounds_exceeded")


def test_prefill_chunks_extend_stage_local_kv_without_advancing_decode_sequence(
   runtime_case,
):
   port = _fresh_ports(runtime_case)[0]
   initial = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2)),
         phase="PREFILL_CHUNK",
         token_span=2,
         request_id="request-prefill-chunk",
         path_id="path-prefill-chunk",
         token_index=0,
         position=0,
      )
   )
   continued = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((3,)),
         phase="PREFILL_CHUNK",
         token_span=1,
         request_id="request-prefill-chunk",
         path_id="path-prefill-chunk",
         token_index=1,
         position=2,
      )
   )
   decoded = port.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((4,)),
         phase="DECODE",
         token_span=1,
         request_id="request-prefill-chunk",
         path_id="path-prefill-chunk",
         token_index=1,
         position=3,
      )
   )

   assert initial.success
   assert continued.success
   assert decoded.success
   state = port.kv_snapshot()["states"]["path-prefill-chunk"]
   assert state["cached_context_tokens"] == 4
   assert state["next_sequence"] == 2


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


def test_stage_local_kv_replay_is_idempotent_and_conflicts_fail_closed(runtime_case):
   port = _fresh_ports(runtime_case)[0]
   path_id = "path:kv-replay"
   request_id = "request:kv-replay"
   prefill = _work_item(
      runtime_case,
      0,
      encode_token_ids((1, 2, 3)),
      request_id=request_id,
      path_id=path_id,
      token_index=-1,
      position=0,
   )

   first = port.execute(prefill)
   duplicate = port.execute(prefill)
   assert first.success and duplicate == first
   assert port.kv_snapshot()["applied_operation_count"] == 1
   _assert_failure(
      port.execute(replace(prefill, payload=encode_token_ids((3, 2, 1)))),
      "kv_sequence_replay_conflict",
   )

   decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      request_id=request_id,
      path_id=path_id,
      token_index=1,
      position=3,
   )
   decoded = port.execute(decode)
   assert decoded.success
   assert port.execute(decode) == decoded
   assert port.kv_snapshot()["applied_operation_count"] == 2
   _assert_failure(
      port.execute(replace(decode, payload=encode_token_ids((5,)))),
      "kv_sequence_replay_conflict",
   )
   port.cancel(path_id)


def test_stage_local_kv_identity_position_and_sequence_mismatches_fail_closed(
   runtime_case,
):
   port = _fresh_ports(runtime_case)[0]
   for invalid_lease in (float("inf"), float("-inf"), float("nan")):
      invalid = _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2, 3)),
         path_id=f"path:invalid-lease:{invalid_lease!r}",
         lease_expires_at=invalid_lease,
      )
      _assert_failure(port.execute(invalid), "invalid_kv_lease")
   path_id = "path:kv-bindings"
   request_id = "request:kv-bindings"
   prefill = _work_item(
      runtime_case,
      0,
      encode_token_ids((1, 2, 3)),
      request_id=request_id,
      path_id=path_id,
      token_index=-1,
      position=0,
   )
   assert port.execute(prefill).success
   valid_decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      request_id=request_id,
      path_id=path_id,
      token_index=1,
      position=3,
   )
   valid_key = valid_decode.batch_key
   assert valid_key is not None
   cases = (
      ("kv_request_id_mismatch", replace(valid_decode, request_id="wrong-request")),
      ("kv_state_missing", replace(valid_decode, path_id="wrong-path")),
      ("kv_path_attempt_mismatch", replace(valid_decode, path_attempt=1)),
      ("kv_position_mismatch", replace(valid_decode, position=4)),
      ("kv_sequence_mismatch", replace(valid_decode, token_index=2)),
      (
         "batch_key_assignment_id_mismatch",
         replace(valid_decode, batch_key=replace(valid_key, assignment_id="wrong-assignment")),
      ),
      (
         "batch_key_manifest_digest_mismatch",
         replace(
            valid_decode,
            batch_key=replace(valid_key, manifest_digest="sha256:" + "0" * 64),
         ),
      ),
      (
         "batch_key_deployment_epoch_mismatch",
         replace(
            valid_decode,
            batch_key=replace(
               valid_key,
               deployment_epoch=valid_key.deployment_epoch + 1,
            ),
         ),
      ),
   )
   for reason, item in cases:
      _assert_failure(port.execute(item), reason)
      state = port.kv_snapshot()["states"][path_id]
      assert state["next_position"] == 3
      assert state["next_sequence"] == 1
   assert port.execute(replace(valid_decode, terminal=True)).success
   assert port.kv_snapshot()["active_state_count"] == 0


def test_kv_state_mutates_only_after_output_validation(runtime_case, monkeypatch):
   port = _fresh_ports(runtime_case)[0]
   prefill = _work_item(
      runtime_case,
      0,
      encode_token_ids((1, 2, 3)),
      path_id="path:atomic",
      request_id="request:atomic",
   )
   original_runtime_result = port._runtime_result

   def reject_output(stage, runtime, output, **_kwargs):
      raise MLXRuntimeError("forced_output_rejection")

   monkeypatch.setattr(port, "_runtime_result", reject_output)
   _assert_failure(port.execute(prefill), "forced_output_rejection")
   assert port.kv_snapshot()["active_state_count"] == 0

   monkeypatch.setattr(port, "_runtime_result", original_runtime_result)
   assert port.execute(prefill).success
   before = port.kv_snapshot()["states"][prefill.path_id]
   decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      path_id=prefill.path_id,
      request_id=prefill.request_id,
      token_index=1,
      position=3,
   )
   monkeypatch.setattr(port, "_runtime_result", reject_output)
   _assert_failure(port.execute(decode), "forced_output_rejection")
   after = port.kv_snapshot()["states"][prefill.path_id]
   assert after["next_position"] == before["next_position"] == 3
   assert after["next_sequence"] == before["next_sequence"] == 1
   assert after["cached_context_tokens"] == before["cached_context_tokens"] == 3
   port.cancel(prefill.path_id)


def test_kv_lifecycle_releases_cancel_expiry_completion_and_worker_crash(
   runtime_case,
):
   class MutableClock:
      now = 10.0

      def __call__(self):
         return self.now

   clock = MutableClock()
   port = _fresh_ports(runtime_case, clock=clock)[0]

   def prefill(path_id, request_id, *, lease=100.0):
      item = _work_item(
         runtime_case,
         0,
         encode_token_ids((1, 2, 3)),
         request_id=request_id,
         path_id=path_id,
         token_index=-1,
         position=0,
         lease_expires_at=lease,
      )
      result = port.execute(item)
      assert result.success, result.failure_reason
      return item

   cancelled = prefill("path:cancel", "request:cancel")
   assert port.kv_snapshot()["active_state_count"] == 1
   port.cancel(cancelled.path_id)
   port.cancel(cancelled.path_id)
   snapshot = port.kv_snapshot()
   assert snapshot["active_state_count"] == 0
   assert snapshot["retained_result_count"] == 0
   _assert_failure(port.execute(cancelled), "path_cancelled")

   expired = prefill("path:expiry", "request:expiry", lease=12.0)
   clock.now = 12.0
   assert port.expire_leases() == (expired.path_id,)
   snapshot = port.kv_snapshot()
   assert snapshot["active_state_count"] == 0
   assert snapshot["release_counts"]["lease_expired"] == 1
   expired_decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      request_id=expired.request_id,
      path_id=expired.path_id,
      token_index=1,
      position=3,
      lease_expires_at=12.0,
   )
   _assert_failure(port.execute(expired_decode), "kv_lease_expired")

   completed = prefill("path:complete", "request:complete")
   completed_decode = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      request_id=completed.request_id,
      path_id=completed.path_id,
      token_index=1,
      position=3,
      terminal=True,
      lease_expires_at=100.0,
   )
   assert port.execute(completed_decode).success
   snapshot = port.kv_snapshot()
   assert snapshot["active_state_count"] == 0
   assert snapshot["retained_result_count"] == 0

   crash_path = "path:worker-crash"
   prefill(crash_path, "request:worker-crash")
   port.close(reason="worker_crash")
   snapshot = port.kv_snapshot()
   assert snapshot["closed"] is True
   assert snapshot["active_state_count"] == 0
   assert snapshot["retained_result_count"] == 0
   assert snapshot["release_counts"]["worker_crash"] == 1
   _assert_failure(port.execute(completed), "runtime_closed")

   replacement = _fresh_ports(runtime_case)[0]
   missing = _work_item(
      runtime_case,
      0,
      encode_token_ids((4,)),
      phase="DECODE",
      token_span=1,
      request_id="request:fresh",
      path_id=crash_path,
      token_index=1,
      position=3,
   )
   _assert_failure(replacement.execute(missing), "kv_state_missing")
   fresh = replacement.execute(
      _work_item(
         runtime_case,
         0,
         encode_token_ids((2, 1)),
         request_id="request:fresh",
         path_id="path:fresh",
         token_index=-1,
         position=0,
         token_span=2,
      )
   )
   assert fresh.success
   state = replacement.kv_snapshot()["states"]["path:fresh"]
   assert state["request_id"] == "request:fresh"
   assert state["cached_context_tokens"] == 2
   replacement.cancel("path:fresh")


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
