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


def _gpt2_block_with_kv(
   hidden: mx.array,
   tensors: Mapping[str, mx.array],
   prefix: str,
   n_head: int,
   epsilon: float,
   past: tuple[mx.array, mx.array] | None,
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
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
   if past is None:
      all_key = key
      all_value = value
   else:
      all_key = mx.concatenate((past[0], key), axis=2)
      all_value = mx.concatenate((past[1], value), axis=2)
   scores = mx.matmul(query, all_key.transpose(0, 1, 3, 2)) / math.sqrt(head_size)
   if past is None:
      positions = mx.arange(sequence)
      causal = positions[:, None] >= positions[None, :]
      scores = mx.where(
         causal[None, None, :, :],
         scores,
         mx.array(-math.inf, dtype=scores.dtype),
      )
   weights = mx.softmax(scores, axis=-1)
   attention = mx.matmul(weights, all_value)
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
   return residual + feed_forward, (all_key, all_value)


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


def _qwen2_linear(hidden: mx.array, weight: Any) -> mx.array:
   if isinstance(weight, Int8RowwiseWeight):
      projected = mx.matmul(
         hidden,
         weight.values.astype(mx.float32).transpose(1, 0),
      )
      return projected * weight.scales
   return mx.matmul(hidden, weight.transpose(1, 0))


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


def _qwen2_block_with_kv(
   hidden: mx.array,
   tensors: Mapping[str, Any],
   prefix: str,
   config: Mapping[str, Any],
   position: int,
   past: tuple[mx.array, mx.array] | None,
   architecture: str = "qwen2",
) -> tuple[mx.array, tuple[mx.array, mx.array]]:
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
   attended = mx.matmul(probabilities, attention_value)
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
   return hidden, (all_key, all_value)


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
      supplied_ids = set(loaded_stages)
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
      self._prefill_operation_count = 0
      self._prefill_input_token_count = 0
      self._decode_operation_count = 0
      self._decode_input_token_count = 0
      self._activation_output_bytes = 0
      self._maximum_observed_work_unit_ms = 0.0
      self._observed_work_unit_count = 0
      self._closed = False
      self._state_lock = RLock()
      self._cancellation_lock = RLock()
      self._cancellation_condition = Condition(self._cancellation_lock)
      self._cancellation_pending = 0
      self._execution_admission_lock = RLock()
      # Exact-path cleanup proof must not queue behind unrelated model work.
      self._resource_index_lock = RLock()
      self._executing_subjects: dict[str, tuple[str, int]] = {}
      self._kv_subjects: dict[str, tuple[str, int]] = {}

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

   def operation_counter_snapshot(self) -> dict[str, int]:
      """Return lock-free monotonic work telemetry for active health probes."""

      return {"applied_operation_count": self._applied_operation_count}

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
            return self._execute_bound(item)
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
      position: int,
      past_layers: Mapping[int, tuple[mx.array, mx.array]],
      path_id: str,
      produce_logits: bool = True,
   ) -> tuple[mx.array, dict[int, tuple[mx.array, mx.array]]]:
      config = runtime["model_config"]
      tensors = loaded.tensors
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
         next_layers: dict[int, tuple[mx.array, mx.array]] = {}
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
         positions = mx.arange(position, position + sequence, dtype=mx.int32)
         hidden = tensors[f"{namespace}wte.weight"][token_ids] + tensors[
            f"{namespace}wpe.weight"
         ][positions]
      else:
         if hidden_states is None or token_ids is not None:
            _reject("non_entry_stage_requires_hidden_states")
         hidden = hidden_states

      epsilon = float(config["layer_norm_epsilon"])
      next_layers: dict[int, tuple[mx.array, mx.array]] = {}
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
      if item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
         if item.phase == "PREFILL" and item.token_index != -1:
            _reject("prefill_sequence_mismatch")
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
      self._activation_output_bytes += len(result.payload or b"")
      if item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
         self._prefill_operation_count += 1
         self._prefill_input_token_count += sequence
      else:
         self._decode_operation_count += 1
         self._decode_input_token_count += sequence
      self._remember_result(item, fingerprint, result)
      return result

   def _execute_bound(self, item: HopWorkItem) -> RuntimeResult:
      if item.phase == "PREFILL_CHUNK" and self.decode_mode != "stage_local_kv":
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

   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
      """Execute serially in input order; one item cannot poison its siblings."""

      if not isinstance(batch, RuntimeBatch):
         raise MLXRuntimeError("invalid_runtime_batch")
      return tuple(self.execute(item) for item in batch.items)


# Compatibility spelling for callers that prefer conventional CamelCase.
MlxRuntimePort = MLXRuntimePort

__all__ = ["MLXRuntimeError", "MLXRuntimePort", "MlxRuntimePort"]
