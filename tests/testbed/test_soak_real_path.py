# SPDX-License-Identifier: AGPL-3.0-only
"""The soak over the *real* control path (opt-in: needs the native iroh sidecar).

Set ``MYCELIUM_TESTBED_IROH_E2E=1`` to run it.  It is skipped by default so the
fast testbed suite stays dependency-free; this is the only soak test that builds
and launches the Rust sidecar.

It proves the two claims the packet makes about the real path:

* the seeded sweep is green and replays byte-identically (same seed -> same run
  digest, different seed -> different digest) while the canonical
  ``harness.run_complete_request`` episode and the real
  ``RequestGatewayService`` mailbox are the paths under test;
* the four invariants can genuinely *fail* on that path - each falsification
  fault is injected through the soak's own seams and the matching code fires.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mycelium_testbed import soak
from mycelium_testbed.trace import EventKind, load_run

pytestmark = pytest.mark.skipif(
    os.environ.get("MYCELIUM_TESTBED_IROH_E2E") != "1",
    reason="opt-in: set MYCELIUM_TESTBED_IROH_E2E=1 (builds the iroh sidecar)",
)


def _config(
    tmp_path: Path,
    binary: Path,
    *,
    seeds: tuple[int, ...],
    events: int,
    run_id: str,
    forced: tuple[soak.ForcedFault, ...] = (),
    real_request: bool = True,
    time_scale: float = 1.0,
) -> soak.SoakConfig:
    return soak.SoakConfig(
        seeds=seeds,
        events=events,
        run_id=run_id,
        out_root=tmp_path,
        native_binary=binary,
        forced=forced,
        real_request=real_request,
        mailbox=True,
        time_scale=time_scale,
    )


def test_real_seed_episode_is_green_and_replays(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(41,),
        events=30,
        run_id="real-replay",
    )

    first = soak.run_seed(config, 41, label="a")
    second = soak.run_seed(config, 41, label="b")
    other = soak.run_seed(config, 42, label="c")

    assert first.violations == (), [v.as_document() for v in first.violations]
    assert second.violations == ()
    assert first.run_digest == second.run_digest
    assert first.run_digest != other.run_digest

    # the canonical request episode really ran the product path
    real = first.detail["real_request"]
    assert real["accepted_status"] == 202
    assert real["terminal"] is not None
    assert first.detail["mailbox"]["schedule_consumed"] == 30

    # at most one native execution per command, across both episodes
    assert max(first.execution_counts.values()) == soak.MAXIMUM_NATIVE_EXECUTIONS_PER_COMMAND
    assert first.execution_counts

    # lifecycle traces per seed, loadable from one directory
    for trace_dir in first.trace_dirs:
        assert (Path(trace_dir) / "run.json").is_file(), trace_dir
    seed_dir = config.run_root() / "seeds" / "a"
    summary = (seed_dir / "summary.txt").read_text(encoding="utf-8")
    assert "mycelium.testbed.trace.v1" in summary
    record = json.loads((seed_dir / "seed.json").read_text(encoding="utf-8"))
    assert record["run_digest"] == first.run_digest
    assert record["observations"]["terminal_classes"]


def test_real_falsification_double_execution_fires_the_at_most_one_code(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(43,),
        events=8,
        run_id="real-double",
        forced=(soak.ForcedFault(soak.FaultKind.DOUBLE_EXECUTION, 3),),
        real_request=False,
    )

    result = soak.run_seed(config, 43)

    assert soak.INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND in (
        result.invariant_codes_fired
    ), result.as_document()


def test_real_falsification_stall_control_loop_fires_the_control_loop_code(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(44,),
        events=8,
        run_id="real-stall",
        # A stalled backend emits a named failure inside the terminal budget.
        # To falsify BOTH properties we must also lose that terminal on the client
        # side; a timely failure is a valid terminal, not a missing frame.
        forced=(
            soak.ForcedFault(soak.FaultKind.DROP_TERMINAL_FRAME, 0),
            soak.ForcedFault(soak.FaultKind.STALL_CONTROL_LOOP, 1),
        ),
        real_request=False,
        # The judged window is measured on the real clock, so the injected stall
        # must outlive the registry's own active-failure detection bound; it is
        # scaled down only to keep the test short.
        time_scale=0.5,
    )

    result = soak.run_seed(config, 44)

    assert soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID in (
        result.invariant_codes_fired
    ), result.as_document()
    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET in (
        result.invariant_codes_fired
    ), result.as_document()
    assert result.product_material["dropped_observation_kinds"] == ["terminal_frame_dropped"]


def test_real_stall_with_timely_named_failure_is_not_a_missing_terminal(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(44,),
        events=8,
        run_id="real-stall-terminal-control",
        forced=(soak.ForcedFault(soak.FaultKind.STALL_CONTROL_LOOP, 1),),
        real_request=False,
        time_scale=0.5,
    )

    result = soak.run_seed(config, 44)

    assert result.invariant_codes_fired == (
        soak.INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID,
    ), result.as_document()
    record = json.loads(
        (config.run_root() / "seeds" / "seed-0044" / "seed.json").read_text()
    )
    row, = record["ledger"]["rows"]
    assert row["terminal_kind"] == "failed"
    assert row["terminal_code"] == "backend_failed"
    assert row["stalled_at_s"] is not None
    assert result.product_material["dropped_observation_kinds"] == []
    events = load_run(result.trace_dirs[0]).events
    admitted, = [e for e in events if e.detail.get("origin") == "soak.gateway_submit"]
    terminal, = [e for e in events if e.kind is EventKind.TERMINAL]
    assert terminal.host_id == admitted.host_id
    assert terminal.detail["code"] == "backend_failed"
    assert 0 <= (terminal.monotonic_ns - admitted.monotonic_ns) / 1e9 < (
        config.resolved_bounds().request_terminal_cancelled_seconds
    )


def test_real_falsification_retire_nonterminal_row_fires_the_retirement_code(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(45,),
        events=8,
        run_id="real-retire",
        forced=(soak.ForcedFault(soak.FaultKind.RETIRE_NONTERMINAL_ROW, 1),),
        real_request=False,
    )

    result = soak.run_seed(config, 45)

    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND in (
        result.invariant_codes_fired
    ), result.as_document()


def test_real_falsification_drop_terminal_frame_fires_the_terminal_code(
    native_iroh_sidecar_binary: Path, tmp_path: Path
) -> None:
    config = _config(
        tmp_path,
        native_iroh_sidecar_binary,
        seeds=(46,),
        events=8,
        run_id="real-drop-terminal",
        forced=(soak.ForcedFault(soak.FaultKind.DROP_TERMINAL_FRAME, 1),),
        real_request=False,
    )

    result = soak.run_seed(config, 46)

    assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET in (
        result.invariant_codes_fired
    ), result.as_document()
