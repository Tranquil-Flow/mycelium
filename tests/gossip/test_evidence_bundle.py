from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from typing import Any, Dict

import pytest

from mycelium_gossip.evidence_bundle import (
    EVIDENCE_BUNDLE_PROTOCOL,
    EvidenceBundleError,
    evidence_bundle_from_dict,
    evidence_bundle_to_dict,
)
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import (
    FailureObservation,
    FailureScope,
    GossipService,
)
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from tests.gossip.helpers import make_record, status_payload


DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
MODEL_IDENTITY = {
    "model_id": "org/model-a",
    "num_layers": 32,
    "manifest_digest": "sha256:" + "a" * 64,
    "resolved_commit": "b" * 40,
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MutatingSnapshotStore(VersionedRecordStore):
    """Mutate the live store immediately after returning its frozen snapshot data."""

    def __init__(self, swarm_id: str, *, monotonic: FakeClock) -> None:
        super().__init__(swarm_id, monotonic=monotonic)
        self.mutate_after_capture = False

    def snapshot(self, *, include_expired: bool = False):
        captured = super().snapshot(include_expired=include_expired)
        if self.mutate_after_capture:
            self.mutate_after_capture = False
            self.apply(
                make_record(
                    RecordKind.STATUS,
                    node_id="node-a",
                    sequence=2,
                    ttl_ms=10_000,
                    payload=status_payload("node-a", lifecycle="draining"),
                )
            )
        return captured


def make_service(
    clock: FakeClock,
    *,
    registry: VersionedRecordStore | None = None,
) -> GossipService:
    selected_registry = registry or VersionedRecordStore("swarm-a", monotonic=clock)
    return GossipService(
        swarm_id="swarm-a",
        node_id="node-local",
        incarnation=1,
        boot_id="boot-node-local-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=clock), "node-local"),
        registry=selected_registry,
        monotonic=clock,
    )


def populate(service: GossipService, clock: FakeClock) -> None:
    # Deliberately apply and announce in non-canonical order.
    for node_id in ("node-b", "node-a"):
        service.registry.apply(
            make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000)
        )
        service.registry.apply(
            make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000)
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
    service.drain()

    for dst_node, probe_id in (("node-b", "probe-b"), ("node-a", "probe-a")):
        service.report_failure(
            FailureObservation(
                route_id="route-1",
                route_generation=7,
                src_node_id="node-local",
                src_endpoint_id="http-local",
                dst_node_id=dst_node,
                dst_endpoint_id="http-overlay",
                offering_id=None,
                failure_kind="connect_timeout",
                scope=FailureScope.EDGE,
                probe_correlation_id=probe_id,
            ),
            quarantine_seconds=20.0,
        )


def capture(service: GossipService, **overrides: Any):
    values: Dict[str, Any] = {
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 4,
        **MODEL_IDENTITY,
    }
    values.update(overrides)
    return service.capture_evidence_bundle(**values)


def recompute_digest(payload: Dict[str, Any]) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "evidence_bundle_digest"}
    canonical = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    payload["evidence_bundle_digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_identical_evidence_has_identical_bundle_and_digest_without_local_times() -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)

    first = capture(service)
    clock.advance(1.0)
    second = capture(service)
    first_wire = evidence_bundle_to_dict(first)
    second_wire = evidence_bundle_to_dict(second)

    assert first == second
    assert first_wire == second_wire
    assert first_wire["protocol"] == EVIDENCE_BUNDLE_PROTOCOL
    assert first_wire["evidence_bundle_digest"].startswith("sha256:")
    assert len(first_wire["evidence_bundle_digest"]) == 71
    assert all("monotonic" not in key for peer in first_wire["peer_states"] for key in peer)
    assert all(
        "monotonic" not in key
        for quarantine in first_wire["quarantines"]
        for key in quarantine
    )
    assert [peer["node_id"] for peer in first_wire["peer_states"]] == ["node-a", "node-b"]
    assert [item["key"] for item in first_wire["quarantines"]] == sorted(
        item["key"] for item in first_wire["quarantines"]
    )


def test_records_preserve_full_wire_evidence_and_record_mutation_changes_generation_and_digest() -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)
    records_at_capture = tuple(entry.record for entry in service.registry.snapshot().records)
    before = capture(service)
    before_wire = evidence_bundle_to_dict(before)

    replacement = make_record(
        RecordKind.STATUS,
        node_id="node-a",
        sequence=2,
        ttl_ms=10_000,
        payload=status_payload("node-a", lifecycle="draining"),
    )
    service.registry.apply(replacement)
    after = capture(service)
    after_wire = evidence_bundle_to_dict(after)

    assert before_wire["records"] == [record.to_dict() for record in records_at_capture]
    assert replacement.to_dict() in after_wire["records"]
    assert set(replacement.to_dict()) == {
        "protocol",
        "swarm_id",
        "kind",
        "origin_node_id",
        "incarnation",
        "sequence",
        "boot_id",
        "generated_at_unix_ms",
        "ttl_ms",
        "payload_hash",
        "payload",
    }
    assert after.snapshot_generation == before.snapshot_generation + 1
    assert after.evidence_bundle_digest != before.evidence_bundle_digest


def test_mutation_after_snapshot_capture_cannot_mix_records_and_views() -> None:
    clock = FakeClock()
    store = MutatingSnapshotStore("swarm-a", monotonic=clock)
    service = make_service(clock, registry=store)
    populate(service, clock)
    generation_before = store.generation
    store.mutate_after_capture = True

    bundle = capture(service)
    wire = evidence_bundle_to_dict(bundle)
    status_record = next(
        record
        for record in wire["records"]
        if record["kind"] == "status" and record["origin_node_id"] == "node-a"
    )
    router_node = next(
        node for node in wire["router_view"]["nodes"] if node["node_id"] == "node-a"
    )
    allocator_node = next(
        node for node in wire["allocator_view"]["nodes"] if node["node_id"] == "node-a"
    )

    assert store.generation == generation_before + 1
    assert bundle.snapshot_generation == generation_before
    assert status_record["sequence"] == 1
    assert router_node["status_version"]["sequence"] == 1
    assert allocator_node["status_version"]["sequence"] == 1


def test_router_and_allocator_views_share_bundle_snapshot_but_keep_eligibility_generations() -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)

    wire = evidence_bundle_to_dict(capture(service))

    assert wire["router_view"]["snapshot_generation"] == wire["snapshot_generation"]
    assert wire["allocator_view"]["snapshot_generation"] == wire["snapshot_generation"]
    assert len(wire["router_view"]["eligibility_generation"]) == 64
    assert len(wire["allocator_view"]["eligibility_generation"]) == 64
    assert wire["router_view"]["eligibility_generation"] != wire["allocator_view"]["eligibility_generation"]


def test_wire_round_trip_is_immutable_and_deterministic() -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)
    bundle = capture(service)

    decoded = evidence_bundle_from_dict(evidence_bundle_to_dict(bundle))

    assert decoded == bundle
    assert evidence_bundle_to_dict(decoded) == evidence_bundle_to_dict(bundle)
    with pytest.raises(FrozenInstanceError):
        decoded.snapshot_generation = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        decoded.records[0] = decoded.records[0]  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["deployment"].__setitem__("deployment_epoch", 5),
        lambda value: value["model"].__setitem__("num_layers", 33),
        lambda value: value["records"][0].__setitem__("sequence", 99),
        lambda value: value["router_view"].__setitem__("snapshot_generation", 99),
        lambda value: value["allocator_view"].__setitem__("eligibility_generation", "0" * 64),
        lambda value: value.__setitem__("evidence_bundle_digest", "0" * 64),
    ],
    ids=("deployment", "model", "records", "router-view", "allocator-view", "digest"),
)
def test_tampering_with_any_bound_section_is_rejected(mutate) -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)
    tampered = copy.deepcopy(evidence_bundle_to_dict(capture(service)))

    mutate(tampered)

    with pytest.raises(EvidenceBundleError):
        evidence_bundle_from_dict(tampered)


def test_recomputed_digest_cannot_authorize_a_view_not_derived_from_bound_evidence() -> None:
    clock = FakeClock()
    service = make_service(clock)
    populate(service, clock)
    tampered = copy.deepcopy(evidence_bundle_to_dict(capture(service)))
    tampered["router_view"]["nodes"][0]["eligible"] = not tampered["router_view"]["nodes"][0]["eligible"]
    recompute_digest(tampered)

    with pytest.raises(EvidenceBundleError, match="router_view"):
        evidence_bundle_from_dict(tampered)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("deployment_id", "not-a-uuid"),
        ("deployment_id", DEPLOYMENT_ID.upper()),
        ("deployment_epoch", -1),
        ("deployment_epoch", True),
        ("model_id", ""),
        ("num_layers", 0),
        ("num_layers", True),
        ("manifest_digest", "sha256:" + "A" * 64),
        ("manifest_digest", "a" * 64),
        ("resolved_commit", "B" * 40),
        ("resolved_commit", "b" * 39),
    ],
)
def test_deployment_and_model_identity_fail_closed(field: str, invalid: Any) -> None:
    clock = FakeClock()
    service = make_service(clock)

    with pytest.raises(EvidenceBundleError):
        capture(service, **{field: invalid})


def test_recomputed_digest_does_not_make_invalid_identity_valid() -> None:
    clock = FakeClock()
    service = make_service(clock)
    wire = evidence_bundle_to_dict(capture(service))
    wire["deployment"]["deployment_id"] = DEPLOYMENT_ID.upper()
    recompute_digest(wire)

    with pytest.raises(EvidenceBundleError, match="canonical UUID"):
        evidence_bundle_from_dict(wire)
