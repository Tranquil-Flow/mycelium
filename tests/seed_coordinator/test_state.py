from __future__ import annotations

from pathlib import Path
import hashlib
import os
import sqlite3

import pytest

from mycelium_membership import (
    HEARTBEAT_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    sign_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    generate_ed25519_signer,
)
from mycelium_seed.state import SeedStateError, SqliteSeedState


def _member(
    *,
    generation: int = 1,
    verification_key_digest: str = "sha256:" + "a" * 64,
) -> dict[str, object]:
    return {
        "node_id": "node-a",
        "endpoint_id": "endpoint-a",
        "endpoint_addrs": ["https://node-a.example.test/control"],
        "peer_class": "mac_mlx_iroh",
        "runtime_capability": {
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        "verification_key_digest": verification_key_digest,
        "incarnation": f"incarnation-{generation}",
        "generation": generation,
        "lease_expires_at": 2_300.0 + generation,
        "last_heartbeat_sequence": generation - 1,
        "last_liveness_at": 2_000.0 + generation,
        "next_heartbeat_due_at": 2_030.0 + generation,
        "last_activity_receipt_at": None,
        "active_requests": generation - 1,
        "lifecycle_state": "NEW" if generation == 1 else "STOPPING",
    }


def _updated_member(
    *,
    verification_key_digest: str = "sha256:" + "a" * 64,
) -> dict[str, object]:
    member = _member(verification_key_digest=verification_key_digest)
    member.update(
        {
            "lease_expires_at": 2_600.0,
            "last_heartbeat_sequence": 1,
            "last_liveness_at": 2_004.0,
            "next_heartbeat_due_at": 2_064.0,
            "last_activity_receipt_at": 2_004.0,
            "active_requests": 3,
            "lifecycle_state": "RUNNING",
        }
    )
    return member


def _heartbeat(signer: Ed25519EvidenceSigner) -> dict[str, object]:
    return sign_membership_message(
        signer=signer,
        message={
            "protocol": HEARTBEAT_PROTOCOL,
            "message_id": "heartbeat-1",
            "swarm_id": "swarm-a",
            "sender_node_id": "node-a",
            "sender_endpoint_id": "endpoint-a",
            "recipient_node_id": "seed-node",
            "incarnation": "incarnation-1",
            "generation": 1,
            "issued_at": 2_004.0,
            "expires_at": 2_060.0,
            "heartbeat_sequence": 1,
            "lifecycle_state": "RUNNING",
            "route_ready": False,
            "active_requests": 3,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": None,
            "activity_peer_node_id": None,
        },
    )


def _renewal(
    signer: Ed25519EvidenceSigner,
    message_id: str = "renewal-1",
) -> dict[str, object]:
    return sign_membership_message(
        signer=signer,
        message={
            "protocol": LEASE_RENEWAL_PROTOCOL,
            "message_id": message_id,
            "swarm_id": "swarm-a",
            "sender_node_id": "seed-node",
            "sender_endpoint_id": "seed-endpoint",
            "recipient_node_id": "node-a",
            "incarnation": "seed-incarnation",
            "generation": 1,
            "issued_at": 2_004.0,
            "expires_at": 2_600.0,
            "heartbeat_message_id": "heartbeat-1",
            "member_incarnation": "incarnation-1",
            "membership_generation": 1,
            "lease_expires_at": 2_600.0,
        },
    )


def _renewal_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(seed_heartbeat_renewals)"
        ).fetchall()
    }


def _renewal_table_sql(mutation: str) -> str:
    columns = [
        "node_id TEXT NOT NULL",
        "generation INTEGER NOT NULL CHECK (generation >= 1)",
        "heartbeat_message_id TEXT NOT NULL",
        "request_envelope_digest TEXT NOT NULL",
        "heartbeat_sequence INTEGER NOT NULL CHECK (heartbeat_sequence >= 1)",
        "heartbeat_json TEXT NOT NULL",
        "renewal_message_id TEXT NOT NULL UNIQUE",
        "renewal_json TEXT NOT NULL",
    ]
    primary_key = "PRIMARY KEY (node_id, generation, heartbeat_message_id)"
    node_foreign_key = (
        "FOREIGN KEY (node_id) REFERENCES seed_members(node_id) "
        "ON DELETE CASCADE"
    )
    renewal_foreign_key = (
        "FOREIGN KEY (renewal_message_id) "
        "REFERENCES seed_emitted_messages(message_id)"
    )
    suffix = " WITHOUT ROWID"

    if mutation == "interim_seven_columns":
        columns.remove("heartbeat_json TEXT NOT NULL")
    elif mutation == "column_order":
        columns.remove("heartbeat_json TEXT NOT NULL")
        columns.append("heartbeat_json TEXT NOT NULL")
    elif mutation == "node_affinity":
        columns[0] = "node_id BLOB NOT NULL"
    elif mutation == "node_nullable":
        columns[0] = "node_id TEXT"
    elif mutation == "generation_nullable":
        columns[1] = "generation INTEGER CHECK (generation >= 1)"
    elif mutation == "heartbeat_json_default":
        columns[5] = "heartbeat_json TEXT NOT NULL DEFAULT '{}'"
    elif mutation == "primary_key_changed":
        primary_key = "PRIMARY KEY (generation, node_id, heartbeat_message_id)"
    elif mutation == "primary_key_missing":
        columns[0] = "node_id TEXT PRIMARY KEY NOT NULL"
        primary_key = ""
    elif mutation == "renewal_unique_missing":
        columns[6] = "renewal_message_id TEXT NOT NULL"
    elif mutation == "generation_check_changed":
        columns[1] = "generation INTEGER NOT NULL CHECK (generation > 1)"
    elif mutation == "sequence_check_missing":
        columns[4] = "heartbeat_sequence INTEGER NOT NULL"
    elif mutation == "rowid":
        suffix = ""
    elif mutation == "node_foreign_key_missing":
        node_foreign_key = ""
    elif mutation == "node_foreign_key_action":
        node_foreign_key = (
            "FOREIGN KEY (node_id) REFERENCES seed_members(node_id) "
            "ON DELETE RESTRICT"
        )
    elif mutation == "renewal_foreign_key_missing":
        renewal_foreign_key = ""
    else:
        assert mutation == "renewal_foreign_key_action"
        renewal_foreign_key = (
            "FOREIGN KEY (renewal_message_id) "
            "REFERENCES seed_emitted_messages(message_id) ON DELETE CASCADE"
        )

    definitions = [
        *columns,
        primary_key,
        node_foreign_key,
        renewal_foreign_key,
    ]
    body = ",\n".join(item for item in definitions if item)
    return f"CREATE TABLE seed_heartbeat_renewals ({body}){suffix}"


@pytest.mark.parametrize(
    "mutation",
    (
        "interim_seven_columns",
        "column_order",
        "node_affinity",
        "node_nullable",
        "generation_nullable",
        "heartbeat_json_default",
        "primary_key_changed",
        "primary_key_missing",
        "renewal_unique_missing",
        "generation_check_changed",
        "sequence_check_missing",
        "rowid",
        "node_foreign_key_missing",
        "node_foreign_key_action",
        "renewal_foreign_key_missing",
        "renewal_foreign_key_action",
    ),
)
def test_current_v5_rejects_malformed_renewal_table(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / f"malformed-{mutation}.sqlite3"
    SqliteSeedState(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE seed_heartbeat_renewals")
        connection.execute(_renewal_table_sql(mutation))

    with pytest.raises(SeedStateError) as rejected:
        SqliteSeedState(database)
    assert rejected.value.code == "seed_state_corrupt"


def test_current_v5_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "current-v5.sqlite3"
    SqliteSeedState(database)
    SqliteSeedState(database)


@pytest.mark.parametrize("legacy_version", ("2", "3", "4"))
def test_schema_v2_to_v4_migrates_without_losing_a4_member_state(
    tmp_path: Path,
    legacy_version: str,
) -> None:
    database = tmp_path / f"seed-state-v{legacy_version}.sqlite3"
    state = SqliteSeedState(database)
    expected_member = _member()
    state.save_member(expected_member)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE seed_metadata SET value = ? WHERE key = 'schema_version'",
            (legacy_version,),
        )
        connection.execute("DROP TABLE IF EXISTS seed_heartbeat_renewals")

    migrated = SqliteSeedState(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("5",)
        assert _renewal_columns(connection) == {
            "node_id",
            "generation",
            "heartbeat_message_id",
            "request_envelope_digest",
            "heartbeat_sequence",
            "heartbeat_json",
            "renewal_message_id",
            "renewal_json",
        }
    assert migrated.load_members() == [expected_member]


def test_schema_v1_migrates_directly_to_v5_with_a4_columns_and_renewals(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-state-v1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE seed_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO seed_metadata (key, value)
            VALUES ('schema_version', '1');
            CREATE TABLE seed_members (
                node_id TEXT PRIMARY KEY,
                endpoint_id TEXT NOT NULL,
                endpoint_addrs_json TEXT NOT NULL,
                verification_key_digest TEXT NOT NULL,
                incarnation TEXT NOT NULL,
                generation INTEGER NOT NULL,
                lease_expires_at REAL NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO seed_members (
                node_id, endpoint_id, endpoint_addrs_json,
                verification_key_digest, incarnation, generation,
                lease_expires_at
            ) VALUES (
                'node-old', 'endpoint-old',
                '["https://node-old.example.test/control"]',
                'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'incarnation-old', 7, 2400.0
            );
            """
        )
    os.chmod(database, 0o600)

    migrated = SqliteSeedState(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("5",)
        member_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(seed_members)")
        }
        assert {
            "peer_class",
            "runtime_capability_json",
            "last_liveness_at",
            "next_heartbeat_due_at",
            "last_activity_receipt_at",
            "active_requests",
            "lifecycle_state",
        } <= member_columns
        assert _renewal_columns(connection)
    member = migrated.load_members()[0]
    assert member["node_id"] == "node-old"
    assert member["generation"] == 7
    assert member["lease_expires_at"] == 2_400.0
    assert member["peer_class"] == "linux_tbd"
    assert member["lifecycle_state"] == "NEW"


def test_heartbeat_commit_and_lookup_are_exact_and_current_generation_bound(
    tmp_path: Path,
) -> None:
    state = SqliteSeedState(tmp_path / "seed-state.sqlite3")
    node_signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    seed_signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    state.bind_identity(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_key_digest=seed_signer.verification_key_digest,
    )
    state.save_member(
        _member(verification_key_digest=node_signer.verification_key_digest)
    )
    updated = _updated_member(
        verification_key_digest=node_signer.verification_key_digest
    )
    heartbeat = _heartbeat(node_signer)
    request_digest = hashlib.sha256(
        canonical_json_bytes(heartbeat)
    ).hexdigest()
    renewal = _renewal(seed_signer)

    committed = state.commit_heartbeat_renewal(
        request_envelope_digest=request_digest,
        heartbeat=heartbeat,
        heartbeat_message_id="heartbeat-1",
        heartbeat_sequence=1,
        heartbeat_expires_at=2_060.0,
        renewal_message_id="renewal-1",
        member=updated,
        renewal=renewal,
        now=2_004.0,
        capacity=16,
    )

    assert committed == renewal
    assert state.load_members() == [updated]
    assert state.load_heartbeat_renewal(
        node_id="node-a",
        endpoint_id="endpoint-a",
        verification_key_digest=node_signer.verification_key_digest,
        incarnation="incarnation-1",
        generation=1,
        heartbeat_message_id="heartbeat-1",
        heartbeat_sequence=1,
        request_envelope_digest=request_digest,
    ) == renewal
    assert state.find_heartbeat_renewal(
        node_id="node-a",
        generation=1,
        heartbeat_message_id="heartbeat-1",
    ) == renewal

    with pytest.raises(SeedStateError) as mismatch:
        state.load_heartbeat_renewal(
            node_id="node-a",
            endpoint_id="endpoint-a",
            verification_key_digest=node_signer.verification_key_digest,
            incarnation="incarnation-1",
            generation=1,
            heartbeat_message_id="heartbeat-1",
            heartbeat_sequence=1,
            request_envelope_digest="0" * 64,
        )
    assert mismatch.value.code == "seed_heartbeat_retry_mismatch"

    state.save_member(
        _member(
            generation=2,
            verification_key_digest=node_signer.verification_key_digest,
        )
    )
    assert state.find_heartbeat_renewal(
        node_id="node-a",
        generation=1,
        heartbeat_message_id="heartbeat-1",
    ) is None
    assert state.load_heartbeat_renewal(
        node_id="node-a",
        endpoint_id="endpoint-a",
        verification_key_digest=node_signer.verification_key_digest,
        incarnation="incarnation-1",
        generation=1,
        heartbeat_message_id="heartbeat-1",
        heartbeat_sequence=1,
        request_envelope_digest=request_digest,
    ) is None


def test_heartbeat_commit_failure_rolls_back_every_effect(tmp_path: Path) -> None:
    database = tmp_path / "seed-state.sqlite3"
    state = SqliteSeedState(database)
    node_signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    seed_signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    state.bind_identity(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_key_digest=seed_signer.verification_key_digest,
    )
    original = _member(
        verification_key_digest=node_signer.verification_key_digest
    )
    state.save_member(original)
    heartbeat = _heartbeat(node_signer)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_heartbeat_renewal
            BEFORE INSERT ON seed_heartbeat_renewals
            BEGIN
                SELECT RAISE(ABORT, 'injected heartbeat failure');
            END;
            """
        )

    with pytest.raises(SeedStateError) as failed:
        state.commit_heartbeat_renewal(
            request_envelope_digest=hashlib.sha256(
                canonical_json_bytes(heartbeat)
            ).hexdigest(),
            heartbeat=heartbeat,
            heartbeat_message_id="heartbeat-1",
            heartbeat_sequence=1,
            heartbeat_expires_at=2_060.0,
            renewal_message_id="renewal-fail",
            member=_updated_member(
                verification_key_digest=node_signer.verification_key_digest
            ),
            renewal=_renewal(seed_signer, "renewal-fail"),
            now=2_004.0,
            capacity=16,
        )
    assert failed.value.code == "seed_state_unavailable"

    assert state.load_members() == [original]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_replay WHERE message_id = 'heartbeat-1'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_emitted_messages "
            "WHERE message_id = 'renewal-fail'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM seed_heartbeat_renewals"
        ).fetchone() == (0,)
