# SPDX-License-Identifier: AGPL-3.0-only
"""A10 mixed-position decode batching: one invocation, exact per-member parity.

Concurrent requests rarely sit at the same decode position, so a runtime that
only batches equal positions almost never forms a batch under continuous
arrivals.  These tests pin the mixed-position decode path:

  * members at different decode positions (different retained KV widths) are
    advanced by ONE real batched backend invocation;
  * every member's payload and committed KV are bit-identical to its own
    sequential reference, and so is every later decode that continues from it;
  * padding of a shorter member's retained KV never enters its committed KV;
  * the Qwen2/Qwen3 ragged block (float and int8) matches each member alone;
  * mixed positions outside DECODE are still refused as one invocation.
"""

from __future__ import annotations

import mlx.core as mx
import numpy as np
import pytest

import mycelium_router.mlx_runtime as mlx_runtime
from mycelium_router.mlx_runtime import (
   RUNTIME_BATCH_DISPATCH_MODE,
   SEQUENTIAL_DISPATCH_MODE,
)
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import (  # noqa: F401 - fixtures for this suite
   _fresh_ports,
   _work_item,
   runtime_case,
)
from tests.a10_deterministic.test_a10_batched_decode_kv_prefix import (
   LEASE,
   _assert_same_kv,
   _decode,
   _digest,
   _dispatch,
   _kv_widths,
   _prefill,
)
from tests.a10_deterministic.test_a10_row_stable_projection import (
   _HIDDEN,
   _QWEN_CONFIG,
   _past,
   _qwen_tensors,
)

class _BlockWidths:
   def __init__(self, monkeypatch):
      self.widths: list[int] = []
      original = mlx_runtime._gpt2_block_with_kv

      def counting(hidden, *args, **kwargs):
         self.widths.append(int(hidden.shape[0]))
         return original(hidden, *args, **kwargs)

      monkeypatch.setattr(mlx_runtime, "_gpt2_block_with_kv", counting)


def test_mixed_position_decode_is_one_invocation_with_exact_parity(
   runtime_case,  # noqa: F811 - pytest fixture
   monkeypatch,
):
   case = runtime_case
   blocks = _BlockWidths(monkeypatch)
   prefill_a = _prefill(case, (1, 2), request="request-a", path="path-a")
   prefill_b = _prefill(case, (3, 4, 5, 6), request="request-b", path="path-b")
   decode_a = _decode(case, 0, request="request-a", path="path-a", token_index=1, position=2)
   decode_b = _decode(case, 1, request="request-b", path="path-b", token_index=1, position=4)

   port = _fresh_ports(case)[0]
   assert port.execute(prefill_a).success is True
   assert port.execute(prefill_b).success is True
   blocks.widths.clear()
   report = _dispatch(port, (decode_a, decode_b), batch_ref="batch:mixed-1")

   assert report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
   assert report.backend_invocations == 1
   assert blocks.widths and set(blocks.widths) == {2}, (
      f"mixed-position decode ran block widths {blocks.widths}; both members "
      "must advance in ONE invocation"
   )
   assert [execution.result.success for execution in report.executed] == [True, True]
   assert [execution.committed_prefix for execution in report.executed] == [3, 5]
   # Padding of the shorter member never enters its committed KV.
   assert _kv_widths(port, "path-a") == {3}
   assert _kv_widths(port, "path-b") == {5}

   sequential = _fresh_ports(case)[0]
   assert sequential.execute(prefill_a).success is True
   assert sequential.execute(prefill_b).success is True
   reference_a = sequential.execute(decode_a)
   reference_b = sequential.execute(decode_b)
   assert _digest(report.executed[0].result.payload) == _digest(reference_a.payload)
   assert _digest(report.executed[1].result.payload) == _digest(reference_b.payload)
   _assert_same_kv(port, sequential, "path-a")
   _assert_same_kv(port, sequential, "path-b")
   observation = port.batch_observation()
   assert observation is not None and observation.claims_runtime_batch() is True


def test_decode_continuing_from_mixed_batched_kv_keeps_exact_parity(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """Two consecutive batched decodes, then a sequential one, all match the
   one-member-at-a-time reference: each member's full prefix is retained."""

   case = runtime_case
   prefill_a = _prefill(case, (1, 2), request="request-a", path="path-a")
   prefill_b = _prefill(case, (3, 4, 5), request="request-b", path="path-b")
   steps = (
      (
         _decode(case, 0, request="request-a", path="path-a", token_index=1, position=2),
         _decode(case, 1, request="request-b", path="path-b", token_index=1, position=3),
      ),
      (
         _decode(case, 2, request="request-a", path="path-a", token_index=2, position=3),
         _decode(case, 3, request="request-b", path="path-b", token_index=2, position=4),
      ),
   )
   tail_a = _decode(case, 4, request="request-a", path="path-a", token_index=3, position=4)
   tail_b = _decode(case, 5, request="request-b", path="path-b", token_index=3, position=5)

   batched = _fresh_ports(case)[0]
   sequential = _fresh_ports(case)[0]
   for port in (batched, sequential):
      assert port.execute(prefill_a).success is True
      assert port.execute(prefill_b).success is True
   for index, (step_a, step_b) in enumerate(steps):
      report = _dispatch(batched, (step_a, step_b), batch_ref=f"batch:cont-{index}")
      assert report.dispatch_mode == RUNTIME_BATCH_DISPATCH_MODE
      reference_a = sequential.execute(step_a)
      reference_b = sequential.execute(step_b)
      assert _digest(report.executed[0].result.payload) == _digest(reference_a.payload)
      assert _digest(report.executed[1].result.payload) == _digest(reference_b.payload)
      _assert_same_kv(batched, sequential, "path-a")
      _assert_same_kv(batched, sequential, "path-b")
   assert _kv_widths(batched, "path-a") == {4}
   assert _kv_widths(batched, "path-b") == {5}
   for tail in (tail_a, tail_b):
      assert _digest(batched.execute(tail).payload) == _digest(
         sequential.execute(tail).payload
      )


def test_mixed_positions_outside_decode_are_not_one_invocation(
   runtime_case,  # noqa: F811 - pytest fixture
):
   """A prefill continuation at different positions is still served one member
   at a time and reported as a split, never claimed as a batch."""

   case = runtime_case
   chunk = lambda request, path, tokens, position, index: _work_item(  # noqa: E731
      case,
      0,
      encode_token_ids(tokens),
      phase="PREFILL_CHUNK",
      token_span=len(tokens),
      request_id=request,
      path_id=path,
      token_index=index,
      position=position,
      idempotency_key=f"{request}:PREFILL_CHUNK:{index}:0",
      lease_expires_at=LEASE,
   )
   port = _fresh_ports(case)[0]
   items = (
      chunk("request-a", "path-a", (0, 1), 2, 1),
      chunk("request-b", "path-b", (2, 3), 4, 1),
   )
   refusal = port._batch_group_refusal(
      tuple(
         mlx_runtime._OfferedMember(
            member_ref=f"m{index}",
            item=item,
            stage=case.graph.stages[0],
            placement=case.graph.stages[0].placements[0],
            loaded=case.loaded[0],
            runtime={},
            offered_at=0.0,
         )
         for index, item in enumerate(items)
      )
   )
   assert refusal == "mixed_start_positions"
   assert SEQUENTIAL_DISPATCH_MODE  # the split mode remains the honest fallback


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
@pytest.mark.parametrize("int8", [False, True])
def test_qwen_ragged_decode_block_matches_each_member_alone(architecture, int8):
   tensors = _qwen_tensors(architecture, int8=int8)
   rng = np.random.default_rng(99)
   widths = (3, 7, 1)
   pasts = [_past(rng, width) for width in widths]
   hidden = mx.array(rng.standard_normal((3, 1, _HIDDEN)).astype(np.float32))

   batched, batched_kv = mlx_runtime._qwen2_block_with_kv(
      hidden,
      tensors,
      "model.layers.0.",
      _QWEN_CONFIG,
      widths,
      list(pasts),
      architecture,
   )
   mx.eval(batched, batched_kv)
   assert isinstance(batched_kv, list) and len(batched_kv) == 3
   for index, width in enumerate(widths):
      alone, alone_kv = mlx_runtime._qwen2_block_with_kv(
         hidden[index : index + 1],
         tensors,
         "model.layers.0.",
         _QWEN_CONFIG,
         width,
         pasts[index],
         architecture,
      )
      mx.eval(alone, alone_kv)
      assert bytes(mx.contiguous(batched[index : index + 1])) == bytes(
         mx.contiguous(alone)
      ), f"member {index} hidden differs from its single-member block"
      for mine, theirs in zip(batched_kv[index], alone_kv):
         assert tuple(mine.shape) == tuple(theirs.shape) == (
            1,
            _QWEN_CONFIG["n_kv_head"],
            width + 1,
            _QWEN_CONFIG["head_dim"],
         )
         assert bytes(mx.contiguous(mine)) == bytes(mx.contiguous(theirs))


def test_qwen_ragged_block_rejects_a_member_count_mismatch():
   tensors = _qwen_tensors("qwen2", int8=False)
   rng = np.random.default_rng(7)
   hidden = mx.array(rng.standard_normal((2, 1, _HIDDEN)).astype(np.float32))
   with pytest.raises(mlx_runtime.MLXRuntimeError, match="ragged_batch_member_mismatch"):
      mlx_runtime._qwen2_block_with_kv(
         hidden,
         tensors,
         "model.layers.0.",
         _QWEN_CONFIG,
         (2,),
         [_past(rng, 2)],
         "qwen2",
      )
