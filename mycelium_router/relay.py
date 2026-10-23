# SPDX-License-Identifier: AGPL-3.0-only
"""Inter-layer relay execution over an already locked path manifest."""

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
from threading import Lock, RLock
from typing import Any, Iterator

from mycelium_router import a10_batch_producer
from mycelium_router.batching import PhaseAwareBatchController
from mycelium_router.contracts import (
   BatchExecutionObservation,
   BatchNetworkStats,
   ExecutionGraph,
   FailureReport,
   HopHeader,
   HopReceiveResult,
   HopWorkItem,
   ManifestDelta,
   ManifestLocked,
   PathCancellation,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   ProgressivePrefillResult,
   RelayOutcome,
   RequestContext,
   RuntimeBatchKey,
   RuntimeResult,
   TokenEvent,
   VerifyEvent,
)
from mycelium_router.payloads import PayloadError, decode_token_ids
from mycelium_router.hop_join import HopArrivalTracker
from mycelium_router.idempotency import (
   hop_identity,
   hop_idempotency_key,
   payload_fingerprint,
   snapshot_payload,
)
from mycelium_router.routing import RoutingError
from mycelium_router.scheduler import (
   BackpressureError,
   DuplicateHopError,
   HopScheduler,
)
from mycelium_router.state import HopStateMachine
from mycelium_router.validation import validate_manifest


_MAX_TERMINAL_PATH_METADATA = 4096
_MAX_PENDING_PATH_CANCELLATIONS = 4096
#: Owners remembered per path id (live and tombstoned paths).
_MAX_PATH_OWNERS = 2 * _MAX_TERMINAL_PATH_METADATA
#: Path-table invariants (see the design note RELAY-PATH-TABLES.md):
#: every tombstone is charged to the authenticated node that created the path
#: it retires (the entry for its own manifest, the sender of a progressive
#: message, the entry for its own consumed pending cancellation, or this node
#: for local authority). A record binds registrations of its owner by the
#: owner's entry and local authority only when its creator is that entry or
#: local authority; otherwise it binds only the same creator. A creator holds
#: at most ``_TOMBSTONE_QUOTA_PER_CREATOR`` records: at its quota it registers
#: no new path id until its own records older than
#: ``_TOMBSTONE_RETENTION_SECONDS`` (relay clock) can be evicted; another
#: creator's records are never evicted, aged or marked for it. An evicted
#: record can only ever be re-registered by its own creator (records by any
#: other creator never bound by it, and only an entry can register in its own
#: name), so eviction never reopens anything for anyone else. A release
#: decided by this node or the authenticated entry binds the owner: the
#: entry's (or local) release binds every attempt up to it, and a local
#: release of a path another participant created also retires that exact
#: attempt for the owner (never a high-water mark: the attempt number was
#: the creator's claim). At most ``_MAX_TOMBSTONE_CREATORS`` creators hold a
#: slot (records or live paths; this node never needs one). A new creator
#: past the cap reclaims the least recently active idle slot -- no live path
#: and every record past the retention horizon -- and is refused when none
#: is idle. The shared replay filter is written only by local authority
#: (unknown-owner tombstones), so no peer drives its rotation.
_TOMBSTONE_RETENTION_SECONDS = 600.0
#: A live (registered or provisional) path with no traffic and no work in
#: flight for this long (relay clock) is retired as a local release, which
#: binds its owner: that attempt never registers again. The longest legitimate
#: idle gap at a relay is one decode cycle (two 240 s confirmed-delivery
#: budgets plus the A7 ack wait, ~482 s) or a sequential ManifestLocked chain
#: before prefill, and the controller's overhead between commands is bounded
#: only by the 930 s route session timeout: so the horizon is that session
#: timeout plus one 240 s delivery budget plus a 30 s margin
#: (``tests/path_cancellation_adversarial`` derives and pins it).
_LIVE_PATH_IDLE_SECONDS = 1200.0
#: The absolute request deadline the route stamps on every physical command:
#: ``issued_at_ms + 3_600_000`` at ``mycelium_live/router_port.py:694`` (and the
#: legacy fallback at ``mycelium_live/route.py:4541``). It is the only
#: machine-enforced wall-clock bound on one command's life: the node's command
#: controller retires a command at it (``command_controller._expire_if_due``),
#: and an A6/A7 recovery successor inherits the same deadline rather than
#: minting a new one (``router_port.py:1551``).
_REQUEST_ABSOLUTE_DEADLINE_SECONDS = 3_600.0
#: An absolute per-registration lifetime ceiling, measured on the relay clock
#: from a path id's *first* registration and never refreshed by traffic, in
#: flight work, or queued work.  Idle expiry alone is a resource-cap defeat: a
#: creator that keeps a path warm (valid hops, queued work, or replayed
#: admitted frames) defeats it and holds its live-path slot and creator slot
#: forever.  The ceiling is the composition of the two enforced bounds above:
#: the command's absolute deadline plus one idle horizon, i.e. the longest a
#: legitimate path may stay quiet and still be inside its command's life.
#: ``tests/path_cancellation_adversarial`` pins both the derivation and the
#: ``3_600_000`` literal it is read from.
_LIVE_PATH_MAX_LIFETIME_SECONDS = (
   _REQUEST_ABSOLUTE_DEADLINE_SECONDS + _LIVE_PATH_IDLE_SECONDS
)
_TOMBSTONE_QUOTA_PER_CREATOR = 8192
_MAX_TOMBSTONE_CREATORS = 64
#: Pending tombstones per authenticated source. A source over its quota
#: displaces only its own oldest entry, so no peer can push out another
#: source's pending tombstone; past the absolute bound the relay fails closed
#: and refuses new pending cancellations.
_MAX_PENDING_PER_SOURCE = _MAX_PENDING_PATH_CANCELLATIONS
_MAX_PENDING_TOTAL = 16 * _MAX_PENDING_PATH_CANCELLATIONS
#: ``_record_cancelled_attempt_locked`` default: the path's recorded value.
_RECORDED_OWNER = object()
#: Local replay-filter attempt marking "every attempt of this path id" (an
#: evicted local unknown-owner record).
_EVICTED_ALL_ATTEMPTS = -(1 << 62)

#: A path id's owner: (entry node id or None, request id). A path id is bound
#: to the owner of its first registration while it is live, and its
#: tombstones apply only to that owner: another entry or request can neither
#: replace a live registration nor be blocked by tombstones it did not cause.
PathOwner = tuple["str | None", str]


#: A tombstone's binding class: ``""`` for the owner's entry or local
#: authority (binds every registration of the owner by them), ``("=", n)``
#: for a local release of exactly attempt ``n`` of a path another
#: participant created (binds the owner for that attempt only), otherwise the
#: non-authoritative creator itself (binds only its own registrations).
TombstoneKey = tuple[str, "PathOwner | None", "str | tuple[str, int]"]


def _same_owner(left: "PathOwner", right: "PathOwner") -> bool:
   """Exact owner equality: a registration without a known entry inherits
   the recorded owner explicitly (``_resolve_owner_locked``), never by a
   wildcard that would let any entry match an entry-less owner."""
   return left == right


def _hop_key(message: Any) -> tuple[str, str, int, str, int, int]:
   """Structured cache key of a hop (``hop_identity``)."""
   return hop_identity(message)
_CANCELLED_PATH_FILTER_BYTES = 1 << 20
_CANCELLED_PATH_FILTER_HASHES = 5
_CANCELLED_PATH_FILTER_ROTATION_CAPACITY = 100_000


class _RotatingReplayFilter:
   """Bounded two-window replay filter with a stable false-positive rate."""

   def __init__(
      self,
      *,
      byte_count: int,
      hashes: int,
      rotation_capacity: int,
   ) -> None:
      if byte_count <= 0 or hashes <= 0 or rotation_capacity <= 0:
         raise ValueError("invalid_replay_filter_configuration")
      self._byte_count = byte_count
      self._hashes = hashes
      self._rotation_capacity = rotation_capacity
      self._current = bytearray(byte_count)
      self._previous = bytearray(byte_count)
      self._current_insertions = 0

   def _positions(self, path_id: str, path_attempt: int) -> tuple[int, ...]:
      digest = hashlib.blake2b(
         f"{path_id}\x00{path_attempt}".encode("utf-8"),
         digest_size=16,
      ).digest()
      first = int.from_bytes(digest[:8], "big")
      second = int.from_bytes(digest[8:], "big") | 1
      bit_count = self._byte_count * 8
      return tuple(
         (first + index * second) % bit_count
         for index in range(self._hashes)
      )

   @staticmethod
   def _contains(window: bytearray, positions: tuple[int, ...]) -> bool:
      return all(
         window[position // 8] & (1 << (position % 8))
         for position in positions
      )

   def add(self, path_id: str, path_attempt: int) -> None:
      if self._current_insertions >= self._rotation_capacity:
         self._previous = self._current
         self._current = bytearray(self._byte_count)
         self._current_insertions = 0
      for position in self._positions(path_id, path_attempt):
         self._current[position // 8] |= 1 << (position % 8)
      self._current_insertions += 1

   def contains(self, path_id: str, path_attempt: int) -> bool:
      positions = self._positions(path_id, path_attempt)
      return self._contains(self._current, positions) or self._contains(
         self._previous,
         positions,
      )


#: A11: one VERIFY feeds the pending token plus at most 16 candidates.
MAX_VERIFY_SPAN = 17


class RelayEngine:
   def __init__(
      self,
      *,
      node_id,
      runtime,
      transport,
      scheduler,
      clock,
      builder,
      device_states,
   ):
      self.node_id = node_id
      self.runtime = runtime
      # Default absent. Only a trusted native grant installer may attach it.
      self.token_path_gate = None
      self.decode_mode = getattr(runtime, "decode_mode", "complete_context_replay")
      if self.decode_mode not in {"complete_context_replay", "stage_local_kv"}:
         raise ValueError("invalid_runtime_decode_mode")
      self.transport = transport
      self.scheduler = scheduler
      self.batch_scheduler = HopScheduler(scheduler.config)
      self.batch_controller = PhaseAwareBatchController(scheduler.config)
      self.clock = clock
      self.builder = builder
      self.device_states = device_states
      self._outcomes: dict[
         tuple[str, int, str, int],
         tuple[RelayOutcome, float, str, str],
      ] = {}
      self._paths: dict[
         str,
         tuple[ExecutionGraph, PathManifest, RequestContext],
      ] = {}
      self._path_lock = RLock()
      # Operations for one path must serialize, but unrelated paths must never
      # share a lock: cancellation owns a fixed two-second proof budget and
      # cannot wait behind a slow request that merely hashes to the same stripe.
      # References include holders and waiters, so an idle entry can be removed
      # without replacing the lock while another thread is about to acquire it.
      self._path_operation_registry_lock = Lock()
      self._path_operation_locks: dict[str, tuple[Lock, int]] = {}
      self._path_generations: OrderedDict[str, int] = OrderedDict()
      # Never reuse a permit after bounded tombstone metadata is evicted.
      self._generation_sequence = 0
      # Highest cancelled attempt per (path id, owner, binding class); owner
      # None is a local unknown-owner tombstone, which binds everyone. One
      # record never lowers, replaces or evicts another's.
      self._cancelled_path_attempts: OrderedDict[TombstoneKey, int] = OrderedDict()
      self._cancelled_path_owners: dict[str, set[tuple[PathOwner | None, str]]] = {}
      self._cancelled_recorded_at: dict[TombstoneKey, float] = {}
      self._cancelled_creator: dict[TombstoneKey, str] = {}
      self._tombstones_by_creator: dict[str, OrderedDict[TombstoneKey, None]] = {}
      self._path_creators: OrderedDict[str, str] = OrderedDict()

      self._pending_by_source: dict[str, int] = {}
      self._path_owners: OrderedDict[str, PathOwner] = OrderedDict()
      self._cancelled_path_filter = _RotatingReplayFilter(
         byte_count=_CANCELLED_PATH_FILTER_BYTES,
         hashes=_CANCELLED_PATH_FILTER_HASHES,
         rotation_capacity=_CANCELLED_PATH_FILTER_ROTATION_CAPACITY,
      )
      self._pending_path_cancellations: OrderedDict[
         tuple[str, str, int, int, str],
         None,
      ] = OrderedDict()
      self._provisional_paths: dict[
         str,
         tuple[str, int, int, str | None],
      ] = {}
      self._entry_node_by_path: dict[str, str] = {}
      self._hop_results: dict[
         tuple[str, str, int, str, int, int],
         tuple[HopReceiveResult, float, str, str],
      ] = {}
      self._prefill_results: dict[
         tuple[str, str, int, str, int, int],
         tuple[ProgressivePrefillResult, float, str, str],
      ] = {}
      self._pending_hops: dict[
         tuple[str, str, int, str, int, int],
         tuple[HopHeader, HopWorkItem, str, int, str | None],
      ] = {}
      # One drainer at a time: a drain owns runtime dispatch and forwarding for
      # the batches it pops.  Ingress never waits for it (``blocking=False``).
      self._drain_lock = Lock()
      # Counts accepted enqueues, so a finishing pump can tell whether work
      # arrived after its last look at the queue (see ``pump_ready_batches``).
      self._enqueue_sequence = 0
      # Per-path hop arrivals at this node: which other requests could still
      # join a live collection window (see ``hop_join_partners_due``).
      self._hop_arrivals = HopArrivalTracker(
         join_horizon_seconds=max(
            1e-3, float(scheduler.config.live_hop_join_horizon_seconds)
         )
      )
      # Idle live-path expiry: last traffic per live path (oldest first) and
      # operations in flight per path (an executing path is never idle).
      self._path_activity: OrderedDict[str, float] = OrderedDict()
      self._path_busy: dict[str, int] = {}
      # Absolute lifetime: a path id's *first* registration on this node.  It
      # is never refreshed, not by traffic, not by queued or in-flight work,
      # and not by a later attempt under the same path id, so no amount of
      # activity can hold a path (and its creator slot) past the ceiling.
      self._path_registered_at: dict[str, float] = {}
      # No successor may publish while old path-id cleanup is in progress.
      self._retiring_paths: set[str] = set()

   def _touch_path_locked(self, path_id: str) -> None:
      """Record traffic for a live path (no-op for an unknown one)."""
      if path_id in self._paths or path_id in self._provisional_paths:
         self._path_activity[path_id] = self.clock.now()
         self._path_activity.move_to_end(path_id)

   @contextmanager
   def _path_in_flight(self, path_id: str) -> Iterator[None]:
      """Locally admitted work, fenced to the generation at entry."""
      with self._admitted_path_activity(path_id) as admit:
         admit()
         yield

   @contextmanager
   def _admitted_path_activity(self, path_id: str):
      """Yield a marker called after scheduler admission and generation check.

      Refusals never refresh or hold busy state. Old finalizers cannot touch
      newer registrations or decrement their busy counts.
      """
      with self._path_lock:
         generation = self._path_generations.get(path_id, 0)
      admitted = False
      def admit():
         nonlocal admitted
         with self._path_lock:
            if admitted or self._path_generations.get(path_id, 0) != generation:
               return
            admitted = True
            self._path_busy[path_id] = self._path_busy.get(path_id, 0) + 1
            self._refresh_path_locked(path_id)
      try:
         yield admit
      finally:
         with self._path_lock:
            first = self._path_registered_at.get(path_id)
            absolute_due = first is not None and self.clock.now() - first >= _LIVE_PATH_MAX_LIFETIME_SECONDS
            if admitted and self._path_generations.get(path_id, 0) == generation:
               remaining = self._path_busy.get(path_id, 1) - 1
               if remaining > 0:
                  self._path_busy[path_id] = remaining
               else:
                  self._path_busy.pop(path_id, None)
               self._refresh_path_locked(path_id)
         if absolute_due:
            self.expire_idle_paths()

   def _refresh_path_locked(self, path_id: str) -> None:
      """``_touch_path_locked`` that a lifetime ceiling can veto."""
      first = self._path_registered_at.get(path_id)
      if first is not None and self.clock.now() - first >= _LIVE_PATH_MAX_LIFETIME_SECONDS:
         return
      self._touch_path_locked(path_id)

   def expire_idle_paths(self) -> tuple[str, ...]:
      """Retire every live path past either bound, as a local release.

      Two independent retirements, and each owner stays bound for that exact
      attempt (exactly what a local release does):

      * **idle** — no traffic, no operation in flight and no queued hop for
        ``_LIVE_PATH_IDLE_SECONDS``;
      * **absolute** — older than ``_LIVE_PATH_MAX_LIFETIME_SECONDS`` since its
        first registration on this node, whatever it is doing.  This one is not
        deferred by queued or in-flight work, and it is not refreshed by a
        later attempt under the same path id, so nothing an authorized
        participant does can hold a live-path or creator slot past the bound.
      """
      now = self.clock.now()
      expired: list[tuple[str, Any]] = []
      with self._path_lock:
         queued = {value[1].path_id for value in self._pending_hops.values()}
         if self._path_activity:
            for path_id, last in tuple(self._path_activity.items()):
               # ``_path_activity`` is ordered oldest first, so the first
               # still-warm path ends the idle sweep.
               if now - last < _LIVE_PATH_IDLE_SECONDS:
                  break
               if path_id not in self._paths and path_id not in self._provisional_paths:
                  self._path_activity.pop(path_id, None)
                  continue
               if self._path_busy.get(path_id) or path_id in queued:
                  self._touch_path_locked(path_id)
                  continue
               registered = self._paths.get(path_id)
               self._release_path_state_locked(path_id)
               self._path_activity.pop(path_id, None)
               expired.append((path_id, registered))
         # The ceiling sweep is not ordered by last activity (a path past the
         # ceiling may have been touched seconds ago), so it scans every live
         # path.  Already-expired ids are absent from the tables and skipped.
         for path_id, first in tuple(self._path_registered_at.items()):
            if now - first < _LIVE_PATH_MAX_LIFETIME_SECONDS:
               continue
            if path_id not in self._paths and path_id not in self._provisional_paths:
               self._path_registered_at.pop(path_id, None)
               continue
            if any(seen == path_id for seen, _ in expired):
               continue
            registered = self._paths.get(path_id)
            self._release_path_state_locked(path_id)
            self._path_activity.pop(path_id, None)
            self._path_registered_at.pop(path_id, None)
            expired.append((path_id, registered))
         self._retiring_paths.update(path_id for path_id, _ in expired)
      for path_id, registered in expired:
         try:
            self._release_synchronized_capacity(registered)
            self._release_path_resources(path_id)
         finally:
            with self._path_lock:
               self._retiring_paths.discard(path_id)
      return tuple(path_id for path_id, _ in expired)

   def entry_node_for_path(self, path_id: str) -> str | None:
      """The entry node that registered ``path_id`` here (``None`` once released or unknown)."""
      with self._path_lock:
         return self._entry_node_by_path.get(path_id)

   def path_generation(self, path_id: str) -> int:
      """Return the generation used to fence entry-side dispatches."""
      with self._path_lock:
         return self._path_generations.get(path_id, 0)

   def _advance_generation_locked(self, path_id: str) -> None:
      self._generation_sequence += 1
      self._path_generations[path_id] = self._generation_sequence
      self._path_generations.move_to_end(path_id)

   def _path_generation(self, path_id: str) -> int:
      return self.path_generation(path_id)

   def _binding_class(self, owner: "PathOwner | None", creator: str) -> str:
      if owner is None or creator == self.node_id or creator == owner[0]:
         return ""
      return creator

   def _evict_tombstone_locked(self, key: TombstoneKey) -> None:
      """Drop one of a creator's own old records. A local unknown-owner
      record leaves an every-attempt mark in the local-only filter (it bound
      everyone, so it must keep refusing); an owner-scoped record bound only
      its creator's own registrations, and any other creator's never."""
      path_id, owner, _ = key
      self._cancelled_path_attempts.pop(key, None)
      creator = self._cancelled_creator.pop(key, self.node_id)
      self._cancelled_recorded_at.pop(key, None)
      if owner is None:
         self._mark_cancelled_attempt_locked(path_id, _EVICTED_ALL_ATTEMPTS)
      records = self._tombstones_by_creator.get(creator)
      if records is not None:
         records.pop(key, None)
         if not records:
            self._tombstones_by_creator.pop(creator, None)
      owners = self._cancelled_path_owners.get(path_id)
      if owners is not None:
         owners.discard((owner, key[2]))
         if owners:
            return
         self._cancelled_path_owners.pop(path_id, None)
      if path_id not in self._paths and path_id not in self._provisional_paths:
         self._path_generations.pop(path_id, None)

   def _prune_creator_tombstones_locked(self, creator: str, limit: int) -> None:
      """Evict a creator's own records past the retention horizon while it
      holds more than ``limit``; never another creator's, never a young one."""
      records = self._tombstones_by_creator.get(creator)
      now = self.clock.now()
      while records and len(records) > limit:
         oldest = next(iter(records))
         if now - self._cancelled_recorded_at.get(oldest, now) < _TOMBSTONE_RETENTION_SECONDS:
            break
         self._evict_tombstone_locked(oldest)

   def _live_creators_locked(self) -> set[str]:
      return {
         creator
         for path_id in (*self._paths, *self._provisional_paths)
         if (creator := self._path_creators.get(path_id)) is not None
      }

   def _creator_slots_locked(self) -> set[str]:
      """Creators holding a slot: records or a live path (never this node)."""
      slots = set(self._tombstones_by_creator) | self._live_creators_locked()
      slots.discard(self.node_id)
      return slots

   def _reclaim_idle_creator_slot_locked(self) -> bool:
      """Free the least recently active idle slot: a creator with no live
      path whose every record is past the retention horizon. Its records are
      evicted as its own quota would evict them (they bound others only for
      deliveries older than the horizon)."""
      now = self.clock.now()
      live = self._live_creators_locked()
      idle: tuple[float, str] | None = None
      for creator, records in self._tombstones_by_creator.items():
         if creator == self.node_id or creator in live or not records:
            continue
         newest = max(self._cancelled_recorded_at.get(key, now) for key in records)
         if now - newest < _TOMBSTONE_RETENTION_SECONDS:
            continue
         if idle is None or newest < idle[0]:
            idle = (newest, creator)
      if idle is None:
         return False
      for key in tuple(self._tombstones_by_creator.get(idle[1], ())):
         self._evict_tombstone_locked(key)
      return True

   def _creator_may_register_locked(self, creator: str) -> bool:
      """Fail closed for this creator only: at its quota (after evicting its
      own records older than the retention horizon), or as a new creator past
      the creator cap when no idle slot can be reclaimed (a slot with a live
      path or a record younger than the horizon is never taken). This node
      never needs a slot."""
      records = self._tombstones_by_creator.get(creator)
      if records is not None and len(records) >= _TOMBSTONE_QUOTA_PER_CREATOR:
         self._prune_creator_tombstones_locked(creator, _TOMBSTONE_QUOTA_PER_CREATOR - 1)
         records = self._tombstones_by_creator.get(creator)
         if records is not None and len(records) >= _TOMBSTONE_QUOTA_PER_CREATOR:
            return False
      if creator == self.node_id:
         return True
      slots = self._creator_slots_locked()
      if creator in slots or len(slots) < _MAX_TOMBSTONE_CREATORS:
         return True
      return self._reclaim_idle_creator_slot_locked()

   def _prune_terminal_path_metadata_locked(self) -> None:
      for table in (self._path_owners, self._path_creators):
         while len(table) > _MAX_PATH_OWNERS:
            path_id, value = table.popitem(last=False)
            if path_id in self._paths or path_id in self._provisional_paths:
               # Never forget a live path's metadata; keep it, newest last.
               table[path_id] = value
               break

   def _resolve_owner_locked(
      self,
      path_id: str,
      request_id: str,
      entry_node_id: str | None,
   ) -> PathOwner:
      """The owner a registration claims; without an entry it inherits the
      recorded entry of the same request."""
      if entry_node_id is None:
         recorded = self._path_owners.get(path_id)
         if recorded is not None and recorded[1] == request_id:
            return recorded
      return (entry_node_id, request_id)

   def _record_owner_locked(self, path_id: str, owner: PathOwner) -> None:
      recorded = self._path_owners.get(path_id)
      if recorded is not None and recorded[0] is not None and owner[0] is None:
         owner = recorded
      self._path_owners[path_id] = owner
      self._path_owners.move_to_end(path_id)

   def _record_cancelled_attempt_locked(
      self,
      path_id: str,
      path_attempt: int,
      owner: "PathOwner | None | object" = _RECORDED_OWNER,
      creator: str | None = None,
      *,
      authority: str | None = None,
      exact: bool = False,
   ) -> None:
      """Record a tombstone charged to ``creator`` (default: the path's
      authenticated creator, else local authority). Its binding class is
      decided by ``authority`` (who released it; default: the creator), or
      is the exact-attempt class when ``exact``."""
      if owner is _RECORDED_OWNER:
         owner = self._path_owners.get(path_id)
      if creator is None:
         creator = self._path_creators.get(path_id, self.node_id)
      if owner is None:
         # Local unknown-owner tombstone: the legacy key, binds everyone.
         self._mark_cancelled_attempt_locked(path_id, path_attempt)
      if exact:
         binding: "str | tuple[str, int]" = ("=", path_attempt)
      else:
         binding = self._binding_class(owner, creator if authority is None else authority)
      key = (path_id, owner, binding)
      previous = self._cancelled_path_attempts.get(key)
      if previous is not None:
         path_attempt = max(path_attempt, previous)
      self._cancelled_path_attempts[key] = path_attempt
      self._cancelled_path_attempts.move_to_end(key)
      self._cancelled_recorded_at[key] = self.clock.now()
      self._cancelled_path_owners.setdefault(path_id, set()).add((owner, key[2]))
      charged = self._cancelled_creator.setdefault(key, creator)
      records = self._tombstones_by_creator.setdefault(charged, OrderedDict())
      records[key] = None
      records.move_to_end(key)
      self._prune_creator_tombstones_locked(charged, _TOMBSTONE_QUOTA_PER_CREATOR)
      self._prune_terminal_path_metadata_locked()

   def _cancelled_attempt_for_locked(
      self,
      path_id: str,
      owner: "PathOwner | None",
      creator: str | None = None,
   ) -> int | None:
      """The highest cancelled attempt that binds a registration of ``owner``
      by ``creator``: local unknown-owner records bind everyone; an owner's
      record by its entry or local authority binds that owner (and an owner
      recorded before its entry was known, that request); a record by any
      other creator binds only that creator."""
      highest: int | None = None
      for recorded, binding in self._cancelled_path_owners.get(path_id, ()):
         if recorded is None:
            applies = True
         elif owner is None:
            applies = True
         elif isinstance(binding, tuple):
            applies = False  # exact-attempt records: see _exactly_released_locked
         else:
            same = recorded == owner or (recorded[0] is None and recorded[1] == owner[1])
            applies = same and (binding == "" or binding == creator)
         if applies:
            attempt = self._cancelled_path_attempts.get((path_id, recorded, binding))
            if attempt is not None and (highest is None or attempt > highest):
               highest = attempt
      return highest

   def _exactly_released_locked(
      self,
      path_id: str,
      owner: "PathOwner | None",
      path_attempt: int,
   ) -> bool:
      """Whether this node released exactly ``path_attempt`` of the owner's
      path (a path another participant created): that attempt never
      registers again, for anyone registering in the owner's name."""
      for recorded, binding in self._cancelled_path_owners.get(path_id, ()):
         if binding != ("=", path_attempt) or recorded is None:
            continue
         if owner is None or recorded == owner or (
            recorded[0] is None and recorded[1] == owner[1]
         ):
            return True
      return False

   @contextmanager
   def _path_operation_lock(self, path_id: str) -> Iterator[None]:
      with self._path_operation_registry_lock:
         registered = self._path_operation_locks.get(path_id)
         if registered is None:
            operation_lock = Lock()
            references = 0
         else:
            operation_lock, references = registered
         self._path_operation_locks[path_id] = (operation_lock, references + 1)
      try:
         with operation_lock:
            yield
      finally:
         with self._path_operation_registry_lock:
            current_lock, current_references = self._path_operation_locks[path_id]
            assert current_lock is operation_lock and current_references >= 1
            if current_references == 1:
               self._path_operation_locks.pop(path_id)
            else:
               self._path_operation_locks[path_id] = (
                  operation_lock,
                  current_references - 1,
               )

   def _mark_cancelled_attempt_locked(self, path_id: str, path_attempt: int) -> None:
      """Local authority only (unknown-owner tombstones): no peer ever writes
      this filter, so no peer can drive its rotation."""
      self._cancelled_path_filter.add(path_id, path_attempt)

   def _was_cancelled_attempt_locked(self, path_id: str, path_attempt: int, *_unused: object) -> bool:
      return self._cancelled_path_filter.contains(
         path_id, path_attempt
      ) or self._cancelled_path_filter.contains(path_id, _EVICTED_ALL_ATTEMPTS)

   @staticmethod
   def _pending_cancellation_key(
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str,
   ) -> tuple[str, str, int, int, str]:
      return (
         path_id,
         request_id,
         path_attempt,
         topology_version,
         entry_node_id,
      )

   def _remember_pending_cancellation_locked(
      self,
      cancellation: PathCancellation,
      source_node_id: str,
   ) -> bool:
      """Record a cancellation of a path not seen yet. A source over its
      quota displaces only its own oldest pending entry; a full table refuses
      (fail closed). No peer can push out another source's pending tombstone."""
      key = self._pending_cancellation_key(
         request_id=cancellation.request_id,
         path_id=cancellation.path_id,
         path_attempt=cancellation.path_attempt,
         topology_version=cancellation.topology_version,
         entry_node_id=source_node_id,
      )
      if key in self._pending_path_cancellations:
         self._pending_path_cancellations.move_to_end(key)
         return True
      if self._pending_by_source.get(source_node_id, 0) >= _MAX_PENDING_PER_SOURCE:
         oldest = next(k for k in self._pending_path_cancellations if k[4] == source_node_id)
         self._forget_pending_locked(oldest)
      elif len(self._pending_path_cancellations) >= _MAX_PENDING_TOTAL:
         return False
      self._pending_path_cancellations[key] = None
      self._pending_by_source[source_node_id] = self._pending_by_source.get(source_node_id, 0) + 1
      return True

   def _forget_pending_locked(self, key: tuple[str, str, int, int, str]) -> None:
      if self._pending_path_cancellations.pop(key, False) is None:
         source = key[4]
         remaining = self._pending_by_source.get(source, 0) - 1
         if remaining > 0:
            self._pending_by_source[source] = remaining
         else:
            self._pending_by_source.pop(source, None)

   def _consume_pending_cancellation_locked(
      self,
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str | None,
   ) -> bool:
      if entry_node_id is None:
         return False
      expected = self._pending_cancellation_key(
         request_id=request_id,
         path_id=path_id,
         path_attempt=path_attempt,
         topology_version=topology_version,
         entry_node_id=entry_node_id,
      )
      path_keys = tuple(
         key for key in self._pending_path_cancellations if key[0] == path_id
      )
      matched = expected in self._pending_path_cancellations
      if matched and not self._creator_may_register_locked(entry_node_id):
         # The entry has no slot for the record: keep its pending
         # cancellation (bounded per source) so the attempt stays refused.
         return True
      for key in path_keys:
         self._forget_pending_locked(key)
      if not matched:
         return False
      self._record_cancelled_attempt_locked(
         path_id, path_attempt, (entry_node_id, request_id), creator=entry_node_id
      )
      self._advance_generation_locked(path_id)
      self._prune_terminal_path_metadata_locked()
      return True

   def _path_is_current_locked(
      self,
      path_id: str,
      path_attempt: int,
      generation: int,
   ) -> bool:
      first = self._path_registered_at.get(path_id)
      if first is not None and self.clock.now() - first >= _LIVE_PATH_MAX_LIFETIME_SECONDS:
         return False
      if self._path_generations.get(path_id, 0) != generation:
         return False
      if self._was_cancelled_attempt_locked(path_id, path_attempt):
         return False
      cancelled_attempt = self._cancelled_attempt_for_locked(
         path_id, self._path_owners.get(path_id), self._path_creators.get(path_id)
      )
      return cancelled_attempt is None or path_attempt > cancelled_attempt

   def _send_if_path_current(
      self,
      path_id: str,
      path_attempt: int,
      generation: int,
      sender,
   ) -> bool:
      with self._admitted_path_activity(path_id) as admit:
         with self._path_lock:
            if not self._path_is_current_locked(path_id, path_attempt, generation):
               return False
            admit()
         # Permit acquisition is the dispatch linearization point. An already
         # permitted send cannot be retracted from an arbitrary transport.
         sender()
      return True

   def dispatch_if_current(
      self,
      *,
      path_id: str,
      path_attempt: int,
      generation: int,
      sender,
   ) -> bool:
      """Dispatch only if cancellation has not invalidated the path permit."""
      return self._send_if_path_current(
         path_id,
         path_attempt,
         generation,
         sender,
      )

   @staticmethod
   def _path_cancelled_outcome(
      request: RequestContext,
      manifest: PathManifest,
      token_index: int,
   ) -> RelayOutcome:
      return RelayOutcome(
         failure_report=FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=token_index,
            scope="REQUEST",
            reason="path_cancelled",
         )
      )

   @staticmethod
   def _runtime_unavailable() -> RuntimeResult:
      return RuntimeResult(
         success=False,
         failure_scope="PLACEMENT",
         failure_reason="worker_runtime_unavailable",
      )

   @staticmethod
   def _token_path_failure(reason: str) -> RuntimeResult:
      return RuntimeResult(success=False, failure_scope="PLACEMENT", failure_reason=reason)

   def _execute_runtime(self, item: HopWorkItem) -> RuntimeResult:
      gate = self.token_path_gate
      if gate is not None:
         reason = gate.claim(item)
         if reason is not None:
            return self._token_path_failure(reason)
      try:
         result = self.runtime.execute(item)
      except Exception:
         return self._runtime_unavailable()
      if gate is not None:
         reason = gate.fence(item)
         if reason is not None:
            return self._token_path_failure(reason)
      return result

   def _execute_runtime_batch(self, batch) -> tuple[RuntimeResult, ...]:
      gate = self.token_path_gate
      if gate is None:
         return self._execute_runtime_batch_unfenced(batch)
      from dataclasses import replace
      reasons = [gate.claim(item) for item in batch.items]
      allowed = tuple(item for item, reason in zip(batch.items, reasons) if reason is None)
      if not allowed:
         return tuple(self._token_path_failure(reason) for reason in reasons)
      admitted = batch if len(allowed) == len(batch.items) else replace(batch, items=allowed, decision=None)
      executed_results = self._execute_runtime_batch_unfenced(admitted)
      if len(executed_results) != len(allowed):
         executed_results = tuple(self._token_path_failure("runtime_batch_result_count_mismatch")
                                  for _ in allowed)
      executed = iter(executed_results)
      results = []
      for item, reason in zip(batch.items, reasons):
         if reason is not None:
            results.append(self._token_path_failure(reason))
         else:
            result = next(executed, self._runtime_unavailable())
            late = gate.fence(item)
            results.append(self._token_path_failure(late) if late is not None else result)
      return tuple(results)

   def _execute_runtime_batch_unfenced(self, batch) -> tuple[RuntimeResult, ...]:
      # A10: a relay-formed batch of >=2 compatible members may already be
      # servable as one real backend invocation. ``attempt_runtime_batch`` is
      # fail-closed by construction (mycelium_router/a10_batch_producer.py):
      # it returns ``None`` whenever the batch is ineligible or the port
      # lacks the A10 surface, in which case behaviour below is
      # byte-for-byte identical to before this existed.
      upgraded = a10_batch_producer.attempt_runtime_batch(self.runtime, batch)
      if upgraded is not None:
         return upgraded
      try:
         return tuple(self.runtime.execute_batch(batch))
      except Exception:
         return tuple(self._runtime_unavailable() for _ in batch.items)

   def _kv_position(self, request: RequestContext, phase: str, token_index: int) -> int:
      if phase in {"PREFILL", "RECOVERY_PREFILL"}:
         return 0
      if phase == "PREFILL_CHUNK":
         size = self.scheduler.config.prefill_chunk_size_tokens
         return 0 if size <= 0 else token_index * size
      if phase in {"DECODE", "VERIFY"}:
         # A11 VERIFY feeds the pending token first, at the DECODE position.
         return len(request.prompt_token_ids) + token_index - 1
      return 0

   def _verify_span_admissible(self, header: HopHeader) -> bool:
      """A VERIFY carries its span (pending + candidates) in the chunk-count field."""

      span = header.prefill_chunk_token_count
      return (
         self.decode_mode == "stage_local_kv"
         and type(span) is int
         and 2 <= span <= MAX_VERIFY_SPAN
         and header.token_index >= 1
      )

   def _is_terminal(
      self,
      request: RequestContext,
      phase: str,
      token_index: int,
      prefill_chunk_token_count: int = 0,
   ) -> bool:
      if phase == "PREFILL":
         return request.max_new_tokens == 1
      if phase == "PREFILL_CHUNK":
         return (
            self._kv_position(request, phase, token_index)
            + prefill_chunk_token_count
            >= len(request.prompt_token_ids)
            and request.max_new_tokens == 1
         )
      if phase == "RECOVERY_PREFILL":
         return token_index + 1 >= request.max_new_tokens
      if phase == "DECODE":
         return token_index + 1 >= request.max_new_tokens
      return False

   def register_path(
      self,
      request: RequestContext,
      manifest: PathManifest,
      graph: ExecutionGraph,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> bool:
      return self.register_path_with_generation(
         request,
         manifest,
         graph,
         source_node_id=source_node_id,
         entry_node_id=entry_node_id,
      ) is not None

   def register_path_with_generation(
      self,
      request: RequestContext,
      manifest: PathManifest,
      graph: ExecutionGraph,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> int | None:
      self.expire_idle_paths()
      with self._path_operation_lock(manifest.path_id):
         validate_manifest(manifest, graph)
         if request.request_id != manifest.request_id:
            return None
         if source_node_id is not None:
            placement_map = {
               placement.placement_id: placement
               for stage in graph.stages
               for placement in stage.placements
            }
            final_placement = placement_map[manifest.ordered_hops[-1].placement_id]
            if not source_node_id or final_placement.node_id != source_node_id:
               return None
            if not entry_node_id:
               return None
         if entry_node_id is not None and not entry_node_id:
            return None
         with self._path_lock:
            if manifest.path_id in self._retiring_paths:
               return None
            first = self._path_registered_at.get(manifest.path_id)
            if first is not None and self.clock.now() - first >= _LIVE_PATH_MAX_LIFETIME_SECONDS:
               return None
            owner = self._resolve_owner_locked(
               manifest.path_id, request.request_id, entry_node_id
            )
            existing = self._paths.get(manifest.path_id)
            provisional = self._provisional_paths.get(manifest.path_id)
            creator = source_node_id or entry_node_id or self.node_id
            if creator != self.node_id and creator not in {
               placement.node_id
               for stage in graph.stages
               for placement in stage.placements
               if placement.placement_id in {hop.placement_id for hop in manifest.ordered_hops}
            }:
               # Only a participant of the path (an authenticated route peer
               # placed on it) can create, and be charged for, its state.
               return None
            if source_node_id is not None and existing is None and provisional is None:
               # A manifest relayed by the final placement (progressive
               # discovery) only confirms a path this node already holds from
               # the authenticated entry chain; it never creates one, so a
               # peer cannot register a path in another entry's name.
               return None
            recorded_owner = self._path_owners.get(manifest.path_id)
            if (
               (existing is not None or provisional is not None)
               and recorded_owner is not None
               and not _same_owner(recorded_owner, owner)
            ):
               # A live path id stays bound to its owner whatever the attempt:
               # another entry or request never replaces it.
               return None
            if self._consume_pending_cancellation_locked(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               topology_version=graph.topology_version,
               entry_node_id=entry_node_id,
            ):
               return None
            if self._was_cancelled_attempt_locked(
               manifest.path_id,
               manifest.path_attempt,
               owner,
            ):
               return None
            cancelled_attempt = self._cancelled_attempt_for_locked(
               manifest.path_id, owner, creator
            )
            if (
               cancelled_attempt is not None
               and manifest.path_attempt <= cancelled_attempt
            ):
               return None
            if self._exactly_released_locked(manifest.path_id, owner, manifest.path_attempt):
               return None
            if existing is None and provisional is None and not self._creator_may_register_locked(creator):
               # The creator is at its tombstone quota (or past the creator
               # cap): fail closed for it alone. Checked after the tombstones,
               # so making room never evicts the record refusing this very
               # registration.
               return None
            if provisional is not None:
               if provisional[:3] != (
                  request.request_id,
                  manifest.path_attempt,
                  graph.topology_version,
               ):
                  return None
               provisional_entry = provisional[3]
               if (
                  entry_node_id is not None
                  and provisional_entry is not None
                  and entry_node_id != provisional_entry
               ):
                  return None
               if entry_node_id is None:
                  entry_node_id = provisional_entry
            if existing is not None:
               existing_manifest = existing[1]
               if manifest.path_attempt < existing_manifest.path_attempt:
                  return None
               if manifest.path_attempt == existing_manifest.path_attempt:
                  if existing != (graph, manifest, request):
                     return None
                  existing_entry = self._entry_node_by_path.get(manifest.path_id)
                  if (
                     entry_node_id is not None
                     and existing_entry is not None
                     and entry_node_id != existing_entry
                  ):
                     return None
                  if entry_node_id is not None:
                     self._entry_node_by_path[manifest.path_id] = entry_node_id
                  self._touch_path_locked(manifest.path_id)
                  return self._path_generations[manifest.path_id]
               existing_entry = self._entry_node_by_path.get(manifest.path_id)
               if (
                  entry_node_id is not None
                  and existing_entry is not None
                  and entry_node_id != existing_entry
               ):
                  return None
               self._record_cancelled_attempt_locked(
                  manifest.path_id,
                  existing_manifest.path_attempt,
               )
            self._paths[manifest.path_id] = (graph, manifest, request)
            self._record_owner_locked(manifest.path_id, owner)
            if existing is None and provisional is None:
               self._path_creators[manifest.path_id] = creator
               self._path_creators.move_to_end(manifest.path_id)
               # A6 replays and A7 successors reuse the path id; the ceiling
               # runs from the *first* registration here, never from the
               # attempt that is being registered now.
               self._path_registered_at.setdefault(
                  manifest.path_id, self.clock.now()
               )
            self._hop_arrivals.record_registration(
               manifest.path_id, at=self.clock.now()
            )
            self._hop_arrivals.end_admission(manifest.request_id)
            self._provisional_paths.pop(manifest.path_id, None)
            self._path_busy.pop(manifest.path_id, None)
            self._advance_generation_locked(manifest.path_id)
            for recorded, binding in self._cancelled_path_owners.get(manifest.path_id, ()):
               self._cancelled_path_attempts.move_to_end((manifest.path_id, recorded, binding))
            self._prune_terminal_path_metadata_locked()
            if entry_node_id is not None:
               self._entry_node_by_path[manifest.path_id] = entry_node_id
            self._touch_path_locked(manifest.path_id)
            return self._path_generations[manifest.path_id]

   def _register_provisional_path(
      self,
      *,
      request_id: str,
      path_id: str,
      path_attempt: int,
      topology_version: int,
      entry_node_id: str | None,
      source_node_id: str | None = None,
   ) -> int | None:
      identity = (
         request_id,
         path_attempt,
         topology_version,
         entry_node_id,
      )
      if entry_node_id is not None and not entry_node_id:
         return None
      with self._path_operation_lock(path_id):
         with self._path_lock:
            if path_id in self._retiring_paths:
               return None
            first = self._path_registered_at.get(path_id)
            if first is not None and self.clock.now() - first >= _LIVE_PATH_MAX_LIFETIME_SECONDS:
               return None
            if self._consume_pending_cancellation_locked(
               request_id=request_id,
               path_id=path_id,
               path_attempt=path_attempt,
               topology_version=topology_version,
               entry_node_id=entry_node_id,
            ):
               return None
            owner = self._resolve_owner_locked(path_id, request_id, entry_node_id)
            creator = source_node_id or entry_node_id or self.node_id
            live = path_id in self._paths or path_id in self._provisional_paths
            recorded_owner = self._path_owners.get(path_id)
            if (
               (path_id in self._paths or path_id in self._provisional_paths)
               and recorded_owner is not None
               and not _same_owner(recorded_owner, owner)
            ):
               return None
            if self._was_cancelled_attempt_locked(path_id, path_attempt, owner):
               return None
            cancelled_attempt = self._cancelled_attempt_for_locked(path_id, owner, creator)
            if cancelled_attempt is not None and path_attempt <= cancelled_attempt:
               return None
            if self._exactly_released_locked(path_id, owner, path_attempt):
               return None
            if not live and not self._creator_may_register_locked(creator):
               return None
            existing = self._provisional_paths.get(path_id)
            if existing is not None:
               if existing[:3] != identity[:3]:
                  return None
               if (
                  existing[3] is not None
                  and entry_node_id is not None
                  and existing[3] != entry_node_id
               ):
                  return None
               return self._path_generations[path_id]
            self._provisional_paths[path_id] = identity
            self._record_owner_locked(path_id, owner)
            if not live:
               self._path_creators[path_id] = creator
               self._path_creators.move_to_end(path_id)
               self._path_registered_at.setdefault(path_id, self.clock.now())
            self._advance_generation_locked(path_id)
            self._path_busy.pop(path_id, None)
            self._path_activity.setdefault(path_id, self.clock.now())
            return self._path_generations[path_id]

   def receive_progressive_prefill(
      self,
      header: HopHeader,
      context: ProgressivePrefillContext,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> ProgressivePrefillResult:
      if self.token_path_gate is not None:
         # Autonomous grants bind a complete locked manifest, not a
         # progressively selected path or a peer-created successor.
         return ProgressivePrefillResult("REJECTED", "token_path_hop_phase")
      self.expire_idle_paths()
      return self._receive_progressive_prefill_inner(
         header, context, source_node_id=source_node_id, entry_node_id=entry_node_id
      )

   def _receive_progressive_prefill_inner(
      self,
      header: HopHeader,
      context: ProgressivePrefillContext,
      *,
      source_node_id: str | None = None,
      entry_node_id: str | None = None,
   ) -> ProgressivePrefillResult:
      now = self.clock.now()
      self._evict_prefill_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return ProgressivePrefillResult("REJECTED", "invalid_idempotency_key")
      build = context.build
      if (
         header.phase != "PREFILL"
         or header.request_id != context.request.request_id
         or header.path_id != build.path_id
         or header.path_attempt != build.path_attempt
         or header.topology_version != context.graph.topology_version
         or header.hop_index != len(build.ordered_hops) - 1
      ):
         return ProgressivePrefillResult("REJECTED", "prefill_context_mismatch")
      hop = build.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return ProgressivePrefillResult("REJECTED", "manifest_hop_mismatch")
      expected_source = (
         ""
         if header.hop_index == 0
         else build.ordered_hops[header.hop_index - 1].placement_id
      )
      if header.source_placement_id != expected_source:
         return ProgressivePrefillResult("REJECTED", "source_placement_mismatch")
      if (
         source_node_id is not None
         and header.hop_index == 0
         and entry_node_id != source_node_id
      ):
         return ProgressivePrefillResult("REJECTED", "entry_node_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in context.graph.stages
         for placement in stage.placements
      }
      if source_node_id is not None and header.hop_index > 0:
         source_placement = placement_map.get(header.source_placement_id)
         if (
            source_placement is None
            or source_placement.node_id != source_node_id
         ):
            return ProgressivePrefillResult("REJECTED", "source_node_mismatch")
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return ProgressivePrefillResult("REJECTED", "destination_not_local")
      try:
         accepted_payload = snapshot_payload(context.payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return ProgressivePrefillResult("REJECTED", "unsupported_payload_type")
      provisional_entry = entry_node_id
      if provisional_entry is None and header.hop_index == 0:
         provisional_entry = source_node_id
      generation = self._register_provisional_path(
         request_id=context.request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         topology_version=context.graph.topology_version,
         entry_node_id=provisional_entry,
         source_node_id=source_node_id,
      )
      if generation is None:
         return ProgressivePrefillResult("REJECTED", "cancelled_path")
      cached = self._prefill_results.get(_hop_key(header))
      if cached is not None:
         if cached[3] != payload_digest:
            return ProgressivePrefillResult(
               "REJECTED",
               "idempotency_payload_mismatch",
            )
         return cached[0]

      state = HopStateMachine(path_attempt=header.path_attempt)
      state.transition("QUEUED", path_attempt=header.path_attempt)
      # Admission point.  Everything above this line may still reject the
      # frame, and a frame that is rejected is not traffic: only an admitted
      # frame refreshes the path or holds it busy (review 7, part a).
      with self._admitted_path_activity(build.path_id) as admit:
         return self._admitted_progressive_prefill(
            header,
            context,
            build=build,
            hop=hop,
            placement=placement,
            generation=int(generation or 0),
            provisional_entry=provisional_entry,
            accepted_payload=accepted_payload,
            payload_digest=payload_digest,
            state=state,
            now=now,
            admit=admit,
         )

   def _admitted_progressive_prefill(
      self,
      header: HopHeader,
      context: ProgressivePrefillContext,
      *,
      build,
      hop,
      placement,
      generation: int,
      provisional_entry: str | None,
      accepted_payload,
      payload_digest: str,
      state: HopStateMachine,
      now: float,
      admit,
   ) -> ProgressivePrefillResult:
      work = HopWorkItem(
         request_id=context.request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=context.request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=0,
         terminal=self._is_terminal(context.request, "PREFILL", -1),
         lease_expires_at=hop.reservation_expires_at,
         batch_key=self._runtime_batch_key(
            graph=context.graph,
            placement=placement,
            phase="PREFILL",
            token_span=header.prefill_chunk_token_count,
         ),
      )
      try:
         self.scheduler.enqueue(work)
      except BackpressureError as error:
         return ProgressivePrefillResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            self.scheduler.withdraw(work)
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         admit()
      selected = self.scheduler.take(work)
      if selected is None:
         # This identity's item is not this dispatch's to execute any more (it was
         # withdrawn with a release, or claimed elsewhere): never execute work this
         # call does not own.
         return ProgressivePrefillResult("QUEUED", "duplicate_pending")
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
      runtime_result = self._execute_runtime(selected)
      with self._path_lock:
         if not self._path_is_current_locked(
            build.path_id,
            build.path_attempt,
            generation,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
      if not runtime_result.success:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            token_index=-1,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self.release_path(build.path_id, path_attempt=build.path_attempt)
         self._remember_prefill(header, result, payload_digest, generation)
         return result

      if self.builder.is_complete(build):
         try:
            manifest = self.builder.lock(build, now=now)
         except RoutingError as error:
            state.transition("FAILED", path_attempt=header.path_attempt)
            report = FailureReport(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=-1,
               scope="PLACEMENT",
               reason=f"manifest_lock_failed:{error}",
               placement_id=hop.placement_id,
               node_id=placement.node_id,
            )
            if not self._send_if_path_current(
               build.path_id,
               build.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return ProgressivePrefillResult("REJECTED", "cancelled_path")
            result = ProgressivePrefillResult(
               "FAILED",
               reason=report.reason,
               failure_report=report,
            )
            self.release_path(build.path_id, path_attempt=build.path_attempt)
            self._remember_prefill(header, result, payload_digest, generation)
            return result
         confirmation = ManifestLocked(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            manifest=manifest,
            build=build,
         )
         generation = self.register_path_with_generation(
            context.request,
            manifest,
            context.graph,
            entry_node_id=provisional_entry,
         )
         if generation is None:
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         if (
            self.decode_mode == "stage_local_kv"
            and runtime_result.token_id is None
         ):
            state.transition("FAILED", path_attempt=header.path_attempt)
            report = FailureReport(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=-1,
               scope="PLACEMENT",
               reason="final_stage_missing_token",
               placement_id=hop.placement_id,
               node_id=placement.node_id,
            )
            if not self._send_if_path_current(
               build.path_id,
               build.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return ProgressivePrefillResult("REJECTED", "cancelled_path")
            result = ProgressivePrefillResult(
               "FAILED",
               reason=report.reason,
               failure_report=report,
            )
            self.release_path(build.path_id, path_attempt=build.path_attempt)
            self._remember_prefill(header, result, payload_digest, generation)
            return result
         token_event = (
            TokenEvent(
               request_id=context.request.request_id,
               path_id=build.path_id,
               path_attempt=build.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if runtime_result.token_id is not None
            else None
         )

         def send_locked_result() -> None:
            self.transport.send_manifest_locked(confirmation)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            send_locked_result,
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = ProgressivePrefillResult(
            "LOCKED",
            confirmation=confirmation,
         )
         self._remember_prefill(header, result, payload_digest, generation)
         return result

      try:
         updated = self.builder.advance(
            build,
            self.device_states.snapshot(),
            now=now,
         )
      except RoutingError as error:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=context.request.request_id,
            path_id=build.path_id,
            path_attempt=build.path_attempt,
            token_index=-1,
            scope="PLACEMENT",
            reason=f"route_extension_failed:{error}",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            build.path_id,
            build.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return ProgressivePrefillResult("REJECTED", "cancelled_path")
         result = ProgressivePrefillResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self.release_path(build.path_id, path_attempt=build.path_attempt)
         self._remember_prefill(header, result, payload_digest, generation)
         return result
      next_index = len(updated.ordered_hops) - 1
      next_hop = updated.ordered_hops[next_index]
      delta = ManifestDelta(
         request_id=context.request.request_id,
         path_id=updated.path_id,
         path_attempt=updated.path_attempt,
         hop_index=next_index,
         hop=next_hop,
      )
      next_header = HopHeader(
         request_id=context.request.request_id,
         path_id=updated.path_id,
         path_attempt=updated.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=next_index,
         source_placement_id=hop.placement_id,
         destination_placement_id=next_hop.placement_id,
         topology_version=context.graph.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=context.request.request_id,
            path_id=updated.path_id,
            path_attempt=updated.path_attempt,
            phase="PREFILL",
            token_index=-1,
            hop_index=next_index,
         ),
         prefill_chunk_token_count=header.prefill_chunk_token_count,
      )
      next_context = ProgressivePrefillContext(
         graph=context.graph,
         request=context.request,
         build=updated,
         payload=runtime_result.payload,
      )
      def send_progressive_hop() -> None:
         self.transport.send_manifest_delta(delta)
         self.transport.send_hop(next_header, next_context)

      if not self._send_if_path_current(
         build.path_id,
         build.path_attempt,
         generation,
         send_progressive_hop,
      ):
         return ProgressivePrefillResult("REJECTED", "cancelled_path")
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = ProgressivePrefillResult(
         "FORWARDED",
         forwarded_header=next_header,
         context=next_context,
      )
      self._remember_prefill(header, result, payload_digest, generation)
      return result

   def receive_hop(
      self,
      header: HopHeader,
      payload: object,
      *,
      source_node_id: str | None = None,
   ) -> HopReceiveResult:
      if self.token_path_gate is not None:
         reason = self.token_path_gate.ingress(header, source_node_id)
         if reason is not None:
            return HopReceiveResult("REJECTED", reason)
      self.expire_idle_paths()
      return self._receive_hop_inner(header, payload, source_node_id=source_node_id)

   def _receive_hop_inner(
      self,
      header: HopHeader,
      payload: object,
      *,
      source_node_id: str | None = None,
   ) -> HopReceiveResult:
      now = self.clock.now()
      self._evict_hop_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return HopReceiveResult("REJECTED", "invalid_idempotency_key")
      if header.phase == "VERIFY" and not self._verify_span_admissible(header):
         return HopReceiveResult("REJECTED", "invalid_verify_span")
      with self._path_lock:
         registration = self._paths.get(header.path_id)
         generation = self._path_generations.get(header.path_id, 0)
      if registration is None:
         return HopReceiveResult("REJECTED", "unknown_path")
      graph, manifest, request = registration
      if (
         header.path_attempt != manifest.path_attempt
         or header.request_id != manifest.request_id
      ):
         return HopReceiveResult("REJECTED", "stale_path_attempt")
      if header.topology_version != manifest.topology_version:
         return HopReceiveResult("REJECTED", "topology_version_mismatch")
      if header.hop_index < 0 or header.hop_index >= len(manifest.ordered_hops):
         return HopReceiveResult("REJECTED", "invalid_hop_index")
      hop = manifest.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return HopReceiveResult("REJECTED", "manifest_hop_mismatch")
      if header.hop_index == 0:
         valid_sources = {""}
         if header.phase in {"DECODE", "VERIFY"}:
            valid_sources.add(manifest.ordered_hops[-1].placement_id)
      else:
         valid_sources = {
            manifest.ordered_hops[header.hop_index - 1].placement_id
         }
      if header.source_placement_id not in valid_sources:
         return HopReceiveResult("REJECTED", "source_placement_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      if source_node_id is not None:
         if header.hop_index == 0:
            if self._entry_node_by_path.get(header.path_id) != source_node_id:
               return HopReceiveResult("REJECTED", "entry_node_mismatch")
         else:
            source_placement = placement_map.get(header.source_placement_id)
            if (
               source_placement is None
               or source_placement.node_id != source_node_id
            ):
               return HopReceiveResult("REJECTED", "source_node_mismatch")
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return HopReceiveResult("REJECTED", "destination_not_local")
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return HopReceiveResult("REJECTED", "unsupported_payload_type")
      cached = self._hop_results.get(_hop_key(header))
      if cached is not None:
         if cached[3] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return cached[0]
      pending = self._pending_hops.get(_hop_key(header))
      if pending is not None:
         if pending[2] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return HopReceiveResult("QUEUED", "duplicate_pending")

      state = HopStateMachine(path_attempt=header.path_attempt)
      state.transition("QUEUED", path_attempt=header.path_attempt)
      # Admission point (review 7, part a): everything above this line may
      # still reject the frame.  A frame from a non-participant, with a bad
      # idempotency key, or naming the wrong attempt/hop/source is refused
      # here, and a refused frame must not refresh or hold the path: otherwise
      # any configured route peer keeps a path (and its creator's slot) alive
      # forever with traffic this relay rejects.
      with self._admitted_path_activity(header.path_id) as admit:
         return self._admitted_receive_hop(
            header,
            graph=graph,
            manifest=manifest,
            request=request,
            hop=hop,
            placement=placement,
            generation=generation,
            state=state,
            now=now,
            accepted_payload=accepted_payload,
            payload_digest=payload_digest,
            admit=admit,
         )

   def _admitted_receive_hop(
      self,
      header: HopHeader,
      *,
      graph,
      manifest: PathManifest,
      request: RequestContext,
      hop,
      placement,
      generation: int,
      state: HopStateMachine,
      now: float,
      accepted_payload,
      payload_digest: str,
      admit,
   ) -> HopReceiveResult:
      work = HopWorkItem(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=self._kv_position(request, header.phase, header.token_index),
         emits_token=(
            header.phase == "PREFILL_CHUNK"
            and self._kv_position(request, header.phase, header.token_index)
            + header.prefill_chunk_token_count
            >= len(request.prompt_token_ids)
         ),
         terminal=self._is_terminal(
            request,
            header.phase,
            header.token_index,
            header.prefill_chunk_token_count,
         ),
         lease_expires_at=hop.reservation_expires_at,
         batch_key=self._runtime_batch_key(
            graph=graph,
            placement=placement,
            phase=header.phase,
            token_span=(
               1
               if header.phase == "DECODE"
               else header.prefill_chunk_token_count
            ),
         ),
      )
      try:
         self.scheduler.enqueue(work)
      except BackpressureError as error:
         return HopReceiveResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            self.scheduler.withdraw(work)
            return HopReceiveResult("REJECTED", "path_cancelled")
         admit()
      selected = self.scheduler.take(work)
      if selected is None:
         # The item this dispatch enqueued is gone (withdrawn with a release, or
         # claimed elsewhere): never execute another identity's work, and never
         # execute this one twice.
         return HopReceiveResult("QUEUED", "duplicate_pending")
      state.transition("ACCEPTED", path_attempt=header.path_attempt)
      state.transition("EXECUTING", path_attempt=header.path_attempt)
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
      runtime_result = self._execute_runtime(selected)
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
      if not runtime_result.success:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.hop_index + 1 < len(manifest.ordered_hops):
         next_hop = manifest.ordered_hops[header.hop_index + 1]
         next_header = HopHeader(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=header.phase,
            token_index=header.token_index,
            hop_index=header.hop_index + 1,
            source_placement_id=hop.placement_id,
            destination_placement_id=next_hop.placement_id,
            topology_version=manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=header.phase,
               token_index=header.token_index,
               hop_index=header.hop_index + 1,
            ),
            prefill_chunk_token_count=header.prefill_chunk_token_count,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_hop(next_header, runtime_result.payload),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "FORWARDED",
            forwarded_header=next_header,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "VERIFY":
         result = self._complete_verify_hop(
            header,
            request,
            manifest,
            hop,
            placement,
            runtime_result,
            payload_digest,
            generation,
         )
         if result.disposition == "FAILED":
            state.transition("FAILED", path_attempt=header.path_attempt)
         elif result.disposition == "COMPLETED":
            state.transition("FORWARDED", path_attempt=header.path_attempt)
         return result

      final_prefill_chunk = (
         self.decode_mode == "stage_local_kv"
         and header.phase == "PREFILL_CHUNK"
         and selected.position + header.prefill_chunk_token_count
         >= len(request.prompt_token_ids)
      )
      if (
         header.phase == "DECODE" or final_prefill_chunk
      ) and runtime_result.token_id is None:
         state.transition("FAILED", path_attempt=header.path_attempt)
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         token_event = (
            TokenEvent(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if final_prefill_chunk
            else None
         )

         def send_prefill_result() -> None:
            self.transport.send_prefill_chunk_completed(event)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            send_prefill_result,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
            token_event=token_event,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result
      if header.phase != "DECODE":
         state.transition("FORWARDED", path_attempt=header.path_attempt)
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result, payload_digest, generation)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      if not self._send_if_path_current(
         header.path_id,
         header.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return HopReceiveResult("REJECTED", "path_cancelled")
      state.transition("FORWARDED", path_attempt=header.path_attempt)
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result, payload_digest, generation)
      return result

   def enqueue_hop(
      self,
      header: HopHeader,
      payload: object,
      *,
      source_node_id: str | None = None,
   ) -> HopReceiveResult:
      """Validate and queue one hop (see ``_enqueue_hop_inner``); a queued hop
      keeps its path busy until it completes."""
      if self.token_path_gate is not None:
         reason = self.token_path_gate.ingress(header, source_node_id)
         if reason is not None:
            return HopReceiveResult("REJECTED", reason)
      self.expire_idle_paths()
      return self._enqueue_hop_inner(header, payload, source_node_id=source_node_id)

   def _enqueue_hop_inner(
      self,
      header: HopHeader,
      payload: object,
      *,
      source_node_id: str | None = None,
   ) -> HopReceiveResult:
      """Validate and queue one hop without blocking for runtime execution.

      Production event loops should call this method for ingress, then call
      ``drain_ready_batches`` immediately and again at ``next_batch_deadline``.
      The synchronous ``receive_hop`` API remains available for compatibility.
      ``source_node_id`` is checked exactly as ``receive_hop`` checks it.
      """
      now = self.clock.now()
      self._evict_hop_results(now=now)
      expected_key = hop_idempotency_key(
         request_id=header.request_id,
         path_id=header.path_id,
         path_attempt=header.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
      )
      if header.idempotency_key != expected_key:
         return HopReceiveResult("REJECTED", "invalid_idempotency_key")
      if header.phase == "VERIFY" and not self._verify_span_admissible(header):
         return HopReceiveResult("REJECTED", "invalid_verify_span")
      with self._path_lock:
         registration = self._paths.get(header.path_id)
         generation = self._path_generations.get(header.path_id, 0)
      if registration is None:
         return HopReceiveResult("REJECTED", "unknown_path")
      graph, manifest, request = registration
      if (
         header.path_attempt != manifest.path_attempt
         or header.request_id != manifest.request_id
      ):
         return HopReceiveResult("REJECTED", "stale_path_attempt")
      if header.topology_version != manifest.topology_version:
         return HopReceiveResult("REJECTED", "topology_version_mismatch")
      if header.hop_index < 0 or header.hop_index >= len(manifest.ordered_hops):
         return HopReceiveResult("REJECTED", "invalid_hop_index")
      hop = manifest.ordered_hops[header.hop_index]
      if header.destination_placement_id != hop.placement_id:
         return HopReceiveResult("REJECTED", "manifest_hop_mismatch")
      if header.hop_index == 0:
         valid_sources = {""}
         if header.phase in {"DECODE", "VERIFY"}:
            valid_sources.add(manifest.ordered_hops[-1].placement_id)
      else:
         valid_sources = {
            manifest.ordered_hops[header.hop_index - 1].placement_id
         }
      if header.source_placement_id not in valid_sources:
         return HopReceiveResult("REJECTED", "source_placement_mismatch")
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      if source_node_id is not None:
         if header.hop_index == 0:
            if self._entry_node_by_path.get(header.path_id) != source_node_id:
               return HopReceiveResult("REJECTED", "entry_node_mismatch")
         else:
            source_placement = placement_map.get(header.source_placement_id)
            if (
               source_placement is None
               or source_placement.node_id != source_node_id
            ):
               return HopReceiveResult("REJECTED", "source_node_mismatch")
      placement = placement_map[hop.placement_id]
      if placement.node_id != self.node_id:
         return HopReceiveResult("REJECTED", "destination_not_local")
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return HopReceiveResult("REJECTED", "unsupported_payload_type")
      cached = self._hop_results.get(_hop_key(header))
      if cached is not None:
         if cached[3] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return cached[0]
      pending = self._pending_hops.get(_hop_key(header))
      if pending is not None:
         if pending[2] != payload_digest:
            return HopReceiveResult("REJECTED", "idempotency_payload_mismatch")
         return HopReceiveResult("QUEUED", "duplicate_pending")

      work = HopWorkItem(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase=header.phase,
         token_index=header.token_index,
         hop_index=header.hop_index,
         placement_id=hop.placement_id,
         qos_class=request.qos_class,
         deficit_ratio=0.0,
         enqueued_at=now,
         idempotency_key=header.idempotency_key,
         payload=accepted_payload,
         prefill_chunk_token_count=header.prefill_chunk_token_count,
         position=self._kv_position(request, header.phase, header.token_index),
         emits_token=(
            header.phase == "PREFILL_CHUNK"
            and self._kv_position(request, header.phase, header.token_index)
            + header.prefill_chunk_token_count
            >= len(request.prompt_token_ids)
         ),
         terminal=self._is_terminal(
            request,
            header.phase,
            header.token_index,
            header.prefill_chunk_token_count,
         ),
         lease_expires_at=hop.reservation_expires_at,
         batch_key=self._runtime_batch_key(
            graph=graph,
            placement=placement,
            phase=header.phase,
            token_span=(
               1
               if header.phase == "DECODE"
               else header.prefill_chunk_token_count
            ),
         ),
         deadline_at=self._hop_deadline(request, header.phase, now=now),
      )
      # Admission point (review 7, part a): a frame that is refused above is
      # not traffic, so it never refreshes or holds this path.
      with self._admitted_path_activity(header.path_id) as admit:
         return self._admitted_enqueue_hop(
            header,
            graph=graph,
            manifest=manifest,
            request=request,
            hop=hop,
            placement=placement,
            generation=generation,
            now=now,
            accepted_payload=accepted_payload,
            payload_digest=payload_digest,
            work=work,
            source_node_id=source_node_id,
            admit=admit,
         )

   def _admitted_enqueue_hop(
      self,
      header: HopHeader,
      *,
      graph,
      manifest: PathManifest,
      request: RequestContext,
      hop,
      placement,
      generation: int,
      now: float,
      accepted_payload,
      payload_digest: str,
      work: HopWorkItem,
      source_node_id: str | None,
      admit,
   ) -> HopReceiveResult:
      try:
         self.batch_scheduler.enqueue(work)
      except BackpressureError as error:
         return HopReceiveResult(
            "REJECTED",
            reason=f"backpressure:{error.reason}",
            retry_after_seconds=error.retry_after_seconds,
         )
      except DuplicateHopError:
         return HopReceiveResult("QUEUED", "duplicate_pending")
      cancelled = False
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            cancelled = True
         else:
            admit()
            self._pending_hops[_hop_key(header)] = (
               header,
               work,
               payload_digest,
               generation,
               source_node_id,
            )
            self._enqueue_sequence += 1
      if cancelled:
         self.batch_scheduler.withdraw(work)
         return HopReceiveResult("REJECTED", "path_cancelled")
      self._hop_arrivals.record(header.path_id, at=now, terminal=bool(work.terminal))
      # A request whose hop is queued here is past admission: never its own partner.
      self._hop_arrivals.end_admission(header.request_id)
      return HopReceiveResult("QUEUED")

   def drain_ready_batches(
      self,
      *,
      force: bool = False,
      maximum_batches: int | None = None,
      blocking: bool = True,
      on_item_error=None,
   ) -> tuple[HopReceiveResult, ...]:
      """Execute ready microbatches; never sleeps or owns an event-loop thread.

      Drains are serialized.  With ``blocking=False`` a caller that finds another
      drain in progress returns ``()`` at once; the active drainer keeps taking
      ready work, including work queued after it started.  A failure while one
      member is completed (forwarded or reported) is that member's alone: it is
      passed to ``on_item_error(header, source_node_id, error)`` and the rest of
      the batch still completes.  Without a handler the first such failure is
      raised once every member of its batch has been completed.
      """
      if maximum_batches is not None and maximum_batches <= 0:
         return ()
      if not self._drain_lock.acquire(blocking=blocking):
         return ()
      try:
         return self._drain_ready_batches_locked(
            force=force,
            maximum_batches=maximum_batches,
            on_item_error=on_item_error,
         )
      finally:
         self._drain_lock.release()

   def pump_ready_batches(self, *, on_item_error=None) -> tuple[HopReceiveResult, ...]:
      """Drain ready work without ever waiting behind another drainer.

      Every ingress may call this right after ``enqueue_hop``.  A caller that
      finds a drain in progress returns at once: the active drainer re-checks the
      queue after it releases, so work queued while it held the drain is never
      stranded.  Work the controller holds for a collection window stays queued;
      the caller's timer drains it at ``next_batch_deadline``.
      """
      completed: list[HopReceiveResult] = []
      while True:
         if not self._drain_lock.acquire(blocking=False):
            return tuple(completed)
         try:
            with self._path_lock:
               sequence = self._enqueue_sequence
            completed.extend(
               self._drain_ready_batches_locked(
                  force=False,
                  maximum_batches=None,
                  on_item_error=on_item_error,
               )
            )
         finally:
            self._drain_lock.release()
         with self._path_lock:
            arrived = self._enqueue_sequence != sequence
         if not arrived or not self.batch_scheduler.queue_depth():
            return tuple(completed)

   def _drain_ready_batches_locked(
      self,
      *,
      force: bool,
      maximum_batches: int | None,
      on_item_error,
   ) -> tuple[HopReceiveResult, ...]:
      self.expire_idle_paths()
      completed: list[HopReceiveResult] = []
      drained = 0
      while self.batch_scheduler.queue_depth():
         if maximum_batches is not None and drained >= maximum_batches:
            break
         now = self.clock.now()
         candidates = self.batch_scheduler.batch_candidates(now=now)
         decision = self.batch_controller.decide(
            candidates,
            now=now,
            force=force,
         )
         if decision.action == "WAIT":
            break
         batch = self.batch_scheduler.pop_batch(
            now=now,
            maximum_items=decision.batch_size,
            maximum_bytes=self.scheduler.config.maximum_runtime_batch_bytes,
            decision=decision,
         )
         started_at = self.clock.now()
         with self._path_lock:
            active = tuple(
               _hop_key(item) in self._pending_hops
               and self._path_is_current_locked(
                  item.path_id,
                  item.path_attempt,
                  self._pending_hops[_hop_key(item)][3],
               )
               for item in batch.items
            )
         if all(active):
            runtime_results = self._execute_runtime_batch(batch)
         else:
            runtime_results = tuple(
               self._execute_runtime(item)
               if is_active
               else self._runtime_unavailable()
               for item, is_active in zip(batch.items, active)
            )
         execution_ms = max(0.0, self.clock.now() - started_at) * 1_000.0
         self.expire_idle_paths()
         if len(runtime_results) != len(batch.items):
            runtime_results = tuple(
               RuntimeResult(
                  success=False,
                  failure_scope="PLACEMENT",
                  failure_reason="runtime_batch_result_count_mismatch",
               )
               for _ in batch.items
            )
         payload_bytes = sum(
            self.batch_scheduler._payload_size(item.payload) for item in batch.items
         )
         self.batch_controller.record_execution(
            BatchExecutionObservation(
               phase=batch.items[0].phase,
               batch_size=len(batch.items),
               payload_bytes=payload_bytes,
               execution_ms=execution_ms,
               successful=all(result.success for result in runtime_results),
            )
         )
         unhandled: BaseException | None = None
         for item, runtime_result in zip(batch.items, runtime_results):
            with self._path_lock:
               pending = self._pending_hops.get(_hop_key(item))
               # A retired batch may finish after replay metadata eviction and
               # re-registration of the same wire identity. Claim only the
               # precise queued object this batch took, never its successor.
               if pending is None or pending[1] is not item:
                  continue
               self._pending_hops.pop(_hop_key(item))
            header, _, payload_digest, generation, source_node_id = pending
            try:
               completed.append(
                  self._complete_queued_hop(
                     header,
                     item,
                     runtime_result,
                     payload_digest,
                     generation,
                  )
               )
            except Exception as error:
               if on_item_error is None:
                  if unhandled is None:
                     unhandled = error
                  continue
               on_item_error(header, source_node_id, error)
         drained += 1
         if unhandled is not None:
            raise unhandled
      return tuple(completed)

   def update_batch_network_stats(
      self,
      placement_id: str,
      stats: BatchNetworkStats,
   ) -> None:
      self.batch_controller.update_network_stats(placement_id, stats)

   def batch_decisions(self):
      return self.batch_controller.decisions()

   def batch_execution_profiles(self):
      return self.batch_controller.execution_profiles()

   def batch_network_stats(self):
      return self.batch_controller.network_stats()

   def pending_batch_hops(self) -> int:
      return self.batch_scheduler.queue_depth()

   def begin_admission(self, request_id: str) -> None:
      """Announce a request accepted here whose path has not registered yet.

      A hop queued meanwhile may wait (at most the collection window) for it.
      """

      self._hop_arrivals.begin_admission(request_id, at=self.clock.now())

   def end_admission(self, request_id: str) -> None:
      self._hop_arrivals.end_admission(request_id)

   def hop_join_partners_due(self) -> int:
      """Active requests, not queued now, predicted to send a hop in the window.

      Zero means no other request can join the current collection window, so a
      live transport drains queued hops at once instead of waiting.
      """

      with self._path_lock:
         active = frozenset(self._paths)
      return self._hop_arrivals.partners_due(
         now=self.clock.now(),
         window_seconds=self.batch_controller.config.live_hop_collection_window_seconds,
         queued_paths=self.batch_scheduler.queued_path_ids(),
         active_paths=active,
      )

   def next_batch_deadline(self) -> float | None:
      now = self.clock.now()
      candidates = self.batch_scheduler.batch_candidates(now=now)
      if not candidates:
         return None
      decision = self.batch_controller.decide(candidates, now=now)
      return now if decision.action == "DISPATCH" else decision.ready_at

   def _complete_queued_hop(
      self,
      header: HopHeader,
      item: HopWorkItem,
      runtime_result: RuntimeResult,
      payload_digest: str,
      generation: int,
   ) -> HopReceiveResult:
      with self._path_lock:
         registration = self._paths.get(item.path_id)
         if registration is None or not self._path_is_current_locked(
            item.path_id,
            item.path_attempt,
            generation,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         self._refresh_path_locked(item.path_id)
         graph, manifest, request = registration
      hop = manifest.ordered_hops[header.hop_index]
      placement = next(
         placement
         for stage in graph.stages
         for placement in stage.placements
         if placement.placement_id == hop.placement_id
      )
      if not runtime_result.success:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope=runtime_result.failure_scope or "PLACEMENT",
            reason=runtime_result.failure_reason or "runtime_failure",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.hop_index + 1 < len(manifest.ordered_hops):
         next_hop = manifest.ordered_hops[header.hop_index + 1]
         next_header = HopHeader(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=header.phase,
            token_index=header.token_index,
            hop_index=header.hop_index + 1,
            source_placement_id=hop.placement_id,
            destination_placement_id=next_hop.placement_id,
            topology_version=manifest.topology_version,
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=header.phase,
               token_index=header.token_index,
               hop_index=header.hop_index + 1,
            ),
            prefill_chunk_token_count=header.prefill_chunk_token_count,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_hop(next_header, runtime_result.payload),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult("FORWARDED", forwarded_header=next_header)
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "VERIFY":
         return self._complete_verify_hop(
            header,
            request,
            manifest,
            hop,
            placement,
            runtime_result,
            payload_digest,
            generation,
         )

      final_prefill_chunk = (
         self.decode_mode == "stage_local_kv"
         and header.phase == "PREFILL_CHUNK"
         and item.position + header.prefill_chunk_token_count
         >= len(request.prompt_token_ids)
      )
      if (
         header.phase == "DECODE" or final_prefill_chunk
      ) and runtime_result.token_id is None:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "FAILED",
            reason=report.reason,
            failure_report=report,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result

      if header.phase == "PREFILL_CHUNK":
         event = PrefillChunkCompleted(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            chunk_index=header.token_index,
            token_count=header.prefill_chunk_token_count,
         )
         token_event = (
            TokenEvent(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=0,
               token_id=runtime_result.token_id,
               sampling_counter=1,
            )
            if final_prefill_chunk
            else None
         )

         def send_prefill_result() -> None:
            self.transport.send_prefill_chunk_completed(event)
            if token_event is not None:
               self.transport.send_token_event(token_event)

         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            send_prefill_result,
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult(
            "COMPLETED",
            prefill_chunk_completed=event,
            token_event=token_event,
         )
         self._remember_hop(header, result, payload_digest, generation)
         return result
      if header.phase != "DECODE":
         result = HopReceiveResult("COMPLETED")
         self._remember_hop(header, result, payload_digest, generation)
         return result
      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         token_id=runtime_result.token_id,
         sampling_counter=header.token_index + 1,
      )
      if not self._send_if_path_current(
         header.path_id,
         header.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return HopReceiveResult("REJECTED", "path_cancelled")
      result = HopReceiveResult("COMPLETED", token_event=event)
      self._remember_hop(header, result, payload_digest, generation)
      return result

   def _complete_verify_hop(
      self,
      header: HopHeader,
      request: RequestContext,
      manifest: PathManifest,
      hop,
      placement,
      runtime_result: RuntimeResult,
      payload_digest: str,
      generation: int,
   ) -> HopReceiveResult:
      """Final stage of an A11 VERIFY: return every position's decision to the entry."""

      decisions: tuple[int, ...] | None = None
      if runtime_result.token_id is None and isinstance(runtime_result.payload, bytes):
         try:
            decisions = decode_token_ids(runtime_result.payload)
         except PayloadError:
            decisions = None
      if decisions is None or len(decisions) != header.prefill_chunk_token_count:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=header.token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_verify_decisions",
            placement_id=hop.placement_id,
            node_id=placement.node_id,
         )
         if not self._send_if_path_current(
            header.path_id,
            header.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return HopReceiveResult("REJECTED", "path_cancelled")
         result = HopReceiveResult("FAILED", reason=report.reason, failure_report=report)
         self._remember_hop(header, result, payload_digest, generation)
         return result
      event = VerifyEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=header.token_index,
         decisions=tuple(int(item) for item in decisions),
      )
      if not self._send_if_path_current(
         header.path_id,
         header.path_attempt,
         generation,
         lambda: self.transport.send_verify_event(event),
      ):
         return HopReceiveResult("REJECTED", "path_cancelled")
      result = HopReceiveResult("COMPLETED", verify_event=event)
      self._remember_hop(header, result, payload_digest, generation)
      return result

   @staticmethod
   def _hop_deadline(
      request: RequestContext,
      phase: str,
      *,
      now: float,
   ) -> float:
      if phase in {"PREFILL", "PREFILL_CHUNK"}:
         return request.admitted_at + max(0.0, request.target_ttft_ms) / 1_000.0
      if phase in {"DECODE", "VERIFY"}:
         return now + max(0.0, request.target_tpot_ms) / 1_000.0
      return float("inf")

   @staticmethod
   def _runtime_batch_key(
      *,
      graph: ExecutionGraph,
      placement,
      phase: str,
      token_span: int,
   ) -> RuntimeBatchKey | None:
      if phase not in {"PREFILL", "PREFILL_CHUNK", "RECOVERY_PREFILL", "DECODE", "VERIFY"}:
         return None
      if token_span <= 0:
         return None
      verify = phase == "VERIFY"
      return RuntimeBatchKey(
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         model_commit=graph.resolved_commit,
         manifest_digest=graph.manifest_digest,
         placement_id=placement.placement_id,
         assignment_id=placement.assignment_id,
         stage_signature=placement.stage_signature,
         load_proof_digest=placement.load_proof_digest,
         runtime_backend=placement.runtime_backend,
         phase=phase,
         hidden_size=graph.hidden_size,
         activation_bytes=graph.activation_bytes,
         token_span=token_span,
         speculative_role="TARGET_VERIFY" if verify else "NONE",
         speculative_width=token_span - 1 if verify else 0,
      )

   def execute_manifest(
      self,
      *,
      graph: ExecutionGraph,
      manifest: PathManifest,
      request: RequestContext,
      phase: str,
      token_index: int,
      payload: object,
      expected_generation: int | None = None,
   ) -> RelayOutcome:
      with self._admitted_path_activity(manifest.path_id) as admit:
         return self._execute_manifest_inner(
            graph=graph,
            manifest=manifest,
            request=request,
            phase=phase,
            token_index=token_index,
            payload=payload,
            expected_generation=expected_generation,
            admit=admit,
         )

   def _execute_manifest_inner(
      self,
      *,
      graph: ExecutionGraph,
      manifest: PathManifest,
      request: RequestContext,
      phase: str,
      token_index: int,
      payload: object,
      expected_generation: int | None = None,
      admit,
   ) -> RelayOutcome:
      generation = (
         self._path_generation(manifest.path_id)
         if expected_generation is None
         else expected_generation
      )
      with self._path_lock:
         if not self._path_is_current_locked(
            manifest.path_id,
            manifest.path_attempt,
            generation,
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
      execution_key = (
         manifest.path_id,
         manifest.path_attempt,
         phase,
         token_index,
      )
      now = self.clock.now()
      self._evict_outcomes(now=now)
      try:
         accepted_payload = snapshot_payload(payload)
         payload_digest = payload_fingerprint(accepted_payload)
      except TypeError:
         return RelayOutcome(
            failure_report=FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope="REQUEST",
               reason="unsupported_payload_type",
            )
         )
      cached = self._outcomes.get(execution_key)
      if cached is not None:
         if cached[3] != payload_digest:
            return RelayOutcome(
               failure_report=FailureReport(
                  request_id=request.request_id,
                  path_id=manifest.path_id,
                  path_attempt=manifest.path_attempt,
                  token_index=token_index,
                  scope="REQUEST",
                  reason="idempotency_payload_mismatch",
               )
            )
         return cached[0]
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      edge_map = {
         (edge.from_placement_id, edge.to_placement_id): edge
         for edge in graph.edges
      }
      current_payload = accepted_payload
      final_result = None
      for hop_index, hop in enumerate(manifest.ordered_hops):
         work = HopWorkItem(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase=phase,
            token_index=token_index,
            hop_index=hop_index,
            placement_id=hop.placement_id,
            qos_class=request.qos_class,
            deficit_ratio=0.0,
            enqueued_at=self.clock.now(),
            idempotency_key=hop_idempotency_key(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=phase,
               token_index=token_index,
               hop_index=hop_index,
            ),
            payload=current_payload,
            position=self._kv_position(request, phase, token_index),
            terminal=self._is_terminal(request, phase, token_index),
            lease_expires_at=hop.reservation_expires_at,
            batch_key=self._runtime_batch_key(
               graph=graph,
               placement=placement_map[hop.placement_id],
               phase=phase,
               token_span=(
                  1
                  if phase == "DECODE"
                  else len(payload)
                  if phase in {"PREFILL", "RECOVERY_PREFILL"}
                  and isinstance(payload, (tuple, list))
                  else 0
               ),
            ),
         )
         self.scheduler.enqueue(work)
         with self._path_lock:
            if not self._path_is_current_locked(
               manifest.path_id,
               manifest.path_attempt,
               generation,
            ):
               self.scheduler.withdraw(work)
               return self._path_cancelled_outcome(
                  request,
                  manifest,
                  token_index,
               )
            admit()
         selected = self.scheduler.take(work)
         if selected is None:
            # The item is no longer this dispatch's to execute: fail closed by
            # name instead of executing whatever the queue would hand out.
            return self._path_cancelled_outcome(request, manifest, token_index)
         result = self._execute_runtime(selected)
         with self._path_lock:
            if not self._path_is_current_locked(
               manifest.path_id,
               manifest.path_attempt,
               generation,
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
         if not result.success:
            placement = placement_map[hop.placement_id]
            edge_id = ""
            if hop_index:
               previous = manifest.ordered_hops[hop_index - 1].placement_id
               edge = edge_map.get((previous, hop.placement_id))
               edge_id = edge.edge_id if edge is not None else ""
            report = FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope=result.failure_scope or "PLACEMENT",
               reason=result.failure_reason or "runtime_failure",
               placement_id=hop.placement_id,
               edge_id=edge_id,
               node_id=placement.node_id,
            )
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
            outcome = RelayOutcome(failure_report=report)
            self._remember(
               execution_key,
               outcome,
               manifest.path_id,
               payload_digest,
               generation,
            )
            return outcome
         current_payload = result.payload
         final_result = result
         if hop_index + 1 < len(manifest.ordered_hops):
            next_hop = manifest.ordered_hops[hop_index + 1]
            header = HopHeader(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               phase=phase,
               token_index=token_index,
               hop_index=hop_index + 1,
               source_placement_id=hop.placement_id,
               destination_placement_id=next_hop.placement_id,
               topology_version=manifest.topology_version,
               idempotency_key=hop_idempotency_key(
                  request_id=request.request_id,
                  path_id=manifest.path_id,
                  path_attempt=manifest.path_attempt,
                  phase=phase,
                  token_index=token_index,
                  hop_index=hop_index + 1,
               ),
            )
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_hop(header, current_payload),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)

      if phase == "PREFILL" or (
         phase == "RECOVERY_PREFILL" and self.decode_mode == "stage_local_kv"
      ):
         if final_result is None or final_result.token_id is None:
            if self.decode_mode == "complete_context_replay":
               outcome = RelayOutcome()
               self._remember(
                  execution_key,
                  outcome,
                  manifest.path_id,
                  payload_digest,
                  generation,
               )
               return outcome
            report = FailureReport(
               request_id=request.request_id,
               path_id=manifest.path_id,
               path_attempt=manifest.path_attempt,
               token_index=token_index,
               scope="PLACEMENT",
               reason="final_stage_missing_token",
               placement_id=manifest.ordered_hops[-1].placement_id,
            )
            if not self._send_if_path_current(
               manifest.path_id,
               manifest.path_attempt,
               generation,
               lambda: self.transport.send_failure_report(report),
            ):
               return self._path_cancelled_outcome(request, manifest, token_index)
            outcome = RelayOutcome(failure_report=report)
            self._remember(
               execution_key,
               outcome,
               manifest.path_id,
               payload_digest,
               generation,
            )
            return outcome
         event = TokenEvent(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=0 if phase == "PREFILL" else token_index,
            token_id=final_result.token_id,
            sampling_counter=1 if phase == "PREFILL" else token_index + 1,
         )
         if not self._send_if_path_current(
            manifest.path_id,
            manifest.path_attempt,
            generation,
            lambda: self.transport.send_token_event(event),
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
         outcome = RelayOutcome(token_event=event)
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
         return outcome
      if phase != "DECODE":
         outcome = RelayOutcome()
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
         return outcome
      if final_result is None or final_result.token_id is None:
         report = FailureReport(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            token_index=token_index,
            scope="PLACEMENT",
            reason="final_stage_missing_token",
            placement_id=manifest.ordered_hops[-1].placement_id,
         )
         if not self._send_if_path_current(
            manifest.path_id,
            manifest.path_attempt,
            generation,
            lambda: self.transport.send_failure_report(report),
         ):
            return self._path_cancelled_outcome(request, manifest, token_index)
         outcome = RelayOutcome(failure_report=report)
         self._remember(
            execution_key,
            outcome,
            manifest.path_id,
            payload_digest,
            generation,
         )
         return outcome

      event = TokenEvent(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         token_index=token_index,
         token_id=final_result.token_id,
         sampling_counter=token_index + 1,
      )
      final_hop = manifest.ordered_hops[-1]
      first_hop = manifest.ordered_hops[0]
      loopback_header = HopHeader(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="LOOPBACK",
         token_index=token_index,
         hop_index=0,
         source_placement_id=final_hop.placement_id,
         destination_placement_id=first_hop.placement_id,
         topology_version=manifest.topology_version,
         idempotency_key=hop_idempotency_key(
            request_id=request.request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            phase="LOOPBACK",
            token_index=token_index,
            hop_index=0,
         ),
      )

      if not self._send_if_path_current(
         manifest.path_id,
         manifest.path_attempt,
         generation,
         lambda: self.transport.send_token_event(event),
      ):
         return self._path_cancelled_outcome(request, manifest, token_index)
      if not self._send_if_path_current(
         manifest.path_id,
         manifest.path_attempt,
         generation,
         lambda: self.transport.send_hop(loopback_header, event.token_id),
      ):
         return self._path_cancelled_outcome(request, manifest, token_index)
      outcome = RelayOutcome(token_event=event)
      self._remember(
         execution_key,
         outcome,
         manifest.path_id,
         payload_digest,
         generation,
      )
      return outcome

   def receive_path_cancellation(
      self,
      cancellation: PathCancellation,
      *,
      source_node_id: str | None,
   ) -> bool:
      if (
         type(cancellation.request_id) is not str
         or not cancellation.request_id
         or type(cancellation.path_id) is not str
         or not cancellation.path_id
         or type(cancellation.path_attempt) is not int
         or type(cancellation.topology_version) is not int
         or type(source_node_id) is not str
         or not source_node_id
      ):
         return False
      with self._path_operation_lock(cancellation.path_id):
         with self._path_lock:
            registered = self._paths.get(cancellation.path_id)
            provisional = self._provisional_paths.get(cancellation.path_id)
            if registered is None and provisional is None:
               claimed_owner = (source_node_id, cancellation.request_id)
               cancelled_attempt = self._cancelled_attempt_for_locked(
                  cancellation.path_id, claimed_owner, source_node_id
               )
               if (
                  self._was_cancelled_attempt_locked(
                     cancellation.path_id,
                     cancellation.path_attempt,
                     claimed_owner,
                  )
                  or (
                     cancelled_attempt is not None
                     and cancellation.path_attempt <= cancelled_attempt
                  )
               ):
                  return False
               return self._remember_pending_cancellation_locked(
                  cancellation,
                  source_node_id,
               )
            if registered is not None:
               graph, manifest, request = registered
               expected = (
                  request.request_id,
                  manifest.path_attempt,
                  graph.topology_version,
               )
               entry_node_id = self._entry_node_by_path.get(cancellation.path_id)
            else:
               assert provisional is not None
               expected = provisional[:3]
               entry_node_id = provisional[3]
            if (
               not source_node_id
               or not entry_node_id
               or type(cancellation.path_attempt) is not int
               or type(cancellation.topology_version) is not int
               or source_node_id != entry_node_id
               or (
                  cancellation.request_id,
                  cancellation.path_attempt,
                  cancellation.topology_version,
               )
               != expected
            ):
               return False
            registered_for_release = registered
            if not self._release_path_state_locked(
               cancellation.path_id,
               path_attempt=cancellation.path_attempt,
               authority=source_node_id,
            ):
               return False
         self._release_synchronized_capacity(registered_for_release)
         self._release_path_resources(cancellation.path_id)
      return True

   def apply_controlled_path_cancellation(
      self,
      cancellation: PathCancellation,
   ) -> bool:
      """Install an owner-authoritative tombstone before path publication.

      Physical control has already generation-fenced the exact request/path
      identity.  Unlike a data-plane cancellation, it does not depend on an
      already-published entry node.  This closes the lifecycle gap where
      cancellation can win after request control is bound but before
      ``register_path`` publishes its provisional or registered path.
      """

      if (
         type(cancellation.request_id) is not str
         or not cancellation.request_id
         or type(cancellation.path_id) is not str
         or not cancellation.path_id
         or type(cancellation.path_attempt) is not int
         or type(cancellation.topology_version) is not int
      ):
         return False
      # Owner control must publish the generation tombstone immediately.  A
      # live execution owns the per-path operation lock across runtime work and
      # transport delivery; waiting for that lock here serialized cancellation
      # behind the operation it was meant to interrupt.  Every publication
      # boundary already validates the generation under ``_path_lock``, so
      # advancing it atomically is the safe preemption point.  The active
      # operation observes the new generation and cannot republish state.
      with self._path_lock:
         registered = self._paths.get(cancellation.path_id)
         provisional = self._provisional_paths.get(cancellation.path_id)
         if registered is not None:
            graph, manifest, request = registered
            if (
               request.request_id,
               manifest.path_attempt,
               graph.topology_version,
            ) != (
               cancellation.request_id,
               cancellation.path_attempt,
               cancellation.topology_version,
            ):
               return False
         elif provisional is not None and provisional[:3] != (
            cancellation.request_id,
            cancellation.path_attempt,
            cancellation.topology_version,
         ):
            return False
         self._release_path_state_locked(
            cancellation.path_id,
            path_attempt=cancellation.path_attempt,
            authority=self.node_id,
         )
      self._release_synchronized_capacity(registered)
      self._release_path_resources(cancellation.path_id)
      return True

   def _cancel_runtime(self, path_id: str) -> None:
      try:
         self.runtime.cancel(path_id)
      except Exception:
         pass

   def _release_path_resources(self, path_id: str) -> None:
      self.scheduler.release_path(path_id)
      self.batch_scheduler.release_path(path_id)
      self._hop_arrivals.forget(path_id)
      self._cancel_runtime(path_id)

   def _release_synchronized_capacity(
      self,
      registered: tuple[ExecutionGraph, PathManifest, RequestContext] | None,
   ) -> None:
      """Retire host-local charges imported for one registered remote path."""

      if registered is None:
         return
      release = getattr(
         self.builder.capacity,
         "release_synchronized_build",
         None,
      )
      if not callable(release):
         return
      manifest = registered[1]
      release(tuple(hop.reservation_id for hop in manifest.ordered_hops))

   def _release_path_state_locked(
      self,
      path_id: str,
      *,
      path_attempt: int | None = None,
      authority: str | None = None,
   ) -> bool:
      """Retire the path. ``authority`` is who decided it: the authenticated
      entry, or this node (``None``: a local release, e.g. a runtime
      failure)."""
      registered = self._paths.get(path_id)
      if registered is not None:
         registered_attempt = registered[1].path_attempt
         if path_attempt is not None and path_attempt != registered_attempt:
            return False
         path_attempt = registered_attempt
      provisional = self._provisional_paths.get(path_id)
      if provisional is not None:
         provisional_attempt = provisional[1]
         if path_attempt is not None and path_attempt != provisional_attempt:
            return False
         path_attempt = provisional_attempt
      known_path = (
         registered is not None
         or provisional is not None
         or path_attempt is not None
         or path_id in self._path_generations
      )
      if path_attempt is not None:
         self._record_cancelled_attempt_locked(path_id, path_attempt, authority=authority)
         owner = self._path_owners.get(path_id)
         creator = self._path_creators.get(path_id, self.node_id)
         if (
            authority is None
            and owner is not None
            and self._binding_class(owner, creator) != ""
         ):
            # A local release of a path another participant created: the
            # owner's own registrations of this exact attempt stay refused
            # (its in-flight ManifestLocked never revives a failed attempt).
            self._record_cancelled_attempt_locked(
               path_id, path_attempt, owner, creator=creator, exact=True
            )
      if known_path:
         self._advance_generation_locked(path_id)
      self._outcomes = {
         key: value
         for key, value in self._outcomes.items()
         if value[2] != path_id
      }
      self._hop_results = {
         key: value
         for key, value in self._hop_results.items()
         if value[2] != path_id
      }
      self._prefill_results = {
         key: value
         for key, value in self._prefill_results.items()
         if value[2] != path_id
      }
      self._paths.pop(path_id, None)
      self._provisional_paths.pop(path_id, None)
      self._path_activity.pop(path_id, None)
      self._path_registered_at.pop(path_id, None)
      self._path_busy.pop(path_id, None)
      self._entry_node_by_path.pop(path_id, None)
      self._pending_hops = {
         key: value
         for key, value in self._pending_hops.items()
         if value[1].path_id != path_id
      }
      self._prune_terminal_path_metadata_locked()
      return known_path

   def release_path(
      self,
      path_id: str,
      *,
      path_attempt: int | None = None,
   ) -> None:
      # Owner-controlled cancellation publishes the exact-attempt tombstone
      # without waiting for a live send's per-path operation lock. Entry
      # cleanup then calls release_path for the same attempt. Waiting for that
      # operation lock a second time can strand infer_cancel_wait behind the
      # data-plane operation it already fenced, even though all relay state and
      # runtime resources are gone. The tombstone is authoritative and blocks
      # late same-attempt registration, so this exact retired attempt is a
      # complete idempotent release. A newer registered attempt is deliberately
      # excluded from the fast path and remains protected by the operation lock.
      if path_attempt is not None:
         with self._path_lock:
            if path_id not in self._paths and path_id not in self._provisional_paths:
               retired = self._cancelled_attempt_for_locked(
                  path_id, self._path_owners.get(path_id), self._path_creators.get(path_id)
               )
               if (
                  self._was_cancelled_attempt_locked(path_id, path_attempt)
                  or (retired is not None and path_attempt <= retired)
                  or self._exactly_released_locked(
                     path_id, self._path_owners.get(path_id), path_attempt
                  )
               ):
                  return
      with self._path_operation_lock(path_id):
         with self._path_lock:
            registered = self._paths.get(path_id)
            known_path = self._release_path_state_locked(
               path_id,
               path_attempt=path_attempt,
            )
         if known_path:
            self._release_synchronized_capacity(registered)
            self._release_path_resources(path_id)

   def cached_outcome_count(self) -> int:
      return len(self._outcomes)

   def _remember_prefill(
      self,
      header: HopHeader,
      result: ProgressivePrefillResult,
      payload_digest: str,
      generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            generation,
         ):
            return
         self._prefill_results[_hop_key(header)] = (
            result,
            self.clock.now(),
            header.path_id,
            payload_digest,
         )
         self._evict_prefill_results(now=self.clock.now())

   def _evict_prefill_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _, _) in tuple(self._prefill_results.items()):
         if now - created_at > retention:
            self._prefill_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _, _) in self._prefill_results.items()
      )
      while len(self._prefill_results) > maximum:
         _, key = oldest.pop(0)
         self._prefill_results.pop(key, None)

   def _remember_hop(
      self,
      header: HopHeader,
      result: HopReceiveResult,
      payload_digest: str,
      expected_generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            header.path_id,
            header.path_attempt,
            expected_generation,
         ):
            return
         self._hop_results[_hop_key(header)] = (
            result,
            self.clock.now(),
            header.path_id,
            payload_digest,
         )
         self._evict_hop_results(now=self.clock.now())

   def _evict_hop_results(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      for key, (_, created_at, _, _) in tuple(self._hop_results.items()):
         if now - created_at > retention:
            self._hop_results.pop(key, None)
      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _, _) in self._hop_results.items()
      )
      while len(self._hop_results) > maximum:
         _, key = oldest.pop(0)
         self._hop_results.pop(key, None)

   def _remember(
      self,
      key: tuple[str, int, str, int],
      outcome: RelayOutcome,
      path_id: str,
      payload_digest: str,
      expected_generation: int,
   ) -> None:
      with self._path_lock:
         if not self._path_is_current_locked(
            path_id,
            key[1],
            expected_generation,
         ):
            return
         self._outcomes[key] = (
            outcome,
            self.clock.now(),
            path_id,
            payload_digest,
         )
         self._evict_outcomes(now=self.clock.now())

   def _evict_outcomes(self, *, now: float) -> None:
      retention = max(
         0.0,
         self.scheduler.config.idempotency_retention_seconds,
      )
      expired = [
         key
         for key, (_, created_at, _, _) in self._outcomes.items()
         if now - created_at > retention
      ]
      for key in expired:
         self._outcomes.pop(key, None)

      maximum = max(
         1,
         self.scheduler.config.maximum_idempotency_entries,
      )
      oldest = sorted(
         (created_at, key)
         for key, (_, created_at, _, _) in self._outcomes.items()
      )
      while len(self._outcomes) > maximum:
         _, key = oldest.pop(0)
         self._outcomes.pop(key, None)
