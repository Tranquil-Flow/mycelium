# SPDX-License-Identifier: AGPL-3.0-only
"""`mycelium_testbed` - loopback harness library for the Mycelium core pilot.

The package is a *harness* library: it imports no product code into its public
surface and re-implements no existing harness.  What lives here:

* :mod:`mycelium_testbed.trace` - the lifecycle trace model, the JSONL run
  directory with its ``run.json`` header, clock-offset correction with reported
  uncertainty, and the summary/timeline renderer (PLAN 5.2 item 4).
* :mod:`mycelium_testbed.iroh_e2e` - reuse adapters that subscribe the trace to
  the *existing* iroh e2e harness artefacts (``tests/e2e_request_iroh``), with
  no product-code change.

Write paths are always chosen by the caller and belong outside the repository
(``~/Documents/mycelium-core-program/runs/<run-id>/``).  See
``mycelium_testbed/DESIGN_NOTES.md`` for the seams this package needs from
product code before a physical run (WP5).
"""

from __future__ import annotations

from mycelium_testbed.trace import (
    COMMANDS_DIRNAME,
    DEFAULT_REFERENCE_HOST,
    EXCHANGE_SCHEMA_VERSION,
    LIFECYCLE_ORDER,
    OK_STATUSES,
    REQUESTS_DIRNAME,
    RUN_HEADER_FILENAME,
    TRACE_SCHEMA_VERSION,
    ClockOffset,
    ClockOffsetUnavailable,
    ClockStamp,
    EventKind,
    ExchangeSample,
    HostClock,
    HostRecord,
    RunHeader,
    TimelineEntry,
    TraceDetailNotSerialisable,
    TraceError,
    TraceEvent,
    TraceFile,
    TraceFileWriter,
    TraceRecorder,
    TraceRun,
    TraceRunDirectory,
    TraceSchemaVersionError,
    UnknownTraceEventKind,
    canonical_event_kinds,
    compute_package_digest,
    correct_events,
    derive_exchange_samples,
    estimate_clock_offset,
    estimate_offsets_by_host,
    event_sort_key,
    first_failure,
    iso_utc,
    load_run,
    merge_timeline,
    pair_one_way_samples,
    pair_round_trip_samples,
    read_boot_id,
    read_git_head,
    render_per_host,
    render_timeline,
    summarise,
)

__all__ = [
    "COMMANDS_DIRNAME",
    "ClockOffset",
    "ClockOffsetUnavailable",
    "ClockStamp",
    "DEFAULT_REFERENCE_HOST",
    "EXCHANGE_SCHEMA_VERSION",
    "EventKind",
    "ExchangeSample",
    "HostClock",
    "HostRecord",
    "LIFECYCLE_ORDER",
    "OK_STATUSES",
    "REQUESTS_DIRNAME",
    "RUN_HEADER_FILENAME",
    "RunHeader",
    "TRACE_SCHEMA_VERSION",
    "TimelineEntry",
    "TraceDetailNotSerialisable",
    "TraceError",
    "TraceEvent",
    "TraceFile",
    "TraceFileWriter",
    "TraceRecorder",
    "TraceRun",
    "TraceRunDirectory",
    "TraceSchemaVersionError",
    "UnknownTraceEventKind",
    "canonical_event_kinds",
    "compute_package_digest",
    "correct_events",
    "derive_exchange_samples",
    "estimate_clock_offset",
    "estimate_offsets_by_host",
    "event_sort_key",
    "first_failure",
    "iso_utc",
    "load_run",
    "merge_timeline",
    "pair_one_way_samples",
    "pair_round_trip_samples",
    "read_boot_id",
    "read_git_head",
    "render_per_host",
    "render_timeline",
    "summarise",
]
