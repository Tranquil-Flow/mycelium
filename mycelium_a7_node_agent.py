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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mycelium_a7_checkpoint_channel import (
   SIGNED_ACK_PROTOCOL,
   CheckpointChannelError,
   signed_ack_statement,
   payload_digest,
   seal_checkpoint,
   verify_signed_checkpoint_ack,
   verify_standby_key_record,
)
from mycelium_a7_fence_store import request_key
from mycelium_a7_kv_successor import A7ContractViolation, RouteAckLedger
from mycelium_a7_standby_store import StandbyShadowError
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_router.contracts import (
   KVCheckpointAck,
   KVCheckpointShipment,
   KVSuccessorCutover,
   KVSuccessorCutoverReceipt,
)
from mycelium_router.kv_checkpoint import KVCheckpointError, decode_kv_checkpoint

Send = Callable[[str, object, bytes], None]


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


@dataclass
class _EntryRole:
   ledger: RouteAckLedger
   verify_standby_signature: Callable[[bytes, dict[str, Any]], bool]
   receipts: list[KVSuccessorCutoverReceipt] = field(default_factory=list)


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

   # -- source role ---------------------------------------------------------------

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
      try:
         signed = standby.receive(sealed, session_digest=session, now_unix_ms=self._clock_ms())
      except (CheckpointChannelError, StandbyShadowError, KVCheckpointError, ValueError):
         return False
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
      except A7ContractViolation:
         return False
      return True

   def receive_cutover(self, cutover: KVSuccessorCutover, *, source_node_id: str | None = None) -> bool:
      if source_node_id is None:
         return False
      with self._lock:
         port = self._ports.get(cutover.placement_id)
         standby = self._standby
      installed, reason, digest = False, "a7_cutover_placement_unknown", ""
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
               )
               if receipt["next_position"] != cutover.expected_next_position:
                  port.cancel(cutover.successor_path_id)
                  raise KVCheckpointError("kv_checkpoint_position_mismatch")
            installed, reason = True, "installed"
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
