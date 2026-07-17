from __future__ import annotations

import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from .schema import RecordEnvelope, logical_record_key, transport_key


class TransportError(RuntimeError):
    pass


class LivenessKind(str, Enum):
    PUT = "put"
    DELETE = "delete"


@dataclass(frozen=True)
class ReceivedRecord:
    transport_key: str
    record: RecordEnvelope
    received_monotonic: float


@dataclass(frozen=True)
class LivenessEvent:
    kind: LivenessKind
    swarm_id: str
    node_id: str
    incarnation: int
    boot_id: str
    received_monotonic: float

    @property
    def identity(self) -> Tuple[str, int, str]:
        return (self.node_id, self.incarnation, self.boot_id)


Unsubscribe = Callable[[], None]
RecordCallback = Callable[[ReceivedRecord], None]
LivenessCallback = Callable[[LivenessEvent], None]


@runtime_checkable
class GossipTransport(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def publish_record(self, record: RecordEnvelope) -> None:
        ...

    def query_records(self, pattern: str) -> Tuple[ReceivedRecord, ...]:
        ...

    def subscribe_records(self, callback: RecordCallback) -> Unsubscribe:
        ...

    def declare_liveness(self, swarm_id: str, node_id: str, incarnation: int, boot_id: str) -> None:
        ...

    def subscribe_liveness(self, callback: LivenessCallback, *, history: bool = True) -> Unsubscribe:
        ...


@dataclass(frozen=True)
class MeshDiagnostics:
    published_records: int = 0
    callback_failures: int = 0
    liveness_puts: int = 0
    liveness_deletes: int = 0


class InMemoryMesh:
    """Deterministic transport emulator for contract and chaos tests."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._clock = monotonic
        self._transports: Dict[int, "InMemoryTransport"] = {}
        self._records: Dict[str, RecordEnvelope] = {}
        self._liveness: Dict[Tuple[str, int, str], Tuple[int, str]] = {}
        self._counters: Counter[str] = Counter()
        self._lock = threading.RLock()

    @property
    def diagnostics(self) -> MeshDiagnostics:
        with self._lock:
            return MeshDiagnostics(**{field: self._counters[field] for field in MeshDiagnostics.__dataclass_fields__})

    def _register(self, transport: "InMemoryTransport") -> None:
        with self._lock:
            self._transports[id(transport)] = transport

    def _unregister(self, transport: "InMemoryTransport") -> None:
        delete_event: Optional[LivenessEvent] = None
        with self._lock:
            self._transports.pop(id(transport), None)
            token = transport._liveness_identity
            if token is not None and token in self._liveness:
                swarm_id = self._liveness.pop(token)[1]
                delete_event = LivenessEvent(
                    kind=LivenessKind.DELETE,
                    swarm_id=swarm_id,
                    node_id=token[0],
                    incarnation=token[1],
                    boot_id=token[2],
                    received_monotonic=self._clock(),
                )
                self._counters["liveness_deletes"] += 1
            transports = tuple(self._transports.values())
        if delete_event is not None:
            self._deliver_liveness(transports, delete_event)

    def _safe_call(self, callback: Callable[[Any], None], value: Any) -> None:
        try:
            callback(value)
        except Exception:
            with self._lock:
                self._counters["callback_failures"] += 1

    def _deliver_record(self, transports: Tuple["InMemoryTransport", ...], received: ReceivedRecord) -> None:
        for transport in transports:
            for callback in transport._record_callbacks_snapshot():
                self._safe_call(callback, received)

    def _deliver_liveness(self, transports: Tuple["InMemoryTransport", ...], event: LivenessEvent) -> None:
        for transport in transports:
            for callback in transport._liveness_callbacks_snapshot():
                self._safe_call(callback, event)

    def _publish(self, record: RecordEnvelope) -> None:
        key = transport_key(record)
        with self._lock:
            current = self._records.get(key)
            if current is None or (record.incarnation, record.sequence) > (current.incarnation, current.sequence):
                self._records[key] = record
            elif (
                (record.incarnation, record.sequence) == (current.incarnation, current.sequence)
                and record.payload_hash == current.payload_hash
            ):
                self._records[key] = record
            self._counters["published_records"] += 1
            transports = tuple(self._transports.values())
        received = ReceivedRecord(key, record, self._clock())
        self._deliver_record(transports, received)

    @staticmethod
    def _matches(pattern: str, key: str) -> bool:
        expression = re.escape(pattern)
        expression = expression.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(expression, key) is not None

    def _query(self, pattern: str) -> Tuple[ReceivedRecord, ...]:
        now = self._clock()
        with self._lock:
            matches = tuple(
                ReceivedRecord(key, record, now)
                for key, record in sorted(self._records.items())
                if self._matches(pattern, key)
            )
        return matches

    def _declare_liveness(
        self,
        owner: "InMemoryTransport",
        swarm_id: str,
        node_id: str,
        incarnation: int,
        boot_id: str,
    ) -> None:
        identity = (node_id, incarnation, boot_id)
        with self._lock:
            previous = owner._liveness_identity
            if previous == identity:
                return
            if previous is not None and previous in self._liveness:
                self._liveness.pop(previous, None)
            self._liveness[identity] = (id(owner), swarm_id)
            owner._liveness_identity = identity
            transports = tuple(self._transports.values())
            self._counters["liveness_puts"] += 1
        event = LivenessEvent(
            kind=LivenessKind.PUT,
            swarm_id=swarm_id,
            node_id=node_id,
            incarnation=incarnation,
            boot_id=boot_id,
            received_monotonic=self._clock(),
        )
        self._deliver_liveness(transports, event)

    def _subscribe_liveness(self, transport: "InMemoryTransport", callback: LivenessCallback, history: bool) -> Unsubscribe:
        unsubscribe = transport._add_liveness_callback(callback)
        if history:
            now = self._clock()
            with self._lock:
                events = tuple(
                    LivenessEvent(LivenessKind.PUT, swarm_id, node, inc, boot, now)
                    for (node, inc, boot), (_, swarm_id) in self._liveness.items()
                )
            for event in events:
                self._safe_call(callback, event)
        return unsubscribe


class InMemoryTransport:
    def __init__(self, mesh: InMemoryMesh, node_id: str) -> None:
        self._mesh = mesh
        self.node_id = node_id
        self._started = False
        self._record_callbacks: List[RecordCallback] = []
        self._liveness_callbacks: List[LivenessCallback] = []
        self._liveness_identity: Optional[Tuple[str, int, str]] = None
        self._lock = threading.RLock()

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    def _require_started(self) -> None:
        if not self.started:
            raise TransportError("transport is not started")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._mesh._register(self)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        self._mesh._unregister(self)
        with self._lock:
            self._liveness_identity = None

    def publish_record(self, record: RecordEnvelope) -> None:
        self._require_started()
        self._mesh._publish(record)

    def query_records(self, pattern: str) -> Tuple[ReceivedRecord, ...]:
        self._require_started()
        return self._mesh._query(pattern)

    def subscribe_records(self, callback: RecordCallback) -> Unsubscribe:
        with self._lock:
            self._record_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._record_callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _add_liveness_callback(self, callback: LivenessCallback) -> Unsubscribe:
        with self._lock:
            self._liveness_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._liveness_callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_liveness(self, callback: LivenessCallback, *, history: bool = True) -> Unsubscribe:
        return self._mesh._subscribe_liveness(self, callback, history)

    def declare_liveness(self, swarm_id: str, node_id: str, incarnation: int, boot_id: str) -> None:
        self._require_started()
        self._mesh._declare_liveness(self, swarm_id, node_id, incarnation, boot_id)

    def _record_callbacks_snapshot(self) -> Tuple[RecordCallback, ...]:
        with self._lock:
            return tuple(self._record_callbacks)

    def _liveness_callbacks_snapshot(self) -> Tuple[LivenessCallback, ...]:
        with self._lock:
            return tuple(self._liveness_callbacks)


InboxItem = Union[ReceivedRecord, LivenessEvent, Any]


@dataclass(frozen=True)
class InboxDiagnostics:
    accepted_records: int = 0
    accepted_priority: int = 0
    coalesced_records: int = 0
    coalesced_priority: int = 0
    dropped_records: int = 0
    dropped_priority: int = 0
    high_watermark: int = 0


class BoundedCoalescingInbox:
    """Hard-bounded callback handoff queue.

    Record updates coalesce by logical evidence key. Priority events use an explicit
    caller key and always drain before record traffic.
    """

    def __init__(self, *, max_records: int = 4_096, max_priority: int = 512) -> None:
        if max_records < 1 or max_priority < 1:
            raise ValueError("inbox limits must be positive")
        self._max_records = max_records
        self._max_priority = max_priority
        self._records: "OrderedDict[Tuple[str, ...], ReceivedRecord]" = OrderedDict()
        self._priority: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
        self._counters: Counter[str] = Counter()
        self._condition = threading.Condition(threading.RLock())

    @property
    def depth(self) -> int:
        with self._condition:
            return len(self._records) + len(self._priority)

    @property
    def diagnostics(self) -> InboxDiagnostics:
        with self._condition:
            return InboxDiagnostics(**{field: self._counters[field] for field in InboxDiagnostics.__dataclass_fields__})

    def _update_high_watermark(self) -> None:
        self._counters["high_watermark"] = max(self._counters["high_watermark"], self.depth)

    def put_record(self, received: ReceivedRecord) -> bool:
        key = logical_record_key(received.record)
        with self._condition:
            if key in self._records:
                self._records[key] = received
                self._counters["coalesced_records"] += 1
                self._condition.notify()
                return True
            if len(self._records) >= self._max_records:
                self._counters["dropped_records"] += 1
                return False
            self._records[key] = received
            self._counters["accepted_records"] += 1
            self._update_high_watermark()
            self._condition.notify()
            return True

    def put_priority(self, key: Tuple[Any, ...], event: Any) -> bool:
        with self._condition:
            if key in self._priority:
                self._priority[key] = event
                self._counters["coalesced_priority"] += 1
                self._condition.notify()
                return True
            if len(self._priority) >= self._max_priority:
                self._counters["dropped_priority"] += 1
                return False
            self._priority[key] = event
            self._counters["accepted_priority"] += 1
            self._update_high_watermark()
            self._condition.notify()
            return True

    def put_liveness(self, event: LivenessEvent) -> bool:
        return self.put_priority(("liveness",) + event.identity, event)

    def pop(self, timeout: Optional[float] = None) -> Optional[InboxItem]:
        with self._condition:
            if not self._priority and not self._records:
                self._condition.wait(timeout)
            if self._priority:
                _, event = self._priority.popitem(last=False)
                return event
            if self._records:
                _, received = self._records.popitem(last=False)
                return received
            return None

    def drain(self, limit: Optional[int] = None) -> Tuple[InboxItem, ...]:
        items: List[InboxItem] = []
        while limit is None or len(items) < limit:
            item = self.pop(timeout=0)
            if item is None:
                break
            items.append(item)
        return tuple(items)
