# SPDX-License-Identifier: AGPL-3.0-only
"""The recovery driver: validate, gate, re-stage, serve, observe, verify.

Call order inside :meth:`RecoveryDriver.recover` -- and therefore the order the
round-spend guard is enforced in:

1. ``stage_validate``            (read-only; unknown => hold, fail-closed)
2. pre-gates 45/46/47/48         (every one must be green *before any serve*)
3. ``bind_fresh`` -> ``stage_prepare`` -> ``stage_refresh``   (only when invalidated)
4. **the round is charged here**, immediately before serve, with no later step
   able to un-charge it
5. ``serve`` -> ``observer`` -> ``verify``

A RECOVER with red or undecided pre-gates returns ``HELD`` with
``round_spent == 0`` and never reaches a serve.  There is no stop path: the
driver cannot take the stage down, so a hold is always the safe outcome.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .commands import RecoveryCommands, StepSpec
from .ledger import RoundLedger, RoundLedgerError
from .probes import scrubbed_env

__all__ = [
    "FAILED",
    "HELD",
    "RECOVERED",
    "RecoveryDriver",
    "RecoveryOutcome",
    "STAGE_INVALIDATED",
    "STAGE_UNKNOWN",
    "STAGE_VALID",
    "StepReceipt",
]

STAGE_VALID = "valid"
STAGE_INVALIDATED = "invalidated"
STAGE_UNKNOWN = "unknown"

RECOVERED = "RECOVERED"
HELD = "HELD"
FAILED = "FAILED"

_STEP_FAILURE_REASON = {
    "bind-fresh": "bind_fresh_failed",
    "stage-prepare": "stage_prepare_failed",
    "stage-refresh": "refresh_failed",
}

_STDOUT_TAIL_CHARS = 400


@dataclass(frozen=True)
class StepReceipt:
    """What one driven step did.  ``ok`` is ``None`` when it could not decide."""

    name: str
    argv: tuple[str, ...]
    exit_code: int | None
    ok: bool | None
    error: str | None
    elapsed_seconds: float
    stdout_tail: str = ""


@dataclass
class RecoveryOutcome:
    state: str
    round_spent: int
    reason: str
    receipts: list[StepReceipt] = field(default_factory=list)
    stage_state: str = STAGE_UNKNOWN
    pre_gates: Mapping[str, bool | None] = field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return self.state == RECOVERED


class RecoveryDriver:
    def __init__(
        self,
        commands: RecoveryCommands,
        ledger: RoundLedger,
        *,
        runner: Callable[..., Any] = subprocess.run,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        timeout: float = 180.0,
        source_commit: str = "unresolved",
        spend_receipt: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.commands = commands
        self.ledger = ledger
        self.runner = runner
        self.env = env
        self.clock = clock
        self.wall_clock = wall_clock
        self.timeout = float(timeout)
        self.source_commit = source_commit
        self.spend_receipt = spend_receipt
        self.log = log or (lambda _message: None)
        self._receipts: list[StepReceipt] = []
        self.serve_invocations = 0

    # ----------------------------------------------------------------- invoking

    def _run(self, name: str, argv: tuple[str, ...] | None) -> StepReceipt:
        """Run one step under a hard bound.  Never raises; ``ok=None`` if undecided."""

        if not argv:
            receipt = StepReceipt(name, (), None, None, "no_command_configured", 0.0)
            self._receipts.append(receipt)
            return receipt
        argv = tuple(str(item) for item in argv)
        started = self.clock()
        try:
            completed = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=scrubbed_env(self.env),
            )
        except subprocess.TimeoutExpired:
            receipt = StepReceipt(name, argv, None, None, "timeout", self.clock() - started)
        except BaseException as exc:  # noqa: BLE001 - a step must never take the loop down
            receipt = StepReceipt(
                name, argv, None, None, f"transport_error:{type(exc).__name__}", self.clock() - started
            )
        else:
            exit_code = int(completed.returncode)
            receipt = StepReceipt(
                name,
                argv,
                exit_code,
                exit_code == 0,
                None if exit_code == 0 else f"exit_{exit_code}",
                self.clock() - started,
                (completed.stdout or "")[-_STDOUT_TAIL_CHARS:],
            )
        self._receipts.append(receipt)
        self.log(f"step={name} exit={receipt.exit_code} ok={receipt.ok}")
        return receipt

    def _run_spec(self, spec: StepSpec) -> StepReceipt:
        return self._run(spec.name, spec.argv)

    # ------------------------------------------------------------------ queries

    def stage_state(self) -> str:
        """``valid`` / ``invalidated`` / ``unknown`` -- unknown on anything odd."""

        if not self.commands.stage_validate:
            return STAGE_UNKNOWN
        receipt = self._run("stage-validate", self.commands.stage_validate)
        if receipt.ok is not True:
            return STAGE_UNKNOWN
        verdict = (receipt.stdout_tail or "").strip().lower()
        if verdict.endswith(STAGE_VALID) and STAGE_INVALIDATED not in verdict:
            return STAGE_VALID
        if verdict.endswith(STAGE_INVALIDATED):
            return STAGE_INVALIDATED
        return STAGE_UNKNOWN

    def run_pre_gates(self) -> dict[str, bool | None]:
        """All pre-gates must be green before any serve; fail fast on the first red."""

        verdicts: dict[str, bool | None] = {}
        for spec in self.commands.gates():
            receipt = self._run_spec(spec)
            verdicts[spec.name] = receipt.ok
            if receipt.ok is not True:
                break
        return verdicts

    # ----------------------------------------------------------------- recovery

    def recover(self) -> RecoveryOutcome:
        self._receipts = []
        stage = self.stage_state()
        if stage == STAGE_UNKNOWN:
            return self._outcome(HELD, 0, "stage_state_unknown", stage, {})

        gates = self.run_pre_gates()
        if not gates or not all(value is True for value in gates.values()):
            return self._outcome(HELD, 0, "pre_gates_red", stage, gates)

        round_spent = 0
        if stage == STAGE_VALID:
            reason = "recovered_reusing_validated_stage"
        else:
            if not self.ledger.has_headroom():
                return self._outcome(HELD, 0, "round_budget_exhausted", stage, gates)
            for spec in self._restage_specs():
                receipt = self._run_spec(spec)
                if receipt.ok is not True:
                    return self._outcome(HELD, 0, _STEP_FAILURE_REASON[spec.name], stage, gates)
            try:
                self.ledger.spend(
                    {
                        "source_commit": self.source_commit,
                        "receipt": self.spend_receipt,
                        "at": self.wall_clock(),
                    }
                )
            except RoundLedgerError as exc:
                self.log(f"ledger_refused:{exc}")
                return self._outcome(HELD, 0, "round_record_refused", stage, gates)
            round_spent = 1
            reason = "recovered_with_new_stage"

        self.serve_invocations += 1
        serve = self._run("serve", self.commands.serve)
        if serve.ok is not True:
            return self._outcome(FAILED, round_spent, "serve_failed", stage, gates)

        if self.commands.observer:
            observer = self._run("observer", self.commands.observer)
            if observer.ok is not True:
                return self._outcome(FAILED, round_spent, "observer_failed", stage, gates)

        verify = self._run("verify", self.commands.verify)
        if verify.ok is not True:
            return self._outcome(FAILED, round_spent, "verify_failed", stage, gates)

        return self._outcome(RECOVERED, round_spent, reason, stage, gates)

    def _restage_specs(self) -> tuple[StepSpec, ...]:
        return (
            StepSpec("bind-fresh", self.commands.bind_fresh or ()),
            StepSpec("stage-prepare", self.commands.stage_prepare or ()),
            StepSpec("stage-refresh", self.commands.stage_refresh or ()),
        )

    def _outcome(
        self,
        state: str,
        round_spent: int,
        reason: str,
        stage: str,
        gates: Mapping[str, bool | None],
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            state=state,
            round_spent=round_spent,
            reason=reason,
            receipts=list(self._receipts),
            stage_state=stage,
            pre_gates=dict(gates),
        )
