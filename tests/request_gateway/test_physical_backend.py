"""Device-free tests for qualification-bound Router request admission.

All routes are local fakes. These tests make no physical-run claim; only a
qualifier-owned current record may open the qualification-gated request path.
"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from mycelium_qualification.evidence import sha256_bytes, sha256_document
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_router.live_ports import PublishedTopologyProvider
from mycelium_router.router import Router
from mycelium_router.serialization import execution_graph_to_dict
from test_backend_cli import (
    RecordingCodec,
    _runtime_stack,
    _synthetic_execution_graph,
)
from test_core import _synthetic_qualification


class CurrentAuthority:
    def __init__(self, record):
        self.record = record

    def current(self):
        return self.record


class RecordingRouter(Router):
    """Production Router facade with admission/cancellation observations only."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.admit_calls = 0
        self.cancel_calls = 0

    def admit(self, *args, **kwargs):
        self.admit_calls += 1
        return super().admit(*args, **kwargs)

    def cancel(self, request_id):
        self.cancel_calls += 1
        return super().cancel(request_id)


class CountingPublishedTopologyProvider(PublishedTopologyProvider):
    def __init__(self, graph):
        super().__init__(graph)
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return super().snapshot()


def submission(record, prompt="PROMPT-PRIVATE-3B4"):
    return InferenceSubmission(
        prompt=prompt,
        max_new_tokens=2,
        qualification=qualification_binding(record),
    )


def backend_stack(
    record=None,
    *,
    graph=None,
    excluded_placements=frozenset(),
):
    current = record or _synthetic_qualification()
    deployment = graph or _synthetic_execution_graph()
    router, clock, capacity, runtime = _runtime_stack(
        graph=deployment,
        router_type=RecordingRouter,
    )
    codec = RecordingCodec()
    authority = CurrentAuthority(current)
    backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock.now,
        qualification_source=authority,
        excluded_placements=excluded_placements,
    )
    return backend, authority, router, codec, capacity, runtime


def assert_zero_router_side_effects(router, codec, capacity, runtime):
    assert router.admit_calls == 0
    assert router.cancel_calls == 0
    assert codec.encoded == []
    assert capacity.requests == []
    assert capacity.committed_ids == set()
    assert capacity.release_calls == []
    assert runtime.executed == []
    assert runtime.executed_batches == []
    assert runtime.cancel_calls == []
    assert router.entry._requests == {}
    assert router.entry._pending_prefills == {}


def assert_rejected_without_side_effects(
    backend,
    accepted,
    router,
    codec,
    capacity,
    runtime,
    *,
    code="qualification_mismatch",
    request_id="physical-backend-reject",
):
    with pytest.raises(AdmissionError) as raised:
        backend.run(
            request_id,
            accepted,
            lambda *_: None,
            lambda: False,
        )
    assert raised.value.code == code
    assert_zero_router_side_effects(router, codec, capacity, runtime)


def test_exact_current_qualification_streams_prompt_through_router_only():
    record = _synthetic_qualification()
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)
    emitted = []

    assert (
        backend.run(
            "physical-backend-ok",
            submission(record),
            lambda i, t: emitted.append((i, t)),
            lambda: False,
        )
        == "completed"
    )

    assert router.admit_calls == 1
    assert codec.encoded == ["PROMPT-PRIVATE-3B4"]
    assert emitted == [(0, "<101>"), (1, "<102>")]
    assert len(capacity.release_calls) == 1
    assert len(runtime.cancel_calls) == 1


def test_qualified_policy_refusal_completes_without_router_admission():
    record = _synthetic_qualification()
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)
    codec.policy_response = lambda prompt: (
        "I can't assist with credential theft." if "phishing" in prompt else None
    )
    emitted = []

    assert backend.run(
        "physical-backend-policy-refusal",
        submission(record, prompt="phishing request"),
        lambda index, text: emitted.append((index, text)),
        lambda: False,
    ) == "completed"

    assert emitted == [(0, "I can't assist with credential theft.")]
    assert_zero_router_side_effects(router, codec, capacity, runtime)


@pytest.mark.parametrize("snapshot_shape", ["missing", "noncallable", "invalid"])
def test_mandatory_router_snapshot_fails_closed(
    monkeypatch,
    snapshot_shape,
):
    record = _synthetic_qualification()
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)
    accepted = submission(record)
    if snapshot_shape == "missing":
        monkeypatch.delattr(Router, "current_deployment", raising=False)
    elif snapshot_shape == "noncallable":
        monkeypatch.setattr(router, "current_deployment", None, raising=False)
    else:
        monkeypatch.setattr(
            router,
            "current_deployment",
            lambda: object(),
            raising=False,
        )

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        code="qualification_unavailable",
        request_id=f"physical-backend-snapshot-{snapshot_shape}",
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda record: object.__setattr__(record, "route_ready", False),
            "readiness_revoked",
        ),
        (
            lambda record: object.__setattr__(
                record, "deployment_id", "wrong-deployment"
            ),
            "qualification_mismatch",
        ),
        (
            lambda record: object.__setattr__(
                record,
                "placement_provenance",
                (
                    "frozen_fixture"
                    if record.placement_provenance == "planner_v2"
                    else "planner_v2"
                ),
            ),
            "qualification_mismatch",
        ),
        (
            lambda record: object.__setattr__(
                record,
                "source_provenance_digest",
                "sha256:" + "f" * 64,
            ),
            "qualification_mismatch",
        ),
    ],
)
def test_changed_current_authority_rejects_before_router_admission(mutate, code):
    record = _synthetic_qualification()
    backend, authority, router, codec, capacity, runtime = backend_stack(record)
    accepted = submission(record)
    mutate(authority.record)

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        code=code,
    )


def test_expired_authority_record_is_absent_and_rejects_before_prompt_encoding():
    record = _synthetic_qualification()
    backend, authority, router, codec, capacity, runtime = backend_stack(record)
    accepted = submission(record)
    authority.record = None

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        code="route_dropped",
        request_id="physical-backend-expired",
    )


def test_live_router_deployment_identity_must_match_current_authority():
    record = _synthetic_qualification()
    graph = _synthetic_execution_graph()
    backend, _authority, router, codec, capacity, runtime = backend_stack(
        record,
        graph=graph,
    )
    accepted = submission(record)
    router.entry.topology.set(replace(graph, deployment_id="different"))

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-deployment",
    )


def test_live_router_manifest_digest_mismatch_is_side_effect_free():
    record = _synthetic_qualification()
    graph = _synthetic_execution_graph()
    backend, _authority, router, codec, capacity, runtime = backend_stack(
        record,
        graph=graph,
    )
    accepted = submission(record)
    router.entry.topology.set(
        replace(graph, manifest_digest=sha256_bytes(b"different-manifest"))
    )

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-manifest",
    )


def test_live_router_canonical_execution_graph_digest_must_match_authority():
    record = _synthetic_qualification()
    object.__setattr__(
        record,
        "execution_graph_digest",
        sha256_bytes(b"different-execution-graph"),
    )
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-graph-digest",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stage_signature", sha256_bytes(b"different-stage-signature")),
        ("load_proof_digest", sha256_bytes(b"different-load-proof")),
        ("placement_id", "unknown-qualified-placement"),
        ("node_id", "wrong-qualified-node"),
        ("assignment_id", "wrong-qualified-assignment"),
    ],
)
def test_live_stage_binding_identity_mismatch_is_side_effect_free(field, value):
    record = _synthetic_qualification()
    bindings = list(record.stage_bindings)
    bindings[0] = replace(bindings[0], **{field: value})
    object.__setattr__(record, "stage_bindings", tuple(bindings))
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id=f"physical-backend-stage-{field}",
    )


@pytest.mark.parametrize(
    "projection_shape",
    ["missing_stage", "duplicate_stage", "duplicate_placement"],
)
def test_stage_binding_projection_requires_exact_complete_stage_coverage(
    projection_shape,
):
    record = _synthetic_qualification()
    if projection_shape == "missing_stage":
        bindings = record.stage_bindings[:-1]
    elif projection_shape == "duplicate_placement":
        bindings = (
            record.stage_bindings[0],
            replace(
                record.stage_bindings[1],
                placement_id=record.stage_bindings[0].placement_id,
            ),
        )
    else:
        bindings = (
            record.stage_bindings[0],
            replace(
                record.stage_bindings[1],
                stage_id=record.stage_bindings[0].stage_id,
            ),
        )
    object.__setattr__(record, "stage_bindings", bindings)
    backend, _authority, router, codec, capacity, runtime = backend_stack(record)

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id=f"physical-backend-projection-{projection_shape}",
    )


def test_inactive_qualified_placement_is_rejected_before_router_admission():
    record = _synthetic_qualification()
    graph = _synthetic_execution_graph()
    selected_id = record.stage_bindings[0].placement_id
    first_stage = graph.stages[0]
    inactive_stage = replace(
        first_stage,
        placements=tuple(
            replace(placement, lifecycle_state="RETIRING")
            if placement.placement_id == selected_id
            else placement
            for placement in first_stage.placements
        ),
    )
    inactive_graph = graph.with_stages((inactive_stage, *graph.stages[1:]))
    object.__setattr__(
        record,
        "execution_graph_digest",
        sha256_document(execution_graph_to_dict(inactive_graph)),
    )
    backend, _authority, router, codec, capacity, runtime = backend_stack(
        record,
        graph=inactive_graph,
    )

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-inactive-placement",
    )


def graph_with_unqualified_alternative(record):
    graph = _synthetic_execution_graph()
    first_stage = graph.stages[0]
    selected = first_stage.placements[0]
    alternative = replace(
        selected,
        placement_id="000-unqualified-stage-000",
        assignment_id="unqualified-assignment-stage-000",
        load_proof_digest=sha256_bytes(b"unqualified-load-proof"),
    )
    expanded_stage = replace(
        first_stage,
        placements=(alternative, *first_stage.placements),
    )
    forward = graph.edges[0]
    loopback = graph.loopback_edges[0]
    expanded = replace(
        graph,
        stages=(expanded_stage, *graph.stages[1:]),
        edges=(
            replace(
                forward,
                edge_id="unqualified-forward-edge",
                from_placement_id=alternative.placement_id,
            ),
            *graph.edges,
        ),
        loopback_edges=(
            replace(
                loopback,
                edge_id="unqualified-loopback-edge",
                to_placement_id=alternative.placement_id,
            ),
            *graph.loopback_edges,
        ),
    )
    object.__setattr__(
        record,
        "execution_graph_digest",
        sha256_document(execution_graph_to_dict(expanded)),
    )
    return expanded, alternative.placement_id


def successor_with_unqualified_alternative(graph):
    first_stage = graph.stages[0]
    selected = first_stage.placements[0]
    alternative = replace(
        selected,
        placement_id="000-unqualified-stage-000",
        assignment_id="unqualified-assignment-stage-000",
        load_proof_digest=sha256_bytes(b"successor-unqualified-load-proof"),
    )
    forward = graph.edges[0]
    loopback = graph.loopback_edges[0]
    successor = replace(
        graph,
        topology_version=graph.topology_version + 1,
        stages=(
            replace(
                first_stage,
                placements=(alternative, *first_stage.placements),
            ),
            *graph.stages[1:],
        ),
        edges=(
            replace(
                forward,
                edge_id="successor-unqualified-forward-edge",
                from_placement_id=alternative.placement_id,
            ),
            *graph.edges,
        ),
        loopback_edges=(
            replace(
                loopback,
                edge_id="successor-unqualified-loopback-edge",
                to_placement_id=alternative.placement_id,
            ),
            *graph.loopback_edges,
        ),
    )
    return successor, alternative.placement_id


def test_gateway_pins_the_single_validated_published_topology_snapshot():
    record = _synthetic_qualification()
    original = _synthetic_execution_graph()
    successor, unqualified_id = successor_with_unqualified_alternative(original)
    router, clock, capacity, runtime = _runtime_stack(
        graph=original,
        router_type=RecordingRouter,
    )
    topology = CountingPublishedTopologyProvider(original)
    router.entry.topology = topology

    class PublishDuringEncode(RecordingCodec):
        def encode(self, prompt):
            topology.publish(successor)
            return super().encode(prompt)

    codec = PublishDuringEncode()
    backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock.now,
        qualification_source=CurrentAuthority(record),
    )

    assert (
        backend.run(
            "physical-backend-published-topology-race",
            submission(record),
            lambda *_: None,
            lambda: False,
        )
        == "completed"
    )

    qualified = tuple(
        binding.placement_id for binding in record.stage_bindings
    )
    admitted = router.get_request(
        "physical-backend-published-topology-race"
    )
    actual_route = tuple(
        hop.placement_id for hop in admitted.manifest.ordered_hops
    )
    reserved = tuple(item.placement_id for item in capacity.requests)
    executed = tuple(item.placement_id for item in runtime.executed)
    encoded_path = tuple(
        delta.hop.placement_id
        for delta in router.entry.transport.manifest_deltas
    )

    assert topology.snapshot_calls == 1
    assert admitted.graph == original
    assert admitted.manifest.topology_version == original.topology_version
    assert admitted.manifest.manifest_digest == original.manifest_digest
    assert actual_route == qualified
    assert reserved == qualified
    assert set(executed) == set(qualified)
    assert unqualified_id not in actual_route
    assert unqualified_id not in reserved
    assert unqualified_id not in executed
    assert unqualified_id not in encoded_path


def test_router_admission_is_constrained_to_qualified_stage_placements():
    record = _synthetic_qualification()
    graph, alternative_id = graph_with_unqualified_alternative(record)
    backend, _authority, router, codec, capacity, _runtime = backend_stack(
        record,
        graph=graph,
    )

    assert (
        backend.run(
            "physical-backend-qualified-path",
            submission(record),
            lambda *_: None,
            lambda: False,
        )
        == "completed"
    )

    selected = tuple(binding.placement_id for binding in record.stage_bindings)
    admitted = tuple(
        hop.placement_id
        for hop in router.get_request(
            "physical-backend-qualified-path"
        ).manifest.ordered_hops
    )
    assert admitted == selected
    assert alternative_id not in admitted
    assert tuple(item.placement_id for item in capacity.requests) == selected
    assert codec.encoded == ["PROMPT-PRIVATE-3B4"]


@pytest.mark.parametrize("broken_constraint", ["forward_edge", "loopback"])
def test_qualified_placement_projection_must_form_a_live_complete_path(
    broken_constraint,
):
    record = _synthetic_qualification()
    graph, _alternative_id = graph_with_unqualified_alternative(record)
    selected = record.stage_bindings[0].placement_id
    if broken_constraint == "forward_edge":
        graph = replace(
            graph,
            edges=tuple(
                edge
                for edge in graph.edges
                if edge.from_placement_id != selected
            ),
        )
    else:
        graph = replace(
            graph,
            loopback_edges=tuple(
                edge
                for edge in graph.loopback_edges
                if edge.to_placement_id != selected
            ),
        )
    object.__setattr__(
        record,
        "execution_graph_digest",
        sha256_document(execution_graph_to_dict(graph)),
    )
    backend, _authority, router, codec, capacity, runtime = backend_stack(
        record,
        graph=graph,
    )

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id=f"physical-backend-path-{broken_constraint}",
    )


def test_caller_cannot_exclude_a_required_qualified_placement():
    record = _synthetic_qualification()
    required = record.stage_bindings[0].placement_id
    backend, _authority, router, codec, capacity, runtime = backend_stack(
        record,
        excluded_placements=frozenset({required}),
    )

    assert_rejected_without_side_effects(
        backend,
        submission(record),
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-required-exclusion",
    )


def test_malformed_current_record_fails_closed_before_prompt_encoding():
    record = _synthetic_qualification()
    backend, authority, router, codec, capacity, runtime = backend_stack(record)
    accepted = submission(record)
    object.__setattr__(record, "source_provenance_digest", "not-a-digest")

    assert authority.record is record
    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        code="qualification_unavailable",
        request_id="physical-backend-malformed",
    )


def test_post_rejection_cleanup_does_not_cancel_router_or_leave_pending_id():
    record = _synthetic_qualification()
    backend, authority, router, codec, capacity, runtime = backend_stack(record)
    accepted = submission(record)
    object.__setattr__(
        authority.record,
        "deployment_id",
        "changed-before-encoding",
    )

    assert_rejected_without_side_effects(
        backend,
        accepted,
        router,
        codec,
        capacity,
        runtime,
        request_id="physical-backend-rejected-cleanup",
    )
    backend.cancel("physical-backend-rejected-cleanup")

    assert_zero_router_side_effects(router, codec, capacity, runtime)
    assert backend._pending_cancelled == set()
    assert backend._awaiting_cancel_ack == set()


class ReusableRouter:
    def __init__(self, deployment):
        self.deployment = deployment
        self.admit_calls = 0
        self.cancel_calls = 0
        self.status = "UNKNOWN"

    def current_deployment(self):
        return self.deployment

    def admit(self, request, client_sink, **kwargs):
        del client_sink, kwargs
        self.admit_calls += 1
        self.status = "DECODING"
        return request.request_id

    def decode_one(self, request_id):
        del request_id
        self.status = "COMPLETED"
        return True

    def request_status(self, request_id):
        del request_id
        return self.status

    def cancel(self, request_id):
        del request_id
        self.cancel_calls += 1
        self.status = "CANCELLED"
        return True


class DefaultSnapshotRouter:
    def __init__(self):
        self.status = "UNKNOWN"
        self.excluded_placements = None

    def admit(self, request, client_sink, *, excluded_placements):
        del client_sink
        self.excluded_placements = excluded_placements
        self.status = "DECODING"
        return request.request_id

    def decode_one(self, request_id):
        del request_id
        self.status = "COMPLETED"
        return True

    def request_status(self, request_id):
        del request_id
        return self.status

    def cancel(self, request_id):
        del request_id
        self.status = "CANCELLED"
        return True


class CleanupGatedRouter(ReusableRouter):
    def __init__(self, deployment):
        super().__init__(deployment)
        self.cancel_requested = threading.Event()
        self.cleanup_complete = threading.Event()
        self.deadlines = []

    def cancel_with_deadline(self, request_id, *, deadline_monotonic_s):
        del request_id
        self.cancel_calls += 1
        self.deadlines.append(deadline_monotonic_s)
        self.cancel_requested.set()
        return True

    def request_status(self, request_id):
        del request_id
        if self.cleanup_complete.is_set():
            return "CANCELLED"
        return self.status

    def decode_one(self, request_id):
        del request_id
        self.cleanup_complete.wait(timeout=1.0)
        return False


def test_ungated_compatibility_caller_retains_default_snapshot_admission():
    graph = _synthetic_execution_graph()
    _router, clock, _capacity, _runtime = _runtime_stack(graph=graph)
    router = DefaultSnapshotRouter()
    excluded = frozenset({"caller-excluded-placement"})
    backend = RouterSessionBackend(
        router=router,
        codec=RecordingCodec(),
        clock=clock.now,
        excluded_placements=excluded,
    )

    assert (
        backend.run(
            "default-snapshot-compatibility",
            InferenceSubmission(
                prompt="default snapshot",
                max_new_tokens=1,
                qualification=qualification_binding(
                    _synthetic_qualification()
                ),
            ),
            lambda *_: None,
            lambda: False,
        )
        == "completed"
    )
    assert router.excluded_placements == excluded


def test_cancel_state_is_request_local_and_does_not_poison_reused_id():
    record = _synthetic_qualification()
    graph = _synthetic_execution_graph()
    _router, clock, _capacity, _runtime = _runtime_stack(graph=graph)
    router = ReusableRouter(graph)
    backend = RouterSessionBackend(
        router=router,
        codec=RecordingCodec(),
        clock=clock.now,
        qualification_source=CurrentAuthority(record),
    )
    checks = iter((False, False, True))

    assert (
        backend.run(
            "reused-request-id",
            submission(record),
            lambda *_: None,
            lambda: next(checks, True),
        )
        == "cancelled"
    )
    backend.cancel("reused-request-id")
    assert (
        backend.run(
            "reused-request-id",
            submission(record),
            lambda *_: None,
            lambda: False,
        )
        == "completed"
    )

    assert router.admit_calls == 2
    assert router.cancel_calls == 1
    assert backend._cancelled == set()
    assert backend._pending_cancelled == set()
    assert backend._internally_cancelled == set()
    assert backend._external_cancellation_observed == set()
    assert backend._awaiting_cancel_ack == set()


def test_backend_waits_for_router_cleanup_terminal_and_preserves_deadline():
    record = _synthetic_qualification()
    graph = _synthetic_execution_graph()
    _router, clock, _capacity, _runtime = _runtime_stack(graph=graph)
    router = CleanupGatedRouter(graph)
    backend = RouterSessionBackend(
        router=router,
        codec=RecordingCodec(),
        clock=clock.now,
        qualification_source=CurrentAuthority(record),
    )
    stop = threading.Event()
    outcome = []

    worker = threading.Thread(
        target=lambda: outcome.append(
            backend.run(
                "cleanup-gated-cancel",
                submission(record),
                lambda *_: None,
                stop.is_set,
            )
        )
    )
    worker.start()
    while router.status != "DECODING":
        worker.join(timeout=0.001)
        assert worker.is_alive()

    backend.cancel_with_deadline(
        "cleanup-gated-cancel",
        deadline_monotonic_s=123.5,
    )
    stop.set()
    assert router.cancel_requested.wait(timeout=1.0)
    assert worker.is_alive()
    assert outcome == []

    router.cleanup_complete.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert outcome == ["cancelled"]
    assert router.deadlines == [123.5]


def test_client_cancellation_reaches_router_once_and_private_material_stays_out_of_error(
    caplog,
):
    record = _synthetic_qualification()
    backend, _authority, router, _codec, _capacity, _runtime = backend_stack(
        record
    )
    private = "PROMPT-PRIVATE-CANCEL-3B4"
    checks = iter((False, False, True))

    assert (
        backend.run(
            "physical-backend-cancel",
            submission(record, private),
            lambda *_: None,
            lambda: next(checks, True),
        )
        == "cancelled"
    )
    backend.cancel("physical-backend-cancel")

    assert router.cancel_calls == 1
    assert backend._cancelled == set()
    assert backend._pending_cancelled == set()
    assert backend._internally_cancelled == set()
    assert backend._external_cancellation_observed == set()
    assert backend._awaiting_cancel_ack == set()
    assert private not in caplog.text
    assert private not in repr(backend.__dict__)
