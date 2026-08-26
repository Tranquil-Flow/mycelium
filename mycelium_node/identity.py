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


def _open_private_parent(key_path: Path) -> list[int]:
    components = key_path.parts
    if len(components) < 2 or components[0] != "/":
        raise NodeIdentityError("node_identity_path_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open("/", flags)
        descriptors.append(current)
        for component in components[1:-1]:
            current = os.open(component, flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise NodeIdentityError("node_identity_path_invalid")
        parent = os.fstat(current)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise NodeIdentityError("node_identity_permissions_invalid")
        return descriptors
    except NodeIdentityError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise NodeIdentityError("node_identity_path_invalid") from exc


def _signer(
    private_bytes: bytes,
    *,
    endpoint_id: str | None = None,
) -> Ed25519EvidenceSigner:
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except ValueError as exc:
        raise NodeIdentityError("node_identity_invalid") from exc
    public_bytes = private_key.public_key().public_bytes_raw()
    digest = sha256_bytes(public_bytes).split(":", 1)[1]
    if endpoint_id is not None and (
        not isinstance(endpoint_id, str)
        or not endpoint_id
        or endpoint_id != endpoint_id.strip()
    ):
        raise NodeIdentityError("node_identity_endpoint_invalid")
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint_id or f"node-identity-{digest[:32]}",
        _private_key=private_key,
        _public_key_bytes=public_bytes,
    )


def _load_existing(
    key_path: Path,
    *,
    allow_incomplete: bool = False,
    endpoint_id: str | None = None,
) -> Ed25519EvidenceSigner | None:
    descriptors: list[int] = []
    try:
        descriptors = _open_private_parent(key_path)
        parent_descriptor = descriptors[-1]
        metadata = os.stat(
            key_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
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
        descriptor = os.open(key_path.name, flags, dir_fd=parent_descriptor)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise NodeIdentityError("node_identity_path_invalid")
        private_bytes = os.read(descriptor, _KEY_BYTES + 1)
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise NodeIdentityError("node_identity_path_invalid")
    except NodeIdentityError:
        raise
    except FileNotFoundError:
        if allow_incomplete:
            return None
        raise NodeIdentityError("node_identity_missing") from None
    except OSError as exc:
        raise NodeIdentityError("node_identity_path_invalid") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if len(private_bytes) < _KEY_BYTES and allow_incomplete:
        return None
    if len(private_bytes) != _KEY_BYTES:
        raise NodeIdentityError("node_identity_invalid")
    return _signer(private_bytes, endpoint_id=endpoint_id)


def _write_new(
    key_path: Path,
    *,
    endpoint_id: str | None = None,
) -> Ed25519EvidenceSigner | None:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors = _open_private_parent(key_path)
    parent_descriptor = descriptors[-1]
    try:
        descriptor = os.open(key_path.name, flags, 0o600, dir_fd=parent_descriptor)
        descriptors.append(descriptor)
    except FileExistsError:
        for open_descriptor in reversed(descriptors):
            os.close(open_descriptor)
        return None
    except OSError as exc:
        for open_descriptor in reversed(descriptors):
            os.close(open_descriptor)
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
        os.fsync(parent_descriptor)
    except OSError as exc:
        try:
            os.unlink(key_path.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise NodeIdentityError("node_identity_write_failed") from exc
    finally:
        for open_descriptor in reversed(descriptors):
            os.close(open_descriptor)
    return _signer(private_bytes, endpoint_id=endpoint_id)


def load_or_create_node_signer(
    key_file: str | Path,
    *,
    endpoint_id: str | None = None,
) -> Ed25519EvidenceSigner:
    """Load one durable signer or atomically create it on first startup."""

    key_path = _absolute_path(key_file)
    _prepare_private_parent(key_path)
    if key_path.exists() or key_path.is_symlink():
        existing = _load_existing(
            key_path,
            allow_incomplete=True,
            endpoint_id=endpoint_id,
        )
        if existing is not None:
            return existing
    else:
        created = _write_new(key_path, endpoint_id=endpoint_id)
        if created is not None:
            return created
    for _attempt in range(_CREATE_RETRIES):
        existing = _load_existing(
            key_path,
            allow_incomplete=True,
            endpoint_id=endpoint_id,
        )
        if existing is not None:
            return existing
        time.sleep(_CREATE_RETRY_SECONDS)
    raise NodeIdentityError("node_identity_invalid")


def load_node_signer(
    key_file: str | Path,
    *,
    endpoint_id: str | None = None,
) -> Ed25519EvidenceSigner:
    """Load an existing owner-only signer without creating any path or key."""

    signer = _load_existing(_absolute_path(key_file), endpoint_id=endpoint_id)
    if signer is None:  # pragma: no cover - load-only never permits incomplete keys
        raise NodeIdentityError("node_identity_invalid")
    return signer
