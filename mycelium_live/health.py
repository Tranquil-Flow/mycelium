"""Publish readiness only while the physical route is provably alive."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .route import LiveRoute


LIVE_QUALIFICATION_REFRESH_AFTER_MS = 55 * 60 * 1_000

class RouteHealthSource:
    """QualificationSource whose answer collapses the instant the route dies."""

    def __init__(
        self,
        *,
        route: LiveRoute,
        refresh: Callable[[], Any] | None = None,
        refresh_allowed: Callable[[], bool] | None = None,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self._route = route
        self._refresh = refresh
        self._refresh_allowed = refresh_allowed or (lambda: True)
        self._clock_unix_ms = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._lock = threading.RLock()
        self._record: Any | None = None
        self._dropped = False

    def publish(self, qualification: Any) -> None:
        with self._lock:
            if self._dropped:
                return
            self._record = qualification

    def drop(self) -> None:
        with self._lock:
            self._dropped = True
            self._record = None

    def current(self) -> Any | None:
        with self._lock:
            if self._dropped or self._record is None:
                return None
            if not self._route.is_alive():
                self.drop()
                return None
            record = self._record
            issued_at = getattr(record, "issued_at_unix_ms", None)
            if (
                self._refresh is not None
                and type(issued_at) is int
                and self._clock_unix_ms() - issued_at
                >= LIVE_QUALIFICATION_REFRESH_AFTER_MS
                and self._refresh_allowed()
            ):
                try:
                    refreshed = self._refresh()
                except Exception:
                    # The browser's independent freshness bound continues to
                    # fail closed. Keep the last record only so a later read can
                    # retry renewal without restarting the physical route.
                    return record
                if not self._route.is_alive():
                    self.drop()
                    return None
                self._record = refreshed
                record = refreshed
            return record


__all__ = ["LIVE_QUALIFICATION_REFRESH_AFTER_MS", "RouteHealthSource"]
