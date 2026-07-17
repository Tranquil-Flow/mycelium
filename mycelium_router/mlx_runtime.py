"""Assignment- and graph-bound MLX implementation of the Router RuntimePort.

The port executes one already loaded GPT-2 stage at a time. It does not load
weights, use pickle/JSON tensor arrays, maintain a KV cache, fuse batches, or
make any distributed-transport claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from threading import Lock
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
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.payloads import (
   PayloadError,
   decode_activation,
   decode_token_ids,
   encode_activation,
)
from mycelium_router.validation import ContractError, validate_execution_graph
from runtime_contracts import validate_normalized_mlx_runtime
from runtime_loader import (
   LoadedStage,
   RuntimeExecutionError,
   canonical_json,
   execute_loaded_stage,
)


_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}
_MLX_DTYPES = {
   "float16": mx.float16,
   "bfloat16": mx.bfloat16,
   "float32": mx.float32,
}
_SUPPORTED_COMPONENTS = {"input_embedding", "decoder", "final_norm", "lm_head"}
_LOAD_PROOF_FIELDS = {
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
}


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
   material = {
      "protocol": "mycelium.stage_signature.v1",
      "deployment_id": graph.deployment_id,
      "deployment_epoch": graph.deployment_epoch,
      "model_id": graph.model_id,
      "resolved_commit": graph.resolved_commit,
      "manifest_digest": graph.manifest_digest,
      "stage_id": stage.stage_id,
      "range": _plain_json(proof["loaded_range"]),
      "components": _plain_json(proof["loaded_components"]),
      "hidden_size": graph.hidden_size,
      "dtype_bytes": graph.activation_bytes,
   }
   encoded = json.dumps(
      material,
      sort_keys=True,
      separators=(",", ":"),
      ensure_ascii=False,
      allow_nan=False,
   ).encode("utf-8")
   return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _range_document(stage: Stage) -> dict[str, int]:
   return {
      "start_layer": stage.layer_range.start_layer,
      "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
      "layer_count": stage.layer_range.layer_count,
   }


class MLXRuntimePort:
   """Serial, item-isolated RuntimePort over assignment-bound LoadedStages."""

   def __init__(
      self,
      node_id: str,
      graph: ExecutionGraph,
      loaded_stages: Mapping[str, LoadedStage],
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
      self._bound = MappingProxyType(bound)
      self._cancelled_paths: set[str] = set()
      self._cancellation_lock = Lock()

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
      if not isinstance(plain_proof, dict) or set(plain_proof) != _LOAD_PROOF_FIELDS:
         _reject("invalid_load_proof_fields", placement.placement_id)
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

   def cancel(self, path_id: str) -> None:
      """Idempotently prevent any future work for ``path_id``."""

      if not isinstance(path_id, str):
         return
      with self._cancellation_lock:
         self._cancelled_paths.add(path_id)

   def _is_cancelled(self, path_id: str) -> bool:
      with self._cancellation_lock:
         return path_id in self._cancelled_paths

   @staticmethod
   def _failure(reason: str) -> RuntimeResult:
      return RuntimeResult(
         success=False,
         failure_scope="PLACEMENT",
         failure_reason=reason,
      )

   def execute(self, item: HopWorkItem) -> RuntimeResult:
      """Execute one item, converting all malformed/unbound work to failure."""

      if not isinstance(item, HopWorkItem):
         return self._failure("invalid_runtime_work_item")
      if self._is_cancelled(item.path_id):
         return self._failure("path_cancelled")
      try:
         return self._execute_bound(item)
      except PayloadError as exc:
         return self._failure(exc.code)
      except MLXRuntimeError as exc:
         return self._failure(exc.code)
      except RuntimeExecutionError as exc:
         return self._failure(str(exc) or "runtime_execution_rejected")
      except Exception:
         return self._failure("runtime_execution_rejected")

   def _execute_bound(self, item: HopWorkItem) -> RuntimeResult:
      if item.phase == "PREFILL_CHUNK":
         _reject("prefill_chunk_requires_kv_continuity")
      if item.phase not in {"PREFILL", "DECODE"}:
         _reject("unsupported_runtime_phase")
      if not isinstance(item.payload, bytes):
         _reject("runtime_payload_must_be_bytes")
      binding = self._bound.get(item.placement_id)
      if binding is None:
         _reject("unbound_runtime_placement")
      stage, placement, loaded, runtime = binding
      self._validate_batch_key(item, placement)

      if stage.stage_id == self.graph.entry_stage_id:
         token_ids = decode_token_ids(item.payload)
         if not token_ids:
            _reject("empty_token_sequence")
         config = runtime["model_config"]
         if len(token_ids) > config["n_positions"]:
            _reject("position_bounds_exceeded")
         if any(token_id >= config["vocab_size"] for token_id in token_ids):
            _reject("token_bounds_exceeded")
         self._validate_sequence_span(item.batch_key, item.phase, len(token_ids))
         tokens = mx.array((token_ids,), dtype=mx.uint32)
         output = execute_loaded_stage(loaded, token_ids=tokens)
      else:
         envelope = decode_activation(item.payload)
         if envelope.dtype != runtime["dtype"]:
            _reject("activation_dtype_mismatch")
         if len(envelope.shape) != 3:
            _reject("activation_rank_mismatch")
         if (
            envelope.shape[0] != 1
            or envelope.shape[2] != self.graph.hidden_size
         ):
            _reject("activation_shape_mismatch")
         sequence = envelope.shape[1]
         if sequence > runtime["model_config"]["n_positions"]:
            _reject("position_bounds_exceeded")
         self._validate_sequence_span(item.batch_key, item.phase, sequence)
         if sys.byteorder != "little":
            _reject("unsupported_host_byte_order")
         raw = mx.array(memoryview(envelope.data), dtype=mx.uint8)
         hidden = raw.view(_MLX_DTYPES[envelope.dtype]).reshape(envelope.shape)
         mx.eval(hidden)
         if not bool(mx.all(mx.isfinite(hidden)).item()):
            _reject("nonfinite_activation")
         output = execute_loaded_stage(loaded, hidden_states=hidden)

      if stage.stage_id == self.graph.final_stage_id:
         expected = (
            1,
            int(output.shape[1]),
            runtime["model_config"]["vocab_size"],
         )
         if tuple(int(value) for value in output.shape) != expected:
            _reject("invalid_final_stage_output")
         token_id = int(mx.argmax(output[0, -1, :]).item())
         return RuntimeResult(success=True, token_id=token_id)

      expected = (1, int(output.shape[1]), self.graph.hidden_size)
      if tuple(int(value) for value in output.shape) != expected:
         _reject("invalid_intermediate_stage_output")
      contiguous = mx.contiguous(output)
      mx.eval(contiguous)
      payload = encode_activation(
         dtype=runtime["dtype"],
         shape=tuple(int(value) for value in contiguous.shape),
         data=bytes(contiguous),
      )
      return RuntimeResult(success=True, payload=payload)

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

   @staticmethod
   def _validate_sequence_span(
      key: RuntimeBatchKey | None,
      phase: str,
      sequence: int,
   ) -> None:
      if key is None:
         _reject("missing_runtime_batch_key")
      if phase == "PREFILL" and key.token_span != sequence:
         _reject("batch_key_token_span_mismatch")
      if phase == "DECODE" and key.token_span != 1:
         _reject("batch_key_token_span_mismatch")

   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
      """Execute serially in input order; one item cannot poison its siblings."""

      if not isinstance(batch, RuntimeBatch):
         raise MLXRuntimeError("invalid_runtime_batch")
      return tuple(self.execute(item) for item in batch.items)


# Compatibility spelling for callers that prefer conventional CamelCase.
MlxRuntimePort = MLXRuntimePort

__all__ = ["MLXRuntimeError", "MLXRuntimePort", "MlxRuntimePort"]
