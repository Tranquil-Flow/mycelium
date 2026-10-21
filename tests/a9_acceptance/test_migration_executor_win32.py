# SPDX-License-Identifier: AGPL-3.0-only
"""The private migration store on the win32 backend (FakeWin32Api).

On a Windows host ``fcntl`` does not exist and POSIX mode bits say nothing
about who can read a file, so the store goes through ``mycelium_host_platform``:
an owner-only private directory, owner-only records written by atomic replace,
and the transaction lock taken with ``LockFileEx``.  The POSIX path is
unchanged and covered by test_migration_executor.py.
"""

from __future__ import annotations

import pytest

from mycelium_platform_neutral import migration
from mycelium_platform_neutral import migration_executor as executor_module
from mycelium_platform_neutral.migration_executor import (
    FileMembershipState,
    MigrationError,
    MigrationExecutor,
)

from tests.a9_acceptance.test_migration_executor import NOW, fixture, request
from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32


@pytest.fixture
def fake(monkeypatch):
    # No POSIX lock may be reached on win32: poison it.
    monkeypatch.setattr(executor_module, "fcntl", None, raising=False)
    return simulate_win32(monkeypatch)


def test_win32_commit_and_restart_use_the_host_platform_lock(fake, tmp_path):
    store, envelope, _ = fixture(tmp_path)
    assert MigrationExecutor(store).execute(request(envelope), now=NOW) == "committed"
    assert FileMembershipState(store.root).read()["node-a"]["generation"] == 6
    assert any(call[0] == "lock_file" for call in fake.calls)
    assert (store.root / "transaction.lock").is_file()


def test_win32_interrupted_migration_resumes_after_restart(fake, tmp_path):
    store, envelope, _ = fixture(tmp_path)
    with pytest.raises(migration.MigrationUnavailable):
        MigrationExecutor(store).execute(request(envelope), now=NOW, stop_after="commit_pending")
    assert MigrationExecutor(FileMembershipState(store.root)).checkpoint() == "commit_pending"
    assert MigrationExecutor(FileMembershipState(store.root)).execute(request(envelope), now=NOW) == "committed"


def test_win32_held_lock_serializes_a_concurrent_resume(fake, tmp_path):
    store, envelope, _ = fixture(tmp_path)
    with FileMembershipState(store.root).lock():
        with pytest.raises(migration.MigrationUnavailable) as busy:
            with FileMembershipState(store.root).lock(blocking=False):
                pass
    assert busy.value.code == "concurrent_resume_serialized"
    with FileMembershipState(store.root).lock(blocking=False):
        pass


def test_win32_foreign_owned_record_is_refused(fake, tmp_path):
    store, _envelope, _ = fixture(tmp_path)
    fake.set_security(store.root / "members.json", owner_only(OTHER_USER_SID))
    with pytest.raises(MigrationError, match="unsafe_storage_file"):
        store.read()


def test_win32_foreign_owned_root_is_refused(fake, tmp_path):
    root = tmp_path / "private"
    FileMembershipState(root)
    fake.set_security(root, owner_only(OTHER_USER_SID))
    with pytest.raises(MigrationError, match="unsafe_storage_permissions"):
        FileMembershipState(root)
