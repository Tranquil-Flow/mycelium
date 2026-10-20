# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory diagnostics state: clock samples, the member's claim-loop history, relay observations."""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Any

from .clock import ClockSamples


class ClientLoop:
    """What the member's own control client saw: how long polls/acks took, and how they ended."""

    def __init__(self, maxlen: int = 256) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._last_command: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def record(self, kind: str, seconds: float, code: str | None, *, at: float | None = None,
               command: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._events.append({"kind": kind, "seconds": float(seconds), "code": code,
                                 "at": time.time() if at is None else at})
            if command is not None:
                self._last_command = dict(command)

    def summary(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            events = list(self._events)
            last_command = None if self._last_command is None else dict(self._last_command)
        polls = [e for e in events if e["kind"] == "poll"]
        acks = [e for e in events if e["kind"] == "ack"]
        errors: dict[str, int] = {}
        for event in events:
            if event["code"] is not None:
                errors[event["code"]] = errors.get(event["code"], 0) + 1
        ok = [e for e in events if e["code"] is None]
        consecutive = 0
        for event in reversed(events):
            if event["code"] is None:
                break
            consecutive += 1
        return {
            "polls": len(polls), "acks": len(acks), "errors": errors, "consecutive_errors": consecutive,
            "median_poll_seconds": _median([e["seconds"] for e in polls if e["code"] is None]),
            "median_ack_seconds": _median([e["seconds"] for e in acks if e["code"] is None]),
            "last_success_age_s": None if not ok else round(max(0.0, now - ok[-1]["at"]), 3),
            "last_command": last_command,
        }


def _median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


class DiagState:
    def __init__(self, *, clock: Any = time.time) -> None:
        self.clock = ClockSamples(clock=clock)
        self.client = ClientLoop()
        self.relay_first_seen: dict[int, float] = {}
        self.relay_generations = 0
        self.relay_last_id: int | None = None
        self.host_cache: tuple[float, dict[str, Any]] | None = None
        self.route_cache: tuple[float, Any] | None = None
        self.lock = threading.Lock()


def state_for(app: Any) -> DiagState:
    state = getattr(app, "_diag_state", None)
    if not isinstance(state, DiagState):
        state = DiagState()
        try:
            app._diag_state = state
        except Exception:  # a frozen stand-in: the state then lives only for this call
            pass
    return state
