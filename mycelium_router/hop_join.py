# SPDX-License-Identifier: AGPL-3.0-only
"""Which other requests could still join a live hop batch on this node.

A live node collects inbound hops for a bounded window before it drains them as
one runtime batch.  Waiting only pays off while another request active on this
node is about to send its next hop; when nobody else can join, the wait is pure
added latency.  ``HopArrivalTracker`` predicts that from what the node itself
observed: each request's last hop arrival here and the interval between its last
two arrivals (its per-token period at this node).

A request is a *partner* when it is not queued right now, its last hop here was
not terminal, it was seen within the join horizon, and its next hop is predicted
no later than the end of the collection window.  An overdue partner still counts:
while this node executes a batch, the next hops of its peers wait in the inbound
transport, so "late" usually means "already here".  A request seen only once has
no period yet; it is a partner while that arrival is inside the horizon.

A request the entry node has *accepted but not yet registered* (its infer
command is in flight, its path is not known to the relay) is announced with
``begin_admission`` and counts as a partner until its path registers, the
admission ends, or the join horizon passes.  Without this a request accepted a
millisecond after another would be invisible when the first one's hop is
queued, and pairing would be a race.  Waiting for an announced admission is
still bounded by the collection window.  The tracker is bounded and holds no
payload, token or KV material: only path identity, two timestamps and a
terminal flag, plus request identities of in-flight admissions.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass
from threading import Lock

MAXIMUM_TRACKED_PATHS = 4_096


@dataclass
class _Arrivals:
   last_at: float
   period: float | None
   terminal: bool
   registered_only: bool = False


class HopArrivalTracker:
   """Bounded per-path record of hop arrivals at this node."""

   def __init__(
      self,
      *,
      join_horizon_seconds: float,
      maximum_paths: int = MAXIMUM_TRACKED_PATHS,
   ):
      if not join_horizon_seconds > 0:
         raise ValueError("invalid_join_horizon")
      if maximum_paths <= 0:
         raise ValueError("invalid_maximum_tracked_paths")
      self._horizon = float(join_horizon_seconds)
      self._maximum = int(maximum_paths)
      self._paths: OrderedDict[str, _Arrivals] = OrderedDict()
      self._admitting: OrderedDict[str, float] = OrderedDict()
      self._lock = Lock()

   def begin_admission(self, request_id: str, *, at: float) -> None:
      """A request was accepted here and its path is about to register."""

      with self._lock:
         self._admitting.pop(request_id, None)
         self._admitting[request_id] = float(at)
         while len(self._admitting) > self._maximum:
            self._admitting.popitem(last=False)

   def end_admission(self, request_id: str) -> None:
      """The request registered its path, failed, or was withdrawn."""

      with self._lock:
         self._admitting.pop(request_id, None)

   def admitting(self) -> int:
      with self._lock:
         return len(self._admitting)

   def record(self, path_id: str, *, at: float, terminal: bool) -> None:
      """Note one accepted hop arrival for ``path_id`` at monotonic time ``at``."""

      with self._lock:
         previous = self._paths.pop(path_id, None)
         period = None
         if (
            previous is not None
            and not previous.registered_only
            and at > previous.last_at
         ):
            period = at - previous.last_at
         self._paths[path_id] = _Arrivals(
            last_at=float(at), period=period, terminal=bool(terminal)
         )
         self._bound_locked()

   def record_registration(self, path_id: str, *, at: float) -> None:
      """A path just registered here: its first hop is expected soon.

      Registration is not a hop, so it never yields a period.
      """

      with self._lock:
         if path_id in self._paths:
            return
         self._paths[path_id] = _Arrivals(
            last_at=float(at), period=None, terminal=False, registered_only=True
         )
         self._bound_locked()

   def _bound_locked(self) -> None:
      while len(self._paths) > self._maximum:
         self._paths.popitem(last=False)

   def forget(self, path_id: str) -> None:
      with self._lock:
         self._paths.pop(path_id, None)

   def tracked(self) -> int:
      with self._lock:
         return len(self._paths)

   def partners_due(
      self,
      *,
      now: float,
      window_seconds: float,
      queued_paths: Collection[str],
      active_paths: Collection[str],
   ) -> int:
      """How many non-queued active requests may send a hop within the window."""

      window = max(0.0, float(window_seconds))
      due = 0
      with self._lock:
         for request_id in [
            request_id
            for request_id, began in self._admitting.items()
            if now - began > self._horizon
         ]:
            self._admitting.pop(request_id, None)
         due += len(self._admitting)
         stale = [
            path_id
            for path_id, arrivals in self._paths.items()
            if now - arrivals.last_at > self._horizon
         ]
         for path_id in stale:
            self._paths.pop(path_id, None)
         for path_id, arrivals in self._paths.items():
            if (
               arrivals.terminal
               or path_id in queued_paths
               or path_id not in active_paths
            ):
               continue
            if arrivals.period is None:
               due += 1
               continue
            # Only a partner predicted after the window closes is not waited for.
            if arrivals.last_at + arrivals.period <= now + window:
               due += 1
      return due
