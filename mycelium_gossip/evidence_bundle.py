from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple

from .registry import RecordSnapshot, RegistrySnapshot
from .schema import logical_record_key, record_from_dict
from .service import FailureObservation, FailureScope, PeerHealthState, PeerState, QuarantineEntry
from .views import (
    allocator_view_to_dict,
    build_allocator_view,
    build_router_view,
    router_view_to_dict,
)

EVIDENCE_BUNDLE_PROTOCOL = "mycelium.gossip.evidence_bundle.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_KEYS = {
    "protocol",
    "swarm_id",
    "deployment",
    "model",
    "snapshot_generation",
    "records",
    "peer_states",
    "quarantines",
    "router_view",
    "allocator_view",
    "evidence_bundle_digest",
}


class EvidenceBundleError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceBundle:
    protocol: str
    swarm_id: str
    deployment: Mapping[str, Any]
    model: Mapping[str, Any]
    snapshot_generation: int
    records: Tuple[Mapping[str, Any], ...]
    peer_states: Tuple[Mapping[str, Any], ...]
    quarantines: Tuple[Mapping[str, Any], ...]
    router_view: Mapping[str, Any]
    allocator_view: Mapping[str, Any]
    evidence_bundle_digest: str


def _canonical_bytes(document: Any) -> bytes:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceBundleError("evidence bundle must be canonical JSON") from exc


def _json_value(value: Any) -> Any:
    """Normalize tuples and string enums to their actual JSON wire representation."""
    return json.loads(_canonical_bytes(value))


def evidence_bundle_digest(document: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in document.items() if key != "evidence_bundle_digest"}
    return "sha256:" + hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _validate_deployment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"deployment_id", "deployment_epoch"}:
        raise EvidenceBundleError("deployment must contain deployment_id and deployment_epoch")
    deployment_id = value.get("deployment_id")
    try:
        canonical = str(uuid.UUID(str(deployment_id)))
    except (TypeError, ValueError) as exc:
        raise EvidenceBundleError("deployment_id must be a canonical UUID") from exc
    if deployment_id != canonical:
        raise EvidenceBundleError("deployment_id must be a canonical UUID")
    epoch = value.get("deployment_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise EvidenceBundleError("deployment_epoch must be a non-negative integer")
    return {"deployment_id": canonical, "deployment_epoch": epoch}


def _validate_model(value: Any) -> dict[str, Any]:
    required = {"model_id", "num_layers", "manifest_digest", "resolved_commit"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceBundleError("model identity fields are incomplete")
    model_id = value.get("model_id")
    num_layers = value.get("num_layers")
    manifest_digest = value.get("manifest_digest")
    resolved_commit = value.get("resolved_commit")
    if not isinstance(model_id, str) or not model_id:
        raise EvidenceBundleError("model_id must be non-empty")
    if isinstance(num_layers, bool) or not isinstance(num_layers, int) or num_layers <= 0:
        raise EvidenceBundleError("num_layers must be a positive integer")
    if not isinstance(manifest_digest, str) or not _SHA256_RE.fullmatch(manifest_digest):
        raise EvidenceBundleError("manifest_digest must be sha256:<64 lowercase hex>")
    if not isinstance(resolved_commit, str) or not _COMMIT_RE.fullmatch(resolved_commit):
        raise EvidenceBundleError("resolved_commit must be immutable 40-hex")
    return {
        "model_id": model_id,
        "num_layers": num_layers,
        "manifest_digest": manifest_digest,
        "resolved_commit": resolved_commit,
    }


def _peer_to_dict(peer: PeerState) -> dict[str, Any]:
    return {
        "node_id": peer.node_id,
        "incarnation": peer.incarnation,
        "boot_id": peer.boot_id,
        "state": peer.state.value,
        "liveness_present": peer.liveness_present,
    }


def _peer_from_dict(value: Any) -> PeerState:
    required = {"node_id", "incarnation", "boot_id", "state", "liveness_present"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceBundleError("peer state shape is invalid")
    node_id = value.get("node_id")
    incarnation = value.get("incarnation")
    boot_id = value.get("boot_id")
    liveness = value.get("liveness_present")
    if not isinstance(node_id, str) or not node_id or not isinstance(boot_id, str) or not boot_id:
        raise EvidenceBundleError("peer identity is invalid")
    if isinstance(incarnation, bool) or not isinstance(incarnation, int) or incarnation < 1:
        raise EvidenceBundleError("peer incarnation is invalid")
    if not isinstance(liveness, bool):
        raise EvidenceBundleError("peer liveness_present must be boolean")
    try:
        state = PeerHealthState(value.get("state"))
    except (TypeError, ValueError) as exc:
        raise EvidenceBundleError("peer state is invalid") from exc
    return PeerState(node_id, incarnation, boot_id, state, liveness, 0.0, None)


def _observation_to_dict(observation: FailureObservation) -> dict[str, Any]:
    return {
        "route_id": observation.route_id,
        "route_generation": observation.route_generation,
        "src_node_id": observation.src_node_id,
        "src_endpoint_id": observation.src_endpoint_id,
        "dst_node_id": observation.dst_node_id,
        "dst_endpoint_id": observation.dst_endpoint_id,
        "offering_id": observation.offering_id,
        "failure_kind": observation.failure_kind,
        "scope": observation.scope.value,
        "probe_correlation_id": observation.probe_correlation_id,
    }


def _quarantine_to_dict(entry: QuarantineEntry) -> dict[str, Any]:
    return {
        "key": list(entry.key),
        "scope": entry.scope.value,
        "observation": _observation_to_dict(entry.observation),
    }


def _quarantine_from_dict(value: Any) -> QuarantineEntry:
    if not isinstance(value, Mapping) or set(value) != {"key", "scope", "observation"}:
        raise EvidenceBundleError("quarantine shape is invalid")
    key_raw = value.get("key")
    observation_raw = value.get("observation")
    if not isinstance(key_raw, list) or not all(isinstance(item, str) and item for item in key_raw):
        raise EvidenceBundleError("quarantine key is invalid")
    if not isinstance(observation_raw, Mapping):
        raise EvidenceBundleError("quarantine observation is invalid")
    try:
        scope = FailureScope(value.get("scope"))
        observation_scope = FailureScope(observation_raw.get("scope"))
        observation = FailureObservation(
            route_id=observation_raw["route_id"],
            route_generation=observation_raw["route_generation"],
            src_node_id=observation_raw["src_node_id"],
            src_endpoint_id=observation_raw["src_endpoint_id"],
            dst_node_id=observation_raw["dst_node_id"],
            dst_endpoint_id=observation_raw["dst_endpoint_id"],
            offering_id=observation_raw.get("offering_id"),
            failure_kind=observation_raw["failure_kind"],
            scope=observation_scope,
            probe_correlation_id=observation_raw["probe_correlation_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceBundleError("quarantine observation is invalid") from exc
    if scope is not observation.scope:
        raise EvidenceBundleError("quarantine scope does not match observation")
    expected_key = tuple(key_raw)
    return QuarantineEntry(expected_key, scope, observation, 0.0, 1.0)


def _snapshot_from_wire(swarm_id: str, generation: int, values: Any) -> tuple[RegistrySnapshot, tuple[dict[str, Any], ...]]:
    if not isinstance(values, list):
        raise EvidenceBundleError("records must be an array")
    parsed = []
    normalized = []
    keys = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise EvidenceBundleError("record must be an object")
        try:
            record = record_from_dict(value)
        except (TypeError, ValueError) as exc:
            raise EvidenceBundleError("record evidence is invalid") from exc
        if record.swarm_id != swarm_id:
            raise EvidenceBundleError("record swarm_id does not match bundle")
        normalized_value = record.to_dict()
        if normalized_value != dict(value):
            raise EvidenceBundleError("record wire evidence is not canonical")
        key = logical_record_key(record)
        if key in keys:
            raise EvidenceBundleError("duplicate logical record in evidence bundle")
        keys.add(key)
        parsed.append(RecordSnapshot(key, record, 0.0, 1.0, False))
        normalized.append(normalized_value)
    sorted_pairs = sorted(zip(parsed, normalized), key=lambda item: item[0].key)
    if normalized != [item[1] for item in sorted_pairs]:
        raise EvidenceBundleError("records must be sorted by logical key")
    snapshot = RegistrySnapshot(swarm_id, generation, 0.0, tuple(item[0] for item in sorted_pairs))
    return snapshot, tuple(item[1] for item in sorted_pairs)


def evidence_bundle_to_dict(bundle: EvidenceBundle) -> dict[str, Any]:
    return _json_value({
        "protocol": bundle.protocol,
        "swarm_id": bundle.swarm_id,
        "deployment": dict(bundle.deployment),
        "model": dict(bundle.model),
        "snapshot_generation": bundle.snapshot_generation,
        "records": list(bundle.records),
        "peer_states": list(bundle.peer_states),
        "quarantines": list(bundle.quarantines),
        "router_view": dict(bundle.router_view),
        "allocator_view": dict(bundle.allocator_view),
        "evidence_bundle_digest": bundle.evidence_bundle_digest,
    })


def evidence_bundle_from_dict(document: Mapping[str, Any]) -> EvidenceBundle:
    if not isinstance(document, Mapping) or set(document) != _REQUIRED_KEYS:
        raise EvidenceBundleError("evidence bundle fields are incomplete or unexpected")
    if document.get("protocol") != EVIDENCE_BUNDLE_PROTOCOL:
        raise EvidenceBundleError(f"expected {EVIDENCE_BUNDLE_PROTOCOL}")
    swarm_id = document.get("swarm_id")
    if not isinstance(swarm_id, str) or not swarm_id:
        raise EvidenceBundleError("swarm_id must be non-empty")
    generation = document.get("snapshot_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise EvidenceBundleError("snapshot_generation must be a non-negative integer")
    deployment = _validate_deployment(document.get("deployment"))
    model = _validate_model(document.get("model"))
    snapshot, records = _snapshot_from_wire(swarm_id, generation, document.get("records"))

    peers_raw = document.get("peer_states")
    if not isinstance(peers_raw, list):
        raise EvidenceBundleError("peer_states must be an array")
    peers = tuple(_peer_from_dict(value) for value in peers_raw)
    peer_wire = tuple(_peer_to_dict(peer) for peer in sorted(peers, key=lambda item: item.node_id))
    if list(peer_wire) != peers_raw or len({peer.node_id for peer in peers}) != len(peers):
        raise EvidenceBundleError("peer_states must be unique and sorted")

    quarantines_raw = document.get("quarantines")
    if not isinstance(quarantines_raw, list):
        raise EvidenceBundleError("quarantines must be an array")
    quarantines = tuple(_quarantine_from_dict(value) for value in quarantines_raw)
    quarantine_wire = tuple(_quarantine_to_dict(item) for item in sorted(quarantines, key=lambda item: item.key))
    if list(quarantine_wire) != quarantines_raw or len({entry.key for entry in quarantines}) != len(quarantines):
        raise EvidenceBundleError("quarantines must be unique and sorted")

    expected_router = _json_value(router_view_to_dict(build_router_view(snapshot, peers, quarantines)))
    expected_allocator = _json_value(allocator_view_to_dict(build_allocator_view(snapshot, peers, quarantines)))
    if document.get("router_view") != expected_router:
        raise EvidenceBundleError("router_view is not derived from bound evidence")
    if document.get("allocator_view") != expected_allocator:
        raise EvidenceBundleError("allocator_view is not derived from bound evidence")

    actual_digest = document.get("evidence_bundle_digest")
    expected_digest = evidence_bundle_digest(document)
    if not isinstance(actual_digest, str) or actual_digest != expected_digest:
        raise EvidenceBundleError("evidence_bundle_digest mismatch")

    return EvidenceBundle(
        protocol=EVIDENCE_BUNDLE_PROTOCOL,
        swarm_id=swarm_id,
        deployment=deployment,
        model=model,
        snapshot_generation=generation,
        records=records,
        peer_states=peer_wire,
        quarantines=quarantine_wire,
        router_view=copy.deepcopy(expected_router),
        allocator_view=copy.deepcopy(expected_allocator),
        evidence_bundle_digest=actual_digest,
    )


def build_evidence_bundle(
    *,
    snapshot: RegistrySnapshot,
    peer_states: Iterable[PeerState],
    quarantines: Iterable[QuarantineEntry],
    deployment_id: str,
    deployment_epoch: int,
    model_id: str,
    num_layers: int,
    manifest_digest: str,
    resolved_commit: str,
) -> EvidenceBundle:
    peers = tuple(sorted(peer_states, key=lambda item: item.node_id))
    quarantine_items = tuple(sorted(quarantines, key=lambda item: item.key))
    document: dict[str, Any] = {
        "protocol": EVIDENCE_BUNDLE_PROTOCOL,
        "swarm_id": snapshot.swarm_id,
        "deployment": {
            "deployment_id": deployment_id,
            "deployment_epoch": deployment_epoch,
        },
        "model": {
            "model_id": model_id,
            "num_layers": num_layers,
            "manifest_digest": manifest_digest,
            "resolved_commit": resolved_commit,
        },
        "snapshot_generation": snapshot.generation,
        "records": [entry.record.to_dict() for entry in snapshot.records],
        "peer_states": [_peer_to_dict(peer) for peer in peers],
        "quarantines": [_quarantine_to_dict(entry) for entry in quarantine_items],
        "router_view": _json_value(
            router_view_to_dict(build_router_view(snapshot, peers, quarantine_items))
        ),
        "allocator_view": _json_value(
            allocator_view_to_dict(build_allocator_view(snapshot, peers, quarantine_items))
        ),
    }
    document["evidence_bundle_digest"] = evidence_bundle_digest(document)
    return evidence_bundle_from_dict(document)
