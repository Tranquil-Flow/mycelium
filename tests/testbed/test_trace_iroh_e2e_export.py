# SPDX-License-Identifier: AGPL-3.0-only
"""Trace export over the *real* iroh e2e path (opt-in: needs the native sidecar).

Set ``MYCELIUM_TESTBED_IROH_E2E=1`` to run it.  It is skipped by default so the
fast testbed suite stays dependency-free, and it is the only test here that
builds or launches the Rust sidecar.

It proves the reuse claim in the packet: the existing
``tests/e2e_request_iroh/harness.run_complete_request`` is traced end to end
(enqueue / delivered / started / finished / receipt / terminal) without a single
change to product code, and the run summary names a clock offset with its
uncertainty.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mycelium_testbed.iroh_e2e import run_traced_complete_request
from mycelium_testbed.trace import EventKind, load_run, summarise

pytestmark = pytest.mark.skipif(
    os.environ.get("MYCELIUM_TESTBED_IROH_E2E") != "1",
    reason="opt-in: set MYCELIUM_TESTBED_IROH_E2E=1 (builds the iroh sidecar)",
)


def test_traced_iroh_e2e_run_exports_a_full_lifecycle(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "traced-iroh"
    traced = run_traced_complete_request(
        native_iroh_sidecar_binary,
        run_dir,
        fsync=True,
    )

    kinds = {event.kind for event in traced.events}
    assert {
        EventKind.ENQUEUE,
        EventKind.DELIVERED,
        EventKind.STARTED,
        EventKind.FINISHED,
        EventKind.RECEIPT,
        EventKind.TERMINAL,
    } <= kinds

    # the product harness's own evidence is unchanged by tracing
    assert traced.evidence.accepted_status == 202
    assert traced.evidence.router_admissions == 1
    assert traced.evidence.local_evidence_only is True
    assert traced.evidence.route_ready is False

    # real hosts in the trace, and real commands with placements
    assert {event.host_id for event in traced.events} == {"node-a", "node-c"}
    commands = [event for event in traced.events if event.command_id]
    assert commands
    assert all(event.placement_id for event in commands)

    # every request file and command file is loadable
    loaded = load_run(run_dir)
    assert loaded.header.package_digest.startswith("sha256:")
    assert loaded.header.product_package_digest is not None
    assert loaded.header.git_head is not None
    assert loaded.header.hosts

    summary = summarise(run_dir)
    assert "clock correction: reference node-a" in summary
    assert "+/-" in summary
    assert "request outcome:" in summary
    assert (
        "first failure: none" in summary
        or "FIRST FAILURE" in summary
    ), summary.splitlines()[:12]
    assert (run_dir / "summary.txt").is_file()
