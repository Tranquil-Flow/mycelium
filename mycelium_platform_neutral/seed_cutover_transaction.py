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

import hashlib
import json
import os
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

PRODUCTION_ROOT_NAME = "mycelium-physical-run"
PRODUCTION_ROOTS_ENV = "MYCELIUM_PRODUCTION_SEED_ROOTS"


def default_production_roots(
    *, home: Path | None = None, environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Roots a cutover must never touch: ``<home>/mycelium-physical-run`` plus any
    absolute paths listed (``os.pathsep``-separated) in ``MYCELIUM_PRODUCTION_SEED_ROOTS``."""
    base = Path(home) if home is not None else Path.home()
    roots = [base / PRODUCTION_ROOT_NAME]
    listed = (os.environ if environ is None else environ).get(PRODUCTION_ROOTS_ENV, "")
    for item in listed.split(os.pathsep):
        candidate = Path(item) if item else None
        if candidate is not None and candidate.is_absolute() and candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


PRODUCTION_ROOTS = default_production_roots()
_IDENTITY_FIELDS = ("seed_key_digest", "swarm_id")
_AUTHORITY_FIELDS = ("authority_epoch", "authority_digest")


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


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def state_readback(*, seed_db: Path, seed_key: Path, swarm_id: str) -> dict[str, Any]:
    """Identity, contract, authority and member generations read from the seed's own durable state.

    ``incumbent_digest`` covers what defines the prior authority: the contract, the identity
    binding, the authority state (epoch and rotation) and the member set with each member's
    key digest.  A member's ``generation`` and ``incarnation`` are deliberately *not* in it:
    an owned restart re-registers every member (generation + 1, new incarnation), so the digest
    is equal across a faithful rollback and changes at a cutover.  Generation and incarnation
    are reported per member and checked on their own.  Process facts (pids, births, listener)
    are the fleet's.
    """
    from mycelium_node.identity import load_node_signer
    from mycelium_seed.state import SqliteSeedState

    signer = load_node_signer(Path(seed_key))
    state = SqliteSeedState(Path(seed_db))
    binding = state.identity_binding()
    if binding["seed_key_digest"] != signer.verification_key_digest or binding["swarm_id"] != swarm_id:
        raise CutoverRefused("seed_not_bound_to_this_key_and_swarm")
    contract = state.membership_contract()
    authority = state.authority_state()
    members = {
        str(m["node_id"]): {"generation": int(m["generation"]), "key_digest": str(m["verification_key_digest"]),
                            "incarnation": str(m["incarnation"])}
        for m in state.load_members()}
    authority_digest = _canonical_digest(authority)
    return {
        "contract": contract["version"], "seed_key_digest": binding["seed_key_digest"], "swarm_id": binding["swarm_id"],
        "authority_epoch": authority["authority_generation"], "authority_digest": authority_digest,
        "members": {n: dict(v) for n, v in members.items()},
        "incumbent_digest": _canonical_digest({
            "contract": contract, "binding": dict(binding), "authority": authority_digest,
            "members": {n: {"key_digest": v["key_digest"]} for n, v in sorted(members.items())}}),
    }


def merge_process_facts(state: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    """Join the fleet's process facts onto the seed-state readback (unknown members refused)."""
    members = {n: dict(v) for n, v in _members(state).items()}
    for node, process in _members(facts).items():
        if node not in members or not isinstance(process, Mapping):
            raise CutoverRefused("process_facts_unknown_member")
        members[node].update(pid=process.get("pid"), birth=process.get("birth"))
    if set(members) != set(_members(facts)):
        raise CutoverRefused("process_facts_member_set_mismatch")
    return {**state, "members": members,
            **{k: facts.get(k) for k in ("owned", "listener_up", "seed_pid", "seed_birth")}}


def stop_evidence(*, seed_port: int, pids: list[int], host: str = "127.0.0.1") -> dict[str, Any]:
    """Observe that nothing listens on the seed port and the owned pids are gone."""
    with socket.socket() as probe:
        probe.settimeout(2.0)
        listening = probe.connect_ex((host, seed_port)) == 0
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        alive.append(pid)
    return {"seed_listener_absent": not listening, "owned_pids_alive": alive}


def _members(doc: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    value = doc.get("members")
    return value if isinstance(value, Mapping) else {}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rollback_checks(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, bool]:
    """The incumbent after an owned restart: same authority, each member restarted exactly once.

    Equal generations are never required: every owned restart re-registers the members.
    """
    b, a = _members(before), _members(after)
    same_members = bool(b) and set(b) == set(a)
    authority = (
        all(before.get(k) == after.get(k) for k in (*_IDENTITY_FIELDS, "contract", "incumbent_digest", *_AUTHORITY_FIELDS))
        and bool(after.get("incumbent_digest")) and same_members
        and all(b[n].get("key_digest") == a[n].get("key_digest") for n in b))
    advanced = same_members and all(
        _is_int(b[n].get("generation")) and _is_int(a[n].get("generation"))
        and a[n]["generation"] == b[n]["generation"] + 1 for n in b)
    restarted = same_members and all(
        isinstance(a[n].get("incarnation"), str) and a[n]["incarnation"] and isinstance(b[n].get("incarnation"), str)
        and b[n]["incarnation"] and a[n]["incarnation"] != b[n]["incarnation"] for n in b)
    checks = {"incumbent_authority_preserved": authority, "member_generations_advanced_by_one": advanced,
              "member_incarnations_restarted": restarted, "seed_listening": after.get("listener_up") is True}
    return {**checks, "incumbent_unchanged": all(checks.values())}


# A committed cutover moves each member's generation exactly twice: the seed commit binds
# generation + 1 (``prepare_seed_cutover``: ``post_generation == generation + 1``) and the owned
# restart re-registers the member once more (+ 1).  Adoption is member-side and does not touch
# the seed's generation.  Anything else is unexplained.
COMMITTED_GENERATION_ADVANCE = 2


def _committed_checks(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, bool]:
    b, a = _members(before), _members(after)
    return {
        "seed_process_replaced": (after.get("seed_pid"), after.get("seed_birth")) != (before.get("seed_pid"), before.get("seed_birth")),
        "seed_listening": after.get("listener_up") is True,
        "contract_v2": after.get("contract") == "v2",
        "identity_preserved": all(before.get(k) == after.get(k) for k in _IDENTITY_FIELDS) and set(a) == set(b)
        and all(a[n].get("key_digest") == b[n].get("key_digest") for n in b),
        "member_generations_advanced_by_commit_and_restart": set(a) == set(b) and bool(b) and all(
            _is_int(b[n].get("generation")) and _is_int(a[n].get("generation"))
            and a[n]["generation"] == b[n]["generation"] + COMMITTED_GENERATION_ADVANCE for n in b),
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
    except Exception as exc:  # noqa: BLE001 - recorded by type and bounded message, restart still proceeds
        failure = f"cutover_failed:{type(exc).__name__}:{str(exc)[:120]}"
        receipt["phases"].append("cutover_failed")
    after = fleet.start(epoch)
    receipt["phases"].append("restarted")
    receipt["after"] = after
    if failure is None or failure.startswith("cutover_failed"):
        if "committed" not in receipt["phases"]:
            receipt["checks"] = _rollback_checks(before, after)
            unchanged = receipt["checks"]["incumbent_unchanged"]
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
