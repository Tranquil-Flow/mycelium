# SPDX-License-Identifier: AGPL-3.0-only
"""The invite registry keeps its owner-only contract on Windows.

POSIX checks mode 0700/0600 and the uid; Windows has neither, so the registry
goes through the host-platform seam there: an owner-only private directory and
an exclusively created owner-only database file.  The fake Win32 API runs these
cases on POSIX hosts; the last cases run against the real API on Windows.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from mycelium_invite import InviteError, SqliteInviteRegistry
from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32


def _code(call) -> str:
    with pytest.raises(InviteError) as caught:
        call()
    return caught.value.code


def test_win32_registry_is_owner_only_and_rejects_replay(tmp_path: Path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    database = tmp_path / "seed" / "state.sqlite3"
    SqliteInviteRegistry(database).consume("nonce-a", now=1_000.0, expires_at=2_000.0)
    assert fake.security_of(database.parent).dacl_protected
    assert fake.security_of(database.parent) == owner_only()
    assert fake.security_of(database).owner_sid == fake.user_sid
    assert any(call[0] == "create_directory" for call in fake.calls)
    # Reopening verifies the existing file instead of recreating it.
    assert _code(
        lambda: SqliteInviteRegistry(database).consume("nonce-a", now=1_001.0, expires_at=2_000.0)
    ) == "invite_replayed"


def test_win32_registry_refuses_a_foreign_owned_directory(tmp_path: Path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    directory = tmp_path / "seed"
    directory.mkdir()
    fake.set_security(directory, owner_only(OTHER_USER_SID))
    assert _code(lambda: SqliteInviteRegistry(directory / "state.sqlite3")) == (
        "invite_registry_permissions_invalid"
    )
    assert not (directory / "state.sqlite3").exists()


def test_win32_registry_refuses_a_directory_that_is_not_owner_only(tmp_path: Path, monkeypatch) -> None:
    simulate_win32(monkeypatch)
    directory = tmp_path / "seed"
    directory.mkdir()  # inherited profile ACL: not protected
    assert _code(lambda: SqliteInviteRegistry(directory / "state.sqlite3")) == (
        "invite_registry_permissions_invalid"
    )


def test_win32_registry_refuses_a_foreign_owned_database(tmp_path: Path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    database = tmp_path / "seed" / "state.sqlite3"
    SqliteInviteRegistry(database)
    fake.set_security(database, owner_only(OTHER_USER_SID))
    assert _code(lambda: SqliteInviteRegistry(database)) == "invite_registry_permissions_invalid"


def test_win32_registry_refuses_a_hard_linked_database(tmp_path: Path, monkeypatch) -> None:
    simulate_win32(monkeypatch)
    database = tmp_path / "seed" / "state.sqlite3"
    SqliteInviteRegistry(database)
    (tmp_path / "other.sqlite3").hardlink_to(database)
    assert _code(lambda: SqliteInviteRegistry(database)) == "invite_registry_path_invalid"


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 API")
def test_real_win32_registry_round_trips_and_rejects_replay(tmp_path: Path) -> None:
    database = tmp_path / "seed" / "state.sqlite3"
    SqliteInviteRegistry(database).consume("nonce-a", now=1_000.0, expires_at=2_000.0)
    assert _code(
        lambda: SqliteInviteRegistry(database).consume("nonce-a", now=1_001.0, expires_at=2_000.0)
    ) == "invite_replayed"


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 API")
def test_real_win32_registry_refuses_an_inherited_acl_directory(tmp_path: Path) -> None:
    directory = tmp_path / "plain"
    directory.mkdir()  # inherits the temp directory's DACL: not owner-only
    assert _code(lambda: SqliteInviteRegistry(directory / "state.sqlite3")) == (
        "invite_registry_permissions_invalid"
    )
