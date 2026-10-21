# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner side: wake idle members before a request and treat a reloading member as *warming*.

A member whose weights are unloaded still answers health and still holds its membership and route
registration, so the owner's liveness path sees it as alive.  This coordinator only asks it to
reload and waits a bounded time; past the deadline it raises ``member_warming`` (retriable) and
leaves the member warming.  It never records a miss, quarantine or loss: a member that really does
not come back stops answering health, or fails its own reload closed, and the existing loss path
(A5/A6) takes over.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .residency import FAILED, IDLE, LOADED, STATES, WARMING, MemberWarming, ResidencyError


def idle_state_of(observation: Any) -> str | None:
    """The member's residency state from a verified health observation, else ``None``."""
    if not isinstance(observation, Mapping):
        return None
    details = observation.get("details")
    detail = details.get("idle_residency") if isinstance(details, Mapping) else None
    state = detail.get("state") if isinstance(detail, Mapping) else None
    return state if state in STATES else None


@dataclass
class WarmResult:
    reload_ms: dict[str, int] = field(default_factory=dict)
    probed: tuple[str, ...] = ()


class WarmingCoordinator:
    def __init__(
        self, *, send_reload: Callable[[str], Any], health: Callable[[str], Any],
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = time.sleep,
        deadline_s: float = 30.0, poll_s: float = 0.25, recent_activity_s: float = 30.0,
    ) -> None:
        self._send_reload, self._health = send_reload, health
        self._clock, self._sleep = clock, sleep
        self._deadline_s, self._poll_s = deadline_s, poll_s
        self._recent_s = recent_activity_s
        self._last_used: dict[str, float] = {}

    def note_used(self, node_ids: Iterable[str]) -> None:
        now = self._clock()
        for node_id in node_ids:
            self._last_used[node_id] = now

    def _recently_used(self, node_id: str, now: float) -> bool:
        seen = self._last_used.get(node_id)
        return seen is not None and 0 <= now - seen < self._recent_s

    def ensure_warm(self, node_ids: Iterable[str], *, deadline_s: float | None = None) -> WarmResult:
        node_ids = list(node_ids)
        started = self._clock()
        end = started + (self._deadline_s if deadline_s is None else deadline_s)
        probed: list[str] = []
        pending: dict[str, float] = {}
        for node_id in sorted(set(node_ids)):
            if self._recently_used(node_id, started):
                continue
            probed.append(node_id)
            state = idle_state_of(self._health(node_id))
            if state in (None, LOADED):
                continue
            if state == FAILED:
                raise ResidencyError("member_reload_failed")
            if state == IDLE:
                self._send_reload(node_id)
            pending[node_id] = self._clock()
        result = WarmResult(probed=tuple(probed))
        while pending:
            if self._clock() >= end:
                raise MemberWarming(tuple(sorted(pending)))
            self._sleep(self._poll_s)
            for node_id in sorted(pending):
                state = idle_state_of(self._health(node_id))
                if state == FAILED:
                    raise ResidencyError("member_reload_failed")
                if state in (None, LOADED):
                    result.reload_ms[node_id] = int(round((self._clock() - pending.pop(node_id)) * 1000))
                elif state == IDLE:  # a lost wake-up: ask again
                    self._send_reload(node_id)
        self.note_used(node_ids)
        return result
