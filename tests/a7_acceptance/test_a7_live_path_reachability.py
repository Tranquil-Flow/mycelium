# SPDX-License-Identifier: AGPL-3.0-only
"""A7 KV-successor seam reachability -- through the live recovery call chain.

The A7 deterministic core (`mycelium_a7_kv_successor.py`) classifies a
replacement-path attempt through its closed cutover-eligibility predicate. This
module is the *live-path* companion to `test_kv_successor_runtime.py`: that one
proves what the module does when called directly; this one proves the live path
actually reaches it, and that the live path cannot silently stop doing so.

The live path pinned here is the physical node mesh, not a test-only seam:

* ``physical_inference_node.py`` composes ``Router(node_id=..., topology=...,
  device_states=..., capacity=..., runtime=..., transport=..., clock=...,
  id_source=..., config=...)`` and binds the physical ``IrohTransport`` to it;
* a failed hop arrives as a ``FailureReport`` and the iroh dispatcher hands it
  to ``router.receive_failure_report(message, source_node_id=...)``;
* ``Router.receive_failure_report`` delegates to
  ``mycelium_router.entry.EntryCoordinator.receive_failure_report``, whose
  replacement-path attempt is the Router's checkpoint-coordinator surface (A7
  specification section 2: "Checkpoint coordinator/Router: owns route-wide
  watermark assembly, acknowledgement, fencing generation, replacement path
  attempt, cutover, and exact source/standby cleanup").

The seam asserted here is ``mycelium_router.entry.classify_kv_successor_recovery``
plus the closed classification record it writes to
``RequestRecord.kv_successor_recovery`` on every replacement attempt. The
harness drives the real ``Router``/``EntryCoordinator`` code exactly like the
in-repo production-conformance suite does (in-process fakes stand in for the
physical transport and stage runtimes), so a change that removes the A7 dispatch
from the coordinator fails this module -- the reachability guard the wave-4
packet requires.

Boundary: reachability is not qualification. The classification a live attempt
can honestly produce today is ``full_context_replay`` (no acknowledged KV
watermark exists), and a ``fenced_kv_successor`` classification must refuse the
attempt because the cutover half is not wired. Nothing here claims checkpoint
transport, durable fencing, a physical standby, browser behaviour, qualification
or promotion.
"""

from __future__ import annotations

import inspect
import sys
import types
from dataclasses import replace

import pytest

import mycelium_a7_kv_successor as a7
import mycelium_router.entry as router_entry
from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeRuntimePort,
    FakeTopologyProvider,
    FakeTransportPort,
    InMemoryClientSink,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.idempotency import payload_fingerprint
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table

A7_MODULE = "mycelium_a7_kv_successor"
LIVE_ENTRY_EXCLUSIONS = frozenset({"node-b-stage-000"})
FAILED_HOP = "node-c-stage-001"
PREFILL_COMMIT_TOKEN = 100
RECOVERY_RESUME_TOKEN = 300


class StageLocalRuntime(FakeRuntimePort):
    """The live route's stage-local-KV runtime shape (mirrors conformance).

    The physical runtime in ``stage_local_kv`` mode answers a prefill with one
    committed token and a recovery prefill with a distinct resumed token; the
    in-process fake reproduces exactly that call shape here.
    """

    decode_mode = "stage_local_kv"

    def execute(self, item):
        result = super().execute(item)
        if item.phase == "PREFILL":
            return replace(result, token_id=PREFILL_COMMIT_TOKEN)
        if item.phase == "RECOVERY_PREFILL":
            return replace(result, token_id=RECOVERY_RESUME_TOKEN)
        return result


def _live_router(runtime=None):
    """Compose the router exactly the way the live node composes it."""

    graph = graph_fixture()
    runtime = FakeRuntimePort(token_base=100) if runtime is None else runtime
    transport = FakeTransportPort()
    router = Router(
        node_id="entry-node",
        topology=FakeTopologyProvider(graph),
        device_states=FakeDeviceStateProvider(state_table()),
        capacity=FakeCapacityPort(),
        runtime=runtime,
        transport=transport,
        clock=ManualClock(),
        id_source=SequenceIdSource(),
        config=RouterConfig(),
    )
    return router, runtime, transport


def _drive_failed_replacement(router, runtime, *, fail_token_index=1):
    """Admit, commit one output, then fail a hop so the replacement runs."""

    request = request_fixture()
    sink = InMemoryClientSink()
    request_id = router.admit(
        request, sink, excluded_placements=LIVE_ENTRY_EXCLUSIONS
    )
    runtime.fail_once(
        placement_id=FAILED_HOP,
        phase="DECODE",
        token_index=fail_token_index,
        scope="PLACEMENT",
    )
    router.generate(request_id, token_count=2)
    return request_id, request, sink


def _recording_shim(real, calls):
    """A module-like wrapper around the real A7 module recording predicate calls."""

    shim = types.ModuleType(A7_MODULE)
    shim.CUTOVER_ELIGIBILITY_PROTOCOL = real.CUTOVER_ELIGIBILITY_PROTOCOL
    shim.compare_compatibility = real.compare_compatibility

    def evaluate_cutover_eligibility(**kwargs):
        calls.append(kwargs)
        return real.evaluate_cutover_eligibility(**kwargs)

    shim.evaluate_cutover_eligibility = evaluate_cutover_eligibility
    return shim


def _eligible_shim(real, calls):
    shim = _recording_shim(real, calls)

    def evaluate_cutover_eligibility(**kwargs):
        calls.append(kwargs)
        return {
            "protocol": real.CUTOVER_ELIGIBILITY_PROTOCOL,
            "outcome": "fenced_kv_successor",
            "kv_successor_eligible": True,
            "reason": "eligible",
            "a6_available": kwargs["a6_available"],
        }

    shim.evaluate_cutover_eligibility = evaluate_cutover_eligibility
    return shim


def test_the_live_seam_exists_on_the_entry_coordinator() -> None:
    assert hasattr(router_entry, "classify_kv_successor_recovery")
    assert callable(router_entry.classify_kv_successor_recovery)
    assert router_entry.KV_SUCCESSOR_MODULE == A7_MODULE
    assert router_entry.KV_SUCCESSOR_EXECUTABLE_OUTCOME == "full_context_replay"


def test_live_replacement_attempt_reaches_the_a7_contract() -> None:
    router, runtime, _transport = _live_router(StageLocalRuntime(token_base=100))
    request_id, request, sink = _drive_failed_replacement(router, runtime)

    record = router.get_request(request_id)
    assert record.manifest.path_attempt == 1
    assert router.request_status(request_id) == "DECODING"
    classification = record.kv_successor_recovery

    assert classification["protocol"] == router_entry.KV_SUCCESSOR_SEAM_PROTOCOL
    assert classification["seam"] == (
        "mycelium_router.entry.classify_kv_successor_recovery"
    )
    assert classification["dispatched"] is True
    assert classification["authoritative_module"] == A7_MODULE
    assert classification["authoritative_function"] == (
        "evaluate_cutover_eligibility"
    )
    assert classification["decode_mode"] == "stage_local_kv"
    assert classification["path_attempt"] == 1
    assert classification["committed_token_count"] == 1

    # The nested classification is exactly the real A7 predicate's closed
    # record for the exact facts the router holds: no acknowledged watermark
    # exists, the replacement path IS the qualified replay path, and nothing
    # is qualified or fenced.
    expected = a7.evaluate_cutover_eligibility(
        route_watermark=None,
        gateway_committed_token_count=1,
        gateway_committed_prefix_digest="sha256:"
        + payload_fingerprint(request.prompt_token_ids + (PREFILL_COMMIT_TOKEN,)),
        compatibility=a7.compare_compatibility({}, {}),
        qualifier_current=False,
        fence_installed=False,
        a6_available=True,
    )
    assert classification["classification"] == expected
    assert classification["recovery_mode"] == "full_context_replay"
    assert classification["kv_successor_eligible"] is False
    assert classification["reason"] == "route_watermark_unavailable"
    assert classification["classification"]["protocol"] == (
        a7.CUTOVER_ELIGIBILITY_PROTOCOL
    )

    # The replacement attempt really ran: the recovery prefill was executed and
    # every token index was delivered exactly once.
    recovery_prefills = [
        item for item in runtime.executed if item.phase == "RECOVERY_PREFILL"
    ]
    assert recovery_prefills
    assert recovery_prefills[-1].payload == (
        request.prompt_token_ids + (PREFILL_COMMIT_TOKEN,)
    )
    assert sink.token_indexes == sorted(set(sink.token_indexes))
    assert RECOVERY_RESUME_TOKEN in sink.token_ids


def test_live_path_calls_the_a7_predicate_with_the_router_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, A7_MODULE, _recording_shim(a7, calls))
    router, runtime, _transport = _live_router(StageLocalRuntime(token_base=100))
    request_id, request, _sink = _drive_failed_replacement(router, runtime)

    assert calls, "the live replacement attempt never reached the A7 predicate"
    assert calls[-1] == {
        "route_watermark": None,
        "gateway_committed_token_count": 1,
        "gateway_committed_prefix_digest": "sha256:"
        + payload_fingerprint(request.prompt_token_ids + (PREFILL_COMMIT_TOKEN,)),
        "compatibility": a7.compare_compatibility({}, {}),
        "qualifier_current": False,
        "fence_installed": False,
        "a6_available": True,
    }
    classification = router.get_request(request_id).kv_successor_recovery
    assert classification["dispatched"] is True
    assert classification["reason"] == "route_watermark_unavailable"


def test_unavailable_a7_module_is_recorded_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, A7_MODULE, None)
    router, runtime, _transport = _live_router(StageLocalRuntime(token_base=100))
    request_id, _request, sink = _drive_failed_replacement(router, runtime)

    record = router.get_request(request_id)
    classification = record.kv_successor_recovery
    assert classification["dispatched"] is False
    assert classification["reason"] == "a7_kv_successor_unavailable"
    assert classification["recovery_mode"] == "full_context_replay"
    assert classification["kv_successor_eligible"] is False
    assert classification["classification"] is None

    # The qualified replay still completes: the seam's absence is visible in the
    # record, it is never a KV-successor claim.
    assert record.manifest.path_attempt == 1
    assert RECOVERY_RESUME_TOKEN in sink.token_ids


def test_a_fenced_kv_successor_classification_never_downgrades_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, A7_MODULE, _eligible_shim(a7, calls))
    router, runtime, _transport = _live_router(StageLocalRuntime(token_base=100))
    request_id, _request, sink = _drive_failed_replacement(router, runtime)

    assert calls, "the live replacement attempt never reached the A7 predicate"
    record = router.get_request(request_id)
    classification = record.kv_successor_recovery
    assert classification["dispatched"] is True
    assert classification["recovery_mode"] == "fenced_kv_successor"
    assert classification["kv_successor_eligible"] is True

    # The router cannot execute a fenced KV cutover, so the attempt fails closed:
    # it is never silently replayed under a fenced-KV classification.
    assert router.request_status(request_id) == "FAILED"
    assert record.cleaned_up is True
    assert [
        item for item in runtime.executed if item.phase == "RECOVERY_PREFILL"
    ] == []
    assert sink.token_ids == [PREFILL_COMMIT_TOKEN]


def test_the_seam_sits_on_the_physical_recovery_call_chain() -> None:
    router_source = inspect.getsource(Router.receive_failure_report)
    assert "self.entry.receive_failure_report" in router_source

    entry_source = inspect.getsource(
        router_entry.EntryCoordinator.receive_failure_report
    )
    assert "classify_kv_successor_recovery(" in entry_source

    from pathlib import Path

    node_source = (Path.cwd() / "physical_inference_node.py").read_text("utf-8")
    assert "from mycelium_router.router import Router" in node_source
    assert "router = Router(" in node_source
    assert "transport.bind_router(router)" in node_source

    iroh_source = (
        Path.cwd() / "mycelium_router" / "transports" / "iroh.py"
    ).read_text("utf-8")
    assert "router.receive_failure_report(message, source_node_id=source_node_id)" in (
        iroh_source
    )


def test_the_classification_is_deterministic_and_closed() -> None:
    first = router_entry.classify_kv_successor_recovery(
        decode_mode="stage_local_kv",
        path_attempt=1,
        prompt_token_ids=(1, 2, 3),
        committed_token_ids=(4, 5),
    )
    second = router_entry.classify_kv_successor_recovery(
        decode_mode="stage_local_kv",
        path_attempt=1,
        prompt_token_ids=(1, 2, 3),
        committed_token_ids=(4, 5),
    )
    assert first == second
    assert set(first) == {
        "protocol",
        "seam",
        "dispatched",
        "authoritative_module",
        "authoritative_function",
        "decode_mode",
        "path_attempt",
        "committed_token_count",
        "recovery_mode",
        "kv_successor_eligible",
        "reason",
        "classification",
        "claim_boundary",
    }
    assert first["committed_token_count"] == 2
    assert first["claim_boundary"] == router_entry.KV_SUCCESSOR_SEAM_CLAIM_BOUNDARY
    with pytest.raises(ValueError):
        router_entry.classify_kv_successor_recovery(
            decode_mode="live_route",
            path_attempt=1,
            prompt_token_ids=(1,),
            committed_token_ids=(),
        )
