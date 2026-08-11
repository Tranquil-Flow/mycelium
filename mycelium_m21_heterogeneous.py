"""Closed M21 heterogeneous membership and internet-native path projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


PROTOCOL = "mycelium.m21_heterogeneous_swarm.v1"
_DIGEST_PREFIX = "sha256:"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(code)
    return copy.deepcopy(dict(value))


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(code)
    return value


def _number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(code)
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(code)
    return result


def _sha256(value: Any, code: str) -> str:
    text = _text(value, code)
    if not text.startswith(_DIGEST_PREFIX) or len(text) != 71:
        raise ValueError(code)
    try:
        int(text.removeprefix(_DIGEST_PREFIX), 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return text


_BINDING = frozenset(
    {
        "swarm_id",
        "seed_key_digest",
        "seed_node_id",
        "deployment_id",
        "model_id",
        "model_revision",
        "membership_generation",
    }
)
_POLICY = frozenset(
    {
        "invitation_ownership",
        "operator_approval",
        "maximum_invite_ttl_seconds",
        "single_use",
        "request_quota_per_hour",
        "byte_quota_per_hour",
        "audit_retention_days",
        "revocation_supported",
        "credential_rotation_supported",
        "abuse_response",
        "permissionless_participation",
        "byzantine_resistance",
        "malicious_worker_confidentiality",
    }
)
_MEMBER = frozenset(
    {
        "member_id",
        "peer_class",
        "runtime_backend",
        "trust_state",
        "generation",
        "incarnation",
        "freshness",
        "revocation_state",
        "activation_eligible",
        "route_participant",
        "eligibility_reason",
        "connectivity",
        "external_network",
        "endpoint_identity_digest",
    }
)
_PATH = frozenset(
    {
        "source_member_id",
        "destination_member_id",
        "path_class",
        "relay_region",
        "cold_rtt_ms",
        "warm_rtt_ms",
        "jitter_ms",
        "loss_ratio",
        "goodput_bytes_per_second",
        "reconnect_count",
        "connection_generation",
        "selected_path_changes",
        "sample_count",
    }
)
_ROUTE = frozenset(
    {
        "physical",
        "route_alive",
        "heterogeneous",
        "participant_count",
        "runtime_class_count",
        "frame_count_before",
        "frame_count_after",
        "latest_output_token_count",
        "tailscale_product_dependency",
        "activation_transport",
        "operator_staging_transport",
    }
)
_FIELDS = frozenset(
    {
        "protocol",
        "generated_at_unix_ms",
        "binding",
        "policy",
        "members",
        "paths",
        "route",
        "gate_state",
        "exclusions",
        "privacy",
        "evidence_digest",
    }
)
_CLASS_RUNTIME = {
    "mac_mlx_iroh": ("mlx", True),
    "linux_numpy_iroh": ("numpy", True),
    "browser_http": ("browser", False),
    "pixel_http": ("android", False),
    "android_termux_iroh": ("pixel-stdlib", False),
    "linux_tbd": ("tbd", False),
}


def pseudonymous_member_id(node_id: str, *, salt: str) -> str:
    _text(node_id, "m21_node_id_invalid")
    _text(salt, "m21_salt_invalid")
    return "peer-" + hashlib.sha256(f"{salt}\0{node_id}".encode()).hexdigest()[:16]


def endpoint_identity_digest(endpoint_id: str) -> str:
    return _digest({"endpoint_id": _text(endpoint_id, "m21_endpoint_invalid")})


def build_heterogeneous_evidence(
    *,
    generated_at_unix_ms: int,
    binding: Mapping[str, Any],
    policy: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    paths: Sequence[Mapping[str, Any]],
    route: Mapping[str, Any],
    exclusions: Sequence[str] = (),
) -> dict[str, Any]:
    route_doc = copy.deepcopy(dict(route))
    eligible_classes = {
        str(member["runtime_backend"])
        for member in members
        if member.get("activation_eligible") is True
        and member.get("route_participant") is True
    }
    ineligible_member = any(
        member.get("activation_eligible") is False
        and member.get("route_participant") is False
        for member in members
    )
    qualified = (
        route_doc.get("physical") is True
        and route_doc.get("route_alive") is True
        and route_doc.get("heterogeneous") is True
        and route_doc.get("tailscale_product_dependency") is False
        and len(eligible_classes) >= 2
        and ineligible_member
    )
    body: dict[str, Any] = {
        "protocol": PROTOCOL,
        "generated_at_unix_ms": generated_at_unix_ms,
        "binding": copy.deepcopy(dict(binding)),
        "policy": copy.deepcopy(dict(policy)),
        "members": [copy.deepcopy(dict(member)) for member in members],
        "paths": [copy.deepcopy(dict(path)) for path in paths],
        "route": route_doc,
        "gate_state": "qualified" if qualified else "withheld",
        "exclusions": list(exclusions),
        "privacy": "no credentials, prompts, output, tensors, activations, raw endpoint ids, private addresses, paths, or usernames",
    }
    body["evidence_digest"] = _digest(body)
    return validate_heterogeneous_evidence(body)


def validate_heterogeneous_evidence(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = _closed(document, _FIELDS, "m21_evidence_invalid")
        if result["protocol"] != PROTOCOL:
            raise ValueError("m21_evidence_invalid")
        _integer(result["generated_at_unix_ms"], "m21_evidence_invalid", minimum=1)
        binding = _closed(result["binding"], _BINDING, "m21_evidence_invalid")
        for key in ("swarm_id", "seed_node_id", "deployment_id", "model_id", "model_revision"):
            _text(binding[key], "m21_evidence_invalid")
        _sha256(binding["seed_key_digest"], "m21_evidence_invalid")
        _integer(binding["membership_generation"], "m21_evidence_invalid", minimum=1)
        policy = _closed(result["policy"], _POLICY, "m21_evidence_invalid")
        for key in ("single_use", "revocation_supported", "credential_rotation_supported"):
            if policy[key] is not True:
                raise ValueError("m21_evidence_invalid")
        for key in ("permissionless_participation", "byzantine_resistance", "malicious_worker_confidentiality"):
            if policy[key] is not False:
                raise ValueError("m21_evidence_invalid")
        for key in ("maximum_invite_ttl_seconds", "request_quota_per_hour", "byte_quota_per_hour", "audit_retention_days"):
            _integer(policy[key], "m21_evidence_invalid", minimum=1)
        for key in ("invitation_ownership", "operator_approval", "abuse_response"):
            _text(policy[key], "m21_evidence_invalid")
        if not isinstance(result["members"], list) or not 1 <= len(result["members"]) <= 4096:
            raise ValueError("m21_evidence_invalid")
        member_ids: set[str] = set()
        route_member_ids: set[str] = set()
        route_runtime_classes: set[str] = set()
        has_ineligible_probe = False
        for raw in result["members"]:
            member = _closed(raw, _MEMBER, "m21_evidence_invalid")
            member_id = _text(member["member_id"], "m21_evidence_invalid")
            if member_id in member_ids:
                raise ValueError("m21_evidence_invalid")
            member_ids.add(member_id)
            for key in ("peer_class", "runtime_backend", "trust_state", "incarnation", "freshness", "revocation_state", "eligibility_reason", "connectivity"):
                _text(member[key], "m21_evidence_invalid")
            expected = _CLASS_RUNTIME.get(member["peer_class"])
            if expected is None or (member["runtime_backend"], member["activation_eligible"]) != expected:
                raise ValueError("m21_evidence_invalid")
            _integer(member["generation"], "m21_evidence_invalid", minimum=1)
            _sha256(member["endpoint_identity_digest"], "m21_evidence_invalid")
            for key in ("activation_eligible", "route_participant", "external_network"):
                if type(member[key]) is not bool:
                    raise ValueError("m21_evidence_invalid")
            if member["route_participant"] and not member["activation_eligible"]:
                raise ValueError("m21_evidence_invalid")
            if member["route_participant"]:
                route_member_ids.add(member_id)
                route_runtime_classes.add(member["runtime_backend"])
            elif not member["activation_eligible"]:
                has_ineligible_probe = True
            if member["connectivity"] not in {"direct", "relay", "unknown"}:
                raise ValueError("m21_evidence_invalid")
        if not isinstance(result["paths"], list) or len(result["paths"]) > 16384:
            raise ValueError("m21_evidence_invalid")
        for raw in result["paths"]:
            path = _closed(raw, _PATH, "m21_evidence_invalid")
            if path["source_member_id"] not in member_ids or path["destination_member_id"] not in member_ids:
                raise ValueError("m21_evidence_invalid")
            if path["source_member_id"] == path["destination_member_id"]:
                raise ValueError("m21_evidence_invalid")
            if path["path_class"] not in {"direct", "relay", "unknown"}:
                raise ValueError("m21_evidence_invalid")
            if path["relay_region"] is not None:
                _text(path["relay_region"], "m21_evidence_invalid")
            for key in ("cold_rtt_ms", "warm_rtt_ms", "jitter_ms", "loss_ratio", "goodput_bytes_per_second"):
                _number(path[key], "m21_evidence_invalid")
            if path["loss_ratio"] > 1:
                raise ValueError("m21_evidence_invalid")
            for key in ("reconnect_count", "connection_generation", "selected_path_changes", "sample_count"):
                _integer(path[key], "m21_evidence_invalid")
        route = _closed(result["route"], _ROUTE, "m21_evidence_invalid")
        for key in ("physical", "route_alive", "heterogeneous", "tailscale_product_dependency"):
            if type(route[key]) is not bool:
                raise ValueError("m21_evidence_invalid")
        for key in ("participant_count", "runtime_class_count", "frame_count_before", "frame_count_after", "latest_output_token_count"):
            _integer(route[key], "m21_evidence_invalid")
        if route["frame_count_after"] < route["frame_count_before"]:
            raise ValueError("m21_evidence_invalid")
        if (
            route["participant_count"] != len(route_member_ids)
            or route["runtime_class_count"] != len(route_runtime_classes)
            or route["heterogeneous"] != (len(route_runtime_classes) >= 2)
        ):
            raise ValueError("m21_evidence_invalid")
        if route["activation_transport"] != "endpointid_authenticated_iroh":
            raise ValueError("m21_evidence_invalid")
        _text(route["operator_staging_transport"], "m21_evidence_invalid")
        if result["gate_state"] not in {"qualified", "withheld"}:
            raise ValueError("m21_evidence_invalid")
        qualified = (
            route["physical"]
            and route["route_alive"]
            and route["heterogeneous"]
            and not route["tailscale_product_dependency"]
            and len(route_runtime_classes) >= 2
            and has_ineligible_probe
        )
        if result["gate_state"] != ("qualified" if qualified else "withheld"):
            raise ValueError("m21_evidence_invalid")
        if not isinstance(result["exclusions"], list) or any(not isinstance(item, str) or not item for item in result["exclusions"]):
            raise ValueError("m21_evidence_invalid")
        if result["privacy"] != "no credentials, prompts, output, tensors, activations, raw endpoint ids, private addresses, paths, or usernames":
            raise ValueError("m21_evidence_invalid")
        supplied = _sha256(result["evidence_digest"], "m21_evidence_invalid")
        unsigned = copy.deepcopy(result)
        del unsigned["evidence_digest"]
        if supplied != _digest(unsigned):
            raise ValueError("m21_evidence_invalid")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "m21_evidence_invalid":
            raise
        raise ValueError("m21_evidence_invalid") from exc


__all__ = [
    "PROTOCOL",
    "build_heterogeneous_evidence",
    "endpoint_identity_digest",
    "pseudonymous_member_id",
    "validate_heterogeneous_evidence",
]
