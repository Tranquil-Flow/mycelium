# SPDX-License-Identifier: AGPL-3.0-only
"""The summary and timeline renderer must make the first failure diagnosable."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from mycelium_testbed.trace import (
    EventKind,
    HostClock,
    TraceRecorder,
    TraceRunDirectory,
    load_run,
    render_per_host,
    render_timeline,
    summarise,
)

REQUEST_ID = "request-summary-001"
COMMAND_ID = "command-summary-001"
SUBMITTING_HOST = "node-a"
EXECUTING_HOST = "node-c"


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _failing_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(
        run_dir, run_id="summary", host_id=SUBMITTING_HOST, reference_host=SUBMITTING_HOST
    ) as run:
        origin = TraceRecorder(SUBMITTING_HOST, run=run)
        peer = TraceRecorder(
            EXECUTING_HOST,
            run=run,
            clock=HostClock(monotonic_offset_ns=-3_000_000_000, wall_offset_ns=4_000_000_000),
        )
        origin.event(
            EventKind.ENQUEUE,
            request_id=REQUEST_ID,
            detail={"frame_digest": _digest(b"hop-1"), "message": "HopHeader"},
        )
        peer.event(
            EventKind.DELIVERED,
            request_id=REQUEST_ID,
            detail={"frame_digest": _digest(b"hop-1"), "message": "HopHeader"},
        )
        peer.event(
            EventKind.STARTED,
            request_id=REQUEST_ID,
            command_id=COMMAND_ID,
            placement_id="node-c-stage-002",
            detail={"phase": "DECODE"},
        )
        peer.event(
            EventKind.FINISHED,
            request_id=REQUEST_ID,
            command_id=COMMAND_ID,
            placement_id="node-c-stage-002",
            status="failed:runtime_failure",
            detail={"phase": "DECODE", "reason": "kv_miss"},
        )
        # the reply closes the exchange, so the clock offset is estimable
        peer.event(
            EventKind.ENQUEUE,
            request_id=REQUEST_ID,
            detail={"frame_digest": _digest(b"reply-1"), "message": "ProgressivePrefillMessage"},
        )
        origin.event(
            EventKind.DELIVERED,
            request_id=REQUEST_ID,
            detail={"frame_digest": _digest(b"reply-1"), "message": "ProgressivePrefillMessage"},
        )
        origin.event(
            EventKind.TERMINAL,
            request_id=REQUEST_ID,
            status="failure_reported",
            detail={"reason": "runtime_failure", "placement_id": "node-c-stage-002"},
        )
    return run_dir


def test_summary_isolates_the_first_failure(tmp_path: Path) -> None:
    text = summarise(_failing_run(tmp_path))

    assert "FIRST FAILURE" in text
    assert f"request={REQUEST_ID}" in text
    assert f"command={COMMAND_ID}" in text
    assert f"host={EXECUTING_HOST}" in text
    assert "failed:runtime_failure" in text
    assert "kv_miss" in text
    assert "last ok transition: started" in text
    assert "request outcome: failure_reported" in text
    assert "clock correction: reference node-a" in text
    assert "+/-" in text
    assert "package_digest=sha256:" in text
    assert re.search(r"git=([0-9a-f]{40}|unknown)", text)


def test_summary_hides_nothing_when_every_transition_is_ok(tmp_path: Path) -> None:
    run_dir = tmp_path / "clean"
    with TraceRunDirectory(run_dir, run_id="clean", host_id="node-a") as run:
        recorder = TraceRecorder("node-a", run=run)
        recorder.event(EventKind.ENQUEUE, request_id=REQUEST_ID)
        recorder.event(EventKind.TERMINAL, request_id=REQUEST_ID, status="completed")

    text = summarise(run_dir)

    assert "first failure: none (every transition is ok)" in text
    assert "request outcome: completed" in text
    assert "t+" in text


def test_compact_clean_summary_reports_each_request_including_unfinished(tmp_path: Path) -> None:
    run_dir = tmp_path / "clean-multiple"
    unfinished = "request-summary-002"
    with TraceRunDirectory(run_dir, run_id="clean-multiple", host_id="node-a") as run:
        recorder = TraceRecorder("node-a", run=run)
        recorder.event(EventKind.ENQUEUE, request_id=REQUEST_ID)
        recorder.event(EventKind.ENQUEUE, request_id=unfinished)
        recorder.event(EventKind.TERMINAL, request_id=REQUEST_ID, status="completed")

    text = summarise(run_dir, include_timeline=False)

    assert "first failure: none (every transition is ok)" in text
    assert f"request outcome: completed (request={REQUEST_ID}, terminal at t+" in text
    assert f"request outcome: no terminal transition recorded (request={unfinished})" in text
    assert text.count("request outcome:") == 2
    assert "after this failure" not in text
    assert "before this failure" not in text


def test_timeline_prints_each_request_in_order_with_deltas(tmp_path: Path) -> None:
    run_dir = _failing_run(tmp_path)
    run = load_run(run_dir)

    text = render_timeline(run, reference_host=SUBMITTING_HOST)
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0].startswith(f"request {REQUEST_ID}: terminal=failure_reported")
    deltas = [line for line in lines[1:] if "t+" in line]
    assert len(deltas) == len(run.events_for_request(REQUEST_ID))
    for line in deltas:
        assert " node-a[" in line or " ~node-c[" in line
        assert "(delta+" in line
    # the timeline is ordered: offsets from the first event are non-decreasing
    offsets = [float(line.split("t+")[1].split("ms")[0]) for line in deltas]
    assert offsets == sorted(offsets)
    # and the per-step deltas are non-negative, never a negative time
    steps = [float(line.split("(delta+")[1].split("ms")[0]) for line in deltas]
    assert all(step >= 0 for step in steps)
    assert steps[0] == 0.0


def test_hosts_are_not_interleaved_without_an_estimate(tmp_path: Path) -> None:
    run_dir = tmp_path / "unpaired"
    with TraceRunDirectory(run_dir, run_id="unpaired", host_id="node-a") as run:
        origin = TraceRecorder("node-a", run=run)
        peer = TraceRecorder("node-c", run=run, clock=HostClock(monotonic_offset_ns=-1_000))
        # no send/receive pair: nothing to estimate an offset from
        origin.event(
            EventKind.ENQUEUE, request_id=REQUEST_ID, detail={"frame_digest": "sha256:x"}
        )
        peer.event(
            EventKind.DELIVERED, request_id=REQUEST_ID, detail={"frame_digest": "sha256:y"}
        )

    text = summarise(run_dir)
    timeline = render_timeline(load_run(run_dir), reference_host="node-a")

    assert "hosts are NOT interleaved" in text
    assert "timeline unavailable" in timeline
    assert "host node-a:" in timeline
    assert "host node-c:" in timeline
    assert render_per_host(load_run(run_dir)).count("host ") == 2
