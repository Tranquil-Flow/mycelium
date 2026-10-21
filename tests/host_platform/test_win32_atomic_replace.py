# SPDX-License-Identifier: AGPL-3.0-only
"""win32 atomic replacement: CREATE_NEW + flush + MoveFileExW + directory flush.

The sequence must be exactly the one that gives the POSIX guarantee on NTFS,
sharing violations are retried within a bound, and exhaustion fails closed
without leaving a temporary file or touching the previous content.
"""

from __future__ import annotations

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_host_platform import _win32, _win32_api as w

from tests.host_platform.fake_win32 import USER_SID, owner_only, parse_sddl, simulate_win32


@pytest.fixture
def fake(monkeypatch):
    fake = simulate_win32(monkeypatch)
    monkeypatch.setattr(_win32, "REPLACE_BACKOFF_SECONDS", (0.0,))
    return fake


@pytest.fixture
def lease(tmp_path, fake):
    lease = mycelium_host_platform.private_directory(tmp_path / "state", create=True)
    yield lease
    lease.close()


def _code(call) -> str:
    with pytest.raises(HostPlatformError) as caught:
        call()
    return caught.value.code


def _leftovers(tmp_path) -> list[str]:
    return sorted(path.name for path in (tmp_path / "state").iterdir() if path.name.endswith(".tmp"))


def test_replace_follows_the_durable_ntfs_sequence(tmp_path, fake, lease) -> None:
    del fake.calls[:]
    lease.atomic_replace("membership.state.json", b'{"v":1}')
    assert (tmp_path / "state" / "membership.state.json").read_bytes() == b'{"v":1}'
    names = fake.call_names()
    create = names.index("open")
    create_call = fake.calls[create]
    assert create_call[4] == w.CREATE_NEW
    assert create_call[3] == 0
    assert create_call[6] == f"O:{USER_SID}D:P(A;;FA;;;{USER_SID})(A;;FA;;;SY)"
    assert create_call[1].startswith(str(tmp_path / "state" / ".membership.state.json."))
    write = names.index("write", create)
    flush_file = names.index("flush", write)
    close_file = names.index("close", flush_file)
    move = names.index("move_replace", close_file)
    flush_directory = names.index("flush", move)
    assert create < write < flush_file < close_file < move < flush_directory
    move_call = fake.calls[move]
    assert move_call[1] == create_call[1]
    assert move_call[2] == str(tmp_path / "state" / "membership.state.json")
    # The directory flush goes through a handle bound to the leased identity.
    flush_open = max(
        index for index, call in enumerate(fake.calls[:flush_directory]) if call[0] == "open"
    )
    assert fake.calls[flush_open][1] == str(tmp_path / "state")
    assert fake.calls[flush_open][2] & w.GENERIC_WRITE
    assert _leftovers(tmp_path) == []


def test_the_replaced_file_is_owner_only_and_readable_back(tmp_path, fake, lease) -> None:
    lease.atomic_replace("state.json", b"one")
    lease.atomic_replace("state.json", b"two")
    assert lease.read_file("state.json", max_bytes=16) == b"two"
    assert fake.security_of(tmp_path / "state" / "state.json") == parse_sddl(
        w.owner_only_file_sddl(USER_SID)
    )
    assert fake.security_of(tmp_path / "state") == owner_only()


@pytest.mark.parametrize(
    "winerror",
    [w.ERROR_SHARING_VIOLATION, w.ERROR_ACCESS_DENIED, w.ERROR_LOCK_VIOLATION],
)
def test_a_transient_sharing_violation_is_retried(tmp_path, fake, lease, winerror) -> None:
    fake.move_failures.extend([winerror, winerror])
    lease.atomic_replace("state.json", b"after")
    assert (tmp_path / "state" / "state.json").read_bytes() == b"after"
    assert fake.call_names().count("move_replace") == 3
    assert _leftovers(tmp_path) == []


def test_persistent_contention_fails_closed_and_keeps_the_old_content(tmp_path, fake, lease) -> None:
    lease.atomic_replace("state.json", b"before")
    fake.move_failures.extend([w.ERROR_SHARING_VIOLATION] * (_win32.REPLACE_ATTEMPTS + 5))
    assert _code(lambda: lease.atomic_replace("state.json", b"after")) == "host_replace_contended"
    assert (tmp_path / "state" / "state.json").read_bytes() == b"before"
    assert _leftovers(tmp_path) == []
    moves = [call for call in fake.calls if call[0] == "move_replace"]
    assert len(moves) == 1 + _win32.REPLACE_ATTEMPTS


def test_a_reader_without_share_delete_blocks_the_replace_bounded(tmp_path, fake, lease) -> None:
    lease.atomic_replace("state.json", b"before")
    reader = fake.open(
        str(tmp_path / "state" / "state.json"),
        access=w.GENERIC_READ,
        share=w.FILE_SHARE_READ,
        disposition=w.OPEN_EXISTING,
        flags=0,
    )
    try:
        assert _code(lambda: lease.atomic_replace("state.json", b"after")) == "host_replace_contended"
    finally:
        fake.close(reader)
    assert (tmp_path / "state" / "state.json").read_bytes() == b"before"
    lease.atomic_replace("state.json", b"after")
    assert (tmp_path / "state" / "state.json").read_bytes() == b"after"


def test_a_non_retryable_move_error_fails_immediately(tmp_path, fake, lease) -> None:
    fake.move_failures.append(w.ERROR_PATH_NOT_FOUND)
    assert _code(lambda: lease.atomic_replace("state.json", b"x")) == "host_io_failed"
    assert fake.call_names().count("move_replace") == 1
    assert not (tmp_path / "state" / "state.json").exists()
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_write_or_flush_failure_never_publishes(tmp_path, fake, lease, operation) -> None:
    fake.fail_next(operation, w.ERROR_ACCESS_DENIED)
    assert _code(lambda: lease.atomic_replace("state.json", b"x")) == "host_io_failed"
    assert "move_replace" not in fake.call_names()
    assert not (tmp_path / "state" / "state.json").exists()
    assert _leftovers(tmp_path) == []


def test_a_directory_flush_failure_is_reported_not_skipped(tmp_path, fake, lease) -> None:
    original_flush = fake.flush
    flushes: list[int] = []

    def flush(handle: int) -> None:
        flushes.append(handle)
        if fake.handles[handle].kind == "directory":
            raise w.Win32Error(w.ERROR_ACCESS_DENIED, "FlushFileBuffers")
        original_flush(handle)

    fake.flush = flush
    assert _code(lambda: lease.atomic_replace("state.json", b"x")) == "host_io_failed"
    assert len(flushes) == 2


def test_replace_refuses_a_directory_whose_acl_widened(tmp_path, fake, lease) -> None:
    from tests.host_platform.fake_win32 import inherited_profile_acl

    fake.set_security(tmp_path / "state", inherited_profile_acl())
    assert _code(lambda: lease.atomic_replace("state.json", b"x")) == "host_permissions_invalid"
    assert not (tmp_path / "state" / "state.json").exists()
