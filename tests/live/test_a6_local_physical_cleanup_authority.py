# SPDX-License-Identifier: AGPL-3.0-only
"""A6 case 15 (cleanup_authority_failure) on real local node processes.

Ordinary gateway -> LiveRouterPort -> ColdRouteRestartOwner -> real
``PhysicalLiveRoute`` over real local ``physical_inference_node.py``
processes, two complete-model replicas. The fault is the product seam
itself: it arms the router's completion-cleanup hold, kills the source,
SIGSTOPs the successor's node at the held cleanup authorization and then
releases the hold.

The owner ruling (2026-10-06) requires the refused successor to end through
the pre-cutover abort: no cutover, the request TERMINAL_BLOCKED and the
coordinator ``failed``. A coordinator ``cancelled`` terminal on a request
nobody cancelled is a mislabel.

Two successor endings are exercised. ``token_bound``: generation reaches
``max_new_tokens``; the route authorizes cleanup and then issues its
completion teardown. ``stop_token``: generation ends on a model terminator;
the route takes its stop branch (the run-day model ends this way). The tiny
local model only ever emits token 0, so the terminator is installed after the
source started: a route captures its stop set when a command starts, so only
the successor stops on it.

Local multi-process session only; never physical or browser qualification.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time

import pytest

from mycelium_a6_case_observations import CaseObservationRecorder, observation_authority
from mycelium_a6_product_fault_seams import ProductCaseSeam
from tests.a6_acceptance.test_case_observations import ROOT, plan  # noqa: F401 - fixture
from tests.live.test_a6_local_physical_recovery import (
    _TERMINALS,
    SIDECAR_BINARY,
    _compose,
    _drain,
    _submit,
    watchdog,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

CASE = "cleanup_authority_failure"
_SSH_SIGNAL_LATENCY_SECONDS = 1.0


def _router_events(caplog, event):
    found = []
    for record in caplog.records:
        if record.name != "mycelium_live.router_port":
            continue
        try:
            document = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("event") == event:
            found.append(document)
    return found


def _run_case(service, owner, target, before_fault):
    request_id = _submit(service, owner)
    events: list = []
    first_token = threading.Event()
    reader = threading.Thread(
        target=_drain, args=(service, request_id, events, first_token), daemon=True)
    reader.start()
    assert first_token.wait(60), "request never produced a token"
    before_fault()
    seam = ProductCaseSeam(CASE, target)
    outcome: dict = {}
    try:
        seam.arm(case_id=CASE)
        outcome["post_state"] = dict(seam.apply(case_id=CASE))
    except Exception as error:  # noqa: BLE001 - measured, asserted by caller
        outcome["refusal"] = str(error)
    reader.join(timeout=180)
    outcome["kinds"] = [event.kind for event in events if event.kind in _TERMINALS]
    outcome["events"] = [(event.kind, event.code) for event in events if event.kind != "token"]
    return request_id, outcome


@pytest.mark.parametrize("ending", ["stop_token", "token_bound"])
def test_refused_successor_cleanup_ends_failed_not_cancelled(
        tmp_path, watchdog, caplog, plan, ending) -> None:  # noqa: F811
    caplog.set_level(logging.INFO, logger="mycelium_live.router_port")
    local, owner, router, service, target = _compose(tmp_path, "full_replica")
    try:
        assert set(local.reference_tokens) == {0}, local.reference_tokens

        def before_fault() -> None:
            if ending == "stop_token":
                owner.set_stop_token_ids([0])

        # A run-day stall reaches the successor's host over ssh, about a
        # second after the seam sees the held cleanup authorization. Give the
        # local stall the same latency so teardown already in flight lands
        # first, as it does on the fleet.
        signal_node = target.signal_node

        def remote_latency_signal(node_id, signum, route=None):
            if signum == signal.SIGSTOP:
                time.sleep(_SSH_SIGNAL_LATENCY_SECONDS)
            return signal_node(node_id, signum, route)

        target.signal_node = remote_latency_signal

        request_id, outcome = _run_case(service, owner, target, before_fault)
        assert "refusal" not in outcome, outcome
        post = outcome["post_state"]
        coordinator = router._coordinator
        assert coordinator is not None
        diagnostics = {
            "post_state": post,
            "events": outcome["events"],
            "coordinator_terminal": coordinator.terminal_state(request_id),
            "committed": coordinator.route_identity(request_id),
            "failed": _router_events(caplog, "a6_recovery_failed"),
            "cancelled": _router_events(caplog, "a6_recovery_cancelled"),
            "mismatch": [record.getMessage() for record in caplog.records
                         if "identity mismatch" in record.getMessage()],
        }
        assert post["successor_stalled"] is True, diagnostics
        assert post["cutover_committed"] is False, diagnostics
        assert coordinator.terminal_state(request_id) == "failed", diagnostics
        assert diagnostics["cancelled"] == [], diagnostics
        assert diagnostics["mismatch"] == [], diagnostics
        assert post["terminal_blocked_reason"] is not None, diagnostics
        # The source-owned v2 writer, once the resumed successor's late
        # cleanup proof released the request.
        recorder = CaseObservationRecorder(
            authority=observation_authority(operator_plan=plan, repo_root=ROOT),
            target=target, settle_seconds=60.0)
        (executed,) = target.executed
        released = recorder._settle_release(executed, None)
        assert released.coordinator_terminal == "failed", diagnostics
        assert released.reservation_count == 0, diagnostics
        if ending == "token_bound":
            # The tiny local model emits its stop token first, so only the
            # token-bound successor has a suffix to discard (clause 91).
            path = recorder.write_cleanup_authority_v2(
                executed, released, router.a6_command_authority(request_id))
            observed = json.loads(path.read_text())["observation"]
            assert observed["coordinator_terminal"] == "failed"
            assert observed["terminal_state"] == "TERMINAL_BLOCKED"
            assert observed["cutover_committed"] is False
    finally:
        service.close()
        owner.close()
        local.close()
