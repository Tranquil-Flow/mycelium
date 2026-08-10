# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-only batch invitation bundles for trusted swarm operators."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import math
import os
from pathlib import Path
import secrets
import shutil
import stat
import time
from typing import Any

from mycelium_invite import mint_invite_bundle
from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.process import private_directory_lease
from mycelium_qualification.evidence import canonical_json_bytes

from .http import SeedHTTPClient


INVITE_BATCH_PROTOCOL = "mycelium.invite_batch.v1"
MAX_BATCH_INVITES = 64
MIN_INVITE_TTL_SECONDS = 30
MAX_INVITE_TTL_SECONDS = 86_400
_CREATE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class InviteBatchError(RuntimeError):
    """Fail-closed batch invitation error carrying a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _existing_owner_file(path: Path, code: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InviteBatchError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise InviteBatchError(code)


def _write_private(path: Path, document: Mapping[str, Any]) -> str:
    body = canonical_json_bytes(dict(document))
    try:
        descriptor = os.open(path, _CREATE_FILE_FLAGS, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(body):
                count = os.write(descriptor, body[written:])
                if count <= 0:
                    raise OSError("short invite batch write")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise InviteBatchError("invite_batch_write_failed") from exc
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _sync_private_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, _OPEN_DIRECTORY_FLAGS)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise OSError("invite batch directory is invalid")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise InviteBatchError("invite_batch_write_failed") from exc


def mint_invite_batch(
    *,
    seed_data_dir: Path,
    seed_url: str,
    swarm_id: str,
    output_root: Path,
    count: int,
    ttl_seconds: int,
    now: Callable[[], float] = time.time,
    batch_id_source: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Verify one live durable seed and mint a bounded owner-only invite batch."""

    if type(count) is not int or not 1 <= count <= MAX_BATCH_INVITES:
        raise InviteBatchError("invite_batch_count_invalid")
    if (
        type(ttl_seconds) is not int
        or not MIN_INVITE_TTL_SECONDS <= ttl_seconds <= MAX_INVITE_TTL_SECONDS
    ):
        raise InviteBatchError("invite_batch_ttl_invalid")
    if not isinstance(swarm_id, str) or not swarm_id or swarm_id != swarm_id.strip():
        raise InviteBatchError("invite_batch_swarm_invalid")
    try:
        issued_at = float(now())
    except Exception as exc:
        raise InviteBatchError("invite_batch_clock_invalid") from exc
    if not math.isfinite(issued_at) or not issued_at > 0:
        raise InviteBatchError("invite_batch_clock_invalid")

    try:
        seed_root = private_directory_lease(seed_data_dir, create=False)
    except ValueError as exc:
        raise InviteBatchError("invite_batch_seed_state_invalid") from exc
    try:
        seed_root.revalidate()
        key_file = seed_root.path / "identity" / "seed.key"
        state_file = seed_root.path / "state.sqlite3"
        _existing_owner_file(key_file, "invite_batch_seed_identity_invalid")
        _existing_owner_file(state_file, "invite_batch_seed_state_invalid")
        signer = load_or_create_node_signer(key_file)
        seed_root.revalidate()
    except InviteBatchError:
        raise
    except Exception as exc:
        raise InviteBatchError("invite_batch_seed_state_invalid") from exc
    finally:
        seed_root.close()

    client = SeedHTTPClient(
        seed_url=seed_url,
        swarm_id=swarm_id,
        seed_key_digest=signer.verification_key_digest,
        seed_key_records=[signer.public_key_record()],
    )
    try:
        # The seed issues its identity response after this process samples its clock.
        # A small verifier headroom avoids rejecting that causally-later statement as
        # future-dated while remaining far inside the bounded identity TTL.
        identity = client.identity(now=issued_at + 1.0)
    except Exception as exc:
        raise InviteBatchError("invite_batch_seed_unverified") from exc

    try:
        output_lease = private_directory_lease(output_root, create=True)
    except ValueError as exc:
        raise InviteBatchError("invite_batch_output_invalid") from exc
    created: Path | None = None
    try:
        output_lease.revalidate()
        source = batch_id_source or (lambda: secrets.token_hex(12))
        batch_id = source()
        if (
            not isinstance(batch_id, str)
            or not batch_id
            or len(batch_id) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in batch_id)
        ):
            raise InviteBatchError("invite_batch_id_invalid")
        batch_directory = output_lease.path / f"invite-batch-{batch_id}"
        try:
            batch_directory.mkdir(mode=0o700)
        except OSError as exc:
            raise InviteBatchError("invite_batch_output_invalid") from exc
        created = batch_directory
        if stat.S_IMODE(created.stat().st_mode) != 0o700:
            raise InviteBatchError("invite_batch_output_invalid")

        files: list[dict[str, Any]] = []
        for index in range(1, count + 1):
            nonce = f"native-{batch_id}-{index:03d}"
            bundle = mint_invite_bundle(
                signer=signer,
                swarm_id=swarm_id,
                seed_url=seed_url,
                ttl_seconds=ttl_seconds,
                nonce=nonce,
                issued_at=issued_at,
            )
            name = f"native-node-{index:03d}.invite.json"
            digest = _write_private(created / name, bundle)
            files.append({"file": name, "digest": digest})
        manifest = {
            "protocol": INVITE_BATCH_PROTOCOL,
            "batch_id": batch_id,
            "swarm_id": swarm_id,
            "seed_url": seed_url,
            "seed_node_id": identity["seed_node_id"],
            "seed_key_digest": signer.verification_key_digest,
            "issued_at": issued_at,
            "expires_at": issued_at + ttl_seconds,
            "invite_count": count,
            "peer_class": "mac_mlx_iroh",
            "activation_eligible_after_join": False,
            "files": files,
        }
        manifest_digest = _write_private(created / "manifest.json", manifest)
        _sync_private_directory(created)
        _sync_private_directory(output_lease.path)
        return {
            "protocol": INVITE_BATCH_PROTOCOL,
            "batch_id": batch_id,
            "output_directory": str(created),
            "invite_count": count,
            "manifest_digest": manifest_digest,
            "route_ready": False,
        }
    except BaseException:
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)
        raise
    finally:
        output_lease.close()


__all__ = [
    "INVITE_BATCH_PROTOCOL",
    "InviteBatchError",
    "MAX_BATCH_INVITES",
    "mint_invite_batch",
]
