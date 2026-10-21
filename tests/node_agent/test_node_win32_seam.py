# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_node identity, membership state and leases on a simulated win32 host.

``sys.platform`` reports ``win32`` and a ``FakeWin32Api`` is injected; every
call must route through the host platform seam (handles, owner-only DACLs,
CREATE_NEW, MoveFileExW) and fail closed with the module's own stable codes.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

import pytest

import mycelium_host_platform
from mycelium_host_platform import _win32, _win32_api as w
from mycelium_node import __main__ as node_main
from mycelium_node import durable_membership, identity, process

from tests.host_platform.fake_win32 import (
    USER_SID,
    USERS_SID,
    inherited_profile_acl,
    owner_only,
    parse_sddl,
    simulate_win32,
)


@pytest.fixture
def fake(monkeypatch):
    fake = simulate_win32(monkeypatch)
    monkeypatch.setattr(_win32, "REPLACE_BACKOFF_SECONDS", (0.0,))
    return fake


def _state(**overrides) -> dict:
    value = {
        "protocol": durable_membership.PROTOCOL,
        "node_id": "node-win",
        "swarm_id": "swarm-win",
        "seed_node_id": "seed-win",
        "seed_url": "https://127.0.0.1:4443",
        "seed_key_digest": "sha256:" + "b" * 64,
        "seed_key_records": [{"verification_key_digest": "sha256:" + "b" * 64}],
        "endpoint_id": "endpoint-win",
        "incarnation": "inc-win",
        "membership_generation": 1,
        "restart_count": 0,
    }
    value.update(overrides)
    return value


def _private_root(path: Path) -> Path:
    mycelium_host_platform.private_directory(path, create=True).close()
    return path


# ------------------------------------------------------------------ identity


def test_identity_is_created_once_through_create_new_and_reloaded(tmp_path, fake) -> None:
    key = tmp_path / "state" / "identity" / "node.key"
    first = identity.load_or_create_node_signer(key)
    second = identity.load_or_create_node_signer(key)
    loaded = identity.load_node_signer(key)
    assert first.verification_key_digest == second.verification_key_digest
    assert loaded.verification_key_digest == first.verification_key_digest
    assert len(key.read_bytes()) == 32
    create = [call for call in fake.calls if call[0] == "open" and call[4] == w.CREATE_NEW]
    assert len(create) == 1
    assert create[0][1] == str(key)
    assert create[0][6] == w.owner_only_file_sddl(USER_SID)
    assert fake.security_of(key.parent) == owner_only()
    assert fake.security_of(tmp_path / "state") == owner_only()
    assert fake.handles == {}


def test_identity_never_uses_posix_descriptor_walks(tmp_path, fake, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("POSIX identity walk used on win32")

    monkeypatch.setattr(identity, "_open_private_parent", forbidden)
    identity.load_or_create_node_signer(tmp_path / "identity" / "node.key")


def _identity_code(call) -> str:
    with pytest.raises(identity.NodeIdentityError) as caught:
        call()
    return caught.value.code


def test_identity_refusals_are_the_stable_node_codes(tmp_path, fake) -> None:
    key = tmp_path / "identity" / "node.key"
    assert _identity_code(lambda: identity.load_node_signer(key)) == "node_identity_path_invalid"
    identity.load_or_create_node_signer(key)

    fake.set_security(key, inherited_profile_acl())
    assert _identity_code(lambda: identity.load_node_signer(key)) == (
        "node_identity_permissions_invalid"
    )
    fake.set_security(key, parse_sddl(w.owner_only_file_sddl(USER_SID)))
    fake.set_security(key.parent, inherited_profile_acl())
    assert _identity_code(lambda: identity.load_node_signer(key)) == (
        "node_identity_permissions_invalid"
    )
    assert _identity_code(lambda: identity.load_or_create_node_signer(key)) == (
        "node_identity_permissions_invalid"
    )
    fake.set_security(key.parent, owner_only())

    os.unlink(key)
    assert _identity_code(lambda: identity.load_node_signer(key)) == "node_identity_missing"
    (tmp_path / "elsewhere.key").write_bytes(b"\x01" * 32)
    key.symlink_to(tmp_path / "elsewhere.key")
    assert _identity_code(lambda: identity.load_node_signer(key)) == "node_identity_path_invalid"


def test_an_incomplete_key_is_invalid_for_load_only(tmp_path, fake) -> None:
    key = tmp_path / "identity" / "node.key"
    _private_root(key.parent)
    with mycelium_host_platform.private_directory(key.parent, create=False) as lease:
        lease.create_file_exclusive("node.key", b"\x02" * 31)
    assert _identity_code(lambda: identity.load_node_signer(key)) == "node_identity_invalid"
    with mycelium_host_platform.private_directory(key.parent, create=False) as lease:
        lease.atomic_replace("node.key", b"\x02" * 33)
    assert _identity_code(lambda: identity.load_node_signer(key)) == "node_identity_invalid"


def test_identity_without_a_win32_api_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "win32")
    monkeypatch.setattr(mycelium_host_platform, "_API_OVERRIDE", None)
    monkeypatch.setattr(mycelium_host_platform, "_REAL_API", None)
    # On a real Windows host the binding would succeed: take ctypes.WinDLL away.
    monkeypatch.delattr(ctypes, "WinDLL", raising=False)
    key = tmp_path / "identity" / "node.key"
    assert _identity_code(lambda: identity.load_or_create_node_signer(key)) == (
        "node_identity_path_invalid"
    )
    assert not key.parent.exists()


# ------------------------------------------------------ durable membership


def _membership_code(call) -> str:
    with pytest.raises(durable_membership.DurableMembershipError) as caught:
        call()
    return caught.value.code


def test_membership_state_round_trips_through_move_file_ex(tmp_path, fake) -> None:
    root = _private_root(tmp_path / "state")
    assert durable_membership.load_membership_state(root) is None
    saved = durable_membership.save_membership_state(root, _state())
    assert durable_membership.load_membership_state(root) == saved
    durable_membership.save_membership_state(root, _state(restart_count=1))
    assert durable_membership.load_membership_state(root)["restart_count"] == 1
    moves = [call for call in fake.calls if call[0] == "move_replace"]
    assert len(moves) == 2
    assert all(call[2] == str(root / durable_membership.FILENAME) for call in moves)
    assert sorted(os.listdir(root)) == [durable_membership.FILENAME]
    assert fake.handles == {}


def test_membership_state_refusals_fail_closed(tmp_path, fake) -> None:
    root = _private_root(tmp_path / "state")
    durable_membership.save_membership_state(root, _state())
    path = root / durable_membership.FILENAME

    fake.set_security(path, inherited_profile_acl())
    assert _membership_code(lambda: durable_membership.load_membership_state(root)) == (
        "node_membership_state_invalid"
    )
    fake.set_security(path, parse_sddl(w.owner_only_file_sddl(USER_SID)))
    os.link(path, root / "second-link")
    assert _membership_code(lambda: durable_membership.load_membership_state(root)) == (
        "node_membership_state_invalid"
    )
    os.unlink(root / "second-link")

    fake.move_failures.extend([w.ERROR_SHARING_VIOLATION] * (_win32.REPLACE_ATTEMPTS + 1))
    assert _membership_code(
        lambda: durable_membership.save_membership_state(root, _state(restart_count=2))
    ) == "node_membership_state_write_failed"
    fake.move_failures.clear()
    assert durable_membership.load_membership_state(root)["restart_count"] == 0

    fake.set_security(root, inherited_profile_acl())
    assert _membership_code(lambda: durable_membership.load_membership_state(root)) == (
        "node_membership_state_invalid"
    )
    assert _membership_code(
        lambda: durable_membership.save_membership_state(root, _state())
    ) == "node_membership_state_write_failed"


def test_non_canonical_membership_bytes_are_invalid(tmp_path, fake) -> None:
    root = _private_root(tmp_path / "state")
    with mycelium_host_platform.private_directory(root, create=False) as lease:
        lease.atomic_replace(
            durable_membership.FILENAME, json.dumps(_state(), indent=2).encode()
        )
    assert _membership_code(lambda: durable_membership.load_membership_state(root)) == (
        "node_membership_state_invalid"
    )


# ------------------------------------------------------------- node leases


def test_private_directory_lease_is_handle_bound_on_win32(tmp_path, fake) -> None:
    target = tmp_path / "data"
    with process.private_directory_lease(target, create=True) as lease:
        assert isinstance(lease, process._Win32PrivateDirectoryLease)
        assert lease.exists and lease.path == target
        lease.revalidate()
        with lease.private_subdirectory("artifacts") as artifacts:
            assert artifacts.path == target / "artifacts"
            artifacts.revalidate()
        with pytest.raises(ValueError):
            lease.private_subdirectory("../escape")
        before = os.getcwd()
        with lease.working_directory():
            assert Path(os.getcwd()) == target
        assert os.getcwd() == before
        fake.set_security(target, inherited_profile_acl())
        with pytest.raises(ValueError, match="binding changed"):
            lease.revalidate()
    assert fake.handles == {}
    assert process.private_directory_path(tmp_path / "other", create=True) == tmp_path / "other"


def test_working_directory_restores_cwd_when_the_body_fails(tmp_path, fake) -> None:
    before = os.getcwd()
    with process.private_directory_lease(tmp_path / "data", create=True) as lease:
        with pytest.raises(RuntimeError):
            with lease.working_directory():
                raise RuntimeError("body")
    assert os.getcwd() == before


def test_lease_refusals_and_absent_leases(tmp_path, fake) -> None:
    widened = tmp_path / "widened"
    widened.mkdir()
    with pytest.raises(ValueError, match="invalid"):
        process.private_directory_lease(widened, create=False)
    with process.private_directory_lease(tmp_path / "absent", create=False) as lease:
        assert lease.exists is False
        with pytest.raises(ValueError):
            lease.revalidate()
    with pytest.raises(ValueError):
        process.private_directory_parent_fd(tmp_path / "x")


# -------------------------------------------------------- executable identity


def _exe(tmp_path: Path, name: str = "mycelium-iroh-sidecar.exe") -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * 62)
    return path


def test_executable_identity_is_handle_bound_on_win32(tmp_path, fake) -> None:
    sidecar = _exe(tmp_path)
    captured = process.capture_executable_identity(
        sidecar, require_canonical=True, require_private_owner=True
    )
    metadata = sidecar.stat()
    assert (captured.path, captured.device, captured.inode, captured.size) == (
        str(sidecar),
        metadata.st_dev,
        metadata.st_ino,
        64,
    )
    assert process.revalidate_executable_identity(captured) is True
    sidecar.write_bytes(b"MZ" + b"\1" * 70)
    assert process.revalidate_executable_identity(captured) is False


def test_executable_identity_refusals_on_win32(tmp_path, fake) -> None:
    script = _exe(tmp_path, "service.py")
    with pytest.raises(ValueError, match="invalid"):
        process.capture_executable_identity(script)
    assert process.capture_executable_identity(script, require_executable=False).path == str(script)

    sidecar = _exe(tmp_path)
    fake.set_security(
        sidecar,
        w.SecurityDescriptor(
            USER_SID,
            False,
            (
                w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0, w.FILE_ALL_ACCESS, USER_SID),
                w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0, w.FILE_WRITE_DATA, USERS_SID),
            ),
        ),
    )
    with pytest.raises(ValueError, match="invalid"):
        process.capture_executable_identity(
            sidecar, require_canonical=True, require_private_owner=True
        )
    # Read-only for other users is fine for the interpreter check.
    assert process.capture_executable_identity(sidecar, require_canonical=True)

    link = tmp_path / "bin" / "link.exe"
    link.symlink_to(_exe(tmp_path, "real.exe"))
    with pytest.raises(ValueError):
        process.capture_executable_identity(link, require_canonical=True)


# ------------------------------------------------------------ node entrypoint


def test_sidecar_candidates_are_exe_on_win32(tmp_path, fake, monkeypatch) -> None:
    candidates = node_main._win32_sidecar_candidates()
    assert candidates and all(path.name == "mycelium-iroh-sidecar.exe" for path in candidates)
    sidecar = _exe(tmp_path)
    monkeypatch.setattr(node_main, "_win32_sidecar_candidates", lambda: [tmp_path / "no.exe", sidecar])
    assert node_main._sidecar_path(None) == sidecar
    assert node_main._sidecar_path(str(sidecar)) == sidecar
    monkeypatch.setattr(node_main, "_win32_sidecar_candidates", lambda: [tmp_path / "no.exe"])
    with pytest.raises(ValueError):
        node_main._sidecar_path(None)


def test_join_bundle_file_is_read_as_a_user_only_file(tmp_path, fake) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes(b'{"a":1}')
    assert node_main._canonical_document(bundle) == {"a": 1}
    fake.set_security(
        bundle,
        w.SecurityDescriptor(
            USER_SID,
            False,
            (
                w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0, w.FILE_ALL_ACCESS, USER_SID),
                w.Ace(w.ACCESS_ALLOWED_ACE_TYPE, 0, 0x120089, USERS_SID),
            ),
        ),
    )
    with pytest.raises(ValueError, match="join bundle file is invalid"):
        node_main._canonical_document(bundle)
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        node_main._canonical_document(empty)


def test_temporary_root_is_owner_only_and_removed_by_identity(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setattr(node_main.tempfile, "gettempdir", lambda: str(tmp_path))
    root = node_main._temporary_root()
    assert root.path.parent == tmp_path and root.path.name.startswith("myc-node-")
    assert fake.security_of(root.path) == owner_only()
    (root.path / "sidecar.sock").write_bytes(b"")
    node_main._remove_temporary_root(root)
    assert not root.path.exists()
    with pytest.raises(RuntimeError):
        node_main._remove_temporary_root(root)

    other = node_main._temporary_root()
    replaced = node_main._TemporaryRoot(other.path, other.device, other.inode + 1)
    with pytest.raises(RuntimeError):
        node_main._remove_temporary_root(replaced)
    assert other.path.exists()
    assert fake.handles == {}
