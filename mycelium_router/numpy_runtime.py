"""Assignment- and graph-bound NumPy implementation of the Router RuntimePort.

The port executes one authenticated stage at a time using only NumPy (no MLX,
CUDA, or MKL claims). It supports complete-context replay for every qualified
architecture and stage-local KV for dense Qwen2/Qwen3. Every binding (graph,
load proof, placement, batch key, payload, cache position, and lease) is
revalidated against loader-held evidence before execution.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Condition, RLock, Thread
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
   _qwen2_block_with_kv,
   _qwen2_embedding,
   _qwen2_linear_checkpointed,
   _rms_norm,
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
   layers: dict[int, tuple[np.ndarray, np.ndarray]]


def _numpy_identity_fields() -> tuple[str, ...]:
   return ("backend", "backend_version", "device", "dtype", "quantization", "architecture")


def _numpy_runtime_identity(runtime: Mapping[str, Any]) -> Mapping[str, Any]:
   return {
      "backend": "numpy",
      "backend_version": importlib.metadata.version("numpy"),
      "device": "cpu",
      "dtype": runtime["dtype"],
      "quantization": runtime["quantization"],
      "architecture": runtime["architecture"],
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

   Dense Qwen2/Qwen3 placements may use ``stage_local_kv``. Other qualified
   architectures remain ``complete_context_replay`` only.
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
      decode_mode: str = "complete_context_replay",
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

      if (
         decode_mode == "stage_local_kv"
         and canonical_runtime["architecture"] not in {"qwen2", "qwen3"}
      ):
         _reject("stage_local_kv_unsupported_architecture")

      self.node_id = node_id
      self.graph = graph
      self.decode_mode = decode_mode
      self._architecture = canonical_runtime["architecture"]
      self._bound = MappingProxyType(bound)
      if clock is not None and not callable(clock):
         _reject("invalid_runtime_clock")
      self._clock = clock or (importlib.import_module("time").monotonic)
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
      # Cleanup proof is request/path scoped. It must remain observable while
      # an unrelated stage operation owns the heavyweight state lock.
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
      canonical_identity = _numpy_runtime_identity(runtime)
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
      """Idempotently reject future work and release path-local KV state."""

      if not isinstance(path_id, str):
         return
      with self._cancellation_condition:
         self._remember_path_marker(self._cancelled_paths, path_id, None)
         self._deferred_cancellation_cleanup.add(path_id)
         self._cancellation_pending += 1
      cleanup_deferred = True
      try:
         # The marker above is the cancellation fence.  Do not wait for the
         # admission lock here: a different execution may hold it while
         # queued behind the stage state lock, which would make cancellation
         # wait for an unrelated whole-stage operation.  If admission is
         # currently free, opportunistically reclaim the state now; otherwise
         # the admitted operation observes the marker at its next checkpoint
         # and its finally block performs the exact release.
         admission_acquired = self._execution_admission_lock.acquire(
            blocking=False
         )
         if admission_acquired:
            try:
               cleanup_now = self._state_lock.acquire(blocking=False)
               if cleanup_now:
                  try:
                     if not self._release_state(path_id, "cancelled"):
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
      """Finish fenced cleanup even when no execution finalizer remains.

      A health or cleanup snapshot can briefly own ``_state_lock`` after the
      last stage operation has already returned.  Cancellation must stay
      nonblocking, but merely adding the path to the deferred set strands its
      KV forever because no later execute-finally handoff is guaranteed.  One
      bounded daemon worker waits for the ordinary admission/state ordering
      and consumes the already-authorized cancellation; proof snapshots remain
      observers of the resulting absence rather than cleanup authority.
      """

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
                        if not self._release_state(path_id, "cancelled"):
                           self._purge_path_replays(path_id)
                        self._deferred_cancellation_cleanup.discard(path_id)
         finally:
            with self._cancellation_condition:
               self._deferred_cleanup_worker_paths.discard(path_id)
               self._cancellation_condition.notify_all()

      worker = Thread(
         target=finish,
         name=f"mycelium-numpy-cancel-cleanup-{path_id[:12]}",
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
               # A queued stage operation may wait here for the active one,
               # but it must never retain the cancellation mutex while doing
               # so. The active operation reaches checkpoints under that
               # mutex; holding it here would invert the two locks and strand
               # both executions plus every subsequent cancellation.
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
               # Cancellation may linearize while this execution already owns
               # admission but is waiting for stage state. Yield both locks so
               # the registered cleanup worker can reclaim the fenced subject
               # before unrelated model work starts another whole operation.
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
      """Release every active path whose reservation lease has expired."""

      current = self._clock() if now is None else now
      if (
         not isinstance(current, (int, float))
         or isinstance(current, bool)
         or not math.isfinite(float(current))
      ):
         raise NumpyRuntimeError("invalid_lease_expiry_time")
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
         raise NumpyRuntimeError("invalid_runtime_close_reason")
      with self._state_lock:
         if self._closed:
            return
         released_any = bool(self._kv_states)
         for path_id in tuple(self._kv_states):
            self._release_state(path_id, reason)
         self._replays.clear()
         self._closed = True
         if not released_any:
            self._release_counts[reason] += 1
            self._last_release_reason = reason

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
      """Return identity/lifecycle evidence without KV tensor values."""

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
            "backend": "numpy",
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
            "cancelled_path_count": len(self._cancelled_paths),
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
      """Execute one fail-closed, path-serialized complete-context-replay call."""

      if not isinstance(item, HopWorkItem):
         return self._failure("invalid_runtime_work_item")
      try:
         self._acquire_execution_state(item)
      except NumpyRouterRuntimeError as exc:
         return self._failure(exc.code)
      try:
         if self._closed:
            return self._failure("runtime_closed")
         try:
            self._checkpoint_path(item.path_id)
         except NumpyRouterRuntimeError as exc:
            return self._failure(exc.code)
         try:
            self.expire_leases()
            return self._execute_bound(item)
         except PayloadError as exc:
            return self._failure(exc.code)
         except NumpyRouterRuntimeError as exc:
            return self._failure(exc.code)
         except RuntimeExecutionError as exc:
            return self._failure(str(exc) or "runtime_execution_rejected")
         except Exception:
            return self._failure("runtime_execution_rejected")
      finally:
         # One stage lock serializes all path-local KV state.  Cancellations
         # for inactive paths may have fenced their future work while this
         # unrelated operation held that lock, so drain every deferred path
         # before publishing the lock as free.  Holding the cancellation lock
         # through state-lock release closes the race where a new cancellation
         # could otherwise miss both the active finalizer and its own
         # opportunistic cleanup attempt.
         with self._cancellation_condition:
            for cancelled_path in tuple(
               self._deferred_cancellation_cleanup
            ):
               if not self._release_state(cancelled_path, "cancelled"):
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
   ) -> tuple[np.ndarray | None, np.ndarray | None, int]:
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
      if (
         sequence > runtime["model_config"]["n_positions"]
         if self.decode_mode == "complete_context_replay"
         else item.position + sequence > runtime["model_config"]["n_positions"]
      ):
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
      contiguous = np.ascontiguousarray(output)
      return RuntimeResult(
         success=True,
         payload=encode_activation(
            dtype=runtime["dtype"],
            shape=tuple(int(value) for value in contiguous.shape),
            data=bytes(contiguous),
         ),
      )

   def _execute_stage_with_kv(
      self,
      *,
      stage: Stage,
      loaded: LoadedStage,
      runtime: Mapping[str, Any],
      token_ids: np.ndarray | None,
      hidden_states: np.ndarray | None,
      position: int,
      past_layers: Mapping[int, tuple[np.ndarray, np.ndarray]],
      path_id: str,
      produce_logits: bool = True,
   ) -> tuple[np.ndarray, dict[int, tuple[np.ndarray, np.ndarray]]]:
      if runtime["architecture"] not in {"qwen2", "qwen3"}:
         _reject("stage_local_kv_unsupported_architecture")
      config = runtime["model_config"]
      tensors = loaded.tensors
      if "input_embedding" in stage.component_roles:
         if token_ids is None or hidden_states is not None:
            _reject("entry_stage_requires_token_ids")
         hidden = _qwen2_embedding(tensors["model.embed_tokens.weight"], token_ids)
      else:
         if hidden_states is None or token_ids is not None:
            _reject("non_entry_stage_requires_hidden_states")
         hidden = hidden_states

      next_layers: dict[int, tuple[np.ndarray, np.ndarray]] = {}
      for layer in range(
         stage.layer_range.start_layer,
         stage.layer_range.end_layer_exclusive,
      ):
         self._checkpoint_path(path_id)
         work_unit_started = time.monotonic()

         def sublayer_checkpoint() -> None:
            nonlocal work_unit_started
            self._observe_cooperative_work_unit(work_unit_started)
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
            checkpoint=sublayer_checkpoint,
         )
         next_layers[layer] = tuple(
            np.ascontiguousarray(array) for array in layer_kv
         )
         self._observe_cooperative_work_unit(work_unit_started)
         self._checkpoint_path(path_id)
      # Decoder/KV construction covers the complete chunk, while
      # autoregressive sampling consumes logits only for its final position.
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
         hidden = _qwen2_linear_checkpointed(
            hidden,
            tensors[head_keys[0]],
            checkpoint=sublayer_checkpoint,
         )
         self._observe_cooperative_work_unit(work_unit_started)
         self._checkpoint_path(path_id)
      output = np.ascontiguousarray(hidden, dtype=np.dtype(runtime["dtype"]))
      if not np.isfinite(output).all():
         _reject("nonfinite_stage_output")
      return output, next_layers

   def _observe_cooperative_work_unit(self, started_at: float) -> None:
      elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1_000.0)
      self._observed_work_unit_count += 1
      self._maximum_observed_work_unit_ms = max(
         self._maximum_observed_work_unit_ms,
         elapsed_ms,
      )

   def _execute_complete_context(
      self,
      item: HopWorkItem,
      stage: Stage,
      loaded: LoadedStage,
      runtime: Mapping[str, Any],
      token_ids: np.ndarray | None,
      hidden_states: np.ndarray | None,
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
            # Complete-context replay still needs the same per-layer and
            # sublayer cancellation checkpoints as stage-local KV execution.
            # Recompute from position zero with an empty KV map, then discard
            # the temporary cache after producing this command's result.
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
            output = _numpy_execute_loaded_stage(loaded, token_ids=token_ids)
         else:
            output = _numpy_execute_loaded_stage(
               loaded, hidden_states=hidden_states
            )
      except _StageNumpyRuntimeError as exc:
         raise NumpyRouterRuntimeError(str(exc)) from exc
      except RuntimeExecutionError as exc:
         raise NumpyRouterRuntimeError(
            str(exc) or "runtime_execution_rejected"
         ) from exc
      if not isinstance(output, np.ndarray):
         _reject("invalid_stage_output_type")
      if not np.isfinite(output).all():
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
      if self.decode_mode == "stage_local_kv":
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
            _reject(
               "kv_sequence_replay_conflict"
               if self.decode_mode == "stage_local_kv"
               else "replay_fingerprint_mismatch"
            )
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
            self._remember_path_marker(
               self._released_paths, item.path_id, "lease_expired"
            )
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
         self._kv_states[item.path_id] = _KVState(
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
