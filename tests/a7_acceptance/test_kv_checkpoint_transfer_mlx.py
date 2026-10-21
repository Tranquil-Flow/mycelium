# SPDX-License-Identifier: AGPL-3.0-only
"""A7: byte-exact MLX stage KV checkpoint export/import (the physical-gate backend)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_router.kv_checkpoint import KVCheckpointError, decode_kv_checkpoint
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import _fresh_ports, _reference_token, _work_item
from test_router_mlx_runtime import runtime_case as _runtime_case_fixture

runtime_case = _runtime_case_fixture

_LEASE = 1_000_000_000_000.0
_MAX = 1 << 24


def _step(case, ports, tokens, *, phase, request_id, path_id, path_attempt, token_index, position, terminal=False):
   payload = encode_token_ids(tokens)
   result = None
   for stage_index, port in enumerate(ports):
      item = replace(
         _work_item(
            case,
            stage_index,
            payload,
            phase=phase,
            token_span=len(tokens) if phase != "DECODE" else 1,
            request_id=request_id,
            path_id=path_id,
            token_index=token_index,
            position=position,
            terminal=terminal,
            lease_expires_at=_LEASE,
         ),
         path_attempt=path_attempt,
      )
      result = port.execute(item)
      assert result.success, result.failure_reason
      payload = result.payload
   return result.token_id


def test_mlx_checkpoint_resumes_on_fresh_ports_without_prefill(runtime_case):
   case = runtime_case
   source = _fresh_ports(case)
   prompt = (1, 2, 3)
   request_id = "request:a7-mlx"
   first = _step(case, source, prompt, phase="PREFILL", request_id=request_id, path_id="path:a7-src", path_attempt=0, token_index=-1, position=0)
   second = _step(case, source, (first,), phase="DECODE", request_id=request_id, path_id="path:a7-src", path_attempt=0, token_index=1, position=len(prompt))
   payloads = [port.export_kv_checkpoint("path:a7-src") for port in source]

   successor = _fresh_ports(case)
   for index, (port, payload) in enumerate(zip(successor, payloads)):
      receipt = port.import_kv_checkpoint(
         payload,
         request_id=request_id,
         path_id="path:a7-succ",
         path_attempt=1,
         placement_id=case.graph.stages[index].placements[0].placement_id,
         lease_expires_at=_LEASE,
         expected_next_position=len(prompt) + 1,
         max_bytes=_MAX,
      )
      assert receipt["next_position"] == len(prompt) + 1
      header, _layers = decode_kv_checkpoint(payload, max_bytes=_MAX)
      assert header["identity"]["runtime_identity"]["backend"] == "mlx"

   resumed = _step(case, successor, (second,), phase="DECODE", request_id=request_id, path_id="path:a7-succ", path_attempt=1, token_index=2, position=len(prompt) + 1)
   uninterrupted = _step(case, source, (second,), phase="DECODE", request_id=request_id, path_id="path:a7-src", path_attempt=0, token_index=2, position=len(prompt) + 1)
   _hidden, reference = _reference_token(case, prompt + (first, second))
   assert resumed == uninterrupted == reference
   for port in successor:
      snapshot = port.kv_snapshot()
      assert snapshot["prefill_operation_count"] == 0
      assert snapshot["decode_operation_count"] == 1
   for left_port, right_port in zip(source, successor):
      _h, left = decode_kv_checkpoint(left_port.export_kv_checkpoint("path:a7-src"), max_bytes=_MAX)
      _h, right = decode_kv_checkpoint(right_port.export_kv_checkpoint("path:a7-succ"), max_bytes=_MAX)
      for layer in left:
         for a, b in zip(left[layer], right[layer]):
            assert a.tobytes() == b.tobytes()


def test_mlx_checkpoint_refuses_numpy_backend_payload(runtime_case):
   case = runtime_case
   source = _fresh_ports(case)
   _step(case, source, (1, 2, 3), phase="PREFILL", request_id="request:a7-x", path_id="path:x", path_attempt=0, token_index=-1, position=0)
   payload = source[0].export_kv_checkpoint("path:x")
   from mycelium_router.kv_checkpoint import encode_kv_checkpoint

   header, layers = decode_kv_checkpoint(payload, max_bytes=_MAX)
   header["identity"]["runtime_identity"]["backend"] = "numpy"
   forged = encode_kv_checkpoint(identity=header["identity"], source=header["source"], position=header["position"], layers=layers)
   (successor,) = _fresh_ports(case)[:1]
   with pytest.raises(KVCheckpointError) as refused:
      successor.import_kv_checkpoint(
         forged,
         request_id="request:a7-x",
         path_id="path:y",
         path_attempt=1,
         placement_id=case.graph.stages[0].placements[0].placement_id,
         lease_expires_at=_LEASE,
         expected_next_position=3,
         max_bytes=_MAX,
      )
   assert refused.value.code == "kv_checkpoint_runtime_identity_incompatible"
   assert successor.kv_snapshot()["active_state_count"] == 0


def test_mlx_standby_port_refuses_ordinary_work_and_continues_from_import(runtime_case):
   """A7 slice 2 on the physical backend: a STANDBY placement's dedicated MLX port."""

   from mycelium_router.contracts import PlacementEdge
   from mycelium_router.mlx_runtime import MLXRuntimeError, MLXRuntimePort
   from mycelium_router.validation import validate_execution_graph

   case = runtime_case
   graph = case.graph
   source_placement = graph.stages[1].placements[0]
   standby_id = source_placement.placement_id + "-standby"
   standby = replace(source_placement, placement_id=standby_id, lifecycle_state="STANDBY")
   entry_id = graph.stages[0].placements[0].placement_id
   graph = replace(
      graph,
      stages=(graph.stages[0], replace(graph.stages[1], placements=(source_placement, standby)), *graph.stages[2:]),
      edges=graph.edges + (PlacementEdge(edge_id="forward:standby", from_placement_id=entry_id, to_placement_id=standby_id, link_id="test:standby"),),
   )
   if len(graph.stages) == 2:
      graph = replace(graph, loopback_edges=graph.loopback_edges + (PlacementEdge(edge_id="loopback:standby", from_placement_id=standby_id, to_placement_id=entry_id, link_id="test:standby"),))
   validate_execution_graph(graph)
   node = source_placement.node_id
   ports = [MLXRuntimePort(a["node_id"], graph, {graph.stages[i].placements[0].placement_id: case.loaded[i]}) for i, a in enumerate(case.assignments)]
   standby_port = MLXRuntimePort(node, graph, {standby_id: case.loaded[1]})
   assert standby_port.standby_placement_ids == frozenset({standby_id})
   with pytest.raises(MLXRuntimeError) as excinfo:
      MLXRuntimePort(node, graph, {source_placement.placement_id: case.loaded[1], standby_id: case.loaded[1]})
   assert excinfo.value.code == "standby_port_mixed_lifecycle"

   def item_for(stage_index, payload, *, placement_id=None, path_attempt=0, **kwargs):
      item = replace(_work_item(case, stage_index, payload, lease_expires_at=_LEASE, **kwargs), path_attempt=path_attempt)
      if placement_id is not None:
         item = replace(item, placement_id=placement_id, batch_key=replace(item.batch_key, placement_id=placement_id))
      return item

   prompt = (1, 2, 3)
   hidden = ports[0].execute(item_for(0, encode_token_ids(prompt), phase="PREFILL", token_span=3, request_id="r", path_id="p:src", token_index=-1, position=0))
   assert hidden.success
   refused = standby_port.execute(item_for(1, hidden.payload, placement_id=standby_id, phase="PREFILL", token_span=3, request_id="r", path_id="p:x", token_index=-1, position=0))
   assert not refused.success and refused.failure_reason == "standby_placement_not_serving"
   first = ports[1].execute(item_for(1, hidden.payload, phase="PREFILL", token_span=3, request_id="r", path_id="p:src", token_index=-1, position=0))
   assert first.success and len(case.assignments) == 2
   standby_port.import_kv_checkpoint(ports[1].export_kv_checkpoint("p:src"), request_id="r", path_id="p:succ", path_attempt=1, placement_id=standby_id, lease_expires_at=_LEASE, expected_next_position=len(prompt), max_bytes=_MAX)
   ports[0].rekey_kv_path("p:src", new_path_id="p:succ", new_path_attempt=1, lease_expires_at=_LEASE)
   step = ports[0].execute(item_for(0, encode_token_ids((first.token_id,)), phase="DECODE", token_span=1, request_id="r", path_id="p:succ", path_attempt=1, token_index=1, position=len(prompt)))
   assert step.success
   resumed = standby_port.execute(item_for(1, step.payload, placement_id=standby_id, phase="DECODE", token_span=1, request_id="r", path_id="p:succ", path_attempt=1, token_index=1, position=len(prompt)))
   assert resumed.success, resumed.failure_reason
   assert resumed.token_id == _reference_token(case, prompt + (first.token_id,))[1]
   assert standby_port.kv_snapshot()["prefill_operation_count"] == 0
