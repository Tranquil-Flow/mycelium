from __future__ import annotations

import copy
from typing import Any

import pytest

from mycelium_gossip.evidence_bundle import evidence_bundle_to_dict
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from mycelium_layer_planner.gossip_adapter import (
    plan_evidence_bundle,
    planner_snapshot_digest,
    planner_snapshot_from_evidence_bundle,
)
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
WORKLOAD = {"preset": "interactive_chat_v1", "concurrency_points": [1], "user_scale": 1}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 0,
    "ttft_slo_ms": 1_000_000,
    "tpot_slo_ms": 1_000_000,
}
PERFORMANCE = {
    "prefill_ms_per_layer_token": 0.001,
    "decode_ms_per_layer_token": 0.001,
    "memory_bandwidth_Bps": 1_000_000_000,
    "spill_bandwidth_Bps": 1_000_000_000,
    "calibration_confidence": 0.9,
}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _service(*, missing_performance: str | None = None, zero_memory: str | None = None, reachable: bool = True):
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
    for node_id in ("node-a", "node-b"):
        profile = profile_payload(node_id)
        status = status_payload(node_id, free_bytes=0 if zero_memory == node_id else 200_000_000)
        if missing_performance != node_id:
            status["performance"] = copy.deepcopy(PERFORMANCE)
        store.apply(make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000, payload=profile))
        store.apply(make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000, payload=status))
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
        store.apply(
            make_record(
                RecordKind.LINK,
                node_id=src,
                ttl_ms=10_000,
                payload=link_payload(src, dst, reachable=reachable),
            )
        )
    service.drain()
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
    snapshot = _snapshot(_bundle(reachable=False))
    assert snapshot["links"] == []


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
