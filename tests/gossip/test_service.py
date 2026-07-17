from __future__ import annotations

import threading
import time
from typing import Callable, Tuple

import pytest

from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind, transport_key
from mycelium_gossip.service import (
    ActiveRouteAtRisk,
    FailureObservation,
    FailureScope,
    GossipService,
    PeerHealthState,
    ServiceError,
    ServiceLifecycleState,
)
from mycelium_gossip.transport import (
    InMemoryMesh,
    InMemoryTransport,
    LivenessEvent,
    LivenessKind,
    ReceivedRecord,
)
from tests.gossip.helpers import make_record


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SpyTransport:
    def __init__(self, replies: Tuple[ReceivedRecord, ...] = ()) -> None:
        self.calls = []
        self.replies = replies
        self.record_callback: Callable | None = None
        self.liveness_callback: Callable | None = None

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def publish_record(self, record) -> None:
        self.calls.append("publish")

    def query_records(self, pattern: str):
        self.calls.append("query")
        return self.replies

    def subscribe_records(self, callback):
        self.calls.append("subscribe_records")
        self.record_callback = callback
        return lambda: None

    def subscribe_liveness(self, callback, *, history: bool = True):
        self.calls.append("subscribe_liveness")
        self.liveness_callback = callback
        return lambda: None

    def declare_liveness(self, swarm_id, node_id, incarnation, boot_id):
        self.calls.append("declare_liveness")


class FailingStartupTransport(SpyTransport):
    def subscribe_records(self, callback):
        self.calls.append("subscribe_records")
        self.record_callback = callback

        def unsubscribe() -> None:
            self.calls.append("unsubscribe_records")

        return unsubscribe

    def subscribe_liveness(self, callback, *, history: bool = True):
        self.calls.append("subscribe_liveness")
        raise RuntimeError("subscription failed")


class LifecycleSpyTransport(SpyTransport):
    def subscribe_records(self, callback):
        self.calls.append("subscribe_records")
        self.record_callback = callback

        def unsubscribe() -> None:
            self.calls.append("unsubscribe_records")

        return unsubscribe

    def subscribe_liveness(self, callback, *, history: bool = True):
        self.calls.append("subscribe_liveness")
        self.liveness_callback = callback

        def unsubscribe() -> None:
            self.calls.append("unsubscribe_liveness")

        return unsubscribe


class BlockingStartTransport(LifecycleSpyTransport):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def start(self) -> None:
        self.calls.append("start")
        self.start_entered.set()
        assert self.release_start.wait(2.0)


class BlockingSnapshotStore(VersionedRecordStore):
    def __init__(self, swarm_id: str, *, monotonic) -> None:
        super().__init__(swarm_id, monotonic=monotonic)
        self.block_snapshot = False
        self.snapshot_started = threading.Event()
        self.release_snapshot = threading.Event()

    def snapshot(self):
        if self.block_snapshot:
            self.snapshot_started.set()
            self.release_snapshot.wait(1.0)
        return super().snapshot()


def make_service(
    clock: FakeClock,
    *,
    node_id: str = "node-local",
    transport=None,
    registry: VersionedRecordStore | None = None,
    suspicion_grace_seconds: float = 3.0,
    max_records_per_peer_per_second: int = 100,
    repair_interval_seconds: float = 5.0,
    shutdown_timeout_seconds: float = 1.0,
) -> GossipService:
    selected = transport or InMemoryTransport(InMemoryMesh(monotonic=clock), node_id)
    store = registry or VersionedRecordStore("swarm-a", monotonic=clock)
    return GossipService(
        swarm_id="swarm-a",
        node_id=node_id,
        incarnation=1,
        boot_id=f"boot-{node_id}",
        transport=selected,
        registry=store,
        monotonic=clock,
        nonce_factory=lambda: "nonce-1",
        suspicion_grace_seconds=suspicion_grace_seconds,
        max_records_per_peer_per_second=max_records_per_peer_per_second,
        repair_interval_seconds=repair_interval_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


def test_start_subscribes_before_snapshot_query_then_declares_liveness() -> None:
    clock = FakeClock()
    reply_record = make_record(RecordKind.PROFILE, node_id="node-a")
    spy = SpyTransport((ReceivedRecord(transport_key(reply_record), reply_record, clock()),))
    service = make_service(clock, transport=spy)

    service.start(background=False)

    assert spy.calls[:5] == [
        "start",
        "subscribe_records",
        "subscribe_liveness",
        "query",
        "declare_liveness",
    ]
    assert service.registry.snapshot().records[0].record == reply_record


def test_start_failure_rolls_back_transport_and_acquired_subscriptions() -> None:
    clock = FakeClock()
    transport = FailingStartupTransport()
    service = make_service(clock, transport=transport)

    with pytest.raises(RuntimeError, match="subscription failed"):
        service.start(background=False)

    assert service.started is False
    assert transport.calls == [
        "start",
        "subscribe_records",
        "subscribe_liveness",
        "unsubscribe_records",
        "stop",
    ]


def test_concurrent_start_initializes_transport_once_and_waiters_observe_running() -> None:
    clock = FakeClock()
    transport = BlockingStartTransport()
    service = make_service(clock, transport=transport)
    completed: list[str] = []

    first = threading.Thread(target=lambda: (service.start(background=False), completed.append("first")))
    second = threading.Thread(target=lambda: (service.start(background=False), completed.append("second")))
    first.start()
    assert transport.start_entered.wait(1.0)
    second.start()

    time.sleep(0.02)
    assert completed == []
    assert service.started is False
    assert service.lifecycle_state is ServiceLifecycleState.STARTING

    transport.release_start.set()
    first.join(1.0)
    second.join(1.0)

    assert sorted(completed) == ["first", "second"]
    assert transport.calls.count("start") == 1
    assert service.lifecycle_state is ServiceLifecycleState.RUNNING
    service.stop()


def test_stop_is_idempotent_and_tears_down_each_resource_once() -> None:
    clock = FakeClock()
    transport = LifecycleSpyTransport()
    service = make_service(clock, transport=transport)
    service.start(background=False)

    service.stop()
    service.stop()

    assert service.lifecycle_state is ServiceLifecycleState.STOPPED
    assert transport.calls.count("unsubscribe_liveness") == 1
    assert transport.calls.count("unsubscribe_records") == 1
    assert transport.calls.count("stop") == 1
    assert transport.calls.index("unsubscribe_liveness") < transport.calls.index("stop")
    assert transport.calls.index("unsubscribe_records") < transport.calls.index("stop")


def test_second_worker_start_failure_preserves_error_and_allows_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    transport = LifecycleSpyTransport()
    service = make_service(clock, transport=transport)
    real_start = threading.Thread.start

    def fail_repair_worker(thread: threading.Thread) -> None:
        if thread.name.startswith("mycelium-gossip-repair-"):
            raise OSError("repair thread start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_repair_worker)
    with pytest.raises(OSError, match="repair thread start failed"):
        service.start(background=True)

    assert service.lifecycle_state is ServiceLifecycleState.STOPPED
    assert transport.calls.count("stop") == 1
    service.start(background=False)
    assert service.lifecycle_state is ServiceLifecycleState.RUNNING
    service.stop()


def test_callback_admitted_during_stop_cannot_leak_into_next_run() -> None:
    clock = FakeClock()
    transport = LifecycleSpyTransport()
    service = make_service(clock, transport=transport)
    service.start(background=False)
    assert transport.record_callback is not None
    old_callback = transport.record_callback
    record = make_record(RecordKind.STATUS, node_id="node-a", ttl_ms=10_000)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    original_submit = service.submit_received

    def blocked_submit(received: ReceivedRecord) -> bool:
        callback_entered.set()
        assert release_callback.wait(1.0)
        return original_submit(received)

    service.submit_received = blocked_submit  # type: ignore[method-assign]
    callback_thread = threading.Thread(
        target=lambda: old_callback(ReceivedRecord(transport_key(record), record, clock())),
        daemon=True,
    )
    callback_thread.start()
    assert callback_entered.wait(1.0)
    stopper = threading.Thread(target=service.stop, daemon=True)
    stopper.start()
    time.sleep(0.02)
    release_callback.set()
    callback_thread.join(1.0)
    stopper.join(1.0)
    assert not callback_thread.is_alive()
    assert not stopper.is_alive()

    service.submit_received = original_submit  # type: ignore[method-assign]
    service.start(background=False)
    service.drain()
    assert service.registry.snapshot().records == ()
    service.stop()


def test_two_peer_startup_converges_from_liveness_history_and_query_repair() -> None:
    clock = FakeClock()
    mesh = InMemoryMesh(monotonic=clock)
    a_transport = InMemoryTransport(mesh, "node-a")
    a_transport.start()
    a_transport.publish_record(make_record(RecordKind.PROFILE, node_id="node-a"))
    a_transport.publish_record(make_record(RecordKind.STATUS, node_id="node-a"))
    a_transport.declare_liveness("swarm-a", "node-a", 1, "boot-node-a-1")

    b = GossipService(
        swarm_id="swarm-a",
        node_id="node-b",
        incarnation=1,
        boot_id="boot-node-b-1",
        transport=InMemoryTransport(mesh, "node-b"),
        registry=VersionedRecordStore("swarm-a", monotonic=clock),
        monotonic=clock,
    )
    b.start(background=False)

    report = b.convergence_report(include_local=False)
    assert report.converged is True
    assert report.peers[0].node_id == "node-a"
    assert report.peers[0].missing_kinds == ()


def test_transport_key_must_match_envelope_identity() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    record = make_record(RecordKind.PROFILE, node_id="node-a")

    assert service.submit_received(ReceivedRecord("mycelium/swarm-a/node-x/profile", record, clock()))
    service.drain()

    assert service.registry.snapshot().records == ()
    assert service.diagnostics.invalid_transport_keys == 1


def test_record_intake_rate_limit_is_per_origin() -> None:
    clock = FakeClock()
    service = make_service(clock, max_records_per_peer_per_second=2)
    service.start(background=False)
    records = [
        make_record(RecordKind.PROFILE, node_id="node-a"),
        make_record(RecordKind.STATUS, node_id="node-a"),
        make_record(RecordKind.OFFERING, node_id="node-a"),
    ]

    accepted = [
        service.submit_received(ReceivedRecord(transport_key(record), record, clock()))
        for record in records
    ]
    service.drain()

    assert accepted == [True, True, False]
    assert service.diagnostics.rate_limited_records == 1
    assert len(service.registry.snapshot().records) == 2


def test_liveness_delete_becomes_suspect_then_dead_after_grace() -> None:
    clock = FakeClock()
    service = make_service(clock, suspicion_grace_seconds=3.0)
    service.start(background=False)
    put = LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock())
    delete = LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-node-a-1", clock())

    service.submit_liveness(put)
    service.drain()
    assert service.peer_state("node-a").state is PeerHealthState.ALIVE

    service.submit_liveness(delete)
    service.drain()
    assert service.peer_state("node-a").state is PeerHealthState.SUSPECT

    clock.advance(3.1)
    service.tick()
    assert service.peer_state("node-a").state is PeerHealthState.DEAD


def test_old_delete_cannot_kill_new_incarnation() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 2, "boot-node-a-2", clock()))
    service.submit_liveness(LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()

    state = service.peer_state("node-a")
    assert state.incarnation == 2
    assert state.state is PeerHealthState.ALIVE
    assert service.diagnostics.stale_liveness_events == 1


def test_same_incarnation_boot_collision_is_excluded() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-a", clock()))
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-b", clock()))
    service.drain()

    assert service.peer_state("node-a").state is PeerHealthState.IDENTITY_CONFLICT
    assert service.diagnostics.identity_conflicts == 1


def test_peer_change_callbacks_run_without_service_state_lock() -> None:
    clock = FakeClock()
    service = make_service(clock)
    callback_probe_results = []
    probe_threads = []

    def callback(_event) -> None:
        acquired = threading.Event()

        def probe_lock() -> None:
            with service._lock:  # type: ignore[attr-defined]
                acquired.set()

        thread = threading.Thread(target=probe_lock, daemon=True)
        probe_threads.append(thread)
        thread.start()
        callback_probe_results.append(acquired.wait(0.2))

    service.subscribe_events(callback)
    service.submit_liveness(
        LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-a", clock())
    )
    service.drain()
    for thread in probe_threads:
        thread.join(1.0)

    assert callback_probe_results == [True]


def test_same_incarnation_recovery_requires_liveness_challenge_and_fresh_status() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    initial_status = make_record(RecordKind.STATUS, node_id="node-a", ttl_ms=10_000)
    service.submit_received(ReceivedRecord(transport_key(initial_status), initial_status, clock()))
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    service.submit_liveness(LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()

    assert service.peer_state("node-a").state is PeerHealthState.SUSPECT
    nonce = service.issue_recovery_challenge("node-a")
    assert service.confirm_recovery("node-a", nonce) is False

    clock.advance(0.1)
    fresh_status = make_record(RecordKind.STATUS, node_id="node-a", sequence=2, ttl_ms=10_000)
    service.submit_received(ReceivedRecord(transport_key(fresh_status), fresh_status, clock()))
    service.drain()
    assert service.confirm_recovery("node-a", nonce) is True
    assert service.peer_state("node-a").state is PeerHealthState.ALIVE


def test_recovery_confirmation_rejects_concurrent_liveness_delete() -> None:
    clock = FakeClock()
    store = BlockingSnapshotStore("swarm-a", monotonic=clock)
    service = make_service(clock, registry=store)
    service.start(background=False)
    initial_status = make_record(RecordKind.STATUS, node_id="node-a", ttl_ms=10_000)
    service.submit_received(ReceivedRecord(transport_key(initial_status), initial_status, clock()))
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    service.submit_liveness(LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    service.submit_liveness(LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    nonce = service.issue_recovery_challenge("node-a")
    clock.advance(0.1)
    fresh_status = make_record(RecordKind.STATUS, node_id="node-a", sequence=2, ttl_ms=10_000)
    service.submit_received(ReceivedRecord(transport_key(fresh_status), fresh_status, clock()))
    service.drain()

    store.block_snapshot = True
    result: list[bool] = []
    confirmer = threading.Thread(target=lambda: result.append(service.confirm_recovery("node-a", nonce)))
    confirmer.start()
    assert store.snapshot_started.wait(1.0)
    service.submit_liveness(LivenessEvent(LivenessKind.DELETE, "swarm-a", "node-a", 1, "boot-node-a-1", clock()))
    service.drain()
    store.release_snapshot.set()
    confirmer.join(1.0)

    assert result == [False]
    state = service.peer_state("node-a")
    assert state is not None
    assert state.state is PeerHealthState.SUSPECT
    assert state.liveness_present is False


def edge_failure() -> FailureObservation:
    return FailureObservation(
        route_id="route-1",
        route_generation=7,
        src_node_id="node-a",
        src_endpoint_id="http-a",
        dst_node_id="node-b",
        dst_endpoint_id="http-b",
        offering_id=None,
        failure_kind="connect_timeout",
        scope=FailureScope.EDGE,
        probe_correlation_id="probe-1",
    )


def test_failure_immediately_emits_route_risk_and_only_quarantines_edge() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    events = []
    service.subscribe_events(events.append)

    emitted = service.report_failure(edge_failure(), quarantine_seconds=2.0)

    assert isinstance(emitted, ActiveRouteAtRisk)
    assert events[-1] == emitted
    assert service.is_edge_quarantined("node-a", "http-a", "node-b", "http-b")
    assert not service.is_peer_quarantined("node-b")
    assert not service.is_offering_quarantined("node-b", "assignment-a")

    clock.advance(2.1)
    service.tick()
    assert not service.is_edge_quarantined("node-a", "http-a", "node-b", "http-b")


def test_offering_and_peer_quarantines_remain_distinct() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    offering = FailureObservation(
        "route-1", 1, "node-a", "http-a", "node-b", "http-b", "assignment-a",
        "runtime_error", FailureScope.OFFERING, "probe-2",
    )
    peer = FailureObservation(
        "route-2", 1, "node-a", "http-a", "node-c", "http-c", None,
        "application_unreachable", FailureScope.PEER, "probe-3",
    )

    service.report_failure(offering)
    service.report_failure(peer)

    assert service.is_offering_quarantined("node-b", "assignment-a")
    assert not service.is_peer_quarantined("node-b")
    assert service.is_peer_quarantined("node-c")


def test_event_callback_failure_isolated() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    seen = []

    def broken(event) -> None:
        raise RuntimeError("observer failed")

    service.subscribe_events(broken)
    service.subscribe_events(seen.append)
    service.report_failure(edge_failure())

    assert isinstance(seen[-1], ActiveRouteAtRisk)
    assert service.diagnostics.event_callback_failures == 1


def test_background_worker_processes_transport_callbacks() -> None:
    mesh = InMemoryMesh()
    receiver = GossipService(
        swarm_id="swarm-a",
        node_id="node-b",
        incarnation=1,
        boot_id="boot-node-b-1",
        transport=InMemoryTransport(mesh, "node-b"),
        registry=VersionedRecordStore("swarm-a"),
        worker_poll_seconds=0.01,
    )
    sender = InMemoryTransport(mesh, "node-a")
    receiver.start(background=True)
    sender.start()
    record = make_record(RecordKind.PROFILE, node_id="node-a")
    sender.publish_record(record)

    deadline = time.monotonic() + 1.0
    while not receiver.registry.snapshot().records and time.monotonic() < deadline:
        time.sleep(0.01)

    receiver.stop()
    sender.stop()
    assert receiver.registry.snapshot().records[0].record == record


def test_publish_rejects_foreign_origin() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    with pytest.raises(ServiceError, match="local node"):
        service.publish_record(make_record(RecordKind.PROFILE, node_id="node-a"))


def test_periodic_expiry_changes_snapshot_without_new_packet() -> None:
    clock = FakeClock()
    service = make_service(clock)
    service.start(background=False)
    record = make_record(RecordKind.STATUS, node_id="node-a", ttl_ms=100)
    service.submit_received(ReceivedRecord(transport_key(record), record, clock()))
    service.drain()
    generation = service.registry.generation

    clock.advance(0.11)
    service.tick()

    assert service.registry.snapshot().records == ()
    assert service.registry.generation == generation + 1


def test_repair_once_recovers_record_missed_during_startup() -> None:
    clock = FakeClock()
    spy = SpyTransport()
    service = make_service(clock, transport=spy)
    service.start(background=False)
    missed = make_record(RecordKind.PROFILE, node_id="node-a", ttl_ms=10_000)
    spy.replies = (ReceivedRecord(transport_key(missed), missed, clock()),)

    submitted = service.repair_once()
    service.drain()

    assert submitted == 1
    assert service.registry.snapshot().records[0].record == missed
    assert service.diagnostics.repair_runs == 1
    assert service.diagnostics.repair_records == 1


def test_repair_skips_known_versions_so_rate_limited_tail_eventually_converges() -> None:
    clock = FakeClock()
    records = tuple(
        make_record(kind, node_id="node-a", ttl_ms=10_000)
        for kind in (RecordKind.PROFILE, RecordKind.STATUS, RecordKind.OFFERING)
    )
    spy = SpyTransport(tuple(ReceivedRecord(transport_key(record), record, clock()) for record in records))
    service = make_service(clock, transport=spy, max_records_per_peer_per_second=2)
    service.start(background=False)
    service.drain()
    assert len(service.registry.snapshot().records) == 2

    clock.advance(1.1)
    submitted = service.repair_once()
    service.drain()

    assert submitted == 1
    assert {entry.record.kind for entry in service.registry.snapshot().records} == {
        RecordKind.PROFILE,
        RecordKind.STATUS,
        RecordKind.OFFERING,
    }


def test_repair_submits_same_version_from_conflicting_boot_identity() -> None:
    clock = FakeClock()
    spy = SpyTransport()
    service = make_service(clock, transport=spy)
    service.start(background=False)
    accepted = make_record(
        RecordKind.STATUS,
        node_id="node-a",
        boot_id="boot-a",
        ttl_ms=10_000,
    )
    service.submit_received(ReceivedRecord(transport_key(accepted), accepted, clock()))
    service.drain()
    conflicting = make_record(
        RecordKind.STATUS,
        node_id="node-a",
        boot_id="boot-b",
        ttl_ms=10_000,
    )
    spy.replies = (ReceivedRecord(transport_key(conflicting), conflicting, clock()),)

    submitted = service.repair_once()
    service.drain()

    assert submitted == 1
    assert service.registry.diagnostics.identity_conflict == 1


def test_public_repair_cancellation_blocks_restart_and_skips_stale_query() -> None:
    clock = FakeClock()
    store = BlockingSnapshotStore("swarm-a", monotonic=clock)
    transport = SpyTransport()
    service = make_service(clock, transport=transport, registry=store)
    service.start(background=False)
    initial_query_count = transport.calls.count("query")
    store.block_snapshot = True
    results = []

    repair_thread = threading.Thread(target=lambda: results.append(service.repair_once()), daemon=True)
    repair_thread.start()
    assert store.snapshot_started.wait(1.0)
    try:
        service.stop()
        assert service.lifecycle_state is ServiceLifecycleState.STOPPING
        with pytest.raises(ServiceError, match="stopping"):
            service.start(background=False)
    finally:
        store.release_snapshot.set()
        repair_thread.join(1.0)
        if service.lifecycle_state is ServiceLifecycleState.RUNNING:
            service.stop()

    assert not repair_thread.is_alive()
    assert results == [0]
    assert transport.calls.count("query") == initial_query_count
    assert service.lifecycle_state is ServiceLifecycleState.STOPPED


class BlockingRepairTransport(SpyTransport):
    def __init__(self) -> None:
        super().__init__()
        self.query_count = 0
        self.repair_started = threading.Event()
        self.release_repair = threading.Event()

    def query_records(self, pattern: str):
        self.query_count += 1
        self.calls.append("query")
        if self.query_count > 1:
            self.repair_started.set()
            self.release_repair.wait(2.0)
        return self.replies


def test_blocked_repair_query_does_not_block_priority_liveness_processing() -> None:
    clock = FakeClock()
    transport = BlockingRepairTransport()
    service = make_service(clock, transport=transport, repair_interval_seconds=0.01)
    service.start(background=True)
    try:
        assert transport.repair_started.wait(1.0)
        service.submit_liveness(
            LivenessEvent(LivenessKind.PUT, "swarm-a", "node-a", 1, "boot-node-a-1", clock())
        )
        deadline = time.monotonic() + 0.5
        while service.peer_state("node-a") is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert service.peer_state("node-a").state is PeerHealthState.ALIVE
    finally:
        transport.release_repair.set()
        service.stop()


def test_cancelled_public_repair_cannot_enqueue_reply_after_stop() -> None:
    clock = FakeClock()
    record = make_record(RecordKind.STATUS, node_id="node-a", ttl_ms=10_000)
    transport = BlockingRepairTransport()
    service = make_service(clock, transport=transport, shutdown_timeout_seconds=0.02)
    service.start(background=False)
    transport.replies = (ReceivedRecord(transport_key(record), record, clock()),)
    results = []
    repair_thread = threading.Thread(target=lambda: results.append(service.repair_once()), daemon=True)
    repair_thread.start()
    assert transport.repair_started.wait(1.0)

    service.stop()
    assert service.lifecycle_state is ServiceLifecycleState.STOPPING
    transport.release_repair.set()
    repair_thread.join(1.0)

    assert not repair_thread.is_alive()
    assert results == [0]
    assert service.lifecycle_state is ServiceLifecycleState.STOPPED
    transport.replies = ()
    service.start(background=False)
    service.drain()
    assert service.registry.snapshot().records == ()
    service.stop()


def test_restart_rejected_until_timed_out_repair_worker_exits() -> None:
    clock = FakeClock()
    transport = BlockingRepairTransport()
    service = make_service(
        clock,
        transport=transport,
        repair_interval_seconds=0.01,
        shutdown_timeout_seconds=0.02,
    )
    service.start(background=True)
    assert transport.repair_started.wait(1.0)

    started_at = time.monotonic()
    service.stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.25
    assert service.started is False
    assert service.lifecycle_state is ServiceLifecycleState.STOPPING
    with pytest.raises(ServiceError, match="stopping"):
        service.start(background=False)

    transport.release_repair.set()
    deadline = time.monotonic() + 1.0
    while service.lifecycle_state is ServiceLifecycleState.STOPPING and time.monotonic() < deadline:
        time.sleep(0.005)
        service.stop()

    assert service.lifecycle_state is ServiceLifecycleState.STOPPED
    assert service.diagnostics.repair_runs == 0
    service.start(background=False)
    assert service.lifecycle_state is ServiceLifecycleState.RUNNING
    service.stop()
