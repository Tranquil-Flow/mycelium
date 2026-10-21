# SPDX-License-Identifier: AGPL-3.0-only
"""In-process A7 successor-recovery coordinator over real stage runtimes.

This composes the A7 building blocks into one request-scoped recovery authority:

* **shadowing** -- after each committed token the source stage exports the KV
  positions added since its last checkpoint; the checkpoint is sealed to the
  standby's verified key; the standby opens it, verifies and stores the chain
  link, and signs an acknowledgement; the coordinator verifies the signature and
  binding and feeds the route watermark ledger (spec section 5);
* **cutover** -- after a covered source failure the coordinator durably advances
  the fence, checks the closed eligibility predicate against the frozen gateway
  watermark, installs the fence on every participant (the standby imports the
  assembled checkpoint for the successor path, surviving stages rekey their KV
  and release the fenced path, and the gateway/router/transport installers
  confirm the new generation), and only then grants the single publication by
  compare-and-swap (spec section 6);
* **fallback** -- any rejection leaves no successor state behind and reports the
  A6 ``full_context_replay`` outcome when A6 is available, or ``aborted``; a KV
  rejection is never labelled as continuity (spec section 7).

It owns no transport and no product wiring: the caller moves sealed bytes and
signed acks between processes, and decides when a failure is covered.  Records
it returns are privacy-reduced (digests, counts, positions, outcomes).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mycelium_a7_checkpoint_channel import (
   StandbyCheckpointKey,
   open_checkpoint,
   payload_digest,
   seal_checkpoint,
   sign_checkpoint_ack,
   verify_signed_checkpoint_ack,
   verify_standby_key_record,
)
from mycelium_a7_fence_store import DurableFenceStore, FenceStoreError, request_key
from mycelium_a7_kv_successor import (
   CHECKPOINT_ACK_PROTOCOL,
   A7ContractViolation,
   RouteAckLedger,
   binding_digest,
   evaluate_cutover_eligibility,
)
from mycelium_a7_standby_store import StandbyShadowError, StandbyShadowStore
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.kv_checkpoint import KVCheckpointError, decode_kv_checkpoint

SUCCESSOR_RESULT_PROTOCOL = "mycelium.a7_successor_recovery_result.v1"
GATEWAY_INSTALLERS = ("request_gateway", "router", "transport_ingress")


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class StandbySide:
   """The standby runtime for one replaced stage (one membership incarnation)."""

   port: Any
   placement_id: str
   placement_digest: str
   incarnation: str
   signer: Ed25519EvidenceSigner
   key: StandbyCheckpointKey
   store: StandbyShadowStore
   max_checkpoint_bytes: int

   def receive(self, sealed: bytes, *, session_digest: str, now_unix_ms: int | None = None) -> dict[str, Any]:
      """Open, verify and store one sealed checkpoint; return the signed ack."""

      binding, payload = open_checkpoint(sealed, standby_key=self.key, max_bytes=self.max_checkpoint_bytes, now_unix_ms=now_unix_ms)
      receipt = self.store.accept(
         request_key=binding["request_key"],
         stage_id=binding["stage_id"],
         sequence=binding["checkpoint_sequence"],
         predecessor_digest=binding["predecessor_digest"],
         payload=payload,
      )
      ack = {
         "protocol": CHECKPOINT_ACK_PROTOCOL,
         "ack_id": _digest(f"ack:{binding['request_key']}:{binding['stage_id']}:{binding['checkpoint_sequence']}"),
         "request_id": binding["request_key"],
         "session_id": session_digest,
         "request_attempt": binding["request_attempt"],
         "stage_id": binding["stage_id"],
         "standby_placement_id": self.placement_digest,
         "standby_membership_incarnation": self.incarnation,
         "checkpoint_sequence": binding["checkpoint_sequence"],
         "checkpoint_digest": receipt["checkpoint_digest"],
         "predecessor_digest": binding["predecessor_digest"],
         "committed_token_count": binding["committed_token_count"],
         "committed_prefix_digest": binding["committed_prefix_digest"],
         "stored_checkpoint_digest_verified": True,
         "stored_checkpoint_digest": receipt["checkpoint_digest"],
         "resource_reservation_generation": 1,
         "acknowledgement_authority": "standby_runtime",
         "ack_authority_generation": 1,
      }
      ack["evidence_digest"] = binding_digest(ack)
      return sign_checkpoint_ack(self.signer, ack)


class SuccessorRecoveryCoordinator:
   """One request's A7 shadowing, fenced cutover and honest fallback."""

   def __init__(
      self,
      *,
      request_id: str,
      session_id: str,
      stage_id: str,
      source_placement_digest: str,
      standby: StandbySide,
      standby_key_record: Mapping[str, Any],
      verify_standby_signature: Callable[[bytes, dict[str, Any]], bool],
      fence_store: DurableFenceStore,
      compatibility: Mapping[str, Any],
      a6_available: bool,
      checkpoint_ttl_ms: int = 60_000,
   ) -> None:
      self._request_id = request_id
      self._request_key = "sha256:" + request_key(request_id)
      self._session_digest = _digest(f"session:{session_id}")
      self._stage_id = stage_id
      self._source_placement = source_placement_digest
      self._standby = standby
      self._verify = verify_standby_signature
      self._standby_public = verify_standby_key_record(
         standby_key_record,
         verify_signature=verify_standby_signature,
         standby_placement_id=standby.placement_digest,
         standby_membership_incarnation=standby.incarnation,
      )
      self._fences = fence_store
      self._compatibility = dict(compatibility)
      self._a6_available = a6_available
      self._ttl = checkpoint_ttl_ms
      begun = fence_store.advance(request_id, expected_fencing_generation=0)
      self._attempt = begun["request_attempt"]
      self._generation = begun["fencing_generation"]
      # Shipping authority belongs to the source's generation; once the fence
      # advances past it, the source can neither publish nor checkpoint.
      self._source_generation = self._generation
      fence_store.grant_publication(request_id, generation=self._generation)
      self._ledger = RouteAckLedger(
         replaced_stage_ids=[stage_id],
         request_attempt=self._attempt,
         current_incarnations={stage_id: standby.incarnation},
      )
      self._shipped_end = 0
      self._sequence = 0
      self._predecessor: str | None = None
      self._checkpoint_bytes = 0

   @property
   def source_generation(self) -> int:
      return self._source_generation

   @property
   def route_watermark(self) -> dict[str, Any] | None:
      return self._ledger.route_watermark

   def ship_checkpoint(
      self,
      source_port: Any,
      source_path_id: str,
      *,
      committed_token_count: int,
      committed_prefix_digest: str,
      deliver: Callable[[bytes], Mapping[str, Any]] | None = None,
      now_unix_ms: int | None = None,
   ) -> str:
      """Ship the KV positions added since the last checkpoint; return the ack outcome."""

      if not self._fences.accepts(self._request_id, self._source_generation):
         raise A7ContractViolation("stale_generation")
      payload = source_port.export_kv_checkpoint(source_path_id, from_position=self._shipped_end)
      header, _layers = decode_kv_checkpoint(payload, max_bytes=self._standby.max_checkpoint_bytes)
      now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
      binding = {
         "request_key": self._request_key,
         "request_attempt": self._attempt,
         "stage_id": self._stage_id,
         "checkpoint_sequence": self._sequence,
         "predecessor_digest": self._predecessor,
         "committed_token_count": committed_token_count,
         "committed_prefix_digest": committed_prefix_digest,
         "kv_position": header["kv_span"][1],
         "checkpoint_digest": payload_digest(payload),
         "byte_size": len(payload),
         "standby_placement_id": self._standby.placement_digest,
         "standby_membership_incarnation": self._standby.incarnation,
         "source_placement_id": self._source_placement,
         "expires_at_unix_ms": now + self._ttl,
      }
      sealed = seal_checkpoint(payload, binding=binding, standby_public_key=self._standby_public, max_bytes=self._standby.max_checkpoint_bytes)
      signed = (
         deliver(sealed)
         if deliver is not None
         else self._standby.receive(sealed, session_digest=self._session_digest, now_unix_ms=now)
      )
      ack = verify_signed_checkpoint_ack(signed, verify_signature=self._verify, binding=binding)
      outcome = self._ledger.record(ack)
      if outcome != "idempotent":
         self._shipped_end = header["kv_span"][1]
         self._sequence += 1
         self._predecessor = binding["checkpoint_digest"]
         self._checkpoint_bytes += len(payload)
      return outcome

   def cutover(
      self,
      *,
      gateway_committed_token_count: int,
      gateway_committed_prefix_digest: str,
      expected_kv_position: int,
      qualifier_current: bool,
      surviving: Sequence[tuple[Any, str]],
      successor_path_id: str,
      lease_expires_at: float,
      installers: Mapping[str, Callable[[int], bool]],
   ) -> dict[str, Any]:
      """Fence the failed attempt and either cut over once or fall back honestly."""

      watermark = self._ledger.route_watermark
      try:
         fenced = self._fences.advance(self._request_id, expected_fencing_generation=self._generation)
      except FenceStoreError as exc:
         return self._result("aborted", exc.code, None, watermark, gateway_committed_token_count)
      self._attempt = fenced["request_attempt"]
      self._generation = fenced["fencing_generation"]

      pre = evaluate_cutover_eligibility(
         route_watermark=watermark,
         gateway_committed_token_count=gateway_committed_token_count,
         gateway_committed_prefix_digest=gateway_committed_prefix_digest,
         compatibility=self._compatibility,
         qualifier_current=qualifier_current,
         fence_installed=True,
         a6_available=self._a6_available,
      )
      if not pre["kv_successor_eligible"]:
         return self._result(pre["outcome"], pre["reason"], None, watermark, gateway_committed_token_count)

      # Install the fence on the standby first (the riskiest step), then the
      # surviving stages, then the gateway/router/transport installers.
      installed: list[tuple[Any, str]] = []
      import_receipt: dict[str, Any] | None = None
      reason = "eligible"
      try:
         assembled = self._standby.store.full_checkpoint(request_key=self._request_key, stage_id=self._stage_id)
         import_receipt = self._standby.port.import_kv_checkpoint(
            assembled,
            request_id=self._request_id,
            path_id=successor_path_id,
            path_attempt=self._attempt,
            placement_id=self._standby.placement_id,
            lease_expires_at=lease_expires_at,
            expected_next_position=expected_kv_position,
            max_bytes=self._standby.max_checkpoint_bytes,
         )
         installed.append((self._standby.port, successor_path_id))
         for port, old_path_id in surviving:
            port.rekey_kv_path(
               old_path_id,
               new_path_id=successor_path_id,
               new_path_attempt=self._attempt,
               lease_expires_at=lease_expires_at,
            )
            installed.append((port, successor_path_id))
      except (KVCheckpointError, StandbyShadowError):
         reason = "compatibility_incompatible"
      if reason == "eligible":
         confirmed = all(
            name in installers and installers[name](self._generation) is True
            for name in GATEWAY_INSTALLERS
         )
         final = evaluate_cutover_eligibility(
            route_watermark=watermark,
            gateway_committed_token_count=gateway_committed_token_count,
            gateway_committed_prefix_digest=gateway_committed_prefix_digest,
            compatibility=self._compatibility,
            qualifier_current=qualifier_current,
            fence_installed=confirmed,
            a6_available=self._a6_available,
         )
         reason = final["reason"]
      if reason != "eligible":
         for port, path_id in installed:
            port.cancel(path_id)
         outcome = "full_context_replay" if self._a6_available else "aborted"
         return self._result(outcome, reason, None, watermark, gateway_committed_token_count)
      self._fences.grant_publication(self._request_id, generation=self._generation)
      return self._result("fenced_kv_successor", "eligible", import_receipt, watermark, gateway_committed_token_count)

   def _result(
      self,
      outcome: str,
      reason: str,
      receipt: Mapping[str, Any] | None,
      watermark: Mapping[str, Any] | None,
      gateway_count: int,
   ) -> dict[str, Any]:
      acked = None if watermark is None else watermark["committed_token_count"]
      return {
         "protocol": SUCCESSOR_RESULT_PROTOCOL,
         "outcome": outcome,
         "reason": reason,
         "request_attempt": self._attempt,
         "fencing_generation": self._generation,
         "gateway_committed_token_count": gateway_count,
         "acknowledged_token_count": acked,
         "acknowledgement_lag": None if acked is None else gateway_count - acked,
         "checkpoint_sequences_shipped": self._sequence,
         "checkpoint_bytes_shipped": self._checkpoint_bytes,
         "successor_checkpoint_digest": None if receipt is None else receipt["checkpoint_digest"],
         "successor_kv_position": None if receipt is None else receipt["next_position"],
         "kv_continuity_claimed": outcome == "fenced_kv_successor",
      }

   def finish(self, *, outcome: str) -> dict[str, int]:
      """Record the terminal outcome once and release every shadow and fence record."""

      self._fences.mark_terminal(self._request_id, generation=self._generation, outcome=outcome)
      released = self._standby.store.release_request(self._request_key)
      self._fences.release(self._request_id)
      return {"shadow_bytes_released": released, **self._standby.store.snapshot()}
