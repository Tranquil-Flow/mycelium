# SPDX-License-Identifier: AGPL-3.0-only
"""Exclusive owner-only lock files on both backends (win32 through FakeWin32Api)."""

from __future__ import annotations

import sys
import os
import threading

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32


def _busy(directory, name="t.lock"):
    with pytest.raises(HostPlatformError) as caught:
        with directory.exclusive_lock(name, blocking=False):
            pass
    return caught.value.code


def test_posix_lock_is_exclusive_and_released(tmp_path):
    root = tmp_path / "private"
    with mycelium_host_platform.private_directory(root, create=True) as first, \
            mycelium_host_platform.private_directory(root, create=False) as second:
        with first.exclusive_lock("t.lock"):
            assert _busy(second) == "host_lock_busy"
        with second.exclusive_lock("t.lock", blocking=False):
            pass
        if sys.platform != "win32":  # no mode bits on win32
            assert oct(os.stat(root / "t.lock").st_mode & 0o777) == "0o600"


def test_posix_blocking_lock_waits_for_the_holder(tmp_path):
    root = tmp_path / "private"
    order = []
    with mycelium_host_platform.private_directory(root, create=True) as directory:
        held = threading.Event()

        def holder():
            with mycelium_host_platform.private_directory(root, create=False) as other:
                with other.exclusive_lock("t.lock"):
                    held.set()
                    order.append("holder")
                    threading.Event().wait(0.2)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        with directory.exclusive_lock("t.lock"):
            order.append("waiter")
        thread.join()
    assert order == ["holder", "waiter"]


def test_posix_lock_refuses_a_symlinked_lock_file(tmp_path):
    root = tmp_path / "private"
    with mycelium_host_platform.private_directory(root, create=True) as directory:
        os.symlink(tmp_path / "elsewhere", root / "t.lock")
        with pytest.raises(HostPlatformError) as caught:
            with directory.exclusive_lock("t.lock"):
                pass
        # The win32 backend refuses the reparse point as a path, POSIX at open.
        assert caught.value.code == ("host_path_invalid" if sys.platform == "win32" else "host_io_failed")


def test_win32_lock_is_exclusive_owner_only_and_released(monkeypatch, tmp_path):
    fake = simulate_win32(monkeypatch)
    root = tmp_path / "private"
    with mycelium_host_platform.private_directory(root, create=True) as first, \
            mycelium_host_platform.private_directory(root, create=False) as second:
        with first.exclusive_lock("t.lock"):
            assert _busy(second) == "host_lock_busy"
        with second.exclusive_lock("t.lock", blocking=False):
            pass
    assert any(call[0] == "unlock_file" for call in fake.calls)
    assert not fake.__dict__.get("file_locks")


def test_win32_lock_refuses_a_file_others_can_write(monkeypatch, tmp_path):
    fake = simulate_win32(monkeypatch)
    root = tmp_path / "private"
    with mycelium_host_platform.private_directory(root, create=True) as directory:
        with directory.exclusive_lock("t.lock"):
            pass
        fake.set_security(root / "t.lock", owner_only(OTHER_USER_SID))
        with pytest.raises(HostPlatformError) as caught:
            with directory.exclusive_lock("t.lock"):
                pass
        assert caught.value.code == "host_permissions_invalid"
