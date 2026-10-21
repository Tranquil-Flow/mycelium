# SPDX-License-Identifier: AGPL-3.0-only
"""A16 re-entry feeds from real authority round trips.

Leaving a non-normal mode needs ``authority_observations`` fresh observations and, after a
withdraw, a renewed eligibility.  Both come only from the authority accepting something this
node sent, never from a local flag:

* **authority observation** = a lease renewal the A1 seed accepted (``authority.renew()``)
  while the member then reads current/fresh;
* **requalification** = a new capability report the authority accepted (A9 eligibility
  renewal, ``authority.report_capability()``), only after a withdraw.

Traffic starts only once the host has already been calm for the recovery window (so a node in
trouble does not poll the authority), at most once per sample interval, and never while the
owner pressed Stop or Pause sharing (wire key ``game_mode``) is on.  A failure records nothing.  Load proof and route
qualification are not claimed here: the withdrawn worker was closed, so ordinary
qualification must run again before any request is placed.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .adaptive_host import AdaptiveHostController


class ReentryFeed:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def tick(self, controller: AdaptiveHostController, authority: Any) -> dict[str, Any]:
        if not controller.recovery_ready():
            return {"action": "none"}
        now = self._clock()
        if self._last is not None and 0 <= now - self._last < controller.sample_interval_s:
            return {"action": "wait"}
        self._last = now
        report: dict[str, Any] = {"action": "observe", "authority": False, "requalified": False}
        generation = controller.generation
        if not self._renewed(authority):
            return report
        controller.record_authority_observation(generation, observed_at=self._clock())
        report["authority"] = True
        if controller.needs_requalification():
            try:
                result = authority.report_capability()
            except Exception:
                return report
            if isinstance(result, dict) and result.get("state") == "self_report_accepted":
                controller.mark_requalified(generation)
                report["requalified"] = True
        return report

    @staticmethod
    def _renewed(authority: Any) -> bool:
        try:
            if getattr(authority, "member", None) is None:
                return False
            authority.renew()
            status = authority.status()
            return status.get("membership_state") == "current" and status.get("lease_state") == "fresh"
        except Exception:
            return False


def feed_for(app: Any) -> ReentryFeed:
    """The app's single re-entry feed (shared by the runtime loop and the detached policy tick)."""
    feed = getattr(app, "_adaptive_reentry", None)
    if feed is None:
        feed = app._adaptive_reentry = ReentryFeed(
            clock=lambda: getattr(app, "_adaptive_clock", time.monotonic)())
    return feed
