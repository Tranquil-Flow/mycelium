# SPDX-License-Identifier: AGPL-3.0-only
"""Row-stable Qwen projections: a row's arithmetic never depends on its batch.

MLX computes a one-row product with a different kernel than a multi-row one, so
the same row could differ in the last bits depending on how many requests share
a runtime batch.  Qwen2/Qwen3 projections are therefore computed so that every
row is bit-identical whether it runs alone or in a batch of any size, which is
what lets a batched request match its target-only output exactly.
"""

from __future__ import annotations

import mlx.core as mx
import numpy as np
import pytest

import mycelium_router.mlx_runtime as mlx_runtime
from weight_quantization import Int8RowwiseWeight

_QWEN_CONFIG = {
   "n_head": 4,
   "n_kv_head": 2,
   "head_dim": 4,
   "rms_norm_epsilon": 1e-6,
   "rope_theta": 10000.0,
}


_HIDDEN = 16


_FFN = 24


def _qwen_tensors(architecture: str, *, int8: bool):
   rng = np.random.default_rng(1234)
   prefix = "model.layers.0."
   heads = _QWEN_CONFIG["n_head"] * _QWEN_CONFIG["head_dim"]
   kv = _QWEN_CONFIG["n_kv_head"] * _QWEN_CONFIG["head_dim"]

   def matrix(rows, cols):
      values = rng.standard_normal((rows, cols)).astype(np.float32) * 0.3
      if not int8:
         return mx.array(values)
      scales = np.maximum(np.abs(values).max(axis=1), 1e-6) / 127.0
      quantized = np.clip(np.round(values / scales[:, None]), -127, 127)
      return Int8RowwiseWeight(
         values=mx.array(quantized.astype(np.int8)),
         scales=mx.array(scales.astype(np.float32)),
      )

   tensors = {
      prefix + "input_layernorm.weight": mx.array(
         rng.uniform(0.5, 1.5, _HIDDEN).astype(np.float32)
      ),
      prefix + "post_attention_layernorm.weight": mx.array(
         rng.uniform(0.5, 1.5, _HIDDEN).astype(np.float32)
      ),
      prefix + "self_attn.q_proj.weight": matrix(heads, _HIDDEN),
      prefix + "self_attn.k_proj.weight": matrix(kv, _HIDDEN),
      prefix + "self_attn.v_proj.weight": matrix(kv, _HIDDEN),
      prefix + "self_attn.o_proj.weight": matrix(_HIDDEN, heads),
      prefix + "mlp.gate_proj.weight": matrix(_FFN, _HIDDEN),
      prefix + "mlp.up_proj.weight": matrix(_FFN, _HIDDEN),
      prefix + "mlp.down_proj.weight": matrix(_HIDDEN, _FFN),
   }
   if architecture == "qwen2":
      for name, width in (("q", heads), ("k", kv), ("v", kv)):
         tensors[prefix + f"self_attn.{name}_proj.bias"] = mx.array(
            rng.standard_normal(width).astype(np.float32) * 0.1
         )
   else:
      for name in ("q", "k"):
         tensors[prefix + f"self_attn.{name}_norm.weight"] = mx.array(
            rng.uniform(0.5, 1.5, _QWEN_CONFIG["head_dim"]).astype(np.float32)
         )
   return tensors


def _past(rng, width):
   shape = (1, _QWEN_CONFIG["n_kv_head"], width, _QWEN_CONFIG["head_dim"])
   return (
      mx.array(rng.standard_normal(shape).astype(np.float32)),
      mx.array(rng.standard_normal(shape).astype(np.float32)),
   )


@pytest.mark.parametrize("int8", [False, True])
def test_qwen_stacked_equal_width_block_matches_each_member_alone(int8):
   """Members at one position with equal retained widths share one stacked
   pass; each row is still bit-identical to that member computed alone."""

   tensors = _qwen_tensors("qwen2", int8=int8)
   rng = np.random.default_rng(5)
   pasts = [_past(rng, 4) for _ in range(3)]
   stacked = (
      mx.concatenate([past[0] for past in pasts], axis=0),
      mx.concatenate([past[1] for past in pasts], axis=0),
   )
   hidden = mx.array(rng.standard_normal((3, 1, _HIDDEN)).astype(np.float32))
   batched, (all_key, all_value) = mlx_runtime._qwen2_block_with_kv(
      hidden, tensors, "model.layers.0.", _QWEN_CONFIG, 4, stacked, "qwen2"
   )
   mx.eval(batched, all_key, all_value)
   for index in range(3):
      alone, (key, value) = mlx_runtime._qwen2_block_with_kv(
         hidden[index : index + 1],
         tensors,
         "model.layers.0.",
         _QWEN_CONFIG,
         4,
         pasts[index],
         "qwen2",
      )
      mx.eval(alone, key, value)
      assert bytes(mx.contiguous(batched[index : index + 1])) == bytes(
         mx.contiguous(alone)
      )
      assert bytes(mx.contiguous(all_key[index : index + 1])) == bytes(
         mx.contiguous(key)
      )
      assert bytes(mx.contiguous(all_value[index : index + 1])) == bytes(
         mx.contiguous(value)
      )


def test_row_stable_matmul_rows_do_not_depend_on_the_batch_size():
   rng = np.random.default_rng(11)
   weight_t = mx.array(rng.standard_normal((48, 40)).astype(np.float32))
   rows = mx.array(rng.standard_normal((9, 1, 48)).astype(np.float32))
   alone = [mlx_runtime._row_stable_matmul(rows[i : i + 1], weight_t) for i in range(9)]
   mx.eval(*alone)
   for size in (2, 3, 9):
      together = mlx_runtime._row_stable_matmul(rows[:size], weight_t)
      mx.eval(together)
      for index in range(size):
         assert bytes(mx.contiguous(together[index : index + 1])) == bytes(
            mx.contiguous(alone[index])
         )
   assert tuple(alone[0].shape) == (1, 1, 40)
