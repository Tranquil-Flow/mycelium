# SPDX-License-Identifier: AGPL-3.0-only
"""Sliding-window counters (pure; clock injected; bounded memory)."""
from __future__ import annotations

from collections import OrderedDict, deque
import threading


class WindowLimiter:
    def __init__(self, limit: int, window: float = 60.0, *, max_keys: int = 8192) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
                if len(self._hits) >= self.max_keys:
                    return False  # fail closed under a flood of fresh keys
            bucket = self._hits[key] = deque()
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True

    def refund(self, key: str) -> None:
        bucket = self._hits.get(key)
        if bucket:
            bucket.pop()

    def _prune(self, now: float) -> None:
        for key in [k for k, b in self._hits.items() if not b or now - b[-1] >= self.window]:
            del self._hits[key]


class DailyBudget:
    """Per-key requests and tokens per UTC day, in memory only (an address is never written to disk).

    ``reserve`` takes one request and up to ``wanted`` tokens at once and returns the tokens granted (``None``
    when the key has no request or no token left today); ``release`` hands back what a refused request took.
    Memory is bounded: past ``max_keys`` the oldest keys are evicted, so a flood of fresh keys can never lock
    anyone out (the swarm-wide budget is the backstop); a restart forgets the counts.
    """

    def __init__(self, requests: int, tokens: int, *, max_keys: int = 8192) -> None:
        self.requests, self.tokens, self.max_keys = requests, tokens, max_keys
        self._used: OrderedDict[str, list[int]] = OrderedDict()  # key -> [day, requests, tokens]
        self._lock = threading.Lock()

    def _row(self, key: str, day: int) -> list[int]:
        row = self._used.get(key)
        if row is None or row[0] != day:
            row = self._used[key] = [day, 0, 0]
        self._used.move_to_end(key)
        while len(self._used) > self.max_keys:
            self._used.popitem(last=False)
        return row

    def reserve(self, key: str, day: int, wanted: int) -> int | None:
        with self._lock:
            row = self._row(key, day)
            left = self.tokens - row[2]
            if row[1] >= self.requests or left < 1:
                return None
            taken = min(wanted, left)
            row[1] += 1
            row[2] += taken
            return taken

    def release(self, key: str, day: int, *, requests: int = 1, tokens: int = 0) -> None:
        with self._lock:
            row = self._used.get(key)
            if row is not None and row[0] == day:
                row[1] = max(row[1] - requests, 0)
                row[2] = max(row[2] - tokens, 0)

    def left(self, key: str, day: int) -> tuple[int, int]:
        with self._lock:
            row = self._used.get(key)
            used_r, used_t = (row[1], row[2]) if row is not None and row[0] == day else (0, 0)
        return max(self.requests - used_r, 0), max(self.tokens - used_t, 0)

    def size(self) -> int:
        with self._lock:
            return len(self._used)
