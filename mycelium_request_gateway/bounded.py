# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, insertion-ordered tables keyed by request id.

A table keyed by a request or session id grows with the number of requests the process ever sees unless every
request reaches its cleanup.  These evict oldest-first at a hard cap and, when a lifetime is given, by age, so a
request that is cancelled before it runs, never acknowledged, or abandoned by its client cannot pin memory.
Entries are looked up by exact id; nothing here scans or prunes on a timer.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Generic, TypeVar

V = TypeVar("V")

DEFAULT_MAX_TRACKED_REQUESTS = 131_072  # above 2 x the service's largest max_sessions (65_536)


def _validated_cap(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("invalid_max_entries")
    return value


def _validated_ttl(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError("invalid_ttl_seconds")
    return float(value)


class BoundedIdMap(Generic[V]):
    """``id -> value`` with a hard entry cap (oldest evicted first) and an optional age limit."""

    def __init__(
        self,
        max_entries: int,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = _validated_cap(max_entries)
        self._ttl = _validated_ttl(ttl_seconds)
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._lock = threading.RLock()

    def _expire(self) -> None:
        if self._ttl is None:
            return
        horizon = self._clock() - self._ttl
        while self._items:
            first = next(iter(self._items.values()))
            if first[0] > horizon:
                break
            self._items.popitem(last=False)

    def __setitem__(self, key: str, value: V) -> None:
        with self._lock:
            self._expire()
            self._items.pop(key, None)
            self._items[key] = (self._clock(), value)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def get(self, key: str, default: V | None = None) -> V | None:
        with self._lock:
            self._expire()
            entry = self._items.get(key)
            return default if entry is None else entry[1]

    def pop(self, key: str, default: V | None = None) -> V | None:
        with self._lock:
            self._expire()
            entry = self._items.pop(key, None)
            return default if entry is None else entry[1]

    def __contains__(self, key: object) -> bool:
        with self._lock:
            self._expire()
            return key in self._items

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._items)

    def keys(self) -> list[str]:
        with self._lock:
            self._expire()
            return list(self._items)


class BoundedIdSet:
    """A set of ids with the same eviction rules as :class:`BoundedIdMap`."""

    def __init__(
        self,
        max_entries: int,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._map: BoundedIdMap[bool] = BoundedIdMap(max_entries, ttl_seconds=ttl_seconds, clock=clock)

    def add(self, key: str) -> None:
        self._map[key] = True

    def discard(self, key: str) -> None:
        self._map.pop(key)

    def __contains__(self, key: object) -> bool:
        return key in self._map

    def __len__(self) -> int:
        return len(self._map)

    def __iter__(self) -> Iterator[str]:
        return iter(self._map.keys())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BoundedIdSet):
            return set(self) == set(other)
        if isinstance(other, (set, frozenset)):
            return set(self) == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


__all__ = ["BoundedIdMap", "BoundedIdSet", "DEFAULT_MAX_TRACKED_REQUESTS"]
