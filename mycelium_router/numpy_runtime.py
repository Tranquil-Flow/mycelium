"""Assignment- and graph-bound NumPy implementation of the Router RuntimePort.

The port executes one already loaded GPT-2 stage at a time using only NumPy
(no MLX, no CUDA, no MKL claims).  It is fail-closed: every binding (graph,
load proof, placement, batch key, payload) is revalidated against loader-held
evidence before any execution.  ``complete_context_replay`` is the sole
supported decode mode; every call receives the full token sequence (entry
stage) or the full hidden-state sequence (intermediate stage) and produces
deterministic activation bytes or a deterministic token id.  No state is
carried between calls and the optional ``kv_snapshot`` only exposes aggregate
lifecycle counters.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, NoReturn

import numpy as np

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
from numpy_runtime import (
   NumpyRuntimeError as _StageNumpyRuntimeError,
   execute_loaded_stage as _numpy_execute_loaded_stage,
)
from runtime_contracts import validate_normalized_numpy_runtime
from runtime_loader import (
   LoadedStage,
   RuntimeExecutionError,
   canonical_json,
)


_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DTYPE_BYTES = {"float16": 2, "bfloat16": 2, "float32": 4}
_SUPPORTED_COMPONENTS = frozenset(
   {"input_embedding", "decoder", "final_norm", "lm_head"}
)
_BASE_LOAD_PROOF_FIELDS = frozenset(
   {
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
)
_STAGE_PACK_PROOF_FIELDS = frozenset(
   {"stage_pack_digest", "stage_pack_verification_digest"}
)
_MAX_RETAINED_OPERATIONS = 4096


class NumpyRouterRuntimeError(ValueError):
   """Fail-closed binding or execution error with a stable code."""

   def __init__(self, code: str, detail: str = "") -> None:
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


# Backwards-compatible alias.  Some test/imports use the short name; the
# canonical class is NumpyRouterRuntimeError to avoid colliding with the
# top-level numpy_runtime.NumpyRuntimeError (assignment-local stage adapter).
NumpyRuntimeError = NumpyRouterRuntimeError


def _reject(code: str, detail: str = "") -> NoReturn:
   raise NumpyRouterRuntimeError(code, detail)


def _plain_json(value: Any) -> Any:
   """Thaw immutable proof mappings through the loader's canonical codec."""

   try:
      return json.loads(canonical_json(value))
   except (TypeError, ValueError, json.JSONDecodeError) as exc:
      raise NumpyRuntimeError("noncanonical_load_proof") from exc


def _stage_signature(
   graph: ExecutionGraph, stage: Stage, proof: Mapping[str, Any]
) -> str:
   runtime = proof.get("runtime")
   if not isinstance(runtime, Mapping):
      _reject("invalid_loaded_stage_runtime", stage.stage_id)
   runtime_backend = runtime.get("backend")
   if not isinstance(runtime_backend, str):
      _reject("invalid_loaded_stage_runtime", stage.stage_id)
   try:
      return stage_signature_for_backend(graph, stage, runtime_backend)
   except ValueError as exc:
      raise NumpyRuntimeError("invalid_loaded_stage_runtime", stage.stage_id) from exc


def _range_document(stage: Stage) -> dict[str, int]:
   return {
      "start_layer": stage.layer_range.start_layer,
      "end_layer_exclusive": stage.layer_range.end_layer_exclusive,
      "layer_count": stage.layer_range.layer_count,
   }


@dataclass(frozen=True)
class _ReplayResult:
   fingerprint: str
   result: RuntimeResult


def _numpy_identity_fields() -> tuple[str, ...]:
   return ("backend", "backend_version", "device", "dtype", "quantization", "architecture")


def _numpy_runtime_identity() -> Mapping[str, Any]:
   return {
      "backend": "numpy",
      "backend_version": importlib.metadata.version("numpy"),
      "device": "cpu",
      "dtype": "float32",
      "quantization": "none",
      "architecture": "gpt2",
   }


def _is_finite_array(value: Any) -> bool:
   try:
      array = np.asarray(value)
   except (TypeError, ValueError):
      return False
   if not np.isfinite(array).all():
      return False
   return True


class NumpyRuntimePort:
   """Serial, item-isolated, fail-closed NumPy RuntimePort.

   ``decode_mode`` is fixed to ``complete_context_replay``: every call carries
   the complete context as either a token-id sequence (entry stage) or a
   hidden-state envelope (intermediate/final stage).  No KV state is retained
   between calls.
   """

   decode_mode = "complete_context_replay"
   backend = "numpy"

   def __init__(
      self,
      node_id: str,
      graph: ExecutionGraph,
      loaded_stages: Mapping[str, LoadedStage],
      *,
      clock: Callable[[], float] | None = None,
   ):
      if not isinstance(node_id, str) or not node_id or node_id != node_id.strip():
         _reject("invalid_runtime_node_id")
      if not isinstance(graph, ExecutionGraph):
         _reject("invalid_execution_graph")
      try:
         validate_execution_graph(graph)
      except (ContractError, AttributeError, TypeError, ValueError) as exc:
         code = exc.code if isinstance(exc, ContractError) else "invalid_execution_graph"
         raise NumpyRuntimeError(code) from exc
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
      if clock is not None and not callable(clock):
         _reject("invalid_runtime_clock")
      self._clock = clock or (importlib.import_module("time").monotonic)
      self._cancelled_paths: OrderedDict[str, None] = OrderedDict()
      self._replays: OrderedDict[tuple[str, str], _ReplayResult] = OrderedDict()
      self._release_counts: Counter[str] = Counter()
      self._applied_operation_count = 0
      self._closed = False
      self._state_lock = RLock()

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
         runtime = validate_normalized_numpy_runtime(plain_proof.get("runtime"))
      except (TypeError, ValueError) as exc:
         raise NumpyRuntimeError(
            "invalid_loaded_stage_runtime", placement.placement_id
         ) from exc
      config = runtime["model_config"]
      if config["n_embd"] != graph.hidden_size:
         _reject("model_hidden_size_mismatch", placement.placement_id)
      if config["n_layer"] != graph.stages[-1].layer_range.end_layer_exclusive:
         _reject("model_layer_range_mismatch", placement.placement_id)
      if _DTYPE_BYTES[runtime["dtype"]] != graph.activation_bytes:
         _reject("model_activation_bytes_mismatch", placement.placement_id)
      if runtime["backend"] != placement.runtime_backend:
         _reject("runtime_backend_mismatch", placement.placement_id)
      if placement.runtime_backend != "numpy":
         _reject("runtime_backend_unsupported", placement.placement_id)

      identity = proof.get("runtime_identity")
      expected_identity_fields = set(_numpy_identity_fields())
      if (
         not isinstance(identity, Mapping)
         or set(identity) != expected_identity_fields
      ):
         _reject("invalid_runtime_identity", placement.placement_id)
      canonical_identity = _numpy_runtime_identity()
      for field, expected in canonical_identity.items():
         if identity.get(field) != expected:
            _reject(
               f"runtime_identity_{field}_mismatch", placement.placement_id
            )
      if not isinstance(identity.get("backend_version"), str) or not identity[
         "backend_version"
      ]:
         _reject("invalid_runtime_identity", placement.placement_id)
      if not isinstance(identity.get("device"), str) or not identity["device"]:
         _reject("invalid_runtime_identity", placement.placement_id)

      probe_shape = proof.get("probe_shape")
      probe_array = getattr(loaded, "probe_output", None)
      probe_shape_tuple: tuple[int, ...] = ()
      if probe_array is not None:
         try:
            probe_shape_tuple = tuple(int(value) for value in np.asarray(probe_array).shape)
         except (TypeError, ValueError):
            probe_shape_tuple = ()
      if (
         not isinstance(probe_shape, (list, tuple))
         or not probe_shape
         or any(
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
            for dimension in probe_shape
         )
         or (
            probe_shape_tuple
            and tuple(int(value) for value in probe_shape)
            != probe_shape_tuple
         )
      ):
         _reject("probe_shape_mismatch", placement.placement_id)
      if probe_array is not None and not _is_finite_array(probe_array):
         _reject("nonfinite_probe_output", placement.placement_id)

      try:
         digest = layer_load_proof_digest(plain_proof)
      except (TypeError, ValueError) as exc:
         raise NumpyRuntimeError("invalid_load_proof", placement.placement_id) from exc
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
      """Idempotently reject future work for ``path_id``; release replay cache."""

      if not isinstance(path_id, str):
         return
      with self._state_lock:
         self._remember_path_marker(self._cancelled_paths, path_id, None)
         self._purge_path_replays(path_id)

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
      key_material: Any = None
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

   def close(self, *, reason: str = "worker_shutdown") -> None:
      """Release all per-path replay cache before worker/process teardown."""

      if not isinstance(reason, str) or not reason:
         raise NumpyRuntimeError("invalid_runtime_close_reason")
      with self._state_lock:
         if self._closed:
            return
         self._replays.clear()
         self._closed = True
         self._release_counts[reason] += 1

   def kv_snapshot(self) -> dict[str, Any]:
      """Return identity/lifecycle evidence without KV tensor values."""

      with self._state_lock:
         return {
            "mode": "complete_context_replay",
            "backend": "numpy",
            "closed": self._closed,
            "active_state_count": 0,
            "retained_result_count": len(self._replays),
            "applied_operation_count": self._applied_operation_count,
            "cancelled_path_count": len(self._cancelled_paths),
            "release_counts": dict(sorted(self._release_counts.items())),
         }

   def execute(self, item: HopWorkItem) -> RuntimeResult:
      """Execute one fail-closed, path-serialized complete-context-replay call."""

      if not isinstance(item, HopWorkItem):
         return self._failure("invalid_runtime_work_item")
      with self._state_lock:
         if self._closed:
            return self._failure("runtime_closed")
         if item.path_id in self._cancelled_paths:
            return self._failure("path_cancelled")
         try:
            return self._execute_bound(item)
         except PayloadError as exc:
            return self._failure(exc.code)
         except NumpyRouterRuntimeError as exc:
            return self._failure(exc.code)
         except RuntimeExecutionError as exc:
            return self._failure(str(exc) or "runtime_execution_rejected")
         except Exception:
            return self._failure("runtime_execution_rejected")

   def _decode_input(
      self,
      item: HopWorkItem,
      stage: Stage,
      runtime: Mapping[str, Any],
   ) -> tuple[np.ndarray | None, np.ndarray | None, int]:
      if not isinstance(item.payload, bytes):
         _reject("runtime_payload_must_be_bytes")
      if stage.stage_id == self.graph.entry_stage_id:
         token_ids = decode_token_ids(item.payload)
         if not token_ids:
            _reject("empty_token_sequence")
         config = runtime["model_config"]
         if len(token_ids) > config["n_positions"]:
            _reject("position_bounds_exceeded")
         if any(token_id >= config["vocab_size"] for token_id in token_ids):
            _reject("token_bounds_exceeded")
         if any(
            not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or token_id < 0
            for token_id in token_ids
         ):
            _reject("invalid_token_id", str(token_ids))
         self._validate_sequence_span(item.batch_key, item.phase, len(token_ids))
         ids = np.array((tuple(token_ids),), dtype=np.int64)
         if not np.isfinite(ids).all():
            _reject("invalid_token_id_shape")
         return ids, None, len(token_ids)

      envelope = decode_activation(item.payload)
      if envelope.dtype != runtime["dtype"]:
         _reject("activation_dtype_mismatch")
      if len(envelope.shape) != 3:
         _reject("activation_rank_mismatch")
      if envelope.shape[0] != 1 or envelope.shape[2] != self.graph.hidden_size:
         _reject("activation_shape_mismatch")
      sequence = envelope.shape[1]
      if sequence > runtime["model_config"]["n_positions"]:
         _reject("position_bounds_exceeded")
      self._validate_sequence_span(item.batch_key, item.phase, sequence)
      hidden = np.frombuffer(envelope.data, dtype=_NUMPY_DTYPES[envelope.dtype])
      hidden = hidden.reshape(envelope.shape)
      if not np.isfinite(hidden).all():
         _reject("nonfinite_activation")
      return None, hidden, sequence

   def _runtime_result(
      self,
      stage: Stage,
      runtime: Mapping[str, Any],
      output: np.ndarray,
   ) -> RuntimeResult:
      if stage.stage_id == self.graph.final_stage_id:
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
      contiguous = np.ascontiguousarray(output)
      return RuntimeResult(
         success=True,
         payload=encode_activation(
            dtype=runtime["dtype"],
            shape=tuple(int(value) for value in contiguous.shape),
            data=bytes(contiguous),
         ),
      )

   def _execute_bound(self, item: HopWorkItem) -> RuntimeResult:
      if item.phase == "PREFILL_CHUNK":
         _reject("prefill_chunk_requires_kv_continuity")
      if item.phase not in {"PREFILL", "RECOVERY_PREFILL", "DECODE"}:
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

      binding = self._bound.get(item.placement_id)
      if binding is None:
         _reject("unbound_runtime_placement")
      stage, placement, loaded, runtime = binding
      self._validate_batch_key(item, placement)
      fingerprint = self._operation_fingerprint(item)
      replay = self._replays.get((item.path_id, item.idempotency_key))
      if replay is not None:
         if replay.fingerprint != fingerprint:
            _reject("replay_fingerprint_mismatch")
         return replay.result

      token_ids, hidden_states, sequence = self._decode_input(item, stage, runtime)
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
         if "input_embedding" in stage.component_roles:
            output = _numpy_execute_loaded_stage(loaded, token_ids=token_ids)
         else:
            output = _numpy_execute_loaded_stage(
               loaded, hidden_states=hidden_states
            )
      except _StageNumpyRuntimeError as exc:
         raise NumpyRouterRuntimeError(str(exc)) from exc
      except RuntimeExecutionError as exc:
         raise NumpyRouterRuntimeError(str(exc) or "runtime_execution_rejected") from exc

      if not isinstance(output, np.ndarray):
         _reject("invalid_stage_output_type")
      if not np.isfinite(output).all():
         _reject("nonfinite_stage_output")
      result = self._runtime_result(stage, runtime, output)

      self._applied_operation_count += 1
      self._remember_result(item, fingerprint, result)
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

   @staticmethod
   def _validate_sequence_span(
      key: RuntimeBatchKey | None,
      phase: str,
      sequence: int,
   ) -> None:
      if key is None:
         _reject("missing_runtime_batch_key")
      if phase in {"PREFILL", "RECOVERY_PREFILL"} and key.token_span != sequence:
         _reject("batch_key_token_span_mismatch")
      if phase == "DECODE" and key.token_span != 1:
         _reject("batch_key_token_span_mismatch")

   def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
      """Execute serially in input order; one item cannot poison its siblings."""

      if not isinstance(batch, RuntimeBatch):
         raise NumpyRuntimeError("invalid_runtime_batch")
      return tuple(self.execute(item) for item in batch.items)


_NUMPY_DTYPES = {
   "float16": np.float16,
   "bfloat16": np.float32,  # NumPy lacks native bfloat16; map to float32 bytes.
   "float32": np.float32,
}


# Compatibility spelling for callers that prefer conventional CamelCase.
NumpyRuntimePortCamel = NumpyRuntimePort

__all__ = [
   "NumpyRouterRuntimeError",
   "NumpyRuntimeError",
   "NumpyRuntimePort",
   "NumpyRuntimePortCamel",
]
