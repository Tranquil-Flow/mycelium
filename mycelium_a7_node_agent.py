# SPDX-License-Identifier: AGPL-3.0-only
"""A7 node agent: the per-node half of successor recovery over Router messages.

One agent runs on every node of a route with A7 coverage and plays whichever
roles that node holds for a request:

* **source** -- after each committed token at the covered stage it exports the
  newly added KV positions, seals them to the standby's verified key and sends a
  ``KVCheckpointShipment`` to the standby node; it verifies the standby's signed
  acknowledgement against the exact binding it sealed and forwards it to the
  entry node;
* **standby** -- it opens, verifies and stores each chain link and returns a
  signed ``KVCheckpointAck``; on a ``KVSuccessorCutover`` it imports the
  assembled checkpoint as the successor path's KV;
* **surviving stage** -- on a ``KVSuccessorCutover`` it rekeys its own KV to the
  successor path, fencing the failed attempt;
* **entry** -- it re-verifies every forwarded acknowledgement with the standby's
  pinned key and feeds the route watermark ledger, and collects fence-install
  receipts for the cutover decision.

Messages travel through an injected ``send(destination_node_id, message, payload)``
(the Router transport's ``send_a7_message``).  Every refusal returns ``False``
or a negative receipt with a stable reason; nothing is retried or synthesized.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mycelium_a7_checkpoint_channel import (
   SIGNED_ACK_PROTOCOL,
   CheckpointChannelError,
   signed_ack_statement,
   payload_digest,
   seal_checkpoint,
   sealed_checkpoint_binding,
   verify_signed_checkpoint_ack,
   verify_standby_key_record,
)
from mycelium_a7_fence_store import request_key
from mycelium_a7_kv_successor import A7ContractViolation, RouteAckLedger
from mycelium_a7_standby_store import StandbyShadowError
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_router.contracts import (
   KVCheckpointAck,
   KVCheckpointCommit,
   KVCheckpointShipment,
   KVShadowRelease,
   KVSuccessorCutover,
   KVSuccessorCutoverReceipt,
)
from mycelium_router.kv_checkpoint import KVCheckpointError, decode_kv_checkpoint

Send = Callable[[str, object, bytes], None]
_MAX_RELEASED_HISTORY = 4096


def _shadow_key(request_id: str) -> str | None:
   """The request key a sealed checkpoint and the standby store use for ``request_id``."""

   try:
      return "sha256:" + request_key(request_id)
   except ValueError:
      return None


def _canonical(value: Any) -> str:
   return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class _SourceRole:
   stage_id: str
   port: Any
   path_id: str
   path_attempt: int
   standby_node_id: str
   standby_placement_id: str
   standby_placement_digest: str
   standby_incarnation: str
   standby_public_key: bytes
   verify_standby_signature: Callable[[bytes, dict[str, Any]], bool]
   source_placement_digest: str
   entry_node_id: str
   max_bytes: int
   ttl_ms: int
   shipped_end: int = 0
   sequence: int = 0
   predecessor: str | None = None
   pending: dict[int, dict[str, Any]] = field(default_factory=dict)
   fenced: bool = False


@dataclass(frozen=True)
class SourceCoverage:
   """Node-static A7 coverage for one local ACTIVE source placement.

   A source role for a request is created from this only when the request's
   entry node commits output on a path through this placement.
   """

   stage_id: str
   port: Any
   standby_node_id: str
   standby_placement_id: str
   standby_placement_digest: str
   standby_incarnation: str
   standby_key_record: Mapping[str, Any]
   verify_standby_signature: Callable[[bytes, dict[str, Any]], bool]
   source_placement_digest: str
   max_bytes: int
   ttl_ms: int = 60_000


@dataclass
class _EntryRole:
   ledger: RouteAckLedger
   verify_standby_signature: Callable[[bytes, dict[str, Any]], bool]
   receipts: list[KVSuccessorCutoverReceipt] = field(default_factory=list)
   last_ack: dict[str, Any] | None = None


class A7NodeAgent:
   def __init__(self, node_id: str, *, send: Send, clock_ms: Callable[[], int] | None = None) -> None:
      self.node_id = node_id
      self._send = send
      self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
      self._lock = threading.RLock()
      self._sources: dict[str, _SourceRole] = {}
      self._entries: dict[str, _EntryRole] = {}
      self._ports: dict[str, Any] = {}
      self._standby: StandbySide | None = None
      self._session_digest: str | None = None
      self._coverage: dict[str, SourceCoverage] = {}
      # Released request ids -> their shadow request key (what a sealed checkpoint names).
      self._released: OrderedDict[str, str | None] = OrderedDict()
      self._released_keys: set[str] = set()
      self._refusals: Counter[str] = Counter()
      # Request id -> successor paths a cutover installed on this node: a
      # terminal release frees them even when it names the old path.
      self._successor_paths: OrderedDict[str, set[str]] = OrderedDict()

   # -- registration ------------------------------------------------------------

   def register_port(self, placement_id: str, port: Any) -> None:
      with self._lock:
         self._ports[placement_id] = port

   def register_standby(self, standby: StandbySide, *, session_digest: str) -> None:
      with self._lock:
         self._standby = standby
         self._ports[standby.placement_id] = standby.port
         self._session_digest = session_digest

   def register_source(
      self,
      request_id: str,
      *,
      stage_id: str,
      port: Any,
      path_id: str,
      path_attempt: int,
      standby_node_id: str,
      standby_placement_id: str,
      standby_placement_digest: str,
      standby_incarnation: str,
      standby_key_record: Mapping[str, Any],
      verify_standby_signature: Callable[[bytes, dict[str, Any]], bool],
      source_placement_digest: str,
      entry_node_id: str,
      max_bytes: int,
      ttl_ms: int = 60_000,
   ) -> None:
      public = verify_standby_key_record(
         standby_key_record,
         verify_signature=verify_standby_signature,
         standby_placement_id=standby_placement_digest,
         standby_membership_incarnation=standby_incarnation,
      )
      with self._lock:
         self._sources[request_id] = _SourceRole(
            stage_id=stage_id,
            port=port,
            path_id=path_id,
            path_attempt=path_attempt,
            standby_node_id=standby_node_id,
            standby_placement_id=standby_placement_id,
            standby_placement_digest=standby_placement_digest,
            standby_incarnation=standby_incarnation,
            standby_public_key=public,
            verify_standby_signature=verify_standby_signature,
            source_placement_digest=source_placement_digest,
            entry_node_id=entry_node_id,
            max_bytes=max_bytes,
            ttl_ms=ttl_ms,
         )

   def register_entry(
      self,
      request_id: str,
      *,
      stage_id: str,
      request_attempt: int,
      standby_incarnation: str,
      verify_standby_signature: Callable[[bytes, dict[str, Any]], bool],
   ) -> None:
      with self._lock:
         self._entries[request_id] = _EntryRole(
            ledger=RouteAckLedger(
               replaced_stage_ids=[stage_id],
               request_attempt=request_attempt,
               current_incarnations={stage_id: standby_incarnation},
            ),
            verify_standby_signature=verify_standby_signature,
         )

   def register_source_coverage(self, placement_id: str, coverage: SourceCoverage) -> None:
      with self._lock:
         self._coverage[placement_id] = coverage

   # -- source role ---------------------------------------------------------------

   def receive_commit(self, commit: KVCheckpointCommit, *, source_node_id: str | None = None) -> bool:
      """Entry committed output through ``committed_token_count``: ship that KV."""

      if source_node_id is None:
         return self._refuse("commit_unauthenticated")
      with self._lock:
         if commit.request_id in self._released:
            return self._refuse("commit_request_released")
         role = self._sources.get(commit.request_id)
         if role is None:
            coverage = self._coverage.get(commit.source_placement_id)
            if coverage is None:
               return self._refuse("commit_placement_not_covered")
            try:
               public = verify_standby_key_record(
                  coverage.standby_key_record,
                  verify_signature=coverage.verify_standby_signature,
                  standby_placement_id=coverage.standby_placement_digest,
                  standby_membership_incarnation=coverage.standby_incarnation,
               )
            except CheckpointChannelError:
               return self._refuse("commit_standby_key_invalid")
            role = _SourceRole(
               stage_id=coverage.stage_id,
               port=coverage.port,
               path_id=commit.path_id,
               path_attempt=commit.path_attempt,
               standby_node_id=coverage.standby_node_id,
               standby_placement_id=coverage.standby_placement_id,
               standby_placement_digest=coverage.standby_placement_digest,
               standby_incarnation=coverage.standby_incarnation,
               standby_public_key=public,
               verify_standby_signature=coverage.verify_standby_signature,
               source_placement_digest=coverage.source_placement_digest,
               entry_node_id=source_node_id,
               max_bytes=coverage.max_bytes,
               ttl_ms=coverage.ttl_ms,
            )
            self._sources[commit.request_id] = role
         if (
            role.fenced
            or source_node_id != role.entry_node_id
            or commit.path_id != role.path_id
            or commit.path_attempt != role.path_attempt
         ):
            return self._refuse("commit_role_mismatch")
      try:
         self.ship(
            commit.request_id,
            request_attempt=commit.request_attempt,
            committed_token_count=commit.committed_token_count,
            committed_prefix_digest=commit.committed_prefix_digest,
         )
      except (A7ContractViolation, KVCheckpointError, CheckpointChannelError, ValueError) as exc:
         return self._refuse("ship_" + str(getattr(exc, "code", None) or exc)[:64])
      return True

   def receive_release(self, release: KVShadowRelease, *, source_node_id: str | None = None) -> bool:
      """Release every A7 record this node holds for a terminal or fenced request."""

      if source_node_id is None:
         return False
      with self._lock:
         role = self._sources.get(release.request_id)
         if role is not None and source_node_id != role.entry_node_id:
            return False
         self._sources.pop(release.request_id, None)
         key = _shadow_key(release.request_id)
         self._released[release.request_id] = key
         if key is not None:
            self._released_keys.add(key)
         while len(self._released) > _MAX_RELEASED_HISTORY:
            _, dropped = self._released.popitem(last=False)
            if dropped is not None:
               self._released_keys.discard(dropped)
         standby = self._standby
         ports = []
         for port in self._ports.values():
            if all(port is not seen for seen in ports):
               ports.append(port)
         # A "fenced" release retires only the old attempt after a successful
         # cutover; every terminal or fallback release also frees the
         # successor KV a cutover installed here.
         successors = () if release.reason == "fenced" else tuple(self._successor_paths.pop(release.request_id, ()))
      if standby is not None:
         standby.store.release_request("sha256:" + request_key(release.request_id))
      for path_id in (release.path_id, *sorted(set(successors) - {release.path_id})):
         for port in ports:
            try:
               # Fences the released path on this node: its KV can never resume.
               port.cancel(path_id)
            except Exception:
               continue
      return True

   def _release_raced(self, request_id: str) -> bool:
      with self._lock:
         return request_id in self._released

   def _cancel_everywhere(self, path_id: str) -> None:
      with self._lock:
         ports: list[Any] = []
         for port in self._ports.values():
            if all(port is not seen for seen in ports):
               ports.append(port)
      for port in ports:
         try:
            port.cancel(path_id)
         except Exception:
            continue

   def _refuse(self, code: str) -> bool:
      with self._lock:
         if code in self._refusals or len(self._refusals) < 64:
            self._refusals[code] += 1
      return False

   def release_entry(self, request_id: str) -> None:
      with self._lock:
         self._entries.pop(request_id, None)

   def shadow_snapshot(self) -> dict[str, Any]:
      with self._lock:
         standby = self._standby
         sources = len(self._sources)
         entries = len(self._entries)
      snapshot = {} if standby is None else {**standby.store.snapshot(), **standby.store.checkpoint_snapshot()}
      with self._lock:
         refusals = dict(self._refusals)
      return {**snapshot, "source_roles": sources, "entry_roles": entries, "refusals": refusals}

   def ship(self, request_id: str, *, request_attempt: int, committed_token_count: int, committed_prefix_digest: str) -> int:
      """Ship the KV positions added since the last checkpoint; returns the sequence sent."""

      with self._lock:
         role = self._sources.get(request_id)
         if role is None or role.fenced:
            raise A7ContractViolation("stale_generation")
         payload = role.port.export_kv_checkpoint(role.path_id, from_position=role.shipped_end)
         header, _ = decode_kv_checkpoint(payload, max_bytes=role.max_bytes)
         binding = {
            "request_key": "sha256:" + request_key(request_id),
            "request_attempt": request_attempt,
            "stage_id": role.stage_id,
            "checkpoint_sequence": role.sequence,
            "predecessor_digest": role.predecessor,
            "committed_token_count": committed_token_count,
            "committed_prefix_digest": committed_prefix_digest,
            "kv_position": header["kv_span"][1],
            "checkpoint_digest": payload_digest(payload),
            "byte_size": len(payload),
            "standby_placement_id": role.standby_placement_digest,
            "standby_membership_incarnation": role.standby_incarnation,
            "source_placement_id": role.source_placement_digest,
            "expires_at_unix_ms": self._clock_ms() + role.ttl_ms,
         }
         sealed = seal_checkpoint(payload, binding=binding, standby_public_key=role.standby_public_key, max_bytes=role.max_bytes)
         sequence = role.sequence
         role.pending[sequence] = binding
         role.shipped_end = header["kv_span"][1]
         role.sequence += 1
         role.predecessor = binding["checkpoint_digest"]
         message = KVCheckpointShipment(
            request_id=request_id,
            path_id=role.path_id,
            path_attempt=role.path_attempt,
            standby_placement_id=role.standby_placement_id,
            checkpoint_sequence=sequence,
            byte_size=len(sealed),
         )
         destination = role.standby_node_id
      self._send(destination, message, sealed)
      return sequence

   def fence_source(self, request_id: str) -> None:
      """The source's attempt was fenced: it may never checkpoint or forward again."""

      with self._lock:
         role = self._sources.get(request_id)
         if role is not None:
            role.fenced = True
            role.pending.clear()

   # -- inbound -------------------------------------------------------------------

   def receive_shipment(self, shipment: KVCheckpointShipment, sealed: bytes, *, source_node_id: str | None = None) -> bool:
      with self._lock:
         standby = self._standby
         session = self._session_digest
      if standby is None or shipment.standby_placement_id != standby.placement_id or source_node_id is None:
         return False
      # The outer shipment header is not signed. Key every check on the request
      # and sequence the sealed checkpoint names (open_checkpoint authenticates
      # exactly that binding) and refuse a header that disagrees with it.
      try:
         binding = sealed_checkpoint_binding(sealed, max_bytes=standby.max_checkpoint_bytes)
      except (CheckpointChannelError, ValueError):
         return False
      shadow_key = binding["request_key"]
      if shadow_key != _shadow_key(shipment.request_id) or binding["checkpoint_sequence"] != shipment.checkpoint_sequence:
         return self._refuse("shipment_header_mismatch")
      # A shipment that lands after its request was released must not rebuild
      # the shadow chain the release just dropped (a late sequence 0 would).
      with self._lock:
         released = shadow_key in self._released_keys
      if released:
         return self._refuse("shipment_request_released")
      try:
         signed = standby.receive(sealed, session_digest=session, now_unix_ms=self._clock_ms())
      except (CheckpointChannelError, StandbyShadowError, KVCheckpointError, ValueError):
         return False
      with self._lock:
         released = shadow_key in self._released_keys
      if released:
         # The release raced this shipment: drop what it just stored.
         standby.store.release_request(shadow_key)
         return self._refuse("shipment_request_released")
      self._send(
         source_node_id,
         KVCheckpointAck(
            request_id=shipment.request_id,
            path_id=shipment.path_id,
            path_attempt=shipment.path_attempt,
            hop="standby_to_source",
            signed_ack=_canonical(signed),
         ),
         b"",
      )
      return True

   def receive_ack(self, ack: KVCheckpointAck, *, source_node_id: str | None = None) -> bool:
      try:
         signed = json.loads(ack.signed_ack)
      except ValueError:
         return False
      if ack.hop == "standby_to_source":
         with self._lock:
            role = self._sources.get(ack.request_id)
            if role is None or role.fenced or source_node_id != role.standby_node_id:
               return False
            sequence = signed.get("ack", {}).get("checkpoint_sequence") if isinstance(signed, dict) else None
            binding = role.pending.get(sequence) if isinstance(sequence, int) else None
            if binding is None:
               return False
            try:
               verify_signed_checkpoint_ack(signed, verify_signature=role.verify_standby_signature, binding=binding)
            except CheckpointChannelError:
               return False
            del role.pending[sequence]
            entry = role.entry_node_id
         self._send(entry, KVCheckpointAck(request_id=ack.request_id, path_id=ack.path_id, path_attempt=ack.path_attempt, hop="source_to_entry", signed_ack=ack.signed_ack), b"")
         return True
      with self._lock:
         entry_role = self._entries.get(ack.request_id)
      if entry_role is None or not isinstance(signed, dict) or not isinstance(signed.get("signature"), dict):
         return False
      if signed.get("protocol") != SIGNED_ACK_PROTOCOL or set(signed) != {"protocol", "ack", "signature"}:
         return False
      if not isinstance(signed["ack"], dict) or not entry_role.verify_standby_signature(
         signed_ack_statement(signed["ack"]), signed["signature"]
      ):
         return False
      try:
         with self._lock:
            entry_role.ledger.record(signed["ack"])
            entry_role.last_ack = {
               "checkpoint_sequence": signed["ack"].get("checkpoint_sequence"),
               "committed_token_count": signed["ack"].get("committed_token_count"),
               "received_monotonic_s": time.monotonic(),
            }
      except A7ContractViolation:
         return False
      return True

   def entry_last_ack(self, request_id: str) -> dict[str, Any] | None:
      """The last verified acknowledgement this entry recorded for one request."""

      with self._lock:
         role = self._entries.get(request_id)
         return None if role is None or role.last_ack is None else dict(role.last_ack)

   def standby_last_stored(self) -> dict[str, Any] | None:
      """The standby's last stored (non-idempotent) checkpoint, when this node is a standby."""

      with self._lock:
         standby = self._standby
      return None if standby is None else standby.store.last_stored()

   def receive_cutover(
      self,
      cutover: KVSuccessorCutover,
      *,
      source_node_id: str | None = None,
      path_entry_node_id: str | None = None,
   ) -> bool:
      """Install a fenced successor (standby import or surviving-stage rekey).

      ``path_entry_node_id`` is the entry this node's relay registered for the
      old path.  A cutover from any other node is refused; KV a stage retained
      after the request's terminal hop is handed over only to a cutover from
      that authenticated entry, and never after this node processed the
      request's release.
      """

      if source_node_id is None:
         return False
      with self._lock:
         port = self._ports.get(cutover.placement_id)
         standby = self._standby
         released = cutover.request_id in self._released
      installed, reason, digest = False, "a7_cutover_placement_unknown", ""
      if released:
         port, reason = None, "a7_cutover_request_released"
      elif path_entry_node_id is not None and path_entry_node_id != source_node_id:
         port, reason = None, "a7_cutover_sender_not_entry"
      if port is not None:
         try:
            if cutover.role == "standby_import":
               if standby is None or standby.placement_id != cutover.placement_id:
                  raise KVCheckpointError("a7_cutover_not_standby")
               shadow_key = "sha256:" + request_key(cutover.request_id)
               stages = standby.store.stage_ids(shadow_key)
               if len(stages) != 1:
                  raise KVCheckpointError("shadow_checkpoint_missing")
               assembled = standby.store.full_checkpoint(request_key=shadow_key, stage_id=stages[0])
               receipt = port.import_kv_checkpoint(
                  assembled,
                  request_id=cutover.request_id,
                  path_id=cutover.successor_path_id,
                  path_attempt=cutover.successor_path_attempt,
                  placement_id=cutover.placement_id,
                  lease_expires_at=cutover.lease_expires_at,
                  expected_next_position=cutover.expected_next_position,
                  max_bytes=standby.max_checkpoint_bytes,
               )
               digest = receipt["checkpoint_digest"]
            else:
               receipt = port.rekey_kv_path(
                  cutover.old_path_id,
                  new_path_id=cutover.successor_path_id,
                  new_path_attempt=cutover.successor_path_attempt,
                  lease_expires_at=cutover.lease_expires_at,
                  truncate_to_position=cutover.expected_next_position,
                  request_id=cutover.request_id,
                  placement_id=cutover.placement_id,
                  allow_terminal_retained=path_entry_node_id is not None and path_entry_node_id == source_node_id,
               )
               if receipt["next_position"] != cutover.expected_next_position:
                  port.cancel(cutover.successor_path_id)
                  raise KVCheckpointError("kv_checkpoint_position_mismatch")
            installed, reason = True, "installed"
            with self._lock:
               self._successor_paths.setdefault(cutover.request_id, set()).add(cutover.successor_path_id)
               self._successor_paths.move_to_end(cutover.request_id)
               while len(self._successor_paths) > _MAX_RELEASED_HISTORY:
                  self._successor_paths.popitem(last=False)
            if self._release_raced(cutover.request_id):
               # The request's release landed while this cutover installed:
               # free what it just installed.
               self._cancel_everywhere(cutover.successor_path_id)
               installed, reason = False, "a7_cutover_request_released"
         except (KVCheckpointError, StandbyShadowError) as exc:
            installed, reason = False, exc.code
      self._send(
         source_node_id,
         KVSuccessorCutoverReceipt(
            request_id=cutover.request_id,
            successor_path_id=cutover.successor_path_id,
            successor_path_attempt=cutover.successor_path_attempt,
            fencing_generation=cutover.fencing_generation,
            placement_id=cutover.placement_id,
            installed=installed,
            reason=reason,
            checkpoint_digest=digest,
         ),
         b"",
      )
      return True

   def receive_cutover_receipt(self, receipt: KVSuccessorCutoverReceipt, *, source_node_id: str | None = None) -> bool:
      with self._lock:
         entry_role = self._entries.get(receipt.request_id)
         if entry_role is None:
            return False
         entry_role.receipts.append(receipt)
      return True

   # -- entry views -----------------------------------------------------------------

   def route_watermark(self, request_id: str) -> dict[str, Any] | None:
      with self._lock:
         role = self._entries.get(request_id)
         return None if role is None else role.ledger.route_watermark

   def cutover_receipts(self, request_id: str) -> tuple[KVSuccessorCutoverReceipt, ...]:
      with self._lock:
         role = self._entries.get(request_id)
         return () if role is None else tuple(role.receipts)
