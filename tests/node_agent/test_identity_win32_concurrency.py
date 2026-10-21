# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrent first start of a node identity on win32.

``_win32_write_new`` holds the new key file with no sharing while it writes it,
so a second thread that loads the identity at that moment gets
ERROR_SHARING_VIOLATION from CreateFileW.  On POSIX that reader sees a partial
file and retries; on Windows the violation was reported as
``node_identity_path_invalid`` (seen on the real host by
test_identity.py::test_concurrent_first_load_converges_on_one_identity).  A
sharing violation while creating is now "not complete yet", and the bounded
retry in ``load_or_create_node_signer`` converges on the one identity.
"""

from __future__ import annotations

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_host_platform import _win32_api as w
from mycelium_node import identity

from tests.host_platform.fake_win32 import simulate_win32


def _held_writer(fake, key):
    return fake.open(
        str(key), access=w.GENERIC_READ | w.GENERIC_WRITE, share=0,
        disposition=w.CREATE_NEW, flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
        sddl=w.owner_only_file_sddl(fake.user_sid),
    )


def test_read_file_reports_a_sharing_violation_distinctly(tmp_path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    root = tmp_path / "identity"
    with mycelium_host_platform.private_directory(root, create=True) as lease:
        handle = _held_writer(fake, root / "node.key")
        try:
            with pytest.raises(HostPlatformError) as caught:
                lease.read_file("node.key", max_bytes=33)
        finally:
            fake.close(handle)
    assert (caught.value.code, caught.value.detail) == ("host_io_failed", "sharing_violation")


def test_a_key_being_created_is_incomplete_not_invalid(tmp_path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    key = tmp_path / "identity" / "node.key"
    with mycelium_host_platform.private_directory(key.parent, create=True):
        pass
    handle = _held_writer(fake, key)
    try:
        assert identity._win32_load_existing(key, allow_incomplete=True, endpoint_id=None) is None
        # Without the creation window (plain load) it still fails closed.
        with pytest.raises(identity.NodeIdentityError):
            identity._win32_load_existing(key, allow_incomplete=False, endpoint_id=None)
    finally:
        fake.close(handle)
