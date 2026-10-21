# SPDX-License-Identifier: AGPL-3.0-only
"""A7 entry-node controller: committed-output shadowing and the fenced cutover.

The entry Router owns the committed-output watermark (owner ruling: committed
output = the tokens the entry has handed to the gateway, each with a
synchronous handoff acknowledgement).  For a request whose locked path crosses
a covered source placement this controller:

* **begins coverage** -- durably opens the request's fence record (attempt 1,
  generation 1, single publisher) and registers the route watermark ledger;
* **commits** -- after each token is handed to the gateway, asks the source to
  ship the stage KV for exactly that committed count/digest
  (``KVCheckpointCommit``); the standby's signed acknowledgement returns via the
  source and feeds the ledger;
* **gates decode** -- the entry does not dispatch the next decode until that
  acknowledgement has arrived or the bounded synchronous-ack window lapses
  (lag is recorded; a lagging route falls back to A6 on failure);
* **cuts over** -- on a covered source failure it durably advances the fence,
  checks the closed eligibility predicate against the frozen gateway watermark,
  sends ``KVSuccessorCutover`` to the standby (import) and every surviving stage
  (rekey), waits for their fence-install receipts, confirms the entry-local
  installers (gateway, router, transport ingress), and only then grants the
  single publication by compare-and-swap; otherwise it releases everything it
  installed and reports the honest A6 ``full_context_replay`` or ``aborted``
  outcome;
* **releases** -- terminal, fenced and fallback requests release the shadow and
  any successor KV on every participant exactly once.

Records it returns are privacy-reduced: counts, positions, generations, digests
and reasons only.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mycelium_a7_fence_store import DurableFenceStore, FenceStoreError, request_key
from mycelium_a7_kv_successor import (
   COMPATIBILITY_IDENTITY_FIELDS,
   compare_compatibility,
   evaluate_cutover_eligibility,
)
from mycelium_a7_standby_qualification import standby_qualification_is_current
from mycelium_router.contracts import (
   KVCheckpointCommit,
   KVShadowRelease,
   KVSuccessorCutover,
   PathManifest,
)

CUTOVER_RECORD_PROTOCOL = "mycelium.a7_entry_cutover.v1"
LAST_CUTOVER_SOURCE = "durable_last_cutover"
ENTRY_INSTALLERS = ("request_gateway", "router", "transport_ingress")
_MAX_TRACKED_REQUESTS = 4096


def _sha(value: Any) -> str:
   data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
   return "sha256:" + hashlib.sha256(data).hexdigest()


def stage_digest(stage_id: str) -> str:
   return _sha(f"a7-stage:{stage_id}".encode("utf-8"))


def committed_prefix_digest(request_id: str, committed_token_ids: Sequence[int]) -> str:
   """The gateway committed-output digest (bound into every checkpoint and ack)."""

   return _sha({"request": request_key(request_id), "committed": [int(token) for token in committed_token_ids]})


# Remote shadow-release delivery: attempts and linear backoff step (seconds).
_RELEASE_ATTEMPTS = 3
_RELEASE_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class StandbyCoverage:
   """One qualified standby covering one source placement of the deployment."""

   stage_id: str
   source_placement_id: str
   source_node_id: str
   standby_placement_id: str
   standby_node_id: str
   standby_incarnation: str
   verify_standby_signature: Callable[[bytes, dict[str, Any]], bool]
   qualification: Mapping[str, Any]


@dataclass
class _Coverage:
   coverage: StandbyCoverage
   path_id: str
   path_attempt: int
   request_attempt: int
   generation: int
   committed: int = 0
   pending_since: float | None = None
   lag_events: int = 0
   max_ack_wait_ms: float = 0.0
   commits_sent: int = 0
   finished: bool = False
   last_send_error: str | None = None
   result: dict[str, Any] | None = None
   participants: set[str] = field(default_factory=set)
   source_lost_accepted: bool = False
   old_path_fenced: bool = False
   # Product evidence (privacy-reduced timings and counts, never token values).
   commit_sent_at: dict[int, float] = field(default_factory=dict)
   ack_waits_ms: list[float] = field(default_factory=list)
   duplicate_commits: int = 0
   standby_receipt_rtt_ms: float | None = None
   last_ack_rtt_ms: float | None = None
   last_ack_sequence: int | None = None


class A7EntryController:
   #: Optional ``fence_old_path(request_id, path_id, path_attempt)``: fences the
   #: failed attempt's in-flight and queued transport forwards at cutover, so a
   #: send still blocked on the dead source cannot hold this entry's cleanup
   #: receipt open until the delivery deadline.  ``None`` changes nothing.
   fence_old_path: Callable[[str, str, int], Any] | None = None

   def __init__(
      self,
      *,
      node_id: str,
      agent: Any,
      send: Callable[[str, object, bytes], None],
      fence_store: DurableFenceStore,
      coverage: Sequence[StandbyCoverage],
      a6_available: bool = True,
      ack_timeout_seconds: float = 2.0,
      receipt_timeout_seconds: float = 5.0,
      clock_ms: Callable[[], int] | None = None,
      poll_seconds: float = 0.002,
      release_remote_asynchronously: bool = False,
   ) -> None:
      self.node_id = node_id
      self._agent = agent
      self._send = send
      self._fences = fence_store
      self._coverage = {entry.source_placement_id: entry for entry in coverage}
      self._a6_available = a6_available
      self._ack_timeout = ack_timeout_seconds
      self._receipt_timeout = receipt_timeout_seconds
      self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
      self._poll = poll_seconds
      self._release_async = release_remote_asynchronously
      self._lock = threading.RLock()
      self._requests: dict[str, _Coverage] = {}
      self._last_request_id: str | None = None
      self._fence_advances = 0
      self._last_cutover_persist_failures = 0
      self._last_cutover_sequence = 0
      self._last_cutover_written = 0
      self._last_cutover_write_lock = threading.Lock()
      # Shadow releases that never reached a participant, by failure code.
      self._release_failures: dict[str, int] = {}

   # -- coverage ------------------------------------------------------------------

   def covered_hop(self, manifest: PathManifest) -> StandbyCoverage | None:
      for hop in manifest.ordered_hops:
         entry = self._coverage.get(hop.placement_id)
         if entry is not None:
            return entry
      return None

   def _begin(self, request_id: str, manifest: PathManifest) -> _Coverage | None:
      coverage = self.covered_hop(manifest)
      if coverage is None:
         return None
      try:
         begun = self._fences.advance(request_id, expected_fencing_generation=0)
         self._fences.grant_publication(request_id, generation=begun["fencing_generation"])
      except FenceStoreError:
         return None
      self._fence_advances += 1
      self._agent.register_entry(
         request_id,
         stage_id=stage_digest(coverage.stage_id),
         request_attempt=begun["request_attempt"],
         standby_incarnation=coverage.standby_incarnation,
         verify_standby_signature=coverage.verify_standby_signature,
      )
      state = _Coverage(
         coverage=coverage,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         request_attempt=begun["request_attempt"],
         generation=begun["fencing_generation"],
      )
      state.participants.update({coverage.source_node_id, coverage.standby_node_id})
      if len(self._requests) >= _MAX_TRACKED_REQUESTS:
         for key in [key for key, value in self._requests.items() if value.finished][: _MAX_TRACKED_REQUESTS // 4]:
            del self._requests[key]
      self._requests[request_id] = state
      return state

   def on_committed(
      self,
      request_id: str,
      manifest: PathManifest,
      committed_token_ids: Sequence[int],
      *,
      path_node_ids: Sequence[str] = (),
   ) -> bool:
      """Output through ``len(committed_token_ids)`` was handed to the gateway.

      ``path_node_ids`` are the nodes of the locked path: each one retains the
      KV of the request's terminal hop until the terminal release, so each one
      is a release participant (a surviving stage need be neither source nor
      standby).
      """

      count = len(committed_token_ids)
      with self._lock:
         state = self._requests.get(request_id)
         if state is None:
            state = self._begin(request_id, manifest)
            if state is None:
               return False
            state.participants.update(node for node in path_node_ids if isinstance(node, str) and node)
         if state.finished or state.path_id != manifest.path_id or state.path_attempt != manifest.path_attempt:
            # A successor path has no further standby: coverage ended at cutover.
            return False
         if count <= state.committed:
            # The gateway was handed a count that did not advance the committed
            # output: a repeated publication at the entry's commit hook.
            state.duplicate_commits += 1
         state.committed = count
         state.pending_since = time.monotonic()
         state.commits_sent += 1
         state.commit_sent_at[count] = state.pending_since
         if len(state.commit_sent_at) > 64:
            del state.commit_sent_at[min(state.commit_sent_at)]
         self._last_request_id = request_id
         message = KVCheckpointCommit(
            request_id=request_id,
            path_id=state.path_id,
            path_attempt=state.path_attempt,
            request_attempt=state.request_attempt,
            source_placement_id=state.coverage.source_placement_id,
            committed_token_count=count,
            committed_prefix_digest=committed_prefix_digest(request_id, committed_token_ids),
         )
         destination = state.coverage.source_node_id
      try:
         self._send(destination, message, b"")
      except Exception as exc:
         with self._lock:
            state.lag_events += 1
            state.pending_since = None
            state.last_send_error = str(getattr(exc, "code", None) or type(exc).__name__)[:64]
         return False
      return True

   def _acknowledged(self, request_id: str) -> int:
      watermark = self._agent.route_watermark(request_id)
      return -1 if watermark is None else int(watermark["committed_token_count"])

   def _note_ack(self, state: _Coverage, request_id: str) -> None:
      """Record the round trip (commit sent here -> verified ack recorded here) of the last ack."""

      reader = getattr(self._agent, "entry_last_ack", None)
      ack = reader(request_id) if callable(reader) else None
      if ack is None or type(ack.get("committed_token_count")) is not int:
         return
      with self._lock:
         sent = state.commit_sent_at.get(ack["committed_token_count"])
         if sent is not None:
            state.last_ack_rtt_ms = round((ack["received_monotonic_s"] - sent) * 1000.0, 3)
            sequence = ack.get("checkpoint_sequence")
            state.last_ack_sequence = sequence if type(sequence) is int else None

   def await_acknowledged(self, request_id: str) -> bool:
      """Wait (bounded) until the standby acknowledged the current committed count."""

      with self._lock:
         state = self._requests.get(request_id)
         if state is None or state.finished or state.pending_since is None:
            return True
         target = state.committed
         started = state.pending_since
      deadline = started + self._ack_timeout
      while True:
         if self._acknowledged(request_id) >= target:
            self._note_ack(state, request_id)
            with self._lock:
               if state.pending_since == started:
                  state.pending_since = None
                  waited = (time.monotonic() - started) * 1000.0
                  state.max_ack_wait_ms = max(state.max_ack_wait_ms, waited)
                  if len(state.ack_waits_ms) < 4096:
                     state.ack_waits_ms.append(waited)
            return True
         if time.monotonic() >= deadline:
            with self._lock:
               if state.pending_since == started:
                  state.pending_since = None
                  state.lag_events += 1
            return False
         time.sleep(self._poll)

   # -- cutover -------------------------------------------------------------------

   def failure_is_covered(self, request_id: str, manifest: PathManifest, report: Any) -> StandbyCoverage | None:
      with self._lock:
         state = self._requests.get(request_id)
      if state is None or state.finished or state.path_id != manifest.path_id:
         return None
      if report.scope != "PLACEMENT" or report.placement_id != state.coverage.source_placement_id:
         return None
      return state.coverage

   def covers_failed_hop(self, request_id: str, path_id: str, placement_id: str) -> bool:
      with self._lock:
         state = self._requests.get(request_id)
         return (
            state is not None
            and not state.finished
            and state.path_id == path_id
            and state.coverage.source_placement_id == placement_id
         )

   def accept_source_lost(self, request_id: str, lost_node_id: str) -> dict[str, Any]:
      """A4 observed ``lost_node_id``'s process exit: does this entry own it?

      Accepts only a request this entry covers whose covered source runs on
      ``lost_node_id`` and that has not been released terminally.  Acceptance
      means the entry owns the failure handling (fenced cutover, or its own
      truthful A6/abort fallback); it is decided before any cutover work, so
      the route can bind the claim first.  Records are privacy-reduced.
      """

      with self._lock:
         state = self._requests.get(request_id)
         if state is None:
            return {"accepted": False, "reason": "request_not_covered"}
         if lost_node_id == self.node_id or state.coverage.source_node_id != lost_node_id:
            return {"accepted": False, "reason": "lost_node_not_covered_source"}
         if state.result is not None and state.result.get("terminal_released"):
            return {"accepted": False, "reason": "request_terminal"}
         repeated = state.source_lost_accepted
         state.source_lost_accepted = True
         return {
            "accepted": True,
            "reason": "source_lost_accepted",
            "repeated": repeated,
            "source_placement_id": state.coverage.source_placement_id,
            "cutover_pending": not state.finished,
         }

   def covered_source_placement(self, request_id: str, lost_node_id: str) -> str | None:
      with self._lock:
         state = self._requests.get(request_id)
         if state is None or state.finished or state.coverage.source_node_id != lost_node_id:
            return None
         return state.coverage.source_placement_id

   def _fence_old_path(self, state: _Coverage, request_id: str, manifest: PathManifest) -> None:
      fence = self.fence_old_path
      if fence is None or state.old_path_fenced:
         return
      state.old_path_fenced = True
      try:
         fence(request_id, manifest.path_id, manifest.path_attempt)
      except Exception:
         # The durable fence already excludes the old attempt; a transport that
         # cannot sweep it keeps its own delivery deadline.
         pass

   def _compatibility(self, coverage: StandbyCoverage) -> dict[str, Any]:
      qualification = coverage.qualification
      source = {name: f"{name}:{qualification.get('source_identity_digest')}" for name in COMPATIBILITY_IDENTITY_FIELDS}
      successor = {name: f"{name}:{qualification.get('standby_identity_digest')}" for name in COMPATIBILITY_IDENTITY_FIELDS}
      return compare_compatibility(source, successor)

   def cutover(
      self,
      *,
      request_id: str,
      old_manifest: PathManifest,
      successor_manifest: PathManifest,
      gateway_committed_token_ids: Sequence[int],
      expected_next_position: int,
      lease_expires_at: float,
      node_for_placement: Callable[[str], str],
      installers: Mapping[str, Callable[[int], bool]],
   ) -> dict[str, Any]:
      started = time.monotonic()
      with self._lock:
         state = self._requests.get(request_id)
      if state is None or state.finished:
         return self._result(None, "route_watermark_unavailable", gateway_committed_token_ids, started)
      coverage = state.coverage
      # The failed source is fenced by authority, never trusted to stop and
      # never sent anything again: a message to it could only sit undelivered
      # and block the exact cleanup proof of whichever path owns the request.
      with self._lock:
         if coverage.source_node_id != self.node_id:
            state.participants.discard(coverage.source_node_id)
      watermark = self._agent.route_watermark(request_id)
      gateway_count = len(gateway_committed_token_ids)
      gateway_digest = committed_prefix_digest(request_id, gateway_committed_token_ids)
      # The failed attempt is fenced first, durably, whatever happens next.
      try:
         fenced = self._fences.advance(request_id, expected_fencing_generation=state.generation)
      except FenceStoreError:
         return self._finish_fallback(state, request_id, old_manifest, None, "fence_not_installed", gateway_committed_token_ids, started, watermark)
      state.request_attempt = fenced["request_attempt"]
      state.generation = fenced["fencing_generation"]
      with self._lock:
         self._fence_advances += 1
      # The failed attempt is now excluded by authority: stop its transport
      # forwards too, whether this cutover succeeds or falls back.
      self._fence_old_path(state, request_id, old_manifest)
      compatibility = self._compatibility(coverage)
      qualifier_current = standby_qualification_is_current(coverage.qualification, now_unix_ms=self._clock_ms())
      pre = evaluate_cutover_eligibility(
         route_watermark=watermark,
         gateway_committed_token_count=gateway_count,
         gateway_committed_prefix_digest=gateway_digest,
         compatibility=compatibility,
         qualifier_current=qualifier_current,
         fence_installed=True,
         a6_available=self._a6_available,
      )
      if not pre["kv_successor_eligible"]:
         return self._finish_fallback(state, request_id, old_manifest, None, pre["reason"], gateway_committed_token_ids, started, watermark)

      # Fence-install on every current participant of the successor path.
      replaced = {coverage.source_placement_id: coverage.standby_placement_id}
      messages: list[tuple[str, KVSuccessorCutover]] = []
      for old_hop, new_hop in zip(old_manifest.ordered_hops, successor_manifest.ordered_hops, strict=True):
         if replaced.get(old_hop.placement_id) == new_hop.placement_id:
            role = "standby_import"
         elif old_hop.placement_id == new_hop.placement_id:
            role = "surviving_rekey"
         else:
            return self._finish_fallback(state, request_id, old_manifest, successor_manifest, "successor_path_mismatch", gateway_committed_token_ids, started, watermark)
         messages.append(
            (
               node_for_placement(new_hop.placement_id),
               KVSuccessorCutover(
                  request_id=request_id,
                  old_path_id=old_manifest.path_id,
                  old_path_attempt=old_manifest.path_attempt,
                  successor_path_id=successor_manifest.path_id,
                  successor_path_attempt=successor_manifest.path_attempt,
                  fencing_generation=state.generation,
                  placement_id=new_hop.placement_id,
                  role=role,
                  expected_next_position=expected_next_position,
                  lease_expires_at=lease_expires_at,
               ),
            )
         )
      sent_at = time.monotonic()
      for destination, message in messages:
         state.participants.add(destination)
         try:
            self._send(destination, message, b"")
         except Exception:
            return self._finish_fallback(state, request_id, old_manifest, successor_manifest, "fence_not_installed", gateway_committed_token_ids, started, watermark)
      expected = {message.placement_id for _destination, message in messages}
      deadline = time.monotonic() + self._receipt_timeout
      receipts: dict[str, Any] = {}
      while True:
         for receipt in self._agent.cutover_receipts(request_id):
            if (
               receipt.successor_path_id == successor_manifest.path_id
               and receipt.successor_path_attempt == successor_manifest.path_attempt
               and receipt.fencing_generation == state.generation
               and receipt.placement_id in expected
            ):
               receipts.setdefault(receipt.placement_id, receipt)
               if receipt.placement_id == coverage.standby_placement_id and state.standby_receipt_rtt_ms is None:
                  state.standby_receipt_rtt_ms = round((time.monotonic() - sent_at) * 1000.0, 3)
         if set(receipts) == expected or any(not item.installed for item in receipts.values()):
            break
         if time.monotonic() >= deadline:
            break
         time.sleep(self._poll)
      if set(receipts) != expected:
         return self._finish_fallback(state, request_id, old_manifest, successor_manifest, "fence_not_installed", gateway_committed_token_ids, started, watermark)
      refused = [item for item in receipts.values() if not item.installed]
      if refused:
         reason = "compatibility_incompatible" if any(item.reason.startswith(("kv_checkpoint", "shadow", "kv_")) for item in refused) else "fence_not_installed"
         return self._finish_fallback(state, request_id, old_manifest, successor_manifest, reason, gateway_committed_token_ids, started, watermark)
      confirmed = all(name in installers and installers[name](state.generation) is True for name in ENTRY_INSTALLERS)
      final = evaluate_cutover_eligibility(
         route_watermark=watermark,
         gateway_committed_token_count=gateway_count,
         gateway_committed_prefix_digest=gateway_digest,
         compatibility=compatibility,
         qualifier_current=standby_qualification_is_current(coverage.qualification, now_unix_ms=self._clock_ms()),
         fence_installed=confirmed and self._fences.accepts(request_id, state.generation),
         a6_available=self._a6_available,
      )
      if not final["kv_successor_eligible"]:
         return self._finish_fallback(state, request_id, old_manifest, successor_manifest, final["reason"], gateway_committed_token_ids, started, watermark)
      try:
         self._fences.grant_publication(request_id, generation=state.generation)
      except FenceStoreError:
         return self._finish_fallback(state, request_id, old_manifest, successor_manifest, "fence_not_installed", gateway_committed_token_ids, started, watermark)
      standby_receipt = next(item for item in receipts.values() if item.placement_id == coverage.standby_placement_id)
      # The old attempt is fenced everywhere it may still exist; shadow data is spent.
      self._release_participants(state, request_id, old_manifest, "fenced")
      with self._lock:
         state.path_id = successor_manifest.path_id
         state.path_attempt = successor_manifest.path_attempt
         state.pending_since = None
         # The successor path has no further standby: coverage ends here, and
         # a second failure belongs to A6/abort.
         state.finished = True
      result = self._result(state, "eligible", gateway_committed_token_ids, started, outcome="fenced_kv_successor", watermark=watermark)
      result["successor_checkpoint_digest"] = standby_receipt.checkpoint_digest
      result["successor_kv_position"] = expected_next_position
      state.result = result
      # Off the decode-resume path: the terminal release persists it again, synchronously.
      self._persist_last_cutover(request_id, background=True)
      return result

   def decline(self, request_id: str, old_manifest: PathManifest, *, reason: str, gateway_committed_token_ids: Sequence[int]) -> dict[str, Any]:
      """A covered failure the entry cannot cut over (e.g. no successor path)."""

      started = time.monotonic()
      with self._lock:
         state = self._requests.get(request_id)
         if state is not None and state.coverage.source_node_id != self.node_id:
            state.participants.discard(state.coverage.source_node_id)
      if state is None:
         return self._result(None, reason, gateway_committed_token_ids, started)
      watermark = self._agent.route_watermark(request_id)
      return self._finish_fallback(state, request_id, old_manifest, None, reason, gateway_committed_token_ids, started, watermark)

   def _release_participants(self, state: _Coverage, request_id: str, manifest: PathManifest, reason: str) -> None:
      release = KVShadowRelease(request_id=request_id, path_id=manifest.path_id, path_attempt=manifest.path_attempt, reason=reason)

      def deliver(destination: str, attempts: int) -> None:
         for attempt in range(attempts):
            try:
               self._send(destination, release, b"")
               return
            except Exception as exc:
               code = str(getattr(exc, "code", None) or type(exc).__name__)[:64]
            if attempt + 1 < attempts:
               time.sleep(_RELEASE_RETRY_SECONDS * (attempt + 1))
         # An unreachable participant is fenced by authority, not trusted to
         # stop; the undelivered release is still recorded, never silent.
         with self._lock:
            if code in self._release_failures or len(self._release_failures) < 32:
               self._release_failures[code] = self._release_failures.get(code, 0) + 1

      for destination in sorted(state.participants):
         if self._release_async and destination != self.node_id:
            # Off the request path: a briefly unreachable peer gets a bounded retry.
            threading.Thread(target=deliver, args=(destination, _RELEASE_ATTEMPTS), name="a7-release", daemon=True).start()
         else:
            deliver(destination, 1)

   def release_failures(self) -> dict[str, int]:
      """Shadow releases this entry could not deliver, by transport failure code."""

      with self._lock:
         return dict(self._release_failures)

   def _finish_fallback(self, state, request_id, old_manifest, successor_manifest, reason, gateway_tokens, started, watermark) -> dict[str, Any]:
      self._fence_old_path(state, request_id, old_manifest)
      if successor_manifest is not None:
         self._release_participants(state, request_id, successor_manifest, "fallback")
      self._release_participants(state, request_id, old_manifest, "fallback")
      self._agent.release_entry(request_id)
      outcome = "full_context_replay" if self._a6_available else "aborted"
      with self._lock:
         state.finished = True
         state.pending_since = None
      result = self._result(state, reason, gateway_tokens, started, outcome=outcome, watermark=watermark)
      state.result = result
      self._persist_last_cutover(request_id)
      return result

   def _persist_last_cutover(self, request_id: str, *, background: bool = False) -> None:
      """Durably keep this request's cutover record beside the fence ledger.

      A route cold restart retires this process and its in-memory record; the
      restarted entry serves the persisted copy for exactly this request.  A
      write failure never changes the request's outcome.  Writes are ordered
      by the snapshot they carry: a background write never replaces a newer
      record.
      """

      snapshot = self.snapshot(request_id)
      result = None if snapshot is None else snapshot.get("result")
      if not isinstance(result, dict) or result.get("protocol") != CUTOVER_RECORD_PROTOCOL:
         return  # no cutover decision for this request: nothing to keep
      persist = getattr(self._fences, "persist_last_cutover", None)
      if not callable(persist):
         return
      with self._lock:
         self._last_cutover_sequence += 1
         sequence = self._last_cutover_sequence

      def write() -> None:
         with self._last_cutover_write_lock:
            if sequence < self._last_cutover_written:
               return
            try:
               persist(request_id, node_id=self.node_id, snapshot=snapshot)
               self._last_cutover_written = sequence
            except (FenceStoreError, OSError):
               with self._lock:
                  self._last_cutover_persist_failures += 1

      if background:
         threading.Thread(target=write, name="a7-last-cutover", daemon=True).start()
      else:
         write()

   def _result(self, state, reason, gateway_tokens, started, *, outcome=None, watermark=None) -> dict[str, Any]:
      gateway_count = len(gateway_tokens)
      acked = None if watermark is None else int(watermark["committed_token_count"])
      return {
         "protocol": CUTOVER_RECORD_PROTOCOL,
         "outcome": outcome or ("full_context_replay" if self._a6_available else "aborted"),
         "reason": reason,
         "request_attempt": None if state is None else state.request_attempt,
         "fencing_generation": None if state is None else state.generation,
         "gateway_committed_token_count": gateway_count,
         "acknowledged_token_count": acked,
         "acknowledgement_lag": None if acked is None else gateway_count - acked,
         "commits_sent": 0 if state is None else state.commits_sent,
         "ack_lag_events": 0 if state is None else state.lag_events,
         "max_ack_wait_ms": 0.0 if state is None else round(state.max_ack_wait_ms, 3),
         "cutover_ms": round((time.monotonic() - started) * 1000.0, 3),
         "kv_continuity_claimed": outcome == "fenced_kv_successor",
         "recovery_prefill": False if outcome == "fenced_kv_successor" else None,
      }

   # -- terminal --------------------------------------------------------------------

   def finish(self, request_id: str, manifest: PathManifest, *, outcome: str) -> None:
      """Record the terminal outcome once and release every participant exactly once."""

      with self._lock:
         state = self._requests.get(request_id)
         if state is None or (state.result is not None and state.result.get("terminal_released")):
            return
         state.finished = True
         state.pending_since = None
         state.result = {**(state.result or {}), "terminal_released": True, "terminal_outcome": outcome}
      self._persist_last_cutover(request_id)
      if state.result.get("outcome") == "aborted":
         outcome = "aborted"
      try:
         self._fences.mark_terminal(request_id, generation=state.generation, outcome=outcome)
      except FenceStoreError:
         pass
      reason = outcome if outcome in {"completed", "cancelled", "failed", "aborted"} else "failed"
      self._release_participants(state, request_id, manifest, reason)
      self._agent.release_entry(request_id)
      try:
         self._fences.release(request_id)
      except FenceStoreError:
         # A record that could not be marked terminal stays for restart
         # reconciliation; it is never silently dropped.
         pass

   def fence_record_count(self) -> int:
      """Durable fence records this entry currently holds (terminal records are released)."""

      return self._fences.record_count()

   def product_evidence(self) -> dict[str, Any] | None:
      """Privacy-reduced evidence of the most recent covered request (product projection).

      Every value is this entry's own observation: counts, generations,
      timings measured here, and the request's cutover decision.  ``None``
      before any covered request.
      """

      with self._lock:
         request_id = self._last_request_id
         state = None if request_id is None else self._requests.get(request_id)
         if state is None:
            return None
         waits = sorted(state.ack_waits_ms)
         document = {
            "committed": state.committed,
            "ack_wait_median_ms": None if not waits else round(
               waits[len(waits) // 2] if len(waits) % 2 else (waits[len(waits) // 2 - 1] + waits[len(waits) // 2]) / 2.0, 3
            ),
            "duplicate_commits": state.duplicate_commits,
            "fencing_generation": state.generation,
            "fence_advances": self._fence_advances,
            "standby_receipt_rtt_ms": state.standby_receipt_rtt_ms,
            "outcome": None if state.result is None else state.result.get("outcome"),
            "request_key": "sha256:" + request_key(request_id),
         }
      if state.last_ack_rtt_ms is None:
         self._note_ack(state, request_id)
      document["last_ack_rtt_ms"] = state.last_ack_rtt_ms
      document["last_ack_checkpoint_sequence"] = state.last_ack_sequence
      return document

   def snapshot(self, request_id: str) -> dict[str, Any] | None:
      with self._lock:
         state = self._requests.get(request_id)
      if state is None:
         # Read outside the controller lock, so a slow disk never holds up the request path.
         return self._persisted_snapshot(request_id)
      with self._lock:
         return {
            "covered": True,
            "committed": state.committed,
            "acknowledged": self._acknowledged(request_id),
            "commits_sent": state.commits_sent,
            "ack_lag_events": state.lag_events,
            "request_attempt": state.request_attempt,
            "fencing_generation": state.generation,
            "finished": state.finished,
            "last_send_error": state.last_send_error,
            "source_lost_accepted": state.source_lost_accepted,
            "old_path_fenced": state.old_path_fenced,
            "result": None if state.result is None else dict(state.result),
         }

   def _persisted_snapshot(self, request_id: str) -> dict[str, Any] | None:
      """The durable last cutover record of exactly this request, when this process has none.

      Served only for the same request digest and entry node; it is the
      record as observed when it was written, marked as restored, never
      recomputed or invented.
      """

      reader = getattr(self._fences, "last_cutover", None)
      if not callable(reader):
         return None
      try:
         snapshot = reader(request_id, node_id=self.node_id)
      except (FenceStoreError, OSError):
         return None
      if not isinstance(snapshot, dict) or not isinstance(snapshot.get("result"), dict):
         return None
      if snapshot["result"].get("protocol") != CUTOVER_RECORD_PROTOCOL:
         return None
      return {**snapshot, "restored_from": LAST_CUTOVER_SOURCE}
