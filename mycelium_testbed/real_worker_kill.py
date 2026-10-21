# SPDX-License-Identifier: AGPL-3.0-only
"""L3 capability: kill the **real** a13 managed worker process, on the shipped path.

PLAN v2 §2.2 (fidelity rule) / root cause R1: ``mycelium_testbed.soak``'s
"restart" re-subscribes a *simulated* mailbox (``soak.py`` ``_restart_owner`` /
``_restart_peer``), so nothing under ``mycelium_testbed`` ever constructed
``mycelium_a13_macos.managed_runtime.ManagedTargetRuntime`` or
``mycelium_a13_macos.worker.ManagedWorker`` - verified: this package contains no
reference to either name.  A physical failure inside
``managed_runtime.py:_replace_worker_if_dead`` therefore could not be reproduced
locally, and hardware became the only debugger.

This module is the missing **capability**, not a fix.  It observes and records;
it implements no worker replacement and repairs no shipped behaviour.  A green
scenario here would be a failure of packet ``m1-harness-real-worker-kill``.

What it does, all on one Mac against the shipped path:

1. :func:`real_worker_facts` - the worker must be a real process group leader
   (``os.getpgid(pid) == pid``), not a mailbox subscription.
2. :func:`kill_real_worker` - ``SIGKILL`` to that real process group and a
   bounded wait for the kernel to make the death visible.  A worker whose
   session has no real pid is *refused*, so a simulated death can never be
   reported as a real kill.
3. :class:`AuthorisationLog` - the ``authorize_load`` callable handed to the
   shipped lifecycle, recording every configure/start grant with a monotonic
   stamp, so "was the replacement re-authorised through the configure/start
   lifecycle?" is a measurement and not an opinion.
4. :func:`observe_recovery` - a *pure observer* (it issues no command of its
   own after the kill) that polls the shipped runtime until the route recovers,
   until the runtime publishes a bounded terminal failure, or until the
   recorded bound expires.
5. :func:`write_evidence` - one JSON record of the whole scenario.

Deliberately out of scope here (see ``research/l3-shipped-path-harness.md``):
the real 0.5B model load, real iroh inference between two processes, the fault
proxy, and the two-Mac physical boundary.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Mapping

#: Protocol tag of the scenario record this module writes.
EVIDENCE_PROTOCOL = "mycelium.testbed.real_worker_kill.v1"

#: The one signal this capability is about: an unannounced process death.
KILL_SIGNAL = signal.SIGKILL

#: Recorded bound for one recovery observation.  It is written into the
#: evidence so a reader can see the claim and its window together.  The shipped
#: code is also bounded (``_MAX_WORKER_REPLACEMENTS = 3``), so a run that hits
#: the bound without a verdict is itself a finding.
DEFAULT_RECOVERY_BOUND_SECONDS = 45.0

#: How long the kernel is given to make a killed process group observable.
_DEFAULT_REAP_BOUND_SECONDS = 10.0

#: The lifecycle gate a replacement must pass again, per PLAN §2.2 / M2.1
#: ("the replacement is re-authorised through the configure/start lifecycle").
#: ``configure`` is the load-granting step; ``start`` is recorded as well and
#: reported, but not required, because its payload binds to a live peer
#: agreement that this one-Mac capability does not stand up.
REQUIRED_REAUTHORISATION_COMMANDS: tuple[str, ...] = ("configure",)


class RealWorkerKillError(RuntimeError):
    """The capability refused to run: the target was not a real worker process."""


def quiet_host_guard(*, ceiling: float = 8.0) -> dict[str, Any]:
    """Report the host load against PLAN §2.1's quiet-host ceiling.

    Reported, never enforced: this capability is also what proves a RED, and a
    skip would hide the finding.  The value travels in the evidence record so a
    reader can weigh the run.
    """

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:  # pragma: no cover - platform without load average
        load1 = load5 = load15 = float("nan")
    return {
        "ceiling": ceiling,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "quiet": bool(load1 <= ceiling),
    }


def process_group_state(pid: Any, *, signum: int = 0) -> str:
    """Probe a process group without letting the probe decide the outcome.

    ``permission_denied`` is a real, measured state of a **killed** group on
    Darwin (signal 0 to a group whose only member is an unreaped zombie is
    EPERM, not ESRCH) and it is reported as its own value.  Collapsing it into
    "absent" would silently overstate what the harness knows, and letting it
    raise would hide the shipped ``process_group_present`` seam this scenario
    exists to expose.
    """

    if not isinstance(pid, int) or pid <= 0:
        return "no_pid"
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "error"
    return "present"


def real_worker_facts(worker: Any) -> dict[str, Any]:
    """Facts that distinguish a real managed worker from a simulated mailbox.

    ``pgid_is_leader`` is the discriminator: ``ManagedWorker.start`` refuses a
    native child that is not its own process-group leader, and a soak mailbox
    re-subscribe has no pid at all.
    """

    pid = getattr(worker, "pid", None)
    session = getattr(worker, "_session", None)
    status: Mapping[str, Any]
    try:
        status = worker.status()
    except Exception as error:  # a dead worker must not break the harness
        status = {"status_error": type(error).__name__}
    pgid = None
    if isinstance(pid, int) and pid > 0:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = None
    # The shipped callable is recorded separately from the harness's own probe:
    # what it does (including raising) under a real kill is observation data.
    try:
        product_present = bool(worker.process_group_present())
        product_error = None
    except Exception as error:
        product_present = None
        product_error = type(error).__name__
    return {
        "pid": pid,
        "process_group_id": pgid,
        "pgid_is_leader": bool(pid and pgid == pid),
        "process_group_state": process_group_state(pid),
        "product_process_group_present": product_present,
        "product_process_group_present_error": product_error,
        "session_pid": getattr(session, "pid", None),
        "session_returncode": getattr(session, "returncode", None),
        "status_running": bool(status.get("running")),
        "status_error_code": status.get("error_code"),
        "configure_dispatched": bool(getattr(worker, "_configure_dispatched", False)),
        "model_load_authorized": bool(status.get("model_load_authorized")),
        "command_count": status.get("command_count"),
    }


def kill_real_worker(worker: Any, *, signum: int = KILL_SIGNAL,
                     reap_bound_seconds: float = _DEFAULT_REAP_BOUND_SECONDS) -> dict[str, Any]:
    """``SIGKILL`` the worker's real process group; never a simulated death.

    Refuses (``RealWorkerKillError``) when the target has no real pid or is not
    a process-group leader, so a harness that has silently fallen back to a
    mailbox double fails loudly instead of reporting a hollow kill.
    """

    before = real_worker_facts(worker)
    pid = before["pid"]
    if not isinstance(pid, int) or pid <= 0:
        raise RealWorkerKillError("worker_has_no_real_pid")
    if not before["pgid_is_leader"]:
        raise RealWorkerKillError("worker_pid_is_not_a_process_group_leader")
    if before["process_group_state"] != "present":
        raise RealWorkerKillError(
            "worker_process_group_not_present_before_kill:"
            f"{before['process_group_state']}")
    session = getattr(worker, "_session", None)
    started = time.monotonic()
    os.killpg(pid, signum)
    # The strong receipt is the reaped status, not a fabricated attribute: the
    # native session's own reader must observe the kernel's -SIGKILL.
    deadline = started + max(0.0, reap_bound_seconds)
    while time.monotonic() < deadline and getattr(session, "returncode", None) is None:
        time.sleep(0.05)
    reaped = getattr(session, "returncode", None) is not None
    after = real_worker_facts(worker)
    return {
        "signal": int(signum),
        "signal_name": signal.Signals(signum).name,
        "pid": pid,
        "process_group_id": before["process_group_id"],
        "killed_at_monotonic": started,
        "reap_waited_seconds": time.monotonic() - started,
        "death_observed_by_reap": reaped,
        "process_group_state_after": after["process_group_state"],
        "product_process_group_present_after": after["product_process_group_present"],
        "product_process_group_present_error_after": after["product_process_group_present_error"],
        "session_returncode_after": after["session_returncode"],
        # -9 only comes from the kernel: the strongest local receipt that this
        # was a real process death and not a fabricated returncode.
        "died_by_requested_signal": after["session_returncode"] == -int(signum),
        "before": before,
        "after": after,
    }


class AuthorisationLog:
    """The ``authorize_load`` callable the shipped lifecycle consults.

    Every envelope is stamped, so the capability can answer "was the
    replacement re-authorised through the configure/start lifecycle *after* the
    kill?" from data.  ``permit`` decides the grant; the log records either way.
    """

    def __init__(self, *, permit: bool = True) -> None:
        self.permit = permit
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, envelope: Mapping[str, Any]) -> bool:
        entry = {
            "at_monotonic": time.monotonic(),
            "command": envelope.get("command") if isinstance(envelope, Mapping) else None,
            "command_id": envelope.get("command_id") if isinstance(envelope, Mapping) else None,
            "run_id": envelope.get("run_id") if isinstance(envelope, Mapping) else None,
            "granted": bool(self.permit),
        }
        with self._lock:
            self._entries.append(entry)
        return bool(self.permit)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def commands(self) -> list[str]:
        return [entry["command"] for entry in self.entries()]

    def since(self, mark: float) -> list[dict[str, Any]]:
        """Grants the shipped lifecycle consulted at or after ``mark``."""

        return [entry for entry in self.entries() if entry["at_monotonic"] >= mark]


def _replacement_record(runtime: Any) -> dict[str, Any] | None:
    """Read the runtime's own replacement ledger, if it wrote one."""

    root = getattr(runtime, "root", None)
    if root is None:
        return None
    path = Path(root) / "worker-replacement.private.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def observe_recovery(runtime: Any, *, previous_worker: Any, killed_at: float,
                     log: AuthorisationLog,
                     bound_seconds: float = DEFAULT_RECOVERY_BOUND_SECONDS,
                     poll_seconds: float = 0.1) -> dict[str, Any]:
    """Watch the shipped runtime after a real kill, for at most ``bound_seconds``.

    Pure observer: no command is dispatched from here, because re-authorising
    the replacement is the shipped behaviour under test, not the harness's job.
    Stops early when the route recovers, when the runtime publishes a terminal
    (blocked/closed) state, or when the bound expires.
    """

    deadline = killed_at + max(0.0, bound_seconds)
    timeline: list[dict[str, Any]] = []
    replacement: Any = None
    replacement_published_at: float | None = None
    route_recovered_at: float | None = None

    def sample(note: str) -> dict[str, Any]:
        # ``ManagedTargetRuntime.status`` is the shipped runtime's own record, and
        # it is the record that *owns* ``route_ready``.  The reserved name is
        # carried below only as a verbatim read of that record, the shape
        # ``mycelium_testbed/iroh_e2e.py`` uses for ``evidence.route_ready``: the
        # harness never recomputes the claim into a boolean of its own, and a
        # value the record did not publish stays absent (``None``) instead of
        # being manufactured into ``False``.  A harness that mints this claim -
        # as a literal or as a computed guess - is the defect
        # ``tests/testbed/test_harness_claim_boundary.py`` exists to catch.
        record = runtime.status()
        entry = {
            "note": note,
            "at_seconds": time.monotonic() - killed_at,
            "state": record.get("state"),
            "error_code": record.get("error_code"),
            "worker_is_previous": runtime.worker is previous_worker,
            "worker_running": bool(record.get("worker_running")),
            "route_ready": record.get("route_ready"),
        }
        timeline.append(entry)
        return entry

    sample("after_kill")
    while True:
        status = runtime.status()
        worker = runtime.worker
        if worker is not previous_worker and replacement is None:
            replacement = worker
            replacement_published_at = time.monotonic()
            sample("replacement_published")
        # "Recovered" = a replacement exists AND the runtime reports it running.
        if replacement is not None and status.get("worker_running"):
            route_recovered_at = time.monotonic()
            sample("replacement_running")
            break
        # Terminal: the shipped runtime has given up; further waiting is idle.
        if status.get("state") in {"blocked", "closed", "failed"}:
            sample("terminal")
            break
        if time.monotonic() >= deadline:
            sample("bound_expired")
            break
        time.sleep(poll_seconds)

    stopped_at = time.monotonic()
    post_kill_commands = [entry["command"] for entry in log.since(killed_at)
                          if entry["command"] is not None]
    replacement_facts = real_worker_facts(replacement) if replacement is not None else None
    status = runtime.status()
    return {
        "bound_seconds": bound_seconds,
        "observed_seconds": stopped_at - killed_at,
        "replacement_published": replacement is not None,
        "replacement_published_seconds": (
            None if replacement_published_at is None else replacement_published_at - killed_at
        ),
        "replacement_pid": None if replacement_facts is None else replacement_facts["pid"],
        "replacement_is_new_real_process": bool(
            replacement_facts is not None
            and replacement_facts["pgid_is_leader"]
            and replacement_facts["process_group_state"] == "present"
        ),
        "replacement_configure_dispatched": (
            None if replacement_facts is None else replacement_facts["configure_dispatched"]
        ),
        "replacement_model_load_authorized": (
            None if replacement_facts is None else replacement_facts["model_load_authorized"]
        ),
        "post_kill_authorisation_commands": post_kill_commands,
        "all_authorisation_commands": log.commands(),
        "reauthorised_after_kill": [
            command for command in REQUIRED_REAUTHORISATION_COMMANDS
            if command in post_kill_commands
        ],
        "reauthorisation_satisfied": all(
            command in post_kill_commands for command in REQUIRED_REAUTHORISATION_COMMANDS
        ),
        "route_recovered": route_recovered_at is not None,
        "route_recovered_seconds": (
            None if route_recovered_at is None else route_recovered_at - killed_at
        ),
        "route_state": status.get("state"),
        "route_error_code": status.get("error_code"),
        "bounded_named_failure": status.get("error_code"),
        "replacement_record": _replacement_record(runtime),
        "timeline": timeline,
    }


def recovery_gap(outcome: Mapping[str, Any]) -> list[str]:
    """The named requirements this observation did not meet, in PLAN's order."""

    gaps: list[str] = []
    if not outcome.get("replacement_published"):
        gaps.append(
            "replacement_not_published_within_bound: the shipped runtime never "
            "replaced the killed worker inside {}s (route_state={!r}, "
            "route_error_code={!r})".format(
                outcome.get("bound_seconds"), outcome.get("route_state"),
                outcome.get("route_error_code"))
        )
    elif not outcome.get("replacement_is_new_real_process"):
        gaps.append(
            "replacement_is_not_a_new_real_process: runtime.worker changed but is "
            "not a live process-group leader (pid={!r})".format(outcome.get("replacement_pid"))
        )
    missing = [command for command in REQUIRED_REAUTHORISATION_COMMANDS
               if command not in (outcome.get("post_kill_authorisation_commands") or [])]
    if missing:
        gaps.append(
            "replacement_not_reauthorised: the shipped lifecycle consulted the load "
            "authorizer for {} after the kill; required {}, observed {}".format(
                outcome.get("post_kill_authorisation_commands") or [], missing,
                outcome.get("all_authorisation_commands") or [])
        )
    if not outcome.get("route_recovered"):
        gaps.append(
            "route_did_not_recover_within_bound: no replacement was observed running "
            "before {}s expired".format(outcome.get("bound_seconds"))
        )
    return gaps


def write_evidence(path: Path | str, *, scenario: str, run_id: str, before: Mapping[str, Any],
                   kill: Mapping[str, Any], outcome: Mapping[str, Any],
                   load: Mapping[str, Any],
                   boundary: Mapping[str, Any] | None = None) -> Path:
    """Write the one JSON record of the scenario and return its path."""

    document = {
        "protocol": EVIDENCE_PROTOCOL,
        "scenario": scenario,
        "run_id": run_id,
        "level": "L3",
        "shipped_path": [
            "mycelium_a13_macos.target_app.TargetApplication.start_work",
            "mycelium_a13_macos.managed_runtime.ManagedTargetRuntime",
            "mycelium_a13_macos.worker.ManagedWorker (real native process, own process group)",
            "mycelium_a13_macos.native_runtime.installed_sidecar (real iroh sidecar binary)",
        ],
        "simulated": [
            "no model weights are loaded (configure is dispatched and authorised, the "
            "native load is not required by this capability)",
            "no two-process iroh inference and no fault proxy",
        ],
        "kill_is_real": bool(kill.get("died_by_requested_signal")),
        "quiet_host": dict(load),
        "worker_before_kill": dict(before),
        "configured_boundary": dict(boundary) if boundary is not None else None,
        "kill": dict(kill),
        "recovery": dict(outcome),
        "gap": recovery_gap(outcome),
        "verdict": "GREEN" if not recovery_gap(outcome) else "RED",
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
