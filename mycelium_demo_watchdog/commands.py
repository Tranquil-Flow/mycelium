# SPDX-License-Identifier: AGPL-3.0-only
"""Configurable command paths for the recovery driver.

The watchdog does not re-implement bring-up.  It *drives* the existing
program-dir steps, referenced as command paths so nothing here rewrites them.
The defaults below point at the two directories the packet names
(``.../m1-real-origin/route-bringup/`` and
``.../takeover-20260925-1501/live-h4/``), with the argv shapes taken verbatim
from the recorded round-7 receipts and the round-7 standing-origin script.

Every default is a *reference*: the operator re-verifies the paths and the
resolved candidate commit at install time.  Nothing in this module executes.

Verdict contract for ``stage_validate``: exit 0 plus ``valid`` on stdout means
the validated stage is still bound to the current boot ids; exit 0 plus
``invalidated`` means it must be re-staged.  Anything else (non-zero exit,
timeout, unrecognised output) is *unknown* and the driver holds, fail-closed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "DEFAULT_H4_RENEWAL_WATCH_DIR",
    "DEFAULT_LIVE_H4_DIR",
    "DEFAULT_ROUTE_BRINGUP_DIR",
    "PYTHON",
    "RecoveryCommands",
    "StepSpec",
    "default_recovery_commands",
]

#: Program-dir interpreter discipline.
PYTHON = "/opt/homebrew/bin/python3.14"

PROGRAM_DIR = Path(os.path.expanduser("~/Documents/mycelium-core-program"))
DEFAULT_ROUTE_BRINGUP_DIR = PROGRAM_DIR / "runs/v2/m1-real-origin/route-bringup"
DEFAULT_LIVE_H4_DIR = PROGRAM_DIR / "runs/v2/takeover-20260925-1501/live-h4"
DEFAULT_H4_RENEWAL_WATCH_DIR = PROGRAM_DIR / "runs/v2/takeover-20260925-1501/h4-renewal-watch"

#: Reference deployment inputs for the recorded round-7 fleet.  Operator-owned.
REFERENCE_REPO = PROGRAM_DIR / "runs/v2/l2-batch12-package/checkout-2a819df"
REFERENCE_PLAN = DEFAULT_ROUTE_BRINGUP_DIR / "operator-plan-fresh-07.json"
REFERENCE_SEED_ROOT = Path(os.path.expanduser("~/mycelium-physical-run/w6-ethonline-20260912T090309Z/seed-r7"))
REFERENCE_PRODUCT_STATE = DEFAULT_ROUTE_BRINGUP_DIR / "product-state-47"
REFERENCE_STATIC_ROOT = REFERENCE_REPO / "ui/web/dist"
REFERENCE_LEDGER = DEFAULT_LIVE_H4_DIR / "ATTEMPT-LEDGER.json"


@dataclass(frozen=True)
class StepSpec:
    """One named step: a label plus the exact argv to invoke."""

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryCommands:
    """The recovery driver's whole contact surface with the world.

    There is deliberately no stop/tear-down member: the watchdog may never take
    the stage down, so a DEGRADED hold has nothing to call.
    """

    stage_validate: tuple[str, ...] | None = None
    bind_fresh: tuple[str, ...] | None = None
    stage_prepare: tuple[str, ...] | None = None
    stage_refresh: tuple[str, ...] | None = None
    serve: tuple[str, ...] | None = None
    observer: tuple[str, ...] | None = None
    verify: tuple[str, ...] | None = None
    #: Pre-gates may be written either as :class:`StepSpec` objects or as plain
    #: ``(name, argv)`` pairs; both are accepted and normalised by :meth:`gates`.
    pre_gates: tuple[StepSpec | tuple[str, Sequence[str]], ...] = ()

    def gates(self) -> tuple[StepSpec, ...]:
        """The pre-gates as :class:`StepSpec`, in the order they must go green."""

        return tuple(
            item
            if isinstance(item, StepSpec)
            else StepSpec(str(item[0]), tuple(str(part) for part in item[1]))
            for item in self.pre_gates
        )

    def as_mapping(self) -> Mapping[str, tuple[str, ...] | None]:
        return {
            "stage_validate": self.stage_validate,
            "bind_fresh": self.bind_fresh,
            "stage_prepare": self.stage_prepare,
            "stage_refresh": self.stage_refresh,
            "serve": self.serve,
            "observer": self.observer,
            "verify": self.verify,
        }


def default_recovery_commands(
    *,
    repo: Path = REFERENCE_REPO,
    plan: Path = REFERENCE_PLAN,
    seed_root: Path = REFERENCE_SEED_ROOT,
    product_state: Path = REFERENCE_PRODUCT_STATE,
    static_root: Path = REFERENCE_STATIC_ROOT,
    route_bringup: Path = DEFAULT_ROUTE_BRINGUP_DIR,
    live_h4: Path = DEFAULT_LIVE_H4_DIR,
    renewal_watch: Path = DEFAULT_H4_RENEWAL_WATCH_DIR,
    python: str = PYTHON,
) -> RecoveryCommands:
    """The reference wiring.  Re-verify on device return before installing."""

    def script(path: object, *args: object) -> tuple[str, ...]:
        return (python, "-B", str(path), *(str(arg) for arg in args))

    gates = (
        StepSpec("45_preflight_route", script(route_bringup / "45_preflight_route.py", repo, plan, seed_root)),
        StepSpec(
            "46_preflight_stack",
            script(route_bringup / "46_preflight_stack_r7.py", repo, plan, seed_root, product_state, static_root),
        ),
        StepSpec(
            "47_preflight_receipt_plumbing",
            script(route_bringup / "47_preflight_receipt_plumbing.py", repo, plan, seed_root, product_state),
        ),
        StepSpec("48_probe_deployment_projection", script(route_bringup / "48_probe_deployment_projection.py", repo, plan)),
    )
    return RecoveryCommands(
        stage_validate=script(route_bringup / "49_probe_assignment_binding.py", plan),
        bind_fresh=script(route_bringup / "40_bind_fresh_plan_r8.py", route_bringup / "operator-plan-fresh-08.json"),
        stage_prepare=script(repo / "scripts/stage_live_route.py", "--operator-plan", "fresh-08", "--command", "prepare"),
        stage_refresh=script(route_bringup / "43_refresh_both_r8.py"),
        serve=script(
            python,
            "-B",
            "-m",
            "mycelium_demo",
            "serve",
            "--mode",
            "live",
            "--operator-plan",
            route_bringup / "operator-plan-fresh-08.json",
            "--product-state-root",
            product_state,
        ),
        observer=script(renewal_watch / "observer.py", "--observe"),
        verify=script(live_h4 / "verify.py", "--output", live_h4 / "SELF-VERIFY-02.json"),
        pre_gates=gates,
    )
