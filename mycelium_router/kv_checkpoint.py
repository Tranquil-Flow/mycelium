# SPDX-License-Identifier: AGPL-3.0-only
"""Byte-exact stage-local KV checkpoint export/import (A7 successor recovery).

A source stage runtime exports the exact KV it holds for one request path as an
immutable, self-describing payload; a standby runtime for the *same* stage can
import it for the successor path and continue decoding at the next position
without any recovery prefill.

The payload is closed and canonical:

``MAGIC (8 bytes) | header length (u32, little endian) | header | data``

* ``header`` is canonical JSON (sorted keys, compact separators, UTF-8) naming the
  exact compatibility identity (model, revision, manifest, deployment epoch, stage
  layer range and component roles, runtime backend/version/device/dtype/
  quantization/architecture, decode mode), the source path identity, the KV
  position/sequence, and one entry per layer for the key and value arrays
  (dtype, shape, offset, byte count);
* ``data`` is the raw little-endian array bytes in header order.

The payload digest is the SHA-256 of the whole byte string.  Import refuses any
payload whose identity is not exactly the importing stage's identity, whose
arrays are not exactly the shapes the stage's own model configuration implies,
whose position differs from the expected committed position, or whose bytes are
truncated, padded, or otherwise inconsistent: unknown compatibility is
incompatible, never assumed (A7 specification section 3).  The codec carries no
confidentiality; transport sealing is a separate layer.

Raw KV never leaves this module in a receipt: receipts carry only identities,
digests, counts and byte totals.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

KV_CHECKPOINT_PAYLOAD_PROTOCOL = "mycelium.stage_kv_checkpoint_payload.v1"
KV_CHECKPOINT_RECEIPT_PROTOCOL = "mycelium.stage_kv_checkpoint_receipt.v1"
MAGIC = b"MYCKV1\x00\x00"
_HEADER_LENGTH = struct.Struct("<I")
MAX_HEADER_BYTES = 64 * 1024
MAX_LAYERS = 4096
# The only array encodings A7 v1 transfers.  Each is little endian and byte exact.
_DTYPES = {"float32": np.dtype("<f4"), "float16": np.dtype("<f2")}
_HEADER_FIELDS = frozenset(
   {
      "protocol",
      "identity",
      "source",
      "position",
      "kv_span",
      "layers",
      "byte_order",
      "data_bytes",
   }
)
_IDENTITY_FIELDS = frozenset(
   {
      "model_id",
      "resolved_commit",
      "manifest_digest",
      "deployment_id",
      "deployment_epoch",
      "layer_range",
      "component_roles",
      "runtime_identity",
      "decode_mode",
   }
)
_RUNTIME_IDENTITY_FIELDS = frozenset(
   {"backend", "backend_version", "device", "dtype", "quantization", "architecture"}
)
_SOURCE_FIELDS = frozenset(
   {"request_id", "path_id", "path_attempt", "placement_id", "assignment_id"}
)
_POSITION_FIELDS = frozenset({"next_position", "next_sequence", "cached_context_tokens"})
_LAYER_FIELDS = frozenset({"layer", "key", "value"})
_ARRAY_FIELDS = frozenset({"dtype", "shape", "offset", "nbytes"})


class KVCheckpointError(ValueError):
   """A checkpoint was refused; ``code`` is a stable, value-free reason."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _refuse(code: str) -> None:
   raise KVCheckpointError(code)


def _canonical(value: Any) -> bytes:
   return json.dumps(
      value,
      sort_keys=True,
      separators=(",", ":"),
      ensure_ascii=False,
      allow_nan=False,
   ).encode("utf-8")


def _sha256_ref(data: bytes) -> str:
   return "sha256:" + hashlib.sha256(data).hexdigest()


def _non_negative_int(value: Any) -> bool:
   return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _text(value: Any) -> bool:
   return isinstance(value, str) and bool(value) and value == value.strip()


def encode_kv_checkpoint(
   *,
   identity: Mapping[str, Any],
   source: Mapping[str, Any],
   position: Mapping[str, int],
   layers: Mapping[int, tuple[np.ndarray, np.ndarray]],
   kv_span: tuple[int, int] | None = None,
) -> bytes:
   """Encode exact KV arrays plus their identity into one canonical payload.

   ``kv_span`` is the half-open position range the arrays cover.  A full
   checkpoint covers ``[0, next_position)``; an incremental one covers only the
   positions added since the previous checkpoint in its chain.
   """

   entries: list[dict[str, Any]] = []
   chunks: list[bytes] = []
   offset = 0
   for layer in sorted(layers):
      pair = layers[layer]
      described: dict[str, Any] = {"layer": int(layer)}
      for name, array in zip(("key", "value"), pair):
         array = np.asarray(array)
         dtype_name = (
            {4: "float32", 2: "float16"}.get(array.dtype.itemsize)
            if array.dtype.kind == "f"
            else None
         )
         if dtype_name is None:
            _refuse("kv_checkpoint_dtype_unsupported")
         data = np.ascontiguousarray(array, dtype=_DTYPES[dtype_name]).tobytes()
         described[name] = {
            "dtype": dtype_name,
            "shape": [int(dimension) for dimension in array.shape],
            "offset": offset,
            "nbytes": len(data),
         }
         chunks.append(data)
         offset += len(data)
      entries.append(described)
   header = {
      "protocol": KV_CHECKPOINT_PAYLOAD_PROTOCOL,
      "identity": dict(identity),
      "source": dict(source),
      "position": dict(position),
      "kv_span": list(kv_span) if kv_span is not None else [0, int(position["next_position"])],
      "layers": entries,
      "byte_order": "little",
      "data_bytes": offset,
   }
   _validate_header(header)
   encoded = _canonical(header)
   if len(encoded) > MAX_HEADER_BYTES:
      _refuse("kv_checkpoint_header_too_large")
   return MAGIC + _HEADER_LENGTH.pack(len(encoded)) + encoded + b"".join(chunks)


def _validate_header(header: Any) -> None:
   if not isinstance(header, dict) or frozenset(header) != _HEADER_FIELDS:
      _refuse("kv_checkpoint_header_invalid")
   if header["protocol"] != KV_CHECKPOINT_PAYLOAD_PROTOCOL:
      _refuse("kv_checkpoint_protocol_invalid")
   if header["byte_order"] != "little":
      _refuse("kv_checkpoint_byte_order_invalid")
   identity = header["identity"]
   if not isinstance(identity, dict) or frozenset(identity) != _IDENTITY_FIELDS:
      _refuse("kv_checkpoint_identity_invalid")
   runtime_identity = identity["runtime_identity"]
   if (
      not isinstance(runtime_identity, dict)
      or frozenset(runtime_identity) != _RUNTIME_IDENTITY_FIELDS
      or not all(_text(runtime_identity[field]) for field in _RUNTIME_IDENTITY_FIELDS)
   ):
      _refuse("kv_checkpoint_identity_invalid")
   for field in ("model_id", "resolved_commit", "manifest_digest", "deployment_id", "decode_mode"):
      if not _text(identity[field]):
         _refuse("kv_checkpoint_identity_invalid")
   if not _non_negative_int(identity["deployment_epoch"]):
      _refuse("kv_checkpoint_identity_invalid")
   layer_range = identity["layer_range"]
   if (
      not isinstance(layer_range, list)
      or len(layer_range) != 2
      or not all(_non_negative_int(bound) for bound in layer_range)
      or not layer_range[0] < layer_range[1] <= MAX_LAYERS
   ):
      _refuse("kv_checkpoint_identity_invalid")
   roles = identity["component_roles"]
   if not isinstance(roles, list) or not roles or not all(_text(role) for role in roles):
      _refuse("kv_checkpoint_identity_invalid")
   source = header["source"]
   if not isinstance(source, dict) or frozenset(source) != _SOURCE_FIELDS:
      _refuse("kv_checkpoint_source_invalid")
   for field in ("request_id", "path_id", "placement_id", "assignment_id"):
      if not _text(source[field]):
         _refuse("kv_checkpoint_source_invalid")
   if not _non_negative_int(source["path_attempt"]):
      _refuse("kv_checkpoint_source_invalid")
   position = header["position"]
   if (
      not isinstance(position, dict)
      or frozenset(position) != _POSITION_FIELDS
      or not all(_non_negative_int(position[field]) for field in _POSITION_FIELDS)
      or position["next_position"] < 1
      or position["cached_context_tokens"] != position["next_position"]
   ):
      _refuse("kv_checkpoint_position_invalid")
   span = header["kv_span"]
   if (
      not isinstance(span, list)
      or len(span) != 2
      or not all(_non_negative_int(bound) for bound in span)
      or not span[0] < span[1] == position["next_position"]
   ):
      _refuse("kv_checkpoint_span_invalid")
   layers = header["layers"]
   if not isinstance(layers, list) or not layers:
      _refuse("kv_checkpoint_layers_invalid")
   expected_layers = list(range(layer_range[0], layer_range[1]))
   if [entry.get("layer") if isinstance(entry, dict) else None for entry in layers] != expected_layers:
      _refuse("kv_checkpoint_layers_invalid")
   cursor = 0
   for entry in layers:
      if frozenset(entry) != _LAYER_FIELDS:
         _refuse("kv_checkpoint_layers_invalid")
      for name in ("key", "value"):
         array = entry[name]
         if not isinstance(array, dict) or frozenset(array) != _ARRAY_FIELDS:
            _refuse("kv_checkpoint_array_invalid")
         if array["dtype"] not in _DTYPES:
            _refuse("kv_checkpoint_dtype_unsupported")
         shape = array["shape"]
         if (
            not isinstance(shape, list)
            or len(shape) != 4
            or not all(_non_negative_int(dimension) and dimension > 0 for dimension in shape)
         ):
            _refuse("kv_checkpoint_array_invalid")
         if shape[2] != span[1] - span[0]:
            _refuse("kv_checkpoint_span_invalid")
         expected_bytes = math.prod(shape) * _DTYPES[array["dtype"]].itemsize
         if array["offset"] != cursor or array["nbytes"] != expected_bytes:
            _refuse("kv_checkpoint_array_invalid")
         cursor += expected_bytes
   if header["data_bytes"] != cursor:
      _refuse("kv_checkpoint_array_invalid")


def decode_kv_checkpoint(
   payload: bytes, *, max_bytes: int
) -> tuple[dict[str, Any], dict[int, tuple[np.ndarray, np.ndarray]]]:
   """Decode a payload into its validated header and fresh, owned arrays."""

   if not isinstance(payload, (bytes, bytearray)):
      _refuse("kv_checkpoint_payload_invalid")
   if not _non_negative_int(max_bytes) or max_bytes < 1:
      _refuse("kv_checkpoint_bound_invalid")
   if len(payload) > max_bytes:
      _refuse("kv_checkpoint_too_large")
   prefix = len(MAGIC) + _HEADER_LENGTH.size
   if len(payload) < prefix or bytes(payload[: len(MAGIC)]) != MAGIC:
      _refuse("kv_checkpoint_magic_invalid")
   (header_length,) = _HEADER_LENGTH.unpack_from(payload, len(MAGIC))
   if header_length < 2 or header_length > MAX_HEADER_BYTES or prefix + header_length > len(payload):
      _refuse("kv_checkpoint_header_invalid")
   raw_header = bytes(payload[prefix : prefix + header_length])
   try:
      header = json.loads(raw_header.decode("utf-8"))
   except (UnicodeDecodeError, ValueError):
      _refuse("kv_checkpoint_header_invalid")
   if _canonical(header) != raw_header:
      _refuse("kv_checkpoint_header_not_canonical")
   _validate_header(header)
   data = memoryview(payload)[prefix + header_length :]
   if len(data) != header["data_bytes"]:
      _refuse("kv_checkpoint_data_length_mismatch")
   layers: dict[int, tuple[np.ndarray, np.ndarray]] = {}
   for entry in header["layers"]:
      pair = []
      for name in ("key", "value"):
         spec = entry[name]
         chunk = data[spec["offset"] : spec["offset"] + spec["nbytes"]]
         array = np.frombuffer(chunk, dtype=_DTYPES[spec["dtype"]]).reshape(spec["shape"]).copy()
         if not np.isfinite(array).all():
            _refuse("kv_checkpoint_nonfinite")
         pair.append(array.astype(array.dtype.newbyteorder("="), copy=False))
      layers[int(entry["layer"])] = (pair[0], pair[1])
   return header, layers


def checkpoint_digest(payload: bytes) -> str:
   return _sha256_ref(bytes(payload))


# ---------------------------------------------------------------------------
# Runtime-port integration.  Both NumPy and MLX stage runtime ports keep their
# per-path KV in ``_kv_states`` under ``_state_lock`` with identical field names;
# these helpers operate on that shared shape so the two ports stay in lockstep.
# ---------------------------------------------------------------------------


def _stage_identity(port: Any, placement_id: str) -> dict[str, Any]:
   binding = port._bound.get(placement_id)
   if binding is None:
      _refuse("kv_checkpoint_placement_unbound")
   stage, _placement, loaded, runtime = binding
   proof_identity = loaded.proof.get("runtime_identity") if isinstance(loaded.proof, Mapping) else None
   if not isinstance(proof_identity, Mapping):
      _refuse("kv_checkpoint_runtime_identity_unknown")
   return {
      "model_id": port.graph.model_id,
      "resolved_commit": port.graph.resolved_commit,
      "manifest_digest": port.graph.manifest_digest,
      "deployment_id": port.graph.deployment_id,
      "deployment_epoch": port.graph.deployment_epoch,
      "layer_range": [
         stage.layer_range.start_layer,
         stage.layer_range.end_layer_exclusive,
      ],
      "component_roles": list(stage.component_roles),
      "runtime_identity": {field: str(proof_identity.get(field, "")) for field in sorted(_RUNTIME_IDENTITY_FIELDS)},
      "decode_mode": port.decode_mode,
   }


def stage_kv_identity(port: Any, placement_id: str) -> dict[str, Any]:
   """The exact KV compatibility identity a port reports for a bound placement."""

   return _stage_identity(port, placement_id)


def _expected_kv_heads_and_dim(runtime: Mapping[str, Any]) -> tuple[int, int]:
   config = runtime.get("model_config") if isinstance(runtime, Mapping) else None
   if not isinstance(config, Mapping):
      _refuse("kv_checkpoint_model_config_unknown")
   n_head = config.get("n_head")
   heads = config.get("n_kv_head", n_head)
   head_dim = config.get("head_dim")
   if head_dim is None and _non_negative_int(config.get("n_embd")) and _non_negative_int(n_head) and n_head:
      head_dim = config["n_embd"] // n_head
   if not (_non_negative_int(heads) and heads and _non_negative_int(head_dim) and head_dim):
      _refuse("kv_checkpoint_model_config_unknown")
   return int(heads), int(head_dim)


def export_port_kv_checkpoint(
   port: Any,
   path_id: str,
   *,
   to_numpy: Callable[[Any], np.ndarray],
   from_position: int = 0,
) -> bytes:
   """Export the exact KV this port holds for ``path_id`` (no mutation).

   ``from_position > 0`` exports only the positions added since then, as an
   incremental checkpoint for a standby that already holds the earlier ones.
   """

   if port.decode_mode != "stage_local_kv":
      _refuse("kv_checkpoint_requires_stage_local_kv")
   with port._state_lock:
      if port._closed:
         _refuse("kv_checkpoint_runtime_closed")
      state = port._kv_states.get(path_id)
      if state is None:
         _refuse("kv_checkpoint_state_missing")
      identity = _stage_identity(port, state.placement_id)
      if not _non_negative_int(from_position) or from_position >= state.next_position:
         _refuse("kv_checkpoint_span_invalid")
      layers = {
         int(layer): (
            np.ascontiguousarray(to_numpy(pair[0])[:, :, from_position:, :]),
            np.ascontiguousarray(to_numpy(pair[1])[:, :, from_position:, :]),
         )
         for layer, pair in state.layers.items()
      }
      span = (from_position, state.next_position)
      source = {
         "request_id": state.request_id,
         "path_id": state.path_id,
         "path_attempt": state.path_attempt,
         "placement_id": state.placement_id,
         "assignment_id": state.assignment_id,
      }
      position = {
         "next_position": state.next_position,
         "next_sequence": state.next_sequence,
         "cached_context_tokens": state.cached_context_tokens,
      }
   return encode_kv_checkpoint(
      identity=identity, source=source, position=position, layers=layers, kv_span=span
   )


def import_port_kv_checkpoint(
   port: Any,
   payload: bytes,
   *,
   request_id: str,
   path_id: str,
   path_attempt: int,
   placement_id: str,
   lease_expires_at: float,
   expected_next_position: int,
   max_bytes: int,
   from_numpy: Callable[[np.ndarray], Any],
   state_factory: Callable[..., Any],
) -> dict[str, Any]:
   """Install an exactly compatible checkpoint as the successor path's KV.

   Returns a privacy-reduced receipt.  Nothing is installed unless every check
   passes; a refusal leaves the port unchanged.
   """

   if port.decode_mode != "stage_local_kv":
      _refuse("kv_checkpoint_requires_stage_local_kv")
   if not _text(request_id) or not _text(path_id) or not _text(placement_id):
      _refuse("kv_checkpoint_successor_invalid")
   if not _non_negative_int(path_attempt) or not _non_negative_int(expected_next_position):
      _refuse("kv_checkpoint_successor_invalid")
   if (
      not isinstance(lease_expires_at, (int, float))
      or isinstance(lease_expires_at, bool)
      or not math.isfinite(float(lease_expires_at))
   ):
      _refuse("kv_checkpoint_successor_invalid")
   header, layers = decode_kv_checkpoint(payload, max_bytes=max_bytes)
   if header["kv_span"][0] != 0:
      _refuse("kv_checkpoint_not_full")
   expected_identity = _stage_identity(port, placement_id)
   for field in sorted(_IDENTITY_FIELDS):
      if header["identity"][field] != expected_identity[field]:
         _refuse(f"kv_checkpoint_{field}_incompatible")
   source = header["source"]
   if source["request_id"] != request_id:
      _refuse("kv_checkpoint_request_mismatch")
   if path_attempt <= source["path_attempt"] or path_id == source["path_id"]:
      _refuse("kv_checkpoint_successor_not_newer")
   position = header["position"]
   if position["next_position"] != expected_next_position:
      _refuse("kv_checkpoint_position_mismatch")
   _stage, placement, _loaded, runtime = port._bound[placement_id]
   heads, head_dim = _expected_kv_heads_and_dim(runtime)
   runtime_dtype = str(expected_identity["runtime_identity"]["dtype"])
   for entry in header["layers"]:
      for name in ("key", "value"):
         spec = entry[name]
         if spec["dtype"] != runtime_dtype:
            _refuse("kv_checkpoint_dtype_incompatible")
         if spec["shape"] != [1, heads, position["next_position"], head_dim]:
            _refuse("kv_checkpoint_shape_incompatible")
   with port._state_lock:
      if port._closed:
         _refuse("kv_checkpoint_runtime_closed")
      with port._cancellation_lock:
         if path_id in port._cancelled_paths:
            _refuse("kv_checkpoint_path_cancelled")
      if path_id in port._kv_states:
         _refuse("kv_checkpoint_state_already_exists")
      if port._released_paths.get(path_id) is not None:
         _refuse("kv_checkpoint_path_released")
      if float(port._clock()) >= float(lease_expires_at):
         _refuse("kv_checkpoint_lease_expired")
      port._kv_states[path_id] = state_factory(
         request_id=request_id,
         path_id=path_id,
         path_attempt=path_attempt,
         placement_id=placement_id,
         assignment_id=placement.assignment_id,
         manifest_digest=port.graph.manifest_digest,
         deployment_epoch=port.graph.deployment_epoch,
         lease_expires_at=float(lease_expires_at),
         next_position=position["next_position"],
         next_sequence=position["next_sequence"],
         cached_context_tokens=position["cached_context_tokens"],
         layers={
            layer: (from_numpy(pair[0]), from_numpy(pair[1]))
            for layer, pair in layers.items()
         },
      )
      with port._resource_index_lock:
         port._kv_subjects[path_id] = (request_id, path_attempt)
      port._update_kv_watermark()
   return {
      "protocol": KV_CHECKPOINT_RECEIPT_PROTOCOL,
      "checkpoint_digest": checkpoint_digest(payload),
      "identity_digest": _sha256_ref(_canonical(header["identity"])),
      "layer_count": len(layers),
      "kv_bytes": int(header["data_bytes"]),
      "next_position": position["next_position"],
      "next_sequence": position["next_sequence"],
      "successor_path_attempt": path_attempt,
   }


def rekey_port_kv_path(
   port: Any,
   old_path_id: str,
   *,
   new_path_id: str,
   new_path_attempt: int,
   lease_expires_at: float,
   truncate_to_position: int | None = None,
   request_id: str | None = None,
   placement_id: str | None = None,
   allow_terminal_retained: bool = False,
) -> dict[str, Any]:
   """Move a surviving stage's KV from a fenced path to its successor path.

   The committed KV bytes do not change.  The old path is marked released, so
   any frame that still arrives for the fenced attempt refuses with
   ``kv_state_released``: this is how a surviving stage runtime installs the A7
   fence.

   ``truncate_to_position`` rolls back the one in-flight, uncommitted decode
   position a surviving stage may already have computed when a later stage
   failed (the entry dispatches a decode only after the previous token is
   committed and acknowledged, so at most one position can be ahead).  Earlier
   positions are causal and unaffected by a later one, so the truncated prefix
   is byte-identical to the committed KV.
   """

   if port.decode_mode != "stage_local_kv":
      _refuse("kv_checkpoint_requires_stage_local_kv")
   if not _text(new_path_id) or not _non_negative_int(new_path_attempt):
      _refuse("kv_checkpoint_successor_invalid")
   if (
      not isinstance(lease_expires_at, (int, float))
      or isinstance(lease_expires_at, bool)
      or not math.isfinite(float(lease_expires_at))
   ):
      _refuse("kv_checkpoint_successor_invalid")
   with port._state_lock:
      if port._closed:
         _refuse("kv_checkpoint_runtime_closed")
      state = port._kv_states.get(old_path_id)
      retained = None
      if state is None:
         # A stage that already executed the request's terminal hop (the
         # source was lost during the final decode step) set its KV aside for
         # exactly this rekey.  Only an authenticated A7 cutover from the
         # path's entry, naming the same request and placement, may take it;
         # its own deadline and lease, and any cancellation or terminal
         # release of the path, are enforced at take time.
         take = getattr(port, "_take_terminal_retained", None)
         if allow_terminal_retained is True and _text(request_id) and _text(placement_id) and callable(take):
            retained = take(old_path_id, request_id=request_id, placement_id=placement_id)
         if retained is None:
            _refuse("kv_checkpoint_state_missing")
         state = retained
         port._kv_states[old_path_id] = state
      elif (request_id is not None and request_id != state.request_id) or (
         placement_id is not None and placement_id != state.placement_id
      ):
         _refuse("kv_checkpoint_subject_mismatch")
      try:
         return _rekey_locked(
            port,
            state,
            old_path_id,
            new_path_id=new_path_id,
            new_path_attempt=new_path_attempt,
            lease_expires_at=lease_expires_at,
            truncate_to_position=truncate_to_position,
         )
      except KVCheckpointError:
         if retained is not None and port._kv_states.get(old_path_id) is retained:
            del port._kv_states[old_path_id]
            port._restore_terminal_retained(old_path_id, retained)
         raise


def _rekey_locked(
   port: Any,
   state: Any,
   old_path_id: str,
   *,
   new_path_id: str,
   new_path_attempt: int,
   lease_expires_at: float,
   truncate_to_position: int | None,
) -> dict[str, Any]:
   if new_path_id == old_path_id or new_path_attempt <= state.path_attempt:
      _refuse("kv_checkpoint_successor_not_newer")
   if new_path_id in port._kv_states or port._released_paths.get(new_path_id) is not None:
      _refuse("kv_checkpoint_state_already_exists")
   if float(port._clock()) >= float(lease_expires_at):
      _refuse("kv_checkpoint_lease_expired")
   if truncate_to_position is not None:
      if not _non_negative_int(truncate_to_position) or truncate_to_position < 1:
         _refuse("kv_checkpoint_position_mismatch")
      rollback = state.next_position - truncate_to_position
      if rollback not in (0, 1) or state.next_sequence - rollback < 1:
         _refuse("kv_checkpoint_position_mismatch")
      if rollback:
         def _prefix(array: Any) -> Any:
            sliced = array[:, :, :truncate_to_position, :]
            return np.ascontiguousarray(sliced) if isinstance(array, np.ndarray) else sliced

         state.layers = {
            layer: (_prefix(pair[0]), _prefix(pair[1])) for layer, pair in state.layers.items()
         }
         state.next_position -= rollback
         state.next_sequence -= rollback
         state.cached_context_tokens -= rollback
   del port._kv_states[old_path_id]
   state.path_id = new_path_id
   state.path_attempt = new_path_attempt
   state.lease_expires_at = float(lease_expires_at)
   port._kv_states[new_path_id] = state
   with port._resource_index_lock:
      port._kv_subjects.pop(old_path_id, None)
      port._kv_subjects[new_path_id] = (state.request_id, new_path_attempt)
   port._remember_path_marker(port._released_paths, old_path_id, "fenced_successor_rekey")
   port._purge_path_replays(old_path_id)
   return {
      "protocol": KV_CHECKPOINT_RECEIPT_PROTOCOL,
      "checkpoint_digest": None,
      "identity_digest": _sha256_ref(_canonical(_stage_identity(port, state.placement_id))),
      "layer_count": len(state.layers),
      "kv_bytes": int(port._kv_bytes(state)),
      "next_position": state.next_position,
      "next_sequence": state.next_sequence,
      "successor_path_attempt": new_path_attempt,
   }
