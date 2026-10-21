# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic A6 integration tests for M16-owned successor path authority.

These tests exercise only route identity, reservation, fencing, and cutover.
They do not claim physical recovery or browser evidence.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_a6_gateway import CommittedDeliverySnapshot, freeze_replay_checkpoint
from mycelium_a6_m16 import M16RecoveryAuthority, recovery_successor_binding
from mycelium_a6_recovery import (
    build_recovery_qualification,
    _prefix_digest,
    recovery_request_binding,
)
from mycelium_m16_runtime import M16AdmissionError, M16RuntimeCoordinator
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    Stage,
    StageCost,
)
from mycelium_router.fakes import ManualClock, SequenceIdSource


def _placement(identifier: str, node_id: str) -> Placement:
    return Placement(
        placement_id=identifier,
        node_id=node_id,
        replica_group_id=f"group-{identifier}",
        assignment_id=f"assignment-{identifier}",
        stage_signature=f"signature-{identifier}",
        load_proof_digest="sha256:" + identifier[-1] * 64,
        runtime_backend="mlx",
        runtime_endpoint=f"private-{identifier}",
    )


def _graph() -> ExecutionGraph:
    a = _placement("placement-a", "node-a")
    b = _placement("placement-b", "node-b")
    c = _placement("placement-c", "node-c")
    d = _placement("placement-d", "node-d")
    e = _placement("placement-e", "node-e")
    f = _placement("placement-f", "node-f")
    return ExecutionGraph(
        deployment_id="deployment-a6",
        deployment_epoch=16,
        topology_version=7,
        model_id="org/model",
        resolved_commit="a" * 40,
        manifest_digest="sha256:" + "f" * 64,
        entry_stage_id="stage-entry",
        final_stage_id="stage-output",
        hidden_size=256,
        activation_bytes=512,
        token_envelope_bytes=8,
        stages=(
            Stage(
                "stage-entry",
                LayerRange(0, 4, 4),
                ("input_embedding", "decoder"),
                StageCost(1.0, 1.0, 64),
                (a, c, e),
            ),
            Stage(
                "stage-output",
                LayerRange(4, 8, 4),
                ("decoder", "lm_head"),
                StageCost(1.0, 1.0, 64),
                (b, d, f),
            ),
        ),
        edges=(
            PlacementEdge("edge-ab", "placement-a", "placement-b", "link-ab"),
            PlacementEdge("edge-cd", "placement-c", "placement-d", "link-cd"),
            PlacementEdge("edge-ef", "placement-e", "placement-f", "link-ef"),
        ),
        loopback_edges=(
            PlacementEdge("edge-ba", "placement-b", "placement-a", "link-ba"),
            PlacementEdge("edge-dc", "placement-d", "placement-c", "link-dc"),
            PlacementEdge("edge-fe", "placement-f", "placement-e", "link-fe"),
        ),
    )


def _request(identifier: str = "request-a6") -> RequestContext:
    return RequestContext(
        request_id=identifier,
        prompt_token_ids=(7, 8, 9),
        max_new_tokens=8,
        expected_new_tokens=8,
        qos_class="interactive",
        admitted_at=100.0,
        target_ttft_ms=5_000.0,
        target_tpot_ms=2_000.0,
        target_tokens_per_second=0.5,
        sampling_seed=0,
        generation_config_digest="sha256:" + "4" * 64,
    )


def _recovery_request(source: RequestContext) -> RequestContext:
    committed_prefix = (101, 102)
    return replace(
        source,
        prompt_token_ids=source.prompt_token_ids + committed_prefix,
        max_new_tokens=source.max_new_tokens - len(committed_prefix),
        expected_new_tokens=source.expected_new_tokens - len(committed_prefix),
    )


def _coordinator(graph: ExecutionGraph | None = None) -> M16RuntimeCoordinator:
    graph = _graph() if graph is None else graph
    node_ids = {
        placement.node_id
        for stage in graph.stages
        for placement in stage.placements
    }
    states = {
        node_id: DeviceState(
            node_id,
            1,
            100.0,
            "ALIVE",
            1_000.0,
            1.0,
            1_000_000,
            0,
            {},
            {},
        )
        for node_id in node_ids
    }
    placement_ids = {
        placement.placement_id
        for stage in graph.stages
        for placement in stage.placements
    }
    capacities = {
        placement_id: {
            "memory_capacity_bytes": 1_000_000,
            "kv_capacity_bytes": 100_000,
            "workspace_capacity_bytes": 100_000,
            "maximum_reservations": 4,
        }
        for placement_id in placement_ids
    }
    return M16RuntimeCoordinator(
        graph=graph,
        device_states=states,
        placement_capacities=capacities,
        workload_profiles={"interactive_chat_v1": "interactive"},
        clock=ManualClock(100.0),
        id_source=SequenceIdSource(),
        config=RouterConfig(reservation_lease_seconds=300.0),
    )


def _source_and_exclusions(coordinator: M16RuntimeCoordinator):
    request = _request()
    source_manifest = coordinator.admit(
        request,
        workload_profile_id="interactive_chat_v1",
    )
    assert coordinator.next_dispatch() == request.request_id
    source_identity = coordinator.route_identity(request.request_id)
    excluded = frozenset(hop.placement_id for hop in source_manifest.ordered_hops)
    return request, source_manifest, source_identity, excluded


def test_reserve_successor_keeps_source_current_and_uses_new_immutable_path() -> None:
    coordinator = _coordinator()
    request, source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )

    successor = coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )
    successor_identity = coordinator.recovery_successor_identity(request.request_id)

    assert successor.path_id != source_manifest.path_id
    assert successor.path_attempt == source_manifest.path_attempt + 1
    assert {hop.placement_id for hop in successor.ordered_hops} == {
        "placement-c",
        "placement-d",
    }
    assert coordinator.route_identity(request.request_id) == source_identity
    assert coordinator.recovery_successor_request(request.request_id) == (
        _recovery_request(request)
    )
    assert successor_identity["request_attempt"] == 2
    assert successor_identity["path_id"] == successor.path_id
    assert successor_identity["path_manifest_digest"].startswith("sha256:")
    assert coordinator.authorizes_route_identity(request.request_id, source_identity)
    assert not coordinator.authorizes_route_identity(
        request.request_id,
        successor_identity,
    )


def test_successor_reservation_rejects_stale_identity_and_missing_exclusions() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )

    stale = dict(source_identity)
    stale["request_attempt"] = 99
    with pytest.raises(M16AdmissionError, match="stale_source_identity"):
        coordinator.reserve_recovery_successor(
            request.request_id,
            recovery_request=_recovery_request(request),
            expected_source_identity=stale,
            excluded_placements=excluded,
        )
    with pytest.raises(M16AdmissionError, match="recovery_exclusions_required"):
        coordinator.reserve_recovery_successor(
            request.request_id,
            recovery_request=_recovery_request(request),
            expected_source_identity=source_identity,
            excluded_placements=frozenset(),
        )
    with pytest.raises(M16AdmissionError, match="successor_not_reserved"):
        coordinator.recovery_successor_identity(request.request_id)

    status = coordinator.status()
    projected = next(
        item for item in status["requests"] if item["request_id"] == request.request_id
    )
    assert projected["reservation_count"] == 2


def test_fence_then_cutover_authorizes_exactly_one_attempt_and_releases_source() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )
    successor_identity = coordinator.recovery_successor_identity(request.request_id)

    with pytest.raises(M16AdmissionError, match="source_attempt_not_fenced"):
        coordinator.cutover_recovery_successor(
            request.request_id,
            expected_source_identity=source_identity,
            expected_successor_identity=successor_identity,
        )

    coordinator.fence_recovery_source(
        request.request_id,
        expected_source_identity=source_identity,
    )
    assert not coordinator.authorizes_route_identity(request.request_id, source_identity)
    assert not coordinator.authorizes_route_identity(
        request.request_id,
        successor_identity,
    )

    stale_successor = dict(successor_identity)
    stale_successor["path_attempt"] += 1
    with pytest.raises(M16AdmissionError, match="stale_successor_identity"):
        coordinator.cutover_recovery_successor(
            request.request_id,
            expected_source_identity=source_identity,
            expected_successor_identity=stale_successor,
        )

    committed = coordinator.cutover_recovery_successor(
        request.request_id,
        expected_source_identity=source_identity,
        expected_successor_identity=successor_identity,
    )
    assert committed == successor_identity
    assert coordinator.route_identity(request.request_id) == successor_identity
    assert not coordinator.authorizes_route_identity(request.request_id, source_identity)
    assert coordinator.authorizes_route_identity(
        request.request_id,
        successor_identity,
    )

    projected = next(
        item
        for item in coordinator.status()["requests"]
        if item["request_id"] == request.request_id
    )
    assert projected["reservation_count"] == 2


def test_discard_successor_releases_only_candidate_and_preserves_source() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )

    assert coordinator.discard_recovery_successor(
        request.request_id,
        expected_source_identity=source_identity,
    )
    assert coordinator.route_identity(request.request_id) == source_identity
    assert coordinator.authorizes_route_identity(request.request_id, source_identity)
    with pytest.raises(M16AdmissionError, match="successor_not_reserved"):
        coordinator.recovery_successor_identity(request.request_id)

    projected = next(
        item
        for item in coordinator.status()["requests"]
        if item["request_id"] == request.request_id
    )
    assert projected["reservation_count"] == 2


def test_fenced_source_can_replace_failed_successor_with_fresh_attempt() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    first_manifest = coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )
    first_identity = coordinator.recovery_successor_identity(request.request_id)
    coordinator.fence_recovery_source(
        request.request_id,
        expected_source_identity=source_identity,
    )
    assert coordinator.discard_recovery_successor(
        request.request_id,
        expected_source_identity=source_identity,
    )
    assert not coordinator.authorizes_route_identity(request.request_id, source_identity)

    failed_placements = frozenset(
        hop.placement_id for hop in first_manifest.ordered_hops
    )
    coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded | failed_placements,
    )
    second_identity = coordinator.recovery_successor_identity(request.request_id)
    assert first_identity["request_attempt"] == source_identity["request_attempt"] + 1
    assert second_identity["request_attempt"] == source_identity["request_attempt"] + 1
    assert second_identity["path_attempt"] == first_identity["path_attempt"] + 1
    assert second_identity["path_id"] != first_identity["path_id"]


def _qualified_recovery_inputs(
    coordinator: M16RuntimeCoordinator,
    request: RequestContext,
    source_identity: dict[str, object],
    *,
    physical_challenge: str = "passed",
):
    deployment = coordinator.recovery_deployment_binding()
    snapshot = CommittedDeliverySnapshot(
        request_id=request.request_id,
        encoded_prompt_token_ids=request.prompt_token_ids,
        generation_settings={
            "protocol": "mycelium.request_gateway.v1",
            "max_new_tokens": request.max_new_tokens,
            "workload_profile_id": None,
            "qos_class": None,
        },
        committed_token_ids=(101, 102),
        delivery_cursor=3,
        publisher_generation=1,
        session_id="session-a6",
        source_attempt=source_identity["request_attempt"],
        path_id=source_identity["path_id"],
        path_digest=source_identity["path_manifest_digest"],
        path_attempt=source_identity["path_attempt"],
        deployment_id=source_identity["deployment_id"],
        deployment_epoch=source_identity["deployment_epoch"],
        topology_generation=source_identity["topology_generation"],
        source_qualification_digest="sha256:" + "0" * 64,
        model_id=deployment["model_id"],
        representation_digest=deployment["representation_digest"],
        tokenizer_digest="sha256:" + "9" * 64,
    )
    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="a6-m16-store",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    source_compatibility = {
        "model_id": deployment["model_id"],
        "model_revision": deployment["model_revision"],
        "representation_digest": deployment["representation_digest"],
        "manifest_digest": deployment["representation_digest"],
        "tokenizer_digest": checkpoint.tokenizer_digest,
        "vocabulary_digest": "sha256:" + "1" * 64,
        "special_token_policy_digest": "sha256:" + "2" * 64,
        "architecture_digest": "sha256:" + "3" * 64,
        "context_policy_digest": "sha256:" + "4" * 64,
        "output_semantics_digest": "sha256:" + "5" * 64,
        "generation_settings_digest": checkpoint.generation_settings_digest,
        "qualification_parity_policy_digest": "sha256:" + "6" * 64,
        "workload_envelope_digest": "sha256:" + "7" * 64,
    }
    qualification = build_recovery_qualification(
        deployment,
        qualification_id="qual-a6-successor",
        request_binding=recovery_request_binding(checkpoint),
        successor_binding={
            "request_id": request.request_id,
            "request_attempt": 2,
            "path_id": "synthetic-pre-reservation-path",
            "path_attempt": 1,
            "path_manifest_digest": "sha256:" + "8" * 64,
            "placement_ids": ["synthetic-placement"],
        },
        compatibility=source_compatibility,
        compatible=True,
        issued_at_unix_ms=1_100,
        expiry_unix_ms=1_900_000,
        physical_challenge=physical_challenge,
    )
    return checkpoint, source_compatibility, qualification


def _qualification_for_reserved_successor(
    coordinator,
    checkpoint,
    source_compatibility,
    manifest,
    *,
    qualification_id="qual-a6-reserved-successor",
):
    return build_recovery_qualification(
        coordinator.recovery_deployment_binding(),
        qualification_id=qualification_id,
        request_binding=recovery_request_binding(checkpoint),
        successor_binding=recovery_successor_binding(
            coordinator.recovery_successor_identity(checkpoint.request_id),
            manifest,
        ),
        compatibility=source_compatibility,
        compatible=True,
        issued_at_unix_ms=1_100,
        expiry_unix_ms=1_900_000,
        physical_challenge="passed",  # synthetic deterministic test input only
    )


def test_qualified_authority_accepts_zero_token_prefix_replay() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(coordinator)
    checkpoint, source_compatibility, _qualification = _qualified_recovery_inputs(
        coordinator, request, source_identity
    )
    checkpoint = replace(
        checkpoint,
        committed_prefix_tokens=(),
        committed_token_count=0,
        committed_prefix_digest=_prefix_digest(()),
        watermark=0,
    )
    authority = M16RecoveryAuthority(coordinator)
    successor = authority.reserve_candidate(
        checkpoint=checkpoint,
        source_request=request,
        expected_source_identity=source_identity,
        excluded_placements=excluded,
        now_unix_ms=1_200,
    )
    assert coordinator.recovery_successor_request(request.request_id).prompt_token_ids == request.prompt_token_ids
    assert coordinator.recovery_successor_request(request.request_id).max_new_tokens == request.max_new_tokens
    assert successor.path_id != source_identity["path_id"]


def test_qualified_authority_reserves_exact_checkpoint_replay_context() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    checkpoint, source_compatibility, _qualification = _qualified_recovery_inputs(
        coordinator,
        request,
        source_identity,
    )

    authority = M16RecoveryAuthority(coordinator)
    successor = authority.reserve_candidate(
        checkpoint=checkpoint,
        source_request=request,
        expected_source_identity=source_identity,
        excluded_placements=excluded,
        now_unix_ms=1_200,
    )
    qualification = _qualification_for_reserved_successor(
        coordinator,
        checkpoint,
        source_compatibility,
        successor,
    )
    authority.qualify_reserved_successor(
        checkpoint=checkpoint,
        successor_manifest=successor,
        qualification=qualification,
        source_compatibility=source_compatibility,
        expected_source_identity=source_identity,
        now_unix_ms=1_200,
    )

    assert successor.path_attempt == 1
    assert coordinator.recovery_successor_identity(request.request_id)[
        "request_attempt"
    ] == 2


def test_unexecuted_physical_challenge_cannot_reserve_successor() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    checkpoint, source_compatibility, qualification = _qualified_recovery_inputs(
        coordinator,
        request,
        source_identity,
        physical_challenge="unexecuted",
    )

    with pytest.raises(
        M16AdmissionError,
        match="recovery_physical_qualification_required",
    ):
        M16RecoveryAuthority(coordinator).reserve_successor(
            checkpoint=checkpoint,
            qualification=qualification,
            source_compatibility=source_compatibility,
            source_request=request,
            expected_source_identity=source_identity,
            excluded_placements=excluded,
            now_unix_ms=1_200,
        )


def test_qualification_for_another_request_cannot_reserve_successor() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    checkpoint, source_compatibility, qualification = _qualified_recovery_inputs(
        coordinator,
        request,
        source_identity,
    )
    wrong_binding = dict(qualification["request_binding"])
    wrong_binding["request_id"] = "another-request"
    wrong_qualification = build_recovery_qualification(
        coordinator.recovery_deployment_binding(),
        qualification_id="qual-a6-wrong-request",
        request_binding=wrong_binding,
        successor_binding=qualification["successor_binding"],
        compatibility=source_compatibility,
        compatible=True,
        issued_at_unix_ms=1_100,
        expiry_unix_ms=1_900_000,
        physical_challenge="passed",  # synthetic deterministic test input only
    )

    with pytest.raises(M16AdmissionError, match="recovery_request_binding_mismatch"):
        M16RecoveryAuthority(coordinator).reserve_successor(
            checkpoint=checkpoint,
            qualification=wrong_qualification,
            source_compatibility=source_compatibility,
            source_request=request,
            expected_source_identity=source_identity,
            excluded_placements=excluded,
            now_unix_ms=1_200,
        )


def test_qualified_authority_rejects_immutable_mismatch_without_reservation() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    checkpoint, source_compatibility, _qualification = _qualified_recovery_inputs(
        coordinator,
        request,
        source_identity,
    )
    incompatible = dict(source_compatibility)
    incompatible["tokenizer_digest"] = "sha256:" + "8" * 64
    authority = M16RecoveryAuthority(coordinator)
    successor = authority.reserve_candidate(
        checkpoint=checkpoint,
        source_request=request,
        expected_source_identity=source_identity,
        excluded_placements=excluded,
        now_unix_ms=1_200,
    )
    qualification = build_recovery_qualification(
        coordinator.recovery_deployment_binding(),
        qualification_id="qual-a6-incompatible",
        request_binding=recovery_request_binding(checkpoint),
        successor_binding=recovery_successor_binding(
            coordinator.recovery_successor_identity(request.request_id),
            successor,
        ),
        compatibility=incompatible,
        compatible=True,
        issued_at_unix_ms=1_100,
        expiry_unix_ms=1_900_000,
        physical_challenge="passed",  # synthetic deterministic test input only
    )

    with pytest.raises(M16AdmissionError, match="recovery_compatibility_mismatch"):
        authority.qualify_reserved_successor(
            checkpoint=checkpoint,
            successor_manifest=successor,
            qualification=qualification,
            source_compatibility=source_compatibility,
            expected_source_identity=source_identity,
            now_unix_ms=1_200,
        )

    projected = next(
        item
        for item in coordinator.status()["requests"]
        if item["request_id"] == request.request_id
    )
    assert projected["reservation_count"] == 2


def test_fenced_successor_failure_aborts_and_releases_both_paths() -> None:
    coordinator = _coordinator()
    request, _source_manifest, source_identity, excluded = _source_and_exclusions(
        coordinator
    )
    coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )
    successor_identity = coordinator.recovery_successor_identity(request.request_id)
    coordinator.fence_recovery_source(
        request.request_id,
        expected_source_identity=source_identity,
    )

    assert coordinator.abort_recovery(
        request.request_id,
        expected_source_identity=source_identity,
        expected_successor_identity=successor_identity,
    )
    assert coordinator.phase(request.request_id) == "terminal"
    assert coordinator.terminal_state(request.request_id) == "failed"
    assert not coordinator.authorizes_route_identity(
        request.request_id,
        source_identity,
    )
    projected = next(
        item
        for item in coordinator.status()["requests"]
        if item["request_id"] == request.request_id
    )
    assert projected["reservation_count"] == 0
