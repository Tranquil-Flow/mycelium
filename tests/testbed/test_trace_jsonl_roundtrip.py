# SPDX-License-Identifier: AGPL-3.0-only
"""JSONL round trip: one file per request, one per command, flushed and readable."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_testbed.trace import (
    COMMANDS_DIRNAME,
    REQUESTS_DIRNAME,
    RUN_HEADER_FILENAME,
    EventKind,
    HostClock,
    TraceDetailNotSerialisable,
    TraceEvent,
    TraceRecorder,
    TraceRunDirectory,
    load_run,
)

REQUEST_A = "request-round-trip-a"
REQUEST_B = "request-round-trip-b"
COMMAND_ONE = "command-round-trip-1"
COMMAND_TWO = "command-round-trip-2"


@pytest.fixture()
def written_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(
        run_dir,
        run_id="round-trip",
        host_id="node-a",
        reference_host="node-a",
        fsync=True,
    ) as run:
        origin = TraceRecorder("node-a", run=run, clock=HostClock())
        peer = TraceRecorder(
            "node-c",
            run=run,
            clock=HostClock(monotonic_offset_ns=1_000, wall_offset_ns=2_000),
        )
        origin.event(
            EventKind.ENQUEUE, request_id=REQUEST_A, detail={"frame_digest": "sha256:a"}
        )
        peer.event(
            EventKind.DELIVERED,
            request_id=REQUEST_A,
            detail={"frame_digest": "sha256:a"},
        )
        peer.event(
            EventKind.STARTED,
            request_id=REQUEST_A,
            command_id=COMMAND_ONE,
            placement_id="node-c-stage-002",
            detail={"phase": "DECODE", "token_index": 0},
        )
        peer.event(
            EventKind.FINISHED,
            request_id=REQUEST_A,
            command_id=COMMAND_ONE,
            placement_id="node-c-stage-002",
            status="ok",
            detail={"phase": "DECODE", "token_index": 0, "token_id": 7},
        )
        origin.event(
            EventKind.RECEIPT,
            request_id=REQUEST_A,
            detail={"message_id": "0f" * 16},
        )
        origin.event(
            EventKind.TERMINAL,
            request_id=REQUEST_A,
            command_id=COMMAND_TWO,
            status="completed",
        )
        origin.event(
            EventKind.ENQUEUE,
            request_id=REQUEST_B,
            detail={"frame_digest": "sha256:b", "nested": {"a": [1, 2, 3]}},
        )
        run.sync()
        # a detached reader sees every flushed line while the writers are still open
        provisional = (run_dir / REQUESTS_DIRNAME / _request_file(run_dir, REQUEST_A))
        assert provisional.read_text(encoding="utf-8").count("\n") == 6
    return run_dir


def _request_file(run_dir: Path, request_id: str) -> str:
    matches = [path for path in (run_dir / REQUESTS_DIRNAME).glob("*.jsonl")]
    for path in matches:
        if request_id in path.read_text(encoding="utf-8"):
            return path.name
    raise AssertionError(f"no request file for {request_id}: {matches}")


def test_layout_is_one_file_per_request_and_per_command(written_run: Path) -> None:
    request_files = sorted((written_run / REQUESTS_DIRNAME).glob("*.jsonl"))
    command_files = sorted((written_run / COMMANDS_DIRNAME).glob("*.jsonl"))

    assert (written_run / RUN_HEADER_FILENAME).is_file()
    assert len(request_files) == 2
    assert len(command_files) == 2
    assert all(path.name.endswith(".jsonl") for path in request_files)


def test_round_trip_preserves_every_field(written_run: Path) -> None:
    run = load_run(written_run)

    assert run.header.run_id == "round-trip"
    assert run.header.reference_host == "node-a"
    assert {record.host_id for record in run.header.hosts} == {"node-a", "node-c"}
    assert all(record.boot_id for record in run.header.hosts)

    assert [event.kind for event in run.events] == [
        EventKind.ENQUEUE,
        EventKind.RECEIPT,
        EventKind.TERMINAL,
        EventKind.ENQUEUE,
        EventKind.DELIVERED,
        EventKind.STARTED,
        EventKind.FINISHED,
    ]
    assert sorted(str(event.kind) for event in run.events) == sorted(
        [
            "enqueue",
            "enqueue",
            "delivered",
            "started",
            "finished",
            "receipt",
            "terminal",
        ]
    )
    for event in run.events:
        assert event.schema_version == run.header.schema_version
        assert event.pid > 0
        assert event.monotonic_ns > 0
        assert event.wall_ns > 0
        assert event.kind is EventKind.parse(str(event.kind))
    terminal = [event for event in run.events if event.kind is EventKind.TERMINAL][0]
    assert terminal.status == "completed"
    assert terminal.command_id == COMMAND_TWO
    assert terminal.request_id == REQUEST_A
    started = [event for event in run.events if event.kind is EventKind.STARTED][0]
    assert started.placement_id == "node-c-stage-002"
    assert started.detail["phase"] == "DECODE"
    nested = [event for event in run.events if event.request_id == REQUEST_B][0]
    assert nested.detail == {"frame_digest": "sha256:b", "nested": {"a": [1, 2, 3]}}


def test_request_and_command_files_carry_the_same_events(written_run: Path) -> None:
    run = load_run(written_run)

    one = [trace_file for trace_file in run.commands if trace_file.subject == COMMAND_ONE]
    assert len(one) == 1
    assert [event.kind for event in one[0].events] == [
        EventKind.STARTED,
        EventKind.FINISHED,
    ]
    request_a = run.events_for_request(REQUEST_A)
    assert {event.sequence for event in request_a} >= {
        event.sequence for event in one[0].events
    }


def test_sequence_is_strictly_increasing_per_host(written_run: Path) -> None:
    run = load_run(written_run)

    by_host: dict[str, list[int]] = {}
    for event in run.events:
        by_host.setdefault(event.host_id, []).append(event.sequence)
    for host_id, sequences in by_host.items():
        assert sequences == sorted(sequences), host_id
        assert len(set(sequences)) == len(sequences), host_id
        assert sequences[0] == 1, host_id


def test_every_line_is_independently_parseable(written_run: Path) -> None:
    paths = sorted((written_run / REQUESTS_DIRNAME).glob("*.jsonl"))
    lines = [
        line
        for path in paths
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == 7
    for line in lines:
        parsed = TraceEvent.from_json_line(line)
        assert parsed.schema_version == run_header_version(written_run)


def run_header_version(run_dir: Path) -> str:
    return load_run(run_dir).header.schema_version


def test_event_detail_must_be_json_serialisable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(run_dir, run_id="detail", host_id="node-a") as run:
        recorder = TraceRecorder("node-a", run=run)
        with pytest.raises(TraceDetailNotSerialisable):
            recorder.event(
                EventKind.ENQUEUE,
                request_id=REQUEST_A,
                detail={"payload": b"raw-bytes-are-not-json"},
            )
        with pytest.raises(TraceDetailNotSerialisable):
            recorder.event(
                EventKind.ENQUEUE,
                request_id=REQUEST_A,
                detail={3: "non-string-key"},  # type: ignore[arg-type]
            )
    # a rejected event is rejected loudly, and leaves no partial line behind
    run = load_run(run_dir)
    assert run.events == ()
