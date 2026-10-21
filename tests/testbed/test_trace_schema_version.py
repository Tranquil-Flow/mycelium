# SPDX-License-Identifier: AGPL-3.0-only
"""The trace file format is versioned, and an unknown event kind fails loudly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_testbed.trace import (
    COMMANDS_DIRNAME,
    LIFECYCLE_ORDER,
    REQUESTS_DIRNAME,
    RUN_HEADER_FILENAME,
    TRACE_SCHEMA_VERSION,
    EventKind,
    TraceEvent,
    TraceRecorder,
    TraceRunDirectory,
    TraceSchemaVersionError,
    UnknownTraceEventKind,
    canonical_event_kinds,
    load_run,
)

# A frozen literal on purpose: changing the trace format must be a deliberate
# edit in two places, never a silent drift between producer and reader.
EXPECTED_SCHEMA_VERSION = "mycelium.testbed.trace.v1"
EXPECTED_KINDS = ("enqueue", "delivered", "started", "finished", "receipt", "terminal")


def test_schema_version_and_vocabulary_are_frozen() -> None:
    assert TRACE_SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION
    assert canonical_event_kinds() == EXPECTED_KINDS
    assert tuple(kind.value for kind in LIFECYCLE_ORDER) == EXPECTED_KINDS


def test_every_written_document_carries_the_schema_version(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(run_dir, run_id="schema", host_id="node-a") as run:
        recorder = TraceRecorder("node-a", run=run)
        for kind in LIFECYCLE_ORDER:
            recorder.event(kind, request_id="request-schema", command_id="command-schema")

    header = json.loads((run_dir / RUN_HEADER_FILENAME).read_text(encoding="utf-8"))
    assert header["schema_version"] == EXPECTED_SCHEMA_VERSION
    assert header["event_kinds"] == list(EXPECTED_KINDS)

    lines = [
        line
        for directory in (REQUESTS_DIRNAME, COMMANDS_DIRNAME)
        for path in (run_dir / directory).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 12
    for line in lines:
        assert json.loads(line)["schema_version"] == EXPECTED_SCHEMA_VERSION

    run = load_run(run_dir)
    assert run.header.schema_version == EXPECTED_SCHEMA_VERSION
    assert [event.kind for event in run.events] == list(LIFECYCLE_ORDER)


def test_unknown_event_kind_raises_instead_of_being_dropped() -> None:
    with pytest.raises(UnknownTraceEventKind):
        EventKind.parse("teleported")

    with pytest.raises(UnknownTraceEventKind):
        EventKind.parse(None)

    document = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "kind": "teleported",
        "host_id": "node-a",
        "pid": 1,
        "recorder_id": "node-a-1-aaaa",
        "sequence": 1,
        "monotonic_ns": 1,
        "wall_ns": 1,
        "request_id": "request-schema",
    }
    with pytest.raises(UnknownTraceEventKind):
        TraceEvent.from_document(document)


def test_unknown_event_kind_in_a_file_fails_the_whole_load(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(run_dir, run_id="schema", host_id="node-a") as run:
        TraceRecorder("node-a", run=run).event(
            EventKind.ENQUEUE, request_id="request-schema"
        )
    request_file = next((run_dir / REQUESTS_DIRNAME).glob("*.jsonl"))
    request_file.write_text(
        request_file.read_text(encoding="utf-8")
        + json.dumps(
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "kind": "teleported",
                "host_id": "node-a",
                "pid": 1,
                "recorder_id": "node-a-1-aaaa",
                "sequence": 2,
                "monotonic_ns": 2,
                "wall_ns": 2,
                "request_id": "request-schema",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(UnknownTraceEventKind) as error:
        load_run(run_dir)
    assert "line=2" in str(error.value)


def test_mismatched_schema_version_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with TraceRunDirectory(run_dir, run_id="schema", host_id="node-a") as run:
        TraceRecorder("node-a", run=run).event(
            EventKind.ENQUEUE, request_id="request-schema"
        )

    request_file = next((run_dir / REQUESTS_DIRNAME).glob("*.jsonl"))
    document = json.loads(request_file.read_text(encoding="utf-8").splitlines()[0])
    document["schema_version"] = "mycelium.testbed.trace.vNEXT"
    request_file.write_text(json.dumps(document) + "\n", encoding="utf-8")
    with pytest.raises(TraceSchemaVersionError):
        load_run(run_dir)

    header_path = run_dir / RUN_HEADER_FILENAME
    header = json.loads(header_path.read_text(encoding="utf-8"))
    header["schema_version"] = "mycelium.testbed.trace.vNEXT"
    header_path.write_text(json.dumps(header), encoding="utf-8")
    with pytest.raises(TraceSchemaVersionError):
        load_run(run_dir)


def test_a_missing_integer_field_is_refused() -> None:
    document = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "kind": "enqueue",
        "host_id": "node-a",
        "pid": 1,
        "recorder_id": "node-a-1-aaaa",
        "sequence": 1,
        "monotonic_ns": 1,
        "wall_ns": "not-an-integer",
        "request_id": "request-schema",
    }
    with pytest.raises(Exception) as error:
        TraceEvent.from_document(document)
    assert "trace_field_not_an_integer" in str(error.value)
