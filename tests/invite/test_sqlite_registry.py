from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading

import pytest

from mycelium_invite import InviteError, SqliteInviteRegistry


def test_sqlite_registry_rejects_replay_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state" / "invites.sqlite3"
    SqliteInviteRegistry(database).consume(
        "nonce-persistent",
        now=1_000.0,
        expires_at=2_000.0,
    )

    with pytest.raises(InviteError) as excinfo:
        SqliteInviteRegistry(database).consume(
            "nonce-persistent",
            now=1_001.0,
            expires_at=2_000.0,
        )

    assert excinfo.value.code == "invite_replayed"
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700


def test_sqlite_registry_allows_only_one_concurrent_consumer(tmp_path: Path) -> None:
    registry = SqliteInviteRegistry(tmp_path / "state" / "invites.sqlite3")
    workers = 8
    barrier = threading.Barrier(workers)

    def consume() -> str:
        barrier.wait(timeout=5)
        try:
            registry.consume("nonce-race", now=1_000.0, expires_at=2_000.0)
        except InviteError as exc:
            return exc.code
        return "accepted"

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _index: consume(), range(workers)))

    assert results.count("accepted") == 1
    assert results.count("invite_replayed") == workers - 1


def test_sqlite_registry_never_stores_raw_invite_token(tmp_path: Path) -> None:
    database = tmp_path / "state" / "invites.sqlite3"
    token_shaped = "header.signature"
    registry = SqliteInviteRegistry(database)
    registry.consume("nonce-only", now=1_000.0, expires_at=2_000.0)

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT nonce, consumed_at, expires_at FROM consumed_invites"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [("nonce-only", 1_000.0, 2_000.0)]
    assert token_shaped.encode() not in database.read_bytes()


def test_sqlite_registry_rejects_expired_nonce_without_consuming_it(tmp_path: Path) -> None:
    registry = SqliteInviteRegistry(tmp_path / "state" / "invites.sqlite3")

    with pytest.raises(InviteError) as excinfo:
        registry.consume("nonce-expired", now=2_001.0, expires_at=2_000.0)
    assert excinfo.value.code == "invite_expired"

    registry.consume("nonce-expired", now=1_999.0, expires_at=2_000.0)


def test_sqlite_registry_prunes_only_expired_rows(tmp_path: Path) -> None:
    registry = SqliteInviteRegistry(tmp_path / "state" / "invites.sqlite3")
    registry.consume("expired", now=1_000.0, expires_at=1_100.0)
    registry.consume("live", now=1_000.0, expires_at=1_300.0)

    assert registry.prune_expired(now=1_200.0) == 1
    registry.consume("expired", now=1_200.0, expires_at=1_400.0)
    with pytest.raises(InviteError, match="invite_replayed"):
        registry.consume("live", now=1_200.0, expires_at=1_300.0)


def test_sqlite_registry_rejects_symlink_database_path(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "external.sqlite3"
    target.write_bytes(b"")
    database = state / "invites.sqlite3"
    try:
        database.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(InviteError) as excinfo:
        SqliteInviteRegistry(database)

    assert excinfo.value.code == "invite_registry_path_invalid"


def test_sqlite_registry_rejects_group_accessible_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)

    with pytest.raises(InviteError) as excinfo:
        SqliteInviteRegistry(state / "invites.sqlite3")

    assert excinfo.value.code == "invite_registry_permissions_invalid"


def test_sqlite_registry_rejects_nonexact_database_mode(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = state / "invites.sqlite3"
    database.write_bytes(b"")
    database.chmod(0o700)

    with pytest.raises(InviteError) as excinfo:
        SqliteInviteRegistry(database)

    assert excinfo.value.code == "invite_registry_permissions_invalid"


def test_sqlite_registry_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(InviteError) as excinfo:
        SqliteInviteRegistry(linked / "state" / "invites.sqlite3")

    assert excinfo.value.code == "invite_registry_path_invalid"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True])
def test_sqlite_registry_rejects_nonfinite_or_boolean_time(tmp_path: Path, now) -> None:
    registry = SqliteInviteRegistry(tmp_path / "state" / "invites.sqlite3")

    with pytest.raises(InviteError) as excinfo:
        registry.consume("nonce-invalid-time", now=now, expires_at=2_000.0)

    assert excinfo.value.code == "invite_time_invalid"
