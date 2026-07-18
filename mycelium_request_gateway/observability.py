"""Label-free privacy-safe counters for request-gateway operations."""
from __future__ import annotations

import threading


_COUNTERS = (
    "requests_admitted_total",
    "admission_rejected_total",
    "token_events_total",
    "requests_completed_total",
    "requests_cancelled_total",
    "requests_failed_total",
)


class GatewayMetrics:
    """Fixed integer counters: no user, route, prompt, token, or endpoint labels."""

    def __init__(self) -> None:
        self._values = {name: 0 for name in _COUNTERS}
        self._lock = threading.Lock()

    def increment(self, name: str) -> None:
        if name not in self._values:
            raise ValueError("unknown_metric")
        with self._lock:
            self._values[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)
