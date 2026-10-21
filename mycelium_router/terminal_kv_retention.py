# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded retention of a stage's KV after it executed a request's terminal hop.

Every stage releases a path's KV once it has executed the terminal hop (normal
completion).  For a request under A7 coverage that is too early: the final
token is committed only when the last stage's result reaches the entry, so a
covered source lost while the final decode step is still outstanding leaves
each surviving stage without the KV the fenced cutover must rekey
(``kv_checkpoint_state_missing``), and the request falls back to A6.

A port with retention enabled (only nodes wired for A7) keeps that KV aside
instead of discarding it.  The path is still released for every other purpose
exactly as before: the ``normal_completion`` marker, release count and active
state accounting do not change, and any further work frame for the path is
refused ``kv_state_released``.  Only a fenced successor rekey may take the
retained KV back.  It is discarded on whichever comes first:

* the request's terminal release (the entry's ``KVShadowRelease`` or its own
  relay cleanup, both arriving as ``cancel(path_id)``), a cancellation, or an
  A6 fallback release;
* the retention deadline (``min(KV lease deadline, now + seconds)``, the
  lease deadline including its progress renewals);
* eviction of the oldest entry beyond ``max_entries``;
* runtime close (node shutdown).

All methods run under the owning port's state lock.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable
from typing import Any

from mycelium_router.leases import kv_lease_deadline

DEFAULT_TERMINAL_KV_RETENTION_SECONDS = 30.0
DEFAULT_TERMINAL_KV_RETENTION_ENTRIES = 64


class TerminalKVRetention:
   """Path id -> (KV state, deadline), oldest first; disabled until enabled."""

   def __init__(self) -> None:
      self.seconds = 0.0
      self.max_entries = 0
      self._held: OrderedDict[str, tuple[Any, float]] = OrderedDict()
      self.release_counts: Counter[str] = Counter()
      self.retained_total = 0
      self.reclaimed_total = 0

   @property
   def enabled(self) -> bool:
      return self.seconds > 0 and self.max_entries > 0

   def enable(self, *, seconds: float, max_entries: int) -> None:
      if (
         isinstance(seconds, bool)
         or not isinstance(seconds, (int, float))
         or not seconds > 0
         or seconds == float("inf")
         or type(max_entries) is not int
         or max_entries < 1
      ):
         raise ValueError("invalid_terminal_kv_retention")
      self.seconds = float(seconds)
      self.max_entries = max_entries

   def __contains__(self, path_id: object) -> bool:
      return path_id in self._held

   def __len__(self) -> int:
      return len(self._held)

   def hold(self, path_id: str, state: Any, *, now: float) -> list[tuple[str, Any, str]]:
      """Retain ``state``; returns the entries this displaced (expired or evicted)."""

      displaced = self.expire(now=now)
      # The KV's own deadline includes its progress renewals.
      deadline = min(float(now) + self.seconds, kv_lease_deadline(state))
      self._held[path_id] = (state, deadline)
      self._held.move_to_end(path_id)
      self.retained_total += 1
      while len(self._held) > self.max_entries:
         evicted_path, (evicted, _deadline) = self._held.popitem(last=False)
         displaced.append((evicted_path, evicted, "evicted"))
      return displaced

   def take(
      self, path_id: str, *, now: float, accept: Callable[[Any], bool] = lambda _state: True
   ) -> tuple[Any | None, list[tuple[str, Any, str]]]:
      """Remove and return a live retained state ``accept`` approves; expired ones are displaced."""

      displaced = self.expire(now=now)
      held = self._held.get(path_id)
      if held is None or not accept(held[0]):
         return None, displaced
      del self._held[path_id]
      self.reclaimed_total += 1
      return held[0], displaced

   def drop(self, path_id: str) -> Any | None:
      held = self._held.pop(path_id, None)
      return None if held is None else held[0]

   def expire(self, *, now: float) -> list[tuple[str, Any, str]]:
      expired = [path_id for path_id, (_state, deadline) in self._held.items() if float(now) >= deadline]
      return [(path_id, self._held.pop(path_id)[0], "expired") for path_id in expired]

   def clear(self) -> list[tuple[str, Any]]:
      held = [(path_id, state) for path_id, (state, _deadline) in self._held.items()]
      self._held.clear()
      return held

   def kv_bytes(self, measure: Callable[[Any], int]) -> int:
      return sum(measure(state) for state, _deadline in self._held.values())

   def snapshot(self, measure: Callable[[Any], int]) -> dict[str, Any]:
      return {
         "enabled": self.enabled,
         "retained_count": len(self._held),
         "retained_kv_bytes": self.kv_bytes(measure),
         "retained_total": self.retained_total,
         "reclaimed_by_rekey_total": self.reclaimed_total,
         "release_counts": dict(sorted(self.release_counts.items())),
      }
