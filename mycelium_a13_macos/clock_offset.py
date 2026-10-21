# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-side record of each peer's clock offset (V1.1 item a).

A peer signs ``issued_at`` on its own clock; the owner knows when the request arrived on its clock.  With ``o`` the
peer's clock ahead of the owner's and ``d`` the one-way transit, ``signed - received = o - d``, so
``o = signed - received + d`` and ``d`` is estimated as half the round trip the peer measured for itself (0 when it
has not told us).  The median of the latest samples is kept per peer, so one slow packet does not decide.  This is a
diagnostic and an admission guard, not a security boundary.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

from .managed_control import MAX_CLOCK_SKEW_SECONDS

SAMPLES = 8
#: An observation older than this says nothing about the peer's clock now (it may have been fixed since).
STALE_AFTER_SECONDS = 300.0


class PeerClockOffsets:
    def __init__(self, *, clock: Callable[[], float] = time.time, samples: int = SAMPLES,
                 stale_after: float = STALE_AFTER_SECONDS,
                 tolerance: float = MAX_CLOCK_SKEW_SECONDS) -> None:
        self._clock = clock
        self._samples = samples
        self._stale_after = float(stale_after)
        self.tolerance = float(tolerance)
        self._lock = threading.Lock()
        self._offsets: dict[str, deque] = {}
        self._rtt: dict[str, float] = {}
        self._seen: dict[str, float] = {}

    @staticmethod
    def _finite(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value)

    def observe(self, node_id: str, *, signed_at: float, received_at: float | None = None,
                rtt_seconds: float | None = None) -> None:
        received = self._clock() if received_at is None else received_at
        if not (self._finite(signed_at) and self._finite(received)):
            return
        with self._lock:
            if self._finite(rtt_seconds) and 0 <= rtt_seconds <= 60:
                self._rtt[node_id] = float(rtt_seconds)
            transit = self._rtt.get(node_id, 0.0) / 2.0
            row = self._offsets.setdefault(node_id, deque(maxlen=self._samples))
            if node_id in self._seen and not self._fresh(node_id):
                row.clear()  # the old samples predate a silence in which the clock may have been fixed
            row.append(float(signed_at) - float(received) + transit)
            self._seen[node_id] = self._clock()

    def _fresh(self, node_id: str) -> bool:
        seen = self._seen.get(node_id)
        return seen is not None and self._clock() - seen <= self._stale_after

    def offset(self, node_id: str) -> float | None:
        with self._lock:
            if not self._fresh(node_id) or not self._offsets.get(node_id):
                return None
            return statistics.median(self._offsets[node_id])

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = self._clock()
            out: dict[str, dict[str, Any]] = {}
            for node, row in self._offsets.items():
                if not row:
                    continue
                value = statistics.median(row)
                fresh = self._fresh(node)
                out[node] = {'offset_seconds': round(value, 3), 'rtt_seconds': self._rtt.get(node),
                             'samples': len(row), 'age_seconds': round(now - self._seen[node], 3),
                             'fresh': fresh, 'tolerance_seconds': self.tolerance,
                             'exceeds_tolerance': fresh and abs(value) > self.tolerance}
            return out

    def exceeded(self, nodes: Iterable[str] | None = None) -> dict[str, float]:
        """Peers heard recently whose offset is beyond the tolerance: ``{node_id: offset_seconds}``."""
        wanted = None if nodes is None else set(nodes)
        return {node: row['offset_seconds'] for node, row in self.snapshot().items()
                if row['exceeds_tolerance'] and (wanted is None or node in wanted)}

    def view(self, node_id: str) -> dict[str, Any]:
        """The status block for one peer; unmeasured peers say so."""
        row = self.snapshot().get(node_id)
        if row is None:
            return {'offset_seconds': None, 'exceeds_tolerance': False, 'tolerance_seconds': self.tolerance,
                    'samples': 0}
        return row
