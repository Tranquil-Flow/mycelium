from __future__ import annotations

import importlib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .codec import DecodeError, decode_record, encode_record
from .schema import RecordEnvelope, transport_key
from .transport import (
    GossipTransport,
    LivenessEvent,
    LivenessKind,
    ReceivedRecord,
    TransportError,
    Unsubscribe,
)


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ZenohUnavailable(TransportError):
    pass


def _safe_segment(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{field} must be a safe key segment")


def parse_liveness_key(key: str, *, expected_swarm: str) -> Tuple[str, int, str]:
    parts = key.split("/")
    if len(parts) != 6 or parts[0] != "mycelium" or parts[2] != "liveness":
        raise ValueError("malformed liveness key")
    if parts[1] != expected_swarm:
        raise ValueError("liveness key belongs to another swarm")
    try:
        incarnation = int(parts[4])
    except ValueError as exc:
        raise ValueError("malformed liveness key incarnation") from exc
    if incarnation < 1:
        raise ValueError("malformed liveness key incarnation")
    _safe_segment(parts[3], "node_id")
    _safe_segment(parts[5], "boot_id")
    return parts[3], incarnation, parts[5]


@dataclass(frozen=True)
class ZenohTransportConfig:
    listen_endpoints: Tuple[str, ...] = ("tcp/127.0.0.1:0",)
    connect_endpoints: Tuple[str, ...] = ()
    multicast_enabled: bool = False
    multicast_address: str = "224.0.0.225:7447"
    multicast_interface: str = "auto"
    gossip_enabled: bool = True
    gossip_multihop: bool = True
    lease_ms: int = 4_000
    keep_alive: int = 4
    query_timeout_seconds: float = 3.0
    allow_wildcard_listen: bool = False

    def __post_init__(self) -> None:
        if self.lease_ms < 1:
            raise ValueError("lease must be positive")
        if self.keep_alive < 1:
            raise ValueError("keep_alive must be positive")
        if self.query_timeout_seconds <= 0:
            raise ValueError("query timeout must be positive")
        if not self.multicast_address or not self.multicast_interface:
            raise ValueError("multicast address and interface must be explicit")
        for endpoint in self.listen_endpoints:
            if not endpoint:
                raise ValueError("listen endpoint cannot be empty")
            wildcard = "0.0.0.0" in endpoint or "[::]" in endpoint or "tcp/:::" in endpoint
            if wildcard and not self.allow_wildcard_listen:
                raise ValueError("wildcard listener requires explicit opt-in")
        if any(not endpoint for endpoint in self.connect_endpoints):
            raise ValueError("connect endpoint cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        multicast: Dict[str, Any] = {
            "enabled": self.multicast_enabled,
            "address": self.multicast_address,
            "interface": self.multicast_interface,
        }
        if self.multicast_enabled:
            multicast.update(
                {
                    "autoconnect": {"peer": ["peer"]},
                    "listen": {"peer": True},
                }
            )
        value: Dict[str, Any] = {
            "mode": "peer",
            "listen": {"endpoints": list(self.listen_endpoints)},
            "scouting": {
                "multicast": multicast,
                "gossip": {
                    "enabled": self.gossip_enabled,
                    "multihop": self.gossip_multihop,
                    "target": {"peer": ["peer"]},
                    "autoconnect": {"peer": ["peer"]},
                },
            },
            "transport": {"link": {"tx": {"lease": self.lease_ms, "keep_alive": self.keep_alive}}},
        }
        if self.connect_endpoints:
            value["connect"] = {"endpoints": list(self.connect_endpoints)}
        return value


@dataclass(frozen=True)
class ZenohTransportDiagnostics:
    published_records: int = 0
    received_records: int = 0
    query_replies: int = 0
    decode_failures: int = 0
    invalid_keys: int = 0
    query_errors: int = 0
    callback_failures: int = 0
    stale_local_publishes: int = 0
    liveness_events: int = 0


class ZenohTransport(GossipTransport):
    """Optional Zenoh adapter; module import remains dependency-free."""

    def __init__(
        self,
        swarm_id: str,
        node_id: str,
        *,
        config: Optional[ZenohTransportConfig] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _safe_segment(swarm_id, "swarm_id")
        _safe_segment(node_id, "node_id")
        self.swarm_id = swarm_id
        self.node_id = node_id
        self.config = config or ZenohTransportConfig()
        self._clock = monotonic
        self._zenoh: Any = None
        self._session: Any = None
        self._queryable: Any = None
        self._liveness_token: Any = None
        self._record_subscribers: Dict[int, Any] = {}
        self._liveness_subscribers: Dict[int, Any] = {}
        self._local_records: Dict[str, RecordEnvelope] = {}
        self._counters: Counter[str] = Counter()
        self._next_subscription_id = 1
        self._started = False
        self._lock = threading.RLock()

    @property
    def diagnostics(self) -> ZenohTransportDiagnostics:
        with self._lock:
            return ZenohTransportDiagnostics(
                **{field: self._counters[field] for field in ZenohTransportDiagnostics.__dataclass_fields__}
            )

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    @staticmethod
    def _load_zenoh() -> Any:
        try:
            return importlib.import_module("zenoh")
        except ImportError as exc:
            raise ZenohUnavailable(
                "Zenoh backend requires optional dependency 'eclipse-zenoh>=1.9,<2'"
            ) from exc

    def _require_started(self) -> None:
        if not self.started or self._session is None:
            raise TransportError("Zenoh transport is not started")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
        zenoh = self._load_zenoh()
        zenoh_config = zenoh.Config.from_json5(json.dumps(self.config.to_dict(), separators=(",", ":")))
        session = zenoh.open(zenoh_config)
        try:
            queryable = session.declare_queryable(
                f"mycelium/{self.swarm_id}/{self.node_id}/**",
                self._on_query,
                complete=True,
            )
        except Exception:
            session.close()
            raise
        with self._lock:
            self._zenoh = zenoh
            self._session = session
            self._queryable = queryable
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            record_subscribers = tuple(self._record_subscribers.values())
            liveness_subscribers = tuple(self._liveness_subscribers.values())
            self._record_subscribers.clear()
            self._liveness_subscribers.clear()
            token = self._liveness_token
            queryable = self._queryable
            session = self._session
            self._liveness_token = None
            self._queryable = None
            self._session = None
        for entity in record_subscribers + liveness_subscribers:
            try:
                entity.undeclare()
            except Exception:
                pass
        if token is not None:
            try:
                token.undeclare()
            except Exception:
                pass
        if queryable is not None:
            try:
                queryable.undeclare()
            except Exception:
                pass
        if session is not None:
            session.close()

    def publish_record(self, record: RecordEnvelope) -> None:
        self._require_started()
        if record.swarm_id != self.swarm_id or record.origin_node_id != self.node_id:
            raise TransportError("Zenoh transport may only publish local records in its configured swarm")
        key = transport_key(record)
        with self._lock:
            current = self._local_records.get(key)
            if current is not None:
                current_version = (current.incarnation, current.sequence)
                candidate_version = (record.incarnation, record.sequence)
                if candidate_version < current_version:
                    self._counters["stale_local_publishes"] += 1
                    return
                if candidate_version == current_version and current.payload_hash != record.payload_hash:
                    raise TransportError("same local record version has conflicting payload")
            self._local_records[key] = record
            session = self._session
        session.put(key, encode_record(record), encoding="application/json")
        with self._lock:
            self._counters["published_records"] += 1

    def _safe_callback(self, callback: Callable[[Any], None], value: Any) -> None:
        try:
            callback(value)
        except Exception:
            with self._lock:
                self._counters["callback_failures"] += 1

    def _decode_sample(self, sample: Any) -> Optional[ReceivedRecord]:
        key = str(sample.key_expr)
        try:
            record = decode_record(sample.payload.to_bytes())
        except (DecodeError, ValueError):
            with self._lock:
                self._counters["decode_failures"] += 1
            return None
        if key != transport_key(record):
            with self._lock:
                self._counters["invalid_keys"] += 1
            return None
        return ReceivedRecord(key, record, self._clock())

    def subscribe_records(self, callback: Callable[[ReceivedRecord], None]) -> Unsubscribe:
        self._require_started()
        zenoh = self._zenoh

        def on_sample(sample: Any) -> None:
            if sample.kind != zenoh.SampleKind.PUT:
                return
            received = self._decode_sample(sample)
            if received is not None:
                with self._lock:
                    self._counters["received_records"] += 1
                self._safe_callback(callback, received)

        with self._lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            subscriber = self._session.declare_subscriber(f"mycelium/{self.swarm_id}/**", on_sample)
            self._record_subscribers[subscription_id] = subscriber

        def unsubscribe() -> None:
            with self._lock:
                entity = self._record_subscribers.pop(subscription_id, None)
            if entity is not None:
                entity.undeclare()

        return unsubscribe

    def _on_query(self, query: Any) -> None:
        try:
            with self._lock:
                records = tuple(self._local_records.items())
            for key, record in records:
                if query.key_expr.intersects(key):
                    query.reply(key, encode_record(record), encoding="application/json")
        except Exception as exc:
            with self._lock:
                self._counters["query_errors"] += 1
            try:
                query.reply_err(str(exc))
            except Exception:
                pass
        finally:
            try:
                query.drop()
            except Exception:
                pass

    def query_records(self, pattern: str) -> Tuple[ReceivedRecord, ...]:
        self._require_started()
        with self._lock:
            session = self._session
        replies = list(session.get(pattern, timeout=self.config.query_timeout_seconds))
        by_key: Dict[str, ReceivedRecord] = {}
        for reply in replies:
            sample = reply.ok
            if sample is None:
                with self._lock:
                    self._counters["query_errors"] += 1
                continue
            received = self._decode_sample(sample)
            if received is None:
                continue
            current = by_key.get(received.transport_key)
            if current is None or (received.record.incarnation, received.record.sequence) > (
                current.record.incarnation,
                current.record.sequence,
            ):
                by_key[received.transport_key] = received
        with self._lock:
            self._counters["query_replies"] += len(by_key)
        return tuple(by_key[key] for key in sorted(by_key))

    def declare_liveness(self, swarm_id: str, node_id: str, incarnation: int, boot_id: str) -> None:
        self._require_started()
        if swarm_id != self.swarm_id or node_id != self.node_id or incarnation < 1:
            raise TransportError("liveness identity does not match configured transport")
        _safe_segment(boot_id, "boot_id")
        key = f"mycelium/{swarm_id}/liveness/{node_id}/{incarnation}/{boot_id}"
        with self._lock:
            previous = self._liveness_token
            self._liveness_token = self._session.liveliness().declare_token(key)
        if previous is not None:
            previous.undeclare()

    def subscribe_liveness(
        self,
        callback: Callable[[LivenessEvent], None],
        *,
        history: bool = True,
    ) -> Unsubscribe:
        self._require_started()
        zenoh = self._zenoh

        def on_sample(sample: Any) -> None:
            key = str(sample.key_expr)
            try:
                node_id, incarnation, boot_id = parse_liveness_key(key, expected_swarm=self.swarm_id)
            except ValueError:
                with self._lock:
                    self._counters["invalid_keys"] += 1
                return
            kind = LivenessKind.PUT if sample.kind == zenoh.SampleKind.PUT else LivenessKind.DELETE
            event = LivenessEvent(kind, self.swarm_id, node_id, incarnation, boot_id, self._clock())
            with self._lock:
                self._counters["liveness_events"] += 1
            self._safe_callback(callback, event)

        with self._lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            subscriber = self._session.liveliness().declare_subscriber(
                f"mycelium/{self.swarm_id}/liveness/*/*/*",
                on_sample,
                history=history,
            )
            self._liveness_subscribers[subscription_id] = subscriber

        def unsubscribe() -> None:
            with self._lock:
                entity = self._liveness_subscribers.pop(subscription_id, None)
            if entity is not None:
                entity.undeclare()

        return unsubscribe
