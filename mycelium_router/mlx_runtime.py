"""Assignment- and graph-bound MLX implementation of the Router RuntimePort.

The port executes one already loaded GPT-2 stage at a time. It does not load
weights, use pickle/JSON tensor arrays, fuse batches, or make any
distributed-transport claim. PREFILL establishes assignment-bound stage-local
KV state; DECODE consumes exactly one token position from that state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Condition, RLock, Thread
from types import MappingProxyType
from typing import Any, NoReturn

import mlx.core as mx
import numpy as np

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   MemberExecution,
   RuntimeBatchRequest,
)
from mycelium_router.a10_runtime_batch import (
   BACKEND_INVOCATIONS_PER_OBSERVED_BATCH,
   MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
   MINIMUM_PHYSICAL_BATCH_MEMBERS,
   RuntimeBatchObservation,
)
from mycelium_router.contracts import (
   ExecutionGraph,
   HopWorkItem,
   RuntimeBatch,
   RuntimeBatchKey,
   RuntimeResult,
   Stage,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.payloads import (
   PayloadError,
   decode_activation,
   decode_token_ids,
   encode_activation,
)
from mycelium_router.stage_signatures import stage_signature_for_backend
from mycelium_router.validation import ContractError, validate_execution_graph
from runtime_contracts import validate_normalized_mlx_runtime
from runtime_loader import (
   LoadedStage,
   RuntimeExecutionError,
   canonical_json,
   execute_loaded_stage as _execute_loaded_stage,
)
from weight_quantization import Int8RowwiseWeight


_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}
_MLX_DTYPES = {
   "float16": mx.float16,
   "bfloat16": mx.bfloat16,
   "float32": mx.float32,
}
_SUPPORTED_COMPONENTS = {"input_embedding", "decoder", "final_norm", "lm_head"}
_MAX_RETAINED_OPERATIONS = 4096
_BASE_LOAD_PROOF_FIELDS = frozenset({
   "protocol",
   "deployment_id",
   "deployment_epoch",
   "assignment_id",
   "node_id",
   "model_id",
   "manifest_digest",
   "resolved_commit",
   "loaded_range",
   "loaded_components",
   "loaded_tensor_keys",
   "loaded_tensor_digest",
   "resolved_component_aliases",
   "runtime",
   "runtime_identity",
   "probe_shape",
   "probe_digest",
   "load_generation",
   "control_plane_binding",
   "route_ready",
   "claim_boundary",
})
_STAGE_PACK_PROOF_FIELDS = frozenset(
   {"stage_pack_digest", "stage_pack_verification_digest"}
)


class MLXRuntimeError(ValueError):
   """Fail-closed binding or execution error with a stable code."""

   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


def _reject(code: str, detail: str = "") -> NoReturn:
   raise MLXRuntimeError(code, detail)


def _plain_json(value: Any) -> Any:
   """Thaw immutable proof mappings through the loader's canonical codec."""

   try:
      return json.loads(canonical_json(value))
   except (TypeError, ValueError, json.JSONDecodeError) as exc:
      raise MLXRuntimeError("noncanonical_load_proof") from exc


def _stage_signature(graph: ExecutionGraph, stage: Stage, proof: Mapping[str, Any]) -> str:
   runtime = proof.get("runtime")
   if not isinstance(runtime, Mapping):
      _reject("invalid_loaded_stage_runtime", stage.stage_id)
   runtime_backend = runtime.get("backend")
   if not isinstance(runtime_backend, str):
      _reject("invalid_loaded_stage_runtime", stage.stage_id)
   try:
      return stage_signature_for_backend(graph, stage, runtime_backend)
   except ValueError as exc:
      raise MLXRuntimeError("invalid_loaded_stage_runtime", stage.stage_id) from exc


def _range_document(stage: Stage) -> dict[str, int]:
   return {
      "start_layer": stage.layer_range.start_layer,
      "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
      "layer_count": stage.layer_range.layer_count,
   }


@dataclass
class _KVState:
   request_id: str
   path_id: str
   path_attempt: int
   placement_id: str
   assignment_id: str
   manifest_digest: str
   deployment_epoch: int
   lease_expires_at: float
   next_position: int
   next_sequence: int
   cached_context_tokens: int
   layers: dict[int, tuple[mx.array, mx.array]]


@dataclass(frozen=True)
class _ReplayResult:
   fingerprint: str
   result: RuntimeResult


# ---------------------------------------------------------------------------
# A10 runtime batching: one real backend invocation over an accepted group
#
# The A10 contract makes runtime batching a *backend-invocation* fact: a claimed
# batch was computed TOGETHER in one invocation, never looped one member at a
# time.  ``MLXRuntimePort`` dispatches an offered group with a single padded
# batched forward pass and publishes the runtime-owned observation record
# ``mycelium.runtime_batch_observation.v1`` for it.  A group this port cannot
# serve as one invocation is served one member at a time and reported honestly
# (``dispatch_mode="sequential_dispatch"``, one real invocation per executed
# member); it never wears a batch label it did not earn.
#
# The single-invocation path runs only when every member is in a state the
# incumbent per-item path would accept, so no incumbent refusal is weakened,
# bypassed or re-spelled by batching.
#
# Claim boundary: deterministic in-process behaviour only.  No physical overlap,
# benchmark, browser, product or qualification claim is made or implied, and no
# closure requirement is earned here.
# ---------------------------------------------------------------------------

#: The frozen observation record this port publishes for an observed group.
A10_OBSERVATION_PROTOCOL = "mycelium.runtime_batch_observation.v1"
#: A group computed together in ONE backend invocation.
RUNTIME_BATCH_DISPATCH_MODE = "runtime_batch"
#: A group served one member at a time; honest, and never a batch.
SEQUENTIAL_DISPATCH_MODE = "sequential_dispatch"


@dataclass(frozen=True)
class RuntimeBatchMemberParity:
   """One member's deterministic parity against its own sequential reference.

   Only equality flags and one bounded scalar deviation are retained.  Payload
   bytes, logits, activations, KV bytes and token material never enter this
   record: parity compares decisions, not tensors.
   """

   member_ref: str
   decision_equal: bool
   payload_digest_equal: bool
   payload_shape_equal: bool
   maximum_absolute_deviation: float


@dataclass(frozen=True)
class RuntimeBatchParityVerdict:
   """The sequential-parity oracle verdict for one dispatched group.

   ``all_equal`` is the only claim the oracle makes: every member's decision and
   payload matched what the incumbent one-member-at-a-time path produced for the
   same work item.
   """

   batch_ref: str
   dispatch_mode: str
   batched_backend_invocations: int
   members: tuple[RuntimeBatchMemberParity, ...]

   @property
   def compared_member_count(self) -> int:
      return len(self.members)

   @property
   def all_equal(self) -> bool:
      return (
         self.batched_backend_invocations == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
         and len(self.members) >= MINIMUM_PHYSICAL_BATCH_MEMBERS
         and all(
            member.decision_equal
            and member.payload_digest_equal
            and member.payload_shape_equal
            for member in self.members
         )
      )


@dataclass
class _OfferedMember:
   """One privately offered candidate member of the next dispatched group."""

   member_ref: str
   item: HopWorkItem
   stage: Stage
   placement: Any
   loaded: LoadedStage
   runtime: Mapping[str, Any]
   offered_at: float


def _layer_norm(
   hidden: mx.array,
   weight: mx.array,
   bias: mx.array,
   epsilon: float,
) -> mx.array:
   compute = hidden.astype(mx.float32)
   mean = mx.mean(compute, axis=-1, keepdims=True)
   variance = mx.mean(mx.square(compute - mean), axis=-1, keepdims=True)
   normalized = (compute - mean) * mx.rsqrt(variance + epsilon)
   return (normalized * weight.astype(mx.float32) + bias.astype(mx.float32)).astype(
      hidden.dtype
   )


def _gelu_new(hidden: mx.array) -> mx.array:
   compute = hidden.astype(mx.float32)
   result = 0.5 * compute * (
      1.0
      + mx.tanh(
         math.sqrt(2.0 / math.pi)
         * (compute + 0.044715 * mx.power(compute, 3))
      )
   )
   return result.astype(hidden.dtype)


def _gpt2_attend(
   query: mx.array,
   key: mx.array,
   value: mx.array,
   past: tuple[mx.array, mx.array] | None,
   head_size: int,
) -> tuple[mx.array, mx.array, mx.array]:
   """GPT-2 attention of ``query`` over retained ``past`` plus the new keys."""

   sequence = int(query.shape[2])
   if past is None:
      all_key = key
      all_value = value
   else:
      all_key = mx.concatenate((past[0], key), axis=2)
      all_value = mx.concatenate((past[1], value), axis=2)
   scores = mx.matmul(query, all_key.transpose(0, 1, 3, 2)) / math.sqrt(head_size)
   if sequence > 1:
      # A multi-position chunk is causal against its own positions whether or not
      # it extends retained KV: query ``offset + i`` may attend keys ``0..offset+i``
      # only.  (A single-position decode needs no mask: every retained key is at
      # or before the query position.)
      offset = 0 if past is None else int(past[0].shape[2])
      query_positions = mx.arange(offset, offset + sequence)
      key_positions = mx.arange(int(all_key.shape[2]))
      causal = query_positions[:, None] >= key_positions[None, :]
      scores = mx.where(
         causal[None, None, :, :],
         scores,
         mx.array(-math.inf, dtype=scores.dtype),
      )
   weights = mx.softmax(scores, axis=-1)
   return mx.matmul(weights, all_value), all_key, all_value


def _gpt2_block_with_kv(
   hidden: mx.array,
   tensors: Mapping[str, mx.array],
   prefix: str,
   n_head: int,
   epsilon: float,
   past: tuple[mx.array, mx.array] | list[tuple[mx.array, mx.array] | None] | None,
) -> tuple[mx.array, Any]:
   residual = hidden
   normalized = _layer_norm(
      hidden,
      tensors[f"{prefix}ln_1.weight"],
      tensors[f"{prefix}ln_1.bias"],
      epsilon,
   )
   qkv = mx.matmul(normalized, tensors[f"{prefix}attn.c_attn.weight"]) + tensors[
      f"{prefix}attn.c_attn.bias"
   ]
   query, key, value = mx.split(qkv, 3, axis=-1)
   batch, sequence, hidden_size = query.shape
   head_size = int(hidden_size) // n_head

   def split_heads(array: mx.array) -> mx.array:
      return array.reshape(batch, sequence, n_head, head_size).transpose(0, 2, 1, 3)

   query = split_heads(query)
   key = split_heads(key)
   value = split_heads(value)
   layer_kv: Any
   if isinstance(past, list):
      # Ragged decode group: every member attends only its own retained KV,
      # through exactly the single-member arithmetic, so no member ever sees
      # padding or another member's keys.
      if len(past) != int(batch):
         _reject("ragged_batch_member_mismatch")
      rows: list[mx.array] = []
      member_kv: list[tuple[mx.array, mx.array]] = []
      for index, member_past in enumerate(past):
         row, member_key, member_value = _gpt2_attend(
            query[index : index + 1],
            key[index : index + 1],
            value[index : index + 1],
            member_past,
            head_size,
         )
         rows.append(row)
         member_kv.append((member_key, member_value))
      attention = mx.concatenate(rows, axis=0)
      layer_kv = member_kv
   else:
      attention, all_key, all_value = _gpt2_attend(
         query, key, value, past, head_size
      )
      layer_kv = (all_key, all_value)
   attention = attention.transpose(0, 2, 1, 3).reshape(batch, sequence, hidden_size)
   attention = (
      mx.matmul(attention, tensors[f"{prefix}attn.c_proj.weight"])
      + tensors[f"{prefix}attn.c_proj.bias"]
   )
   hidden = residual + attention

   residual = hidden
   normalized = _layer_norm(
      hidden,
      tensors[f"{prefix}ln_2.weight"],
      tensors[f"{prefix}ln_2.bias"],
      epsilon,
   )
   feed_forward = (
      mx.matmul(normalized, tensors[f"{prefix}mlp.c_fc.weight"])
      + tensors[f"{prefix}mlp.c_fc.bias"]
   )
   feed_forward = _gelu_new(feed_forward)
   feed_forward = (
      mx.matmul(feed_forward, tensors[f"{prefix}mlp.c_proj.weight"])
      + tensors[f"{prefix}mlp.c_proj.bias"]
   )
   return residual + feed_forward, layer_kv


def _rms_norm(
   hidden: mx.array,
   weight: mx.array,
   epsilon: float,
) -> mx.array:
   dtype = hidden.dtype
   compute = hidden.astype(mx.float32)
   normalized = compute * mx.rsqrt(
      mx.mean(mx.square(compute), axis=-1, keepdims=True) + epsilon
   )
   return (normalized * weight.astype(mx.float32)).astype(dtype)


def _row_stable_matmul(hidden: mx.array, weight_t: mx.array) -> mx.array:
   """``hidden @ weight_t`` whose every row is independent of the batch size.

   MLX computes a single-row product with a different kernel than a multi-row
   one, and the two differ in the last bits.  A lone row is therefore computed
   as row 0 of a two-row product (the second row is zeros and is discarded), so
   a request's arithmetic is bit-identical whether it runs alone or as any
   member of a runtime batch; exact output parity between batched and
   target-only execution then holds by construction.
   """

   rows = 1
   for extent in hidden.shape[:-1]:
      rows *= int(extent)
   if rows == 1:
      padded = mx.concatenate((hidden, mx.zeros_like(hidden)), axis=0)
      return mx.matmul(padded, weight_t)[:1]
   return mx.matmul(hidden, weight_t)


#: ``int8_execution`` of a port whose int8 linears run on persistent packed state.
INT8_EXECUTION_PERSISTENT_PACKED = "persistent_packed_int8"
#: ``int8_execution`` of a port that upcasts every int8 weight per linear call.
INT8_EXECUTION_PER_CALL_MATERIALIZATION = "per_call_float32_materialization"
#: ``int8_execution`` of a port that holds no row-wise int8 weights.
INT8_EXECUTION_NONE = "none"

# Persistent packed int8 execution (A9 ``persistent_int8_execution``): the row-wise
# int8 values are packed ONCE, at port construction, into MLX's unsigned 8-bit
# affine layout with group scale 1 and group bias -128.  Every packed value
# therefore dequantizes to exactly its signed int8 value, the row scale is
# applied to the product exactly as before, and no decode-time call converts a
# whole weight matrix to float32.
_PACKED_INT8_GROUP_SIZE = 128
_PACKED_INT8_BITS = 8
# MLX computes a product with few rows by a vector kernel and one with many rows
# by a tiled matrix kernel, and the two differ in the last bits.  Every packed
# product is therefore computed with at least this many rows (zero rows pad a
# smaller input and are discarded), so a row's arithmetic does not depend on how
# many other rows share the call.  Port construction verifies that property for
# every packed shape and refuses packing when it does not hold.
_PACKED_INT8_MINIMUM_ROWS = 32


@dataclass(frozen=True, slots=True)
class PackedInt8RowwiseWeight(Int8RowwiseWeight):
   """A row-wise int8 weight plus its persistent packed execution state."""

   packed: Any
   group_scales: Any
   group_biases: Any


def _pack_int8_rowwise(weight: Int8RowwiseWeight) -> Int8RowwiseWeight:
   """Return ``weight`` with persistent packed state, or unchanged if unpackable."""

   values = weight.values
   if (
      isinstance(weight, PackedInt8RowwiseWeight)
      or not isinstance(values, mx.array)
      or values.dtype != mx.int8
      or values.ndim != 2
   ):
      return weight
   rows, columns = (int(extent) for extent in values.shape)
   if rows < 1 or columns < 1 or columns % _PACKED_INT8_GROUP_SIZE:
      return weight
   unsigned = (values.astype(mx.int16) + 128).astype(mx.uint8)
   packed = (
      unsigned.reshape(rows, columns // 4, 4)
      .view(mx.uint32)
      .reshape(rows, columns // 4)
   )
   groups = columns // _PACKED_INT8_GROUP_SIZE
   group_scales = mx.ones((rows, groups), dtype=mx.float32)
   group_biases = mx.full((rows, groups), -128.0, dtype=mx.float32)
   mx.eval(packed, group_scales, group_biases)
   return PackedInt8RowwiseWeight(
      values=values,
      scales=weight.scales,
      packed=packed,
      group_scales=group_scales,
      group_biases=group_biases,
   )


def _packed_int8_product(hidden: mx.array, weight: PackedInt8RowwiseWeight) -> mx.array:
   """``hidden @ values.T`` from the packed state, row-stable by construction."""

   width = int(hidden.shape[-1])
   leading = tuple(int(extent) for extent in hidden.shape[:-1])
   flat = hidden.reshape(-1, width)
   rows = int(flat.shape[0])
   if rows < _PACKED_INT8_MINIMUM_ROWS:
      flat = mx.concatenate(
         (
            flat,
            mx.zeros((_PACKED_INT8_MINIMUM_ROWS - rows, width), dtype=flat.dtype),
         ),
         axis=0,
      )
   product = mx.quantized_matmul(
      flat,
      weight.packed,
      weight.group_scales,
      weight.group_biases,
      transpose=True,
      group_size=_PACKED_INT8_GROUP_SIZE,
      bits=_PACKED_INT8_BITS,
   )[:rows]
   return product.reshape(*leading, int(product.shape[-1]))


def _packed_int8_rows_are_call_independent(weight: PackedInt8RowwiseWeight) -> bool:
   """Every row of a packed product must not depend on the rows it shares a call with."""

   width = int(weight.packed.shape[1]) * 4
   floor = _PACKED_INT8_MINIMUM_ROWS
   # The reference row count is independent of the floor and well inside the
   # tiled kernel's range, so a floor that admits the vector kernel is caught.
   total = max(2 * floor + 1, 129)
   probe = mx.random.normal((total, width), key=mx.random.key(0x5EED))
   reference = _packed_int8_product(probe, weight)
   mx.eval(reference)
   for rows in sorted({1, 2, 8, max(1, floor - 1), floor, floor + 1, 2 * floor + 1}):
      partial = _packed_int8_product(probe[:rows], weight)
      if not bool(mx.array_equal(partial, reference[:rows]).item()):
         return False
   return True


def _persistent_int8_tensors(
   tensors: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
   """Pack every row-wise int8 matrix once; fall back whole-stage if unsafe.

   Returns the execution tensor mapping and the resulting ``int8_execution``.
   Packing is all-or-nothing per mapping so a stage never mixes kernels.
   """

   quantized = {
      key: value
      for key, value in tensors.items()
      if isinstance(value, Int8RowwiseWeight)
   }
   if not quantized:
      return tensors, INT8_EXECUTION_NONE
   packed: dict[str, Any] = {}
   for key, value in quantized.items():
      candidate = _pack_int8_rowwise(value)
      if not isinstance(candidate, PackedInt8RowwiseWeight):
         return tensors, INT8_EXECUTION_PER_CALL_MATERIALIZATION
      packed[key] = candidate
   verified_shapes: set[tuple[int, int]] = set()
   for value in packed.values():
      shape = tuple(int(extent) for extent in value.values.shape)
      if shape in verified_shapes:
         continue
      if not _packed_int8_rows_are_call_independent(value):
         return tensors, INT8_EXECUTION_PER_CALL_MATERIALIZATION
      verified_shapes.add(shape)
   execution = dict(tensors)
   execution.update(packed)
   return MappingProxyType(execution), INT8_EXECUTION_PERSISTENT_PACKED


def _qwen2_linear(hidden: mx.array, weight: Any) -> mx.array:
   if isinstance(weight, PackedInt8RowwiseWeight):
      return _packed_int8_product(hidden, weight) * weight.scales
   if isinstance(weight, Int8RowwiseWeight):
      projected = _row_stable_matmul(
         hidden,
         weight.values.astype(mx.float32).transpose(1, 0),
      )
      return projected * weight.scales
   return _row_stable_matmul(hidden, weight.transpose(1, 0))


def _qwen2_embedding(weight: Any, token_ids: mx.array) -> mx.array:
   if isinstance(weight, Int8RowwiseWeight):
      return (
         weight.values[token_ids].astype(mx.float32)
         * weight.scales[token_ids, None]
      )
   return weight[token_ids]


def _qwen2_rope_at_position(
   query: mx.array,
   key: mx.array,
   *,
   position: int,
   theta: float,
) -> tuple[mx.array, mx.array]:
   sequence = int(query.shape[2])
   head_dim = int(query.shape[3])
   exponent = mx.arange(0, head_dim, 2, dtype=mx.float32) / head_dim
   inv_freq = 1.0 / mx.power(mx.array(theta, dtype=mx.float32), exponent)
   positions = mx.arange(position, position + sequence, dtype=mx.float32)
   frequencies = positions.reshape(-1, 1) * inv_freq.reshape(1, -1)
   embedding = mx.concatenate((frequencies, frequencies), axis=-1)
   cosine = mx.cos(embedding)[None, None, :, :]
   sine = mx.sin(embedding)[None, None, :, :]

   def rotate_half(value: mx.array) -> mx.array:
      first, second = mx.split(value, 2, axis=-1)
      return mx.concatenate((-second, first), axis=-1)

   return (
      query * cosine + rotate_half(query) * sine,
      key * cosine + rotate_half(key) * sine,
   )


def _qwen2_attend(
   query: mx.array,
   key: mx.array,
   value: mx.array,
   past: tuple[mx.array, mx.array] | None,
   position: int,
   config: Mapping[str, Any],
) -> tuple[mx.array, mx.array, mx.array]:
   """Rotate at ``position`` and attend over retained ``past`` plus the new keys."""

   n_head = int(config["n_head"])
   n_kv_head = int(config["n_kv_head"])
   head_dim = int(config["head_dim"])
   sequence = int(query.shape[2])
   query, key = _qwen2_rope_at_position(
      query,
      key,
      position=position,
      theta=float(config["rope_theta"]),
   )
   if past is None:
      all_key = key
      all_value = value
   else:
      all_key = mx.concatenate((past[0], key), axis=2)
      all_value = mx.concatenate((past[1], value), axis=2)
   repeats = n_head // n_kv_head
   attention_key = mx.repeat(all_key, repeats, axis=1)
   attention_value = mx.repeat(all_value, repeats, axis=1)
   scores = mx.matmul(
      query, attention_key.transpose(0, 1, 3, 2)
   ) / math.sqrt(head_dim)
   query_positions = mx.arange(position, position + sequence)
   key_positions = mx.arange(int(all_key.shape[2]))
   causal = query_positions[:, None] >= key_positions[None, :]
   probabilities = mx.softmax(
      mx.where(
         causal[None, None, :, :],
         scores,
         mx.array(-math.inf, dtype=scores.dtype),
      ),
      axis=-1,
   )
   return mx.matmul(probabilities, attention_value), all_key, all_value


def _qwen2_block_with_kv(
   hidden: mx.array,
   tensors: Mapping[str, Any],
   prefix: str,
   config: Mapping[str, Any],
   position: int | tuple[int, ...],
   past: tuple[mx.array, mx.array] | list[tuple[mx.array, mx.array] | None] | None,
   architecture: str = "qwen2",
) -> tuple[mx.array, Any]:
   n_head = int(config["n_head"])
   n_kv_head = int(config["n_kv_head"])
   head_dim = int(config["head_dim"])
   residual = hidden
   normalized = _rms_norm(
      hidden,
      tensors[prefix + "input_layernorm.weight"],
      float(config["rms_norm_epsilon"]),
   )
   query = _qwen2_linear(
      normalized, tensors[prefix + "self_attn.q_proj.weight"]
   )
   key = _qwen2_linear(
      normalized, tensors[prefix + "self_attn.k_proj.weight"]
   )
   value = _qwen2_linear(
      normalized, tensors[prefix + "self_attn.v_proj.weight"]
   )
   if architecture == "qwen2":
      query = query + tensors[prefix + "self_attn.q_proj.bias"]
      key = key + tensors[prefix + "self_attn.k_proj.bias"]
      value = value + tensors[prefix + "self_attn.v_proj.bias"]
   batch, sequence = int(hidden.shape[0]), int(hidden.shape[1])
   query = query.reshape(batch, sequence, n_head, head_dim).transpose(0, 2, 1, 3)
   key = key.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
   value = value.reshape(batch, sequence, n_kv_head, head_dim).transpose(0, 2, 1, 3)
   if architecture == "qwen3":
      query = _rms_norm(
         query,
         tensors[prefix + "self_attn.q_norm.weight"],
         float(config["rms_norm_epsilon"]),
      )
      key = _rms_norm(
         key,
         tensors[prefix + "self_attn.k_norm.weight"],
         float(config["rms_norm_epsilon"]),
      )
   layer_kv: Any
   if isinstance(past, list):
      # Ragged decode group: each member is rotated at its own position and
      # attends only its own retained KV, through the single-member arithmetic.
      if (
         len(past) != batch
         or not isinstance(position, tuple)
         or len(position) != batch
      ):
         _reject("ragged_batch_member_mismatch")
      rows: list[mx.array] = []
      member_kv: list[tuple[mx.array, mx.array]] = []
      for index, member_past in enumerate(past):
         row, member_key, member_value = _qwen2_attend(
            query[index : index + 1],
            key[index : index + 1],
            value[index : index + 1],
            member_past,
            int(position[index]),
            config,
         )
         rows.append(row)
         member_kv.append((member_key, member_value))
      attended = mx.concatenate(rows, axis=0)
      layer_kv = member_kv
   else:
      if not isinstance(position, int) or isinstance(position, bool):
         _reject("invalid_kv_position")
      attended, all_key, all_value = _qwen2_attend(
         query, key, value, past, position, config
      )
      layer_kv = (all_key, all_value)
   attended = attended.transpose(0, 2, 1, 3).reshape(batch, sequence, -1)
   hidden = residual + _qwen2_linear(
      attended, tensors[prefix + "self_attn.o_proj.weight"]
   )
   residual = hidden
   normalized = _rms_norm(
      hidden,
      tensors[prefix + "post_attention_layernorm.weight"],
      float(config["rms_norm_epsilon"]),
   )
   gate = _qwen2_linear(normalized, tensors[prefix + "mlp.gate_proj.weight"])
   gate = gate * mx.sigmoid(gate)
   up = _qwen2_linear(normalized, tensors[prefix + "mlp.up_proj.weight"])
   hidden = residual + _qwen2_linear(
      gate * up, tensors[prefix + "mlp.down_proj.weight"]
   )
   return hidden, layer_kv


class MLXRuntimePort:
   """Serial, item-isolated RuntimePort over assignment-bound LoadedStages."""

   decode_mode = "stage_local_kv"

   def __init__(
      self,
      node_id: str,
      graph: ExecutionGraph,
      loaded_stages: Mapping[str, LoadedStage],
      *,
      clock: Callable[[], float] | None = None,
      decode_mode: str = "stage_local_kv",
   ):
      if not isinstance(node_id, str) or not node_id or node_id != node_id.strip():
         _reject("invalid_runtime_node_id")
      if sys.byteorder != "little":
         _reject("unsupported_host_byte_order")
      if not isinstance(graph, ExecutionGraph):
         _reject("invalid_execution_graph")
      try:
         validate_execution_graph(graph)
      except (ContractError, AttributeError, TypeError, ValueError) as exc:
         code = exc.code if isinstance(exc, ContractError) else "invalid_execution_graph"
         raise MLXRuntimeError(code) from exc
      if not isinstance(graph.model_id, str) or not graph.model_id:
         _reject("missing_model_id")
      if not isinstance(loaded_stages, Mapping):
         _reject("invalid_loaded_stage_mapping")
      if decode_mode not in {"stage_local_kv", "complete_context_replay"}:
         _reject("invalid_runtime_decode_mode")
      if not loaded_stages:
         _reject("missing_local_loaded_stages")
      if not all(isinstance(key, str) and key for key in loaded_stages):
         _reject("invalid_loaded_stage_mapping")

      placement_records = {
         placement.placement_id: (stage, placement)
         for stage in graph.stages
         for placement in stage.placements
      }
      active_local_ids = {
         placement_id
         for placement_id, (_, placement) in placement_records.items()
         if placement.node_id == node_id and placement.lifecycle_state == "ACTIVE"
      }
      standby_local_ids = {
         placement_id
         for placement_id, (_, placement) in placement_records.items()
         if placement.node_id == node_id and placement.lifecycle_state == "STANDBY"
      }
      supplied_ids = set(loaded_stages)
      # A7: a standby port binds only local STANDBY placements.  It owns no
      # ordinary work: it never prefills and decodes only a successor path
      # whose KV a fenced cutover imported.  KV is keyed by path, so a standby
      # placement never shares a port with an ordinary placement of the same
      # route.
      standby_port = bool(supplied_ids) and supplied_ids <= standby_local_ids
      if standby_port:
         if decode_mode != "stage_local_kv":
            _reject("standby_requires_stage_local_kv")
         active_local_ids = set(supplied_ids)
      elif supplied_ids & standby_local_ids:
         _reject("standby_port_mixed_lifecycle", sorted(supplied_ids & standby_local_ids)[0])
      missing = active_local_ids - supplied_ids
      extra = supplied_ids - active_local_ids
      if missing:
         _reject("missing_active_local_placement", sorted(missing)[0])
      if extra:
         placement_id = sorted(extra)[0]
         record = placement_records.get(placement_id)
         if record is None:
            _reject("unknown_loaded_stage_placement", placement_id)
         if record[1].node_id != node_id:
            _reject("nonlocal_loaded_stage_placement", placement_id)
         _reject("inactive_loaded_stage_placement", placement_id)
      if not active_local_ids:
         _reject("missing_active_local_placement")

      self._validate_graph_stage_roles(graph)
      bound: dict[str, tuple[Stage, Any, LoadedStage, dict[str, Any]]] = {}
      canonical_runtime: Any = None
      for placement_id in sorted(active_local_ids):
         stage, placement = placement_records[placement_id]
         loaded = loaded_stages[placement_id]
         if not isinstance(loaded, LoadedStage):
            _reject("invalid_loaded_stage", placement_id)
         runtime = self._validate_loaded_binding(graph, stage, placement, loaded)
         if canonical_runtime is None:
            canonical_runtime = runtime
         elif runtime != canonical_runtime:
            _reject("loaded_stage_runtime_mismatch", placement_id)
         bound[placement_id] = (stage, placement, loaded, runtime)

      self.node_id = node_id
      self.graph = graph
      self.decode_mode = decode_mode
      self._architecture = canonical_runtime["architecture"]
      self._bound = MappingProxyType(bound)
      # Persistent packed int8 state is built once here (load/warmup), never
      # per operation; the authenticated LoadedStage tensors stay untouched.
      self._execution_tensors: dict[int, Mapping[str, Any]] = {}
      int8_modes: set[str] = set()
      for _stage, _placement, loaded, _runtime in bound.values():
         if self._architecture in {"qwen2", "qwen3"}:
            execution, mode = _persistent_int8_tensors(loaded.tensors)
         else:
            execution, mode = loaded.tensors, INT8_EXECUTION_NONE
         self._execution_tensors[id(loaded)] = execution
         int8_modes.add(mode)
      int8_modes.discard(INT8_EXECUTION_NONE)
      #: How this port executes row-wise int8 weights (one mode per port).
      self.int8_execution = (
         INT8_EXECUTION_PER_CALL_MATERIALIZATION
         if INT8_EXECUTION_PER_CALL_MATERIALIZATION in int8_modes
         else (int8_modes.pop() if int8_modes else INT8_EXECUTION_NONE)
      )
      if self.int8_execution == INT8_EXECUTION_PER_CALL_MATERIALIZATION:
         for _stage, _placement, loaded, _runtime in bound.values():
            self._execution_tensors[id(loaded)] = loaded.tensors
      self.standby_placement_ids = frozenset(bound) if standby_port else frozenset()
      if clock is not None and not callable(clock):
         _reject("invalid_runtime_clock")
      self._clock = clock or time.monotonic
      self._cancelled_paths: OrderedDict[str, None] = OrderedDict()
      self._deferred_cancellation_cleanup: set[str] = set()
      self._kv_states: dict[str, _KVState] = {}
      self._released_paths: OrderedDict[str, str] = OrderedDict()
      self._replays: OrderedDict[tuple[str, str], _ReplayResult] = OrderedDict()
      self._release_counts: Counter[str] = Counter()
      self._peak_kv_bytes = 0
      self._last_release_reason: str | None = None
      self._applied_operation_count = 0
      self._placement_counters = {
         placement_id: {"prefill_operation_count": 0, "decode_operation_count": 0}
         for placement_id in self._bound
      }
      self._prefill_operation_count = 0
      self._prefill_input_token_count = 0
      self._decode_operation_count = 0
      self._decode_input_token_count = 0
      self._activation_output_bytes = 0
      self._maximum_observed_work_unit_ms = 0.0
      self._observed_work_unit_count = 0
      self._closed = False
      self._state_lock = RLock()
      # A10 live publication: bounded, privacy-reduced stage-compute intervals of
      # the operations this stage actually executed (physical monotonic clock).
      from mycelium_router.a10_live_publication import StageComputePublication

      self._a10_publication = StageComputePublication(node_id=node_id, graph=graph)
      self._a10_publication_failures = 0
      self._cancellation_lock = RLock()
      self._cancellation_condition = Condition(self._cancellation_lock)
      self._cancellation_pending = 0
      self._execution_admission_lock = RLock()
      # Exact-path cleanup proof must not queue behind unrelated model work.
      self._resource_index_lock = RLock()
      self._executing_subjects: dict[str, tuple[str, int]] = {}
      self._kv_subjects: dict[str, tuple[str, int]] = {}
      # A10: privately offered candidate members, and the observations this stage
      # runtime published for the groups it actually dispatched.
      self._offered_members: dict[str, _OfferedMember] = {}
      self._offered_lock = RLock()
      self._observations: list[RuntimeBatchObservation] = []

   @staticmethod
   def _validate_graph_stage_roles(graph: ExecutionGraph) -> None:
      if graph.stages[0].layer_range.start_layer != 0:
         _reject("model_layer_range_mismatch")
      for index, stage in enumerate(graph.stages):
         roles = stage.component_roles
         if (
            not isinstance(roles, tuple)
            or not roles
            or len(roles) != len(set(roles))
            or set(roles) - _SUPPORTED_COMPONENTS
            or "decoder" not in roles
         ):
            _reject("invalid_stage_component_roles", stage.stage_id)
         is_entry = index == 0
         is_final = index == len(graph.stages) - 1
         if ("input_embedding" in roles) != is_entry:
            _reject("entry_component_role_mismatch", stage.stage_id)
         if ("final_norm" in roles) != is_final or ("lm_head" in roles) != is_final:
            _reject("final_component_role_mismatch", stage.stage_id)

   @staticmethod
   def _validate_loaded_binding(
      graph: ExecutionGraph,
      stage: Stage,
      placement: Any,
      loaded: LoadedStage,
   ) -> dict[str, Any]:
      proof = loaded.proof
      if not isinstance(proof, Mapping):
         _reject("invalid_load_proof", placement.placement_id)
      plain_proof = _plain_json(proof)
      proof_fields = (
         frozenset(plain_proof) if isinstance(plain_proof, dict) else frozenset()
      )
      if proof_fields not in {
         _BASE_LOAD_PROOF_FIELDS,
         _BASE_LOAD_PROOF_FIELDS | _STAGE_PACK_PROOF_FIELDS,
      }:
         _reject("invalid_load_proof_fields", placement.placement_id)
      if proof_fields & _STAGE_PACK_PROOF_FIELDS:
         for field in _STAGE_PACK_PROOF_FIELDS:
            if not _SHA256_REF_RE.fullmatch(str(proof.get(field, ""))):
               _reject(f"invalid_{field}", placement.placement_id)
      if proof.get("protocol") != "mycelium.layer_load_proof.v1":
         _reject("unsupported_load_proof_protocol", placement.placement_id)

      expected_identity = {
         "deployment_id": graph.deployment_id,
         "deployment_epoch": graph.deployment_epoch,
         "assignment_id": placement.assignment_id,
         "node_id": placement.node_id,
         "model_id": graph.model_id,
         "manifest_digest": graph.manifest_digest,
         "resolved_commit": graph.resolved_commit,
      }
      for field, expected in expected_identity.items():
         if proof.get(field) != expected:
            _reject(f"load_proof_{field}_mismatch", placement.placement_id)
      if _plain_json(proof.get("loaded_range")) != _range_document(stage):
         _reject("load_proof_stage_range_mismatch", placement.placement_id)
      if _plain_json(proof.get("loaded_components")) != list(stage.component_roles):
         _reject("load_proof_component_roles_mismatch", placement.placement_id)
      if proof.get("route_ready") is not False:
         _reject("invalid_load_proof_claim_boundary", placement.placement_id)
      if not isinstance(proof.get("claim_boundary"), str) or not proof[
         "claim_boundary"
      ].strip():
         _reject("invalid_load_proof_claim_boundary", placement.placement_id)
      if (
         not isinstance(proof.get("load_generation"), int)
         or isinstance(proof.get("load_generation"), bool)
         or proof["load_generation"] < 0
      ):
         _reject("invalid_load_generation", placement.placement_id)
      for field in ("loaded_tensor_digest", "probe_digest"):
         if not _SHA256_REF_RE.fullmatch(str(proof.get(field, ""))):
            _reject(f"invalid_{field}", placement.placement_id)

      tensor_keys = proof.get("loaded_tensor_keys")
      if not isinstance(tensor_keys, (list, tuple)):
         _reject("invalid_loaded_tensor_keys", placement.placement_id)
      tensor_keys_list = list(tensor_keys)
      if (
         not all(isinstance(key, str) and key for key in tensor_keys_list)
         or tensor_keys_list != sorted(tensor_keys_list)
         or len(tensor_keys_list) != len(set(tensor_keys_list))
         or tensor_keys_list != sorted(loaded.tensors)
      ):
         _reject("loaded_tensor_keys_mismatch", placement.placement_id)
      if _plain_json(proof.get("resolved_component_aliases")) != _plain_json(
         loaded.resolved_aliases
      ):
         _reject("resolved_component_aliases_mismatch", placement.placement_id)

      try:
         runtime = validate_normalized_mlx_runtime(plain_proof.get("runtime"))
      except (TypeError, ValueError) as exc:
         raise MLXRuntimeError("invalid_loaded_stage_runtime", placement.placement_id) from exc
      config = runtime["model_config"]
      if config["n_embd"] != graph.hidden_size:
         _reject("model_hidden_size_mismatch", placement.placement_id)
      if config["n_layer"] != graph.stages[-1].layer_range.end_layer_exclusive:
         _reject("model_layer_range_mismatch", placement.placement_id)
      if _DTYPE_BYTES[runtime["dtype"]] != graph.activation_bytes:
         _reject("model_activation_bytes_mismatch", placement.placement_id)
      if runtime["backend"] != placement.runtime_backend:
         _reject("runtime_backend_mismatch", placement.placement_id)

      identity = proof.get("runtime_identity")
      if not isinstance(identity, Mapping) or set(identity) != {
         "backend",
         "backend_version",
         "device",
         "dtype",
         "quantization",
         "architecture",
      }:
         _reject("invalid_runtime_identity", placement.placement_id)
      for field in ("backend", "dtype", "quantization", "architecture"):
         if identity.get(field) != runtime[field]:
            _reject(f"runtime_identity_{field}_mismatch", placement.placement_id)
      if not isinstance(identity.get("backend_version"), str) or not identity[
         "backend_version"
      ]:
         _reject("invalid_runtime_identity", placement.placement_id)
      if not isinstance(identity.get("device"), str) or not identity["device"]:
         _reject("invalid_runtime_identity", placement.placement_id)

      probe_shape = proof.get("probe_shape")
      if (
         not isinstance(probe_shape, (list, tuple))
         or not probe_shape
         or any(
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
            for dimension in probe_shape
         )
         or tuple(probe_shape) != tuple(int(value) for value in loaded.probe_output.shape)
      ):
         _reject("probe_shape_mismatch", placement.placement_id)
      if not bool(mx.all(mx.isfinite(loaded.probe_output)).item()):
         _reject("nonfinite_probe_output", placement.placement_id)

      try:
         digest = layer_load_proof_digest(plain_proof)
      except (TypeError, ValueError) as exc:
         raise MLXRuntimeError("invalid_load_proof", placement.placement_id) from exc
      if digest != placement.load_proof_digest:
         _reject("load_proof_digest_mismatch", placement.placement_id)
      if placement.stage_signature != _stage_signature(graph, stage, proof):
         _reject("stage_signature_mismatch", placement.placement_id)
      return runtime

   def _remember_path_marker(
      self,
      markers: OrderedDict[str, Any],
      path_id: str,
      value: Any,
   ) -> None:
      markers[path_id] = value
      markers.move_to_end(path_id)
      while len(markers) > _MAX_RETAINED_OPERATIONS:
         markers.popitem(last=False)

   def _purge_path_replays(self, path_id: str) -> None:
      for key in tuple(self._replays):
         if key[0] == path_id:
            self._replays.pop(key, None)

   def cancel(self, path_id: str) -> None:
      """Idempotently reject future work and release active KV for ``path_id``."""

      if not isinstance(path_id, str):
         return
      with self._cancellation_condition:
         self._remember_path_marker(self._cancelled_paths, path_id, None)
         self._deferred_cancellation_cleanup.add(path_id)
         self._cancellation_pending += 1
      cleanup_deferred = True
      try:
         # Never wait behind a whole stage operation. New execute calls see the
         # cancellation marker before acquiring state; if one operation already
         # owns state, its per-layer checkpoint exits and its finally block
         # performs the exact cleanup below.
         admission_acquired = self._execution_admission_lock.acquire(
            blocking=False
         )
         if admission_acquired:
            try:
               cleanup_now = self._state_lock.acquire(blocking=False)
               if cleanup_now:
                  try:
                     self._release_state(path_id, "cancellation")
                     self._purge_path_replays(path_id)
                     with self._cancellation_condition:
                        self._deferred_cancellation_cleanup.discard(path_id)
                     cleanup_deferred = False
                  finally:
                     self._state_lock.release()
            finally:
               self._execution_admission_lock.release()
      finally:
         with self._cancellation_condition:
            self._cancellation_pending -= 1
            self._cancellation_condition.notify_all()
      if cleanup_deferred:
         self._schedule_deferred_cancellation_cleanup(path_id)

   def _schedule_deferred_cancellation_cleanup(self, path_id: str) -> None:
      """Finish fenced cleanup when a transient observer owned stage state."""

      with self._cancellation_condition:
         cleanup_workers = getattr(self, "_deferred_cleanup_worker_paths", None)
         if cleanup_workers is None:
            cleanup_workers = set()
            self._deferred_cleanup_worker_paths = cleanup_workers
         if path_id in cleanup_workers:
            return
         cleanup_workers.add(path_id)

      def finish() -> None:
         try:
            with self._execution_admission_lock:
               with self._state_lock:
                  with self._cancellation_condition:
                     if path_id in self._deferred_cancellation_cleanup:
                        self._release_state(path_id, "cancellation")
                        self._purge_path_replays(path_id)
                        self._deferred_cancellation_cleanup.discard(path_id)
         finally:
            with self._cancellation_condition:
               self._deferred_cleanup_worker_paths.discard(path_id)
               self._cancellation_condition.notify_all()

      worker = Thread(
         target=finish,
         name=f"mycelium-mlx-cancel-cleanup-{path_id[:12]}",
         daemon=True,
      )
      try:
         worker.start()
      except BaseException:
         with self._cancellation_condition:
            self._deferred_cleanup_worker_paths.discard(path_id)
         raise

   def _acquire_execution_state(self, item: HopWorkItem) -> None:
      while True:
         with self._execution_admission_lock:
            with self._cancellation_condition:
               if item.path_id in self._cancelled_paths:
                  _reject("path_cancelled")
               cancellation_pending = (
                  self._cancellation_pending > 0
                  or bool(self._deferred_cancellation_cleanup)
               )
            if not cancellation_pending:
               # Never wait for the active stage while holding the
               # cancellation mutex. Active MLX work checkpoints under that
               # mutex, so the inverse order deadlocks the entire stage.
               self._state_lock.acquire()
               with self._cancellation_condition:
                  cancelled = item.path_id in self._cancelled_paths
                  cancellation_pending = (
                     self._cancellation_pending > 0
                     or bool(self._deferred_cancellation_cleanup)
                  )
                  if not cancelled and not cancellation_pending:
                     with self._resource_index_lock:
                        self._executing_subjects[item.path_id] = (
                           item.request_id,
                           item.path_attempt,
                        )
               if cancelled:
                  self._state_lock.release()
                  _reject("path_cancelled")
               if not cancellation_pending:
                  return
               # A cancellation can arrive after this execution acquired
               # admission but while it was queued for stage state. Yield both
               # locks so exact cleanup wins the handoff before unrelated work.
               self._state_lock.release()
         with self._cancellation_condition:
            while (
               (
                  self._cancellation_pending > 0
                  or bool(self._deferred_cancellation_cleanup)
               )
               and item.path_id not in self._cancelled_paths
            ):
               self._cancellation_condition.wait()

   def _checkpoint_path(self, path_id: str) -> None:
      with self._cancellation_lock:
         if path_id in self._cancelled_paths:
            _reject("path_cancelled")

   @staticmethod
   def _failure(reason: str) -> RuntimeResult:
      return RuntimeResult(
         success=False,
         failure_scope="PLACEMENT",
         failure_reason=reason,
      )

   @staticmethod
   def _operation_fingerprint(item: HopWorkItem) -> str:
      if not isinstance(item.payload, bytes):
         _reject("runtime_payload_must_be_bytes")
      key = item.batch_key
      key_material = None
      if isinstance(key, RuntimeBatchKey):
         key_material = {
            field: getattr(key, field)
            for field in RuntimeBatchKey.__dataclass_fields__
         }
      material = {
         "request_id": item.request_id,
         "path_id": item.path_id,
         "path_attempt": item.path_attempt,
         "phase": item.phase,
         "token_index": item.token_index,
         "position": item.position,
         "hop_index": item.hop_index,
         "placement_id": item.placement_id,
         "idempotency_key": item.idempotency_key,
         "terminal": item.terminal,
         "lease_expires_at": repr(item.lease_expires_at),
         "batch_key": key_material,
         "payload_digest": hashlib.sha256(item.payload).hexdigest(),
      }
      encoded = json.dumps(
         material,
         sort_keys=True,
         separators=(",", ":"),
         ensure_ascii=False,
         allow_nan=False,
      ).encode("utf-8")
      return hashlib.sha256(encoded).hexdigest()

   def _remember_result(
      self,
      item: HopWorkItem,
      fingerprint: str,
      result: RuntimeResult,
   ) -> None:
      self._replays[(item.path_id, item.idempotency_key)] = _ReplayResult(
         fingerprint=fingerprint,
         result=result,
      )
      self._replays.move_to_end((item.path_id, item.idempotency_key))
      while len(self._replays) > _MAX_RETAINED_OPERATIONS:
         self._replays.popitem(last=False)

   def _release_state(self, path_id: str, reason: str) -> bool:
      state = self._kv_states.pop(path_id, None)
      if state is None:
         return False
      with self._resource_index_lock:
         self._kv_subjects.pop(path_id, None)
      if path_id not in self._released_paths:
         self._remember_path_marker(self._released_paths, path_id, reason)
      self._release_counts[reason] += 1
      self._last_release_reason = reason
      state.layers.clear()
      self._purge_path_replays(path_id)
      return True

   def expire_leases(self, *, now: float | None = None) -> tuple[str, ...]:
      """Release every active path whose bound reservation lease has expired."""

      current = self._clock() if now is None else now
      if (
         not isinstance(current, (int, float))
         or isinstance(current, bool)
         or not math.isfinite(float(current))
      ):
         raise MLXRuntimeError("invalid_lease_expiry_time")
      expired: list[str] = []
      with self._state_lock:
         for path_id, state in tuple(self._kv_states.items()):
            if float(current) >= state.lease_expires_at:
               if self._release_state(path_id, "lease_expired"):
                  expired.append(path_id)
      return tuple(sorted(expired))

   def close(self, *, reason: str = "worker_shutdown") -> None:
      """Release all stage state before worker/process teardown."""

      if not isinstance(reason, str) or not reason:
         raise MLXRuntimeError("invalid_runtime_close_reason")
      with self._state_lock:
         if self._closed:
            return
         for path_id in tuple(self._kv_states):
            self._release_state(path_id, reason)
         self._replays.clear()
         self._closed = True

   @staticmethod
   def _kv_bytes(state: _KVState) -> int:
      return sum(
         int(array.nbytes)
         for pair in state.layers.values()
         for array in pair
      )

   def _update_kv_watermark(self) -> None:
      active_bytes = sum(self._kv_bytes(state) for state in self._kv_states.values())
      self._peak_kv_bytes = max(self._peak_kv_bytes, active_bytes)

   def export_kv_checkpoint(self, path_id: str, *, from_position: int = 0) -> bytes:
      """Export the exact stage-local KV held for ``path_id`` (A7 successor recovery).

      The payload is byte exact and self-describing; it carries no confidentiality.
      """

      from mycelium_router.kv_checkpoint import export_port_kv_checkpoint

      return export_port_kv_checkpoint(
         self,
         path_id,
         to_numpy=lambda array: np.array(array, copy=True),
         from_position=from_position,
      )

   def import_kv_checkpoint(
      self,
      payload: bytes,
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      placement_id: str,
      lease_expires_at: float,
      expected_next_position: int,
      max_bytes: int,
   ) -> dict[str, Any]:
      """Install an exactly compatible checkpoint as a successor path's KV.

      The successor then continues with DECODE at ``expected_next_position``;
      no recovery prefill is needed.  Any incompatibility refuses with
      ``KVCheckpointError`` and changes nothing.
      """

      from mycelium_router.kv_checkpoint import import_port_kv_checkpoint

      return import_port_kv_checkpoint(
         self,
         payload,
         request_id=request_id,
         path_id=path_id,
         path_attempt=path_attempt,
         placement_id=placement_id,
         lease_expires_at=lease_expires_at,
         expected_next_position=expected_next_position,
         max_bytes=max_bytes,
         from_numpy=mx.array,
         state_factory=_KVState,
      )

   def rekey_kv_path(
      self,
      old_path_id: str,
      *,
      new_path_id: str,
      new_path_attempt: int,
      lease_expires_at: float,
      truncate_to_position: int | None = None,
   ) -> dict[str, Any]:
      """Move this stage's KV to a fenced successor path (A7 cutover)."""

      from mycelium_router.kv_checkpoint import rekey_port_kv_path

      return rekey_port_kv_path(
         self,
         old_path_id,
         new_path_id=new_path_id,
         new_path_attempt=new_path_attempt,
         lease_expires_at=lease_expires_at,
         truncate_to_position=truncate_to_position,
      )

   def kv_snapshot(self) -> dict[str, Any]:
      """Return identity/lifecycle evidence without exposing KV tensor values."""

      self.expire_leases()
      with self._state_lock:
         states = {
            path_id: {
               "request_id": state.request_id,
               "path_attempt": state.path_attempt,
               "placement_id": state.placement_id,
               "assignment_id": state.assignment_id,
               "manifest_digest": state.manifest_digest,
               "deployment_epoch": state.deployment_epoch,
               "lease_expires_at": state.lease_expires_at,
               "next_position": state.next_position,
               "next_sequence": state.next_sequence,
               "cached_context_tokens": state.cached_context_tokens,
               "layer_count": len(state.layers),
               "kv_bytes": self._kv_bytes(state),
            }
            for path_id, state in sorted(self._kv_states.items())
         }
         return {
            "mode": self.decode_mode,
            "backend": "mlx",
            "architecture": self._architecture,
            "closed": self._closed,
            "active_state_count": len(states),
            "active_kv_bytes": sum(state["kv_bytes"] for state in states.values()),
            "peak_kv_bytes": self._peak_kv_bytes,
            "current_position": max(
               (state["next_position"] for state in states.values()), default=None
            ),
            "release_state": (
               "closed"
               if self._closed
               else "active"
               if states
               else "released"
               if self._last_release_reason is not None
               else "idle"
            ),
            "last_release_reason": self._last_release_reason,
            "states": states,
            "retained_result_count": len(self._replays),
            "applied_operation_count": self._applied_operation_count,
            "placement_counters": {
               placement_id: {
                  **counts,
                  "active_state_count": sum(
                     state["placement_id"] == placement_id for state in states.values()
                  ),
                  "active_kv_bytes": sum(
                     state["kv_bytes"] for state in states.values()
                     if state["placement_id"] == placement_id
                  ),
               }
               for placement_id, counts in sorted(self._placement_counters.items())
            },
            "prefill_operation_count": self._prefill_operation_count,
            "prefill_input_token_count": self._prefill_input_token_count,
            "decode_operation_count": self._decode_operation_count,
            "decode_input_token_count": self._decode_input_token_count,
            "activation_output_bytes": self._activation_output_bytes,
            "maximum_observed_work_unit_ms": self._maximum_observed_work_unit_ms,
            "observed_work_unit_count": self._observed_work_unit_count,
            "release_counts": dict(sorted(self._release_counts.items())),
         }

   def kv_snapshot_nonblocking(self) -> dict[str, Any] | None:
      """Return a snapshot only when no stage operation owns runtime state."""

      if not self._state_lock.acquire(blocking=False):
         return None
      try:
         return self.kv_snapshot()
      finally:
         self._state_lock.release()

   def _a10_record(
      self,
      item: HopWorkItem,
      started_at: float,
      finished_at: float,
      result: RuntimeResult,
      *,
      runtime_batch_ref: str = "",
   ) -> None:
      """Record one executed stage operation; publication never fails the work."""

      try:
         self._a10_publication.record(
            item,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            runtime_batch_ref=runtime_batch_ref,
         )
      except Exception:  # noqa: BLE001 - evidence loss is counted, never fatal
         self._a10_publication_failures += 1

   def a10_publication_snapshot(self) -> dict[str, Any]:
      """The bounded A10 live publication: stage-compute intervals and batch records.

      Runtime-owned, privacy-reduced and closed
      (``mycelium_router.a10_live_publication.PUBLICATION_PROTOCOL``); the node
      co-signs it inside its ordinary snapshot.
      """

      observations = getattr(self, "batch_observations", None)
      return self._a10_publication.snapshot(
         batch_observations=observations() if callable(observations) else ()
      )

   def operation_counter_snapshot(self) -> dict[str, int]:
      """Return lock-free monotonic work telemetry for active health probes."""

      return {"applied_operation_count": self._applied_operation_count}

   def a10_verifier_stages(self) -> tuple[Any, ...]:
      """This port's local target stages as A10 verifier stages, in graph order.

      Each stage runs this port's own stage kernel under its stage-state lock; the
      final stage returns the greedy target decision for every input position.
      """

      from mycelium_router.a10_runtime_verifier import (
         PortVerifierStage,
         local_verifier_stages,
      )

      return local_verifier_stages(
         self,
         lambda placement_id: PortVerifierStage(
            self,
            placement_id,
            token_array=lambda tokens: mx.array((tokens,), dtype=mx.uint32),
            logits_row=lambda output, index: output[0, index, :].tolist(),
         ),
      )

   def begin_target_verifier(
      self,
      prefix_tokens: Any,
      *,
      verification_ref: str,
      stages: Any = None,
   ) -> Any:
      """Open one request-local A10 target verifier over this port's target stages.

      The returned object is a real ``TargetVerificationRuntimePort``
      (``begin_verification`` / ``sequential_reference``).  ``stages`` may extend
      the chain with another local port's stages for a split in-process target.
      """

      from mycelium_router.a10_runtime_verifier import RuntimeTargetVerifier

      return RuntimeTargetVerifier(
         self.a10_verifier_stages() if stages is None else stages,
         prefix_tokens=prefix_tokens,
         verification_ref=verification_ref,
      )

   def kv_subject_clean(
      self,
      request_id: str,
      path_id: str,
      path_attempt: int,
   ) -> bool:
      """Prove exact-path absence without waiting on unrelated model work."""

      if (
         not isinstance(request_id, str)
         or not request_id
         or not isinstance(path_id, str)
         or not path_id
         or type(path_attempt) is not int
         or path_attempt < 0
      ):
         return False
      with self._resource_index_lock:
         return (
            path_id not in self._executing_subjects
            and path_id not in self._kv_subjects
         )

   def execute(self, item: HopWorkItem) -> RuntimeResult:
      """Execute one fail-closed, path-serialized stage-local KV operation."""

      if not isinstance(item, HopWorkItem):
         return self._failure("invalid_runtime_work_item")
      try:
         self._acquire_execution_state(item)
      except MLXRuntimeError as exc:
         return self._failure(exc.code)
      try:
         if self._closed:
            return self._failure("runtime_closed")
         try:
            self._checkpoint_path(item.path_id)
         except MLXRuntimeError as exc:
            return self._failure(exc.code)
         try:
            self.expire_leases()
            started_at = time.monotonic()
            result = self._execute_bound(item)
            self._a10_record(item, started_at, time.monotonic(), result)
            return result
         except PayloadError as exc:
            return self._failure(exc.code)
         except MLXRuntimeError as exc:
            return self._failure(exc.code)
         except RuntimeExecutionError as exc:
            return self._failure(str(exc) or "runtime_execution_rejected")
         except Exception:
            return self._failure("runtime_execution_rejected")
      finally:
         # Drain cancellation-fenced KV owned by other, inactive paths while
         # this operation still owns the one stage-state lock.  Keep the
         # cancellation lock until state unlock so a newly arriving cancel
         # cannot fall between the final sweep and its own nonblocking cleanup.
         with self._cancellation_condition:
            for cancelled_path in tuple(
               self._deferred_cancellation_cleanup
            ):
               self._release_state(cancelled_path, "cancellation")
               self._purge_path_replays(cancelled_path)
               self._deferred_cancellation_cleanup.discard(cancelled_path)
            with self._resource_index_lock:
               self._executing_subjects.pop(item.path_id, None)
            self._cancellation_condition.notify_all()
            self._state_lock.release()

   def _decode_input(
      self,
      item: HopWorkItem,
      stage: Stage,
      runtime: Mapping[str, Any],
   ) -> tuple[mx.array | None, mx.array | None, int]:
      if not isinstance(item.payload, bytes):
         _reject("runtime_payload_must_be_bytes")
      if stage.stage_id == self.graph.entry_stage_id:
         token_ids = decode_token_ids(item.payload)
         if not token_ids:
            _reject("empty_token_sequence")
         config = runtime["model_config"]
         if (
            len(token_ids) > config["n_positions"]
            if self.decode_mode == "complete_context_replay"
            else item.position + len(token_ids) > config["n_positions"]
         ):
            _reject("position_bounds_exceeded")
         if any(token_id >= config["vocab_size"] for token_id in token_ids):
            _reject("token_bounds_exceeded")
         self._validate_sequence_span(item.batch_key, item.phase, len(token_ids))
         return mx.array((token_ids,), dtype=mx.uint32), None, len(token_ids)

      envelope = decode_activation(item.payload)
      if envelope.dtype != runtime["dtype"]:
         _reject("activation_dtype_mismatch")
      if len(envelope.shape) != 3:
         _reject("activation_rank_mismatch")
      if envelope.shape[0] != 1 or envelope.shape[2] != self.graph.hidden_size:
         _reject("activation_shape_mismatch")
      sequence = envelope.shape[1]
      if (
         sequence > runtime["model_config"]["n_positions"]
         if self.decode_mode == "complete_context_replay"
         else item.position + sequence > runtime["model_config"]["n_positions"]
      ):
         _reject("position_bounds_exceeded")
      self._validate_sequence_span(item.batch_key, item.phase, sequence)
      if sys.byteorder != "little":
         _reject("unsupported_host_byte_order")
      raw = mx.array(memoryview(envelope.data), dtype=mx.uint8)
      hidden = raw.view(_MLX_DTYPES[envelope.dtype]).reshape(envelope.shape)
      mx.eval(hidden)
      if not bool(mx.all(mx.isfinite(hidden)).item()):
         _reject("nonfinite_activation")
      return None, hidden, sequence

   def _execute_stage_with_kv(
      self,
      *,
      stage: Stage,
      loaded: LoadedStage,
      runtime: Mapping[str, Any],
      token_ids: mx.array | None,
      hidden_states: mx.array | None,
      position: int | tuple[int, ...],
      past_layers: Mapping[int, Any],
      path_id: str,
      produce_logits: bool = True,
      all_positions: bool = False,
   ) -> tuple[mx.array, dict[int, Any]]:
      config = runtime["model_config"]
      tensors = self._tensors_for(loaded)
      start = stage.layer_range.start_layer
      end = stage.layer_range.end_layer_exclusive
      if runtime["architecture"] in {"qwen2", "qwen3"}:
         if "input_embedding" in stage.component_roles:
            if token_ids is None or hidden_states is not None:
               _reject("entry_stage_requires_token_ids")
            hidden = _qwen2_embedding(
               tensors["model.embed_tokens.weight"], token_ids
            )
         else:
            if hidden_states is None or token_ids is not None:
               _reject("non_entry_stage_requires_hidden_states")
            hidden = hidden_states
         next_layers: dict[int, Any] = {}
         for layer in range(start, end):
            self._checkpoint_path(path_id)
            work_unit_started = time.monotonic()
            hidden, layer_kv = _qwen2_block_with_kv(
               hidden,
               tensors,
               f"model.layers.{layer}.",
               config,
               position,
               past_layers.get(layer),
               runtime["architecture"],
            )
            next_layers[layer] = layer_kv
            mx.eval(hidden, *layer_kv)
            self._observe_cooperative_work_unit(work_unit_started)
            self._checkpoint_path(path_id)
         # KV state covers the complete chunk; only its final position is
         # required for the next autoregressive vocabulary projection.
         if (
            produce_logits
            and not all_positions
            and "lm_head" in stage.component_roles
            and int(hidden.shape[1]) > 1
         ):
            hidden = hidden[:, -1:, :]
         if produce_logits and "final_norm" in stage.component_roles:
            self._checkpoint_path(path_id)
            work_unit_started = time.monotonic()
            hidden = _rms_norm(
               hidden,
               tensors["model.norm.weight"],
               float(config["rms_norm_epsilon"]),
            )
            mx.eval(hidden)
            self._observe_cooperative_work_unit(work_unit_started)
         if produce_logits and "lm_head" in stage.component_roles:
            self._checkpoint_path(path_id)
            work_unit_started = time.monotonic()
            aliases = loaded.resolved_aliases
            if not isinstance(aliases, Mapping):
               _reject("invalid_loaded_stage_aliases")
            alias = aliases.get("lm_head", {})
            if not isinstance(alias, Mapping):
               _reject("invalid_loaded_stage_aliases")
            head_keys = alias.get("tensor_keys", ["lm_head.weight"])
            if (
               not isinstance(head_keys, (list, tuple))
               or len(head_keys) != 1
               or not isinstance(head_keys[0], str)
            ):
               _reject("invalid_loaded_stage_aliases")
            hidden = _qwen2_linear(hidden, tensors[head_keys[0]])
            mx.eval(hidden)
            self._observe_cooperative_work_unit(work_unit_started)
            self._checkpoint_path(path_id)
         mx.eval(
            hidden,
            *(array for pair in next_layers.values() for array in pair),
         )
         if not bool(mx.all(mx.isfinite(hidden)).item()):
            _reject("nonfinite_stage_output")
         return hidden, next_layers

      transformer_key = f"transformer.h.{start}.ln_1.weight"
      plain_key = f"h.{start}.ln_1.weight"
      if transformer_key in tensors and plain_key not in tensors:
         namespace = "transformer."
      elif plain_key in tensors and transformer_key not in tensors:
         namespace = ""
      else:
         _reject("invalid_loaded_stage_namespace")

      if "input_embedding" in stage.component_roles:
         if token_ids is None or hidden_states is not None:
            _reject("entry_stage_requires_token_ids")
         sequence = int(token_ids.shape[1])
         if isinstance(position, tuple):
            # Ragged decode group: each member is embedded at its own position.
            if len(position) != int(token_ids.shape[0]):
               _reject("ragged_batch_member_mismatch")
            rows = [
               tensors[f"{namespace}wte.weight"][token_ids[index : index + 1]]
               + tensors[f"{namespace}wpe.weight"][
                  mx.arange(
                     int(member_position),
                     int(member_position) + sequence,
                     dtype=mx.int32,
                  )
               ]
               for index, member_position in enumerate(position)
            ]
            hidden = mx.concatenate(rows, axis=0)
         else:
            positions = mx.arange(position, position + sequence, dtype=mx.int32)
            hidden = tensors[f"{namespace}wte.weight"][token_ids] + tensors[
               f"{namespace}wpe.weight"
            ][positions]
      else:
         if hidden_states is None or token_ids is not None:
            _reject("non_entry_stage_requires_hidden_states")
         hidden = hidden_states

      epsilon = float(config["layer_norm_epsilon"])
      next_layers: dict[int, Any] = {}
      for layer in range(start, end):
         self._checkpoint_path(path_id)
         work_unit_started = time.monotonic()
         hidden, layer_kv = _gpt2_block_with_kv(
            hidden,
            tensors,
            f"{namespace}h.{layer}.",
            config["n_head"],
            epsilon,
            past_layers.get(layer),
         )
         next_layers[layer] = layer_kv
         mx.eval(hidden, *layer_kv)
         self._observe_cooperative_work_unit(work_unit_started)
         self._checkpoint_path(path_id)
      if "final_norm" in stage.component_roles:
         self._checkpoint_path(path_id)
         work_unit_started = time.monotonic()
         hidden = _layer_norm(
            hidden,
            tensors[f"{namespace}ln_f.weight"],
            tensors[f"{namespace}ln_f.bias"],
            epsilon,
         )
         mx.eval(hidden)
         self._observe_cooperative_work_unit(work_unit_started)
      if "lm_head" in stage.component_roles:
         self._checkpoint_path(path_id)
         work_unit_started = time.monotonic()
         aliases = loaded.resolved_aliases
         if not isinstance(aliases, Mapping):
            _reject("invalid_loaded_stage_aliases")
         alias = aliases.get("lm_head", {})
         if not isinstance(alias, Mapping):
            _reject("invalid_loaded_stage_aliases")
         head_keys = alias.get("tensor_keys", ["lm_head.weight"])
         if (
            not isinstance(head_keys, (list, tuple))
            or len(head_keys) != 1
            or not isinstance(head_keys[0], str)
         ):
            _reject("invalid_loaded_stage_aliases")
         hidden = mx.matmul(hidden, tensors[head_keys[0]].transpose(1, 0))
         mx.eval(hidden)
         self._observe_cooperative_work_unit(work_unit_started)
         self._checkpoint_path(path_id)
      mx.eval(hidden, *(array for pair in next_layers.values() for array in pair))
      if not bool(mx.all(mx.isfinite(hidden)).item()):
         _reject("nonfinite_stage_output")
      return hidden, next_layers

   def _tensors_for(self, loaded: LoadedStage) -> Mapping[str, Any]:
      """The execution tensors of a bound stage (packed int8 when available)."""

      execution = self._execution_tensors.get(id(loaded))
      return loaded.tensors if execution is None else execution

   def _observe_cooperative_work_unit(self, started_at: float) -> None:
      elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1_000.0)
      self._observed_work_unit_count += 1
      self._maximum_observed_work_unit_ms = max(
         self._maximum_observed_work_unit_ms,
         elapsed_ms,
      )

   def _runtime_result(
      self,
      stage: Stage,
      runtime: Mapping[str, Any],
      output: mx.array,
      *,
      produce_token: bool = True,
   ) -> RuntimeResult:
      if stage.stage_id == self.graph.final_stage_id:
         if not produce_token:
            return RuntimeResult(success=True)
         expected = (1, int(output.shape[1]), runtime["model_config"]["vocab_size"])
         if tuple(int(value) for value in output.shape) != expected:
            _reject("invalid_final_stage_output")
         return RuntimeResult(
            success=True,
            token_id=quantized_greedy_token_id(output[0, -1, :].tolist()),
         )

      expected = (1, int(output.shape[1]), self.graph.hidden_size)
      if tuple(int(value) for value in output.shape) != expected:
         _reject("invalid_intermediate_stage_output")
      contiguous = mx.contiguous(output)
      mx.eval(contiguous)
      return RuntimeResult(
         success=True,
         payload=encode_activation(
            dtype=runtime["dtype"],
            shape=tuple(int(value) for value in contiguous.shape),
            data=bytes(contiguous),
         ),
      )

   @staticmethod
   def _is_full_context_replay_chunk(item: HopWorkItem) -> bool:
      """True only for a prefill chunk that is the complete prompt.

      Complete-context replay retains no stage-local KV, so it can never
      continue a chunked prefill.  The one chunk it accepts is the first
      chunk (sequence 0, position 0) that is also the final chunk
      (``emits_token``): the entire prompt in one operation, which is exactly
      a replay prefill.  Every other chunk still needs KV continuity.
      """

      return (
         type(item.token_index) is int
         and item.token_index == 0
         and type(item.position) is int
         and item.position == 0
         and item.emits_token is True
      )

   def _execute_complete_context(
      self,
      item: HopWorkItem,
      stage: Stage,
      loaded: LoadedStage,
      runtime: Mapping[str, Any],
      token_ids: mx.array | None,
      hidden_states: mx.array | None,
      sequence: int,
      fingerprint: str,
   ) -> RuntimeResult:
      if item.phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}:
         if item.phase == "PREFILL" and item.token_index != -1:
            _reject("prefill_sequence_mismatch")
         if (
            item.phase == "PREFILL_CHUNK"
            and not self._is_full_context_replay_chunk(item)
         ):
            _reject("prefill_chunk_requires_kv_continuity")
         if item.phase == "RECOVERY_PREFILL" and item.token_index < 0:
            _reject("recovery_prefill_sequence_mismatch")
         if item.position != 0:
            _reject("prefill_position_mismatch")
      else:
         if item.position <= 0:
            _reject("decode_position_mismatch")
         if sequence != item.position + 1:
            _reject("decode_context_position_mismatch")
         if item.token_index < 0:
            _reject("decode_sequence_mismatch")

      try:
         if runtime["architecture"] in {"qwen2", "qwen3"}:
            # Replay from position zero through the checkpointed executor.
            # The temporary KV is intentionally discarded: complete-context
            # mode recomputes on every decode, but it must remain cooperatively
            # interruptible while holding the relay's exact-path operation lock.
            output, _temporary_layers = self._execute_stage_with_kv(
               stage=stage,
               loaded=loaded,
               runtime=runtime,
               token_ids=token_ids,
               hidden_states=hidden_states,
               position=0,
               past_layers={},
               path_id=item.path_id,
            )
         elif "input_embedding" in stage.component_roles:
            output = _execute_loaded_stage(loaded, token_ids=token_ids)
         else:
            output = _execute_loaded_stage(loaded, hidden_states=hidden_states)
      except RuntimeExecutionError as exc:
         raise MLXRuntimeError(str(exc) or "runtime_execution_rejected") from exc
      mx.eval(output)
      if not bool(mx.all(mx.isfinite(output)).item()):
         _reject("nonfinite_stage_output")
      result = self._runtime_result(stage, runtime, output)
      self._applied_operation_count += 1
      phase_counter = (
         "decode_operation_count" if item.phase == "DECODE" else "prefill_operation_count"
      )
      self._placement_counters[item.placement_id][phase_counter] += 1
      self._activation_output_bytes += len(result.payload or b"")
      if item.phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}:
         self._prefill_operation_count += 1
         self._prefill_input_token_count += sequence
      else:
         self._decode_operation_count += 1
         self._decode_input_token_count += sequence
      self._remember_result(item, fingerprint, result)
      return result

   def _execute_bound(self, item: HopWorkItem) -> RuntimeResult:
      if (
         item.phase == "PREFILL_CHUNK"
         and self.decode_mode != "stage_local_kv"
         and not self._is_full_context_replay_chunk(item)
      ):
         _reject("prefill_chunk_requires_kv_continuity")
      if item.phase not in {
         "PREFILL",
         "PREFILL_CHUNK",
         "RECOVERY_PREFILL",
         "DECODE",
      }:
         _reject("unsupported_runtime_phase")
      if not isinstance(item.payload, bytes):
         _reject("runtime_payload_must_be_bytes")
      if not isinstance(item.idempotency_key, str) or not item.idempotency_key:
         _reject("invalid_idempotency_key")
      if (
         not isinstance(item.position, int)
         or isinstance(item.position, bool)
         or item.position < 0
      ):
         _reject("invalid_kv_position")
      if not isinstance(item.terminal, bool):
         _reject("invalid_terminal_marker")
      if (
         not isinstance(item.lease_expires_at, (int, float))
         or isinstance(item.lease_expires_at, bool)
         or not math.isfinite(float(item.lease_expires_at))
      ):
         _reject("invalid_kv_lease")

      binding = self._bound.get(item.placement_id)
      if binding is None:
         _reject("unbound_runtime_placement")
      if item.placement_id in self.standby_placement_ids and item.phase != "DECODE":
         _reject("standby_placement_not_serving")
      stage, placement, loaded, runtime = binding
      self._validate_batch_key(item, placement)
      fingerprint = self._operation_fingerprint(item)
      replay = self._replays.get((item.path_id, item.idempotency_key))
      if replay is not None:
         if replay.fingerprint != fingerprint:
            _reject("kv_sequence_replay_conflict")
         return replay.result

      token_ids, hidden_states, sequence = self._decode_input(item, stage, runtime)
      if self.decode_mode == "complete_context_replay":
         return self._execute_complete_context(
            item,
            stage,
            loaded,
            runtime,
            token_ids,
            hidden_states,
            sequence,
            fingerprint,
         )
      starts_kv = item.phase in {"PREFILL", "RECOVERY_PREFILL"} or (
         item.phase == "PREFILL_CHUNK" and item.token_index == 0
      )
      produce_token = item.phase != "PREFILL_CHUNK" or item.emits_token
      continues_prefill = (
         item.phase == "PREFILL_CHUNK" and item.token_index > 0
      )
      if starts_kv:
         if item.phase == "PREFILL" and item.token_index != -1:
            _reject("kv_prefill_sequence_mismatch")
         if item.phase == "RECOVERY_PREFILL" and item.token_index < 0:
            _reject("kv_recovery_sequence_mismatch")
         if item.phase == "PREFILL_CHUNK" and item.token_index != 0:
            _reject("kv_prefill_chunk_sequence_mismatch")
         if item.position != 0:
            _reject("kv_position_mismatch")
         if item.path_id in self._kv_states:
            _reject("kv_state_already_exists")
         released_reason = self._released_paths.get(item.path_id)
         if released_reason == "lease_expired":
            _reject("kv_lease_expired")
         if released_reason is not None:
            _reject("kv_state_released")
         if float(self._clock()) >= float(item.lease_expires_at):
            self._released_paths.setdefault(item.path_id, "lease_expired")
            _reject("kv_lease_expired")
         output, layers = self._execute_stage_with_kv(
            stage=stage,
            loaded=loaded,
            runtime=runtime,
            token_ids=token_ids,
            hidden_states=hidden_states,
            position=item.position,
            past_layers={},
            path_id=item.path_id,
            produce_logits=produce_token,
         )
         result = self._runtime_result(
            stage,
            runtime,
            output,
            produce_token=produce_token,
         )
         state = _KVState(
            request_id=item.request_id,
            path_id=item.path_id,
            path_attempt=item.path_attempt,
            placement_id=item.placement_id,
            assignment_id=placement.assignment_id,
            manifest_digest=self.graph.manifest_digest,
            deployment_epoch=self.graph.deployment_epoch,
            lease_expires_at=float(item.lease_expires_at),
            next_position=sequence,
            next_sequence=(
               item.token_index + 1
               if item.phase == "RECOVERY_PREFILL"
               else 1
            ),
            cached_context_tokens=sequence,
            layers=layers,
         )
         self._kv_states[item.path_id] = state
         with self._resource_index_lock:
            self._kv_subjects[item.path_id] = (
               item.request_id,
               item.path_attempt,
            )
         self._update_kv_watermark()
      else:
         state = self._kv_states.get(item.path_id)
         if state is None:
            released_reason = self._released_paths.get(item.path_id)
            if released_reason == "lease_expired":
               _reject("kv_lease_expired")
            if released_reason is not None:
               _reject("kv_state_released")
            _reject("kv_state_missing")
         if item.request_id != state.request_id:
            _reject("kv_request_id_mismatch")
         if item.path_attempt != state.path_attempt:
            _reject("kv_path_attempt_mismatch")
         if item.placement_id != state.placement_id:
            _reject("kv_placement_id_mismatch")
         if item.position != state.next_position:
            _reject("kv_position_mismatch")
         if not continues_prefill and item.token_index != state.next_sequence:
            _reject("kv_sequence_mismatch")
         if continues_prefill and item.token_index < 1:
            _reject("kv_prefill_chunk_sequence_mismatch")
         if float(item.lease_expires_at) != state.lease_expires_at:
            _reject("kv_lease_mismatch")
         if float(self._clock()) >= state.lease_expires_at:
            self._release_state(item.path_id, "lease_expired")
            _reject("kv_lease_expired")
         output, layers = self._execute_stage_with_kv(
            stage=stage,
            loaded=loaded,
            runtime=runtime,
            token_ids=token_ids,
            hidden_states=hidden_states,
            position=item.position,
            past_layers=state.layers,
            path_id=item.path_id,
            produce_logits=produce_token,
         )
         result = self._runtime_result(
            stage,
            runtime,
            output,
            produce_token=produce_token,
         )
         state.layers = layers
         state.next_position += sequence
         if not continues_prefill:
            state.next_sequence += 1
         state.cached_context_tokens += sequence
         self._update_kv_watermark()

      self._applied_operation_count += 1
      phase_counter = (
         "decode_operation_count" if item.phase == "DECODE" else "prefill_operation_count"
      )
      self._placement_counters[item.placement_id][phase_counter] += 1
      self._activation_output_bytes += len(result.payload or b"")
      if item.phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}:
         self._prefill_operation_count += 1
         self._prefill_input_token_count += sequence
      else:
         self._decode_operation_count += 1
         self._decode_input_token_count += sequence
      self._remember_result(item, fingerprint, result)
      if item.terminal:
         self._release_state(item.path_id, "normal_completion")
      return result

   def _validate_batch_key(self, item: HopWorkItem, placement: Any) -> None:
      key = item.batch_key
      if not isinstance(key, RuntimeBatchKey):
         _reject("missing_runtime_batch_key")
      expected = {
         "deployment_id": self.graph.deployment_id,
         "deployment_epoch": self.graph.deployment_epoch,
         "model_commit": self.graph.resolved_commit,
         "manifest_digest": self.graph.manifest_digest,
         "placement_id": placement.placement_id,
         "assignment_id": placement.assignment_id,
         "stage_signature": placement.stage_signature,
         "load_proof_digest": placement.load_proof_digest,
         "runtime_backend": placement.runtime_backend,
         "phase": item.phase,
         "hidden_size": self.graph.hidden_size,
         "activation_bytes": self.graph.activation_bytes,
         "speculative_role": "NONE",
         "speculative_width": 0,
      }
      for field, value in expected.items():
         if getattr(key, field) != value:
            _reject(f"batch_key_{field}_mismatch")
      if (
         not isinstance(key.token_span, int)
         or isinstance(key.token_span, bool)
         or key.token_span <= 0
      ):
         _reject("invalid_batch_key_token_span")

   def _validate_sequence_span(
      self,
      key: RuntimeBatchKey | None,
      phase: str,
      sequence: int,
   ) -> None:
      if key is None:
         _reject("missing_runtime_batch_key")
      if (
         phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}
         and key.token_span != sequence
      ):
         _reject("batch_key_token_span_mismatch")
      if phase == "DECODE" and key.token_span != 1:
         _reject("batch_key_token_span_mismatch")
      if (
         phase == "DECODE"
         and self.decode_mode == "stage_local_kv"
         and sequence != 1
      ):
         _reject("decode_requires_single_token")

   # -- A10 runtime batch dispatch ------------------------------------------

   @staticmethod
   def dispatch_mode() -> str:
      """The A10 dispatch mode this port reports for an offered group."""

      return RUNTIME_BATCH_DISPATCH_MODE

   @staticmethod
   def _item_starts_kv(item: HopWorkItem) -> bool:
      """True for a member that begins its own stage-local KV (no continuation)."""

      return item.phase in {"PREFILL", "RECOVERY_PREFILL"} or (
         item.phase == "PREFILL_CHUNK" and item.token_index == 0
      )

   @staticmethod
   def _member_outcome(result: RuntimeResult) -> str:
      if result.success:
         return "completed"
      if (
         result.failure_scope in {"member_cancelled", "cancelled"}
         or result.failure_reason.startswith("cancelled")
      ):
         return "cancelled"
      return "failed"

   @staticmethod
   def _kv_identity(item: HopWorkItem) -> str:
      """This member's own KV identity -- never a peer's."""

      return f"kv:{item.path_id}:{item.path_attempt}:{item.placement_id}"

   @staticmethod
   def _incarnation_for(placement: Any, loaded: LoadedStage) -> str:
      """A deterministic identity for the exact bound stage runtime."""

      generation = loaded.proof.get("load_generation", 0)
      return f"mlx:{placement.node_id}:{placement.placement_id}:g{generation}"

   def offer_batch_member(self, item: HopWorkItem, *, member_ref: str) -> str:
      """Offer one item as a candidate member of the next dispatched group.

      Offering is a proposal, never membership: only a dispatch that computes the
      member attests it.  The item is bound-validated exactly as the per-item path
      validates it, and stays private until the group is dispatched.
      """

      if not isinstance(item, HopWorkItem):
         _reject("invalid_runtime_work_item")
      if (
         not isinstance(member_ref, str)
         or not member_ref
         or member_ref != member_ref.strip()
      ):
         _reject("invalid_batch_member_ref")
      binding = self._bound.get(item.placement_id)
      if binding is None:
         _reject("unbound_runtime_placement")
      if item.placement_id in self.standby_placement_ids:
         _reject("standby_placement_not_batched")
      stage, placement, loaded, runtime = binding
      self._validate_batch_key(item, placement)
      offered = _OfferedMember(
         member_ref=member_ref,
         item=item,
         stage=stage,
         placement=placement,
         loaded=loaded,
         runtime=runtime,
         offered_at=self._clock(),
      )
      with self._offered_lock:
         existing = self._offered_members.get(member_ref)
         if existing is not None:
            if (
               existing.item.path_id != item.path_id
               or existing.item.idempotency_key != item.idempotency_key
            ):
               _reject("duplicate_batch_member_ref")
            return member_ref
         self._offered_members[member_ref] = offered
      return member_ref

   def batch_observations(self) -> tuple[RuntimeBatchObservation, ...]:
      """Every runtime-owned observation this stage published, in dispatch order."""

      with self._offered_lock:
         return tuple(self._observations)

   def batch_observation(self) -> RuntimeBatchObservation | None:
      """The latest runtime-owned observation, or ``None`` when none exists.

      Absent evidence is ``None``: never a zeroed or synthesized record.
      """

      with self._offered_lock:
         return self._observations[-1] if self._observations else None

   def execution_interval(self) -> tuple[float, float]:
      """The measured collection/execution interval of the last observed group."""

      latest = self.batch_observation()
      if latest is None:
         _reject("missing_execution_interval_report")
      return (latest.collected_at, latest.executed_at)

   def _fresh_member_ready(self, member: _OfferedMember) -> bool:
      """True only when the incumbent path would accept this member as a fresh start."""

      item = member.item
      if not self._item_starts_kv(item):
         return False
      if item.position != 0:
         return False
      if item.phase == "PREFILL" and item.token_index != -1:
         return False
      if item.phase == "RECOVERY_PREFILL" and item.token_index < 0:
         return False
      if item.phase == "PREFILL_CHUNK" and item.token_index != 0:
         return False
      if item.path_id in self._kv_states:
         return False
      if self._released_paths.get(item.path_id) is not None:
         return False
      if float(self._clock()) >= float(item.lease_expires_at):
         return False
      if (item.path_id, item.idempotency_key) in self._replays:
         return False
      return True

   def _state_accepts(self, item: HopWorkItem, state: _KVState) -> bool:
      """True only when the incumbent continuation path would accept this member."""

      continues_prefill = item.phase == "PREFILL_CHUNK" and item.token_index > 0
      if item.request_id != state.request_id or item.path_attempt != state.path_attempt:
         return False
      if item.placement_id != state.placement_id:
         return False
      if item.position != state.next_position:
         return False
      if not continues_prefill and item.token_index != state.next_sequence:
         return False
      if continues_prefill and item.token_index < 1:
         return False
      if float(item.lease_expires_at) != state.lease_expires_at:
         return False
      if float(self._clock()) >= state.lease_expires_at:
         return False
      if (item.path_id, item.idempotency_key) in self._replays:
         return False
      return True

   def _batch_group_refusal(self, members: tuple[_OfferedMember, ...]) -> str | None:
      """Why this offered group cannot be ONE invocation, or ``None`` when it can.

      Deliberately conservative: the single-invocation path runs only when every
      member is in a state the incumbent per-item path would accept, so batching
      never weakens, bypasses or re-spells an incumbent refusal.  Anything this
      predicate cannot prove is served one member at a time and reported as a
      split.
      """

      if len(members) < MINIMUM_PHYSICAL_BATCH_MEMBERS:
         return "single_member_group"
      first = members[0]
      for member in members:
         item = member.item
         if member.stage.stage_id != first.stage.stage_id:
            return "heterogeneous_proposed_group"
         if member.placement.placement_id != first.placement.placement_id:
            return "heterogeneous_proposed_group"
         if item.phase != first.item.phase:
            return "mixed_member_phases"
         # A decode group may mix positions: each member is rotated/embedded at
         # its own position and attends only its own retained KV.  Every other
         # phase still needs one shared start position.
         if item.position != first.item.position and item.phase != "DECODE":
            return "mixed_start_positions"
         if bool(item.emits_token) != bool(first.item.emits_token):
            return "mixed_member_readout"
         if item.path_id in self._cancelled_paths:
            return "member_cancelled_before_launch"
         if self._item_starts_kv(item) != self._item_starts_kv(first.item):
            return "mixed_kv_continuation"
      if self._item_starts_kv(first.item):
         if any(not self._fresh_member_ready(member) for member in members):
            return "member_state_not_ready"
         return None
      pasts: list[Mapping[int, tuple[mx.array, mx.array]]] = []
      for member in members:
         state = self._kv_states.get(member.item.path_id)
         if state is None or not self._state_accepts(member.item, state):
            return "member_state_not_ready"
         pasts.append(state.layers)
      if any(not layers for layers in pasts):
         return "member_state_not_ready"
      layer_sets = {tuple(sorted(layers)) for layers in pasts}
      widths = {
         int(next(iter(layers.values()))[0].shape[2]) for layers in pasts
      }
      if len(layer_sets) != 1:
         return "member_kv_width_mismatch"
      if len(widths) != 1 and first.item.phase != "DECODE":
         return "member_kv_width_mismatch"
      return None

   def _single_invocation_context(
      self,
      members: tuple[_OfferedMember, ...],
   ) -> tuple[bool, int | tuple[int, ...], dict[int, Any]]:
      """The start position(s) and retained past for the one invocation.

      Members at one shared position with equal retained widths are stacked into
      one past per layer.  A decode group with mixed positions keeps every
      member's own past (a per-layer list) and one position per member, so each
      member attends exactly its own unpadded KV.
      """

      first = members[0].item
      if self._item_starts_kv(first):
         return True, int(first.position), {}
      pasts = [self._kv_states[member.item.path_id].layers for member in members]
      if len({int(member.item.position) for member in members}) != 1 or len(
         {int(next(iter(layers.values()))[0].shape[2]) for layers in pasts}
      ) != 1:
         ragged: dict[int, list[tuple[mx.array, mx.array]]] = {
            layer: [layers[layer] for layers in pasts] for layer in pasts[0]
         }
         return (
            False,
            tuple(int(member.item.position) for member in members),
            ragged,
         )
      stacked: dict[int, tuple[mx.array, mx.array]] = {}
      for layer in pasts[0]:
         keys = [layers[layer][0] for layers in pasts]
         values = [layers[layer][1] for layers in pasts]
         stacked[layer] = (
            keys[0] if len(keys) == 1 else mx.concatenate(keys, axis=0),
            values[0] if len(values) == 1 else mx.concatenate(values, axis=0),
         )
      return False, int(first.position), stacked

   def _acquire_group_state(self, members: tuple[_OfferedMember, ...]) -> None:
      """Acquire the one stage-state lock for the whole group, cancellation fenced."""

      while True:
         with self._execution_admission_lock:
            with self._cancellation_condition:
               cancellation_pending = (
                  self._cancellation_pending > 0
                  or bool(self._deferred_cancellation_cleanup)
               )
            if not cancellation_pending:
               self._state_lock.acquire()
               with self._cancellation_condition:
                  cancellation_pending = (
                     self._cancellation_pending > 0
                     or bool(self._deferred_cancellation_cleanup)
                  )
                  if not cancellation_pending:
                     with self._resource_index_lock:
                        for member in members:
                           self._executing_subjects[member.item.path_id] = (
                              member.item.request_id,
                              member.item.path_attempt,
                           )
               if not cancellation_pending:
                  return
               self._state_lock.release()
         with self._cancellation_condition:
            while (
               (
                  self._cancellation_pending > 0
                  or bool(self._deferred_cancellation_cleanup)
               )
               and not any(
                  member.item.path_id in self._cancelled_paths
                  for member in members
               )
            ):
               self._cancellation_condition.wait()

   def _checkpoint_group(self, members: tuple[_OfferedMember, ...]) -> None:
      """Post-launch cancellation fence: a member cancelled after launch is discarded."""

      for member in members:
         self._checkpoint_path(member.item.path_id)

   def _batched_stage_readout(
      self,
      hidden: mx.array,
      *,
      stage: Stage,
      loaded: LoadedStage,
      runtime: Mapping[str, Any],
   ) -> mx.array:
      """Apply the stage's final norm and vocabulary head across the whole batch.

      The shared per-item stage path applies the GPT-2 readout by component role
      even when ``produce_logits`` is false, so for that architecture the readout
      is already applied and this method is the identity.  The Qwen2/Qwen3 branch
      gates the readout on ``produce_logits``, so it is applied here instead --
      across every position, which for per-position norm and head operations is
      exactly the arithmetic the per-item path performs on its single last
      position.
      """

      if runtime["architecture"] not in {"qwen2", "qwen3"}:
         return hidden
      roles = stage.component_roles
      tensors = self._tensors_for(loaded)
      config = runtime["model_config"]
      if "final_norm" in roles:
         hidden = _rms_norm(
            hidden,
            tensors["model.norm.weight"],
            float(config["rms_norm_epsilon"]),
         )
      if "lm_head" in roles:
         aliases = loaded.resolved_aliases
         if not isinstance(aliases, Mapping):
            _reject("invalid_loaded_stage_aliases")
         alias = aliases.get("lm_head", {})
         if not isinstance(alias, Mapping):
            _reject("invalid_loaded_stage_aliases")
         head_keys = alias.get("tensor_keys", ["lm_head.weight"])
         if (
            not isinstance(head_keys, (list, tuple))
            or len(head_keys) != 1
            or not isinstance(head_keys[0], str)
         ):
            _reject("invalid_loaded_stage_aliases")
         hidden = _qwen2_linear(hidden, tensors[head_keys[0]])
      return hidden

   def _batched_dispatch(
      self,
      members: tuple[_OfferedMember, ...],
   ) -> tuple[tuple[MemberExecution, ...], tuple[tuple[str, str], ...]]:
      """Compute every accepted member in ONE padded batched backend invocation.

      Members are padded to the widest member.  A pad position is never attended
      by a real position of its own member (per-member causal mask) and never
      enters a member's reported result or committed KV (per-member truncation),
      so padding cannot leak between members or into a result.  The pass is one
      real backend invocation, and commit is atomic: every member's output and KV
      is validated before the first state mutation, so a refused pass leaves no
      member partially committed.
      """

      self._acquire_group_state(members)
      try:
         if self._closed:
            _reject("runtime_closed")
         for member in members:
            if member.item.path_id in self._cancelled_paths:
               _reject("path_cancelled")
         self.expire_leases()
         if self._batch_group_refusal(members) is not None:
            _reject("batch_group_not_ready")
         fresh, start_position, past_layers = self._single_invocation_context(members)
         stage = members[0].stage
         loaded = members[0].loaded
         runtime = members[0].runtime
         emit = members[0].item.phase != "PREFILL_CHUNK" or bool(
            members[0].item.emits_token
         )
         token_rows: list[list[int]] = []
         hidden_rows: list[mx.array] = []
         lengths: list[int] = []
         for member in members:
            token_ids, hidden_states, sequence = self._decode_input(
               member.item, stage, runtime
            )
            lengths.append(sequence)
            if token_ids is not None:
               token_rows.append([int(value) for value in token_ids[0].tolist()])
            if hidden_states is not None:
               hidden_rows.append(hidden_states)
         width = max(lengths)
         if token_rows:
            batched_tokens: mx.array | None = mx.array(
               [row + [0] * (width - len(row)) for row in token_rows],
               dtype=mx.uint32,
            )
            batched_hidden: mx.array | None = None
         else:
            padded: list[mx.array] = []
            for row, length in zip(hidden_rows, lengths):
               if length < width:
                  padding = mx.zeros(
                     (1, width - length, int(row.shape[2])), dtype=row.dtype
                  )
                  row = mx.concatenate((row, padding), axis=1)
               padded.append(row)
            batched_tokens = None
            batched_hidden = (
               padded[0] if len(padded) == 1 else mx.concatenate(padded, axis=0)
            )
         output, next_layers = self._execute_stage_with_kv(
            stage=stage,
            loaded=loaded,
            runtime=runtime,
            token_ids=batched_tokens,
            hidden_states=batched_hidden,
            position=start_position,
            past_layers=past_layers,
            path_id=members[0].item.path_id,
            produce_logits=False,
         )
         if emit:
            output = self._batched_stage_readout(
               output, stage=stage, loaded=loaded, runtime=runtime
            )
         mx.eval(output, *(array for pair in next_layers.values() for array in pair))
         self._checkpoint_group(members)
         if not bool(mx.all(mx.isfinite(output)).item()):
            _reject("nonfinite_stage_output")
         pending: list[
            tuple[_OfferedMember, int, RuntimeResult, dict[int, tuple[mx.array, mx.array]]]
         ] = []
         for index, member in enumerate(members):
            length = lengths[index]
            row = output[index : index + 1][:, :length, :]
            if not bool(mx.all(mx.isfinite(row)).item()):
               _reject("nonfinite_stage_output")
            result = self._runtime_result(stage, runtime, row, produce_token=emit)
            layers: dict[int, tuple[mx.array, mx.array]] = {}
            for layer, layer_kv in next_layers.items():
               if isinstance(layer_kv, list):
                  # Ragged decode: the member's own unpadded past plus its key.
                  key_rows, value_rows = layer_kv[index]
               else:
                  all_key, all_value = layer_kv
                  key_rows = all_key[index : index + 1]
                  value_rows = all_value[index : index + 1]
                  # Keep the member's whole retained prefix plus its own real
                  # positions; only padding past that is dropped.
                  retained = (
                     int(past_layers[layer][0].shape[2])
                     if layer in past_layers
                     else 0
                  )
                  if int(key_rows.shape[2]) > retained + length:
                     key_rows = key_rows[:, :, : retained + length, :]
                     value_rows = value_rows[:, :, : retained + length, :]
               layers[layer] = (key_rows, value_rows)
            pending.append((member, length, result, layers))
         executions: list[MemberExecution] = []
         outcomes: list[tuple[str, str]] = []
         for member, length, result, layers in pending:
            item = member.item
            if fresh:
               state = _KVState(
                  request_id=item.request_id,
                  path_id=item.path_id,
                  path_attempt=item.path_attempt,
                  placement_id=item.placement_id,
                  assignment_id=member.placement.assignment_id,
                  manifest_digest=self.graph.manifest_digest,
                  deployment_epoch=self.graph.deployment_epoch,
                  lease_expires_at=float(item.lease_expires_at),
                  next_position=length,
                  next_sequence=(
                     item.token_index + 1
                     if item.phase == "RECOVERY_PREFILL"
                     else 1
                  ),
                  cached_context_tokens=length,
                  layers=layers,
               )
               self._kv_states[item.path_id] = state
               with self._resource_index_lock:
                  self._kv_subjects[item.path_id] = (
                     item.request_id,
                     item.path_attempt,
                  )
            else:
               state = self._kv_states[item.path_id]
               state.layers = layers
               state.next_position += length
               if not (item.phase == "PREFILL_CHUNK" and item.token_index > 0):
                  state.next_sequence += 1
               state.cached_context_tokens += length
            self._update_kv_watermark()
            self._applied_operation_count += 1
            phase_counter = (
               "decode_operation_count"
               if item.phase == "DECODE"
               else "prefill_operation_count"
            )
            self._placement_counters[item.placement_id][phase_counter] += 1
            self._activation_output_bytes += len(result.payload or b"")
            if item.phase in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL"}:
               self._prefill_operation_count += 1
               self._prefill_input_token_count += length
            else:
               self._decode_operation_count += 1
               self._decode_input_token_count += length
            self._remember_result(
               item, self._operation_fingerprint(item), result
            )
            if item.terminal:
               self._release_state(item.path_id, "normal_completion")
            executions.append(
               MemberExecution(
                  member_ref=member.member_ref,
                  kv_ref=self._kv_identity(item),
                  result=result,
                  committed_prefix=int(state.next_position),
               )
            )
            outcomes.append((member.member_ref, self._member_outcome(result)))
         return tuple(executions), tuple(outcomes)
      finally:
         with self._cancellation_condition:
            for cancelled_path in tuple(self._deferred_cancellation_cleanup):
               self._release_state(cancelled_path, "cancellation")
               self._purge_path_replays(cancelled_path)
               self._deferred_cancellation_cleanup.discard(cancelled_path)
            with self._resource_index_lock:
               for member in members:
                  self._executing_subjects.pop(member.item.path_id, None)
            self._cancellation_condition.notify_all()
            self._state_lock.release()

   def _run_members_individually(
      self,
      members: tuple[_OfferedMember, ...],
   ) -> tuple[tuple[MemberExecution, ...], tuple[tuple[str, str], ...]]:
      """Serve every member one at a time -- the honest split, never a batch.

      A member refused at admission is still counted as one attempted
      invocation, which can only overstate the invocation count; the split
      signature is therefore never understated and a loop can never be reported
      as a batch.
      """

      executions: list[MemberExecution] = []
      outcomes: list[tuple[str, str]] = []
      for member in members:
         result = self.execute(member.item)
         state = self._kv_states.get(member.item.path_id)
         executions.append(
            MemberExecution(
               member_ref=member.member_ref,
               kv_ref=self._kv_identity(member.item),
               result=result,
               committed_prefix=int(state.next_position) if state is not None else 0,
            )
         )
         outcomes.append((member.member_ref, self._member_outcome(result)))
      return tuple(executions), tuple(outcomes)

   def _publish_observation(
      self,
      *,
      request: RuntimeBatchRequest,
      stage_id: str,
      backend_invocations: int,
      collected_at: float,
      outcomes: tuple[tuple[str, str], ...],
   ) -> RuntimeBatchObservation:
      observation = RuntimeBatchObservation(
         batch_ref=request.batch_ref,
         phase=request.phase,
         stage=stage_id,
         backend_invocation_id=(
            f"invocation:{request.batch_ref}:{backend_invocations}"
         ),
         backend_invocations=backend_invocations,
         membership_authority=MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
         proposed_member_count=request.proposed_member_count,
         actual_member_count=len(outcomes),
         member_outcomes=outcomes,
         collected_at=collected_at,
         executed_at=self._clock(),
         cleanup="released",
      )
      with self._offered_lock:
         self._observations.append(observation)
      return observation

   def _dispatch_offered_batch(
      self,
      request: RuntimeBatchRequest,
   ) -> BatchExecutionReport:
      """Resolve one offered group, compute it, and publish what actually happened."""

      if not isinstance(request, RuntimeBatchRequest):
         _reject("invalid_runtime_batch_request")
      with self._offered_lock:
         resolved: list[_OfferedMember] = []
         for member_ref in request.member_refs:
            member = self._offered_members.pop(member_ref, None)
            if member is not None:
               resolved.append(member)
      if not resolved:
         _reject("unoffered_batch_member")
      accepted = tuple(
         member
         for member in resolved
         if member.item.path_id not in self._cancelled_paths
      )
      if not accepted:
         _reject("path_cancelled")
      collected_at = min(member.offered_at for member in accepted)
      # A runtime batch is claimed only when the runtime computed exactly the set
      # the relay proposed.  Anything short of that is a split, and a split is
      # reported as a split (resolution 2), never as a batch.
      complete = (
         len(resolved) == len(request.member_refs)
         and len(accepted) == request.proposed_member_count
      )
      if complete and self._batch_group_refusal(accepted) is None:
         invocation_started = time.monotonic()
         executions, outcomes = self._batched_dispatch(accepted)
         invocation_finished = time.monotonic()
         dispatch_mode = RUNTIME_BATCH_DISPATCH_MODE
         backend_invocations = BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
         # Every member was computed inside the one backend invocation, so each
         # member's stage-compute interval is that invocation's interval.
         members = {member.member_ref: member for member in accepted}
         for execution in executions:
            member = members.get(execution.member_ref)
            if member is not None:
               self._a10_record(
                  member.item,
                  invocation_started,
                  invocation_finished,
                  execution.result,
                  runtime_batch_ref=request.batch_ref,
               )
      else:
         executions, outcomes = self._run_members_individually(accepted)
         dispatch_mode = SEQUENTIAL_DISPATCH_MODE
         backend_invocations = len(executions)
      self._publish_observation(
         request=request,
         stage_id=accepted[0].stage.stage_id,
         backend_invocations=backend_invocations,
         collected_at=collected_at,
         outcomes=outcomes,
      )
      return BatchExecutionReport(
         dispatch_mode=dispatch_mode,
         backend_invocations=backend_invocations,
         membership_authority=MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
         incarnation=self._incarnation_for(accepted[0].placement, accepted[0].loaded),
         executed=executions,
         arena_open=False,
      )

   def verify_sequential_parity(
      self,
      report: BatchExecutionReport,
      *,
      batch_ref: str,
      members: Mapping[str, HopWorkItem],
   ) -> RuntimeBatchParityVerdict:
      """Compare a batched dispatch against the incumbent per-item path, per member.

      The reference authority is the incumbent ``execute`` path on a pristine port
      built from the same bindings -- not a re-implementation and not this port's
      own committed state.  Parity is per member: token decision equality, payload
      shape equality, payload digest equality and the maximum absolute deviation
      between the two payloads.  Payload bytes, logits, activations and KV
      material are never retained in the verdict.
      """

      if not isinstance(report, BatchExecutionReport):
         _reject("invalid_batch_execution_report")
      if (
         report.dispatch_mode != RUNTIME_BATCH_DISPATCH_MODE
         or report.backend_invocations != BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
         or len(report.executed) < MINIMUM_PHYSICAL_BATCH_MEMBERS
      ):
         _reject("observed_batch_required_for_parity")
      reference = MLXRuntimePort(
         self.node_id,
         self.graph,
         {placement_id: bound[2] for placement_id, bound in self._bound.items()},
      )
      compared: list[RuntimeBatchMemberParity] = []
      for execution in report.executed:
         item = members.get(execution.member_ref)
         if item is None:
            _reject("parity_reference_member_mismatch")
         compared.append(
            self._compare_member_parity(
               execution.member_ref,
               execution.result,
               reference.execute(item),
            )
         )
      return RuntimeBatchParityVerdict(
         batch_ref=batch_ref,
         dispatch_mode=report.dispatch_mode,
         batched_backend_invocations=report.backend_invocations,
         members=tuple(compared),
      )

   @staticmethod
   def _payload_arrays(
      result: RuntimeResult,
   ) -> tuple[tuple[int, ...], str, mx.array | None]:
      """(shape, digest, array) for one result payload -- never published."""

      payload = result.payload
      if payload is None:
         return (), "", None
      if not isinstance(payload, bytes):
         _reject("invalid_member_result_fails_closed")
      envelope = decode_activation(payload)
      raw = mx.array(memoryview(envelope.data), dtype=mx.uint8)
      array = raw.view(_MLX_DTYPES[envelope.dtype]).reshape(envelope.shape)
      array = array.astype(mx.float32)
      return (
         tuple(int(value) for value in envelope.shape),
         hashlib.sha256(payload).hexdigest(),
         array,
      )

   @classmethod
   def _compare_member_parity(
      cls,
      member_ref: str,
      batched: RuntimeResult,
      sequential: RuntimeResult,
   ) -> RuntimeBatchMemberParity:
      batched_shape, batched_digest, batched_array = cls._payload_arrays(batched)
      sequential_shape, sequential_digest, sequential_array = cls._payload_arrays(
         sequential
      )
      deviation = 0.0
      if batched_array is not None and sequential_array is not None:
         if batched_shape == sequential_shape:
            difference = mx.abs(batched_array - sequential_array)
            deviation = float(mx.max(difference).item())
      return RuntimeBatchMemberParity(
         member_ref=member_ref,
         decision_equal=batched.token_id == sequential.token_id,
         payload_digest_equal=batched_digest == sequential_digest,
         payload_shape_equal=batched_shape == sequential_shape,
         maximum_absolute_deviation=deviation,
      )

   def execute_batch(self, batch: Any) -> tuple[RuntimeResult, ...] | BatchExecutionReport:
      """Dispatch an offered group, or run a ``RuntimeBatch`` one item at a time.

      ``RuntimeBatch`` keeps the incumbent ordered, item-isolated behaviour and
      returns its result tuple; it computes no observed batch and therefore
      publishes no observation record (missing evidence is absent, never zero and
      never success).  ``RuntimeBatchRequest`` is the A10 dispatch: it resolves the
      offered members, computes an eligible group in one padded batched forward
      pass, and returns this runtime's own ``BatchExecutionReport``.  A group that
      cannot be served as one invocation is served one member at a time and
      reported as ``sequential_dispatch``.
      """

      if isinstance(batch, RuntimeBatchRequest):
         return self._dispatch_offered_batch(batch)
      if not isinstance(batch, RuntimeBatch):
         raise MLXRuntimeError("invalid_runtime_batch")
      return tuple(self.execute(item) for item in batch.items)


# Compatibility spelling for callers that prefer conventional CamelCase.
MlxRuntimePort = MLXRuntimePort

__all__ = [
   "A10_OBSERVATION_PROTOCOL",
   "MLXRuntimeError",
   "MLXRuntimePort",
   "MlxRuntimePort",
   "RUNTIME_BATCH_DISPATCH_MODE",
   "SEQUENTIAL_DISPATCH_MODE",
   "RuntimeBatchMemberParity",
   "RuntimeBatchParityVerdict",
]
