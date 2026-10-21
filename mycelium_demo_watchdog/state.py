# SPDX-License-Identifier: AGPL-3.0-only
"""The watchdog state machine: four states, a legal-transition table, and decide().

Semantics (DEMO-RESILIENCE-DESIGN.md §"Watchdog state machine"):

``PROBE``
    Every ~30 s.  Read-only: device reachability, local process liveness, origin
    health.  Never mutates anything.
``HEALTHY``
    All probes green; the renewal observer runs before the 75-minute deadline so
    qualification never lapses.
``DEGRADED``
    A device is offline.  Hold the stage, keep probing, never raise, never exit.
``RECOVER``
    The device is back.  Validate/re-stage, serve, observe, verify.  A physical
    round is spent only when the validated stage is actually invalidated.

``STOPPED`` is the only terminal state and is reachable only through an explicit
stop request; no probe outcome may ever terminate the watchdog.
"""
from __future__ import annotations

import enum
from typing import Mapping

from .probes import HealthReport

__all__ = [
    "LEGAL_TRANSITIONS",
    "IllegalTransition",
    "WatchdogState",
    "check_transition",
    "decide",
]


class WatchdogState(str, enum.Enum):
    PROBE = "PROBE"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVER = "RECOVER"
    STOPPED = "STOPPED"


class IllegalTransition(RuntimeError):
    """Raised when the machine is asked to take an undeclared edge."""


#: The complete edge set.  ``HEALTHY -> DEGRADED`` is the device drop;
#: ``DEGRADED -> RECOVER`` (never ``DEGRADED -> HEALTHY``) is the return, because
#: boot ids drift on sleep and a returned device must be re-bound before it counts
#: as serving.  ``STOPPED`` has no outgoing edge.
LEGAL_TRANSITIONS: Mapping[WatchdogState, frozenset[WatchdogState]] = {
    WatchdogState.PROBE: frozenset(
        {
            WatchdogState.HEALTHY,
            WatchdogState.DEGRADED,
            WatchdogState.RECOVER,
            WatchdogState.STOPPED,
        }
    ),
    WatchdogState.HEALTHY: frozenset(
        {
            WatchdogState.HEALTHY,
            WatchdogState.PROBE,
            WatchdogState.DEGRADED,
            WatchdogState.RECOVER,
            WatchdogState.STOPPED,
        }
    ),
    WatchdogState.DEGRADED: frozenset(
        {WatchdogState.DEGRADED, WatchdogState.RECOVER, WatchdogState.STOPPED}
    ),
    WatchdogState.RECOVER: frozenset(
        {WatchdogState.HEALTHY, WatchdogState.DEGRADED, WatchdogState.STOPPED}
    ),
    WatchdogState.STOPPED: frozenset(),
}


def check_transition(frm: WatchdogState, to: WatchdogState) -> None:
    """Raise :class:`IllegalTransition` unless ``frm -> to`` is a declared edge."""

    if to not in LEGAL_TRANSITIONS[frm]:
        raise IllegalTransition(f"illegal_watchdog_transition:{frm.value}->{to.value}")


def decide(
    current: WatchdogState,
    report: HealthReport,
    *,
    stop_requested: bool = False,
) -> WatchdogState:
    """The single decision rule, as a pure function of the probe report.

    Fail-closed in one direction only: an *unknown* probe result never produces a
    health claim.  A device that is not **known** reachable holds ``DEGRADED``
    rather than sending the driver at the stage.
    """

    if stop_requested or current is WatchdogState.STOPPED:
        return WatchdogState.STOPPED
    if report.all_green:
        # A returned device must be re-bound: 'looks green' after a hold is a
        # RECOVER, not a HEALTHY claim, because boot ids drift on sleep.
        return WatchdogState.RECOVER if current is WatchdogState.DEGRADED else WatchdogState.HEALTHY
    if report.device_reachable is not True:
        # Offline, or not known to be online: hold the stage and keep probing.
        return WatchdogState.DEGRADED
    return WatchdogState.RECOVER
