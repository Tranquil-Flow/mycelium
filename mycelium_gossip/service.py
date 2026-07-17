from __future__ import annotations

import secrets
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .registry import RegistryEvent, VersionedRecordStore
from .schema import RecordEnvelope, RecordKind, logical_record_key, transport_key
from .transport import (
    BoundedCoalescingInbox,
    GossipTransport,
    LivenessEvent,
    LivenessKind,
    ReceivedRecord,
)


class ServiceError(ValueError):
    pass


class ServiceLifecycleState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class PeerHealthState(str, Enum):
    ALIVE = "alive"
    SUSPECT = "suspect"
    DEAD = "dead"
    IDENTITY_CONFLICT = "identity_conflict"


class FailureScope(str, Enum):
    EDGE = "edge"
    OFFERING = "offering"
    PEER = "peer"


@dataclass(frozen=True)
class FailureObservation:
    route_id: str
    route_generation: int
    src_node_id: str
    src_endpoint_id: str
    dst_node_id: str
    dst_endpoint_id: str
    offering_id: Optional[str]
    failure_kind: str
    scope: FailureScope
    probe_correlation_id: str

    def __post_init__(self) -> None:
        if not self.route_id or self.route_generation < 0:
            raise ServiceError("route identity is invalid")
        if not self.src_node_id or not self.dst_node_id or not self.failure_kind:
            raise ServiceError("failure observation lacks node or failure identity")
        if self.scope is FailureScope.EDGE and (not self.src_endpoint_id or not self.dst_endpoint_id):
            raise ServiceError("edge failure requires endpoint IDs")
        if self.scope is FailureScope.OFFERING and not self.offering_id:
            raise ServiceError("offering_id is required for offering scope")
        if not self.probe_correlation_id:
            raise ServiceError("probe_correlation_id is required")


@dataclass(frozen=True)
class ActiveRouteAtRisk:
    observation: FailureObservation
    observed_at_monotonic: float
    quarantine_until_monotonic: float


@dataclass(frozen=True)
class PeerState:
    node_id: str
    incarnation: int
    boot_id: str
    state: PeerHealthState
    liveness_present: bool
    changed_at_monotonic: float
    suspect_since_monotonic: Optional[float] = None


@dataclass(frozen=True)
class PeerStateChanged:
    previous: Optional[PeerState]
    current: PeerState
    reason: str


@dataclass(frozen=True)
class EvidenceChanged:
    registry_event: RegistryEvent


@dataclass(frozen=True)
class QuarantineExpired:
    key: Tuple[str, ...]
    expired_at_monotonic: float


@dataclass(frozen=True)
class QuarantineEntry:
    key: Tuple[str, ...]
    scope: FailureScope
    observation: FailureObservation
    created_at_monotonic: float
    expires_at_monotonic: float


@dataclass(frozen=True)
class ConvergencePeer:
    node_id: str
    incarnation: int
    boot_id: str
    state: PeerHealthState
    missing_kinds: Tuple[RecordKind, ...]


@dataclass(frozen=True)
class ConvergenceReport:
    converged: bool
    snapshot_generation: int
    peers: Tuple[ConvergencePeer, ...]


@dataclass(frozen=True)
class ServiceDiagnostics:
    submitted_records: int = 0
    ingested_records: int = 0
    invalid_transport_keys: int = 0
    rate_limited_records: int = 0
    submitted_liveness: int = 0
    stale_liveness_events: int = 0
    identity_conflicts: int = 0
    failures_reported: int = 0
    event_callback_failures: int = 0
    worker_failures: int = 0
    repair_runs: int = 0
    repair_records: int = 0
    repair_failures: int = 0
    teardown_failures: int = 0


@dataclass(frozen=True)
class _RecoveryChallenge:
    node_id: str
    incarnation: int
    boot_id: str
    nonce: str
    issued_at_monotonic: float
    suspect_since_monotonic: float


EventCallback = Callable[[Any], None]


class GossipService:
    """Coordinates evidence transport without making routing/allocation decisions."""

    def __init__(
        self,
        *,
        swarm_id: str,
        node_id: str,
        incarnation: int,
        boot_id: str,
        transport: GossipTransport,
        registry: VersionedRecordStore,
        monotonic: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        suspicion_grace_seconds: float = 4.0,
        quarantine_seconds: float = 5.0,
        worker_poll_seconds: float = 0.05,
        repair_interval_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 1.0,
        max_records_per_peer_per_second: int = 100,
        inbox: Optional[BoundedCoalescingInbox] = None,
    ) -> None:
        if registry.swarm_id != swarm_id:
            raise ServiceError("registry swarm does not match service swarm")
        if incarnation < 1 or suspicion_grace_seconds <= 0 or quarantine_seconds <= 0:
            raise ServiceError("service timing and incarnation must be positive")
        if worker_poll_seconds <= 0 or repair_interval_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ServiceError("worker, repair, and shutdown timing must be positive")
        if max_records_per_peer_per_second < 1:
            raise ServiceError("rate limit must be positive")
        self.swarm_id = swarm_id
        self.node_id = node_id
        self.incarnation = incarnation
        self.boot_id = boot_id
        self.transport = transport
        self.registry = registry
        self._clock = monotonic
        self._nonce_factory = nonce_factory
        self._suspicion_grace_seconds = suspicion_grace_seconds
        self._default_quarantine_seconds = quarantine_seconds
        self._worker_poll_seconds = worker_poll_seconds
        self._repair_interval_seconds = repair_interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._rate_limit = max_records_per_peer_per_second
        self._inbox = inbox or BoundedCoalescingInbox()
        self._record_times: Dict[str, Deque[float]] = defaultdict(deque)
        self._peers: Dict[str, PeerState] = {}
        self._challenges: Dict[str, _RecoveryChallenge] = {}
        self._quarantines: Dict[Tuple[str, ...], QuarantineEntry] = {}
        self._event_callbacks: List[EventCallback] = []
        self._unsubscribe_callbacks: List[Callable[[], None]] = []
        self._counters: Counter[str] = Counter()
        self._lifecycle_state = ServiceLifecycleState.STOPPED
        self._stop_event: Optional[threading.Event] = None
        self._worker: Optional[threading.Thread] = None
        self._repair_worker: Optional[threading.Thread] = None
        self._active_repairs: Dict[threading.Event, int] = {}
        self._transport_active = False
        self._teardown_complete = True
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self.registry.subscribe(lambda event: self._emit(EvidenceChanged(event)))

    @property
    def diagnostics(self) -> ServiceDiagnostics:
        with self._lock:
            return ServiceDiagnostics(**{field: self._counters[field] for field in ServiceDiagnostics.__dataclass_fields__})

    @property
    def started(self) -> bool:
        with self._lock:
            return self._lifecycle_state is ServiceLifecycleState.RUNNING

    @property
    def lifecycle_state(self) -> ServiceLifecycleState:
        with self._lock:
            self._reap_finished_workers_locked()
            return self._lifecycle_state

    def _reap_finished_workers_locked(self) -> None:
        if self._lifecycle_state is not ServiceLifecycleState.STOPPING or not self._teardown_complete:
            return
        worker_alive = self._worker is not None and self._worker.is_alive()
        repair_alive = self._repair_worker is not None and self._repair_worker.is_alive()
        if worker_alive or repair_alive or self._active_repairs:
            return
        self._worker = None
        self._repair_worker = None
        self._stop_event = None
        self._inbox.drain()
        self._record_times.clear()
        self._lifecycle_state = ServiceLifecycleState.STOPPED

    def _accepts_run(self, stop_event: threading.Event, *, allow_starting: bool = False) -> bool:
        allowed_states = {ServiceLifecycleState.RUNNING}
        if allow_starting:
            allowed_states.add(ServiceLifecycleState.STARTING)
        with self._lock:
            return (
                self._stop_event is stop_event
                and self._lifecycle_state in allowed_states
                and not stop_event.is_set()
            )

    def _submit_received_for_run(self, stop_event: threading.Event, received: ReceivedRecord) -> bool:
        with self._lock:
            if not self._accepts_run(stop_event, allow_starting=True):
                return False
            return self.submit_received(received)

    def _submit_liveness_for_run(self, stop_event: threading.Event, event: LivenessEvent) -> bool:
        with self._lock:
            if not self._accepts_run(stop_event, allow_starting=True):
                return False
            return self.submit_liveness(event)

    def subscribe_events(self, callback: EventCallback) -> Callable[[], None]:
        with self._lock:
            self._event_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._event_callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _emit(self, event: Any) -> None:
        with self._lock:
            callbacks = tuple(self._event_callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                with self._lock:
                    self._counters["event_callback_failures"] += 1

    def start(self, *, background: bool = True) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._reap_finished_workers_locked()
                if self._lifecycle_state is ServiceLifecycleState.RUNNING:
                    return
                if self._lifecycle_state is ServiceLifecycleState.STOPPING:
                    raise ServiceError("service is stopping; old workers must exit before restart")
                if self._lifecycle_state is ServiceLifecycleState.STARTING:
                    raise ServiceError("service start is already in progress")
                self._lifecycle_state = ServiceLifecycleState.STARTING
                stop_event = threading.Event()
                self._stop_event = stop_event
                self._worker = None
                self._repair_worker = None
                self._unsubscribe_callbacks = []
                self._transport_active = False
                self._teardown_complete = False

            unsubscribe_callbacks: List[Callable[[], None]] = []
            worker: Optional[threading.Thread] = None
            repair_worker: Optional[threading.Thread] = None
            transport_started = False
            worker_started = False
            repair_worker_started = False
            try:
                self.transport.start()
                transport_started = True
                with self._lock:
                    self._transport_active = True

                def on_record(received: ReceivedRecord) -> None:
                    self._submit_received_for_run(stop_event, received)

                def on_liveness(event: LivenessEvent) -> None:
                    self._submit_liveness_for_run(stop_event, event)

                unsubscribe_callbacks.append(
                    self.transport.subscribe_records(on_record)
                )
                unsubscribe_callbacks.append(
                    self.transport.subscribe_liveness(
                        on_liveness,
                        history=True,
                    )
                )
                for received in self.transport.query_records(f"mycelium/{self.swarm_id}/**"):
                    if not self._accepts_run(stop_event, allow_starting=True):
                        break
                    self.submit_received(received)
                self.drain()
                self.transport.declare_liveness(
                    self.swarm_id,
                    self.node_id,
                    self.incarnation,
                    self.boot_id,
                )
                self.drain()
                if background:
                    worker = threading.Thread(
                        target=self._worker_loop,
                        args=(stop_event,),
                        name=f"mycelium-gossip-{self.node_id}",
                        daemon=True,
                    )
                    repair_worker = threading.Thread(
                        target=self._repair_loop,
                        args=(stop_event,),
                        name=f"mycelium-gossip-repair-{self.node_id}",
                        daemon=True,
                    )
                with self._lock:
                    self._unsubscribe_callbacks = list(unsubscribe_callbacks)
                    self._worker = worker
                    self._repair_worker = repair_worker
                if worker is not None:
                    worker.start()
                    worker_started = True
                if repair_worker is not None:
                    repair_worker.start()
                    repair_worker_started = True
                with self._lock:
                    self._lifecycle_state = ServiceLifecycleState.RUNNING
            except Exception:
                stop_event.set()
                for unsubscribe in reversed(unsubscribe_callbacks):
                    try:
                        unsubscribe()
                    except Exception:
                        with self._lock:
                            self._counters["teardown_failures"] += 1
                transport_stopped = not transport_started
                if transport_started:
                    try:
                        self.transport.stop()
                        transport_stopped = True
                    except Exception:
                        transport_stopped = False
                        with self._lock:
                            self._counters["teardown_failures"] += 1
                deadline = time.monotonic() + self._shutdown_timeout_seconds
                for candidate, candidate_started in (
                    (worker, worker_started),
                    (repair_worker, repair_worker_started),
                ):
                    if candidate_started and candidate is not None and candidate is not threading.current_thread():
                        try:
                            candidate.join(timeout=max(0.0, deadline - time.monotonic()))
                        except Exception:
                            with self._lock:
                                self._counters["teardown_failures"] += 1
                with self._lock:
                    self._unsubscribe_callbacks = []
                    self._transport_active = not transport_stopped
                    self._teardown_complete = transport_stopped
                    self._worker = worker if worker_started and worker is not None and worker.is_alive() else None
                    self._repair_worker = (
                        repair_worker
                        if repair_worker_started and repair_worker is not None and repair_worker.is_alive()
                        else None
                    )
                    self._lifecycle_state = ServiceLifecycleState.STOPPING
                    self._reap_finished_workers_locked()
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            deadline = time.monotonic() + self._shutdown_timeout_seconds
            with self._lock:
                self._reap_finished_workers_locked()
                if self._lifecycle_state is ServiceLifecycleState.STOPPED:
                    return
                if self._lifecycle_state is ServiceLifecycleState.STARTING:
                    raise ServiceError("service cannot stop reentrantly while starting")
                self._lifecycle_state = ServiceLifecycleState.STOPPING
                stop_event = self._stop_event
                if stop_event is not None:
                    stop_event.set()
                unsubscribe_callbacks = list(self._unsubscribe_callbacks)
                self._unsubscribe_callbacks = []
                transport_active = self._transport_active
                worker = self._worker
                repair_worker = self._repair_worker

            for unsubscribe in reversed(unsubscribe_callbacks):
                try:
                    unsubscribe()
                except Exception:
                    with self._lock:
                        self._counters["teardown_failures"] += 1

            transport_error: Optional[Exception] = None
            transport_stopped = not transport_active
            if transport_active:
                try:
                    self.transport.stop()
                    transport_stopped = True
                except Exception as error:
                    transport_error = error
                    with self._lock:
                        self._counters["teardown_failures"] += 1

            with self._lock:
                self._transport_active = not transport_stopped
                self._teardown_complete = transport_stopped

            for candidate in (worker, repair_worker):
                if candidate is not None and candidate is not threading.current_thread():
                    try:
                        candidate.join(timeout=max(0.0, deadline - time.monotonic()))
                    except Exception:
                        with self._lock:
                            self._counters["teardown_failures"] += 1

            with self._lock:
                self._worker = worker if worker is not None and worker.is_alive() else None
                self._repair_worker = (
                    repair_worker if repair_worker is not None and repair_worker.is_alive() else None
                )
                self._reap_finished_workers_locked()

            if transport_error is not None:
                raise transport_error

    def publish_record(self, record: RecordEnvelope) -> None:
        if not self.started:
            raise ServiceError("service is not started")
        if record.origin_node_id != self.node_id:
            raise ServiceError("service may only publish records owned by its local node")
        result = self.registry.apply(record)
        if result.status.value == "accepted":
            self.transport.publish_record(record)

    def submit_received(self, received: ReceivedRecord) -> bool:
        now = self._clock()
        origin = received.record.origin_node_id
        with self._lock:
            timestamps = self._record_times[origin]
            cutoff = now - 1.0
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._rate_limit:
                self._counters["rate_limited_records"] += 1
                return False
            timestamps.append(now)
            self._counters["submitted_records"] += 1
        return self._inbox.put_record(received)

    def submit_liveness(self, event: LivenessEvent) -> bool:
        with self._lock:
            self._counters["submitted_liveness"] += 1
        return self._inbox.put_liveness(event)

    def _process_received(self, received: ReceivedRecord) -> None:
        if received.transport_key != transport_key(received.record):
            with self._lock:
                self._counters["invalid_transport_keys"] += 1
            return
        result = self.registry.apply(received.record)
        if result.status.value == "accepted":
            with self._lock:
                self._counters["ingested_records"] += 1

    def _replace_peer_locked(self, current: PeerState, reason: str) -> PeerStateChanged:
        previous = self._peers.get(current.node_id)
        self._peers[current.node_id] = current
        return PeerStateChanged(previous, current, reason)

    def _process_liveness_locked(self, event: LivenessEvent, now: float) -> Optional[PeerStateChanged]:
        current = self._peers.get(event.node_id)
        if current is not None and event.incarnation < current.incarnation:
            self._counters["stale_liveness_events"] += 1
            return None
        if current is not None and event.incarnation == current.incarnation and event.boot_id != current.boot_id:
            conflict = PeerState(
                node_id=event.node_id,
                incarnation=current.incarnation,
                boot_id=current.boot_id,
                state=PeerHealthState.IDENTITY_CONFLICT,
                liveness_present=current.liveness_present or event.kind is LivenessKind.PUT,
                changed_at_monotonic=now,
                suspect_since_monotonic=current.suspect_since_monotonic,
            )
            self._counters["identity_conflicts"] += 1
            return self._replace_peer_locked(conflict, "same incarnation claimed by another boot ID")

        if event.kind is LivenessKind.PUT:
            if current is None or event.incarnation > current.incarnation:
                updated = PeerState(
                    event.node_id,
                    event.incarnation,
                    event.boot_id,
                    PeerHealthState.ALIVE,
                    True,
                    now,
                    None,
                )
                return self._replace_peer_locked(updated, "liveness put")
            if current.state in {PeerHealthState.SUSPECT, PeerHealthState.DEAD}:
                updated = PeerState(
                    current.node_id,
                    current.incarnation,
                    current.boot_id,
                    current.state,
                    True,
                    current.changed_at_monotonic,
                    current.suspect_since_monotonic,
                )
                return self._replace_peer_locked(updated, "liveness returned; application challenge required")
            if not current.liveness_present:
                updated = PeerState(
                    current.node_id,
                    current.incarnation,
                    current.boot_id,
                    current.state,
                    True,
                    current.changed_at_monotonic,
                    current.suspect_since_monotonic,
                )
                return self._replace_peer_locked(updated, "liveness refreshed")
            return None

        if current is None or event.incarnation != current.incarnation or event.boot_id != current.boot_id:
            self._counters["stale_liveness_events"] += 1
            return None
        if current.state is PeerHealthState.IDENTITY_CONFLICT:
            return None
        if current.state is PeerHealthState.ALIVE:
            updated = PeerState(
                current.node_id,
                current.incarnation,
                current.boot_id,
                PeerHealthState.SUSPECT,
                False,
                now,
                now,
            )
            return self._replace_peer_locked(updated, "liveness delete")
        if current.liveness_present:
            updated = PeerState(
                current.node_id,
                current.incarnation,
                current.boot_id,
                current.state,
                False,
                current.changed_at_monotonic,
                current.suspect_since_monotonic,
            )
            return self._replace_peer_locked(updated, "liveness absent")
        return None

    def _process_liveness(self, event: LivenessEvent) -> None:
        if event.swarm_id != self.swarm_id:
            with self._lock:
                self._counters["stale_liveness_events"] += 1
            return
        now = self._clock()
        with self._lock:
            change = self._process_liveness_locked(event, now)
        if change is not None:
            self._emit(change)

    def _process_item(self, item: Any) -> None:
        if isinstance(item, ReceivedRecord):
            self._process_received(item)
        elif isinstance(item, LivenessEvent):
            self._process_liveness(item)

    def drain(self, limit: Optional[int] = None) -> int:
        items = self._inbox.drain(limit)
        for item in items:
            self._process_item(item)
        self.tick()
        return len(items)

    def _worker_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                item = self._inbox.pop(timeout=self._worker_poll_seconds)
                if item is not None and not stop_event.is_set():
                    self._process_item(item)
                if not stop_event.is_set():
                    self.tick()
            except Exception:
                with self._lock:
                    self._counters["worker_failures"] += 1

    def repair_once(self) -> int:
        """Query all peers for current records and enqueue them through normal intake."""
        with self._lock:
            if self._lifecycle_state is not ServiceLifecycleState.RUNNING or self._stop_event is None:
                raise ServiceError("service is not started")
            stop_event = self._stop_event
        return self._run_tracked_repair(stop_event)

    def _run_tracked_repair(self, stop_event: threading.Event) -> int:
        with self._lock:
            if not self._accepts_run(stop_event):
                return 0
            self._active_repairs[stop_event] = self._active_repairs.get(stop_event, 0) + 1
        try:
            return self._repair_once_for_run(stop_event)
        finally:
            with self._lock:
                remaining = self._active_repairs.get(stop_event, 0) - 1
                if remaining > 0:
                    self._active_repairs[stop_event] = remaining
                else:
                    self._active_repairs.pop(stop_event, None)
                self._reap_finished_workers_locked()

    def _repair_once_for_run(self, stop_event: threading.Event) -> int:
        if not self._accepts_run(stop_event):
            return 0
        snapshot = self.registry.snapshot()
        if not self._accepts_run(stop_event):
            return 0
        known = {
            entry.key: (
                entry.record.incarnation,
                entry.record.boot_id,
                entry.record.sequence,
                entry.record.payload_hash,
            )
            for entry in snapshot.records
        }
        submitted = 0
        for received in self.transport.query_records(f"mycelium/{self.swarm_id}/**"):
            if not self._accepts_run(stop_event):
                return 0
            record = received.record
            fingerprint = (record.incarnation, record.boot_id, record.sequence, record.payload_hash)
            if known.get(logical_record_key(record)) == fingerprint:
                continue
            accepted = self._submit_received_for_run(stop_event, received)
            if accepted:
                submitted += 1
            elif not self._accepts_run(stop_event):
                return 0
        with self._lock:
            if not self._accepts_run(stop_event):
                return 0
            self._counters["repair_runs"] += 1
            self._counters["repair_records"] += submitted
        return submitted

    def _repair_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self._repair_interval_seconds):
            try:
                self._run_tracked_repair(stop_event)
            except Exception:
                if self._accepts_run(stop_event):
                    with self._lock:
                        self._counters["repair_failures"] += 1

    def tick(self) -> None:
        now = self._clock()
        self.registry.expire_due()
        peer_events: List[PeerStateChanged] = []
        quarantine_events: List[QuarantineExpired] = []
        with self._lock:
            for node_id, current in tuple(self._peers.items()):
                if (
                    current.state is PeerHealthState.SUSPECT
                    and current.suspect_since_monotonic is not None
                    and now - current.suspect_since_monotonic >= self._suspicion_grace_seconds
                ):
                    peer_events.append(
                        self._replace_peer_locked(
                            PeerState(
                                current.node_id,
                                current.incarnation,
                                current.boot_id,
                                PeerHealthState.DEAD,
                                current.liveness_present,
                                now,
                                current.suspect_since_monotonic,
                            ),
                            "suspicion grace expired",
                        )
                    )
            for key, entry in tuple(self._quarantines.items()):
                if now >= entry.expires_at_monotonic:
                    self._quarantines.pop(key, None)
                    quarantine_events.append(QuarantineExpired(key, now))
        for event in (*peer_events, *quarantine_events):
            self._emit(event)

    def peer_state(self, node_id: str) -> Optional[PeerState]:
        with self._lock:
            return self._peers.get(node_id)

    def peer_states_snapshot(self) -> Tuple[PeerState, ...]:
        """Return deterministic frozen membership evidence for view construction."""
        with self._lock:
            return tuple(self._peers[node_id] for node_id in sorted(self._peers))

    def issue_recovery_challenge(self, node_id: str) -> str:
        now = self._clock()
        with self._lock:
            peer = self._peers.get(node_id)
            if (
                peer is None
                or peer.state not in {PeerHealthState.SUSPECT, PeerHealthState.DEAD}
                or not peer.liveness_present
                or peer.suspect_since_monotonic is None
            ):
                raise ServiceError("peer is not eligible for same-incarnation recovery challenge")
            nonce = self._nonce_factory()
            self._challenges[node_id] = _RecoveryChallenge(
                node_id,
                peer.incarnation,
                peer.boot_id,
                nonce,
                now,
                peer.suspect_since_monotonic,
            )
            return nonce

    def confirm_recovery(self, node_id: str, nonce: str, *, application_reachable: bool = True) -> bool:
        now = self._clock()
        with self._lock:
            challenge = self._challenges.get(node_id)
            peer = self._peers.get(node_id)
            if (
                challenge is None
                or peer is None
                or nonce != challenge.nonce
                or not application_reachable
                or not peer.liveness_present
                or (peer.incarnation, peer.boot_id) != (challenge.incarnation, challenge.boot_id)
            ):
                return False
        fresh_status = False
        for entry in self.registry.snapshot().records:
            record = entry.record
            if (
                record.origin_node_id == node_id
                and record.kind is RecordKind.STATUS
                and record.incarnation == challenge.incarnation
                and record.boot_id == challenge.boot_id
                and entry.accepted_at_monotonic > challenge.suspect_since_monotonic
            ):
                fresh_status = True
                break
        if not fresh_status:
            return False
        with self._lock:
            current_challenge = self._challenges.get(node_id)
            current = self._peers.get(node_id)
            if (
                current is None
                or current_challenge != challenge
                or current.state not in {PeerHealthState.SUSPECT, PeerHealthState.DEAD}
                or not current.liveness_present
                or current.suspect_since_monotonic != challenge.suspect_since_monotonic
                or (current.incarnation, current.boot_id) != (challenge.incarnation, challenge.boot_id)
            ):
                return False
            recovered = PeerState(
                current.node_id,
                current.incarnation,
                current.boot_id,
                PeerHealthState.ALIVE,
                True,
                now,
                None,
            )
            self._challenges.pop(node_id, None)
            change = self._replace_peer_locked(recovered, "nonce challenge and fresh status confirmed")
        self._emit(change)
        return True

    def convergence_report(self, *, include_local: bool = True) -> ConvergenceReport:
        snapshot = self.registry.snapshot()
        kinds_by_identity: Dict[Tuple[str, int, str], set[RecordKind]] = defaultdict(set)
        for entry in snapshot.records:
            record = entry.record
            kinds_by_identity[(record.origin_node_id, record.incarnation, record.boot_id)].add(record.kind)
        with self._lock:
            states = tuple(self._peers.values())
        peers: List[ConvergencePeer] = []
        required = (RecordKind.PROFILE, RecordKind.STATUS)
        for peer in sorted(states, key=lambda item: item.node_id):
            if not include_local and peer.node_id == self.node_id:
                continue
            if peer.state is not PeerHealthState.ALIVE:
                missing = required
            else:
                present = kinds_by_identity[(peer.node_id, peer.incarnation, peer.boot_id)]
                missing = tuple(kind for kind in required if kind not in present)
            peers.append(
                ConvergencePeer(
                    peer.node_id,
                    peer.incarnation,
                    peer.boot_id,
                    peer.state,
                    missing,
                )
            )
        converged = bool(peers) and all(peer.state is PeerHealthState.ALIVE and not peer.missing_kinds for peer in peers)
        return ConvergenceReport(converged, snapshot.generation, tuple(peers))

    @staticmethod
    def _quarantine_key(observation: FailureObservation) -> Tuple[str, ...]:
        if observation.scope is FailureScope.EDGE:
            return (
                FailureScope.EDGE.value,
                observation.src_node_id,
                observation.src_endpoint_id,
                observation.dst_node_id,
                observation.dst_endpoint_id,
            )
        if observation.scope is FailureScope.OFFERING:
            return (FailureScope.OFFERING.value, observation.dst_node_id, str(observation.offering_id))
        return (FailureScope.PEER.value, observation.dst_node_id)

    def report_failure(
        self,
        observation: FailureObservation,
        *,
        quarantine_seconds: Optional[float] = None,
    ) -> ActiveRouteAtRisk:
        now = self._clock()
        duration = self._default_quarantine_seconds if quarantine_seconds is None else quarantine_seconds
        if duration <= 0:
            raise ServiceError("quarantine duration must be positive")
        key = self._quarantine_key(observation)
        entry = QuarantineEntry(key, observation.scope, observation, now, now + duration)
        event = ActiveRouteAtRisk(observation, now, entry.expires_at_monotonic)
        with self._lock:
            self._quarantines[key] = entry
            self._counters["failures_reported"] += 1
        self._emit(event)
        return event

    def quarantine_snapshot(self) -> Tuple[QuarantineEntry, ...]:
        with self._lock:
            return tuple(self._quarantines[key] for key in sorted(self._quarantines))

    def is_edge_quarantined(self, src_node: str, src_endpoint: str, dst_node: str, dst_endpoint: str) -> bool:
        with self._lock:
            return (FailureScope.EDGE.value, src_node, src_endpoint, dst_node, dst_endpoint) in self._quarantines

    def is_offering_quarantined(self, node_id: str, offering_id: str) -> bool:
        with self._lock:
            return (FailureScope.OFFERING.value, node_id, offering_id) in self._quarantines

    def is_peer_quarantined(self, node_id: str) -> bool:
        with self._lock:
            return (FailureScope.PEER.value, node_id) in self._quarantines
