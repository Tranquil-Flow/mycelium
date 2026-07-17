from __future__ import annotations

import pytest

from mycelium_gossip.schema import RecordKind, transport_key
from mycelium_gossip.transport import (
    BoundedCoalescingInbox,
    GossipTransport,
    InMemoryMesh,
    InMemoryTransport,
    LivenessEvent,
    LivenessKind,
    ReceivedRecord,
    TransportError,
)
from tests.gossip.helpers import make_record


def test_in_memory_transport_satisfies_protocol() -> None:
    transport = InMemoryTransport(InMemoryMesh(), "node-a")
    assert isinstance(transport, GossipTransport)


def test_three_peer_publish_subscribe_and_query() -> None:
    mesh = InMemoryMesh()
    a = InMemoryTransport(mesh, "node-a")
    b = InMemoryTransport(mesh, "node-b")
    c = InMemoryTransport(mesh, "node-c")
    for transport in (a, b, c):
        transport.start()

    received = []
    b.subscribe_records(received.append)
    record = make_record(RecordKind.PROFILE, node_id="node-a")
    a.publish_record(record)

    assert [item.record for item in received] == [record]
    replies = c.query_records("mycelium/swarm-a/**")
    assert [item.record for item in replies] == [record]
    assert replies[0].transport_key == transport_key(record)


def test_query_cache_does_not_regress_to_stale_publish() -> None:
    mesh = InMemoryMesh()
    a = InMemoryTransport(mesh, "node-a")
    b = InMemoryTransport(mesh, "node-b")
    a.start()
    b.start()
    newer = make_record(RecordKind.STATUS, node_id="node-a", sequence=2)
    stale = make_record(RecordKind.STATUS, node_id="node-a", sequence=1)

    a.publish_record(newer)
    a.publish_record(stale)

    assert [item.record.sequence for item in b.query_records("mycelium/swarm-a/**")] == [2]


def test_liveness_history_and_delete_are_observable() -> None:
    mesh = InMemoryMesh()
    a = InMemoryTransport(mesh, "node-a")
    b = InMemoryTransport(mesh, "node-b")
    a.start()
    b.start()
    a.declare_liveness("swarm-a", "node-a", 3, "boot-a")

    events = []
    b.subscribe_liveness(events.append, history=True)
    a.stop()

    assert [event.kind for event in events] == [LivenessKind.PUT, LivenessKind.DELETE]
    assert events[0].identity == ("node-a", 3, "boot-a")


def test_callback_failure_does_not_block_other_subscribers() -> None:
    mesh = InMemoryMesh()
    a = InMemoryTransport(mesh, "node-a")
    b = InMemoryTransport(mesh, "node-b")
    c = InMemoryTransport(mesh, "node-c")
    for transport in (a, b, c):
        transport.start()

    b.subscribe_records(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    seen = []
    c.subscribe_records(seen.append)
    a.publish_record(make_record(RecordKind.PROFILE))

    assert len(seen) == 1
    assert mesh.diagnostics.callback_failures == 1


def test_transport_requires_start_and_stop_is_idempotent() -> None:
    transport = InMemoryTransport(InMemoryMesh(), "node-a")
    with pytest.raises(TransportError, match="not started"):
        transport.publish_record(make_record(RecordKind.PROFILE))
    transport.start()
    transport.stop()
    transport.stop()


def test_inbox_coalesces_updates_by_logical_record_key() -> None:
    inbox = BoundedCoalescingInbox(max_records=4, max_priority=2)
    first = make_record(RecordKind.STATUS, sequence=1)
    latest = make_record(RecordKind.STATUS, sequence=2)

    assert inbox.put_record(ReceivedRecord(transport_key(first), first, 1.0)) is True
    assert inbox.put_record(ReceivedRecord(transport_key(latest), latest, 2.0)) is True

    drained = inbox.drain()
    assert len(drained) == 1
    assert drained[0].record.sequence == 2
    assert inbox.diagnostics.coalesced_records == 1


def test_inbox_is_hard_bounded_and_reports_drops() -> None:
    inbox = BoundedCoalescingInbox(max_records=2, max_priority=1)
    a = make_record(RecordKind.PROFILE, node_id="node-a")
    b = make_record(RecordKind.PROFILE, node_id="node-b")
    c = make_record(RecordKind.PROFILE, node_id="node-c")

    assert inbox.put_record(ReceivedRecord(transport_key(a), a, 1.0)) is True
    assert inbox.put_record(ReceivedRecord(transport_key(b), b, 1.0)) is True
    assert inbox.put_record(ReceivedRecord(transport_key(c), c, 1.0)) is False

    assert inbox.depth == 2
    assert inbox.diagnostics.dropped_records == 1
    assert inbox.diagnostics.high_watermark == 2


def test_priority_liveness_drains_before_records_and_coalesces() -> None:
    inbox = BoundedCoalescingInbox(max_records=4, max_priority=2)
    record = make_record(RecordKind.PROFILE)
    put = LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-a", 1.0)
    delete = LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-a", 2.0)

    inbox.put_record(ReceivedRecord(transport_key(record), record, 1.0))
    inbox.put_liveness(put)
    inbox.put_liveness(delete)

    drained = inbox.drain()
    assert isinstance(drained[0], LivenessEvent)
    assert drained[0].kind is LivenessKind.DELETE
    assert isinstance(drained[1], ReceivedRecord)
    assert inbox.diagnostics.coalesced_priority == 1


def test_unsubscribe_prevents_future_delivery() -> None:
    mesh = InMemoryMesh()
    a = InMemoryTransport(mesh, "node-a")
    b = InMemoryTransport(mesh, "node-b")
    a.start()
    b.start()
    seen = []
    unsubscribe = b.subscribe_records(seen.append)
    unsubscribe()

    a.publish_record(make_record(RecordKind.PROFILE))

    assert seen == []
