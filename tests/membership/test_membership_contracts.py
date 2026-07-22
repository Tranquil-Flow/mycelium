from __future__ import annotations

import copy
import json

import pytest

from mycelium_membership import (
    ASSIGNMENT_OFFER_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    DRAIN_ACK_PROTOCOL,
    HEARTBEAT_PROTOCOL,
    JOIN_ACCEPTANCE_PROTOCOL,
    JOIN_REQUEST_PROTOCOL,
    LINK_PROBE_REPORT_PROTOCOL,
    MembershipContractError,
    sign_membership_message,
    validate_membership_message,
    verify_join_request,
    verify_membership_message,
)
from mycelium_qualification.signing import generate_ed25519_signer


def _message(protocol: str, *, endpoint_id: str | None = None) -> dict:
    seed_to_node = protocol in {JOIN_ACCEPTANCE_PROTOCOL, ASSIGNMENT_OFFER_PROTOCOL}
    sender_node_id = "seed-node" if seed_to_node else "node-a"
    sender_endpoint_id = endpoint_id or (
        "endpoint-seed" if seed_to_node else "endpoint-node-a"
    )
    common = {
        "protocol": protocol,
        "message_id": "message-001",
        "swarm_id": "swarm-demo",
        "sender_node_id": sender_node_id,
        "sender_endpoint_id": sender_endpoint_id,
        "recipient_node_id": "node-a" if seed_to_node else "seed-node",
        "incarnation": "incarnation-001",
        "generation": 1,
        "issued_at": 1_000.0,
        "expires_at": 1_300.0,
    }
    specific = {
        JOIN_REQUEST_PROTOCOL: {
            "generation": 0,
            "invite_nonce": "invite-nonce-001",
            "endpoint_addr": {
                "id": sender_endpoint_id,
                "addrs": ["iroh-relay://relay.example/node-a"],
            },
            "software_version": "0.1.0",
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        },
        JOIN_ACCEPTANCE_PROTOCOL: {
            "request_message_id": "join-request-001",
            "accepted_node_id": "node-a",
            "accepted_incarnation": "incarnation-001",
            "membership_generation": 1,
            "lease_expires_at": 4_600.0,
        },
        CAPABILITY_REPORT_PROTOCOL: {
            "platform": "macOS",
            "architecture": "arm64",
            "memory_bytes": 17_179_869_184,
            "available_storage_bytes": 100_000_000_000,
            "backends": ["mlx"],
            "precisions": ["float16"],
        },
        LINK_PROBE_REPORT_PROTOCOL: {
            "target_node_id": "seed-node",
            "target_endpoint_id": "endpoint-seed",
            "reachable": True,
            "rtt_ms": 42.5,
            "goodput_bytes_per_second": 12_000_000.0,
            "probe_sequence": 3,
        },
        HEARTBEAT_PROTOCOL: {
            "heartbeat_sequence": 7,
            "lifecycle_state": "RUNNING",
            "route_ready": False,
            "active_requests": 0,
        },
        ASSIGNMENT_OFFER_PROTOCOL: {
            "deployment_id": "deployment-001",
            "deployment_epoch": 4,
            "assignment_id": "assignment-001",
            "assignment_digest": "sha256:" + "a" * 64,
            "stage_pack_digest": "sha256:" + "b" * 64,
            "graph_digest": "sha256:" + "c" * 64,
            "load_generation": 9,
            "placement_provenance": "offline_capacity_planner",
            "peer_endpoint_records": [
                {
                    "node_id": "node-b",
                    "endpoint_id": "endpoint-node-b",
                    "deployment_epoch": 4,
                    "membership_generation": 2,
                    "valid_from": 1_000.0,
                    "valid_until": 1_300.0,
                }
            ],
        },
        ASSIGNMENT_RESULT_PROTOCOL: {
            "deployment_id": "deployment-001",
            "deployment_epoch": 4,
            "assignment_id": "assignment-001",
            "accepted": True,
            "result_code": "loaded",
            "load_proof_digest": "sha256:" + "d" * 64,
            "runtime_endpoint": "iroh://endpoint-node-a/assignment-001",
        },
        DRAIN_ACK_PROTOCOL: {
            "drain_id": "drain-001",
            "active_requests": 0,
            "last_request_id": None,
            "completed_at": 1_050.0,
        },
    }[protocol]
    return {**common, **specific}


@pytest.mark.parametrize(
    "protocol",
    [
        JOIN_REQUEST_PROTOCOL,
        JOIN_ACCEPTANCE_PROTOCOL,
        CAPABILITY_REPORT_PROTOCOL,
        LINK_PROBE_REPORT_PROTOCOL,
        HEARTBEAT_PROTOCOL,
        ASSIGNMENT_OFFER_PROTOCOL,
        ASSIGNMENT_RESULT_PROTOCOL,
        DRAIN_ACK_PROTOCOL,
    ],
)
def test_all_membership_messages_round_trip_with_pinned_signer(protocol: str) -> None:
    message = _message(protocol)
    signer = generate_ed25519_signer(endpoint_id=message["sender_endpoint_id"])

    envelope = sign_membership_message(signer=signer, message=message)
    verified = verify_membership_message(
        envelope,
        now=1_100.0,
        expected_key_digest=signer.verification_key_digest,
        expected_protocol=protocol,
    )

    assert verified == message
    assert envelope["verification_key"] == signer.public_key_record()
    json.dumps(envelope, sort_keys=True, allow_nan=False)


def test_join_request_has_explicit_untrusted_key_verification_path() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    envelope = sign_membership_message(
        signer=signer,
        message=_message(JOIN_REQUEST_PROTOCOL),
    )

    verified = verify_join_request(envelope, now=1_100.0)

    assert verified["invite_nonce"] == "invite-nonce-001"
    assert envelope["verification_key"]["verification_key_digest"] == signer.verification_key_digest


def test_untrusted_join_verifier_rejects_every_post_join_protocol() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    envelope = sign_membership_message(
        signer=signer,
        message=_message(HEARTBEAT_PROTOCOL),
    )

    with pytest.raises(MembershipContractError) as excinfo:
        verify_join_request(envelope, now=1_100.0)

    assert excinfo.value.code == "join_request_protocol_required"


def test_pinned_verifier_rejects_wrong_key_and_tampering() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    other = generate_ed25519_signer(endpoint_id="endpoint-other")
    envelope = sign_membership_message(
        signer=signer,
        message=_message(CAPABILITY_REPORT_PROTOCOL),
    )

    with pytest.raises(MembershipContractError) as excinfo:
        verify_membership_message(
            envelope,
            now=1_100.0,
            expected_key_digest=other.verification_key_digest,
            expected_protocol=CAPABILITY_REPORT_PROTOCOL,
        )
    assert excinfo.value.code == "membership_key_pin_mismatch"

    tampered = copy.deepcopy(envelope)
    tampered["message"]["memory_bytes"] += 1
    with pytest.raises(MembershipContractError) as excinfo:
        verify_membership_message(
            tampered,
            now=1_100.0,
            expected_key_digest=signer.verification_key_digest,
            expected_protocol=CAPABILITY_REPORT_PROTOCOL,
        )
    assert excinfo.value.code == "membership_signature_invalid"


def test_signer_endpoint_must_match_message_sender_endpoint() -> None:
    signer = generate_ed25519_signer(endpoint_id="different-endpoint")

    with pytest.raises(MembershipContractError) as excinfo:
        sign_membership_message(
            signer=signer,
            message=_message(HEARTBEAT_PROTOCOL),
        )

    assert excinfo.value.code == "membership_signer_endpoint_mismatch"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda message: message.update(extra=True), "membership_fields_invalid"),
        (lambda message: message.update(generation=-1), "membership_generation_invalid"),
        (lambda message: message.update(issued_at=float("nan")), "membership_time_invalid"),
        (lambda message: message.update(expires_at=5_000.0), "membership_ttl_invalid"),
        (lambda message: message.update(route_ready=True), "membership_route_ready_invalid"),
        (lambda message: message.update(active_requests=-1), "membership_integer_invalid"),
    ],
)
def test_heartbeat_schema_fails_closed(mutation, code: str) -> None:
    message = _message(HEARTBEAT_PROTOCOL)
    mutation(message)

    with pytest.raises(MembershipContractError) as excinfo:
        validate_membership_message(message)

    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("now", "code"),
    [
        (1_301.0, "membership_message_expired"),
        (900.0, "membership_message_from_future"),
        (float("nan"), "membership_time_invalid"),
        (True, "membership_time_invalid"),
    ],
)
def test_verifier_rejects_expired_future_or_invalid_clock(now, code: str) -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    envelope = sign_membership_message(
        signer=signer,
        message=_message(HEARTBEAT_PROTOCOL),
    )

    with pytest.raises(MembershipContractError) as excinfo:
        verify_membership_message(
            envelope,
            now=now,
            expected_key_digest=signer.verification_key_digest,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )

    assert excinfo.value.code == code


def test_join_request_generation_and_endpoint_address_are_bound() -> None:
    message = _message(JOIN_REQUEST_PROTOCOL)
    message["generation"] = 1
    with pytest.raises(MembershipContractError, match="membership_join_generation_invalid"):
        validate_membership_message(message)

    message = _message(JOIN_REQUEST_PROTOCOL)
    message["endpoint_addr"]["id"] = "substituted-endpoint"
    with pytest.raises(MembershipContractError, match="membership_endpoint_id_mismatch"):
        validate_membership_message(message)


def test_envelope_is_exact_and_never_contains_private_material() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-seed")
    envelope = sign_membership_message(
        signer=signer,
        message=_message(ASSIGNMENT_OFFER_PROTOCOL),
    )
    envelope["extra"] = True

    with pytest.raises(MembershipContractError) as excinfo:
        verify_membership_message(
            envelope,
            now=1_100.0,
            expected_key_digest=signer.verification_key_digest,
            expected_protocol=ASSIGNMENT_OFFER_PROTOCOL,
        )
    assert excinfo.value.code == "membership_envelope_invalid"

    encoded = json.dumps(sign_membership_message(
        signer=signer,
        message=_message(ASSIGNMENT_OFFER_PROTOCOL),
    ), sort_keys=True).lower()
    assert "private" not in encoded
    assert "secret" not in encoded


@pytest.mark.parametrize(
    ("protocol", "field"),
    [
        (JOIN_ACCEPTANCE_PROTOCOL, "accepted_node_id"),
        (LINK_PROBE_REPORT_PROTOCOL, "target_node_id"),
    ],
)
def test_directed_subject_must_match_recipient(protocol: str, field: str) -> None:
    message = _message(protocol)
    message[field] = "third-party-node"

    with pytest.raises(MembershipContractError) as excinfo:
        validate_membership_message(message)

    assert excinfo.value.code == "membership_recipient_mismatch"


@pytest.mark.parametrize(
    ("protocol", "field", "value"),
    [
        (HEARTBEAT_PROTOCOL, "protocol", []),
        (HEARTBEAT_PROTOCOL, "lifecycle_state", {}),
        (HEARTBEAT_PROTOCOL, "issued_at", 10**10_000),
        (CAPABILITY_REPORT_PROTOCOL, "platform", "\ud800"),
        (JOIN_ACCEPTANCE_PROTOCOL, "membership_generation", True),
        (JOIN_ACCEPTANCE_PROTOCOL, "membership_generation", 1.0),
        (DRAIN_ACK_PROTOCOL, "active_requests", False),
    ],
    ids=[
        "unhashable-protocol",
        "unhashable-lifecycle",
        "huge-time-integer",
        "invalid-unicode",
        "bool-generation",
        "float-generation",
        "bool-active-requests",
    ],
)
def test_hostile_json_values_fail_with_stable_contract_error(
    protocol: str,
    field: str,
    value: object,
) -> None:
    message = _message(protocol)
    message[field] = value

    with pytest.raises(MembershipContractError):
        validate_membership_message(message)


@pytest.mark.parametrize("field", ["peer_class", "runtime_capability"])
def test_join_request_requires_signed_peer_runtime_declaration(field: str) -> None:
    message = _message(JOIN_REQUEST_PROTOCOL)
    message.pop(field)

    with pytest.raises(MembershipContractError) as excinfo:
        validate_membership_message(message)

    assert excinfo.value.code == "membership_fields_invalid"


@pytest.mark.parametrize("field", ["placement_provenance", "peer_endpoint_records"])
def test_assignment_offer_requires_signed_provenance_and_peer_records(field: str) -> None:
    message = _message(ASSIGNMENT_OFFER_PROTOCOL)
    message.pop(field)

    with pytest.raises(MembershipContractError) as excinfo:
        validate_membership_message(message)

    assert excinfo.value.code == "membership_fields_invalid"


def test_assignment_offer_rejects_endpoint_record_outside_signed_validity() -> None:
    message = _message(ASSIGNMENT_OFFER_PROTOCOL)
    message["peer_endpoint_records"][0]["valid_until"] = message["expires_at"] + 1.0

    with pytest.raises(MembershipContractError) as excinfo:
        validate_membership_message(message)

    assert excinfo.value.code == "membership_peer_endpoint_validity_invalid"
