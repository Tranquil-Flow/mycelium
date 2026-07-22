# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable fail-closed Ed25519 identity for a physical node agent."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner


_KEY_BYTES = 32
_CREATE_RETRIES = 100
_CREATE_RETRY_SECONDS = 0.005


class NodeIdentityError(RuntimeError):
    """Stable node-identity filesystem or key failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _prepare_private_parent(key_path: Path) -> None:
    parent = key_path.parent
    try:
        missing: list[Path] = []
        nearest = parent
        while not nearest.exists() and not nearest.is_symlink():
            missing.append(nearest)
            if nearest == nearest.parent:
                break
            nearest = nearest.parent
        for ancestor in (nearest, *nearest.parents):
            metadata = ancestor.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise NodeIdentityError("node_identity_path_invalid")
        if not stat.S_ISDIR(nearest.lstat().st_mode):
            raise NodeIdentityError("node_identity_path_invalid")
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise NodeIdentityError("node_identity_path_invalid")
            directory.chmod(0o700)
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise NodeIdentityError("node_identity_path_invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise NodeIdentityError("node_identity_permissions_invalid")
        if metadata.st_uid != os.getuid():
            raise NodeIdentityError("node_identity_permissions_invalid")
    except NodeIdentityError:
        raise
    except OSError as exc:
        raise NodeIdentityError("node_identity_path_invalid") from exc


def _signer(private_bytes: bytes) -> Ed25519EvidenceSigner:
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except ValueError as exc:
        raise NodeIdentityError("node_identity_invalid") from exc
    public_bytes = private_key.public_key().public_bytes_raw()
    digest = sha256_bytes(public_bytes).split(":", 1)[1]
    return Ed25519EvidenceSigner(
        endpoint_id=f"node-identity-{digest[:32]}",
        _private_key=private_key,
        _public_key_bytes=public_bytes,
    )


def _load_existing(key_path: Path, *, allow_incomplete: bool = False) -> Ed25519EvidenceSigner | None:
    try:
        metadata = key_path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise NodeIdentityError("node_identity_path_invalid")
        if mode != 0o600:
            raise NodeIdentityError("node_identity_permissions_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(key_path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise NodeIdentityError("node_identity_path_invalid")
            private_bytes = os.read(descriptor, _KEY_BYTES + 1)
        finally:
            os.close(descriptor)
    except NodeIdentityError:
        raise
    except OSError as exc:
        raise NodeIdentityError("node_identity_path_invalid") from exc
    if len(private_bytes) < _KEY_BYTES and allow_incomplete:
        return None
    if len(private_bytes) != _KEY_BYTES:
        raise NodeIdentityError("node_identity_invalid")
    return _signer(private_bytes)


def _write_new(key_path: Path) -> Ed25519EvidenceSigner | None:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return None
    except OSError as exc:
        raise NodeIdentityError("node_identity_path_invalid") from exc
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(private_bytes):
            count = os.write(descriptor, private_bytes[written:])
            if count <= 0:
                raise OSError("short node identity write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        try:
            key_path.unlink()
        except OSError:
            pass
        raise NodeIdentityError("node_identity_write_failed") from exc
    finally:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(
            key_path.parent,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise NodeIdentityError("node_identity_write_failed") from exc
    return _signer(private_bytes)


def load_or_create_node_signer(key_file: str | Path) -> Ed25519EvidenceSigner:
    """Load one durable signer or atomically create it on first startup."""

    key_path = _absolute_path(key_file)
    _prepare_private_parent(key_path)
    if key_path.exists() or key_path.is_symlink():
        existing = _load_existing(key_path, allow_incomplete=True)
        if existing is not None:
            return existing
    else:
        created = _write_new(key_path)
        if created is not None:
            return created
    for _attempt in range(_CREATE_RETRIES):
        existing = _load_existing(key_path, allow_incomplete=True)
        if existing is not None:
            return existing
        time.sleep(_CREATE_RETRY_SECONDS)
    raise NodeIdentityError("node_identity_invalid")
