# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load the exact seed private key bound by durable authority metadata."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import stat

from mycelium_node.identity import NodeIdentityError, load_node_signer
from mycelium_qualification.signing import Ed25519EvidenceSigner


class SeedAuthorityError(RuntimeError):
    """Stable error for a missing, ambiguous, or mismatched authority key."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


PRODUCT_PSEUDONYM_KEY_FILE = "product.pseudonym.key"
_PSEUDONYM_BYTES = 32


def derive_product_pseudonym_salt(
    signer: Ed25519EvidenceSigner,
    *,
    swarm_id: str,
) -> bytes:
    signature = signer.sign(
        {
            "protocol": "mycelium.product_pseudonym_seed.v1",
            "seed_key_digest": signer.verification_key_digest,
            "swarm_id": swarm_id,
        }
    )
    return hashlib.sha256(base64.b64decode(signature["signature"])).digest()


def load_product_pseudonym_salt(identity_root: Path) -> bytes:
    path = identity_root / PRODUCT_PSEUDONYM_KEY_FILE
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SeedAuthorityError("seed_product_pseudonym_key_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            value = os.read(descriptor, _PSEUDONYM_BYTES + 1)
        finally:
            os.close(descriptor)
    except SeedAuthorityError:
        raise
    except FileNotFoundError as exc:
        raise SeedAuthorityError("seed_product_pseudonym_key_missing") from exc
    except OSError as exc:
        raise SeedAuthorityError("seed_product_pseudonym_key_invalid") from exc
    if len(value) != _PSEUDONYM_BYTES:
        raise SeedAuthorityError("seed_product_pseudonym_key_invalid")
    return value


def ensure_product_pseudonym_salt(
    identity_root: Path,
    *,
    signer: Ed25519EvidenceSigner,
    swarm_id: str,
) -> bytes:
    path = identity_root / PRODUCT_PSEUDONYM_KEY_FILE
    if path.exists() or path.is_symlink():
        return load_product_pseudonym_salt(identity_root)
    value = derive_product_pseudonym_salt(signer, swarm_id=swarm_id)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(value):
            count = os.write(descriptor, value[written:])
            if count <= 0:
                raise OSError("short pseudonym-key write")
            written += count
        os.fsync(descriptor)
    except FileExistsError:
        return load_product_pseudonym_salt(identity_root)
    except OSError as exc:
        raise SeedAuthorityError("seed_product_pseudonym_key_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return value


def load_bound_seed_signer(
    identity_root: Path,
    *,
    expected_digest: str,
) -> Ed25519EvidenceSigner:
    """Select the one allowed key file matching committed database metadata."""

    matches: list[Ed25519EvidenceSigner] = []
    observed = False
    for name in ("seed.key", "seed.next.key"):
        path = identity_root / name
        if not path.exists() and not path.is_symlink():
            continue
        observed = True
        try:
            signer = load_node_signer(path)
        except NodeIdentityError as exc:
            raise SeedAuthorityError(exc.code) from exc
        if signer.verification_key_digest == expected_digest:
            matches.append(signer)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SeedAuthorityError("seed_authority_key_ambiguous")
    raise SeedAuthorityError(
        "seed_authority_key_mismatch" if observed else "seed_authority_key_missing"
    )


__all__ = [
    "PRODUCT_PSEUDONYM_KEY_FILE",
    "SeedAuthorityError",
    "derive_product_pseudonym_salt",
    "ensure_product_pseudonym_salt",
    "load_bound_seed_signer",
    "load_product_pseudonym_salt",
]
