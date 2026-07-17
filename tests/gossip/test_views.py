from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import (
    FailureObservation,
    FailureScope,
    PeerHealthState,
    PeerState,
    QuarantineEntry,
)
from mycelium_gossip.views import (
    allocator_view_to_dict,
    build_allocator_view,
    build_router_view,
    router_view_to_dict,
)
from tests.gossip.helpers import link_payload, make_record, offering_payload, status_payload


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def alive(node_id: str, boot_id: str | None = None) -> PeerState:
    return PeerState(
        node_id=node_id,
        incarnation=1,
        boot_id=boot_id or f"boot-{node_id}-1",
        state=PeerHealthState.ALIVE,
        liveness_present=True,
        changed_at_monotonic=100.0,
    )


def populated_store(clock: FakeClock) -> VersionedRecordStore:
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    for node_id in ("node-a", "node-b"):
        store.apply(make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000))
        store.apply(make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=1_000))
        store.apply(make_record(RecordKind.OFFERING, node_id=node_id, ttl_ms=1_000))
    store.apply(
        make_record(
            RecordKind.LINK,
            node_id="node-a",
            ttl_ms=1_000,
            payload=link_payload("node-a", "node-b"),
        )
    )
    return store


def quarantine(scope: FailureScope) -> QuarantineEntry:
    observation = FailureObservation(
        route_id="route-1",
        route_generation=1,
        src_node_id="node-a",
        src_endpoint_id="http-overlay",
        dst_node_id="node-b",
        dst_endpoint_id="http-overlay",
        offering_id="assignment-a" if scope is FailureScope.OFFERING else None,
        failure_kind="test",
        scope=scope,
        probe_correlation_id="probe-1",
    )
    if scope is FailureScope.EDGE:
        key = ("edge", "node-a", "http-overlay", "node-b", "http-overlay")
    elif scope is FailureScope.OFFERING:
        key = ("offering", "node-b", "assignment-a")
    else:
        key = ("peer", "node-b")
    return QuarantineEntry(key, scope, observation, 100.0, 105.0)


def test_router_view_exposes_frozen_evidence_not_route_decisions() -> None:
    clock = FakeClock()
    store = populated_store(clock)

    view = build_router_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())

    assert view.snapshot_generation == store.generation
    assert len(view.eligibility_generation) == 64
    assert [node.node_id for node in view.nodes] == ["node-a", "node-b"]
    assert all(node.eligible for node in view.nodes)
    assert view.nodes[0].offerings[0].assignment_id == "assignment-a"
    assert view.nodes[0].profile_version.sequence == 1
    assert view.nodes[0].status_version.sequence == 1
    assert len(view.edges) == 1
    assert view.edges[0].src_endpoint_id == "http-overlay"
    assert view.edges[0].dst_endpoint_id == "http-overlay"
    assert view.edges[0].eligible is True
    assert not hasattr(view, "selected_route")
    assert not hasattr(view, "score")


def test_derived_views_have_distinct_versioned_wire_envelopes() -> None:
    clock = FakeClock()
    store = populated_store(clock)
    peers = (alive("node-a"), alive("node-b"))

    router_payload = router_view_to_dict(build_router_view(store.snapshot(), peers, ()))
    allocator_payload = allocator_view_to_dict(build_allocator_view(store.snapshot(), peers, ()))

    assert router_payload["protocol"] == "mycelium.gossip.router_view.v1"
    assert allocator_payload["protocol"] == "mycelium.gossip.allocator_view.v1"
    assert router_payload["nodes"][0]["peer_state"] == "alive"
    assert allocator_payload["nodes"][0]["peer_state"] == "alive"
    assert len(router_payload["eligibility_generation"]) == 64
    assert len(allocator_payload["eligibility_generation"]) == 64


def test_old_incarnation_link_is_retired_after_source_restart() -> None:
    store = populated_store(FakeClock())
    for kind in (RecordKind.PROFILE, RecordKind.STATUS, RecordKind.OFFERING):
        store.apply(
            make_record(
                kind,
                node_id="node-a",
                incarnation=2,
                boot_id="boot-node-a-2",
                sequence=0,
            )
        )
    restarted = PeerState(
        "node-a",
        2,
        "boot-node-a-2",
        PeerHealthState.ALIVE,
        True,
        101.0,
    )

    view = build_router_view(store.snapshot(), (restarted, alive("node-b")), ())

    node_a = next(node for node in view.nodes if node.node_id == "node-a")
    assert node_a.eligible is True
    assert view.edges == ()


def test_edge_quarantine_does_not_quarantine_peer_or_offering() -> None:
    store = populated_store(FakeClock())

    view = build_router_view(
        store.snapshot(),
        (alive("node-a"), alive("node-b")),
        (quarantine(FailureScope.EDGE),),
    )

    assert all(node.eligible for node in view.nodes)
    assert view.edges[0].eligible is False
    assert view.edges[0].exclusion_reasons == ("edge_quarantined",)


def test_offering_quarantine_only_removes_matching_runtime_offering() -> None:
    store = populated_store(FakeClock())

    view = build_router_view(
        store.snapshot(),
        (alive("node-a"), alive("node-b")),
        (quarantine(FailureScope.OFFERING),),
    )

    node_b = next(node for node in view.nodes if node.node_id == "node-b")
    assert node_b.offerings == ()
    assert node_b.eligible is False
    assert "no_ready_offering" in node_b.exclusion_reasons
    assert next(node for node in view.nodes if node.node_id == "node-a").eligible is True


def test_peer_quarantine_excludes_node_and_incident_edges() -> None:
    store = populated_store(FakeClock())

    view = build_router_view(
        store.snapshot(),
        (alive("node-a"), alive("node-b")),
        (quarantine(FailureScope.PEER),),
    )

    node_b = next(node for node in view.nodes if node.node_id == "node-b")
    assert node_b.eligible is False
    assert "peer_quarantined" in node_b.exclusion_reasons
    assert view.edges[0].eligible is False
    assert "destination_ineligible" in view.edges[0].exclusion_reasons


def test_suspect_peer_and_draining_status_are_explicit_exclusions() -> None:
    store = populated_store(FakeClock())
    draining = status_payload("node-a", lifecycle="draining")
    store.apply(make_record(RecordKind.STATUS, node_id="node-a", sequence=2, payload=draining))
    suspect = PeerState(
        "node-b",
        1,
        "boot-node-b-1",
        PeerHealthState.SUSPECT,
        False,
        101.0,
        101.0,
    )

    view = build_router_view(store.snapshot(), (alive("node-a"), suspect), ())

    node_a = next(node for node in view.nodes if node.node_id == "node-a")
    node_b = next(node for node in view.nodes if node.node_id == "node-b")
    assert "status_not_ready" in node_a.exclusion_reasons
    assert "peer_not_alive" in node_b.exclusion_reasons


def test_allocator_view_counts_unified_memory_domain_once() -> None:
    store = populated_store(FakeClock())

    view = build_allocator_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())

    node_a = next(node for node in view.nodes if node.node_id == "node-a")
    assert len(node_a.memory_domains) == 1
    domain = node_a.memory_domains[0]
    assert domain.memory_domain_id == "unified-0"
    assert domain.total_bytes == 48 * 1024**3
    assert domain.allocatable_after_reservations_bytes == 32 * 1024**3
    assert domain.committed_bytes == 8 * 1024**3
    assert domain.reclaimable_bytes == 2 * 1024**3
    assert domain.reservation_generation == 1
    assert node_a.total_allocatable_bytes == 32 * 1024**3


def test_allocator_view_keeps_ineligible_node_with_reason_for_diagnostics() -> None:
    store = populated_store(FakeClock())
    status = status_payload("node-b")
    status["concurrency_limit"] = 0
    store.apply(make_record(RecordKind.STATUS, node_id="node-b", sequence=2, payload=status))

    view = build_allocator_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())

    node_b = next(node for node in view.nodes if node.node_id == "node-b")
    assert node_b.eligible is False
    assert node_b.exclusion_reasons == ("no_concurrency_capacity",)


def test_expired_status_and_offering_disappear_from_views_without_packet() -> None:
    clock = FakeClock()
    store = populated_store(clock)
    clock.advance(1.1)
    store.expire_due()

    router = build_router_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())
    allocator = build_allocator_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())

    assert all("status_missing" in node.exclusion_reasons for node in router.nodes)
    assert all(node.offerings == () for node in router.nodes)
    assert all(node.eligible is False for node in allocator.nodes)


def test_view_dataclasses_and_collections_are_immutable() -> None:
    store = populated_store(FakeClock())
    view = build_router_view(store.snapshot(), (alive("node-a"), alive("node-b")), ())

    with pytest.raises(FrozenInstanceError):
        view.snapshot_generation = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        view.nodes[0].endpoints[0] = view.nodes[0].endpoints[0]  # type: ignore[index]


def test_non_probed_offering_never_becomes_eligible() -> None:
    clock = FakeClock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    store.apply(make_record(RecordKind.PROFILE, node_id="node-a"))
    store.apply(make_record(RecordKind.STATUS, node_id="node-a"))
    payload = offering_payload("node-a")
    payload["readiness_state"] = "loaded"
    store.apply(make_record(RecordKind.OFFERING, node_id="node-a", payload=payload))

    view = build_router_view(store.snapshot(), (alive("node-a"),), ())

    assert view.nodes[0].offerings == ()
    assert view.nodes[0].eligible is False
