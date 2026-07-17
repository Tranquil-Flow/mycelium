from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordEnvelope, RecordKind, logical_record_key
from mycelium_gossip.service import GossipService, PeerStateChanged
from mycelium_gossip.state import NodeStateSession, open_node_state
from mycelium_gossip.transport import LivenessCallback, ReceivedRecord, RecordCallback, Unsubscribe
from mycelium_gossip.zenoh_transport import ZenohTransport, ZenohTransportConfig


class JsonlRecorder:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        document = {
            "event": event,
            "monotonic_ns": time.monotonic_ns(),
            "unix_time_ns": time.time_ns(),
            **fields,
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def _same_record_version(left: RecordEnvelope, right: RecordEnvelope) -> bool:
    return (
        logical_record_key(left) == logical_record_key(right)
        and left.boot_id == right.boot_id
        and left.incarnation == right.incarnation
        and left.sequence == right.sequence
        and left.payload_hash == right.payload_hash
    )


class DropFirstInboundRecord:
    """Fault injector: suppress one subscriber delivery while preserving Zenoh queries."""

    def __init__(
        self,
        inner: ZenohTransport,
        *,
        origin_node_id: str,
        kind: RecordKind,
        recorder: JsonlRecorder,
    ) -> None:
        self.inner = inner
        self.origin_node_id = origin_node_id
        self.kind = kind
        self.recorder = recorder
        self._dropped: Optional[ReceivedRecord] = None
        self._query_seen_at_ns: Optional[int] = None
        self._lock = threading.Lock()

    @property
    def dropped(self) -> Optional[ReceivedRecord]:
        with self._lock:
            return self._dropped

    @property
    def query_seen_at_ns(self) -> Optional[int]:
        with self._lock:
            return self._query_seen_at_ns

    @property
    def diagnostics(self) -> Any:
        return self.inner.diagnostics

    def start(self) -> None:
        self.inner.start()

    def stop(self) -> None:
        self.inner.stop()

    def publish_record(self, record: RecordEnvelope) -> None:
        self.inner.publish_record(record)

    def query_records(self, pattern: str) -> Tuple[ReceivedRecord, ...]:
        results = self.inner.query_records(pattern)
        event: Optional[ReceivedRecord] = None
        with self._lock:
            if self._dropped is not None and self._query_seen_at_ns is None:
                event = next(
                    (
                        received
                        for received in results
                        if _same_record_version(received.record, self._dropped.record)
                    ),
                    None,
                )
                if event is not None:
                    self._query_seen_at_ns = time.monotonic_ns()
        if event is not None:
            self.recorder.write(
                "fault_exact_version_returned_by_query",
                origin_node_id=event.record.origin_node_id,
                kind=event.record.kind.value,
                incarnation=event.record.incarnation,
                sequence=event.record.sequence,
                boot_id=event.record.boot_id,
                payload_hash=event.record.payload_hash,
                transport_key=event.transport_key,
            )
        return results

    def subscribe_records(self, callback: RecordCallback) -> Unsubscribe:
        def wrapped(received: ReceivedRecord) -> None:
            should_drop = False
            with self._lock:
                if (
                    self._dropped is None
                    and received.record.origin_node_id == self.origin_node_id
                    and received.record.kind is self.kind
                ):
                    self._dropped = received
                    should_drop = True
            if should_drop:
                self.recorder.write(
                    "fault_drop",
                    origin_node_id=received.record.origin_node_id,
                    kind=received.record.kind.value,
                    incarnation=received.record.incarnation,
                    sequence=received.record.sequence,
                    boot_id=received.record.boot_id,
                    payload_hash=received.record.payload_hash,
                    transport_key=received.transport_key,
                )
                return
            callback(received)

        return self.inner.subscribe_records(wrapped)

    def declare_liveness(self, swarm_id: str, node_id: str, incarnation: int, boot_id: str) -> None:
        self.inner.declare_liveness(swarm_id, node_id, incarnation, boot_id)

    def subscribe_liveness(
        self,
        callback: LivenessCallback,
        *,
        history: bool = True,
    ) -> Unsubscribe:
        return self.inner.subscribe_liveness(callback, history=history)


def _profile_payload(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.device_profile.v2",
        "node_id": args.node_id,
        "software_version": "0.1.0-physical-qualification",
        "protocol_versions": ["mycelium.gossip.record.v1"],
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "memory_domains": [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": args.total_memory_bytes,
            }
        ],
        "endpoints": [
            {
                "endpoint_id": "zenoh-control",
                "transport": "tcp",
                "host": args.local_ip,
                "port": args.local_port,
                "scope": "overlay",
                "inbound": True,
            }
        ],
        "policy": {"available_for_swarm": True},
    }


def _status_payload(args: argparse.Namespace) -> Dict[str, Any]:
    allocatable = max(0, args.total_memory_bytes - 4 * 1024**3)
    return {
        "protocol": "mycelium.device_status.v1",
        "node_id": args.node_id,
        "lifecycle": "ready",
        "memory_domains": [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": args.total_memory_bytes,
                "allocatable_after_reservations_bytes": allocatable,
                "committed_bytes": 0,
                "reclaimable_bytes": 0,
                "reservation_generation": 1,
            }
        ],
        "queue_depth": 0,
        "in_flight": 0,
        "concurrency_limit": 1,
    }


def _link_payload(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.link_state.v1",
        "src_node_id": args.node_id,
        "dst_node_id": args.peer_node_id,
        "src_endpoint_id": "zenoh-control",
        "dst_endpoint_id": "zenoh-control",
        "reachable": True,
        "connect_rtt_ema_ms": 0.0,
        "rtt_p95_ms": 0.0,
        "jitter_ms": 0.0,
        "loss_ratio": 0.0,
        "goodput_mbps": 0.0,
        "sample_count": 1,
        "measurement_method": "zenoh-control",
    }


def _publish_evidence(
    service: GossipService,
    state: NodeStateSession,
    args: argparse.Namespace,
    recorder: JsonlRecorder,
) -> None:
    payloads = (
        (RecordKind.PROFILE, _profile_payload(args)),
        (RecordKind.STATUS, _status_payload(args)),
        (RecordKind.LINK, _link_payload(args)),
    )
    for kind, payload in payloads:
        record = state.build_record(
            swarm_id=args.swarm_id,
            kind=kind,
            payload=payload,
            ttl_ms=args.ttl_ms,
            generated_at_unix_ms=int(time.time() * 1_000),
        )
        service.publish_record(record)
        recorder.write(
            "record_published",
            kind=kind.value,
            incarnation=record.incarnation,
            sequence=record.sequence,
            payload_hash=record.payload_hash,
        )


def _atomic_json(path: Path, document: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _record_document(record: RecordEnvelope) -> Dict[str, Any]:
    return {
        "boot_id": record.boot_id,
        "incarnation": record.incarnation,
        "kind": record.kind.value,
        "logical_key": list(logical_record_key(record)),
        "origin_node_id": record.origin_node_id,
        "payload_fields": sorted(record.payload),
        "payload_hash": record.payload_hash,
        "payload_protocol": str(record.payload["protocol"]),
        "sequence": record.sequence,
    }


def _snapshot(
    service: GossipService,
    transport: DropFirstInboundRecord | ZenohTransport,
    config: ZenohTransportConfig,
    state: NodeStateSession,
    recovered_at_ns: Optional[int],
) -> Dict[str, Any]:
    registry = service.registry.snapshot()
    convergence = service.convergence_report(include_local=True)
    dropped = transport.dropped if isinstance(transport, DropFirstInboundRecord) else None
    return {
        "boot_id": state.boot_id,
        "config": config.to_dict(),
        "convergence": {
            "converged": convergence.converged,
            "peers": [
                {
                    "boot_id": peer.boot_id,
                    "incarnation": peer.incarnation,
                    "missing_kinds": [kind.value for kind in peer.missing_kinds],
                    "node_id": peer.node_id,
                    "state": peer.state.value,
                }
                for peer in convergence.peers
            ],
            "snapshot_generation": convergence.snapshot_generation,
        },
        "fault": {
            "drop_kind": transport.kind.value if isinstance(transport, DropFirstInboundRecord) else None,
            "dropped": _record_document(dropped.record) if dropped is not None else None,
            "exact_version_query_seen_at_monotonic_ns": (
                transport.query_seen_at_ns if isinstance(transport, DropFirstInboundRecord) else None
            ),
            "recovered_at_monotonic_ns": recovered_at_ns,
        },
        "hostname": platform.node(),
        "incarnation": state.incarnation,
        "node_id": state.node_id,
        "peer_states": [
            {
                "boot_id": peer.boot_id,
                "incarnation": peer.incarnation,
                "liveness_present": peer.liveness_present,
                "node_id": peer.node_id,
                "state": peer.state.value,
            }
            for peer in service.peer_states_snapshot()
        ],
        "pid": os.getpid(),
        "records": [_record_document(entry.record) for entry in registry.records],
        "service_diagnostics": asdict(service.diagnostics),
        "transport_diagnostics": asdict(transport.diagnostics),
        "updated_monotonic_ns": time.monotonic_ns(),
        "updated_unix_time_ns": time.time_ns(),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Two-host explicit-endpoint Zenoh qualification peer")
    parser.add_argument("--swarm-id", default="physical-qual")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--peer-node-id", required=True)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--local-port", required=True, type=int)
    parser.add_argument("--peer-ip", required=True)
    parser.add_argument("--peer-port", required=True, type=int)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--total-memory-bytes", required=True, type=int)
    parser.add_argument("--drop-first-kind", choices=[kind.value for kind in RecordKind])
    parser.add_argument("--repair-interval-seconds", default=0.5, type=float)
    parser.add_argument("--ttl-ms", default=120_000, type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    control_dir = args.output_dir / "control"
    control_dir.mkdir(exist_ok=True)
    recorder = JsonlRecorder(args.output_dir / "events.jsonl")
    recorder.write("process_start", argv=list(os.sys.argv), pid=os.getpid())

    config = ZenohTransportConfig(
        listen_endpoints=(f"tcp/{args.local_ip}:{args.local_port}",),
        connect_endpoints=(f"tcp/{args.peer_ip}:{args.peer_port}",),
        multicast_enabled=False,
        gossip_enabled=False,
        gossip_multihop=False,
        lease_ms=2_000,
        keep_alive=4,
        query_timeout_seconds=3.0,
    )
    _atomic_json(args.output_dir / "zenoh-config.json", config.to_dict())

    stop_requested = False
    state: Optional[NodeStateSession] = None
    service: Optional[GossipService] = None
    recovered_at_ns: Optional[int] = None
    try:
        state = open_node_state(args.state_path, node_id=args.node_id)
        inner = ZenohTransport(args.swarm_id, args.node_id, config=config)
        if args.drop_first_kind is not None:
            transport: DropFirstInboundRecord | ZenohTransport = DropFirstInboundRecord(
                inner,
                origin_node_id=args.peer_node_id,
                kind=RecordKind(args.drop_first_kind),
                recorder=recorder,
            )
        else:
            transport = inner
        service = GossipService(
            swarm_id=args.swarm_id,
            node_id=args.node_id,
            incarnation=state.incarnation,
            boot_id=state.boot_id,
            transport=transport,
            registry=VersionedRecordStore(args.swarm_id),
            suspicion_grace_seconds=3.0,
            worker_poll_seconds=0.02,
            repair_interval_seconds=args.repair_interval_seconds,
        )

        def record_service_event(event: Any) -> None:
            if isinstance(event, PeerStateChanged):
                recorder.write(
                    "peer_state_changed",
                    node_id=event.current.node_id,
                    incarnation=event.current.incarnation,
                    boot_id=event.current.boot_id,
                    state=event.current.state.value,
                    liveness_present=event.current.liveness_present,
                    reason=event.reason,
                )

        service.subscribe_events(record_service_event)
        recorder.write("service_starting", incarnation=state.incarnation, boot_id=state.boot_id)
        service.start(background=True)
        _publish_evidence(service, state, args, recorder)
        recorder.write("service_ready", incarnation=state.incarnation, boot_id=state.boot_id)
        _atomic_json(
            args.output_dir / "ready.json",
            {
                "boot_id": state.boot_id,
                "incarnation": state.incarnation,
                "node_id": state.node_id,
                "pid": os.getpid(),
            },
        )

        while not stop_requested:
            if (control_dir / "crash").exists():
                (control_dir / "crash").unlink()
                recorder.write("crash_command", pid=os.getpid())
                os.kill(os.getpid(), signal.SIGKILL)
            if (control_dir / "stop").exists():
                (control_dir / "stop").unlink()
                recorder.write("stop_command", pid=os.getpid())
                stop_requested = True

            if isinstance(transport, DropFirstInboundRecord) and transport.dropped is not None:
                target = transport.dropped.record
                repaired = any(
                    _same_record_version(entry.record, target)
                    for entry in service.registry.snapshot().records
                )
                if repaired and recovered_at_ns is None:
                    recovered_at_ns = time.monotonic_ns()
                    recorder.write(
                        "fault_recovered",
                        origin_node_id=target.origin_node_id,
                        kind=target.kind.value,
                        incarnation=target.incarnation,
                        sequence=target.sequence,
                        boot_id=target.boot_id,
                        payload_hash=target.payload_hash,
                        exact_version_query_seen_at_monotonic_ns=transport.query_seen_at_ns,
                        repair_runs=service.diagnostics.repair_runs,
                        repair_records=service.diagnostics.repair_records,
                    )

            _atomic_json(
                args.output_dir / "snapshot.json",
                _snapshot(service, transport, config, state, recovered_at_ns),
            )
            time.sleep(0.05)
    except BaseException as exc:
        recorder.write("process_error", error_type=type(exc).__name__, error=str(exc))
        _atomic_json(
            args.output_dir / "error.json",
            {"error": str(exc), "error_type": type(exc).__name__, "pid": os.getpid()},
        )
        raise
    finally:
        if service is not None:
            service.stop()
        if state is not None:
            state.close()
        recorder.write("process_exit", clean=stop_requested, pid=os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
