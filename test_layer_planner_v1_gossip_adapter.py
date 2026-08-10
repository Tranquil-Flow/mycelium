from __future__ import annotations

import copy
from typing import Any

import pytest

from mycelium_capacity_profiles import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    compile_capacity_profile,
    placement_calibration_digest,
    status_with_capacity_profile,
)
from mycelium_gossip.evidence_bundle import evidence_bundle_to_dict
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.signed_bundle import seal_evidence_bundle
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    plan_signed_evidence,
    planner_snapshot_digest,
    planner_snapshot_from_evidence_bundle,
    planner_snapshot_from_signed_evidence,
)
from mycelium_qualification.signing import generate_ed25519_signer
from tests.gossip.helpers import link_payload, make_record, profile_payload, status_payload

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
MODEL_SCOPE = {
    "model_id": "org/model",
    "num_layers": 4,
    "manifest_digest": "sha256:" + "a" * 64,
    "resolved_commit": "b" * 40,
}
PLANNER_MODEL = {
    "model_id": "org/model",
    "revision": "b" * 40,
    "weight_digest": "sha256:" + "a" * 64,
    "architecture": "Decoder",
    "num_layers": 4,
    "hidden_size": 128,
    "dtype_bytes": 2,
    "kv_heads": 2,
    "head_dim": 32,
    "weight_bytes": 60_000_000,
}
WORKLOAD = {
    "preset": "interactive_chat_v1",
    "concurrency_points": [1],
    "user_scale": 1,
    "context_bucket": "interactive-4k",
}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 0,
    "ttft_slo_ms": 1_000_000,
    "tpot_slo_ms": 1_000_000,
}
PERFORMANCE = {
    "backend": "mlx",
    "decode_mode": "stage_local_kv",
    "prefill_ms_per_layer_token": 0.001,
    "decode_ms_per_layer_token": 0.001,
    "memory_bandwidth_Bps": 1_000_000_000,
    "spill_bandwidth_Bps": 1_000_000_000,
    "calibration_confidence": 0.9,
    "runtime_build": "mlx-1",
    "hardware_class": "apple-silicon",
    "power_mode": "ac",
}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _service(
    *,
    missing_performance: str | None = None,
    zero_memory: str | None = None,
    reachable: bool = True,
    missing_link: tuple[str, str] | None = None,
    performance_by_node: dict[str, dict[str, Any]] | None = None,
):
    clock = Clock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    service = GossipService(
        swarm_id="swarm-a",
        node_id="local",
        incarnation=1,
        boot_id="boot-local-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=clock), "local"),
        registry=store,
        monotonic=clock,
    )
    capacity_profiles = {}
    for node_id in ("node-a", "node-b"):
        device_profile = profile_payload(node_id)
        status = status_payload(node_id, free_bytes=0 if zero_memory == node_id else 200_000_000)
        if missing_performance != node_id:
            status["performance"] = copy.deepcopy(
                (performance_by_node or {}).get(node_id, PERFORMANCE)
            )
            capacity_profile = compile_capacity_profile(
                CapacityProfileKey(
                    model_digest=MODEL_SCOPE["manifest_digest"],
                    source_evidence_digest=placement_calibration_digest(
                        node_id, status["performance"]
                    ),
                    quantization="none",
                    backend=status["performance"]["backend"],
                    runtime_build=status["performance"]["runtime_build"],
                    hardware_class=status["performance"]["hardware_class"],
                    power_mode=status["performance"]["power_mode"],
                    context_bucket=WORKLOAD["context_bucket"],
                    kv_mode=status["performance"]["decode_mode"],
                ),
                (
                    CapacityObservation(
                        concurrency=1,
                        sample_count=3,
                        p95_ttft_ms=10.0,
                        p95_tpot_ms=5.0,
                        aggregate_output_tps=2.0,
                        peak_memory_bytes=10_000_000,
                        memory_budget_bytes=200_000_000,
                    ),
                ),
                CapacityProfilePolicy(
                    ttft_p95_slo_ms=1_000.0,
                    tpot_p95_slo_ms=1_000.0,
                    min_samples=3,
                ),
            )
            status = status_with_capacity_profile(
                status,
                capacity_profile,
                allow_concurrency_limit_update=True,
            )
            capacity_profiles[node_id] = capacity_profile
        store.apply(
            make_record(
                RecordKind.PROFILE,
                node_id=node_id,
                ttl_ms=10_000,
                payload=device_profile,
            )
        )
        store.apply(make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000, payload=status))
        store.apply(
            make_record(
                RecordKind.MEMBERSHIP,
                node_id=node_id,
                ttl_ms=10_000,
            )
        )
        service.submit_liveness(
            LivenessEvent(
                LivenessKind.PUT,
                "swarm-a",
                node_id,
                1,
                f"boot-{node_id}-1",
                clock(),
            )
        )
    for src, dst in (("node-a", "node-b"), ("node-b", "node-a")):
        if missing_link == (src, dst):
            continue
        store.apply(
            make_record(
                RecordKind.LINK,
                node_id=src,
                ttl_ms=10_000,
                payload=link_payload(src, dst, reachable=reachable),
            )
        )
    service.drain()
    service._m13_capacity_profiles = capacity_profiles
    return service


def _bundle(**service_kwargs):
    return _service(**service_kwargs).capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=3,
        **MODEL_SCOPE,
    )


def _snapshot(bundle=None, *, model=None, policy=None):
    return planner_snapshot_from_evidence_bundle(
        _bundle() if bundle is None else bundle,
        model=PLANNER_MODEL if model is None else model,
        workload=WORKLOAD,
        policy=POLICY if policy is None else policy,
    )


def _signed(service=None, *, capacity_profiles=None):
    selected = _service() if service is None else service
    bundle = selected.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=3,
        **MODEL_SCOPE,
    )
    profiles = (
        selected._m13_capacity_profiles
        if capacity_profiles is None
        else capacity_profiles
    )
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    signed = seal_evidence_bundle(
        bundle,
        signer=signer,
        captured_at_unix_ms=1_000,
        valid_for_ms=5_000,
        authority_generation=2,
        capacity_profiles=profiles,
    )
    return signer, signed


def test_valid_two_node_bundle_plans_without_runtime_offerings() -> None:
    bundle = _bundle()
    wire = evidence_bundle_to_dict(bundle)
    assert all("no_ready_offering" in node["exclusion_reasons"] for node in wire["router_view"]["nodes"])
    assert all(node["eligible"] for node in wire["allocator_view"]["nodes"])

    snapshot = _snapshot(bundle)
    plan = plan_evidence_bundle(bundle, model=PLANNER_MODEL, workload=WORKLOAD, policy=POLICY)

    assert [node["node_id"] for node in snapshot["nodes"]] == ["node-a", "node-b"]
    assert {(link["src"], link["dst"]) for link in snapshot["links"]} == {
        ("node-a", "node-b"),
        ("node-b", "node-a"),
    }
    assert plan.snapshot_digest == planner_snapshot_digest(snapshot)
    assert plan.protocol == "mycelium.route_plan.v2"
    assert plan.placements
    assert snapshot["placement_provenance"] == "planner_v2"
    assert snapshot["decode_mode"] == "stage_local_kv"
    assert plan.diagnostics["placement_provenance"] == "planner_v2"


def test_mixed_mlx_numpy_nodes_can_share_complete_context_replay() -> None:
    mlx = {**PERFORMANCE, "decode_mode": "complete_context_replay"}
    numpy = {
        **PERFORMANCE,
        "backend": "numpy",
        "decode_mode": "complete_context_replay",
        "runtime_build": "numpy-2.5",
        "hardware_class": "x86-64-cpu",
    }
    snapshot = _snapshot(
        _bundle(performance_by_node={"node-a": mlx, "node-b": numpy})
    )

    assert snapshot["decode_mode"] == "complete_context_replay"
    assert snapshot["node_runtime"] == {
        "node-a": {"backend": "mlx", "decode_mode": "complete_context_replay"},
        "node-b": {"backend": "numpy", "decode_mode": "complete_context_replay"},
    }


def test_signed_evidence_and_capacity_profiles_are_the_serving_planner_authority() -> None:
    signer, signed = _signed()

    snapshot = planner_snapshot_from_signed_evidence(
        signed,
        expected_verification_key_digest=signer.verification_key_digest,
        now_unix_ms=2_000,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
        quantization="none",
    )
    plan = plan_signed_evidence(
        signed,
        expected_verification_key_digest=signer.verification_key_digest,
        now_unix_ms=2_000,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
        quantization="none",
    )

    assert set(snapshot["capacity_profile_bindings"]) == {"node-a", "node-b"}
    assert snapshot["evidence_authority"]["authority_generation"] == 2
    assert snapshot["evidence_authority"]["verification_key_digest"] == (
        signer.verification_key_digest
    )
    assert plan.snapshot_digest == planner_snapshot_digest(snapshot)


def test_serving_planner_rejects_unsigned_stale_or_missing_profile_evidence() -> None:
    service = _service()
    signer, signed = _signed(service)
    with pytest.raises(ValueError, match="signed evidence bundle"):
        planner_snapshot_from_signed_evidence(
            evidence_bundle_to_dict(_bundle()),
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=2_000,
            model=PLANNER_MODEL,
            workload=WORKLOAD,
            policy=POLICY,
            quantization="none",
        )
    with pytest.raises(ValueError, match="not current"):
        planner_snapshot_from_signed_evidence(
            signed,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=6_000,
            model=PLANNER_MODEL,
            workload=WORKLOAD,
            policy=POLICY,
            quantization="none",
        )

    missing = dict(service._m13_capacity_profiles)
    missing.pop("node-a")
    _, signed_missing = _signed(service, capacity_profiles=missing)
    with pytest.raises(ValueError, match="missing capacity profile.*node-a"):
        planner_snapshot_from_signed_evidence(
            signed_missing,
            expected_verification_key_digest=signed_missing["verification_key"][
                "verification_key_digest"
            ],
            now_unix_ms=2_000,
            model=PLANNER_MODEL,
            workload=WORKLOAD,
            policy=POLICY,
            quantization="none",
        )


def test_serving_planner_rejects_profile_source_or_slot_mismatch() -> None:
    service = _service()
    profiles = dict(service._m13_capacity_profiles)
    original = profiles["node-a"]
    profiles["node-a"] = compile_capacity_profile(
        CapacityProfileKey(
            **{
                **original.key.to_document(),
                "source_evidence_digest": "sha256:" + "f" * 64,
            }
        ),
        tuple(point.as_observation() for point in original.points),
        original.policy,
    )
    signer, signed = _signed(service, capacity_profiles=profiles)
    with pytest.raises(ValueError, match="reference is missing or mismatched"):
        planner_snapshot_from_signed_evidence(
            signed,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=2_000,
            model=PLANNER_MODEL,
            workload=WORKLOAD,
            policy=POLICY,
            quantization="none",
        )


def test_same_bundle_and_request_is_deterministic() -> None:
    bundle = _bundle()
    first_snapshot = _snapshot(bundle)
    second_snapshot = _snapshot(bundle)
    first_plan = plan_evidence_bundle(bundle, model=PLANNER_MODEL, workload=WORKLOAD, policy=POLICY)
    second_plan = plan_evidence_bundle(bundle, model=PLANNER_MODEL, workload=WORKLOAD, policy=POLICY)

    assert first_snapshot == second_snapshot
    assert first_plan == second_plan


def test_route_snapshot_digest_changes_with_new_evidence_generation() -> None:
    service = _service()
    before = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID, deployment_epoch=3, **MODEL_SCOPE
    )
    replacement = status_payload("node-a", lifecycle="ready", free_bytes=190_000_000)
    replacement["performance"] = copy.deepcopy(PERFORMANCE)
    service.registry.apply(
        make_record(RecordKind.STATUS, node_id="node-a", sequence=2, ttl_ms=10_000, payload=replacement)
    )
    after = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID, deployment_epoch=3, **MODEL_SCOPE
    )

    before_snapshot = _snapshot(before)
    after_snapshot = _snapshot(after)
    assert after.snapshot_generation == before.snapshot_generation + 1
    assert before_snapshot["evidence_bundle_digest"] != after_snapshot["evidence_bundle_digest"]
    assert planner_snapshot_digest(before_snapshot) != planner_snapshot_digest(after_snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "other/model"),
        ("num_layers", 5),
        ("revision", "c" * 40),
        ("weight_digest", "sha256:" + "d" * 64),
    ],
)
def test_mismatched_planner_model_is_rejected(field: str, value) -> None:
    model = copy.deepcopy(PLANNER_MODEL)
    model[field] = value
    with pytest.raises(ValueError, match=field):
        _snapshot(model=model)


def test_tampered_deployment_or_bundle_digest_is_rejected() -> None:
    for mutator in (
        lambda wire: wire["deployment"].__setitem__("deployment_epoch", 4),
        lambda wire: wire.__setitem__("evidence_bundle_digest", "sha256:" + "0" * 64),
    ):
        wire = evidence_bundle_to_dict(_bundle())
        mutator(wire)
        with pytest.raises(ValueError, match="digest"):
            _snapshot(wire)


def test_missing_calibration_fails_closed() -> None:
    with pytest.raises(ValueError, match="missing performance calibration.*node-a"):
        _snapshot(_bundle(missing_performance="node-a"))


def test_missing_allocatable_memory_fails_closed() -> None:
    bundle = _bundle(zero_memory="node-a")
    snapshot = _snapshot(bundle)
    assert [node["node_id"] for node in snapshot["nodes"]] == ["node-b"]


def test_nonzero_reserve_is_rejected_to_prevent_double_reservation() -> None:
    policy: dict[str, Any] = copy.deepcopy(POLICY)
    policy["memory_reserve_fraction"] = 0.1
    with pytest.raises(ValueError, match="already net of reservations"):
        _snapshot(policy=policy)


def test_unreachable_links_are_not_fabricated() -> None:
    with pytest.raises(ValueError, match="required directed link is unreachable"):
        _snapshot(_bundle(reachable=False))


def test_missing_required_directed_link_fails_closed() -> None:
    with pytest.raises(ValueError, match="required directed link is missing for node-b->node-a"):
        _snapshot(_bundle(missing_link=("node-b", "node-a")))


def test_missing_or_stale_self_membership_fails_closed() -> None:
    bundle = evidence_bundle_to_dict(_bundle())
    bundle["records"] = [
        record
        for record in bundle["records"]
        if not (
            record["kind"] == "membership"
            and record["origin_node_id"] == "node-a"
        )
    ]
    from mycelium_gossip.evidence_bundle import evidence_bundle_digest

    bundle["evidence_bundle_digest"] = evidence_bundle_digest(bundle)
    with pytest.raises(ValueError, match="derived from bound evidence|membership"):
        _snapshot(bundle)


def test_link_goodput_and_latency_mapping_preserves_direction() -> None:
    snapshot = _snapshot()
    for link in snapshot["links"]:
        assert link["rtt_ms"] == 6.0
        assert link["jitter_ms"] == 0.5
        assert link["bandwidth_Bps"] == 25_000_000.0
        assert link["inferred"] is False
        assert link["stale"] is False


def test_planner_snapshot_carries_exact_bundle_lineage() -> None:
    bundle = _bundle()
    snapshot = _snapshot(bundle)
    assert snapshot["protocol"] == "mycelium.layer_planner_snapshot.v1"
    assert snapshot["deployment"] == bundle.deployment
    assert snapshot["snapshot_generation"] == bundle.snapshot_generation
    assert snapshot["evidence_bundle_digest"] == bundle.evidence_bundle_digest


def _primary_counts(plan) -> dict[str, int]:
    return {
        placement.node_id: placement.layer_range.count
        for placement in plan.placements
        if placement.primary
    }


def test_compute_only_ab_changes_contiguous_dp_allocation() -> None:
    baseline_service = _service()
    baseline_bundle = baseline_service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=3,
        **MODEL_SCOPE,
    )
    baseline = plan_evidence_bundle(
        baseline_bundle,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
    )

    slower = status_payload("node-a", free_bytes=200_000_000)
    slower_performance = copy.deepcopy(PERFORMANCE)
    slower_performance["prefill_ms_per_layer_token"] = 0.01
    slower_performance["decode_ms_per_layer_token"] = 0.01
    slower["performance"] = slower_performance
    baseline_service.registry.apply(
        make_record(
            RecordKind.STATUS,
            node_id="node-a",
            sequence=2,
            ttl_ms=10_000,
            payload=slower,
        )
    )
    candidate_bundle = baseline_service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=4,
        **MODEL_SCOPE,
    )
    candidate = plan_evidence_bundle(
        candidate_bundle,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
    )

    assert _primary_counts(baseline) == {"node-a": 2, "node-b": 2}
    assert _primary_counts(candidate) == {"node-a": 1, "node-b": 3}


def test_fast_memory_only_ab_changes_dp_and_preserves_total_capacity() -> None:
    service = _service()
    baseline_bundle = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=3,
        **MODEL_SCOPE,
    )
    baseline = plan_evidence_bundle(
        baseline_bundle,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
    )
    tiered = status_payload("node-a", free_bytes=200_000_000)
    tiered["performance"] = copy.deepcopy(PERFORMANCE)
    tiered["memory_domains"] = [
        {
            "memory_domain_id": "system-0",
            "kind": "system",
            "total_bytes": 180_000_000,
            "allocatable_after_reservations_bytes": 180_000_000,
            "committed_bytes": 0,
            "reclaimable_bytes": 0,
            "reservation_generation": 2,
        },
        {
            "memory_domain_id": "vram-0",
            "kind": "vram",
            "total_bytes": 20_000_000,
            "allocatable_after_reservations_bytes": 20_000_000,
            "committed_bytes": 0,
            "reclaimable_bytes": 0,
            "reservation_generation": 2,
        },
    ]
    service.registry.apply(
        make_record(
            RecordKind.STATUS,
            node_id="node-a",
            sequence=2,
            ttl_ms=10_000,
            payload=tiered,
        )
    )
    candidate_bundle = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=4,
        **MODEL_SCOPE,
    )
    candidate_snapshot = _snapshot(candidate_bundle)
    candidate = plan_evidence_bundle(
        candidate_bundle,
        model=PLANNER_MODEL,
        workload=WORKLOAD,
        policy=POLICY,
    )
    node_a = next(node for node in candidate_snapshot["nodes"] if node["node_id"] == "node-a")

    assert node_a["fast_memory_bytes"] == 20_000_000
    assert node_a["total_memory_bytes"] == 200_000_000
    assert _primary_counts(baseline) == {"node-a": 2, "node-b": 2}
    assert _primary_counts(candidate) == {"node-a": 1, "node-b": 3}
