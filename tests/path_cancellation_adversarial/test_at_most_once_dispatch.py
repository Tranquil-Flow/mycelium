# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic at-most-once dispatch tests (packet a4-at-most-once).

The soak fired ``at_most_one_native_execution_per_command`` when a work item was
left in the relay's shared execution queue by a dispatch that was *rejected after
its enqueue*, and a later, unrelated dispatch popped it and executed it - a
second native execution of a command identity whose path was already gone.

These tests construct that window deterministically (no wall clock, no load, no
sleeps): a duplicate frame for one command is injected while the path is
released underneath it, and the next request's dispatch is the popper.

RED before the fix, GREEN after:

* :func:`test_rejected_late_dispatch_leaves_no_stale_work`
* :func:`test_rejected_late_dispatch_is_not_executed_by_a_later_request`
* :func:`test_duplicate_frame_for_the_same_command_executes_once`
* :func:`test_legitimate_retry_after_a_terminal_failure_still_executes`
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading

import pytest

import mycelium_router.relay as relay_module
from mycelium_router.contracts import HopHeader, RequestContext
from mycelium_router.fakes import InMemoryClientSink
from test_router_policy import request_fixture

from ._harness import ENTRY_NODE, build_mesh_case, join_bounded

EXCLUDED_PLACEMENTS = frozenset({"node-b-stage-000"})


@dataclass(frozen=True)
class _Frame:
    """One captured hop delivery: the frame a duplicate would repeat."""

    header: HopHeader
    payload: object
    source_node_id: str


def _second_request(request_id: str, *, max_new_tokens: int = 3) -> RequestContext:
    return replace(
        request_fixture(),
        request_id=request_id,
        max_new_tokens=max_new_tokens,
    )


def _executions_for(runtime, idempotency_key: str) -> list[object]:
    return [
        item for item in runtime.executed if item.idempotency_key == idempotency_key
    ]


def _capture_entry_deliveries(relay) -> list[_Frame]:
    """Record the frames the entry relay receives (header + payload)."""

    frames: list[_Frame] = []
    original = relay.receive_hop

    def capture(header, payload, *, source_node_id=None):
        frames.append(_Frame(header, payload, source_node_id or ""))
        return original(header, payload, source_node_id=source_node_id)

    relay.receive_hop = capture
    return frames


def _pause_relay_fingerprint(entered: threading.Event, release: threading.Event):
    """Pause one duplicate delivery *after* it read path state, *before* the cache.

    ``receive_hop`` reads the registration, validates the header, then
    fingerprints the payload and only then consults the result cache.  Pausing at
    the fingerprint is the deterministic seam for the window the soak hit: the
    delivery has already observed a live registered path, and the release lands
    before it consults the cache and enqueues its work item.
    """

    original = relay_module.payload_fingerprint

    def paused(payload: object) -> str:
        entered.set()
        assert release.wait(timeout=1.0)
        return original(payload)

    relay_module.payload_fingerprint = paused
    return original


def _decode_frame(case) -> _Frame:
    """Run one legitimate decode step and return the hop-0 frame it delivered."""

    relay = case.entry.relay
    frames = _capture_entry_deliveries(relay)
    try:
        assert case.entry.decode_one_distributed(case.request.request_id) is True
    finally:
        del relay.receive_hop
    assert frames, "entry decode hop was not delivered"
    return frames[0]


def _inject_duplicate(case, frame: _Frame, results, errors) -> threading.Thread:
    """Deliver one recorded frame again through the router ingress."""

    def call():
        return case.routers[ENTRY_NODE].receive_hop(
            frame.header,
            frame.payload,
            source_node_id=frame.source_node_id or None,
        )

    def target() -> None:
        try:
            results.append(call())
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion
            errors.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_rejected_late_dispatch_leaves_no_stale_work() -> None:
    """A dispatch rejected after its enqueue must not leave its item queued."""

    case = build_mesh_case(request_id="request-at-most-once-stale")
    relay = case.entry.relay
    frame = _decode_frame(case)
    assert _executions_for(case.runtimes[ENTRY_NODE], frame.header.idempotency_key)

    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []
    original = _pause_relay_fingerprint(entered, release)
    try:
        thread = _inject_duplicate(case, frame, results, errors)
        assert entered.wait(timeout=1.0)
        # The release lands while the duplicate delivery sits between its path
        # read and its cache read - the exact window the soak opened.
        assert case.entry.cancel(case.request.request_id) is True
        release.set()
        join_bounded(thread)
    finally:
        release.set()
        relay_module.payload_fingerprint = original

    assert errors == []
    assert [getattr(result, "reason", None) for result in results] == ["path_cancelled"]
    # The rejected dispatch owns its item: nothing may survive in the queue for a
    # later dispatch to pick up.
    assert relay.scheduler.queue_depth() == 0


def test_rejected_late_dispatch_is_not_executed_by_a_later_request() -> None:
    """The stale item must never execute a second time, under any later dispatch."""

    case = build_mesh_case(request_id="request-at-most-once-replay")
    relay = case.entry.relay
    runtime = case.runtimes[ENTRY_NODE]
    frame = _decode_frame(case)
    key = frame.header.idempotency_key
    assert len(_executions_for(runtime, key)) == 1

    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []
    original = _pause_relay_fingerprint(entered, release)
    try:
        thread = _inject_duplicate(case, frame, results, errors)
        assert entered.wait(timeout=1.0)
        assert case.entry.cancel(case.request.request_id) is True
        release.set()
        join_bounded(thread)
    finally:
        release.set()
        relay_module.payload_fingerprint = original

    # Age the rejected item so a priority-ordered pop would prefer it, then let a
    # completely separate request be prefilled on the same entry router.
    case.clock.advance(30.0)
    second = _second_request("request-at-most-once-next")
    sink = InMemoryClientSink()
    try:
        case.entry.start_distributed_prefill(
            second,
            sink,
            excluded_placements=EXCLUDED_PLACEMENTS,
        )
    except BaseException as error:  # noqa: BLE001 - the assertion below is the point
        errors.append(error)

    # at_most_one_native_execution_per_command, as the invariant is stated: the
    # released command identity executed once and only once.
    assert len(_executions_for(runtime, key)) == 1
    # and the later request's own command really did execute.
    assert len(
        _executions_for(runtime, f"{second.request_id}:path-2:0:PREFILL:-1:0")
    ) == 1


def test_duplicate_frame_for_the_same_command_executes_once() -> None:
    """A duplicated frame of one command is absorbed, not executed twice."""

    case = build_mesh_case(request_id="request-at-most-once-duplicate-frame")
    runtime = case.runtimes[ENTRY_NODE]
    frame = _decode_frame(case)
    key = frame.header.idempotency_key
    assert len(_executions_for(runtime, key)) == 1

    # The same frame, delivered again while the path is live (a duplicated
    # control frame - the soak's transport duplicate).
    duplicate = case.routers[ENTRY_NODE].receive_hop(
        frame.header,
        frame.payload,
        source_node_id=frame.source_node_id or None,
    )

    assert getattr(duplicate, "disposition", None) in {"FORWARDED", "COMPLETED"}
    assert len(_executions_for(runtime, key)) == 1


def test_legitimate_retry_after_a_terminal_failure_still_executes() -> None:
    """The fix must not swallow legitimate work: a later attempt still executes."""

    case = build_mesh_case(request_id="request-at-most-once-retry", max_new_tokens=3)
    relay = case.entry.relay
    runtime = case.runtimes[ENTRY_NODE]
    frame = _decode_frame(case)
    key = frame.header.idempotency_key
    assert len(_executions_for(runtime, key)) == 1

    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []
    original = _pause_relay_fingerprint(entered, release)
    try:
        thread = _inject_duplicate(case, frame, results, errors)
        assert entered.wait(timeout=1.0)
        assert case.entry.cancel(case.request.request_id) is True
        release.set()
        join_bounded(thread)
    finally:
        release.set()
        relay_module.payload_fingerprint = original

    assert errors == []
    assert relay.scheduler.queue_depth() == 0

    # A fresh request is legitimate new work and must execute (retry semantics are
    # untouched: nothing about the rejected identity is remembered as "done").
    case.clock.advance(30.0)
    second = _second_request("request-at-most-once-fresh")
    sink = InMemoryClientSink()
    case.entry.start_distributed_prefill(
        second,
        sink,
        excluded_placements=EXCLUDED_PLACEMENTS,
    )
    assert len(
        _executions_for(runtime, f"{second.request_id}:path-2:0:PREFILL:-1:0")
    ) == 1
    assert len(_executions_for(runtime, key)) == 1


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(pytest.main([__file__, "-q"]))
