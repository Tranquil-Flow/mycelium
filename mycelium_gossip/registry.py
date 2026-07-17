from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .schema import RecordEnvelope, RecordKind, logical_record_key


LogicalKey = Tuple[str, ...]


class ApplyStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"
    IDENTITY_CONFLICT = "identity_conflict"
    WRONG_SWARM = "wrong_swarm"
    CAPACITY_REJECTED = "capacity_rejected"


class EventKind(str, Enum):
    ACCEPTED = "accepted"
    EXPIRED = "expired"


@dataclass(frozen=True)
class RegistryEvent:
    kind: EventKind
    key: LogicalKey
    record: RecordEnvelope
    generation: int
    at_monotonic: float


@dataclass(frozen=True)
class ApplyResult:
    status: ApplyStatus
    event: Optional[RegistryEvent] = None
    reason: str = ""


@dataclass(frozen=True)
class RecordSnapshot:
    key: LogicalKey
    record: RecordEnvelope
    accepted_at_monotonic: float
    expires_at_monotonic: float
    expired: bool


@dataclass(frozen=True)
class RegistrySnapshot:
    swarm_id: str
    generation: int
    created_at_monotonic: float
    records: Tuple[RecordSnapshot, ...]


@dataclass(frozen=True)
class RegistryDiagnostics:
    accepted: int = 0
    duplicate: int = 0
    stale: int = 0
    conflict: int = 0
    identity_conflict: int = 0
    wrong_swarm: int = 0
    capacity_rejected: int = 0
    expired: int = 0
    callback_failures: int = 0


@dataclass
class _StoredRecord:
    key: LogicalKey
    record: RecordEnvelope
    accepted_at_monotonic: float
    expires_at_monotonic: float
    expired: bool = False


class VersionedRecordStore:
    """Thread-safe, transport-neutral evidence registry.

    Ordering is application-owned: `(incarnation, sequence)` wins. Receipt time only
    controls local freshness; replay and duplicates never extend a record's lifetime.
    """

    def __init__(
        self,
        swarm_id: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        max_peers: int = 256,
        max_links_per_origin: int = 1_024,
        max_records: int = 16_384,
    ) -> None:
        if max_peers < 1 or max_links_per_origin < 1 or max_records < 1:
            raise ValueError("registry limits must be positive")
        self.swarm_id = swarm_id
        self._clock = monotonic
        self._max_peers = max_peers
        self._max_links_per_origin = max_links_per_origin
        self._max_records = max_records
        self._records: Dict[LogicalKey, _StoredRecord] = {}
        self._origin_identity: Dict[str, Tuple[int, str]] = {}
        self._generation = 0
        self._counters: Counter[str] = Counter()
        self._subscribers: List[Callable[[RegistryEvent], None]] = []
        self._lock = threading.RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def diagnostics(self) -> RegistryDiagnostics:
        with self._lock:
            return RegistryDiagnostics(**{field: self._counters[field] for field in RegistryDiagnostics.__dataclass_fields__})

    def subscribe(self, callback: Callable[[RegistryEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self, events: Iterable[RegistryEvent]) -> None:
        for event in events:
            with self._lock:
                subscribers = tuple(self._subscribers)
            for callback in subscribers:
                try:
                    callback(event)
                except Exception:
                    with self._lock:
                        self._counters["callback_failures"] += 1

    def _result(self, status: ApplyStatus, reason: str = "") -> ApplyResult:
        with self._lock:
            self._counters[status.value] += 1
        return ApplyResult(status=status, reason=reason)

    def _known_origins(self) -> set[str]:
        return {entry.record.origin_node_id for entry in self._records.values()}

    def _link_count(self, origin: str) -> int:
        return sum(
            1
            for entry in self._records.values()
            if entry.record.origin_node_id == origin and entry.record.kind is RecordKind.LINK
        )

    def _expire_entry(self, entry: _StoredRecord, now: float) -> RegistryEvent:
        entry.expired = True
        self._generation += 1
        self._counters["expired"] += 1
        return RegistryEvent(
            kind=EventKind.EXPIRED,
            key=entry.key,
            record=entry.record,
            generation=self._generation,
            at_monotonic=now,
        )

    def _retire_older_incarnation(self, origin: str, incarnation: int, now: float) -> List[RegistryEvent]:
        events: List[RegistryEvent] = []
        for entry in self._records.values():
            if (
                entry.record.origin_node_id == origin
                and entry.record.incarnation < incarnation
                and not entry.expired
            ):
                events.append(self._expire_entry(entry, now))
        return events

    def apply(self, record: RecordEnvelope) -> ApplyResult:
        now = self._clock()
        events: List[RegistryEvent] = []
        with self._lock:
            if record.swarm_id != self.swarm_id:
                return self._result(ApplyStatus.WRONG_SWARM, "record belongs to another swarm")

            identity = self._origin_identity.get(record.origin_node_id)
            if identity is not None:
                highest_incarnation, boot_id = identity
                if record.incarnation < highest_incarnation:
                    return self._result(ApplyStatus.STALE, "origin incarnation is stale")
                if record.incarnation == highest_incarnation and record.boot_id != boot_id:
                    return self._result(
                        ApplyStatus.IDENTITY_CONFLICT,
                        "same node/incarnation claimed by multiple boot IDs",
                    )

            key = logical_record_key(record)
            current = self._records.get(key)
            if current is not None:
                current_version = (current.record.incarnation, current.record.sequence)
                candidate_version = (record.incarnation, record.sequence)
                if candidate_version < current_version:
                    return self._result(ApplyStatus.STALE, "record version is stale")
                if candidate_version == current_version:
                    if record.payload_hash == current.record.payload_hash and record.boot_id == current.record.boot_id:
                        return self._result(ApplyStatus.DUPLICATE)
                    return self._result(ApplyStatus.CONFLICT, "same version carries different evidence")
            else:
                origins = self._known_origins()
                if record.origin_node_id not in origins and len(origins) >= self._max_peers:
                    return self._result(ApplyStatus.CAPACITY_REJECTED, "peer cardinality limit reached")
                if len(self._records) >= self._max_records:
                    return self._result(ApplyStatus.CAPACITY_REJECTED, "record cardinality limit reached")
                if record.kind is RecordKind.LINK and self._link_count(record.origin_node_id) >= self._max_links_per_origin:
                    return self._result(ApplyStatus.CAPACITY_REJECTED, "link cardinality limit reached")

            if identity is None or record.incarnation > identity[0]:
                events.extend(self._retire_older_incarnation(record.origin_node_id, record.incarnation, now))
                self._origin_identity[record.origin_node_id] = (record.incarnation, record.boot_id)

            self._records[key] = _StoredRecord(
                key=key,
                record=record,
                accepted_at_monotonic=now,
                expires_at_monotonic=now + record.ttl_ms / 1_000.0,
                expired=False,
            )
            self._generation += 1
            self._counters["accepted"] += 1
            accepted = RegistryEvent(
                kind=EventKind.ACCEPTED,
                key=key,
                record=record,
                generation=self._generation,
                at_monotonic=now,
            )
            events.append(accepted)
            result = ApplyResult(status=ApplyStatus.ACCEPTED, event=accepted)

        self._notify(events)
        return result

    def expire_due(self) -> Tuple[RegistryEvent, ...]:
        now = self._clock()
        with self._lock:
            events = tuple(
                self._expire_entry(entry, now)
                for entry in self._records.values()
                if not entry.expired and now >= entry.expires_at_monotonic
            )
        self._notify(events)
        return events

    def snapshot(self, *, include_expired: bool = False) -> RegistrySnapshot:
        now = self._clock()
        with self._lock:
            records = tuple(
                RecordSnapshot(
                    key=entry.key,
                    record=entry.record,
                    accepted_at_monotonic=entry.accepted_at_monotonic,
                    expires_at_monotonic=entry.expires_at_monotonic,
                    expired=entry.expired,
                )
                for key, entry in sorted(self._records.items())
                if include_expired or not entry.expired
            )
            return RegistrySnapshot(
                swarm_id=self.swarm_id,
                generation=self._generation,
                created_at_monotonic=now,
                records=records,
            )

    def fresh_record(self, key: LogicalKey) -> Optional[RecordEnvelope]:
        with self._lock:
            entry = self._records.get(key)
            if entry is None or entry.expired:
                return None
            return entry.record
