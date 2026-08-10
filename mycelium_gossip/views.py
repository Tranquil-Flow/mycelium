from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .registry import RecordSnapshot, RegistrySnapshot
from .schema import RecordKind
from .service import FailureScope, PeerHealthState, PeerState, QuarantineEntry


@dataclass(frozen=True)
class EvidenceVersion:
    incarnation: int
    sequence: int
    payload_hash: str


@dataclass(frozen=True)
class EndpointEvidence:
    endpoint_id: str
    transport: str
    host: str
    port: int
    scope: str
    inbound: bool


@dataclass(frozen=True)
class OfferingEvidence:
    deployment_id: str
    deployment_epoch: int
    assignment_id: str
    manifest_digest: str
    resolved_commit: str
    model_id: str
    start_layer: int
    end_layer_exclusive: int
    runtime_instance_id: str
    load_generation: int
    proof_digest: str
    inference_endpoint_id: str
    version: EvidenceVersion


@dataclass(frozen=True)
class RouterNodeEvidence:
    node_id: str
    incarnation: Optional[int]
    boot_id: Optional[str]
    peer_state: Optional[PeerHealthState]
    eligible: bool
    exclusion_reasons: Tuple[str, ...]
    endpoints: Tuple[EndpointEvidence, ...]
    offerings: Tuple[OfferingEvidence, ...]
    profile_version: Optional[EvidenceVersion]
    status_version: Optional[EvidenceVersion]
    queue_depth: Optional[int]
    in_flight: Optional[int]
    concurrency_limit: Optional[int]


@dataclass(frozen=True)
class RouterEdgeEvidence:
    src_node_id: str
    src_endpoint_id: str
    dst_node_id: str
    dst_endpoint_id: str
    reachable: bool
    eligible: bool
    exclusion_reasons: Tuple[str, ...]
    connect_rtt_ema_ms: Optional[float]
    rtt_p95_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_ratio: Optional[float]
    goodput_mbps: Optional[float]
    sample_count: int
    measurement_method: str
    version: EvidenceVersion


@dataclass(frozen=True)
class RouterView:
    snapshot_generation: int
    eligibility_generation: str
    nodes: Tuple[RouterNodeEvidence, ...]
    edges: Tuple[RouterEdgeEvidence, ...]


@dataclass(frozen=True)
class MemoryDomainEvidence:
    memory_domain_id: str
    kind: str
    total_bytes: int
    allocatable_after_reservations_bytes: int
    committed_bytes: int
    reclaimable_bytes: int
    reservation_generation: int


@dataclass(frozen=True)
class AllocatorNodeEvidence:
    node_id: str
    incarnation: Optional[int]
    boot_id: Optional[str]
    peer_state: Optional[PeerHealthState]
    eligible: bool
    exclusion_reasons: Tuple[str, ...]
    memory_domains: Tuple[MemoryDomainEvidence, ...]
    fast_allocatable_bytes: int
    total_allocatable_bytes: int
    queue_depth: Optional[int]
    in_flight: Optional[int]
    concurrency_limit: Optional[int]
    profile_version: Optional[EvidenceVersion]
    status_version: Optional[EvidenceVersion]


@dataclass(frozen=True)
class AllocatorView:
    snapshot_generation: int
    eligibility_generation: str
    nodes: Tuple[AllocatorNodeEvidence, ...]


def router_view_to_dict(view: RouterView) -> Dict[str, Any]:
    """Serialize immutable Router evidence under its owned wire identifier."""
    return {"protocol": "mycelium.gossip.router_view.v1", **asdict(view)}


def allocator_view_to_dict(view: AllocatorView) -> Dict[str, Any]:
    """Serialize immutable Allocator evidence under its owned wire identifier."""
    return {"protocol": "mycelium.gossip.allocator_view.v1", **asdict(view)}


def _version(entry: Optional[RecordSnapshot]) -> Optional[EvidenceVersion]:
    if entry is None:
        return None
    record = entry.record
    return EvidenceVersion(record.incarnation, record.sequence, record.payload_hash)


def _number(payload: Mapping[str, Any], field: str) -> Optional[float]:
    value = payload.get(field)
    if value is None:
        return None
    return float(value)


def _index_snapshot(snapshot: RegistrySnapshot):
    profiles: Dict[str, RecordSnapshot] = {}
    statuses: Dict[str, RecordSnapshot] = {}
    offerings: Dict[str, List[RecordSnapshot]] = {}
    links: List[RecordSnapshot] = []
    node_ids = set()
    for entry in snapshot.records:
        record = entry.record
        node_ids.add(record.origin_node_id)
        if record.kind is RecordKind.PROFILE:
            profiles[record.origin_node_id] = entry
        elif record.kind is RecordKind.STATUS:
            statuses[record.origin_node_id] = entry
        elif record.kind is RecordKind.OFFERING:
            offerings.setdefault(record.origin_node_id, []).append(entry)
        elif record.kind is RecordKind.LINK:
            links.append(entry)
            node_ids.add(str(record.payload["dst_node_id"]))
    return profiles, statuses, offerings, links, node_ids


def _quarantine_sets(quarantines: Iterable[QuarantineEntry]):
    peers = set()
    offerings = set()
    edges = set()
    for entry in quarantines:
        if entry.scope is FailureScope.PEER:
            peers.add(entry.key[1])
        elif entry.scope is FailureScope.OFFERING:
            offerings.add((entry.key[1], entry.key[2]))
        elif entry.scope is FailureScope.EDGE:
            edges.add(tuple(entry.key[1:]))
    return peers, offerings, edges


def _endpoints(profile: Optional[RecordSnapshot]) -> Tuple[EndpointEvidence, ...]:
    if profile is None:
        return ()
    results = []
    for endpoint in profile.record.payload["endpoints"]:
        results.append(
            EndpointEvidence(
                endpoint_id=str(endpoint["endpoint_id"]),
                transport=str(endpoint["transport"]),
                host=str(endpoint["host"]),
                port=int(endpoint["port"]),
                scope=str(endpoint["scope"]),
                inbound=bool(endpoint["inbound"]),
            )
        )
    return tuple(sorted(results, key=lambda item: item.endpoint_id))


def _identity_matches(entry: Optional[RecordSnapshot], peer: Optional[PeerState]) -> bool:
    if entry is None or peer is None:
        return False
    record = entry.record
    return (record.incarnation, record.boot_id) == (peer.incarnation, peer.boot_id)


def _ready_offerings(
    entries: Sequence[RecordSnapshot],
    peer: Optional[PeerState],
    endpoint_ids: set[str],
    quarantined: set[Tuple[str, str]],
) -> Tuple[OfferingEvidence, ...]:
    results: List[OfferingEvidence] = []
    for entry in entries:
        record = entry.record
        payload = record.payload
        assignment_id = str(payload["assignment_id"])
        if peer is None or (record.incarnation, record.boot_id) != (peer.incarnation, peer.boot_id):
            continue
        if payload["readiness_state"] != "loaded_and_probed":
            continue
        if str(payload["inference_endpoint_id"]) not in endpoint_ids:
            continue
        if (record.origin_node_id, assignment_id) in quarantined:
            continue
        results.append(
            OfferingEvidence(
                deployment_id=str(payload["deployment_id"]),
                deployment_epoch=int(payload["deployment_epoch"]),
                assignment_id=assignment_id,
                manifest_digest=str(payload["manifest_digest"]),
                resolved_commit=str(payload["resolved_commit"]),
                model_id=str(payload["model_id"]),
                start_layer=int(payload["start_layer"]),
                end_layer_exclusive=int(payload["end_layer_exclusive"]),
                runtime_instance_id=str(payload["runtime_instance_id"]),
                load_generation=int(payload["load_generation"]),
                proof_digest=str(payload["proof_digest"]),
                inference_endpoint_id=str(payload["inference_endpoint_id"]),
                version=_version(entry),  # type: ignore[arg-type]
            )
        )
    return tuple(sorted(results, key=lambda item: (item.deployment_id, item.assignment_id, item.inference_endpoint_id)))


def _digest(parts: Any) -> str:
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def build_router_view(
    snapshot: RegistrySnapshot,
    peer_states: Iterable[PeerState],
    quarantines: Iterable[QuarantineEntry],
) -> RouterView:
    profiles, statuses, offering_entries, links, record_nodes = _index_snapshot(snapshot)
    peers = {peer.node_id: peer for peer in peer_states}
    node_ids = record_nodes | set(peers)
    quarantined_peers, quarantined_offerings, quarantined_edges = _quarantine_sets(quarantines)
    nodes: List[RouterNodeEvidence] = []

    for node_id in sorted(node_ids):
        profile = profiles.get(node_id)
        status = statuses.get(node_id)
        peer = peers.get(node_id)
        endpoints = _endpoints(profile)
        endpoint_ids = {endpoint.endpoint_id for endpoint in endpoints}
        offerings = _ready_offerings(
            offering_entries.get(node_id, ()),
            peer,
            endpoint_ids,
            quarantined_offerings,
        )
        reasons: List[str] = []
        if profile is None:
            reasons.append("profile_missing")
        if status is None:
            reasons.append("status_missing")
        if peer is None or peer.state is not PeerHealthState.ALIVE:
            reasons.append("peer_not_alive")
        if peer is not None and (
            (profile is not None and not _identity_matches(profile, peer))
            or (status is not None and not _identity_matches(status, peer))
        ):
            reasons.append("identity_mismatch")
        if status is not None and status.record.payload["lifecycle"] != "ready":
            reasons.append("status_not_ready")
        if node_id in quarantined_peers:
            reasons.append("peer_quarantined")
        if not offerings:
            reasons.append("no_ready_offering")
        status_payload = status.record.payload if status is not None else {}
        nodes.append(
            RouterNodeEvidence(
                node_id=node_id,
                incarnation=peer.incarnation if peer else None,
                boot_id=peer.boot_id if peer else None,
                peer_state=peer.state if peer else None,
                eligible=not reasons,
                exclusion_reasons=tuple(reasons),
                endpoints=endpoints,
                offerings=offerings,
                profile_version=_version(profile),
                status_version=_version(status),
                queue_depth=int(status_payload["queue_depth"]) if "queue_depth" in status_payload else None,
                in_flight=int(status_payload["in_flight"]) if "in_flight" in status_payload else None,
                concurrency_limit=int(status_payload["concurrency_limit"]) if "concurrency_limit" in status_payload else None,
            )
        )

    nodes_tuple = tuple(nodes)
    node_by_id = {node.node_id: node for node in nodes_tuple}
    endpoint_sets = {node.node_id: {endpoint.endpoint_id for endpoint in node.endpoints} for node in nodes_tuple}
    edges: List[RouterEdgeEvidence] = []
    for entry in sorted(
        links,
        key=lambda item: (
            item.record.origin_node_id,
            str(item.record.payload["src_endpoint_id"]),
            str(item.record.payload["dst_node_id"]),
            str(item.record.payload["dst_endpoint_id"]),
        ),
    ):
        record = entry.record
        payload = record.payload
        src_node = record.origin_node_id
        dst_node = str(payload["dst_node_id"])
        src_endpoint = str(payload["src_endpoint_id"])
        dst_endpoint = str(payload["dst_endpoint_id"])
        reasons = []
        if not bool(payload["reachable"]):
            reasons.append("unreachable")
        if src_node not in node_by_id or not node_by_id[src_node].eligible:
            reasons.append("source_ineligible")
        if dst_node not in node_by_id or not node_by_id[dst_node].eligible:
            reasons.append("destination_ineligible")
        if src_endpoint not in endpoint_sets.get(src_node, set()):
            reasons.append("source_endpoint_missing")
        if dst_endpoint not in endpoint_sets.get(dst_node, set()):
            reasons.append("destination_endpoint_missing")
        if (src_node, src_endpoint, dst_node, dst_endpoint) in quarantined_edges:
            reasons.append("edge_quarantined")
        edges.append(
            RouterEdgeEvidence(
                src_node_id=src_node,
                src_endpoint_id=src_endpoint,
                dst_node_id=dst_node,
                dst_endpoint_id=dst_endpoint,
                reachable=bool(payload["reachable"]),
                eligible=not reasons,
                exclusion_reasons=tuple(reasons),
                connect_rtt_ema_ms=_number(payload, "connect_rtt_ema_ms"),
                rtt_p95_ms=_number(payload, "rtt_p95_ms"),
                jitter_ms=_number(payload, "jitter_ms"),
                loss_ratio=_number(payload, "loss_ratio"),
                goodput_mbps=_number(payload, "goodput_mbps"),
                sample_count=int(payload["sample_count"]),
                measurement_method=str(payload["measurement_method"]),
                version=_version(entry),  # type: ignore[arg-type]
            )
        )
    edges_tuple = tuple(edges)
    generation = _digest((nodes_tuple, edges_tuple))
    return RouterView(snapshot.generation, generation, nodes_tuple, edges_tuple)


def _memory_domains(status: Optional[RecordSnapshot]) -> Tuple[MemoryDomainEvidence, ...]:
    if status is None:
        return ()
    results = []
    seen = set()
    for domain in status.record.payload["memory_domains"]:
        domain_id = str(domain["memory_domain_id"])
        if domain_id in seen:
            continue
        seen.add(domain_id)
        results.append(
            MemoryDomainEvidence(
                memory_domain_id=domain_id,
                kind=str(domain["kind"]),
                total_bytes=int(domain["total_bytes"]),
                allocatable_after_reservations_bytes=int(domain.get("allocatable_after_reservations_bytes", 0)),
                committed_bytes=int(domain.get("committed_bytes", 0)),
                reclaimable_bytes=int(domain.get("reclaimable_bytes", 0)),
                reservation_generation=int(domain.get("reservation_generation", 0)),
            )
        )
    return tuple(sorted(results, key=lambda item: item.memory_domain_id))


def build_allocator_view(
    snapshot: RegistrySnapshot,
    peer_states: Iterable[PeerState],
    quarantines: Iterable[QuarantineEntry],
) -> AllocatorView:
    profiles, statuses, _, _, record_nodes = _index_snapshot(snapshot)
    peers = {peer.node_id: peer for peer in peer_states}
    node_ids = record_nodes | set(peers)
    quarantined_peers, _, _ = _quarantine_sets(quarantines)
    nodes: List[AllocatorNodeEvidence] = []
    for node_id in sorted(node_ids):
        profile = profiles.get(node_id)
        status = statuses.get(node_id)
        peer = peers.get(node_id)
        reasons: List[str] = []
        if profile is None:
            reasons.append("profile_missing")
        if status is None:
            reasons.append("status_missing")
        if peer is None or peer.state is not PeerHealthState.ALIVE:
            reasons.append("peer_not_alive")
        if peer is not None and (
            (profile is not None and not _identity_matches(profile, peer))
            or (status is not None and not _identity_matches(status, peer))
        ):
            reasons.append("identity_mismatch")
        status_payload = status.record.payload if status is not None else {}
        if status is not None and status_payload["lifecycle"] != "ready":
            reasons.append("status_not_ready")
        if status is not None and int(status_payload["concurrency_limit"]) <= 0:
            reasons.append("no_concurrency_capacity")
        if node_id in quarantined_peers:
            reasons.append("peer_quarantined")
        domains = _memory_domains(status)
        total_allocatable = sum(domain.allocatable_after_reservations_bytes for domain in domains)
        fast_allocatable = sum(
            domain.allocatable_after_reservations_bytes
            for domain in domains
            if domain.kind in {"unified", "vram", "accelerator"}
        )
        if fast_allocatable == 0:
            fast_allocatable = total_allocatable
        if status is not None and total_allocatable <= 0:
            reasons.append("no_allocatable_memory")
        nodes.append(
            AllocatorNodeEvidence(
                node_id=node_id,
                incarnation=peer.incarnation if peer else None,
                boot_id=peer.boot_id if peer else None,
                peer_state=peer.state if peer else None,
                eligible=not reasons,
                exclusion_reasons=tuple(reasons),
                memory_domains=domains,
                fast_allocatable_bytes=fast_allocatable,
                total_allocatable_bytes=total_allocatable,
                queue_depth=int(status_payload["queue_depth"]) if "queue_depth" in status_payload else None,
                in_flight=int(status_payload["in_flight"]) if "in_flight" in status_payload else None,
                concurrency_limit=int(status_payload["concurrency_limit"]) if "concurrency_limit" in status_payload else None,
                profile_version=_version(profile),
                status_version=_version(status),
            )
        )
    nodes_tuple = tuple(nodes)
    generation = _digest(nodes_tuple)
    return AllocatorView(snapshot.generation, generation, nodes_tuple)
