# SPDX-License-Identifier: AGPL-3.0-only
"""win32 private directories: handle-bound, reparse-refusing, owner-only.

Driven through ``FakeWin32Api`` with ``sys.platform`` reported as ``win32``;
every refusal must be a stable ``HostPlatformError`` code, never a silent
success and never a raw ``OSError``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_host_platform import _win32_api as w

from tests.host_platform.fake_win32 import (
    EVERYONE_SID,
    OTHER_USER_SID,
    USER_SID,
    USERS_SID,
    inherited_profile_acl,
    owner_only,
    simulate_win32,
)


@pytest.fixture
def fake(monkeypatch):
    return simulate_win32(monkeypatch)


def _code(call) -> str:
    with pytest.raises(HostPlatformError) as caught:
        call()
    return caught.value.code


def _open(path: Path, *, create: bool = True):
    return mycelium_host_platform.private_directory(path, create=create)


def test_creating_a_nested_private_directory_uses_the_owner_only_sddl(tmp_path, fake) -> None:
    target = tmp_path / "Mycelium" / "state"
    with _open(target) as lease:
        assert lease.exists
        assert lease.path == str(target)
        lease.revalidate()
    created = [call for call in fake.calls if call[0] == "create_directory"]
    assert [call[1] for call in created] == [str(tmp_path / "Mycelium"), str(target)]
    expected = f"O:{USER_SID}D:P(A;OICI;FA;;;{USER_SID})(A;OICI;FA;;;SY)"
    assert {call[2] for call in created} == {expected}
    assert fake.security_of(target) == owner_only()


def test_every_component_is_opened_without_following_reparse_points(tmp_path, fake) -> None:
    target = tmp_path / "a" / "b"
    _open(target).close()
    opens = [call for call in fake.calls if call[0] == "open"]
    assert opens
    for _name, _path, _access, _share, _disposition, flags, _sddl in opens:
        assert flags & w.FILE_FLAG_OPEN_REPARSE_POINT
        assert flags & w.FILE_FLAG_BACKUP_SEMANTICS


def test_the_leased_directory_and_its_parent_are_held_without_share_delete(tmp_path, fake) -> None:
    target = tmp_path / "state"
    with _open(target):
        for path in (target, tmp_path):
            held = fake.open_handles_for(path)
            assert held, path
            assert all(not handle.share & w.FILE_SHARE_DELETE for handle in held)
        # The directory cannot be renamed away while it is leased.
        with pytest.raises(w.Win32Error) as caught:
            fake.move_replace(str(target), str(tmp_path / "moved"))
        assert caught.value.winerror == w.ERROR_SHARING_VIOLATION
    assert fake.handles == {}


def test_an_existing_owner_only_directory_is_accepted(tmp_path, fake) -> None:
    target = tmp_path / "state"
    target.mkdir()
    fake.set_security(target, owner_only())
    with _open(target, create=False) as lease:
        assert lease.exists
        assert lease.identity is not None


@pytest.mark.parametrize(
    ("descriptor", "reason"),
    [
        (inherited_profile_acl(), "dacl_not_protected"),
        (
            w.SecurityDescriptor(
                USER_SID,
                True,
                (
                    w.Ace(0, 0x3, w.FILE_ALL_ACCESS, USER_SID),
                    w.Ace(0, 0x3, 0x120089, USERS_SID),
                ),
            ),
            "untrusted_principal",
        ),
        (
            w.SecurityDescriptor(
                USER_SID,
                True,
                (w.Ace(0, 0x3, w.FILE_ALL_ACCESS, USER_SID), w.Ace(0, 0, 0x120089, EVERYONE_SID)),
            ),
            "untrusted_principal",
        ),
        (
            w.SecurityDescriptor(
                USER_SID,
                True,
                (w.Ace(0, 0x3, w.FILE_ALL_ACCESS, w.ADMINISTRATORS_SID), w.Ace(0, 3, w.FILE_ALL_ACCESS, USER_SID)),
            ),
            "untrusted_principal",
        ),
        (
            w.SecurityDescriptor(
                OTHER_USER_SID, True, (w.Ace(0, 0x3, w.FILE_ALL_ACCESS, USER_SID),)
            ),
            "owner_not_user",
        ),
        (w.SecurityDescriptor(USER_SID, True, None), "null_dacl"),
        (w.SecurityDescriptor(USER_SID, True, ()), "empty_dacl"),
        (
            w.SecurityDescriptor(
                USER_SID,
                True,
                (
                    w.Ace(1, 0x3, w.FILE_WRITE_DATA, EVERYONE_SID),
                    w.Ace(0, 0x3, w.FILE_ALL_ACCESS, USER_SID),
                ),
            ),
            "ace_type_refused",
        ),
        (
            w.SecurityDescriptor(
                USER_SID, True, (w.Ace(0, 0x3, w.FILE_ALL_ACCESS, w.SYSTEM_SID),)
            ),
            "user_not_granted",
        ),
    ],
)
def test_a_directory_that_is_not_owner_only_is_refused(tmp_path, fake, descriptor, reason) -> None:
    target = tmp_path / "state"
    target.mkdir()
    fake.set_security(target, descriptor)
    with pytest.raises(HostPlatformError) as caught:
        _open(target, create=False)
    assert caught.value.code == "host_permissions_invalid"
    assert caught.value.detail == reason
    assert fake.handles == {}


def test_a_reparse_point_anywhere_on_the_path_is_refused(tmp_path, fake) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "junction").symlink_to(real, target_is_directory=True)
    assert _code(lambda: _open(tmp_path / "junction" / "state")) == "host_path_invalid"
    assert not (real / "state").exists()
    assert _code(lambda: _open(tmp_path / "junction", create=False)) == "host_path_invalid"
    assert fake.handles == {}


def test_a_final_path_mismatch_is_refused(tmp_path, fake) -> None:
    target = tmp_path / "state"
    _open(target).close()
    fake.final_path_overrides[os.path.realpath(target)] = str(tmp_path / "elsewhere")
    assert _code(lambda: _open(target, create=False)) == "host_path_invalid"


def test_a_missing_directory_without_create_is_an_absent_lease(tmp_path, fake) -> None:
    with _open(tmp_path / "state", create=False) as lease:
        assert lease.exists is False
        assert _code(lease.revalidate) == "host_path_invalid"
    assert not (tmp_path / "state").exists()


def test_a_missing_ancestor_without_create_is_refused(tmp_path, fake) -> None:
    assert _code(lambda: _open(tmp_path / "a" / "b", create=False)) == "host_path_invalid"


def test_revalidate_catches_a_changed_security_descriptor(tmp_path, fake) -> None:
    target = tmp_path / "state"
    with _open(target) as lease:
        fake.set_security(target, inherited_profile_acl())
        assert _code(lease.revalidate) == "host_permissions_invalid"


def test_subdirectories_are_created_owner_only(tmp_path, fake) -> None:
    with _open(tmp_path / "state") as lease:
        with lease.subdirectory("artifacts") as child:
            assert child.path == str(tmp_path / "state" / "artifacts")
            child.revalidate()
        for name in ("", ".", "..", "a/b", "a\\b", "c:ads", "trailing."):
            assert _code(lambda name=name: lease.subdirectory(name)) == "host_path_invalid"
    assert fake.security_of(tmp_path / "state" / "artifacts") == owner_only()


def test_read_file_returns_only_owner_only_single_link_plain_files(tmp_path, fake) -> None:
    with _open(tmp_path / "state") as lease:
        assert _code(lambda: lease.read_file("missing", max_bytes=16)) == "host_file_missing"
        lease.create_file_exclusive("key", b"k" * 32)
        assert lease.read_file("key", max_bytes=32) == b"k" * 32
        assert _code(lambda: lease.read_file("key", max_bytes=31)) == "host_file_invalid"

        os.link(tmp_path / "state" / "key", tmp_path / "state" / "key-link")
        assert _code(lambda: lease.read_file("key", max_bytes=32)) == "host_file_invalid"
        os.unlink(tmp_path / "state" / "key-link")

        (tmp_path / "state" / "plain").write_bytes(b"x")
        assert _code(lambda: lease.read_file("plain", max_bytes=8)) == "host_permissions_invalid"

        (tmp_path / "outside").write_bytes(b"secret")
        (tmp_path / "state" / "link").symlink_to(tmp_path / "outside")
        assert _code(lambda: lease.read_file("link", max_bytes=8)) == "host_path_invalid"
    assert fake.handles == {}


def test_create_file_exclusive_never_overwrites(tmp_path, fake) -> None:
    with _open(tmp_path / "state") as lease:
        lease.create_file_exclusive("key", b"first")
        assert _code(lambda: lease.create_file_exclusive("key", b"second")) == "host_file_exists"
        assert lease.read_file("key", max_bytes=16) == b"first"
    create = [call for call in fake.calls if call[0] == "open" and call[4] == w.CREATE_NEW]
    assert create[0][6] == f"O:{USER_SID}D:P(A;;FA;;;{USER_SID})(A;;FA;;;SY)"
    assert create[0][3] == 0  # no sharing while the new file is written


def test_a_failed_exclusive_write_removes_the_partial_file(tmp_path, fake) -> None:
    with _open(tmp_path / "state") as lease:
        fake.fail_next("write", w.ERROR_ACCESS_DENIED)
        assert _code(lambda: lease.create_file_exclusive("key", b"data")) == "host_io_failed"
        assert not (tmp_path / "state" / "key").exists()
        fake.short_writes = True
        assert _code(lambda: lease.create_file_exclusive("key", b"data")) == "host_io_failed"
        assert not (tmp_path / "state" / "key").exists()


def test_close_is_idempotent_and_releases_every_handle(tmp_path, fake) -> None:
    lease = _open(tmp_path / "state")
    lease.close()
    lease.close()
    assert fake.handles == {}
    assert _code(lease.revalidate) == "host_path_invalid"
