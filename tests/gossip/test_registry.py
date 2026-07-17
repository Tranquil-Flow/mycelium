from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mycelium_gossip.registry import ApplyStatus, EventKind, VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from tests.gossip.helpers import link_payload, make_record, status_payload


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_accepts_newer_sequence_and_rejects_stale_record() -> None:
    clock = FakeClock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    first = make_record(RecordKind.STATUS, sequence=1)
    newer = make_record(RecordKind.STATUS, sequence=2)

    assert store.apply(first).status is ApplyStatus.ACCEPTED
    assert store.apply(newer).status is ApplyStatus.ACCEPTED
    assert store.apply(first).status is ApplyStatus.STALE

    snapshot = store.snapshot()
    assert snapshot.generation == 2
    assert len(snapshot.records) == 1
    assert snapshot.records[0].record.sequence == 2


def test_higher_incarnation_supersedes_lower_incarnation() -> None:
    store = VersionedRecordStore("swarm-a")
    old = make_record(RecordKind.STATUS, sequence=50, incarnation=1)
    restarted = make_record(RecordKind.STATUS, sequence=1, incarnation=2)

    store.apply(old)
    result = store.apply(restarted)

    assert result.status is ApplyStatus.ACCEPTED
    assert store.snapshot().records[0].record.incarnation == 2


def test_same_incarnation_with_different_boot_id_is_identity_conflict() -> None:
    store = VersionedRecordStore("swarm-a")
    first = make_record(RecordKind.STATUS, sequence=1, boot_id="boot-one")
    collision = make_record(RecordKind.STATUS, sequence=2, boot_id="boot-two")

    store.apply(first)
    result = store.apply(collision)

    assert result.status is ApplyStatus.IDENTITY_CONFLICT
    assert store.snapshot().records[0].record.boot_id == "boot-one"


def test_same_version_different_payload_is_conflict() -> None:
    store = VersionedRecordStore("swarm-a")
    first = make_record(RecordKind.STATUS, sequence=1)
    changed_payload = status_payload("node-a")
    changed_payload["queue_depth"] = 3
    equivocation = make_record(RecordKind.STATUS, sequence=1, payload=changed_payload)

    store.apply(first)
    result = store.apply(equivocation)

    assert result.status is ApplyStatus.CONFLICT
    assert store.snapshot().records[0].record.payload["queue_depth"] == 0


def test_duplicate_does_not_refresh_ttl_and_expiry_emits_once() -> None:
    clock = FakeClock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    record = make_record(RecordKind.STATUS, ttl_ms=1_000)

    assert store.apply(record).status is ApplyStatus.ACCEPTED
    clock.advance(0.9)
    assert store.apply(record).status is ApplyStatus.DUPLICATE
    clock.advance(0.2)

    events = store.expire_due()
    assert len(events) == 1
    assert events[0].kind is EventKind.EXPIRED
    assert store.snapshot().records == ()
    assert store.snapshot(include_expired=True).records[0].expired is True
    assert store.expire_due() == ()
    assert store.generation == 2


def test_higher_sequence_revives_expired_key() -> None:
    clock = FakeClock()
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    store.apply(make_record(RecordKind.STATUS, sequence=1, ttl_ms=100))
    clock.advance(0.2)
    store.expire_due()

    result = store.apply(make_record(RecordKind.STATUS, sequence=2, ttl_ms=100))

    assert result.status is ApplyStatus.ACCEPTED
    assert store.snapshot().records[0].expired is False
    assert store.generation == 3


def test_snapshot_and_payload_are_immutable() -> None:
    store = VersionedRecordStore("swarm-a")
    store.apply(make_record(RecordKind.PROFILE))
    snapshot = store.snapshot()

    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.records[0].record.payload["node_id"] = "evil"  # type: ignore[index]


def test_wrong_swarm_is_rejected_without_generation_change() -> None:
    store = VersionedRecordStore("swarm-a")

    result = store.apply(make_record(RecordKind.PROFILE, swarm_id="swarm-b"))

    assert result.status is ApplyStatus.WRONG_SWARM
    assert store.generation == 0


def test_peer_cardinality_limit_is_hard_and_diagnostic() -> None:
    store = VersionedRecordStore("swarm-a", max_peers=2)
    assert store.apply(make_record(RecordKind.PROFILE, node_id="node-a")).status is ApplyStatus.ACCEPTED
    assert store.apply(make_record(RecordKind.PROFILE, node_id="node-b")).status is ApplyStatus.ACCEPTED

    rejected = store.apply(make_record(RecordKind.PROFILE, node_id="node-c"))

    assert rejected.status is ApplyStatus.CAPACITY_REJECTED
    assert store.diagnostics.capacity_rejected == 1
    assert {item.record.origin_node_id for item in store.snapshot().records} == {"node-a", "node-b"}


def test_link_cardinality_is_bounded_per_origin_and_endpoint_pairs_survive() -> None:
    store = VersionedRecordStore("swarm-a", max_links_per_origin=2)
    first = make_record(
        RecordKind.LINK,
        sequence=1,
        payload=link_payload("node-a", "node-b", "lan", "lan"),
    )
    second = make_record(
        RecordKind.LINK,
        sequence=1,
        payload=link_payload("node-a", "node-b", "overlay", "overlay"),
    )
    third = make_record(
        RecordKind.LINK,
        sequence=1,
        payload=link_payload("node-a", "node-c", "relay", "relay"),
    )

    assert store.apply(first).status is ApplyStatus.ACCEPTED
    assert store.apply(second).status is ApplyStatus.ACCEPTED
    assert store.apply(third).status is ApplyStatus.CAPACITY_REJECTED

    endpoint_pairs = {
        (entry.record.payload["src_endpoint_id"], entry.record.payload["dst_endpoint_id"])
        for entry in store.snapshot().records
    }
    assert endpoint_pairs == {("lan", "lan"), ("overlay", "overlay")}


def test_observer_receives_accepted_and_expired_semantic_events() -> None:
    clock = FakeClock()
    seen = []
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    store.subscribe(seen.append)
    record = make_record(RecordKind.STATUS, ttl_ms=100)

    store.apply(record)
    store.apply(record)
    clock.advance(0.2)
    store.expire_due()

    assert [event.kind for event in seen] == [EventKind.ACCEPTED, EventKind.EXPIRED]
