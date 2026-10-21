# SPDX-License-Identifier: AGPL-3.0-only
"""A10 batched decode commit: each member keeps its whole retained KV prefix.

A batched decode advances every member by one position.  The committed stage-
local KV of each member must be its full retained prefix plus that position,
bit-identical to the one-member-at-a-time path, so the member's later decodes
(batched or not) see exactly the context they would have seen alone.
"""

from __future__ import annotations

import hashlib

import mlx.core as mx

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   RuntimeBatchRequest,
   compatibility_digest,
)
from mycelium_router.mlx_runtime import RUNTIME_BATCH_DISPATCH_MODE
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import (  # noqa: F401 - fixtures for this suite
   _fresh_ports,
   _work_item,
   runtime_case,
)

LEASE = 1_000_000_000_000.0


def _digest(payload: bytes | None) -> str:
   assert isinstance(payload, bytes)
   return hashlib.sha256(payload).hexdigest()


def _prefill(case, token_ids, *, request, path):
   return _work_item(
      case,
      0,
      encode_token_ids(token_ids),
      phase="PREFILL",
      token_span=len(token_ids),
      request_id=request,
      path_id=path,
      token_index=-1,
      position=0,
      idempotency_key=f"{request}:PREFILL:-1:0",
      lease_expires_at=LEASE,
   )


def _decode(case, token_id, *, request, path, token_index, position):
   return _work_item(
      case,
      0,
      encode_token_ids((token_id,)),
      phase="DECODE",
      token_span=1,
      request_id=request,
      path_id=path,
      token_index=token_index,
      position=position,
      idempotency_key=f"{request}:DECODE:{token_index}:0",
      lease_expires_at=LEASE,
   )


def _dispatch(port, items, *, batch_ref) -> BatchExecutionReport:
   refs = tuple(f"{batch_ref}:member-{index}" for index in range(len(items)))
   for item, ref in zip(items, refs):
      port.offer_batch_member(item, member_ref=ref)
   report = port.execute_batch(
      RuntimeBatchRequest(
         batch_ref=batch_ref,
         phase=items[0].phase,
         compatibility_key=compatibility_digest(items[0].batch_key),
         member_refs=refs,
         proposed_member_count=len(refs),
      )
   )
   assert isinstance(report, BatchExecutionReport)
   return report


def _kv_widths(port, path_id):
   state = port._kv_states[path_id]
   return {int(pair[0].shape[2]) for pair in state.layers.values()}


def _assert_same_kv(batched, sequential, path_id):
   """Committed stage-local KV is bit-identical to the sequential reference.

   The tiny fixture model can map different retained contexts to the same
   activation, so payload digests alone could miss a KV defect; the committed
   tensors themselves are compared.
   """

   left = batched._kv_states[path_id]
   right = sequential._kv_states[path_id]
   assert left.next_position == right.next_position
   assert sorted(left.layers) == sorted(right.layers)
   for layer in left.layers:
      for mine, theirs in zip(left.layers[layer], right.layers[layer]):
         assert tuple(mine.shape) == tuple(theirs.shape)
         assert bytes(mx.contiguous(mine)) == bytes(mx.contiguous(theirs)), (
            f"{path_id} layer {layer} committed KV differs from the sequential path"
         )


def test_same_position_consecutive_batched_decodes_keep_the_full_prefix(
   runtime_case,  # noqa: F811 - pytest fixture
):
   case = runtime_case
   prefill_a = _prefill(case, (1, 2), request="request-a", path="path-a")
   prefill_b = _prefill(case, (3, 4), request="request-b", path="path-b")
   batched = _fresh_ports(case)[0]
   sequential = _fresh_ports(case)[0]
   for port in (batched, sequential):
      assert port.execute(prefill_a).success is True
      assert port.execute(prefill_b).success is True
   for step in range(2):
      position = 2 + step
      items = (
         _decode(case, (7 + step) % 7, request="request-a", path="path-a", token_index=1 + step, position=position),
         _decode(case, (9 + step) % 7, request="request-b", path="path-b", token_index=1 + step, position=position),
      )
      report = _dispatch(batched, items, batch_ref=f"batch:same-{step}")
      assert report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
      for execution, item in zip(report.executed, items):
         assert _digest(execution.result.payload) == _digest(
            sequential.execute(item).payload
         )
      _assert_same_kv(batched, sequential, "path-a")
      _assert_same_kv(batched, sequential, "path-b")
   assert _kv_widths(batched, "path-a") == {4}
   assert _kv_widths(batched, "path-b") == {4}
