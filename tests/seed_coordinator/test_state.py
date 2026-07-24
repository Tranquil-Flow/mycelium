from __future__ import annotations

from pathlib import Path
import os
import sqlite3

import pytest

from mycelium_seed.state import SeedStateError, SqliteSeedState


def _member(*, generation: int = 1) -> dict[str, object]:
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
        "verification_key_digest": "sha256:" + "a" * 64,
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


def _updated_member() -> dict[str, object]:
    member = _member()
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


def _renewal(message_id: str = "renewal-1") -> dict[str, object]:
    return {
        "message": {
            "protocol": "mycelium.membership.lease_renewal.v1",
            "message_id": message_id,
            "heartbeat_message_id": "heartbeat-1",
            "lease_expires_at": 2_600.0,
        },
        "signature": {"value": "test-signature"},
    }


def _renewal_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(seed_heartbeat_renewals)"
        ).fetchall()
    }


def test_schema_v4_migrates_to_v5_without_losing_a4_member_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-state.sqlite3"
    state = SqliteSeedState(database)
    expected_member = _member()
    state.save_member(expected_member)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE seed_metadata SET value = '4' WHERE key = 'schema_version'"
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
    state.save_member(_member())
    updated = _updated_member()
    renewal = _renewal()

    committed = state.commit_heartbeat_renewal(
        request_envelope_digest="digest-a",
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
        verification_key_digest="sha256:" + "a" * 64,
        incarnation="incarnation-1",
        generation=1,
        heartbeat_message_id="heartbeat-1",
        request_envelope_digest="digest-a",
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
            verification_key_digest="sha256:" + "a" * 64,
            incarnation="incarnation-1",
            generation=1,
            heartbeat_message_id="heartbeat-1",
            request_envelope_digest="digest-b",
        )
    assert mismatch.value.code == "seed_heartbeat_retry_mismatch"

    state.save_member(_member(generation=2))
    assert state.find_heartbeat_renewal(
        node_id="node-a",
        generation=1,
        heartbeat_message_id="heartbeat-1",
    ) is None
    assert state.load_heartbeat_renewal(
        node_id="node-a",
        endpoint_id="endpoint-a",
        verification_key_digest="sha256:" + "a" * 64,
        incarnation="incarnation-1",
        generation=1,
        heartbeat_message_id="heartbeat-1",
        request_envelope_digest="digest-a",
    ) is None


def test_heartbeat_commit_failure_rolls_back_every_effect(tmp_path: Path) -> None:
    database = tmp_path / "seed-state.sqlite3"
    state = SqliteSeedState(database)
    original = _member()
    state.save_member(original)
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
            request_envelope_digest="digest-a",
            heartbeat_message_id="heartbeat-1",
            heartbeat_sequence=1,
            heartbeat_expires_at=2_060.0,
            renewal_message_id="renewal-fail",
            member=_updated_member(),
            renewal=_renewal("renewal-fail"),
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
