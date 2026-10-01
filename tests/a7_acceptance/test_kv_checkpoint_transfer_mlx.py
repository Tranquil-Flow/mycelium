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
