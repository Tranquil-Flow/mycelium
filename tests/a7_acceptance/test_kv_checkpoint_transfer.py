# SPDX-License-Identifier: AGPL-3.0-only
"""A7: byte-exact stage KV checkpoint export/import continues without replay.

A source stage exports the exact KV it holds for a request; a fresh runtime for
the same placement imports it for the successor path (a newer attempt) and the
next DECODE is bit-identical to the uninterrupted run, with no prefill on the
successor.  Every incompatibility refuses and leaves the importer unchanged.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mycelium_router.kv_checkpoint import (
   MAGIC,
   KVCheckpointError,
   checkpoint_digest,
   decode_kv_checkpoint,
   encode_kv_checkpoint,
)
from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.payloads import encode_token_ids
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _work_item

_LEASE = 1_000_000_000_000.0
_MAX = 1 << 20


def _execute(ports, case, tokens, *, phase, request_id, path_id, path_attempt, token_index, position, terminal=False):
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
      if result.payload is not None:
         payload = result.payload
   return result.token_id


def _fresh_ports(case):
   return tuple(
      NumpyRuntimePort(
         assignment["node_id"],
         case.graph,
         {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
         decode_mode="stage_local_kv",
      )
      for index, assignment in enumerate(case.assignments)
   )


def _source_after_one_decode(case, request_id, path_id, prompt):
   first = _execute(case.ports, case, prompt, phase="PREFILL", request_id=request_id, path_id=path_id, path_attempt=0, token_index=-1, position=0)
   second = _execute(case.ports, case, (first,), phase="DECODE", request_id=request_id, path_id=path_id, path_attempt=0, token_index=1, position=len(prompt))
   return first, second


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_imported_checkpoint_continues_bit_identically_without_prefill(tmp_path: Path, architecture: str) -> None:
   case = _case(tmp_path, architecture)
   prompt = (1, 7, 11, 5)
   request_id = f"request:{architecture}:a7"
   first, second = _source_after_one_decode(case, request_id, "path:source", prompt)
   payloads = [port.export_kv_checkpoint("path:source") for port in case.ports]
   # Export does not mutate the source.
   assert all(port.kv_snapshot()["states"]["path:source"]["next_position"] == len(prompt) + 1 for port in case.ports)

   successors = _fresh_ports(case)
   for index, (port, payload) in enumerate(zip(successors, payloads)):
      receipt = port.import_kv_checkpoint(
         payload,
         request_id=request_id,
         path_id="path:successor",
         path_attempt=1,
         placement_id=case.graph.stages[index].placements[0].placement_id,
         lease_expires_at=_LEASE,
         expected_next_position=len(prompt) + 1,
         max_bytes=_MAX,
      )
      assert receipt["checkpoint_digest"] == checkpoint_digest(payload)
      assert receipt["next_position"] == len(prompt) + 1
      assert receipt["layer_count"] == 1
      assert set(receipt) == {
         "protocol", "checkpoint_digest", "identity_digest", "layer_count",
         "kv_bytes", "next_position", "next_sequence", "successor_path_attempt",
      }

   resumed = _execute(successors, case, (second,), phase="DECODE", request_id=request_id, path_id="path:successor", path_attempt=1, token_index=2, position=len(prompt) + 1)
   uninterrupted = _execute(case.ports, case, (second,), phase="DECODE", request_id=request_id, path_id="path:source", path_attempt=0, token_index=2, position=len(prompt) + 1)
   assert resumed == uninterrupted == _reference(case, prompt + (first, second))
   for port in successors:
      snapshot = port.kv_snapshot()
      assert snapshot["prefill_operation_count"] == 0
      assert snapshot["decode_operation_count"] == 1
   # After the same next step, the successor's KV equals the source's byte for byte.
   for source, successor in zip(case.ports, successors):
      _h1, source_layers = decode_kv_checkpoint(source.export_kv_checkpoint("path:source"), max_bytes=_MAX)
      _h2, successor_layers = decode_kv_checkpoint(successor.export_kv_checkpoint("path:successor"), max_bytes=_MAX)
      assert source_layers.keys() == successor_layers.keys()
      for layer in source_layers:
         for left, right in zip(source_layers[layer], successor_layers[layer]):
            assert left.tobytes() == right.tobytes()


def _payload_and_successor(tmp_path: Path):
   case = _case(tmp_path, "qwen2")
   prompt = (1, 7, 11, 5)
   _source_after_one_decode(case, "request:neg", "path:source", prompt)
   payload = case.ports[0].export_kv_checkpoint("path:source")
   (successor,) = _fresh_ports(case)[:1]
   placement = case.graph.stages[0].placements[0].placement_id
   kwargs = dict(
      request_id="request:neg",
      path_id="path:successor",
      path_attempt=1,
      placement_id=placement,
      lease_expires_at=_LEASE,
      expected_next_position=len(prompt) + 1,
      max_bytes=_MAX,
   )
   return case, payload, successor, kwargs


def _reencode(payload: bytes, mutate) -> bytes:
   header, layers = decode_kv_checkpoint(payload, max_bytes=_MAX)
   mutate(header, layers)
   return encode_kv_checkpoint(identity=header["identity"], source=header["source"], position=header["position"], layers=layers)


@pytest.mark.parametrize(
   ("mutate", "code"),
   [
      (lambda h, layers: h["identity"]["runtime_identity"].__setitem__("backend", "mlx"), "kv_checkpoint_runtime_identity_incompatible"),
      (lambda h, layers: h["identity"]["runtime_identity"].__setitem__("dtype", "float16"), "kv_checkpoint_runtime_identity_incompatible"),
      (lambda h, layers: h["identity"].__setitem__("manifest_digest", "sha256:" + "0" * 64), "kv_checkpoint_manifest_digest_incompatible"),
      (lambda h, layers: h["identity"].__setitem__("deployment_epoch", 2), "kv_checkpoint_deployment_epoch_incompatible"),
      (lambda h, layers: h["identity"].__setitem__("decode_mode", "complete_context_replay"), "kv_checkpoint_decode_mode_incompatible"),
      (lambda h, layers: h["source"].__setitem__("request_id", "request:other"), "kv_checkpoint_request_mismatch"),
      (lambda h, layers: h["source"].__setitem__("path_attempt", 1), "kv_checkpoint_successor_not_newer"),
      (lambda h, layers: layers.__setitem__(0, tuple(a.astype(np.float16) for a in layers[0])), "kv_checkpoint_dtype_incompatible"),
   ],
)
def test_incompatible_checkpoint_refuses_and_installs_nothing(tmp_path: Path, mutate, code) -> None:
   _case_, payload, successor, kwargs = _payload_and_successor(tmp_path)
   with pytest.raises(KVCheckpointError) as refused:
      successor.import_kv_checkpoint(_reencode(payload, mutate), **kwargs)
   assert refused.value.code == code
   assert successor.kv_snapshot()["active_state_count"] == 0


def test_position_size_and_byte_tampering_refuse(tmp_path: Path) -> None:
   _case_, payload, successor, kwargs = _payload_and_successor(tmp_path)
   cases = {
      "kv_checkpoint_position_mismatch": (payload, {**kwargs, "expected_next_position": kwargs["expected_next_position"] + 1}),
      "kv_checkpoint_too_large": (payload, {**kwargs, "max_bytes": len(payload) - 1}),
      "kv_checkpoint_data_length_mismatch": (payload[:-1], kwargs),
      "kv_checkpoint_magic_invalid": (b"X" + payload[1:], kwargs),
      "kv_checkpoint_header_not_canonical": (_pad_header(payload), kwargs),
   }
   for code, (blob, arguments) in cases.items():
      with pytest.raises(KVCheckpointError) as refused:
         successor.import_kv_checkpoint(blob, **arguments)
      assert refused.value.code == code
   assert successor.kv_snapshot()["active_state_count"] == 0


def _pad_header(payload: bytes) -> bytes:
   (length,) = struct.unpack_from("<I", payload, len(MAGIC))
   start = len(MAGIC) + 4
   header = payload[start : start + length]
   padded = header.replace(b",", b", ", 1)
   return MAGIC + struct.pack("<I", len(padded)) + padded + payload[start + length :]


def test_existing_state_and_released_path_refuse(tmp_path: Path) -> None:
   _case_, payload, successor, kwargs = _payload_and_successor(tmp_path)
   successor.import_kv_checkpoint(payload, **kwargs)
   with pytest.raises(KVCheckpointError) as refused:
      successor.import_kv_checkpoint(payload, **kwargs)
   assert refused.value.code == "kv_checkpoint_state_already_exists"
   successor.cancel(kwargs["path_id"])
   with pytest.raises(KVCheckpointError) as refused:
      successor.import_kv_checkpoint(payload, **kwargs)
   assert refused.value.code in {"kv_checkpoint_path_cancelled", "kv_checkpoint_path_released"}


def test_export_without_state_refuses(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].export_kv_checkpoint("path:none")
   assert refused.value.code == "kv_checkpoint_state_missing"
