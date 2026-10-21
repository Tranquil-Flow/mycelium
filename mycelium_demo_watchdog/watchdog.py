# SPDX-License-Identifier: AGPL-3.0-only
"""The always-on owner of the demo lifecycle.

One tick = one read-only probe pass, then at most one decision:

* ``HEALTHY`` -- all green; run the renewal observer before the 75-minute
  deadline so qualification never lapses;
* ``DEGRADED`` -- hold the stage and keep probing: nothing is stopped, nothing is
  restarted, nothing is raised;
* ``RECOVER`` -- the device is back; hand off to the recovery driver, and only
  claim ``HEALTHY`` when the driver actually recovered.

The watchdog never exits on a probe outcome and owns no stop path.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .driver import HELD, RECOVERED, RecoveryOutcome
from .probes import HealthReport, probe_once, scrubbed_env
from .state import WatchdogState, check_transition, decide

__all__ = [
    "DEFAULT_STATE_DIR",
    "DemoWatchdog",
    "ObserverRunner",
    "Transition",
    "WatchdogConfig",
]

DEFAULT_STATE_DIR = Path(os.path.expanduser("~/mycelium-demo-watchdog"))
DEFAULT_CAPABILITY_PATH = Path(
    os.path.expanduser("~/Documents/mycelium-core-program/runs/v2"
    "/closeout-priority-20260925/public-h2/private/proxy.cap")
)


@dataclass(frozen=True)
class Transition:
    """A recorded state change; ``at`` is wall-clock seconds."""

    at: float
    frm: WatchdogState
    to: WatchdogState
    reason: str


class ObserverRunner:
    """Runs the renewal observer under a hard bound; returns ``True`` on exit 0."""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 300.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.argv = tuple(str(item) for item in argv)
        self.runner = runner
        self.timeout = float(timeout)
        self.env = env
        self.calls = 0

    def run(self) -> bool:
        self.calls += 1
        try:
            completed = self.runner(
                self.argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=scrubbed_env(self.env),
            )
        except BaseException:  # noqa: BLE001 - an unproved renewal is not a crash
            return False
        return int(completed.returncode) == 0


@dataclass(frozen=True)
class WatchdogConfig:
    """Everything the live watchdog needs; tests override only what they assert."""

    laptop_target: str = os.environ.get("MYCELIUM_MEMBER_MAC_SSH_HOST", "member@member-mac")
    laptop_key_path: str = os.environ.get("MYCELIUM_MEMBER_MAC_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519_m4pro_to_laptop"))
    ssh_connect_timeout: float = 6.0
    ssh_total_timeout: float = 8.0
    probe_interval_seconds: float = 30.0
    probe_process_timeout_seconds: float = 5.0
    origin_url: str = "http://127.0.0.1:18590/__mycelium/live-status"
    origin_headers: Mapping[str, str] = field(default_factory=dict)
    renewal_interval_seconds: float = 55 * 60
    renewal_deadline_seconds: float = 75 * 60
    observer_timeout_seconds: float = 300.0
    source_commit: str = "unresolved"
    state_dir: Path = DEFAULT_STATE_DIR
    ledger_path: Path | None = None


class DemoWatchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        device_probe: Any,
        process_probe: Any,
        origin_probe: Any,
        driver: Any,
        observer: Any,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.device_probe = device_probe
        self.process_probe = process_probe
        self.origin_probe = origin_probe
        self.driver = driver
        self.observer = observer
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleeper = sleeper
        self.log = log or (lambda _message: None)

        self.state: WatchdogState = WatchdogState.PROBE
        self.transitions: list[Transition] = []
        self.degraded_ticks = 0
        #: Always zero: the watchdog owns no destructive path, so a hold cannot
        #: call one.  The suites assert this stays zero through every scenario.
        self.destructive_invocations = 0
        self.serve_start_wall: float | None = None
        self.last_health: HealthReport | None = None
        self.last_recovery: RecoveryOutcome | None = None
        self.last_recovery_error: str | None = None
        self.last_renewal_wall: float | None = None
        self.last_renewal_reason: str | None = None
        self.renewal_fresh = False
        self.renewals_attempted = 0
        self.renewals_succeeded = 0
        self._stop_requested = False

    # ------------------------------------------------------------------- probing

    def probe(self) -> HealthReport:
        report = probe_once(
            device=self.device_probe,
            process=self.process_probe,
            origin=self.origin_probe,
        )
        self.last_health = report
        return report

    def request_stop(self) -> None:
        self._stop_requested = True

    # ------------------------------------------------------------------ renewing

    def renewal_due(self, now_wall: float) -> str:
        """``due`` / ``not_due`` / ``window_missed`` for the 75-minute deadline."""

        start = self.serve_start_wall if self.serve_start_wall is not None else now_wall
        if now_wall >= start + self.config.renewal_deadline_seconds:
            return "window_missed"
        anchor = self.last_renewal_wall if self.last_renewal_wall is not None else start
        if now_wall - anchor >= self.config.renewal_interval_seconds:
            return "due"
        return "not_due"

    def _maybe_renew(self, now_wall: float) -> None:
        due = self.renewal_due(now_wall)
        if due == "not_due":
            return
        if due == "window_missed":
            # Fail-closed: a lapsed window is reported, never papered over.
            self.renewal_fresh = False
            self.last_renewal_reason = "renewal_window_missed"
            self.log("renewal_window_missed")
            return
        self.renewals_attempted += 1
        if self.observer.run():
            self.renewals_succeeded += 1
            self.last_renewal_wall = now_wall
            self.renewal_fresh = True
            self.last_renewal_reason = "renewed"
            self.log("renewal_proved")
        else:
            self.renewal_fresh = False
            self.last_renewal_reason = "renewal_unproven"
            self.log("renewal_unproven")

    # ------------------------------------------------------------------- tick

    def _record(self, target: WatchdogState, reason: str) -> Transition:
        check_transition(self.state, target)
        transition = Transition(self.wall_clock(), self.state, target, reason)
        self.state = target
        self.transitions.append(transition)
        self.log(f"transition {transition.frm.value}->{transition.to.value} reason={reason}")
        return transition

    def _recover(self) -> RecoveryOutcome:
        try:
            outcome = self.driver.recover()
        except BaseException as exc:  # noqa: BLE001 - a driver fault must not end the watchdog
            self.last_recovery_error = f"{type(exc).__name__}: {exc}"[:200]
            self.log(f"recovery_driver_exception {self.last_recovery_error}")
            return RecoveryOutcome(
                state=HELD, round_spent=0, reason="driver_exception", receipts=[], stage_state="unknown"
            )
        self.last_recovery = outcome
        return outcome

    def tick(self) -> Transition | None:
        if self.state is WatchdogState.STOPPED:
            return None
        now_wall = self.wall_clock()
        if self.serve_start_wall is None:
            self.serve_start_wall = now_wall

        report = self.probe()
        target = decide(self.state, report, stop_requested=self._stop_requested)

        if target is WatchdogState.STOPPED:
            return self._record(WatchdogState.STOPPED, "stop_requested")

        if target is WatchdogState.HEALTHY:
            transition = None
            if self.state is not WatchdogState.HEALTHY:
                transition = self._record(WatchdogState.HEALTHY, "all_probes_green")
            self._maybe_renew(now_wall)
            return transition

        if target is WatchdogState.DEGRADED:
            self.degraded_ticks += 1
            if self.state is WatchdogState.DEGRADED:
                return None
            reason = (
                "device_offline"
                if report.device_reachable is False
                else "device_not_known_reachable"
            )
            return self._record(WatchdogState.DEGRADED, reason)

        # target is RECOVER
        self._record(WatchdogState.RECOVER, "device_back_or_service_unhealthy")
        outcome = self._recover()
        if outcome.state == RECOVERED:
            self.degraded_ticks = 0
            return self._record(WatchdogState.HEALTHY, f"recovered:{outcome.reason}")
        self.degraded_ticks += 1
        return self._record(WatchdogState.DEGRADED, f"recovery_held:{outcome.reason}")

    # -------------------------------------------------------------------- run

    def run(
        self,
        *,
        max_ticks: int | None = None,
        max_seconds: float | None = None,
    ) -> list[Transition]:
        first = len(self.transitions)
        ticks = 0
        deadline = None if max_seconds is None else self.clock() + max_seconds
        while True:
            if self.state is WatchdogState.STOPPED:
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            if deadline is not None and self.clock() >= deadline:
                break
            self.tick()
            ticks += 1
            if self.state is WatchdogState.STOPPED:
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            if deadline is not None and self.clock() >= deadline:
                break
            self.sleeper(self.config.probe_interval_seconds)
        return self.transitions[first:]
