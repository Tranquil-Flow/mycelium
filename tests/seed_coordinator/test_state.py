from __future__ import annotations

from pathlib import Path
import sqlite3

from mycelium_seed import SqliteSeedState


def test_schema_v2_migrates_to_v3_with_durable_heartbeat_responses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-state.sqlite3"
    SqliteSeedState(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE IF EXISTS seed_heartbeat_renewals")
        connection.execute(
            "UPDATE seed_metadata SET value = '2' WHERE key = 'schema_version'"
        )

    SqliteSeedState(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(seed_heartbeat_renewals)"
            ).fetchall()
        }
    assert version == "3"
    assert columns == {
        "node_id",
        "generation",
        "heartbeat_message_id",
        "request_envelope_digest",
        "heartbeat_sequence",
        "renewal_message_id",
        "renewal_json",
    }
