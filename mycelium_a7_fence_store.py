"""Durable A7 fence generations, single-publication CAS and restart reconciliation.

The A7 specification (sections 6 and 8) requires that the request attempt, path
generation and fencing generation advance *durably* before any successor decode,
that exactly one generation may ever publish, and that after a coordinator restart
every recovery attempt reconciles to exactly one of ``shadow_restored``,
``fallback_required``, ``released``, ``aborted`` or ``already_terminal``.

This store keeps one canonical, self-digested record per request in an owner-only
directory.  Every mutation is a compare-and-swap under an exclusive lock and is
written with write-to-temporary, ``fsync``, atomic rename and directory ``fsync``,
so a completed call survives a crash.  A record that fails validation is moved
aside to quarantine and never trusted; the request then has no recoverable
authority and reconciles to fallback or abort, never to readiness.

Raw request identifiers are never written: records are keyed by a digest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DURABLE_FENCE_RECORD_PROTOCOL = "mycelium.a7_durable_fence_record.v1"
DURABLE_FENCE_RECEIPT_PROTOCOL = "mycelium.a7_durable_fence_receipt.v1"
RECONCILIATION_PROTOCOL = "mycelium.a7_fence_reconciliation.v1"
DURABILITY = "fsync_atomic_rename"
TERMINAL_OUTCOMES = frozenset(
   {"fenced_kv_successor", "full_context_replay", "aborted", "cancelled", "completed", "failed"}
)
RECONCILED_STATES = (
   "shadow_restored",
   "fallback_required",
   "released",
   "aborted",
   "already_terminal",
)
_RECORD_FIELDS = frozenset(
   {
      "protocol",
      "request_key",
      "request_attempt",
      "path_generation",
      "fencing_generation",
      "published_generation",
      "terminal_outcome",
      "record_sequence",
      "previous_digest",
      "evidence_digest",
   }
)


class FenceStoreError(ValueError):
   """A fence operation was refused; ``code`` is a stable, value-free reason."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _canonical(value: Any) -> bytes:
   return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
   return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def request_key(request_id: str) -> str:
   if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
      raise FenceStoreError("fence_request_id_invalid")
   return hashlib.sha256(("mycelium.a7.fence:" + request_id).encode("utf-8")).hexdigest()


def _positive(value: Any) -> bool:
   return isinstance(value, int) and not isinstance(value, bool) and value >= 1


class DurableFenceStore:
   """Owner-only, crash-safe fence ledger for one coordinator."""

   def __init__(self, root: Path | str) -> None:
      self._root = Path(root)
      self._lock = threading.RLock()
      self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
      info = os.lstat(self._root)
      if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
         raise FenceStoreError("fence_store_root_invalid")
      if info.st_uid != os.geteuid():
         raise FenceStoreError("fence_store_root_not_owned")
      os.chmod(self._root, 0o700)
      (self._root / "quarantine").mkdir(mode=0o700, exist_ok=True)

   # -- storage --------------------------------------------------------------

   def _path(self, key: str) -> Path:
      return self._root / f"{key}.json"

   @contextmanager
   def _exclusive(self) -> Iterator[None]:
      with self._lock:
         descriptor = os.open(self._root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
         try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
         finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

   def _read(self, key: str) -> dict[str, Any] | None:
      path = self._path(key)
      try:
         info = os.lstat(path)
      except FileNotFoundError:
         return None
      try:
         if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FenceStoreError("fence_record_invalid")
         if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise FenceStoreError("fence_record_permissions_invalid")
         raw = path.read_bytes()
         record = json.loads(raw.decode("utf-8"))
         self._validate(record, key)
         if _canonical(record) != raw:
            raise FenceStoreError("fence_record_not_canonical")
         return record
      except (FenceStoreError, UnicodeDecodeError, ValueError) as exc:
         self._quarantine(path)
         code = exc.code if isinstance(exc, FenceStoreError) else "fence_record_invalid"
         raise FenceStoreError(code) from exc

   def _quarantine(self, path: Path) -> None:
      target = self._root / "quarantine" / f"{path.name}.{os.getpid()}.{threading.get_ident()}"
      try:
         os.replace(path, target)
         self._fsync_dir(self._root)
      except FileNotFoundError:
         pass

   @staticmethod
   def _fsync_dir(directory: Path) -> None:
      descriptor = os.open(directory, os.O_RDONLY)
      try:
         os.fsync(descriptor)
      finally:
         os.close(descriptor)

   def _write(self, key: str, record: dict[str, Any]) -> None:
      self._validate(record, key)
      data = _canonical(record)
      temporary = self._root / f".{key}.{os.getpid()}.tmp"
      descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      try:
         os.write(descriptor, data)
         os.fsync(descriptor)
      finally:
         os.close(descriptor)
      os.replace(temporary, self._path(key))
      self._fsync_dir(self._root)

   @staticmethod
   def _validate(record: Any, key: str) -> None:
      if not isinstance(record, dict) or frozenset(record) != _RECORD_FIELDS:
         raise FenceStoreError("fence_record_invalid")
      if record["protocol"] != DURABLE_FENCE_RECORD_PROTOCOL or record["request_key"] != key:
         raise FenceStoreError("fence_record_invalid")
      for field in ("request_attempt", "path_generation", "fencing_generation", "record_sequence"):
         if not _positive(record[field]):
            raise FenceStoreError("fence_record_invalid")
      published = record["published_generation"]
      if published is not None and published != record["fencing_generation"]:
         raise FenceStoreError("fence_record_invalid")
      outcome = record["terminal_outcome"]
      if outcome is not None and outcome not in TERMINAL_OUTCOMES:
         raise FenceStoreError("fence_record_invalid")
      previous = record["previous_digest"]
      if (record["record_sequence"] == 1) != (previous is None):
         raise FenceStoreError("fence_record_invalid")
      body = {field: value for field, value in record.items() if field != "evidence_digest"}
      if record["evidence_digest"] != _digest(body):
         raise FenceStoreError("fence_record_digest_mismatch")

   @staticmethod
   def _seal(body: dict[str, Any]) -> dict[str, Any]:
      return {**body, "evidence_digest": _digest(body)}

   def _next(self, current: dict[str, Any] | None, key: str, **changes: Any) -> dict[str, Any]:
      base = current or {
         "protocol": DURABLE_FENCE_RECORD_PROTOCOL,
         "request_key": key,
         "request_attempt": 0,
         "path_generation": 0,
         "fencing_generation": 0,
         "published_generation": None,
         "terminal_outcome": None,
         "record_sequence": 0,
         "previous_digest": None,
      }
      body = {field: value for field, value in base.items() if field != "evidence_digest"}
      body.update(changes)
      body["record_sequence"] = base["record_sequence"] + 1
      body["previous_digest"] = current["evidence_digest"] if current else None
      return self._seal(body)

   @staticmethod
   def _receipt(record: dict[str, Any], action: str) -> dict[str, Any]:
      return {
         "protocol": DURABLE_FENCE_RECEIPT_PROTOCOL,
         "action": action,
         "request_attempt": record["request_attempt"],
         "path_generation": record["path_generation"],
         "fencing_generation": record["fencing_generation"],
         "published_generation": record["published_generation"],
         "terminal_outcome": record["terminal_outcome"],
         "record_digest": record["evidence_digest"],
         "durability": DURABILITY,
      }

   # -- operations -------------------------------------------------------------

   def current(self, request_id: str) -> dict[str, Any] | None:
      key = request_key(request_id)
      with self._exclusive():
         record = self._read(key)
      return None if record is None else self._receipt(record, "read")

   def advance(self, request_id: str, *, expected_fencing_generation: int) -> dict[str, Any]:
      """CAS: advance attempt, path and fence together, durably, from the expected generation."""

      if not isinstance(expected_fencing_generation, int) or isinstance(expected_fencing_generation, bool) or expected_fencing_generation < 0:
         raise FenceStoreError("fence_expected_generation_invalid")
      key = request_key(request_id)
      with self._exclusive():
         current = self._read(key)
         generation = 0 if current is None else current["fencing_generation"]
         if current is not None and current["terminal_outcome"] is not None:
            raise FenceStoreError("fence_request_terminal")
         if generation != expected_fencing_generation:
            raise FenceStoreError("fence_generation_conflict")
         record = self._next(
            current,
            key,
            request_attempt=(current["request_attempt"] if current else 0) + 1,
            path_generation=(current["path_generation"] if current else 0) + 1,
            fencing_generation=generation + 1,
            published_generation=None,
         )
         self._write(key, record)
      return self._receipt(record, "advanced")

   def accepts(self, request_id: str, generation: int) -> bool:
      """True only for the current generation of a non-terminal request."""

      key = request_key(request_id)
      with self._exclusive():
         current = self._read(key)
      return (
         current is not None
         and current["terminal_outcome"] is None
         and isinstance(generation, int)
         and not isinstance(generation, bool)
         and generation == current["fencing_generation"]
      )

   def grant_publication(self, request_id: str, *, generation: int) -> dict[str, Any]:
      """CAS: exactly one publisher, and only for the current generation."""

      key = request_key(request_id)
      with self._exclusive():
         current = self._read(key)
         if current is None or current["terminal_outcome"] is not None:
            raise FenceStoreError("fence_publication_unavailable")
         if generation != current["fencing_generation"]:
            raise FenceStoreError("stale_generation")
         if current["published_generation"] is not None:
            raise FenceStoreError("duplicate_publisher")
         record = self._next(current, key, published_generation=generation)
         self._write(key, record)
      return self._receipt(record, "publication_granted")

   def mark_terminal(self, request_id: str, *, generation: int, outcome: str) -> dict[str, Any]:
      """Record one terminal outcome for the current generation, exactly once."""

      if outcome not in TERMINAL_OUTCOMES:
         raise FenceStoreError("fence_terminal_outcome_invalid")
      key = request_key(request_id)
      with self._exclusive():
         current = self._read(key)
         if current is None:
            raise FenceStoreError("fence_request_unknown")
         if current["terminal_outcome"] is not None:
            raise FenceStoreError("duplicate_terminal")
         if generation != current["fencing_generation"]:
            raise FenceStoreError("stale_generation")
         record = self._next(current, key, terminal_outcome=outcome)
         self._write(key, record)
      return self._receipt(record, "terminal")

   def release(self, request_id: str) -> bool:
      """Remove a terminal record (exact cleanup).  Non-terminal records stay."""

      key = request_key(request_id)
      with self._exclusive():
         current = self._read(key)
         if current is None:
            return False
         if current["terminal_outcome"] is None:
            raise FenceStoreError("fence_request_not_terminal")
         self._path(key).unlink()
         self._fsync_dir(self._root)
      return True

   def record_count(self) -> int:
      """Durable fence records currently held (temporary and quarantined files excluded)."""

      with self._exclusive():
         return sum(
            1
            for path in self._root.iterdir()
            if path.suffix == ".json" and not path.name.startswith(".") and path.is_file() and not path.is_symlink()
         )

   def reconcile(self, request_id: str, *, shadow_checkpoint_valid: bool, a6_available: bool) -> dict[str, Any]:
      """Reconcile one request after a coordinator restart (spec section 8).

      Any non-terminal request is fenced forward first, so no generation that
      existed before the restart can ever publish again.  A request that was
      publishing when the coordinator stopped is an ambiguous publisher: it is
      never resumed and reconciles to fallback or abort.
      """

      if not isinstance(shadow_checkpoint_valid, bool) or not isinstance(a6_available, bool):
         raise FenceStoreError("fence_reconcile_input_invalid")
      key = request_key(request_id)
      fallback = "fallback_required" if a6_available else "aborted"
      with self._exclusive():
         try:
            current = self._read(key)
         except FenceStoreError as exc:
            return self._reconciliation(fallback, reason=exc.code, record=None)
         if current is None:
            if any(self._root.joinpath("quarantine").glob(f"{key}.json.*")):
               return self._reconciliation(fallback, reason="fence_record_quarantined", record=None)
            return self._reconciliation("released", reason="no_fence_record", record=None)
         if current["terminal_outcome"] is not None:
            return self._reconciliation("already_terminal", reason=current["terminal_outcome"], record=current)
         was_publishing = current["published_generation"] is not None
         record = self._next(
            current,
            key,
            request_attempt=current["request_attempt"] + 1,
            path_generation=current["path_generation"] + 1,
            fencing_generation=current["fencing_generation"] + 1,
            published_generation=None,
         )
         self._write(key, record)
      if was_publishing:
         return self._reconciliation(fallback, reason="ambiguous_publisher_fenced", record=record)
      if shadow_checkpoint_valid:
         return self._reconciliation("shadow_restored", reason="shadow_checkpoint_valid", record=record)
      return self._reconciliation(fallback, reason="shadow_checkpoint_unavailable", record=record)

   @staticmethod
   def _reconciliation(state: str, *, reason: str, record: dict[str, Any] | None) -> dict[str, Any]:
      return {
         "protocol": RECONCILIATION_PROTOCOL,
         "state": state,
         "reason": reason,
         "fencing_generation": None if record is None else record["fencing_generation"],
         "record_digest": None if record is None else record["evidence_digest"],
      }
