from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import threading

import pytest

from mycelium_seed.state import SeedStateError, SqliteSeedState


def _member(*, generation: int) -> dict[str, object]:
    return {
        "node_id": "node-a",
        "endpoint_id": "endpoint-a",
        "endpoint_addrs": ["https://node-a.example.test/control"],
        "peer_class": "browser_http",
        "runtime_capability": {
            "runtime_backend": "browser",
            "transport": "http",
            "activation_protocol": None,
        },
        "verification_key_digest": "sha256:" + "a" * 64,
        "incarnation": "incarnation-a",
        "generation": generation,
        "lease_expires_at": 2_300.0,
        "last_heartbeat_sequence": 0,
        "last_liveness_at": 2_000.0,
        "next_heartbeat_due_at": 2_030.0,
        "last_activity_receipt_at": None,
        "active_requests": 0,
        "lifecycle_state": "NEW" if generation == 1 else "STOPPING",
    }


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


def test_member_authority_guard_holds_cross_instance_write_reservation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-state.sqlite3"
    guarded_state = SqliteSeedState(database)
    contender_state = SqliteSeedState(database)
    guarded_state.save_member(_member(generation=1))
    save_entered = threading.Event()
    save_finished = threading.Event()
    errors: list[BaseException] = []
    original_save = contender_state.save_member

    def observed_save(member: dict[str, object]) -> None:
        save_entered.set()
        original_save(member)

    contender_state.save_member = observed_save  # type: ignore[method-assign]

    def contend() -> None:
        try:
            contender_state.save_member(_member(generation=2))
        except BaseException as exc:
            errors.append(exc)
        finally:
            save_finished.set()

    thread = threading.Thread(target=contend, daemon=True)
    with guarded_state.member_authority_guard(node_id="node-a") as member:
        assert member["generation"] == 1
        thread.start()
        assert save_entered.wait(timeout=1)
        assert not save_finished.wait(timeout=0.05)
        assert guarded_state.load_members()[0]["generation"] == 1

    thread.join(timeout=1)
    assert not thread.is_alive()
    assert save_finished.is_set()
    assert errors == []
    assert guarded_state.load_members()[0]["generation"] == 2


def test_member_authority_guard_rejects_corrupt_persisted_member(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seed-state.sqlite3"
    state = SqliteSeedState(database)
    state.save_member(_member(generation=1))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE seed_members SET endpoint_addrs_json = '[]' "
            "WHERE node_id = 'node-a'"
        )

    with pytest.raises(SeedStateError) as excinfo:
        with state.member_authority_guard(node_id="node-a"):
            pass
    assert excinfo.value.code == "seed_state_corrupt"


def test_member_authority_guard_maps_busy_database_to_stable_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "seed-state.sqlite3"
    state = SqliteSeedState(database)
    state.save_member(_member(generation=1))
    original_connect = state._connect  # noqa: SLF001 - bounded busy oracle

    def short_timeout_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.execute("PRAGMA busy_timeout = 10")
        return connection

    monkeypatch.setattr(state, "_connect", short_timeout_connect)
    locker = sqlite3.connect(database, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(SeedStateError) as excinfo:
            with state.member_authority_guard(node_id="node-a"):
                pass
        assert excinfo.value.code == "seed_state_unavailable"
    finally:
        locker.rollback()
        locker.close()
