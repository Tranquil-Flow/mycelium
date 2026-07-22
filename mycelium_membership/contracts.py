"""Strict signed control-plane contracts for swarm membership."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)

JOIN_REQUEST_PROTOCOL = "mycelium.membership.join_request.v1"
JOIN_ACCEPTANCE_PROTOCOL = "mycelium.membership.join_acceptance.v1"
CAPABILITY_REPORT_PROTOCOL = "mycelium.membership.capability_report.v1"
LINK_PROBE_REPORT_PROTOCOL = "mycelium.membership.link_probe_report.v1"
HEARTBEAT_PROTOCOL = "mycelium.membership.heartbeat.v1"
ASSIGNMENT_OFFER_PROTOCOL = "mycelium.membership.assignment_offer.v1"
ASSIGNMENT_RESULT_PROTOCOL = "mycelium.membership.assignment_result.v1"
DRAIN_ACK_PROTOCOL = "mycelium.membership.drain_ack.v1"
SIGNED_MESSAGE_PROTOCOL = "mycelium.membership.signed_message.v1"

MAX_MESSAGE_TTL_SECONDS = 3_600.0
MAX_CLOCK_SKEW_SECONDS = 30.0

_COMMON_FIELDS = frozenset(
    {
        "protocol",
        "message_id",
        "swarm_id",
        "sender_node_id",
        "sender_endpoint_id",
        "recipient_node_id",
        "incarnation",
        "generation",
        "issued_at",
        "expires_at",
    }
)
_SPECIFIC_FIELDS = {
    JOIN_REQUEST_PROTOCOL: frozenset(
        {"invite_nonce", "endpoint_addr", "software_version"}
    ),
    JOIN_ACCEPTANCE_PROTOCOL: frozenset(
        {
            "request_message_id",
            "accepted_node_id",
            "accepted_incarnation",
            "membership_generation",
            "lease_expires_at",
        }
    ),
    CAPABILITY_REPORT_PROTOCOL: frozenset(
        {
            "platform",
            "architecture",
            "memory_bytes",
            "available_storage_bytes",
            "backends",
            "precisions",
        }
    ),
    LINK_PROBE_REPORT_PROTOCOL: frozenset(
        {
            "target_node_id",
            "target_endpoint_id",
            "reachable",
            "rtt_ms",
            "goodput_bytes_per_second",
            "probe_sequence",
        }
    ),
    HEARTBEAT_PROTOCOL: frozenset(
        {"heartbeat_sequence", "lifecycle_state", "route_ready", "active_requests"}
    ),
    ASSIGNMENT_OFFER_PROTOCOL: frozenset(
        {
            "deployment_id",
            "deployment_epoch",
            "assignment_id",
            "assignment_digest",
            "stage_pack_digest",
            "graph_digest",
            "load_generation",
        }
    ),
    ASSIGNMENT_RESULT_PROTOCOL: frozenset(
        {
            "deployment_id",
            "deployment_epoch",
            "assignment_id",
            "accepted",
            "result_code",
            "load_proof_digest",
            "runtime_endpoint",
        }
    ),
    DRAIN_ACK_PROTOCOL: frozenset(
        {"drain_id", "active_requests", "last_request_id", "completed_at"}
    ),
}
_ENVELOPE_FIELDS = frozenset(
    {"protocol", "message", "signature", "verification_key"}
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIFECYCLE_STATES = frozenset(
    {"NEW", "CONFIGURED", "RUNNING", "DRAINING", "STOPPING", "STOPPED"}
)


class MembershipContractError(ValueError):
    """Fail-closed schema or signature error carrying one stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise MembershipContractError(code)


def _segment(value: Any) -> bool:
    return isinstance(value, str) and _SEGMENT_RE.fullmatch(value) is not None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= minimum
    )


def _text(value: Any, *, maximum: int = 512) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value.encode("utf-8")) <= maximum
    )


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _string_array(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and len(value) <= 64
        and all(_segment(item) for item in value)
        and len(set(value)) == len(value)
    )


def _validate_common(message: Mapping[str, Any], protocol: str) -> None:
    expected = _COMMON_FIELDS | _SPECIFIC_FIELDS[protocol]
    _require(set(message) == expected, "membership_fields_invalid")
    for field in (
        "message_id",
        "swarm_id",
        "sender_node_id",
        "sender_endpoint_id",
        "recipient_node_id",
        "incarnation",
    ):
        _require(_segment(message[field]), "membership_identifier_invalid")
    _require(_integer(message["generation"]), "membership_generation_invalid")
    issued_at = _number(message["issued_at"])
    expires_at = _number(message["expires_at"])
    _require(issued_at is not None and expires_at is not None, "membership_time_invalid")
    _require(expires_at > issued_at, "membership_time_invalid")
    _require(
        expires_at - issued_at <= MAX_MESSAGE_TTL_SECONDS,
        "membership_ttl_invalid",
    )
    if protocol == JOIN_REQUEST_PROTOCOL:
        _require(message["generation"] == 0, "membership_join_generation_invalid")
    else:
        _require(message["generation"] >= 1, "membership_generation_invalid")


def _validate_join_request(message: Mapping[str, Any]) -> None:
    _require(_segment(message["invite_nonce"]), "membership_identifier_invalid")
    _require(_text(message["software_version"], maximum=128), "membership_text_invalid")
    endpoint = message["endpoint_addr"]
    _require(
        isinstance(endpoint, Mapping) and set(endpoint) == {"id", "addrs"},
        "membership_endpoint_addr_invalid",
    )
    _require(
        endpoint["id"] == message["sender_endpoint_id"],
        "membership_endpoint_id_mismatch",
    )
    addrs = endpoint["addrs"]
    _require(
        isinstance(addrs, list)
        and bool(addrs)
        and len(addrs) <= 16
        and all(_text(item, maximum=1_024) for item in addrs),
        "membership_endpoint_addr_invalid",
    )


def _validate_join_acceptance(message: Mapping[str, Any]) -> None:
    for field in (
        "request_message_id",
        "accepted_node_id",
        "accepted_incarnation",
    ):
        _require(_segment(message[field]), "membership_identifier_invalid")
    _require(
        message["accepted_node_id"] == message["recipient_node_id"],
        "membership_recipient_mismatch",
    )
    _require(
        message["membership_generation"] == message["generation"],
        "membership_generation_invalid",
    )
    lease = _number(message["lease_expires_at"])
    issued = _number(message["issued_at"])
    _require(lease is not None and issued is not None and lease > issued, "membership_time_invalid")


def _validate_capability(message: Mapping[str, Any]) -> None:
    _require(_text(message["platform"], maximum=128), "membership_text_invalid")
    _require(_segment(message["architecture"]), "membership_identifier_invalid")
    for field in ("memory_bytes", "available_storage_bytes"):
        _require(_integer(message[field]), "membership_integer_invalid")
    for field in ("backends", "precisions"):
        _require(_string_array(message[field]), "membership_array_invalid")


def _validate_link_probe(message: Mapping[str, Any]) -> None:
    for field in ("target_node_id", "target_endpoint_id"):
        _require(_segment(message[field]), "membership_identifier_invalid")
    _require(
        message["target_node_id"] == message["recipient_node_id"],
        "membership_recipient_mismatch",
    )
    _require(isinstance(message["reachable"], bool), "membership_boolean_invalid")
    for field in ("rtt_ms", "goodput_bytes_per_second"):
        value = _number(message[field])
        _require(value is not None and value >= 0.0, "membership_number_invalid")
    _require(_integer(message["probe_sequence"]), "membership_integer_invalid")


def _validate_heartbeat(message: Mapping[str, Any]) -> None:
    _require(_integer(message["heartbeat_sequence"]), "membership_integer_invalid")
    _require(message["lifecycle_state"] in _LIFECYCLE_STATES, "membership_lifecycle_invalid")
    _require(message["route_ready"] is False, "membership_route_ready_invalid")
    _require(_integer(message["active_requests"]), "membership_integer_invalid")


def _validate_assignment_identity(message: Mapping[str, Any]) -> None:
    for field in ("deployment_id", "assignment_id"):
        _require(_segment(message[field]), "membership_identifier_invalid")
    _require(_integer(message["deployment_epoch"]), "membership_integer_invalid")


def _validate_assignment_offer(message: Mapping[str, Any]) -> None:
    _validate_assignment_identity(message)
    for field in ("assignment_digest", "stage_pack_digest", "graph_digest"):
        _require(_digest(message[field]), "membership_digest_invalid")
    _require(_integer(message["load_generation"], minimum=1), "membership_integer_invalid")


def _validate_assignment_result(message: Mapping[str, Any]) -> None:
    _validate_assignment_identity(message)
    _require(isinstance(message["accepted"], bool), "membership_boolean_invalid")
    _require(_segment(message["result_code"]), "membership_identifier_invalid")
    proof = message["load_proof_digest"]
    endpoint = message["runtime_endpoint"]
    if message["accepted"]:
        _require(_digest(proof), "membership_digest_invalid")
        _require(_text(endpoint, maximum=1_024), "membership_text_invalid")
    else:
        _require(proof is None and endpoint is None, "membership_result_shape_invalid")


def _validate_drain_ack(message: Mapping[str, Any]) -> None:
    _require(_segment(message["drain_id"]), "membership_identifier_invalid")
    _require(message["active_requests"] == 0, "membership_active_requests_invalid")
    last = message["last_request_id"]
    _require(last is None or _segment(last), "membership_identifier_invalid")
    completed = _number(message["completed_at"])
    issued = _number(message["issued_at"])
    expires = _number(message["expires_at"])
    _require(
        completed is not None
        and issued is not None
        and expires is not None
        and issued <= completed <= expires,
        "membership_time_invalid",
    )


_VALIDATORS = {
    JOIN_REQUEST_PROTOCOL: _validate_join_request,
    JOIN_ACCEPTANCE_PROTOCOL: _validate_join_acceptance,
    CAPABILITY_REPORT_PROTOCOL: _validate_capability,
    LINK_PROBE_REPORT_PROTOCOL: _validate_link_probe,
    HEARTBEAT_PROTOCOL: _validate_heartbeat,
    ASSIGNMENT_OFFER_PROTOCOL: _validate_assignment_offer,
    ASSIGNMENT_RESULT_PROTOCOL: _validate_assignment_result,
    DRAIN_ACK_PROTOCOL: _validate_drain_ack,
}


def validate_membership_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact schema and return a detached JSON-compatible copy."""

    _require(isinstance(message, Mapping), "membership_message_invalid")
    protocol = message.get("protocol")
    _require(protocol in _SPECIFIC_FIELDS, "membership_protocol_invalid")
    _validate_common(message, protocol)
    _VALIDATORS[protocol](message)
    try:
        return json.loads(canonical_json_bytes(dict(message)))
    except Exception as exc:
        raise MembershipContractError("membership_message_invalid") from exc


def sign_membership_message(
    *,
    signer: Ed25519EvidenceSigner,
    message: Mapping[str, Any],
) -> dict[str, Any]:
    """Sign one validated message and include only the public verification key."""

    validated = validate_membership_message(message)
    _require(
        signer.endpoint_id == validated["sender_endpoint_id"],
        "membership_signer_endpoint_mismatch",
    )
    return {
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": validated,
        "signature": signer.sign(validated),
        "verification_key": signer.public_key_record(),
    }


def _verify_envelope(
    envelope: Mapping[str, Any],
    *,
    now: float,
    expected_key_digest: str | None,
    expected_protocol: str,
) -> dict[str, Any]:
    current = _number(now)
    _require(current is not None, "membership_time_invalid")
    _require(
        isinstance(envelope, Mapping)
        and set(envelope) == _ENVELOPE_FIELDS
        and envelope.get("protocol") == SIGNED_MESSAGE_PROTOCOL,
        "membership_envelope_invalid",
    )
    message = validate_membership_message(envelope["message"])
    _require(message["protocol"] == expected_protocol, "membership_protocol_mismatch")
    record = envelope["verification_key"]
    signature = envelope["signature"]
    _require(isinstance(record, Mapping), "membership_verification_key_invalid")
    record_digest = record.get("verification_key_digest")
    if expected_key_digest is not None:
        _require(_digest(expected_key_digest), "membership_key_pin_invalid")
        _require(
            record_digest == expected_key_digest,
            "membership_key_pin_mismatch",
        )
    _require(
        isinstance(signature, Mapping)
        and signature.get("signer_endpoint_id") == message["sender_endpoint_id"],
        "membership_signer_endpoint_mismatch",
    )
    try:
        verifier = build_ed25519_verifier([record])
        valid = verifier(canonical_json_bytes(message), dict(signature))
    except Exception as exc:
        raise MembershipContractError("membership_verification_key_invalid") from exc
    _require(valid, "membership_signature_invalid")
    _require(current <= float(message["expires_at"]), "membership_message_expired")
    _require(
        current + MAX_CLOCK_SKEW_SECONDS >= float(message["issued_at"]),
        "membership_message_from_future",
    )
    return message


def verify_membership_message(
    envelope: Mapping[str, Any],
    *,
    now: float,
    expected_key_digest: str,
    expected_protocol: str,
) -> dict[str, Any]:
    """Verify a post-join message against an already pinned public-key digest."""

    _require(_text(expected_key_digest, maximum=128), "membership_key_pin_invalid")
    _require(expected_protocol in _SPECIFIC_FIELDS, "membership_protocol_invalid")
    return _verify_envelope(
        envelope,
        now=now,
        expected_key_digest=expected_key_digest,
        expected_protocol=expected_protocol,
    )


def verify_join_request(
    envelope: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Verify a self-presented join key; caller must separately consume its invite."""

    if (
        not isinstance(envelope, Mapping)
        or not isinstance(envelope.get("message"), Mapping)
        or envelope["message"].get("protocol") != JOIN_REQUEST_PROTOCOL
    ):
        raise MembershipContractError("join_request_protocol_required")
    return _verify_envelope(
        envelope,
        now=now,
        expected_key_digest=None,
        expected_protocol=JOIN_REQUEST_PROTOCOL,
    )
