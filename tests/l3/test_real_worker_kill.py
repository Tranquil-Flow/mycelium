# SPDX-License-Identifier: AGPL-3.0-only
"""L3: kill the REAL a13 managed worker process and bound the recovery (PLAN §2.2, R1).

The gap this scenario closes (PLAN v2 §1 R1, M2 step 1): the local harness never
started the shipped ``a13_macos`` managed worker, so a physical failure inside
``managed_runtime.py:_replace_worker_if_dead`` could not be reproduced on one Mac
and hardware became the only debugger (34 preview builds in 6 days).

Nothing here simulates the death.  ``kill_real_worker`` refuses a target with no
real pid, and the tests additionally assert the kernel's ``-SIGKILL`` returncode,
so a mailbox re-subscribe can never be reported as a kill.

This is harness fidelity, not the M2 fix: no replacement is implemented and
``_replace_worker_if_dead`` is not repaired.  The shipped path is observed, and
these scenarios are expected to be RED on the candidate until the fix lands.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from test_control_pump import _world
from test_managed_control import DEPLOYMENT_ID, RUN_ID
from test_managed_staging import bundle
from test_worker import POLICY, SAFE

from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_testbed.real_worker_kill import (
    DEFAULT_RECOVERY_BOUND_SECONDS,
    AuthorisationLog,
    kill_real_worker,
    observe_recovery,
    quiet_host_guard,
    real_worker_facts,
    recovery_gap,
    write_evidence,
)

pytestmark = pytest.mark.emulation

#: The shipped lifecycle's own configure payload shape (see
#: ``worker._OPERATION_PAYLOAD_FIELDS['configure']``).  The native load itself
#: is not required by this capability: dispatching configure is what closes the
#: pre-load window (``ManagedWorker._configure_dispatched``), which is the
#: boundary the recovery semantics under test turn on.
CONFIGURE_PAYLOAD = {
    "assignment_file": "assignment.json",
    "artifact_report_file": "report.json",
    "graph": {},
    "device_states": {},
    "load_generation": 1,
}


def _configured_boundary(worker) -> dict:
    """How far the shipped candidate can express "configure/start was dispatched".

    The candidate under test guards replacement on ``ManagedWorker``'s
    configure-dispatch flag and its ``requires_cold_restart`` refusal.  An older
    candidate may not carry those symbols at all, and that is itself the
    boundary being measured - so the harness records which symbols exist
    instead of assuming today's shape exists on every revision.
    """

    requires = getattr(worker, "requires_cold_restart", None)
    return {
        "configure_dispatched_symbol": hasattr(worker, "_configure_dispatched"),
        "configure_dispatched": bool(getattr(worker, "_configure_dispatched", False)),
        "requires_cold_restart_symbol": callable(requires),
        "requires_cold_restart": bool(requires()) if callable(requires) else None,
    }


@contextlib.contextmanager
def shipped_path(tmp_path: Path, *, log: AuthorisationLog):
    """The real one-Mac shipped path: signed world -> offer -> real managed runtime.

    Reuses ``tests/a13_macos``'s existing integration (``_world``, the staged
    bundle, ``TargetApplication.start_work``) exactly as
    ``test_minimal_managed_flow`` drives it; nothing is re-implemented here.
    """

    archive, _ = bundle()
    with _world(tmp_path / "world") as world:
        mailbox = world["authority"]._control_mailbox
        ca = world["authority"].seed_server.material.ca_certificate
        app = TargetApplication(world["target"].state_root,
                               _test_peer_factory=lambda root: world["target"])
        runtime = None
        try:
            offer = mailbox.publish_offer(target=world["member"], run_id=RUN_ID,
                                          deployment_id=DEPLOYMENT_ID, archive=archive)
            status = app.start_work(
                offer,
                acknowledged=True,
                resource_policy=POLICY,
                limits=WorkerLimits(max_rss_bytes=512 * 1024 * 1024, max_disk_bytes=100_000,
                                    max_wall_seconds=180, max_commands=20),
                authorize_load=log,
                _test_observer=lambda: dict(SAFE),
                _test_ca_certificate=ca,
            )
            runtime = app._runtime
            assert status["state"] == "running", status
            assert runtime is not None and runtime.worker is not None
            yield app, runtime, offer, mailbox, world
        finally:
            with contextlib.suppress(Exception):
                app.stop_control()


def _record(tmp_path: Path, scenario: str, *, before, kill, outcome, load, boundary=None) -> Path:
    path = write_evidence(tmp_path / "l3-evidence" / f"{scenario}.json", scenario=scenario,
                          run_id=RUN_ID, before=before, kill=kill, outcome=outcome, load=load,
                          boundary=boundary)
    print(f"L3_EVIDENCE {scenario} {path}")
    print(json.dumps({"scenario": scenario, "kill_is_real": kill.get("died_by_requested_signal"),
                      "recovery": {key: value for key, value in outcome.items()
                                   if key != "timeline"}},
                     indent=2, sort_keys=True, default=str))
    return path


def _assert_capability_is_real(before, kill) -> None:
    """Pre-conditions: a real worker was started and a real SIGKILL landed.

    If the capability had silently degraded to a mailbox double these fail
    first, so a RED below can never be an artefact of a hollow harness.
    """

    assert before["pgid_is_leader"], f"not a real process group leader: {before}"
    assert before["status_running"] is True, before
    assert kill["death_observed_by_reap"] is True, kill
    assert kill["died_by_requested_signal"] is True, (
        "the worker's native session did not report the kernel's -SIGKILL; "
        f"this was not a real process death: {kill}")


def _assert_recovery_semantics(scenario: str, outcome, evidence_path: Path) -> None:
    gaps = recovery_gap(outcome)
    assert not gaps, (
        f"L3 {scenario}: the shipped path did not recover the killed real worker "
        f"through the lifecycle required by PLAN §2.2 / M2.1.\n"
        f"unmet requirements: {json.dumps(gaps, indent=2)}\n"
        f"observed: {json.dumps({k: v for k, v in outcome.items() if k != 'timeline'}, indent=2, default=str)}\n"
        f"evidence: {evidence_path}"
    )


def test_real_worker_kill_after_configure_requires_reauthorised_replacement(tmp_path):
    """S4/S5-shaped on the configured boundary: kill after configure, then bound.

    The worker has passed the shipped configure gate, so the runtime knows model
    load state may exist.  A real SIGKILL must still produce, inside the recorded
    bound, a replacement that is re-authorised through the configure/start
    lifecycle.  On the current candidate the shipped runtime instead publishes a
    terminal ``runtime_cold_restart_required`` refusal and no replacement at all.
    """

    scenario = "S4S5-configured-real-kill"
    log = AuthorisationLog(permit=True)
    with shipped_path(tmp_path, log=log) as (app, runtime, offer, mailbox, world):
        before = real_worker_facts(runtime.worker)
        assert before["pid"] is not None and before["pid"] > 0, before

        # The shipped configure step: consult the run's load authorizer, then
        # dispatch to the real native process.  Whether the native load itself
        # succeeds is not this scenario's subject; the dispatch is.
        runtime.worker.send(command_id="l3-configure", command="configure",
                            payload=CONFIGURE_PAYLOAD)
        boundary = _configured_boundary(runtime.worker)
        assert boundary["configure_dispatched"], (
            "the shipped configure did not close the pre-load window; the "
            f"configured boundary under test was never reached: {boundary}")
        assert log.commands() == ["configure"], log.commands()

        kill = kill_real_worker(runtime.worker)
        outcome = observe_recovery(runtime, previous_worker=runtime.worker,
                                   killed_at=kill["killed_at_monotonic"], log=log,
                                   bound_seconds=DEFAULT_RECOVERY_BOUND_SECONDS)
        evidence = _record(tmp_path, scenario, before=before, kill=kill, outcome=outcome,
                           load=quiet_host_guard(), boundary=boundary)

        _assert_capability_is_real(before, kill)
        _assert_recovery_semantics(scenario, outcome, evidence)

        # Unreached while RED: the route must serve again with no owner replay.
        assert app.status()["native_control"]["state"] == "running", app.status()


def test_real_worker_kill_preload_replacement_is_not_reauthorised(tmp_path):
    """The same kill before configure: isolates *which* half of the gap is missing.

    No configure has been dispatched, so the shipped runtime does replace the
    worker (a real new process) - but it does not re-authorise the replacement
    through the lifecycle, which is exactly what PLAN §2.2 / M2.1 requires before
    a route can be claimed again.  This is the weaker boundary, and it fails on
    the re-authorisation requirement rather than on the absence of a replacement.
    """

    scenario = "S4S5-preload-real-kill"
    log = AuthorisationLog(permit=True)
    with shipped_path(tmp_path, log=log) as (app, runtime, offer, mailbox, world):
        before = real_worker_facts(runtime.worker)
        boundary = _configured_boundary(runtime.worker)
        assert boundary["configure_dispatched"] is False, boundary

        kill = kill_real_worker(runtime.worker)
        outcome = observe_recovery(runtime, previous_worker=runtime.worker,
                                   killed_at=kill["killed_at_monotonic"], log=log,
                                   bound_seconds=DEFAULT_RECOVERY_BOUND_SECONDS)
        evidence = _record(tmp_path, scenario, before=before, kill=kill, outcome=outcome,
                           load=quiet_host_guard(), boundary=boundary)

        _assert_capability_is_real(before, kill)
        # The runtime really does mint a second real process on this boundary...
        assert outcome["replacement_published"] is True, outcome
        assert outcome["replacement_is_new_real_process"] is True, outcome
        assert outcome["replacement_pid"] != before["pid"], outcome
        # ...and re-authorising it is the part that is missing.
        _assert_recovery_semantics(scenario, outcome, evidence)
