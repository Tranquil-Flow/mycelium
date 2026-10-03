"""Persistent packed int8 execution of the MLX stage runtime.

Failure modes this module catches:

* the packed layout dequantizing to anything but the exact signed int8 values;
* a packed linear that drifts from the per-call float32 upcast beyond float32
  accumulation-order noise, or changes a quantized greedy decision;
* a packed row whose result depends on how many rows share the call (that would
  break exact batched versus target-only parity);
* an unpackable or row-unstable stage silently mixing kernels instead of
  falling back whole-stage to the per-call path;
* a decode-time call that still converts a whole int8 matrix to float32.
"""

from __future__ import annotations

import mlx.core as mx
import pytest

import mycelium_router.mlx_runtime as mlx_runtime
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import (
   INT8_EXECUTION_NONE,
   INT8_EXECUTION_PER_CALL_MATERIALIZATION,
   INT8_EXECUTION_PERSISTENT_PACKED,
   PackedInt8RowwiseWeight,
   _pack_int8_rowwise,
   _persistent_int8_tensors,
   _qwen2_block_with_kv,
   _qwen2_linear,
)
from weight_quantization import Int8RowwiseWeight


def _int8_weight(rows: int, columns: int, seed: int) -> Int8RowwiseWeight:
   values = mx.random.randint(-127, 128, (rows, columns), key=mx.random.key(seed))
   scales = mx.random.uniform(1e-3, 2e-2, (rows,), key=mx.random.key(seed + 1))
   weight = Int8RowwiseWeight(values.astype(mx.int8), scales.astype(mx.float32))
   mx.eval(weight.values, weight.scales)
   return weight


def _close(actual: mx.array, expected: mx.array, relative: float = 1e-5) -> bool:
   """Equal up to float32 accumulation-order noise, relative to the magnitude."""

   scale = max(1.0, float(mx.max(mx.abs(expected)).item()))
   return float(mx.max(mx.abs(actual - expected)).item()) <= relative * scale


def _legacy_linear(hidden: mx.array, weight: Int8RowwiseWeight) -> mx.array:
   return _qwen2_linear(hidden, Int8RowwiseWeight(weight.values, weight.scales))


def test_packed_state_dequantizes_to_the_exact_int8_values():
   weight = _int8_weight(96, 256, 1)
   packed = _pack_int8_rowwise(weight)
   assert isinstance(packed, PackedInt8RowwiseWeight)
   assert packed.values is weight.values and packed.scales is weight.scales
   restored = mx.dequantize(
      packed.packed,
      packed.group_scales,
      packed.group_biases,
      group_size=mlx_runtime._PACKED_INT8_GROUP_SIZE,
      bits=mlx_runtime._PACKED_INT8_BITS,
   )
   assert bool(mx.array_equal(restored, weight.values.astype(mx.float32)).item())


@pytest.mark.parametrize("rows", [1, 2, 8, 31, 32, 40])
def test_packed_linear_matches_the_per_call_upcast(rows):
   weight = _int8_weight(160, 384, 3)
   packed = _pack_int8_rowwise(weight)
   hidden = mx.random.normal((1, rows, 384), key=mx.random.key(rows))
   expected = _legacy_linear(hidden, weight)
   actual = _qwen2_linear(hidden, packed)
   assert actual.shape == expected.shape and actual.dtype == mx.float32
   assert _close(actual, expected)
   last = actual[0, -1, :].tolist()
   assert quantized_greedy_token_id(last) == quantized_greedy_token_id(
      expected[0, -1, :].tolist()
   )


@pytest.mark.parametrize("shape", [(128, 256), (512, 384), (1024, 128)])
def test_packed_rows_do_not_depend_on_the_rows_sharing_the_call(shape):
   packed = _pack_int8_rowwise(_int8_weight(*shape, seed=7))
   probe = mx.random.normal((80, shape[1]), key=mx.random.key(11))
   reference = _qwen2_linear(probe, packed)
   for rows in (1, 2, 3, 8, 9, 10, 11, 12, 16, 24, 31, 32, 33, 64):
      partial = _qwen2_linear(probe[:rows], packed)
      assert bool(mx.array_equal(partial, reference[:rows]).item()), rows
   # Batched as (batch, sequence, width) the same rows are still identical.
   batched = _qwen2_linear(probe[:24].reshape(3, 8, shape[1]), packed)
   assert bool(mx.array_equal(batched.reshape(24, -1), reference[:24]).item())


def test_stage_mapping_is_packed_once_and_leaves_other_tensors_alone():
   norm = mx.ones((256,), dtype=mx.float32)
   tensors = {
      "model.layers.0.mlp.up_proj.weight": _int8_weight(512, 256, 5),
      "model.layers.0.input_layernorm.weight": norm,
   }
   execution, mode = _persistent_int8_tensors(tensors)
   assert mode == INT8_EXECUTION_PERSISTENT_PACKED
   assert isinstance(execution["model.layers.0.mlp.up_proj.weight"], PackedInt8RowwiseWeight)
   assert execution["model.layers.0.input_layernorm.weight"] is norm
   assert not isinstance(tensors["model.layers.0.mlp.up_proj.weight"], PackedInt8RowwiseWeight)


def test_stage_without_int8_weights_is_not_repacked():
   tensors = {"w": mx.ones((4, 128), dtype=mx.float32)}
   execution, mode = _persistent_int8_tensors(tensors)
   assert execution is tensors and mode == INT8_EXECUTION_NONE


def test_an_unpackable_matrix_falls_back_whole_stage():
   tensors = {
      "a.weight": _int8_weight(64, 256, 1),
      "b.weight": _int8_weight(64, 200, 2),  # not a multiple of the group size
   }
   execution, mode = _persistent_int8_tensors(tensors)
   assert mode == INT8_EXECUTION_PER_CALL_MATERIALIZATION
   assert execution is tensors


def test_a_row_unstable_kernel_falls_back_whole_stage(monkeypatch):
   # Without the row floor MLX switches kernels with the row count, so the
   # construction-time check must refuse packing rather than break parity.
   monkeypatch.setattr(mlx_runtime, "_PACKED_INT8_MINIMUM_ROWS", 1)
   tensors = {"a.weight": _int8_weight(3584, 3584, 1)}
   execution, mode = _persistent_int8_tensors(tensors)
   assert mode == INT8_EXECUTION_PER_CALL_MATERIALIZATION
   assert execution is tensors


def test_packed_decode_never_upcasts_a_whole_int8_matrix(monkeypatch):
   config = {
      "n_head": 2,
      "n_kv_head": 1,
      "head_dim": 64,
      "rope_theta": 10000.0,
      "rms_norm_epsilon": 1e-6,
   }
   hidden_size, intermediate = 128, 256
   prefix = "model.layers.0."
   tensors = {
      prefix + "input_layernorm.weight": mx.ones((hidden_size,)),
      prefix + "post_attention_layernorm.weight": mx.ones((hidden_size,)),
      prefix + "self_attn.q_proj.weight": _int8_weight(128, hidden_size, 1),
      prefix + "self_attn.k_proj.weight": _int8_weight(64, hidden_size, 2),
      prefix + "self_attn.v_proj.weight": _int8_weight(64, hidden_size, 3),
      prefix + "self_attn.o_proj.weight": _int8_weight(hidden_size, 128, 4),
      prefix + "self_attn.q_proj.bias": mx.zeros((128,)),
      prefix + "self_attn.k_proj.bias": mx.zeros((64,)),
      prefix + "self_attn.v_proj.bias": mx.zeros((64,)),
      prefix + "mlp.gate_proj.weight": _int8_weight(intermediate, hidden_size, 5),
      prefix + "mlp.up_proj.weight": _int8_weight(intermediate, hidden_size, 6),
      prefix + "mlp.down_proj.weight": _int8_weight(hidden_size, intermediate, 7),
   }
   execution, mode = _persistent_int8_tensors(tensors)
   assert mode == INT8_EXECUTION_PERSISTENT_PACKED
   prompt = mx.random.normal((1, 5, hidden_size), key=mx.random.key(1))
   expected_prefill, expected_kv = _qwen2_block_with_kv(
      prompt, tensors, prefix, config, 0, None
   )
   actual_prefill, actual_kv = _qwen2_block_with_kv(
      prompt, execution, prefix, config, 0, None
   )
   assert _close(actual_prefill, expected_prefill)

   upcasts: list[tuple[int, ...]] = []
   original_astype = mx.array.astype

   def counting_astype(self, dtype, *args, **kwargs):
      if self.dtype == mx.int8 and self.ndim == 2 and dtype == mx.float32:
         upcasts.append(tuple(self.shape))
      return original_astype(self, dtype, *args, **kwargs)

   monkeypatch.setattr(mx.array, "astype", counting_astype)
   step = mx.random.normal((1, 1, hidden_size), key=mx.random.key(2))
   decoded, _ = _qwen2_block_with_kv(step, execution, prefix, config, 5, actual_kv)
   mx.eval(decoded)
   assert upcasts == []
   # The guard is not vacuous: the per-call path upcasts every matrix.
   legacy, _ = _qwen2_block_with_kv(step, tensors, prefix, config, 5, expected_kv)
   mx.eval(legacy)
   assert len(upcasts) == 7
   monkeypatch.undo()
   assert _close(decoded, legacy)
