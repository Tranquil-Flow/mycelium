# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded standby shadow store for A7 incremental KV checkpoint chains.

A source stage sends its standby one full checkpoint (sequence 0, positions
``[0, n)``) and then incremental checkpoints that each cover only the positions
added since the previous one.  The standby verifies every link before storing
it (A7 specification section 5; implementation contract D2):

* sequence 0 is a full checkpoint with no predecessor;
* each later checkpoint names the previous stored digest as its predecessor,
  advances the sequence by exactly one, starts exactly where the stored state
  ends, and carries the same compatibility identity and source path;
* an exact repeat of the latest checkpoint is idempotent and stores nothing;
* any skip, regression, conflicting repeat, identity or source change, or bound
  overrun refuses and leaves the stored chain unchanged.

The assembled state can be re-encoded as one full checkpoint, byte-identical to
what the source would export at the same position, for import by the successor.
Nothing here is routed, published or qualified; the store only holds bounded
shadow data and reports counts for exact cleanup.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from mycelium_router.kv_checkpoint import (
   KVCheckpointError,
   checkpoint_digest,
   decode_kv_checkpoint,
   encode_kv_checkpoint,
)

SHADOW_RECEIPT_PROTOCOL = "mycelium.a7_standby_shadow_receipt.v1"


class StandbyShadowError(ValueError):
   """A shadow checkpoint was refused; ``code`` is a stable, value-free reason."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


@dataclass
class _Entry:
   identity: dict[str, Any]
   source: dict[str, Any]
   position: dict[str, int]
   layers: dict[int, tuple[np.ndarray, np.ndarray]]
   sequence: int
   last_digest: str
   end: int
   kv_bytes: int
   checkpoint_count: int = 1
   checkpoint_payload_bytes: int = 0


def _layer_bytes(layers: dict[int, tuple[np.ndarray, np.ndarray]]) -> int:
   return sum(int(array.nbytes) for pair in layers.values() for array in pair)


class StandbyShadowStore:
   """Per (request, stage) verified checkpoint chains with hard byte bounds."""

   def __init__(self, *, max_entry_bytes: int, max_entries: int) -> None:
      for value in (max_entry_bytes, max_entries):
         if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise StandbyShadowError("shadow_store_bound_invalid")
      self._max_entry_bytes = max_entry_bytes
      self._max_entries = max_entries
      self._entries: dict[tuple[str, str], _Entry] = {}
      self._last_stored: dict[str, Any] | None = None
      self._lock = threading.RLock()

   def accept(
      self,
      *,
      request_key: str,
      stage_id: str,
      sequence: int,
      predecessor_digest: str | None,
      payload: bytes,
   ) -> dict[str, Any]:
      if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
         raise StandbyShadowError("shadow_sequence_invalid")
      try:
         header, layers = decode_kv_checkpoint(payload, max_bytes=self._max_entry_bytes)
      except KVCheckpointError as exc:
         raise StandbyShadowError(exc.code) from exc
      digest = checkpoint_digest(payload)
      start, end = header["kv_span"]
      key = (request_key, stage_id)
      with self._lock:
         entry = self._entries.get(key)
         if entry is None:
            if sequence != 0 or predecessor_digest is not None:
               raise StandbyShadowError("skipped_predecessor")
            if start != 0:
               raise StandbyShadowError("shadow_first_checkpoint_not_full")
            if len(self._entries) >= self._max_entries:
               raise StandbyShadowError("shadow_store_full")
            entry = _Entry(
               identity=header["identity"],
               source=header["source"],
               position=header["position"],
               layers=layers,
               sequence=0,
               last_digest=digest,
               end=end,
               kv_bytes=_layer_bytes(layers),
               checkpoint_count=1,
               checkpoint_payload_bytes=len(payload),
            )
            if entry.kv_bytes > self._max_entry_bytes:
               raise StandbyShadowError("shadow_entry_too_large")
            self._entries[key] = entry
            self._last_stored = {"request_key": request_key, "checkpoint_sequence": 0, "payload_bytes": len(payload)}
            return self._receipt(entry, digest, "advanced")
         if sequence == entry.sequence:
            if digest == entry.last_digest:
               return self._receipt(entry, digest, "idempotent")
            raise StandbyShadowError("conflicting_repeat")
         if sequence < entry.sequence:
            raise StandbyShadowError("regressed_position")
         if sequence != entry.sequence + 1:
            raise StandbyShadowError("skipped_predecessor")
         if predecessor_digest != entry.last_digest:
            raise StandbyShadowError("predecessor_mismatch")
         if header["identity"] != entry.identity:
            raise StandbyShadowError("shadow_identity_mismatch")
         if header["source"] != entry.source:
            raise StandbyShadowError("shadow_source_mismatch")
         if start != entry.end:
            raise StandbyShadowError("regressed_position" if start < entry.end else "skipped_predecessor")
         merged = {
            layer: (
               np.concatenate((entry.layers[layer][0], pair[0]), axis=2),
               np.concatenate((entry.layers[layer][1], pair[1]), axis=2),
            )
            for layer, pair in layers.items()
         }
         merged_bytes = _layer_bytes(merged)
         if merged_bytes > self._max_entry_bytes:
            raise StandbyShadowError("shadow_entry_too_large")
         entry.layers = merged
         entry.position = header["position"]
         entry.sequence = sequence
         entry.last_digest = digest
         entry.end = end
         entry.kv_bytes = merged_bytes
         entry.checkpoint_count += 1
         entry.checkpoint_payload_bytes += len(payload)
         self._last_stored = {"request_key": request_key, "checkpoint_sequence": sequence, "payload_bytes": len(payload)}
         return self._receipt(entry, digest, "advanced")

   @staticmethod
   def _receipt(entry: _Entry, digest: str, outcome: str) -> dict[str, Any]:
      return {
         "protocol": SHADOW_RECEIPT_PROTOCOL,
         "outcome": outcome,
         "checkpoint_digest": digest,
         "stored_checkpoint_digest_verified": True,
         "checkpoint_sequence": entry.sequence,
         "next_position": entry.end,
         "kv_bytes": entry.kv_bytes,
      }

   def full_checkpoint(self, *, request_key: str, stage_id: str) -> bytes:
      """Re-encode the assembled chain as one full checkpoint for import."""

      with self._lock:
         entry = self._entries.get((request_key, stage_id))
         if entry is None:
            raise StandbyShadowError("shadow_checkpoint_missing")
         return encode_kv_checkpoint(
            identity=entry.identity,
            source=entry.source,
            position=entry.position,
            layers=entry.layers,
            kv_span=(0, entry.end),
         )

   def stage_ids(self, request_key: str) -> tuple[str, ...]:
      """The stages holding a shadow chain for one request."""

      with self._lock:
         return tuple(sorted(stage for key, stage in self._entries if key == request_key))

   def release_request(self, request_key: str) -> int:
      """Drop every shadow entry for one request; returns the bytes released."""

      with self._lock:
         keys = [key for key in self._entries if key[0] == request_key]
         released = sum(self._entries[key].kv_bytes for key in keys)
         for key in keys:
            del self._entries[key]
      return released

   def snapshot(self) -> dict[str, int]:
      with self._lock:
         return {
            "shadow_entry_count": len(self._entries),
            "shadow_kv_bytes": sum(entry.kv_bytes for entry in self._entries.values()),
         }

   def last_stored(self) -> dict[str, Any] | None:
      """The last stored checkpoint's (request key, sequence, payload bytes); kept after release."""

      with self._lock:
         return None if self._last_stored is None else dict(self._last_stored)

   def checkpoint_snapshot(self) -> dict[str, int]:
      """Stored (verified, non-idempotent) checkpoints and their payload bytes, for held chains."""

      with self._lock:
         return {
            "shadow_checkpoint_count": sum(entry.checkpoint_count for entry in self._entries.values()),
            "shadow_checkpoint_payload_bytes": sum(
               entry.checkpoint_payload_bytes for entry in self._entries.values()
            ),
         }
