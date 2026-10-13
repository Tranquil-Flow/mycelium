# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sliding-window counters (pure; clock injected; bounded memory)."""
from __future__ import annotations

from collections import deque


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
