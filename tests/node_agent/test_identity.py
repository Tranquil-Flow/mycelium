from __future__ import annotations

import os
from pathlib import Path
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from mycelium_node.identity import NodeIdentityError, load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier


def test_identity_survives_restart_and_signs_with_same_pin(tmp_path: Path) -> None:
    key_path = tmp_path / "private" / "node-identity.key"
    first = load_or_create_node_signer(key_path)
    second = load_or_create_node_signer(key_path)

    assert first.endpoint_id == second.endpoint_id
    assert first.verification_key_digest == second.verification_key_digest
    assert first.public_key_record() == second.public_key_record()
    statement = {"protocol": "test.node.identity.v1", "value": 17}
    verifier = build_ed25519_verifier([second.public_key_record()])
    assert verifier(canonical_json_bytes(statement), first.sign(statement)) is True

    metadata = key_path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.lstat().st_mode) == 0o700
    assert key_path.stat().st_size == 32
    assert "verification_key" not in key_path.name


def test_concurrent_first_load_converges_on_one_identity(tmp_path: Path) -> None:
    key_path = tmp_path / "identity" / "node.key"
    with ThreadPoolExecutor(max_workers=8) as pool:
        signers = list(pool.map(lambda _index: load_or_create_node_signer(key_path), range(32)))
    assert len({item.endpoint_id for item in signers}) == 1
    assert len({item.verification_key_digest for item in signers}) == 1


def test_rejects_symlinked_key_and_hardlinked_key(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "target"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    symlink = private / "symlink.key"
    symlink.symlink_to(target)
    with pytest.raises(NodeIdentityError) as symlink_error:
        load_or_create_node_signer(symlink)
    assert symlink_error.value.code == "node_identity_path_invalid"

    hardlink = private / "hardlink.key"
    os.link(target, hardlink)
    with pytest.raises(NodeIdentityError) as hardlink_error:
        load_or_create_node_signer(hardlink)
    assert hardlink_error.value.code == "node_identity_path_invalid"


def test_rejects_symlinked_or_public_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(NodeIdentityError) as linked_error:
        load_or_create_node_signer(linked_parent / "node.key")
    assert linked_error.value.code == "node_identity_path_invalid"

    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    with pytest.raises(NodeIdentityError) as permissions_error:
        load_or_create_node_signer(public_parent / "node.key")
    assert permissions_error.value.code == "node_identity_permissions_invalid"


def test_rejects_truncated_or_nonexact_key_file_mode(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    truncated = private / "truncated.key"
    truncated.write_bytes(b"short")
    truncated.chmod(0o600)
    with pytest.raises(NodeIdentityError) as truncated_error:
        load_or_create_node_signer(truncated)
    assert truncated_error.value.code == "node_identity_invalid"

    public = private / "public.key"
    public.write_bytes(b"x" * 32)
    public.chmod(0o644)
    with pytest.raises(NodeIdentityError) as public_error:
        load_or_create_node_signer(public)
    assert public_error.value.code == "node_identity_permissions_invalid"

    owner_executable = private / "owner-executable.key"
    owner_executable.write_bytes(b"y" * 32)
    owner_executable.chmod(0o700)
    with pytest.raises(NodeIdentityError) as owner_mode_error:
        load_or_create_node_signer(owner_executable)
    assert owner_mode_error.value.code == "node_identity_permissions_invalid"
