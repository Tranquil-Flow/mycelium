from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Sequence, Tuple


RECORD_PROTOCOL = "mycelium.gossip.record.v1"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FORBIDDEN_FIELDS = {
    "api_key",
    "auth_key",
    "access_token",
    "refresh_token",
    "bearer_token",
    "password",
    "private_key",
    "secret",
    "shared_secret",
}


class SchemaError(ValueError):
    pass


class RecordKind(str, Enum):
    PROFILE = "profile"
    STATUS = "status"
    LINK = "link"
    MEMBERSHIP = "membership"
    OFFERING = "offering"


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _thaw(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaError("payload is not canonical JSON") from exc


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _require_segment(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.fullmatch(value):
        raise SchemaError(f"{field} must be a safe key segment")
    return value


def _require_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SchemaError(f"{field} must be an integer >= {minimum}")
    return value


def _require_number(value: Any, field: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise SchemaError(f"{field} must be finite and >= {minimum}")
    return result


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{field} must be boolean")
    return value


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field} must be an object")
    return value


def _require_sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SchemaError(f"{field} must be an array")
    return value


def _required(payload: Mapping[str, Any], names: Sequence[str]) -> None:
    missing = [name for name in names if name not in payload]
    if missing:
        raise SchemaError("missing required field(s): " + ", ".join(missing))


def _allowed(payload: Mapping[str, Any], names: Sequence[str]) -> None:
    unknown = sorted(set(payload) - set(names))
    if unknown:
        raise SchemaError("unknown field(s): " + ", ".join(unknown))


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_FIELDS:
                raise SchemaError(f"forbidden field: {key}")
            _reject_forbidden_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_fields(item)


def _validate_endpoints(endpoints: Any) -> None:
    for index, endpoint in enumerate(_require_sequence(endpoints, "endpoints")):
        item = _require_mapping(endpoint, f"endpoints[{index}]")
        _required(item, ("endpoint_id", "transport", "host", "port", "scope", "inbound"))
        _allowed(item, ("endpoint_id", "transport", "host", "port", "scope", "inbound", "priority", "extensions"))
        _require_segment(item["endpoint_id"], f"endpoints[{index}].endpoint_id")
        _require_segment(item["transport"], f"endpoints[{index}].transport")
        if not isinstance(item["host"], str) or not item["host"] or len(item["host"]) > 255:
            raise SchemaError(f"endpoints[{index}].host is invalid")
        port = _require_int(item["port"], f"endpoints[{index}].port", 1)
        if port > 65535:
            raise SchemaError(f"endpoints[{index}].port exceeds 65535")
        if item["scope"] not in {"lan", "overlay", "manual", "relay", "loopback"}:
            raise SchemaError(f"endpoints[{index}].scope is invalid")
        _require_bool(item["inbound"], f"endpoints[{index}].inbound")


def _validate_memory_domains(domains: Any) -> None:
    seen = set()
    for index, domain in enumerate(_require_sequence(domains, "memory_domains")):
        item = _require_mapping(domain, f"memory_domains[{index}]")
        _required(item, ("memory_domain_id", "kind", "total_bytes"))
        _allowed(
            item,
            (
                "memory_domain_id",
                "kind",
                "total_bytes",
                "allocatable_after_reservations_bytes",
                "committed_bytes",
                "reclaimable_bytes",
                "reservation_generation",
                "extensions",
            ),
        )
        domain_id = _require_segment(item["memory_domain_id"], f"memory_domains[{index}].memory_domain_id")
        if domain_id in seen:
            raise SchemaError("duplicate memory_domain_id")
        seen.add(domain_id)
        if item["kind"] not in {"system", "unified", "vram", "accelerator", "other"}:
            raise SchemaError(f"memory_domains[{index}].kind is invalid")
        _require_int(item["total_bytes"], f"memory_domains[{index}].total_bytes")
        for field in (
            "allocatable_after_reservations_bytes",
            "committed_bytes",
            "reclaimable_bytes",
            "reservation_generation",
        ):
            if field in item:
                _require_int(item[field], f"memory_domains[{index}].{field}")


def _validate_profile(payload: Mapping[str, Any], origin: str) -> None:
    allowed = (
        "protocol",
        "node_id",
        "software_version",
        "protocol_versions",
        "platform",
        "architecture",
        "cpu",
        "total_ram_bytes",
        "unified_memory",
        "memory_domains",
        "accelerators",
        "backends",
        "precisions",
        "model_inventory",
        "storage",
        "endpoints",
        "policy",
        "extensions",
    )
    _required(payload, ("protocol", "node_id", "software_version", "protocol_versions", "platform", "architecture", "memory_domains", "endpoints", "policy"))
    _allowed(payload, allowed)
    if payload["protocol"] != "mycelium.device_profile.v2":
        raise SchemaError("profile protocol must be mycelium.device_profile.v2")
    if payload["node_id"] != origin:
        raise SchemaError("profile node_id must match origin_node_id")
    _require_segment(payload["node_id"], "payload.node_id")
    if not isinstance(payload["software_version"], str) or not payload["software_version"]:
        raise SchemaError("software_version must be non-empty")
    versions = _require_sequence(payload["protocol_versions"], "protocol_versions")
    if not versions or not all(isinstance(item, str) and item for item in versions):
        raise SchemaError("protocol_versions must contain strings")
    _validate_memory_domains(payload["memory_domains"])
    _validate_endpoints(payload["endpoints"])
    _require_mapping(payload["policy"], "policy")


def _validate_status(payload: Mapping[str, Any], origin: str) -> None:
    allowed = (
        "protocol",
        "node_id",
        "lifecycle",
        "memory_domains",
        "available_ram_bytes",
        "available_vram_bytes",
        "available_storage_bytes",
        "utilization",
        "thermal",
        "ac_power",
        "battery_percent",
        "queue_depth",
        "in_flight",
        "concurrency_limit",
        "error_ewma",
        "performance",
        "kv_cache_headroom_bytes",
        "active_assignments",
        "ingress_mbps",
        "egress_mbps",
        "quality",
        "extensions",
    )
    _required(payload, ("protocol", "node_id", "lifecycle", "memory_domains", "queue_depth", "in_flight", "concurrency_limit"))
    _allowed(payload, allowed)
    if payload["protocol"] != "mycelium.device_status.v1":
        raise SchemaError("status protocol must be mycelium.device_status.v1")
    if payload["node_id"] != origin:
        raise SchemaError("status node_id must match origin_node_id")
    if payload["lifecycle"] not in {"joining", "ready", "draining", "offline"}:
        raise SchemaError("status lifecycle is invalid")
    _validate_memory_domains(payload["memory_domains"])
    for field in ("queue_depth", "in_flight", "concurrency_limit"):
        _require_int(payload[field], field)
    for field in ("available_ram_bytes", "available_vram_bytes", "available_storage_bytes", "kv_cache_headroom_bytes"):
        if field in payload:
            _require_int(payload[field], field)


def _validate_link(payload: Mapping[str, Any], origin: str) -> None:
    allowed = (
        "protocol",
        "src_node_id",
        "dst_node_id",
        "src_endpoint_id",
        "dst_endpoint_id",
        "reachable",
        "connect_rtt_ema_ms",
        "rtt_p95_ms",
        "jitter_ms",
        "loss_ratio",
        "goodput_mbps",
        "sample_count",
        "measurement_method",
        "measurement_payload_bytes",
        "connection_state",
        "quality",
        "extensions",
    )
    required = (
        "protocol",
        "src_node_id",
        "dst_node_id",
        "src_endpoint_id",
        "dst_endpoint_id",
        "reachable",
        "sample_count",
        "measurement_method",
    )
    _required(payload, required)
    _allowed(payload, allowed)
    if payload["protocol"] != "mycelium.link_state.v1":
        raise SchemaError("link protocol must be mycelium.link_state.v1")
    if payload["src_node_id"] != origin:
        raise SchemaError("link src_node_id must match origin_node_id")
    for field in ("src_node_id", "dst_node_id", "src_endpoint_id", "dst_endpoint_id", "measurement_method"):
        _require_segment(payload[field], field)
    _require_bool(payload["reachable"], "reachable")
    _require_int(payload["sample_count"], "sample_count")
    for field in ("connect_rtt_ema_ms", "rtt_p95_ms", "jitter_ms", "goodput_mbps"):
        if field in payload and payload[field] is not None:
            _require_number(payload[field], field)
    if "loss_ratio" in payload:
        loss = _require_number(payload["loss_ratio"], "loss_ratio")
        if loss > 1.0:
            raise SchemaError("loss_ratio must be <= 1")


def _validate_membership(payload: Mapping[str, Any], origin: str) -> None:
    allowed = ("protocol", "subject_node_id", "subject_incarnation", "state", "reporter_node_id", "reason", "extensions")
    _required(payload, ("protocol", "subject_node_id", "subject_incarnation", "state", "reporter_node_id", "reason"))
    _allowed(payload, allowed)
    if payload["protocol"] != "mycelium.membership.v1":
        raise SchemaError("membership protocol must be mycelium.membership.v1")
    if payload["reporter_node_id"] != origin:
        raise SchemaError("membership reporter_node_id must match origin_node_id")
    _require_segment(payload["subject_node_id"], "subject_node_id")
    _require_segment(payload["reporter_node_id"], "reporter_node_id")
    _require_int(payload["subject_incarnation"], "subject_incarnation")
    if payload["state"] not in {"alive", "suspect", "dead"}:
        raise SchemaError("membership state is invalid")
    if not isinstance(payload["reason"], str) or len(payload["reason"]) > 256:
        raise SchemaError("membership reason is invalid")


def _validate_offering(payload: Mapping[str, Any], origin: str) -> None:
    allowed = (
        "protocol",
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "manifest_digest",
        "resolved_commit",
        "model_id",
        "start_layer",
        "end_layer_exclusive",
        "runtime_instance_id",
        "load_generation",
        "readiness_state",
        "proof_digest",
        "inference_endpoint_id",
        "backend",
        "precision",
        "performance",
        "extensions",
    )
    required = (
        "protocol",
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "manifest_digest",
        "resolved_commit",
        "model_id",
        "start_layer",
        "end_layer_exclusive",
        "runtime_instance_id",
        "load_generation",
        "readiness_state",
        "proof_digest",
        "inference_endpoint_id",
    )
    _required(payload, required)
    _allowed(payload, allowed)
    if payload["protocol"] != "mycelium.runtime_offering.v1":
        raise SchemaError("offering protocol must be mycelium.runtime_offering.v1")
    for field in ("deployment_id", "assignment_id", "model_id", "runtime_instance_id", "inference_endpoint_id"):
        _require_segment(payload[field], field)
    for field in ("manifest_digest", "proof_digest"):
        if not isinstance(payload[field], str) or not _DIGEST_RE.fullmatch(payload[field]):
            raise SchemaError(f"{field} must be a sha256 digest")
    if not isinstance(payload["resolved_commit"], str) or not _COMMIT_RE.fullmatch(payload["resolved_commit"]):
        raise SchemaError("resolved_commit must be an immutable hexadecimal revision")
    for field in ("deployment_epoch", "start_layer", "end_layer_exclusive", "load_generation"):
        _require_int(payload[field], field)
    if payload["end_layer_exclusive"] <= payload["start_layer"]:
        raise SchemaError("offering layer range must be non-empty and half-open")
    if payload["readiness_state"] not in {"loading", "loaded", "loaded_and_probed", "draining", "failed"}:
        raise SchemaError("readiness_state is invalid")


_VALIDATORS = {
    RecordKind.PROFILE: _validate_profile,
    RecordKind.STATUS: _validate_status,
    RecordKind.LINK: _validate_link,
    RecordKind.MEMBERSHIP: _validate_membership,
    RecordKind.OFFERING: _validate_offering,
}


@dataclass(frozen=True)
class RecordEnvelope:
    protocol: str
    swarm_id: str
    kind: RecordKind
    origin_node_id: str
    incarnation: int
    sequence: int
    boot_id: str
    generated_at_unix_ms: int
    ttl_ms: int
    payload_hash: str
    payload: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "swarm_id": self.swarm_id,
            "kind": self.kind.value,
            "origin_node_id": self.origin_node_id,
            "incarnation": self.incarnation,
            "sequence": self.sequence,
            "boot_id": self.boot_id,
            "generated_at_unix_ms": self.generated_at_unix_ms,
            "ttl_ms": self.ttl_ms,
            "payload_hash": self.payload_hash,
            "payload": _thaw(self.payload),
        }


def _validate_envelope_fields(
    *,
    swarm_id: Any,
    kind: Any,
    origin_node_id: Any,
    incarnation: Any,
    sequence: Any,
    boot_id: Any,
    generated_at_unix_ms: Any,
    ttl_ms: Any,
    payload: Any,
) -> Tuple[str, RecordKind, str, int, int, str, int, int, Mapping[str, Any]]:
    swarm = _require_segment(swarm_id, "swarm_id")
    try:
        record_kind = kind if isinstance(kind, RecordKind) else RecordKind(kind)
    except (TypeError, ValueError) as exc:
        raise SchemaError("kind is unsupported") from exc
    origin = _require_segment(origin_node_id, "origin_node_id")
    incarnation_value = _require_int(incarnation, "incarnation", 1)
    sequence_value = _require_int(sequence, "sequence", 0)
    boot = _require_segment(boot_id, "boot_id")
    generated = _require_int(generated_at_unix_ms, "generated_at_unix_ms")
    ttl = _require_int(ttl_ms, "ttl_ms", 1)
    if ttl > 7 * 24 * 60 * 60 * 1000:
        raise SchemaError("ttl_ms exceeds seven-day hard cap")
    payload_mapping = _require_mapping(payload, "payload")
    _reject_forbidden_fields(payload_mapping)
    _VALIDATORS[record_kind](payload_mapping, origin)
    return swarm, record_kind, origin, incarnation_value, sequence_value, boot, generated, ttl, payload_mapping


def build_record(
    *,
    swarm_id: str,
    kind: RecordKind,
    origin_node_id: str,
    incarnation: int,
    sequence: int,
    boot_id: str,
    generated_at_unix_ms: int,
    ttl_ms: int,
    payload: Mapping[str, Any],
) -> RecordEnvelope:
    swarm, record_kind, origin, inc, seq, boot, generated, ttl, validated_payload = _validate_envelope_fields(
        swarm_id=swarm_id,
        kind=kind,
        origin_node_id=origin_node_id,
        incarnation=incarnation,
        sequence=sequence,
        boot_id=boot_id,
        generated_at_unix_ms=generated_at_unix_ms,
        ttl_ms=ttl_ms,
        payload=payload,
    )
    digest = canonical_payload_hash(validated_payload)
    return RecordEnvelope(
        protocol=RECORD_PROTOCOL,
        swarm_id=swarm,
        kind=record_kind,
        origin_node_id=origin,
        incarnation=inc,
        sequence=seq,
        boot_id=boot,
        generated_at_unix_ms=generated,
        ttl_ms=ttl,
        payload_hash=digest,
        payload=_freeze(validated_payload),
    )


def record_from_dict(value: Mapping[str, Any]) -> RecordEnvelope:
    required = (
        "protocol",
        "swarm_id",
        "kind",
        "origin_node_id",
        "incarnation",
        "sequence",
        "boot_id",
        "generated_at_unix_ms",
        "ttl_ms",
        "payload_hash",
        "payload",
    )
    _required(value, required)
    _allowed(value, required)
    if value["protocol"] != RECORD_PROTOCOL:
        raise SchemaError(f"protocol must be {RECORD_PROTOCOL}")
    record = build_record(
        swarm_id=value["swarm_id"],
        kind=value["kind"],
        origin_node_id=value["origin_node_id"],
        incarnation=value["incarnation"],
        sequence=value["sequence"],
        boot_id=value["boot_id"],
        generated_at_unix_ms=value["generated_at_unix_ms"],
        ttl_ms=value["ttl_ms"],
        payload=value["payload"],
    )
    if value["payload_hash"] != record.payload_hash:
        raise SchemaError("payload hash mismatch")
    return record


def logical_record_key(record: RecordEnvelope) -> Tuple[str, ...]:
    base = (record.swarm_id, record.origin_node_id, record.kind.value)
    payload = record.payload
    if record.kind is RecordKind.LINK:
        return base + (
            str(payload["src_endpoint_id"]),
            str(payload["dst_node_id"]),
            str(payload["dst_endpoint_id"]),
        )
    if record.kind is RecordKind.MEMBERSHIP:
        return base + (str(payload["subject_node_id"]),)
    if record.kind is RecordKind.OFFERING:
        return base + (
            str(payload["deployment_id"]),
            str(payload["assignment_id"]),
            str(payload["inference_endpoint_id"]),
        )
    return base


def transport_key(record: RecordEnvelope) -> str:
    key = logical_record_key(record)
    return "/".join(("mycelium",) + key)
