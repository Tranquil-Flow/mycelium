# SPDX-License-Identifier: AGPL-3.0-only
"""Owned stop -> commit -> adopt -> restart transaction for the A9 seed cutover.

The private fleet's ``up`` refuses existing attempt roots, so a cutover cannot
restart it in place.  This transaction instead owns the whole sequence over an
injected :class:`OwnedFleet`: it reads the owned fleet back, stops it and
requires the seed listener and every owned pid to be gone, runs the commit and
the member adoptions only then, restarts the fleet into a *fresh* restart epoch
root (never a spent one), and reads identity, contract version, member
generations and process replacement back.  A failed cutover restarts the
unchanged incumbent and proves it unchanged.  It never touches the production
seed root and never kills by pattern: stopping is the fleet's own owned stop.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

PRODUCTION_ROOTS = (Path("/Users/evinova-self/mycelium-physical-run"),)
_IDENTITY_FIELDS = ("seed_key_digest", "swarm_id")


class CutoverRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OwnedFleet(Protocol):
    def readback(self) -> dict[str, Any]: ...
    def stop(self) -> dict[str, Any]: ...
    def start(self, epoch: Path) -> dict[str, Any]: ...


def require_owned_root(root: Path, *, forbidden_roots: tuple[Path, ...] = PRODUCTION_ROOTS) -> Path:
    resolved = Path(os.path.realpath(root))
    for forbidden in forbidden_roots:
        banned = Path(os.path.realpath(forbidden))
        if resolved == banned or banned in resolved.parents:
            raise CutoverRefused("owned_root_forbidden")
    return resolved


def claim_restart_epoch(parent: Path, *, number: int | None = None) -> Path:
    """Create the next ``restart-N`` root exclusively; an existing one is never reused."""
    parent = Path(parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if number is None:
        number = 1 + max((int(p.name.split("-")[1]) for p in parent.glob("restart-[0-9]*")), default=0)
    epoch = parent / f"restart-{number}"
    if os.path.lexists(epoch):
        raise CutoverRefused("restart_epoch_exists")
    epoch.mkdir(mode=0o700)
    return epoch


class CommandOwnedFleet:
    """Adapter over three fixed argv lists (the run-day fleet's own readback/stop/start)."""

    def __init__(self, commands: Mapping[str, Any], *, timeout_s: float = 300.0) -> None:
        if set(commands) != {"readback", "stop", "start"} or not all(
                isinstance(v, list) and v and all(isinstance(a, str) for a in v) for v in commands.values()):
            raise CutoverRefused("fleet_command_invalid")
        self._commands = {k: list(v) for k, v in commands.items()}
        self._timeout = timeout_s

    def _run(self, name: str, extra: list[str] | None = None) -> dict[str, Any]:
        done = subprocess.run([*self._commands[name], *(extra or [])], capture_output=True, text=True,
                              timeout=self._timeout, check=False, shell=False)
        try:
            doc = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise CutoverRefused("fleet_command_output_invalid") from None
        if done.returncode != 0 or not isinstance(doc, dict):
            raise CutoverRefused("fleet_command_output_invalid")
        return doc

    def readback(self) -> dict[str, Any]:
        return self._run("readback")

    def stop(self) -> dict[str, Any]:
        return self._run("stop")

    def start(self, epoch: Path) -> dict[str, Any]:
        return self._run("start", [str(epoch)])


def _members(doc: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    value = doc.get("members")
    return value if isinstance(value, Mapping) else {}


def _unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    if any(before.get(k) != after.get(k) for k in (*_IDENTITY_FIELDS, "contract", "incumbent_digest")):
        return False
    b, a = _members(before), _members(after)
    return set(b) == set(a) and all(
        b[n].get("generation") == a[n].get("generation") and b[n].get("key_digest") == a[n].get("key_digest")
        for n in b)


def _committed_checks(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, bool]:
    b, a = _members(before), _members(after)
    return {
        "seed_process_replaced": (after.get("seed_pid"), after.get("seed_birth")) != (before.get("seed_pid"), before.get("seed_birth")),
        "seed_listening": after.get("listener_up") is True,
        "contract_v2": after.get("contract") == "v2",
        "identity_preserved": all(before.get(k) == after.get(k) for k in _IDENTITY_FIELDS) and set(a) == set(b)
        and all(a[n].get("key_digest") == b[n].get("key_digest") for n in b),
        "member_generations_advanced_by_one": set(a) == set(b) and bool(b) and all(
            a[n].get("generation") == b[n].get("generation") + 1 for n in b),
        "member_processes_replaced": all(
            (a[n].get("pid"), a[n].get("birth")) != (b[n].get("pid"), b[n].get("birth")) for n in b if n in a),
    }


def run_seed_cutover(
    fleet: OwnedFleet, *, cutover: Callable[[], Any], adopt: Mapping[str, Callable[[], Any]],
    epoch_parent: Path, owned_root: Path, expect_refusal: bool = False,
    forbidden_roots: tuple[Path, ...] = PRODUCTION_ROOTS,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {"protocol": "mycelium.a9_seed_cutover_transaction.v1", "phases": [],
                               "expect_refusal": expect_refusal, "checks": {}}

    def refuse(reason: str) -> dict[str, Any]:
        receipt.update(status="REFUSED", reason=reason)
        return receipt

    try:
        require_owned_root(owned_root, forbidden_roots=forbidden_roots)
    except CutoverRefused as refused:
        return refuse(refused.code)
    before = fleet.readback()
    receipt["before"] = before
    if before.get("owned") is not True or before.get("listener_up") is not True:
        return refuse("fleet_not_owned" if before.get("owned") is not True else "fleet_not_running")
    stopped = fleet.stop()
    receipt["stop"] = stopped
    receipt["phases"].append("stopped")
    if stopped.get("seed_listener_absent") is not True or stopped.get("owned_pids_alive") != []:
        return refuse("stop_not_confirmed")
    epoch = claim_restart_epoch(epoch_parent)
    receipt["restart_epoch"] = epoch.name
    failure: str | None = None
    try:
        cutover()
        receipt["phases"].append("committed")
        if expect_refusal:
            failure = "negative_cutover_not_refused"
        else:
            for node in sorted(adopt):
                adopt[node]()
                receipt["phases"].append(f"adopted:{node}")
    except Exception as exc:  # noqa: BLE001 - recorded by type, restart still proceeds
        failure = f"cutover_failed:{type(exc).__name__}"
        receipt["phases"].append("cutover_failed")
    after = fleet.start(epoch)
    receipt["phases"].append("restarted")
    receipt["after"] = after
    if failure is None or failure.startswith("cutover_failed"):
        if "committed" not in receipt["phases"]:
            unchanged = _unchanged(before, after) and after.get("listener_up") is True
            receipt["checks"]["incumbent_unchanged"] = unchanged
            receipt["reason"] = failure
            receipt["status"] = ("NEGATIVE_PRESERVED" if expect_refusal else "ROLLED_BACK") if unchanged else "ROLLBACK_DIVERGED"
            return receipt
        checks = _committed_checks(before, after)
        receipt["checks"] = checks
        receipt["status"] = "COMMITTED_VERIFIED" if all(checks.values()) else "POST_RESTART_READBACK_FAILED"
        return receipt
    # A negative cutover that committed anyway: the transaction reports it, never hides it.
    receipt["checks"] = _committed_checks(before, after)
    receipt.update(status="NEGATIVE_NOT_REFUSED", reason=failure)
    return receipt
