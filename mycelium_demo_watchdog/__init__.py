# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium_demo_watchdog`` — the always-on, self-healing owner of the demo.

The watchdog owns the demo lifecycle one layer above the product supervisor:
it probes (device, local process, origin), holds through a device drop, and
drives the existing round-bringup steps to recover — spending a physical round
only when the validated stage is actually invalidated.

Nothing here touches hardware and nothing here loads a service: the CLI refuses
to run live unless it is explicitly armed, and the LaunchAgent plist ships as a
template.
"""
from __future__ import annotations

from .commands import RecoveryCommands, StepSpec, default_recovery_commands
from .driver import (
    FAILED,
    HELD,
    RECOVERED,
    RecoveryDriver,
    RecoveryOutcome,
    StepReceipt,
)
from .ledger import RoundLedger, RoundLedgerError
from .probes import (
    HTTPHealthProbe,
    HealthReport,
    ProbeResult,
    ProcessProbe,
    SSHProbe,
    private_capability_headers,
    probe_once,
)
from .state import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    WatchdogState,
    check_transition,
    decide,
)
from .watchdog import DemoWatchdog, ObserverRunner, Transition, WatchdogConfig

__all__ = [
    "FAILED",
    "HELD",
    "HTTPHealthProbe",
    "HealthReport",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "ObserverRunner",
    "ProbeResult",
    "ProcessProbe",
    "RECOVERED",
    "RecoveryCommands",
    "RecoveryDriver",
    "RecoveryOutcome",
    "RoundLedger",
    "RoundLedgerError",
    "SSHProbe",
    "StepReceipt",
    "StepSpec",
    "Transition",
    "WatchdogConfig",
    "WatchdogState",
    "check_transition",
    "decide",
    "default_recovery_commands",
    "DemoWatchdog",
    "private_capability_headers",
    "probe_once",
]

__version__ = "0.1.0"
