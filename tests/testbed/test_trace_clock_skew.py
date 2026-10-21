# SPDX-License-Identifier: AGPL-3.0-only
"""Two-process trace merge under a deliberately skewed clock.

Two child processes emulate two hosts on one machine: the responder runs with a
monotonic clock 2.5 s behind and a wall clock 45 s behind the initiator.  The
children write *only* through the testbed trace surface, so the offset has to be
recovered from the trace itself.

The test asserts, in order:

1. raw monotonic order is wrong - the responder's ``delivered`` looks like it
   happened before the initiator's ``enqueue``;
2. the four-timestamp estimator recovers the offset it never observed, within the
   uncertainty it reports, and that uncertainty is small next to the skew;
3. the corrected timeline matches the causal order, including both cross-host
   legs (a four-timestamp estimate is causally safe: it can only place the
   receiver's stamp after the sender's);
4. a host with no estimated offset is refused, never silently aligned.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from mycelium_testbed.trace import (
    ClockOffsetUnavailable,
    EventKind,
    TraceRunDirectory,
    correct_events,
    estimate_clock_offset,
    load_run,
    merge_timeline,
    pair_round_trip_samples,
    summarise,
)

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "_skew_child.py"

REQUEST_ID = "request-skew-001"
COMMAND_ID = "command-skew-001"
INITIATOR_HOST = "node-a"
RESPONDER_HOST = "node-c"
RESPONDER_MONOTONIC_OFFSET_NS = -2_500_000_000
RESPONDER_WALL_OFFSET_NS = -45_000_000_000


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _spawn(role: str, *, host_id: str, offsets: tuple[int, int], run_dir: Path):
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(CHILD),
            "--role",
            role,
            "--run-dir",
            str(run_dir),
            "--host-id",
            host_id,
            "--request-id",
            REQUEST_ID,
            "--command-id",
            COMMAND_ID,
            "--monotonic-offset-ns",
            str(offsets[0]),
            "--wall-offset-ns",
            str(offsets[1]),
        ],
        cwd=str(ROOT),
        env=_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _relay_one_exchange(run_dir: Path) -> tuple[int, int]:
    """Run the two children through one ping/reply exchange."""

    responder = _spawn(
        "responder",
        host_id=RESPONDER_HOST,
        offsets=(RESPONDER_MONOTONIC_OFFSET_NS, RESPONDER_WALL_OFFSET_NS),
        run_dir=run_dir,
    )
    assert responder.stdin is not None and responder.stdout is not None
    try:
        assert responder.stdout.readline().strip() == "ready"
        initiator = _spawn(
            "initiator",
            host_id=INITIATOR_HOST,
            offsets=(0, 0),
            run_dir=run_dir,
        )
        assert initiator.stdin is not None and initiator.stdout is not None
        try:
            ping_line = initiator.stdout.readline()
            assert ping_line.startswith("ping "), ping_line
            responder.stdin.write(ping_line)
            responder.stdin.flush()
            reply_line = responder.stdout.readline()
            assert reply_line.startswith("reply "), reply_line
            initiator.stdin.write(reply_line)
            initiator.stdin.flush()
            assert initiator.stdout.readline().strip() == "done"
        finally:
            initiator.stdin.close()
            initiator.wait(timeout=30)
    finally:
        responder.stdin.close()
        responder.wait(timeout=30)
    responder_stderr = responder.stderr.read() if responder.stderr else ""
    initiator_stderr = ""
    assert responder.returncode == 0, responder_stderr
    return responder.returncode, 0


@pytest.fixture(scope="module")
def skewed_run(tmp_path_factory: pytest.TempPathFactory):
    run_dir = tmp_path_factory.mktemp("skewed-clock") / "run"
    TraceRunDirectory(
        run_dir,
        run_id="skewed-clock",
        host_id=INITIATOR_HOST,
        reference_host=INITIATOR_HOST,
        notes="two-process skewed-clock merge",
    ).close()
    _relay_one_exchange(run_dir)
    return load_run(run_dir)


def _event(events, host_id: str, kind: EventKind):
    matches = [
        event for event in events if event.host_id == host_id and event.kind is kind
    ]
    assert len(matches) == 1, (host_id, kind, matches)
    return matches[0]


def test_raw_order_is_inverted_by_the_skew(skewed_run) -> None:
    enqueue = _event(skewed_run.events, INITIATOR_HOST, EventKind.ENQUEUE)
    delivered = _event(skewed_run.events, RESPONDER_HOST, EventKind.DELIVERED)

    assert delivered.monotonic_ns < enqueue.monotonic_ns
    assert delivered.wall_ns < enqueue.wall_ns


def test_correction_recovers_the_offset_within_reported_uncertainty(skewed_run) -> None:
    samples = pair_round_trip_samples(
        skewed_run.events, a_host=INITIATOR_HOST, b_host=RESPONDER_HOST
    )

    assert len(samples) >= 1
    offset = estimate_clock_offset(samples, reference_host=INITIATOR_HOST)

    assert offset.method == "four_timestamp_ntp"
    assert offset.sample_count >= 1
    assert offset.uncertainty_ns > 0
    assert abs(offset.monotonic_offset_ns - RESPONDER_MONOTONIC_OFFSET_NS) <= (
        offset.uncertainty_ns
    )
    assert abs(offset.wall_offset_ns - RESPONDER_WALL_OFFSET_NS) <= offset.uncertainty_ns
    # the estimate is useful, not merely consistent: uncertainty << skew
    assert offset.uncertainty_ns < abs(RESPONDER_MONOTONIC_OFFSET_NS) // 100


def test_corrected_timeline_matches_causal_order(skewed_run) -> None:
    entries = merge_timeline(skewed_run, reference_host=INITIATOR_HOST)
    order = [(entry.host_id, entry.event.kind) for entry in entries]

    assert order == [
        (INITIATOR_HOST, EventKind.ENQUEUE),
        (RESPONDER_HOST, EventKind.DELIVERED),
        (RESPONDER_HOST, EventKind.STARTED),
        (RESPONDER_HOST, EventKind.FINISHED),
        (RESPONDER_HOST, EventKind.ENQUEUE),
        (INITIATOR_HOST, EventKind.DELIVERED),
    ]
    # the two cross-host legs are the ones the skew inverted; they must be strict
    cross_host = [
        (entries[0], entries[1]),
        (entries[4], entries[5]),
    ]
    for earlier, later in cross_host:
        assert earlier.monotonic_ns < later.monotonic_ns
    # ... and they carry the estimated uncertainty rather than a silent guess
    assert entries[1].uncertainty_ns > 0
    assert entries[1].reference is False


def test_corrected_wall_clock_agrees_with_corrected_monotonic(skewed_run) -> None:
    entries = merge_timeline(skewed_run, reference_host=INITIATOR_HOST)
    enqueue = entries[0]
    delivered = entries[1]

    assert enqueue.wall_ns < delivered.wall_ns


def test_unestimated_host_is_refused_not_silently_aligned(skewed_run) -> None:
    with pytest.raises(ClockOffsetUnavailable):
        correct_events(skewed_run.events, {}, reference_host=INITIATOR_HOST)


def test_summary_reports_the_clock_estimate_and_a_clean_timeline(skewed_run) -> None:
    text = summarise(skewed_run.run_dir, reference_host=INITIATOR_HOST)

    assert "clock correction: reference node-a" in text
    assert "four_timestamp_ntp" in text
    assert "+/-" in text
    assert "first failure: none" in text
    assert REQUEST_ID in text
    assert RESPONDER_HOST in text
