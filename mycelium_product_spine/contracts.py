# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict privacy-reduced contracts for the unified M12 product evidence spine."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from typing import Any, Callable, Mapping


SNAPSHOT_PROTOCOL = "mycelium.product_snapshot.v1"
EVENT_PROTOCOL = "mycelium.product_event.v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_ITEMS = 4_096
ENTITY_KINDS = (
    "artifact",
    "assignment",
    "device",
    "directed_link",
    "incident",
    "load_proof",
    "qualification",
    "request",
    "route",
    "runtime_kv_ownership",
    "source_provenance",
    "stage",
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")
_MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})?\Z"
)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HOST_PORT = re.compile(r"(?:[A-Za-z0-9.-]+):[0-9]{1,5}\Z")
_CREDENTIAL = re.compile(
    r"(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|"
    r"\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)",
    re.IGNORECASE,
)
_DENIED_KEYS = {
    "activation",
    "activations",
    "api_key",
    "auth_token",
    "bearer_token",
    "completion",
    "decoded_output",
    "endpoint_addr",
    "endpoint_addrs",
    "hidden_state",
    "hidden_states",
    "hostname",
    "input_ids",
    "invite_nonce",
    "ip",
    "ip_address",
    "kv_cache",
    "logits",
    "message",
    "model_weights",
    "output_ids",
    "password",
    "path",
    "private_key",
    "prompt",
    "reservation_id",
    "secret",
    "tensor",
    "tensors",
    "token",
    "token_ids",
    "tokens",
}


class ProductContractError(ValueError):
    """Stable validation failure that never embeds rejected source data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _exact(value: object, fields: set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) == fields


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _code(value: object) -> bool:
    return isinstance(value, str) and _CODE.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _integer(value: object, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= MAX_SAFE_INTEGER
    )


def _optional_identifier(value: object) -> bool:
    return value is None or _identifier(value)


def _optional_integer(value: object, *, minimum: int = 0) -> bool:
    return value is None or _integer(value, minimum=minimum)


def _optional_digest(value: object) -> bool:
    return value is None or _digest(value)


def _safe_public_string(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return False
    lowered = value.lower()
    if (
        "/" in value
        or "\\" in value
        or "://" in value
        or _HOST_PORT.fullmatch(value) is not None
        or _CREDENTIAL.search(value) is not None
        or lowered == "localhost"
        or lowered.endswith((".local", ".lan", ".internal"))
    ):
        return False
    try:
        ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _model_id(value: object) -> bool:
    return isinstance(value, str) and _MODEL_ID.fullmatch(value) is not None


def _privacy_walk(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _DENIED_KEYS:
                return False
            if not _privacy_walk(item):
                return False
        return True
    if isinstance(value, list):
        return len(value) <= MAX_ITEMS and all(_privacy_walk(item) for item in value)
    if isinstance(value, str):
        return _CREDENTIAL.search(value) is None
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


def _binding(value: object) -> bool:
    fields = {
        "deployment_id",
        "deployment_epoch",
        "route_id",
        "route_generation",
        "topology_version",
    }
    return (
        _exact(value, fields)
        and _optional_identifier(value["deployment_id"])
        and _optional_integer(value["deployment_epoch"])
        and _optional_identifier(value["route_id"])
        and _optional_integer(value["route_generation"])
        and _optional_integer(value["topology_version"])
    )


def _freshness(value: object) -> bool:
    fields = {"status", "observed_at_unix_ms", "valid_until_unix_ms"}
    if not _exact(value, fields):
        return False
    status = value["status"]
    observed = value["observed_at_unix_ms"]
    valid_until = value["valid_until_unix_ms"]
    return (
        status in {"current", "stale", "missing", "conflict", "unsupported", "replay"}
        and _optional_integer(observed)
        and _optional_integer(valid_until)
        and (
            observed is None
            or valid_until is None
            or int(valid_until) >= int(observed)
        )
    )


def _identifier_list(value: object, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and len(set(value)) == len(value)
        and all(_identifier(item) for item in value)
    )


def _code_list(value: object, *, maximum: int = 128) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and len(set(value)) == len(value)
        and all(_code(item) for item in value)
    )


def _device(value: object) -> bool:
    fields = {
        "peer_class",
        "membership_generation",
        "authority_generation",
        "incarnation",
        "lifecycle",
        "lease_freshness",
        "runtime_backend",
        "transport",
        "activation_protocol",
        "activation_eligible",
        "placement_id",
    }
    return (
        _exact(value, fields)
        and _identifier(value["peer_class"])
        and _integer(value["membership_generation"], minimum=1)
        and _integer(value["authority_generation"], minimum=1)
        and _identifier(value["incarnation"])
        and _code(value["lifecycle"])
        and value["lease_freshness"] in {"fresh", "stale", "expired"}
        and _identifier(value["runtime_backend"])
        and _identifier(value["transport"])
        and _optional_identifier(value["activation_protocol"])
        and type(value["activation_eligible"]) is bool
        and _optional_identifier(value["placement_id"])
    )


def _directed_link(value: object) -> bool:
    fields = {
        "src_device_id",
        "dst_device_id",
        "connectivity",
        "measurement_digest",
    }
    return (
        _exact(value, fields)
        and _identifier(value["src_device_id"])
        and _identifier(value["dst_device_id"])
        and value["src_device_id"] != value["dst_device_id"]
        and value["connectivity"] in {"unknown", "measured"}
        and _optional_digest(value["measurement_digest"])
        and (
            value["connectivity"] == "measured"
            or value["measurement_digest"] is None
        )
    )


def _stage(value: object) -> bool:
    fields = {
        "stage_index",
        "start_layer",
        "end_layer_exclusive",
        "component_roles",
        "decode_mode",
    }
    return (
        _exact(value, fields)
        and _integer(value["stage_index"])
        and _integer(value["start_layer"])
        and _integer(value["end_layer_exclusive"], minimum=1)
        and value["end_layer_exclusive"] > value["start_layer"]
        and _identifier_list(value["component_roles"])
        and value["decode_mode"] in {"stage_local_kv", "complete_context_replay"}
    )


def _route(value: object) -> bool:
    fields = {
        "deployment_id",
        "model_id",
        "topology_version",
        "decode_mode",
        "placement_provenance",
        "route_alive",
        "concurrency_eligible",
        "cancellation_cleanup_bound_ms",
        "publisher_generation_fenced",
        "scoped_liveness_proven",
    }
    return (
        _exact(value, fields)
        and _identifier(value["deployment_id"])
        and _model_id(value["model_id"])
        and _integer(value["topology_version"])
        and value["decode_mode"] in {"stage_local_kv", "complete_context_replay"}
        and value["placement_provenance"] in {
            "operator_selected",
            "planner_v2",
            "frozen_fixture",
        }
        and type(value["route_alive"]) is bool
        and type(value["concurrency_eligible"]) is bool
        and _integer(value["cancellation_cleanup_bound_ms"], minimum=1)
        and type(value["publisher_generation_fenced"]) is bool
        and type(value["scoped_liveness_proven"]) is bool
    )


def _assignment(value: object) -> bool:
    fields = {
        "device_id",
        "stage_id",
        "membership_generation",
        "load_generation",
        "assignment_digest",
        "stage_pack_digest",
    }
    return (
        _exact(value, fields)
        and _identifier(value["device_id"])
        and _identifier(value["stage_id"])
        and _integer(value["membership_generation"], minimum=1)
        and _integer(value["load_generation"], minimum=1)
        and _digest(value["assignment_digest"])
        and _digest(value["stage_pack_digest"])
    )


def _artifact(value: object) -> bool:
    fields = {"artifact_digest", "size_bytes", "cache_state"}
    return (
        _exact(value, fields)
        and _digest(value["artifact_digest"])
        and _integer(value["size_bytes"])
        and value["cache_state"] in {"verified", "missing", "corrupt", "unknown"}
    )


def _load_proof(value: object) -> bool:
    fields = {"proof_digest", "assignment_id", "load_generation", "ready"}
    return (
        _exact(value, fields)
        and _digest(value["proof_digest"])
        and _identifier(value["assignment_id"])
        and _integer(value["load_generation"], minimum=1)
        and type(value["ready"]) is bool
    )


def _runtime_kv(value: object) -> bool:
    fields = {
        "device_id",
        "stage_id",
        "decode_mode",
        "kv_state_count",
        "kv_byte_budget",
        "proof_digest",
    }
    return (
        _exact(value, fields)
        and _identifier(value["device_id"])
        and _identifier(value["stage_id"])
        and value["decode_mode"] in {"stage_local_kv", "complete_context_replay"}
        and _integer(value["kv_state_count"])
        and _integer(value["kv_byte_budget"])
        and _optional_digest(value["proof_digest"])
    )


def _request(value: object) -> bool:
    fields = {"state", "path_attempt", "sequence", "qualification_id"}
    return (
        _exact(value, fields)
        and value["state"] in {"accepted", "running", "completed", "failed", "cancelled"}
        and _integer(value["path_attempt"], minimum=1)
        and _integer(value["sequence"])
        and _identifier(value["qualification_id"])
    )


def _incident(value: object) -> bool:
    fields = {"state", "reason_code", "observed_at_unix_ms"}
    return (
        _exact(value, fields)
        and _code(value["state"])
        and _code(value["reason_code"])
        and _integer(value["observed_at_unix_ms"])
    )


def _qualification(value: object) -> bool:
    fields = {
        "qualification_digest",
        "route_ready",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "reason_codes",
    }
    return (
        _exact(value, fields)
        and _digest(value["qualification_digest"])
        and type(value["route_ready"]) is bool
        and _integer(value["issued_at_unix_ms"])
        and _optional_integer(value["expires_at_unix_ms"])
        and (
            value["expires_at_unix_ms"] is None
            or value["expires_at_unix_ms"] >= value["issued_at_unix_ms"]
        )
        and _code_list(value["reason_codes"])
    )


def _source_provenance(value: object) -> bool:
    fields = {"authority", "source_protocol", "source_generation", "evidence_digest"}
    return (
        _exact(value, fields)
        and _safe_public_string(value["authority"])
        and _identifier(value["source_protocol"])
        and _optional_integer(value["source_generation"])
        and _optional_digest(value["evidence_digest"])
    )


_ATTRIBUTE_VALIDATORS: dict[str, Callable[[object], bool]] = {
    "artifact": _artifact,
    "assignment": _assignment,
    "device": _device,
    "directed_link": _directed_link,
    "incident": _incident,
    "load_proof": _load_proof,
    "qualification": _qualification,
    "request": _request,
    "route": _route,
    "runtime_kv_ownership": _runtime_kv,
    "source_provenance": _source_provenance,
    "stage": _stage,
}


def _publication(value: object) -> bool:
    fields = {
        "snapshot_id",
        "generation",
        "cursor",
        "published_at_unix_ms",
        "source_mode",
    }
    return (
        _exact(value, fields)
        and _identifier(value["snapshot_id"])
        and _integer(value["generation"], minimum=1)
        and _integer(value["cursor"])
        and _integer(value["published_at_unix_ms"])
        and value["source_mode"] in {"fixture", "replay", "degraded", "live"}
    )


def _source_state(value: object) -> bool:
    fields = {
        "source_id",
        "authority",
        "status",
        "observed_at_unix_ms",
        "valid_until_unix_ms",
        "generation",
        "reason_code",
    }
    if not _exact(value, fields):
        return False
    status = value["status"]
    reason = value["reason_code"]
    return (
        _identifier(value["source_id"])
        and _safe_public_string(value["authority"])
        and status in {"current", "stale", "missing", "conflict", "unsupported", "replay"}
        and _optional_integer(value["observed_at_unix_ms"])
        and _optional_integer(value["valid_until_unix_ms"])
        and _optional_integer(value["generation"])
        and (reason is None if status in {"current", "replay"} else _code(reason))
    )


def _entity(value: object) -> bool:
    fields = {
        "entity_id",
        "kind",
        "label",
        "source_id",
        "binding",
        "freshness",
        "attributes",
    }
    if not _exact(value, fields):
        return False
    kind = value["kind"]
    validator = _ATTRIBUTE_VALIDATORS.get(kind)
    return (
        validator is not None
        and _identifier(value["entity_id"])
        and _safe_public_string(value["label"])
        and _identifier(value["source_id"])
        and _binding(value["binding"])
        and _freshness(value["freshness"])
        and validator(value["attributes"])
    )


def _relation(value: object) -> bool:
    fields = {"relation_id", "kind", "from_entity_id", "to_entity_id", "source_id"}
    return (
        _exact(value, fields)
        and _identifier(value["relation_id"])
        and value["kind"]
        in {
            "assigned_to",
            "observes",
            "owns",
            "placed_on",
            "produced_by",
            "qualifies",
            "reports",
        }
        and _identifier(value["from_entity_id"])
        and _identifier(value["to_entity_id"])
        and value["from_entity_id"] != value["to_entity_id"]
        and _identifier(value["source_id"])
    )


def _readiness(value: object) -> bool:
    fields = {"scope_id", "dimension", "state", "reason_code", "source_id"}
    return (
        _exact(value, fields)
        and _identifier(value["scope_id"])
        and value["dimension"]
        in {
            "artifacts",
            "membership",
            "product_source",
            "qualification",
            "route_challenge",
            "runtime",
            "transport",
        }
        and value["state"] in {"ready", "not_ready", "unknown", "unsupported"}
        and (value["reason_code"] is None or _code(value["reason_code"]))
        and _identifier(value["source_id"])
    )


def _notice(value: object) -> bool:
    fields = {"notice_id", "scope_id", "severity", "code", "source_id"}
    return (
        _exact(value, fields)
        and _identifier(value["notice_id"])
        and _identifier(value["scope_id"])
        and value["severity"] in {"info", "warning", "error"}
        and _code(value["code"])
        and _identifier(value["source_id"])
    )


def _provenance(value: object) -> bool:
    fields = {"projector", "projector_version", "source_mode"}
    return (
        _exact(value, fields)
        and _safe_public_string(value["projector"])
        and _identifier(value["projector_version"])
        and value["source_mode"] in {"fixture", "replay", "degraded", "live"}
    )


def _unique(items: object, field: str) -> bool:
    return (
        isinstance(items, list)
        and len(items) <= MAX_ITEMS
        and all(isinstance(item, Mapping) for item in items)
        and len({item.get(field) for item in items}) == len(items)
    )


def validate_product_snapshot(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one closed privacy-reduced M12 product snapshot."""

    fields = {
        "protocol",
        "publication",
        "supported_entity_kinds",
        "source_states",
        "entities",
        "relations",
        "readiness",
        "notices",
        "provenance",
    }
    if not _exact(document, fields) or document["protocol"] != SNAPSHOT_PROTOCOL:
        raise ProductContractError("product_snapshot_invalid")
    if (
        document["supported_entity_kinds"] != list(ENTITY_KINDS)
        or not _publication(document["publication"])
        or not _unique(document["source_states"], "source_id")
        or not all(_source_state(item) for item in document["source_states"])
        or not _unique(document["entities"], "entity_id")
        or not all(_entity(item) for item in document["entities"])
        or not _unique(document["relations"], "relation_id")
        or not all(_relation(item) for item in document["relations"])
        or not isinstance(document["readiness"], list)
        or len(document["readiness"]) > MAX_ITEMS
        or not all(_readiness(item) for item in document["readiness"])
        or not _unique(document["notices"], "notice_id")
        or not all(_notice(item) for item in document["notices"])
        or not _provenance(document["provenance"])
        or document["publication"]["source_mode"]
        != document["provenance"]["source_mode"]
        or not _privacy_walk(document)
    ):
        raise ProductContractError("product_snapshot_invalid")
    source_ids = {item["source_id"] for item in document["source_states"]}
    entity_ids = {item["entity_id"] for item in document["entities"]}
    if any(item["source_id"] not in source_ids for item in document["entities"]):
        raise ProductContractError("product_snapshot_source_unbound")
    if any(
        item["source_id"] not in source_ids
        or item["from_entity_id"] not in entity_ids
        or item["to_entity_id"] not in entity_ids
        for item in document["relations"]
    ):
        raise ProductContractError("product_snapshot_relation_unbound")
    if any(item["source_id"] not in source_ids for item in document["readiness"]):
        raise ProductContractError("product_snapshot_readiness_unbound")
    if any(item["source_id"] not in source_ids for item in document["notices"]):
        raise ProductContractError("product_snapshot_notice_unbound")
    try:
        return json.loads(
            json.dumps(
                dict(document),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProductContractError("product_snapshot_invalid") from exc


def validate_product_event(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one complete-snapshot event with an explicit cursor predecessor."""

    fields = {"protocol", "cursor", "previous_cursor", "event_kind", "snapshot"}
    if (
        not _exact(document, fields)
        or document["protocol"] != EVENT_PROTOCOL
        or not _integer(document["cursor"], minimum=1)
        or not _integer(document["previous_cursor"])
        or document["cursor"] != document["previous_cursor"] + 1
        or document["event_kind"]
        not in {"conflict", "snapshot_published", "source_degraded", "source_recovered"}
    ):
        raise ProductContractError("product_event_invalid")
    snapshot = validate_product_snapshot(document["snapshot"])
    if snapshot["publication"]["cursor"] != document["cursor"]:
        raise ProductContractError("product_event_snapshot_unbound")
    return {
        "protocol": EVENT_PROTOCOL,
        "cursor": document["cursor"],
        "previous_cursor": document["previous_cursor"],
        "event_kind": document["event_kind"],
        "snapshot": snapshot,
    }
