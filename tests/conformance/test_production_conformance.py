"""Cross-check the public production Router against the independent model."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from mycelium_conformance.router_model import ModelEvent, ModelState, RouterModel
from mycelium_conformance.trace_generator import (
    TraceAction,
    generate_bounded_traces,
    minimize_trace,
    run_reference_trace,
    trace_to_json,
)
from mycelium_router.contracts import (
    FailureReport,
    HopHeader,
    RouterConfig,
    TokenEvent,
)
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeRuntimePort,
    FakeTopologyProvider,
    FakeTransportPort,
    InMemoryClientSink,
    InProcessMesh,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from test_router_contracts import graph_fixture
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table


REQUEST_ID = "request-conformance"


class StageLocalFakeRuntime(FakeRuntimePort):
    decode_mode = "stage_local_kv"

    def execute(self, item):
        result = super().execute(item)
        if item.phase in {"PREFILL", "RECOVERY_PREFILL"}:
            return replace(result, token_id=self.token_base)
        return result


@dataclass(frozen=True)
class ObservableState:
    phase: str
    path_attempt: int
    next_sequence: int
    tokens: tuple[int, ...]
    sink_tokens: tuple[int, ...]
    reservations: int
    release_count: int
    runtime_cancel_count: int
    terminal_count: int
    recovery_count: int


@dataclass(frozen=True)
class ProductionDetail:
    observable: ObservableState
    path_identity: tuple[object, ...]
    active_reservation_ids: frozenset[str]
    release_calls: tuple[tuple[str, ...], ...]
    runtime_cancel_calls: tuple[str, ...]


@dataclass(frozen=True)
class TraceDifference:
    action_index: int
    action: str
    model_code: str
    expected: ObservableState
    observed: ObservableState
    reason: str


class ProductionTraceDriver:
    """Materialize symbolic events only through existing Router public methods."""

    def __init__(self) -> None:
        self.graph = graph_fixture()
        self.clock = ManualClock()
        self.capacity = FakeCapacityPort(clock=self.clock)
        self.runtime = FakeRuntimePort()
        self.transport = FakeTransportPort()
        self.sink = InMemoryClientSink()
        self.request = request_fixture(
            request_id=REQUEST_ID,
            prompt_token_ids=(11, 12),
            max_new_tokens=2,
            expected_new_tokens=2,
        )
        self.router = Router(
            node_id="node-a",
            topology=FakeTopologyProvider(self.graph),
            device_states=FakeDeviceStateProvider(state_table()),
            capacity=self.capacity,
            runtime=self.runtime,
            transport=self.transport,
            clock=self.clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(maximum_recovery_attempts=1),
        )
        self.manifests: dict[int, object] = {}

    def apply(
        self,
        action: TraceAction,
        resolved_events: tuple[ModelEvent, ...],
    ) -> None:
        if action.name in {"admit", "duplicate_admit"}:
            self._admit()
            return
        if action.name == "cancel":
            self.router.cancel(REQUEST_ID)
            return
        event = resolved_events[0]
        if event.kind == "TOKEN":
            self._token(event)
            return
        if event.kind == "FAILURE":
            self._failure(event)
            return
        raise AssertionError(f"unmapped_trace_action:{action.name}")

    def _admit(self) -> None:
        try:
            self.router.admit(
                self.request,
                self.sink,
                excluded_placements=frozenset({"node-b-stage-000"}),
            )
        except ValueError as error:
            if str(error) != "duplicate_request_id":
                raise
        self._remember_current_manifest()

    def _token(self, event: ModelEvent) -> None:
        manifest = self.manifests.get(event.path_attempt)
        path_id = self._materialize_path_id(event, manifest)
        source = self._token_source(event.peer, manifest)
        token_id = event.payload[0] if event.payload else 0
        self.router.receive_token_event(
            TokenEvent(
                request_id=REQUEST_ID,
                path_id=path_id,
                path_attempt=event.path_attempt if event.path_attempt is not None else -1,
                token_index=event.sequence if event.sequence is not None else -1,
                token_id=token_id,
                sampling_counter=(event.sequence or 0) + 1,
            ),
            source_node_id=source,
        )

    def _failure(self, event: ModelEvent) -> None:
        manifest = self.manifests.get(event.path_attempt)
        path_id = self._materialize_path_id(event, manifest)
        placement_id, owner = self._failure_identity(event.peer, manifest)
        source = owner
        if event.peer == "non_owner":
            source = "node-off-path"
        if event.peer == "off_path":
            source = "node-off-path"
        if event.payload == ("failure",):
            token_index = (event.sequence if event.sequence is not None else 0) - 1
            for stage in self.graph.stages:
                for placement in stage.placements:
                    self.runtime.fail_once(
                        placement_id=placement.placement_id,
                        phase="RECOVERY_PREFILL",
                        token_index=token_index,
                        scope="PLACEMENT",
                    )
        self.router.receive_failure_report(
            FailureReport(
                request_id=REQUEST_ID,
                path_id=path_id,
                path_attempt=event.path_attempt if event.path_attempt is not None else -1,
                token_index=event.sequence if event.sequence is not None else -1,
                scope="PLACEMENT",
                reason="conformance_injected_failure",
                placement_id=placement_id,
                node_id=owner,
            ),
            source_node_id=source,
        )
        self._remember_current_manifest()

    def _remember_current_manifest(self) -> None:
        try:
            record = self.router.get_request(REQUEST_ID)
        except KeyError:
            return
        self.manifests[record.manifest.path_attempt] = record.manifest

    @staticmethod
    def _materialize_path_id(event: ModelEvent, manifest) -> str:
        canonical = f"path-{event.path_attempt}"
        if manifest is not None and event.path_id == canonical:
            return manifest.path_id
        return f"conformance-{event.path_id or 'missing-path'}"

    def _token_source(self, peer: str, manifest) -> str:
        if manifest is None:
            return "node-off-path"
        if peer == "off_path":
            return "node-off-path"
        placement_nodes = self._placement_nodes()
        final_node = placement_nodes[manifest.ordered_hops[-1].placement_id]
        if peer == "final":
            return final_node
        if peer == "non_final":
            return next(
                placement_nodes[hop.placement_id]
                for hop in manifest.ordered_hops[:-1]
                if placement_nodes[hop.placement_id] != final_node
            )
        return "node-off-path"

    def _failure_identity(self, peer: str, manifest) -> tuple[str, str]:
        if manifest is None or peer == "off_path":
            return "off-path-placement", "node-off-path"
        placement = manifest.ordered_hops[len(manifest.ordered_hops) // 2]
        owner = self._placement_nodes()[placement.placement_id]
        return placement.placement_id, owner

    def _placement_nodes(self) -> dict[str, str]:
        return {
            placement.placement_id: placement.node_id
            for stage in self.graph.stages
            for placement in stage.placements
        }

    def detail(self) -> ProductionDetail:
        active = frozenset(self.capacity.committed_ids - self.capacity.released_ids)
        try:
            record = self.router.get_request(REQUEST_ID)
        except KeyError:
            observable = ObservableState(
                phase="NEW",
                path_attempt=-1,
                next_sequence=0,
                tokens=(),
                sink_tokens=tuple(self.sink.token_ids),
                reservations=len(active),
                release_count=len(self.capacity.release_calls),
                runtime_cancel_count=len(self.runtime.cancel_calls),
                terminal_count=0,
                recovery_count=0,
            )
            path_identity: tuple[object, ...] = ()
        else:
            terminal = int(record.status in {"COMPLETED", "CANCELLED", "FAILED"})
            observable = ObservableState(
                phase=record.status,
                path_attempt=record.manifest.path_attempt,
                next_sequence=len(record.generated_token_ids),
                tokens=tuple(record.generated_token_ids),
                sink_tokens=tuple(self.sink.token_ids),
                reservations=len(active),
                release_count=len(self.capacity.release_calls),
                runtime_cancel_count=len(self.runtime.cancel_calls),
                terminal_count=terminal,
                recovery_count=record.manifest.path_attempt,
            )
            path_identity = (
                record.manifest.path_id,
                record.manifest.path_attempt,
                record.manifest.ordered_hops,
                record.manifest.loopback_edge_id,
            )
        return ProductionDetail(
            observable=observable,
            path_identity=path_identity,
            active_reservation_ids=active,
            release_calls=tuple(self.capacity.release_calls),
            runtime_cancel_calls=tuple(self.runtime.cancel_calls),
        )


def reference_model() -> RouterModel:
    return RouterModel(
        prompt_tokens=(11, 12),
        maximum_new_tokens=2,
        path_width=3,
        maximum_recovery_attempts=1,
    )


def project_model(state: ModelState) -> ObservableState:
    return ObservableState(
        phase=state.phase,
        path_attempt=state.path_attempt,
        next_sequence=state.next_sequence,
        tokens=state.emitted_tokens,
        sink_tokens=state.emitted_tokens,
        reservations=state.reservations,
        release_count=state.release_count,
        runtime_cancel_count=state.runtime_cancel_count,
        terminal_count=state.terminal_count,
        recovery_count=state.recovery_count,
    )


def compare_trace(trace: tuple[TraceAction, ...]) -> TraceDifference | None:
    reference = run_reference_trace(reference_model(), trace)
    production = ProductionTraceDriver()
    for index, action in enumerate(trace):
        before = production.detail()
        production.apply(action, reference.resolved_events[index])
        after = production.detail()
        expected = project_model(reference.states[index + 1])
        if expected != after.observable:
            return TraceDifference(
                action_index=index,
                action=action.name,
                model_code=reference.codes[index],
                expected=expected,
                observed=after.observable,
                reason="observable_state_mismatch",
            )
        model_mutated = reference.states[index + 1] != reference.states[index]
        if not model_mutated and after != before:
            return TraceDifference(
                action_index=index,
                action=action.name,
                model_code=reference.codes[index],
                expected=expected,
                observed=after.observable,
                reason="rejected_event_mutated_production_detail",
            )
    return None


def test_all_4385_bounded_request_traces_match_public_router():
    traces = generate_bounded_traces(maximum_tail_depth=3)
    for trace in traces:
        difference = compare_trace(trace)
        if difference is None:
            continue
        minimal = minimize_trace(trace, lambda candidate: compare_trace(candidate) is not None)
        minimal_difference = compare_trace(minimal)
        pytest.fail(
            "model/production lifecycle disagreement\n"
            f"minimal_trace={trace_to_json(minimal)}\n"
            f"difference={minimal_difference!r}"
        )


def make_router(
    *,
    config: RouterConfig | None = None,
    transport=None,
    runtime=None,
):
    graph = graph_fixture()
    capacity = FakeCapacityPort()
    selected_runtime = runtime or FakeRuntimePort()
    selected_transport = transport or FakeTransportPort()
    router = Router(
        node_id="node-a",
        topology=FakeTopologyProvider(graph),
        device_states=FakeDeviceStateProvider(state_table()),
        capacity=capacity,
        runtime=selected_runtime,
        transport=selected_transport,
        clock=ManualClock(),
        id_source=SequenceIdSource(),
        config=config or RouterConfig(),
    )
    return router, graph, capacity, selected_runtime, selected_transport


def test_tokens_reject_before_request_and_during_progressive_prefill():
    router, _, capacity, _, transport = make_router()
    sink = InMemoryClientSink()
    unknown = TokenEvent(
        request_id=REQUEST_ID,
        path_id="unknown-path",
        path_attempt=0,
        token_index=0,
        token_id=999,
        sampling_counter=1,
    )

    assert not router.receive_token_event(unknown, source_node_id="node-a")
    assert capacity.committed_ids == set()

    router.start_distributed_prefill(
        request_fixture(
            request_id=REQUEST_ID,
            prompt_token_ids=(11, 12),
            max_new_tokens=2,
        ),
        sink,
        excluded_placements=frozenset({"node-b-stage-000"}),
    )
    header, _ = transport.hops[0]
    active_before = capacity.committed_ids - capacity.released_ids

    assert router.request_status(REQUEST_ID) == "PREFILL"
    assert not router.receive_token_event(
        replace(
            unknown,
            path_id=header.path_id,
            path_attempt=header.path_attempt,
        ),
        source_node_id="node-a",
    )
    assert router.request_status(REQUEST_ID) == "PREFILL"
    assert sink.token_ids == []
    assert capacity.committed_ids - capacity.released_ids == active_before


def test_tokens_reject_while_chunked_prefill_is_locked():
    graph = three_device_graph()
    states = state_table()
    states["node-d"] = replace(states["node-a"], node_id="node-d")
    capacity = FakeCapacityPort()
    mesh = InProcessMesh()
    routers = {}
    for node_id in ("node-a", "node-c", "node-d"):
        router = Router(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider(states),
            capacity=capacity,
            runtime=FakeRuntimePort(),
            transport=mesh.transport_for(node_id),
            clock=ManualClock(),
            id_source=SequenceIdSource(),
            config=RouterConfig(prefill_chunk_size_tokens=2),
        )
        mesh.register_router(node_id, router)
        routers[node_id] = router
    mesh.defer_prefill_chunk_completions = True
    entry = routers["node-a"]
    sink = InMemoryClientSink()
    request = request_fixture(
        request_id=REQUEST_ID,
        prompt_token_ids=(11, 12, 13, 14),
        max_new_tokens=2,
    )

    entry.start_distributed_prefill(
        request,
        sink,
        excluded_placements=frozenset({"node-b-stage-000"}),
    )
    record = entry.get_request(REQUEST_ID)
    placement_nodes = {
        placement.placement_id: placement.node_id
        for stage in graph.stages
        for placement in stage.placements
    }
    final_node = placement_nodes[record.manifest.ordered_hops[-1].placement_id]

    assert record.status == "LOCKED"
    assert not entry.receive_token_event(
        TokenEvent(
            request_id=REQUEST_ID,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            token_index=0,
            token_id=999,
            sampling_counter=1,
        ),
        source_node_id=final_node,
    )
    assert record.status == "LOCKED"
    assert record.generated_token_ids == []
    assert sink.token_ids == []


def test_recovery_runtime_receives_prompt_plus_tokens_as_explicit_prefill():
    driver = ProductionTraceDriver()
    reference = run_reference_trace(
        reference_model(),
        (
            TraceAction("admit"),
            TraceAction("token_next"),
            TraceAction("failure_current"),
        ),
    )
    actions = (
        TraceAction("admit"),
        TraceAction("token_next"),
        TraceAction("failure_current"),
    )
    for index, action in enumerate(actions):
        driver.apply(action, reference.resolved_events[index])

    recovery_items = [
        item for item in driver.runtime.executed if item.phase == "RECOVERY_PREFILL"
    ]
    assert recovery_items
    assert recovery_items[0].payload == (11, 12, 101)
    assert driver.router.get_request(REQUEST_ID).manifest.path_attempt == 1


def _registered_hop_fixture(*, runtime=None):
    router, _, _, runtime, transport = make_router(runtime=runtime)
    request = request_fixture(
        request_id=REQUEST_ID,
        prompt_token_ids=(11, 12),
        max_new_tokens=2,
    )
    router.admit(
        request,
        InMemoryClientSink(),
        excluded_placements=frozenset({"node-b-stage-000"}),
    )
    record = router.get_request(REQUEST_ID)
    assert router.register_path(request, record.manifest, record.graph)
    first_hop = record.manifest.ordered_hops[0]
    header = HopHeader(
        request_id=REQUEST_ID,
        path_id=record.manifest.path_id,
        path_attempt=record.manifest.path_attempt,
        phase="DECODE",
        token_index=0,
        hop_index=0,
        source_placement_id=record.manifest.ordered_hops[-1].placement_id,
        destination_placement_id=first_hop.placement_id,
        topology_version=record.manifest.topology_version,
        idempotency_key=hop_idempotency_key(
            request_id=REQUEST_ID,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            phase="DECODE",
            token_index=0,
            hop_index=0,
        ),
    )
    return router, runtime, transport, header


def _registered_hop_replay(
    second_payload: bytes,
) -> tuple[str, str, tuple[int, int], tuple[int, int]]:
    router, runtime, transport, header = _registered_hop_fixture()
    first = router.receive_hop(header, b"activation-a")
    before_second = (len(runtime.executed), len(transport.hops))
    second = router.receive_hop(header, second_payload)
    after_second = (len(runtime.executed), len(transport.hops))
    return first.disposition, second.disposition, before_second, after_second


def _pending_hop_replay(
    second_payload: bytes,
) -> tuple[str, str, str, tuple[int, int], tuple[int, int]]:
    router, runtime, _, header = _registered_hop_fixture()
    first = router.enqueue_hop(header, b"activation-a")
    before_second = (router.pending_batch_hops(), len(runtime.executed_batches))
    second = router.enqueue_hop(header, second_payload)
    after_second = (router.pending_batch_hops(), len(runtime.executed_batches))
    return (
        first.disposition,
        second.disposition,
        second.reason,
        before_second,
        after_second,
    )


def _hop_trace_disagrees(trace: tuple[TraceAction, ...]) -> bool:
    names = tuple(action.name for action in trace)
    required = ("admit_and_register", "hop_activation_a", "hop_activation_b")
    if names != required:
        return False
    _, disposition, _, _ = _registered_hop_replay(b"activation-b")
    return disposition != "REJECTED"


def test_exact_locked_hop_duplicate_is_cached_without_side_effects():
    first, duplicate, before, after = _registered_hop_replay(b"activation-a")

    assert first == "FORWARDED"
    assert duplicate == first
    assert after == before


def test_stage_local_exact_locked_hop_duplicate_is_cached_without_side_effects():
    router, runtime, transport, header = _registered_hop_fixture(
        runtime=StageLocalFakeRuntime()
    )
    first = router.receive_hop(header, b"activation-a")
    before = (len(runtime.executed), len(transport.hops))
    duplicate = router.receive_hop(header, b"activation-a")

    assert duplicate == first
    assert (len(runtime.executed), len(transport.hops)) == before


def test_counterexample_conflicting_locked_hop_payload_fails_closed():
    machine = reference_model()
    state = run_reference_trace(machine, (TraceAction("admit"),)).final_state
    first_event = ModelEvent(
        "HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=0,
        peer="entry",
        hop_index=0,
        payload=(1,),
    )
    first = machine.apply(state, first_event)
    expected = machine.apply(first.state, replace(first_event, payload=(2,)))
    assert expected.code == "conflicting_duplicate"

    _, disposition, before_conflict, after_conflict = _registered_hop_replay(
        b"activation-b"
    )
    if disposition != "REJECTED":
        trace = tuple(
            TraceAction(name)
            for name in (
                "admit_and_register",
                "hop_activation_a",
                "hop_activation_b",
            )
        )
        minimal = minimize_trace(trace, _hop_trace_disagrees)
        pytest.fail(
            "conflicting locked-hop replay returned cached success\n"
            f"minimal_trace={trace_to_json(minimal)}\n"
            f"observed_disposition={disposition}\n"
            f"side_effect_counts_before={before_conflict}\n"
            f"side_effect_counts_after={after_conflict}"
        )
    assert after_conflict == before_conflict


def _progressive_prefill_replay(
    *,
    conflicting: bool,
    runtime=None,
) -> tuple[str, str, tuple[int, int], tuple[int, int]]:
    router, _, _, runtime, transport = make_router(runtime=runtime)
    router.start_distributed_prefill(
        request_fixture(
            request_id=REQUEST_ID,
            prompt_token_ids=(11, 12),
            max_new_tokens=2,
        ),
        InMemoryClientSink(),
        excluded_placements=frozenset({"node-b-stage-000"}),
    )
    header, context = transport.hops[0]
    first = router.receive_progressive_prefill(header, context)
    assert first.disposition in {"FORWARDED", "LOCKED"}
    before_second = (len(runtime.executed), len(transport.hops))
    second_context = (
        replace(context, payload=b"conflicting-prefill-payload")
        if conflicting
        else context
    )
    second = router.receive_progressive_prefill(header, second_context)
    after_second = (len(runtime.executed), len(transport.hops))
    return first.disposition, second.disposition, before_second, after_second


def _prefill_trace_disagrees(trace: tuple[TraceAction, ...]) -> bool:
    names = tuple(action.name for action in trace)
    required = ("start_prefill", "deliver_prefill_a", "deliver_prefill_b")
    if names != required:
        return False
    _, disposition, _, _ = _progressive_prefill_replay(conflicting=True)
    return disposition != "REJECTED"


def test_exact_progressive_prefill_duplicate_is_cached_without_side_effects():
    first, duplicate, before, after = _progressive_prefill_replay(
        conflicting=False
    )

    assert first in {"FORWARDED", "LOCKED"}
    assert duplicate == first
    assert after == before


def test_stage_local_exact_progressive_prefill_duplicate_is_cached():
    first, duplicate, before, after = _progressive_prefill_replay(
        conflicting=False,
        runtime=StageLocalFakeRuntime(),
    )

    assert duplicate == first
    assert after == before


def test_counterexample_conflicting_progressive_prefill_payload_fails_closed():
    machine = reference_model()
    state = machine.apply(machine.initial_state(), ModelEvent("ADMIT")).state
    state = machine.apply(
        state,
        ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0),
    ).state
    first_event = ModelEvent(
        "PREFILL_HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=-1,
        peer="entry",
        hop_index=0,
        payload=(1,),
    )
    first = machine.apply(state, first_event)
    expected = machine.apply(first.state, replace(first_event, payload=(2,)))
    assert expected.code == "conflicting_duplicate"

    _, disposition, before_conflict, after_conflict = _progressive_prefill_replay(
        conflicting=True
    )
    if disposition != "REJECTED":
        trace = tuple(
            TraceAction(name)
            for name in (
                "start_prefill",
                "deliver_prefill_a",
                "deliver_prefill_b",
            )
        )
        minimal = minimize_trace(trace, _prefill_trace_disagrees)
        pytest.fail(
            "conflicting progressive-prefill replay returned cached success\n"
            f"minimal_trace={trace_to_json(minimal)}\n"
            f"observed_disposition={disposition}\n"
            f"side_effect_counts_before={before_conflict}\n"
            f"side_effect_counts_after={after_conflict}"
        )
    assert after_conflict == before_conflict


def _pending_trace_disagrees(trace: tuple[TraceAction, ...]) -> bool:
    names = tuple(action.name for action in trace)
    required = ("admit_and_register", "enqueue_activation_a", "enqueue_activation_b")
    if names != required:
        return False
    _, disposition, _, _, _ = _pending_hop_replay(b"activation-b")
    return disposition != "REJECTED"


def test_exact_pending_hop_duplicate_remains_single_queued_item():
    first, duplicate, reason, before, after = _pending_hop_replay(b"activation-a")

    assert first == "QUEUED"
    assert duplicate == "QUEUED"
    assert reason == "duplicate_pending"
    assert before == (1, 0)
    assert after == before


def test_pending_hop_snapshots_mutable_payload_before_execution():
    router, runtime, _, header = _registered_hop_fixture()
    payload = bytearray(b"activation-a")

    assert router.enqueue_hop(header, payload).disposition == "QUEUED"
    payload[:] = b"activation-b"
    completed = router.drain_ready_batches(force=True)

    assert completed
    assert runtime.executed_batches[0].items[0].payload == b"activation-a"


def test_stage_local_completed_enqueue_replay_returns_cached_result():
    router, runtime, _, header = _registered_hop_fixture(
        runtime=StageLocalFakeRuntime()
    )
    assert router.enqueue_hop(header, b"activation-a").disposition == "QUEUED"
    completed = router.drain_ready_batches(force=True)[0]
    batch_count = len(runtime.executed_batches)

    replay = router.enqueue_hop(header, b"activation-a")

    assert replay == completed
    assert router.pending_batch_hops() == 0
    assert len(runtime.executed_batches) == batch_count


def test_counterexample_conflicting_pending_hop_payload_fails_closed():
    machine = reference_model()
    state = run_reference_trace(machine, (TraceAction("admit"),)).final_state
    first_event = ModelEvent(
        "ENQUEUE_HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=0,
        peer="entry",
        hop_index=0,
        payload=(1,),
    )
    first = machine.apply(state, first_event)
    expected = machine.apply(first.state, replace(first_event, payload=(2,)))
    assert expected.code == "conflicting_duplicate"

    _, disposition, reason, before_conflict, after_conflict = _pending_hop_replay(
        b"activation-b"
    )
    if disposition != "REJECTED":
        trace = tuple(
            TraceAction(name)
            for name in (
                "admit_and_register",
                "enqueue_activation_a",
                "enqueue_activation_b",
            )
        )
        minimal = minimize_trace(trace, _pending_trace_disagrees)
        pytest.fail(
            "conflicting pending-hop replay returned duplicate-pending success\n"
            f"minimal_trace={trace_to_json(minimal)}\n"
            f"observed_disposition={disposition}\n"
            f"observed_reason={reason}\n"
            f"pending_counts_before={before_conflict}\n"
            f"pending_counts_after={after_conflict}"
        )
    assert after_conflict == before_conflict
