from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from itertools import count
from pathlib import Path
import hashlib
import json
import math
import sqlite3
import threading

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import (
    HEARTBEAT_PROTOCOL,
    MAX_MESSAGE_TTL_SECONDS,
    sign_membership_message,
)
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import (
    SeedCoordinator,
    SeedCoordinatorError,
    SeedStateError,
    SqliteSeedState,
)


NOW = 2_000.0
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _coordinator(
    tmp_path: Path,
    *,
    signer=None,
    id_prefix: str = "seed-message",
    clock=lambda: NOW,
    lease_seconds: float = 300.0,
    message_ttl_seconds: float = 60.0,
) -> SeedCoordinator:
    database = tmp_path / "seed-state" / "state.sqlite3"
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=(
            generate_ed25519_signer(endpoint_id="seed-endpoint")
            if signer is None
            else signer
        ),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=clock,
        id_source=_ids(id_prefix),
        lease_seconds=lease_seconds,
        message_ttl_seconds=message_ttl_seconds,
    )


def _node(
    tmp_path: Path,
    *,
    incarnation: str = "incarnation-a",
    clock=lambda: NOW,
) -> NodeMembershipSession:
    return NodeMembershipSession(
        node_id="node-a",
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "nodes" / "node-a.key"),
        incarnation=incarnation,
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=clock,
        id_source=_ids(f"node-{incarnation}"),
    )


def _join(
    coordinator: SeedCoordinator,
    node: NodeMembershipSession,
    *,
    nonce: str,
    now: float,
) -> None:
    bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=now)
    request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://node-a.example.test/control"],
    )
    acceptance = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=request,
    )
    node.accept_join(
        acceptance,
        seed_key_digest=verified["seed_key_digest"],
    )


def _activation_heartbeat(node: NodeMembershipSession) -> dict:
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=3)
    assert heartbeat is not None
    message = deepcopy(heartbeat["message"])
    message.update(
        {
            "liveness_source": "activation_receipt",
            "activity_receipt_digest": "sha256:" + "b" * 64,
            "activity_peer_node_id": "node-b",
        }
    )
    return sign_membership_message(signer=node.signer, message=message)


def test_exact_heartbeat_retry_survives_expiry_restart_and_preserves_a4_state(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-durable",
        clock=lambda: clock[0],
        lease_seconds=5.0,
    )
    node = _node(tmp_path, clock=lambda: clock[0])
    _join(coordinator, node, nonce="invite-durable", now=clock[0])

    clock[0] = NOW + 4.0
    heartbeat = _activation_heartbeat(node)
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    first_renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    )
    member = coordinator.member("node-a")
    assert member["peer_class"] == "mac_mlx_iroh"
    assert member["runtime_capability"] == MAC_RUNTIME_CAPABILITY
    assert member["last_heartbeat_sequence"] == 1
    assert member["lease_expires_at"] == NOW + 9.0
    assert member["last_liveness_at"] == NOW + 4.0
    assert member["next_heartbeat_due_at"] == NOW + 64.0
    assert member["last_activity_receipt_at"] == NOW + 4.0
    assert member["active_requests"] == 3
    assert member["lifecycle_state"] == "RUNNING"
    assert first_renewal["message"]["expires_at"] == first_renewal["message"][
        "lease_expires_at"
    ]

    before_expiry = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert before_expiry == accepted
    assert coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    ) == first_renewal

    clock[0] = NOW + 6.0
    after_old_lease_and_request_expiry = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert after_old_lease_and_request_expiry == accepted
    assert canonical_json_bytes(
        coordinator.lease_renewal(
            node_id="node-a",
            heartbeat_message_id=accepted["message_id"],
        )
    ) == canonical_json_bytes(first_renewal)

    restored = _coordinator(
        tmp_path,
        signer=signer,
        id_prefix="seed-restored",
        clock=lambda: clock[0],
        lease_seconds=5.0,
    )
    assert restored.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    ) == first_renewal
    assert restored.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    ) == accepted
    restored_member = restored.member("node-a")
    for field in (
        "peer_class",
        "runtime_capability",
        "last_heartbeat_sequence",
        "lease_expires_at",
        "last_liveness_at",
        "next_heartbeat_due_at",
        "last_activity_receipt_at",
        "active_requests",
        "lifecycle_state",
    ):
        assert restored_member[field] == member[field]


def test_same_heartbeat_id_with_changed_signed_payload_fails_closed(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-changed", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )

    changed_message = deepcopy(heartbeat["message"])
    changed_message["active_requests"] = 1
    changed = sign_membership_message(signer=node.signer, message=changed_message)
    with pytest.raises(SeedCoordinatorError) as mismatch:
        coordinator.receive_member_message(
            changed,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert mismatch.value.code == "seed_heartbeat_retry_mismatch"


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("recipient_node_id", "other-seed"),
        ("sender_endpoint_id", "other-endpoint"),
        ("incarnation", "other-incarnation"),
        ("generation", 2),
    ],
)
def test_same_heartbeat_id_changed_signed_relational_binding_is_retry_mismatch(
    tmp_path: Path,
    field: str,
    changed_value: object,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-relational-retry", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )

    changed_message = deepcopy(heartbeat["message"])
    changed_message[field] = changed_value
    changed_signer = (
        replace(node.signer, endpoint_id=str(changed_value))
        if field == "sender_endpoint_id"
        else node.signer
    )
    changed = sign_membership_message(
        signer=changed_signer,
        message=changed_message,
    )
    with pytest.raises(SeedCoordinatorError) as mismatch:
        coordinator.receive_member_message(
            changed,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert mismatch.value.code == "seed_heartbeat_retry_mismatch"


def test_uncommitted_signed_relational_mismatch_keeps_generic_diagnostic(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-uncommitted-mismatch", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    changed_message = deepcopy(heartbeat["message"])
    changed_message["recipient_node_id"] = "other-seed"
    changed = sign_membership_message(signer=node.signer, message=changed_message)

    with pytest.raises(SeedCoordinatorError) as mismatch:
        coordinator.receive_member_message(
            changed,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert mismatch.value.code == "seed_message_mismatch"


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    [
        ("heartbeat_sequence", 2),
        ("renewal_message_id", "tampered-renewal-id"),
    ],
)
@pytest.mark.parametrize("recovery_path", ["load", "find", "concurrent", "restart"])
def test_tampered_heartbeat_binding_fails_closed_on_every_recovery_path(
    tmp_path: Path,
    column: str,
    tampered_value: object,
    recovery_path: str,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(tmp_path, signer=signer)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-binding-corruption", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    heartbeat_id = accepted["message_id"]
    request_digest = hashlib.sha256(
        canonical_json_bytes(heartbeat)
    ).hexdigest()
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=heartbeat_id,
    )
    database = tmp_path / "seed-state" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        if column == "heartbeat_sequence":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_sequence = ? "
                "WHERE heartbeat_message_id = ?",
                (tampered_value, heartbeat_id),
            )
        else:
            assert column == "renewal_message_id"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_message_id = ? "
                "WHERE heartbeat_message_id = ?",
                (tampered_value, heartbeat_id),
            )

    state = SqliteSeedState(database)
    with pytest.raises(
        (SeedCoordinatorError, SeedStateError),
    ) as unavailable:
        if recovery_path == "load":
            state.load_heartbeat_renewal(
                node_id="node-a",
                endpoint_id=node.signer.endpoint_id,
                verification_key_digest=node.signer.verification_key_digest,
                incarnation="incarnation-a",
                generation=1,
                heartbeat_message_id=heartbeat_id,
                heartbeat_sequence=1,
                request_envelope_digest=request_digest,
            )
        elif recovery_path == "find":
            state.find_heartbeat_renewal(
                node_id="node-a",
                generation=1,
                heartbeat_message_id=heartbeat_id,
            )
        elif recovery_path == "concurrent":
            state.commit_heartbeat_renewal(
                request_envelope_digest=request_digest,
                heartbeat=heartbeat,
                heartbeat_message_id=heartbeat_id,
                heartbeat_sequence=1,
                heartbeat_expires_at=float(accepted["expires_at"]),
                renewal_message_id="unused-concurrent-renewal",
                member=state.load_members()[0],
                renewal=renewal,
                now=NOW,
                capacity=16,
            )
        else:
            restarted = _coordinator(
                tmp_path,
                signer=signer,
                id_prefix="seed-restarted-corrupt",
            )
            restarted.lease_renewal(
                node_id="node-a",
                heartbeat_message_id=heartbeat_id,
            )
    assert unavailable.value.code == "seed_state_corrupt"


_DURABLE_CORRUPTIONS = (
    "digest_short",
    "digest_nonhex",
    "digest_different",
    "row_sequence_bool_text",
    "row_sequence_zero",
    "row_sequence_later",
    "heartbeat_malformed_json",
    "heartbeat_noncanonical_json",
    "heartbeat_nonfinite_json",
    "heartbeat_bad_signature",
    "heartbeat_changed_signed_field",
    "heartbeat_bool_generation",
    "heartbeat_bool_sequence",
    "heartbeat_wrong_message_id",
    "heartbeat_wrong_node",
    "heartbeat_wrong_endpoint",
    "heartbeat_wrong_incarnation",
    "heartbeat_wrong_generation",
    "heartbeat_wrong_recipient",
    "renewal_row_message_id",
    "renewal_wrong_message_id",
    "renewal_wrong_heartbeat_id",
    "renewal_wrong_generation",
    "renewal_wrong_membership_generation",
    "renewal_bool_generation",
    "renewal_bool_membership_generation",
    "renewal_wrong_member_incarnation",
    "renewal_wrong_recipient",
    "renewal_malformed_json",
)


def _canonical_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _mutate_durable_renewal(
    database: Path,
    *,
    heartbeat_message_id: str,
    corruption: str,
) -> None:
    with sqlite3.connect(database) as connection:
        if corruption == "digest_short":
            connection.execute(
                "UPDATE seed_heartbeat_renewals "
                "SET request_envelope_digest = 'abc' "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        elif corruption == "digest_nonhex":
            connection.execute(
                "UPDATE seed_heartbeat_renewals "
                "SET request_envelope_digest = ? "
                "WHERE heartbeat_message_id = ?",
                ("g" * 64, heartbeat_message_id),
            )
        elif corruption == "digest_different":
            connection.execute(
                "UPDATE seed_heartbeat_renewals "
                "SET request_envelope_digest = ? "
                "WHERE heartbeat_message_id = ?",
                ("0" * 64, heartbeat_message_id),
            )
        elif corruption == "row_sequence_bool_text":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_sequence = 'true' "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        elif corruption == "row_sequence_zero":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_sequence = 0 "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        elif corruption == "row_sequence_later":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_sequence = 2 "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        elif corruption == "renewal_row_message_id":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE seed_heartbeat_renewals "
                "SET renewal_message_id = 'other-renewal' "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        else:
            row = connection.execute(
                """
                SELECT heartbeat_json, renewal_json
                FROM seed_heartbeat_renewals
                WHERE heartbeat_message_id = ?
                """,
                (heartbeat_message_id,),
            ).fetchone()
            assert row is not None
            heartbeat = json.loads(row[0])
            renewal = json.loads(row[1])

        if corruption == "heartbeat_malformed_json":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = '{' "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )
        elif corruption == "heartbeat_noncanonical_json":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (json.dumps(heartbeat, indent=2), heartbeat_message_id),
            )
        elif corruption == "heartbeat_nonfinite_json":
            raw = _canonical_text(heartbeat).replace(
                f'"issued_at":{heartbeat["message"]["issued_at"]}',
                '"issued_at":NaN',
            )
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (raw, heartbeat_message_id),
            )
        elif corruption == "heartbeat_bad_signature":
            heartbeat["signature"]["signature"] = "AAAA"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_changed_signed_field":
            heartbeat["message"]["active_requests"] += 1
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_bool_generation":
            heartbeat["message"]["generation"] = True
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_bool_sequence":
            heartbeat["message"]["heartbeat_sequence"] = True
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_message_id":
            heartbeat["message"]["message_id"] = "other-heartbeat"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_node":
            heartbeat["message"]["sender_node_id"] = "other-node"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_endpoint":
            heartbeat["message"]["sender_endpoint_id"] = "other-endpoint"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_incarnation":
            heartbeat["message"]["incarnation"] = "other-incarnation"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_generation":
            heartbeat["message"]["generation"] = 2
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "heartbeat_wrong_recipient":
            heartbeat["message"]["recipient_node_id"] = "other-seed"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET heartbeat_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(heartbeat), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_message_id":
            renewal["message"]["message_id"] = "other-renewal"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_heartbeat_id":
            renewal["message"]["heartbeat_message_id"] = "other-heartbeat"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_generation":
            renewal["message"]["generation"] = 2
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_membership_generation":
            renewal["message"]["membership_generation"] = 2
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_bool_generation":
            renewal["message"]["generation"] = True
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_bool_membership_generation":
            renewal["message"]["membership_generation"] = True
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_member_incarnation":
            renewal["message"]["member_incarnation"] = "other-incarnation"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_wrong_recipient":
            renewal["message"]["recipient_node_id"] = "other-node"
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = ? "
                "WHERE heartbeat_message_id = ?",
                (_canonical_text(renewal), heartbeat_message_id),
            )
        elif corruption == "renewal_malformed_json":
            connection.execute(
                "UPDATE seed_heartbeat_renewals SET renewal_json = '[' "
                "WHERE heartbeat_message_id = ?",
                (heartbeat_message_id,),
            )


@pytest.mark.parametrize("corruption", _DURABLE_CORRUPTIONS)
@pytest.mark.parametrize(
    "recovery_path",
    ("cache", "load", "find", "restart", "existing"),
)
def test_durable_heartbeat_corruption_matrix_fails_closed(
    tmp_path: Path,
    corruption: str,
    recovery_path: str,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(tmp_path, signer=signer)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-corruption-matrix", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    heartbeat_id = accepted["message_id"]
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=heartbeat_id,
    )
    if corruption == "row_sequence_later":
        heartbeat_two = node.heartbeat(
            lifecycle_state="RUNNING",
            active_requests=1,
        )
        assert heartbeat_two is not None
        coordinator.receive_member_message(
            heartbeat_two,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
        coordinator.lease_renewal(
            node_id="node-a",
            heartbeat_message_id=heartbeat_id,
        )
    request_digest = hashlib.sha256(canonical_json_bytes(heartbeat)).hexdigest()
    database = tmp_path / "seed-state" / "state.sqlite3"
    _mutate_durable_renewal(
        database,
        heartbeat_message_id=heartbeat_id,
        corruption=corruption,
    )
    state = SqliteSeedState(database)

    with pytest.raises((SeedCoordinatorError, SeedStateError)) as failed:
        if recovery_path == "cache":
            coordinator.lease_renewal(
                node_id="node-a",
                heartbeat_message_id=heartbeat_id,
            )
        elif recovery_path == "load":
            state.load_heartbeat_renewal(
                node_id="node-a",
                endpoint_id=node.signer.endpoint_id,
                verification_key_digest=node.signer.verification_key_digest,
                incarnation="incarnation-a",
                generation=1,
                heartbeat_message_id=heartbeat_id,
                heartbeat_sequence=1,
                request_envelope_digest=request_digest,
            )
        elif recovery_path == "find":
            state.find_heartbeat_renewal(
                node_id="node-a",
                generation=1,
                heartbeat_message_id=heartbeat_id,
            )
        elif recovery_path == "restart":
            restarted = _coordinator(
                tmp_path,
                signer=signer,
                id_prefix="seed-corruption-restart",
            )
            restarted.lease_renewal(
                node_id="node-a",
                heartbeat_message_id=heartbeat_id,
            )
        else:
            assert recovery_path == "existing"
            state.commit_heartbeat_renewal(
                request_envelope_digest=request_digest,
                heartbeat=heartbeat,
                heartbeat_message_id=heartbeat_id,
                heartbeat_sequence=1,
                heartbeat_expires_at=float(accepted["expires_at"]),
                renewal_message_id=renewal["message"]["message_id"],
                member=state.load_members()[0],
                renewal=renewal,
                now=NOW,
                capacity=16,
            )
    assert failed.value.code == "seed_state_corrupt"


def test_valid_prior_heartbeat_remains_recoverable_after_later_sequence(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-prior-sequence", now=NOW)
    heartbeat_one = node.heartbeat(lifecycle_state="CONFIGURED", active_requests=0)
    heartbeat_two = node.heartbeat(lifecycle_state="RUNNING", active_requests=1)
    assert heartbeat_one is not None
    assert heartbeat_two is not None
    coordinator.receive_member_message(
        heartbeat_one,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    renewal_one = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=heartbeat_one["message"]["message_id"],
    )
    coordinator.receive_member_message(
        heartbeat_two,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )

    assert coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=heartbeat_one["message"]["message_id"],
    ) == renewal_one


def test_immediate_heartbeat_still_emits_a_strictly_advancing_renewal(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-immediate", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    )

    assert renewal["message"]["lease_expires_at"] > NOW + 300.0
    assert node.accept_lease_renewal(
        renewal,
        heartbeat_message_id=accepted["message_id"],
    )["lease_expires_at"] == renewal["message"]["lease_expires_at"]


def test_exact_maximum_lease_is_accepted(
    tmp_path: Path,
) -> None:
    """Exact MAX is valid per the membership contract (<= MAX)."""
    _coordinator(
        tmp_path,
        lease_seconds=MAX_MESSAGE_TTL_SECONDS,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_seconds", True),
        ("lease_seconds", math.inf),
        ("lease_seconds", math.nan),
        ("message_ttl_seconds", True),
        ("message_ttl_seconds", math.inf),
        ("message_ttl_seconds", math.nan),
        (
            "message_ttl_seconds",
            math.nextafter(MAX_MESSAGE_TTL_SECONDS, math.inf),
        ),
    ],
)
def test_coordinator_ttl_boundaries_reject_nonfinite_bool_and_over_max(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match=field + " is invalid"):
        _coordinator(tmp_path, **kwargs)


def test_message_ttl_exact_maximum_is_allowed(tmp_path: Path) -> None:
    _coordinator(
        tmp_path,
        message_ttl_seconds=MAX_MESSAGE_TTL_SECONDS,
    )


def test_nextafter_max_lease_has_headroom_and_bounded_renewals(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    lease_seconds = math.nextafter(MAX_MESSAGE_TTL_SECONDS, -math.inf)
    coordinator = _coordinator(
        tmp_path,
        clock=lambda: clock[0],
        lease_seconds=lease_seconds,
    )
    node = _node(tmp_path, clock=lambda: clock[0])
    _join(coordinator, node, nonce="invite-near-maximum", now=clock[0])
    initial_lease = coordinator.member("node-a")["lease_expires_at"]
    initial_ceiling = clock[0] + MAX_MESSAGE_TTL_SECONDS
    assert initial_lease < initial_ceiling

    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    )
    assert initial_lease < renewal["message"]["lease_expires_at"] <= initial_ceiling

    node.accept_lease_renewal(
        renewal,
        heartbeat_message_id=accepted["message_id"],
    )
    clock[0] = math.nextafter(NOW + 1e-9, math.inf)
    later_heartbeat = node.heartbeat(
        lifecycle_state="RUNNING",
        active_requests=0,
    )
    assert later_heartbeat is not None
    later_accepted = coordinator.receive_member_message(
        later_heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    later_renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=later_accepted["message_id"],
    )
    later_deadline = later_renewal["message"]["lease_expires_at"]
    assert later_deadline > renewal["message"]["lease_expires_at"]
    assert later_deadline <= clock[0] + MAX_MESSAGE_TTL_SECONDS


def test_just_below_maximum_lease_still_strictly_advances(
    tmp_path: Path,
) -> None:
    just_below = MAX_MESSAGE_TTL_SECONDS - 1.0
    coordinator = _coordinator(
        tmp_path / "below",
        lease_seconds=just_below,
    )
    node = _node(tmp_path / "below")
    _join(coordinator, node, nonce="invite-boundary", now=NOW)
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    accepted = coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    renewal = coordinator.lease_renewal(
        node_id="node-a",
        heartbeat_message_id=accepted["message_id"],
    )
    assert renewal["message"]["lease_expires_at"] > NOW + just_below


def test_injected_heartbeat_transaction_failure_leaves_no_partial_effects(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path, id_prefix="seed-rollback")
    node = _node(tmp_path)
    _join(coordinator, node, nonce="invite-rollback", now=NOW)
    database = tmp_path / "seed-state" / "state.sqlite3"
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=2)
    assert heartbeat is not None
    heartbeat_id = heartbeat["message"]["message_id"]

    state = SqliteSeedState(database)
    before_member = state.load_members()
    with sqlite3.connect(database) as connection:
        before_emitted = connection.execute(
            "SELECT COUNT(*) FROM seed_emitted_messages"
        ).fetchone()[0]
        connection.executescript(
            """
            CREATE TRIGGER fail_heartbeat_renewal
            BEFORE INSERT ON seed_heartbeat_renewals
            BEGIN
                SELECT RAISE(ABORT, 'injected heartbeat failure');
            END;
            """
        )

    with pytest.raises(SeedCoordinatorError) as failed:
        coordinator.receive_member_message(
            heartbeat,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert failed.value.code == "seed_state_unavailable"

    assert state.load_members() == before_member
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_replay WHERE message_id = ?",
            (heartbeat_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_emitted_messages"
        ).fetchone() == (before_emitted,)
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_heartbeat_renewals"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER fail_heartbeat_renewal")

    coordinator.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    assert coordinator.member("node-a")["last_heartbeat_sequence"] == 1


def test_two_independent_coordinators_concurrently_return_one_exact_renewal(
    tmp_path: Path,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    first = _coordinator(tmp_path, signer=signer, id_prefix="seed-race-a")
    node = _node(tmp_path)
    _join(first, node, nonce="invite-race", now=NOW)
    second = _coordinator(tmp_path, signer=signer, id_prefix="seed-race-b")
    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    coordinators = (first, second)
    barrier = threading.Barrier(len(coordinators))

    def attempt(coordinator: SeedCoordinator) -> dict:
        barrier.wait(timeout=5)
        message = coordinator.receive_member_message(
            heartbeat,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
        return coordinator.lease_renewal(
            node_id="node-a",
            heartbeat_message_id=message["message_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        renewals = list(executor.map(attempt, coordinators))

    assert renewals[0] == renewals[1]
    with sqlite3.connect(
        tmp_path / "seed-state" / "state.sqlite3"
    ) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_heartbeat_renewals"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_emitted_messages"
        ).fetchone() == (2,)


def test_stale_generation_cannot_recover_prior_renewal(tmp_path: Path) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    stale = _coordinator(tmp_path, signer=signer, id_prefix="seed-stale")
    old_node = _node(tmp_path, incarnation="incarnation-old")
    _join(stale, old_node, nonce="invite-old", now=NOW)
    heartbeat = old_node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    stale.receive_member_message(
        heartbeat,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    heartbeat_id = heartbeat["message"]["message_id"]

    current = _coordinator(tmp_path, signer=signer, id_prefix="seed-current")
    new_node = _node(tmp_path, incarnation="incarnation-new")
    _join(current, new_node, nonce="invite-new", now=NOW)
    assert current.member("node-a")["generation"] == 2

    with pytest.raises(SeedCoordinatorError) as stale_lookup:
        stale.lease_renewal(
            node_id="node-a",
            heartbeat_message_id=heartbeat_id,
        )
    assert stale_lookup.value.code == "seed_state_member_stale"
    with pytest.raises(SeedCoordinatorError) as stale_retry:
        stale.receive_member_message(
            heartbeat,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert stale_retry.value.code == "seed_state_member_stale"


def test_stale_seed_cannot_regress_newer_heartbeat_state(tmp_path: Path) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    stale = _coordinator(tmp_path, signer=signer, id_prefix="seed-stale")
    node = _node(tmp_path)
    _join(stale, node, nonce="invite-stale", now=NOW)
    current = _coordinator(tmp_path, signer=signer, id_prefix="seed-current")
    heartbeat_one = node.heartbeat(lifecycle_state="CONFIGURED", active_requests=0)
    heartbeat_two = node.heartbeat(lifecycle_state="RUNNING", active_requests=1)
    assert heartbeat_one is not None
    assert heartbeat_two is not None

    current.receive_member_message(
        heartbeat_two,
        expected_protocol=HEARTBEAT_PROTOCOL,
    )
    with pytest.raises(SeedCoordinatorError) as conflict:
        stale.receive_member_message(
            heartbeat_one,
            expected_protocol=HEARTBEAT_PROTOCOL,
        )
    assert conflict.value.code == "seed_state_member_conflict"
    restored = _coordinator(tmp_path, signer=signer, id_prefix="seed-restored")
    member = restored.member("node-a")
    assert member["last_heartbeat_sequence"] == 2
    assert member["active_requests"] == 1
    assert member["lifecycle_state"] == "RUNNING"
