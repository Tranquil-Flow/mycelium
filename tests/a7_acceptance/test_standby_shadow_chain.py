# SPDX-License-Identifier: AGPL-3.0-only
"""A7: incremental shadow checkpoint chains, assembly, and surviving-stage rekey."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a7_standby_store import StandbyShadowError, StandbyShadowStore
from mycelium_router.kv_checkpoint import KVCheckpointError, checkpoint_digest, decode_kv_checkpoint
from test_router_numpy_qwen_kv import _case, _reference
from tests.a7_acceptance.test_kv_checkpoint_transfer import _LEASE, _MAX, _execute, _fresh_ports

_REQ = "request:chain"
_KEY = "sha256:" + "1" * 64
_STAGE = "sha256:" + "2" * 64


def _decode_steps(case, prompt, steps, *, store, stage_index):
   """Run prefill and ``steps`` decodes; ship each new span of stage ``stage_index`` to the store."""

   port = case.ports[stage_index]
   tokens = [_execute(case.ports, case, prompt, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)]
   previous_end, previous_digest, sequence = 0, None, 0
   receipts = []

   def ship() -> None:
      nonlocal previous_end, previous_digest, sequence
      payload = port.export_kv_checkpoint("path:src", from_position=previous_end)
      receipts.append(store.accept(request_key=_KEY, stage_id=_STAGE, sequence=sequence, predecessor_digest=previous_digest, payload=payload))
      header, _ = decode_kv_checkpoint(payload, max_bytes=_MAX)
      previous_end, previous_digest, sequence = header["kv_span"][1], checkpoint_digest(payload), sequence + 1

   ship()
   for step in range(1, steps + 1):
      position = len(prompt) + step - 1
      tokens.append(_execute(case.ports, case, (tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=step, position=position))
      ship()
   return tokens, receipts


def test_incremental_chain_assembles_the_exact_full_checkpoint_and_resumes(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   prompt = (1, 7, 11, 5)
   store = StandbyShadowStore(max_entry_bytes=_MAX, max_entries=4)
   tokens, receipts = _decode_steps(case, prompt, 3, store=store, stage_index=1)
   assert [receipt["checkpoint_sequence"] for receipt in receipts] == [0, 1, 2, 3]
   assert receipts[-1]["next_position"] == len(prompt) + 3
   # The assembled shadow equals what the source would export in full, byte for byte.
   assembled = store.full_checkpoint(request_key=_KEY, stage_id=_STAGE)
   assert assembled == case.ports[1].export_kv_checkpoint("path:src")

   successors = _fresh_ports(case)
   # The replaced stage imports the shadow; the surviving stage rekeys its own KV.
   successors[1].import_kv_checkpoint(
      assembled,
      request_id=_REQ,
      path_id="path:succ",
      path_attempt=1,
      placement_id=case.graph.stages[1].placements[0].placement_id,
      lease_expires_at=_LEASE,
      expected_next_position=len(prompt) + 3,
      max_bytes=_MAX,
   )
   case.ports[0].rekey_kv_path("path:src", new_path_id="path:succ", new_path_attempt=1, lease_expires_at=_LEASE)
   ports = (case.ports[0], successors[1])
   resumed = _execute(ports, case, (tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:succ", path_attempt=1, token_index=4, position=len(prompt) + 3)
   assert resumed == _reference(case, prompt + tuple(tokens))
   assert successors[1].kv_snapshot()["prefill_operation_count"] == 0

   # The surviving stage installed the fence: the old attempt is refused.
   stale = _execute_one(case, 0, tokens[-1], path_id="path:src", path_attempt=0, position=len(prompt) + 3, token_index=4)
   assert stale.failure_reason == "kv_state_released"
   assert store.release_request(_KEY) > 0
   assert store.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}


def _execute_one(case, stage_index, token, *, path_id, path_attempt, position, token_index):
   from dataclasses import replace

   from mycelium_router.payloads import encode_token_ids
   from test_router_numpy_runtime import _work_item

   item = replace(
      _work_item(case, stage_index, encode_token_ids((token,)), phase="DECODE", token_span=1, request_id=_REQ, path_id=path_id, token_index=token_index, position=position, lease_expires_at=_LEASE),
      path_attempt=path_attempt,
   )
   return case.ports[stage_index].execute(item)


def _two_links(tmp_path: Path):
   case = _case(tmp_path, "qwen2")
   prompt = (1, 7, 11, 5)
   port = case.ports[1]
   first = _execute(case.ports, case, prompt, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)
   full = port.export_kv_checkpoint("path:src")
   _execute(case.ports, case, (first,), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=1, position=len(prompt))
   delta = port.export_kv_checkpoint("path:src", from_position=len(prompt))
   return full, delta


def test_chain_links_must_be_contiguous_and_consistent(tmp_path: Path) -> None:
   full, delta = _two_links(tmp_path)
   store = StandbyShadowStore(max_entry_bytes=_MAX, max_entries=4)
   accept = store.accept

   def code(**kwargs) -> str:
      with pytest.raises(StandbyShadowError) as refused:
         accept(request_key=_KEY, stage_id=_STAGE, **kwargs)
      return refused.value.code

   assert code(sequence=0, predecessor_digest=None, payload=delta) == "shadow_first_checkpoint_not_full"
   assert code(sequence=1, predecessor_digest=checkpoint_digest(full), payload=full) == "skipped_predecessor"
   first = accept(request_key=_KEY, stage_id=_STAGE, sequence=0, predecessor_digest=None, payload=full)
   assert accept(request_key=_KEY, stage_id=_STAGE, sequence=0, predecessor_digest=None, payload=full)["outcome"] == "idempotent"
   assert code(sequence=0, predecessor_digest=None, payload=delta) == "conflicting_repeat"
   assert code(sequence=2, predecessor_digest=first["checkpoint_digest"], payload=delta) == "skipped_predecessor"
   assert code(sequence=1, predecessor_digest="sha256:" + "0" * 64, payload=delta) == "predecessor_mismatch"
   assert code(sequence=1, predecessor_digest=first["checkpoint_digest"], payload=full) == "regressed_position"
   accept(request_key=_KEY, stage_id=_STAGE, sequence=1, predecessor_digest=first["checkpoint_digest"], payload=delta)
   assert code(sequence=0, predecessor_digest=None, payload=full) == "regressed_position"


def test_bounds_refuse_without_storing(tmp_path: Path) -> None:
   full, _delta = _two_links(tmp_path)
   small = StandbyShadowStore(max_entry_bytes=len(full) - 1, max_entries=4)
   with pytest.raises(StandbyShadowError) as refused:
      small.accept(request_key=_KEY, stage_id=_STAGE, sequence=0, predecessor_digest=None, payload=full)
   assert refused.value.code == "kv_checkpoint_too_large"
   one = StandbyShadowStore(max_entry_bytes=_MAX, max_entries=1)
   one.accept(request_key=_KEY, stage_id=_STAGE, sequence=0, predecessor_digest=None, payload=full)
   with pytest.raises(StandbyShadowError) as refused:
      one.accept(request_key="sha256:" + "3" * 64, stage_id=_STAGE, sequence=0, predecessor_digest=None, payload=full)
   assert refused.value.code == "shadow_store_full"
   assert one.snapshot()["shadow_entry_count"] == 1


def test_a_delta_is_never_importable_and_rekey_refuses_stale_targets(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   prompt = (1, 7, 11, 5)
   first = _execute(case.ports, case, prompt, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)
   _execute(case.ports, case, (first,), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=1, position=len(prompt))
   delta = case.ports[1].export_kv_checkpoint("path:src", from_position=len(prompt))
   (successor,) = _fresh_ports(case)[1:]
   with pytest.raises(KVCheckpointError) as refused:
      successor.import_kv_checkpoint(delta, request_id=_REQ, path_id="path:s", path_attempt=1, placement_id=case.graph.stages[1].placements[0].placement_id, lease_expires_at=_LEASE, expected_next_position=len(prompt) + 1, max_bytes=_MAX)
   assert refused.value.code == "kv_checkpoint_not_full"
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path("path:src", new_path_id="path:s", new_path_attempt=0, lease_expires_at=_LEASE)
   assert refused.value.code == "kv_checkpoint_successor_not_newer"
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].export_kv_checkpoint("path:src", from_position=len(prompt) + 1)
   assert refused.value.code == "kv_checkpoint_span_invalid"
