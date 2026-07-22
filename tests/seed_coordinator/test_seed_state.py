from __future__ import annotations

from pathlib import Path
import os
import sqlite3

from mycelium_seed.state import SqliteSeedState


def test_schema_v1_migrates_directly_to_current_member_contract(tmp_path: Path) -> None:
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
            """
        )
    os.chmod(database, 0o600)

    SqliteSeedState(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'"
        ).fetchone()
        columns = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(seed_members)")
        }

    assert version == ("4",)
    assert columns["last_heartbeat_sequence"] == ("INTEGER", 1, "0")
    assert columns["peer_class"] == ("TEXT", 1, "'linux_tbd'")
    assert columns["runtime_capability_json"][0:2] == ("TEXT", 1)
    assert "runtime_backend" in columns["runtime_capability_json"][2]
    assert columns["last_liveness_at"] == ("REAL", 1, "0")
    assert columns["next_heartbeat_due_at"] == ("REAL", 1, "0")
    assert columns["last_activity_receipt_at"] == ("REAL", 0, None)
    assert columns["active_requests"] == ("INTEGER", 1, "0")
    assert columns["lifecycle_state"] == ("TEXT", 1, "'NEW'")
