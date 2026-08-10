# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-only operator controls for one durable seed membership authority."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import time
from typing import Any, Callable

from mycelium_invite import SqliteInviteRegistry
from mycelium_node.identity import load_node_signer, load_or_create_node_signer
from mycelium_node.process import private_directory_lease
from mycelium_membership import peer_runtime_is_activation_eligible
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier

from .authority import (
    PRODUCT_PSEUDONYM_KEY_FILE,
    SeedAuthorityError,
    derive_product_pseudonym_salt,
    ensure_product_pseudonym_salt,
    load_bound_seed_signer,
)
from .coordinator import SeedCoordinator, SeedCoordinatorError
from .state import SeedStateError, SqliteSeedState


SEED_OPERATOR_INVENTORY_PROTOCOL = "mycelium.seed_operator_inventory.v1"
SEED_OPERATOR_REVOCATION_PROTOCOL = "mycelium.seed_operator_revocation.v1"
SEED_BACKUP_PROTOCOL = "mycelium.seed_backup.v1"
SEED_KEY_TRANSITION_PROTOCOL = "mycelium.seed_key_transition.v1"
SEED_OPERATOR_ROTATION_PROTOCOL = "mycelium.seed_operator_rotation.v1"
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_ROTATION_OVERLAP_SECONDS = 86_400.0


class SeedOperatorError(RuntimeError):
    """Stable fail-closed operator error without private source values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _write_private(path: Path, body: bytes) -> None:
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
        while written < len(body):
            count = os.write(descriptor, body[written:])
            if count <= 0:
                raise OSError("short private write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise SeedOperatorError("seed_operator_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        raise SeedOperatorError("seed_operator_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SeedOperatorError("seed_operator_backup_invalid") from exc
    return "sha256:" + digest.hexdigest()


def _verify_database_integrity(path: Path) -> None:
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=30.0,
        )
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise SeedOperatorError("seed_operator_backup_database_corrupt")
    except SeedOperatorError:
        raise
    except sqlite3.Error as exc:
        raise SeedOperatorError("seed_operator_backup_database_corrupt") from exc
    finally:
        if connection is not None:
            connection.close()


def _backup_manifest(backup_root: Path) -> dict[str, Any]:
    manifest_path = backup_root / "manifest.json"
    _owner_file(manifest_path, "seed_operator_backup_invalid")
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise SeedOperatorError("seed_operator_backup_invalid") from exc
    expected = {
        "protocol",
        "backup_id",
        "created_at_unix_ms",
        "swarm_id",
        "seed_node_id",
        "seed_key_digest",
        "authority_generation",
        "database_digest",
        "member_count",
    }
    allowed = (expected, expected | {"product_pseudonym_digest"})
    if (
        not isinstance(document, dict)
        or set(document) not in allowed
        or document.get("protocol") != SEED_BACKUP_PROTOCOL
        or canonical_json_bytes(document) != raw
    ):
        raise SeedOperatorError("seed_operator_backup_invalid")
    return document


def verify_seed_key_transition(
    envelope: dict[str, Any],
    *,
    now: float,
    expected_old_digest: str | None = None,
) -> dict[str, Any]:
    """Verify continuity proof from both sides of one seed-key transition."""

    expected_envelope = {
        "protocol",
        "transition",
        "old_signature",
        "old_verification_key",
        "new_signature",
        "new_verification_key",
    }
    transition_fields = {
        "swarm_id",
        "seed_node_id",
        "previous_generation",
        "authority_generation",
        "old_seed_key_digest",
        "new_seed_key_digest",
        "initiated_at",
        "effective_at",
        "overlap_expires_at",
        "reason",
    }
    if (
        not isinstance(envelope, dict)
        or set(envelope) != expected_envelope
        or envelope.get("protocol") != SEED_KEY_TRANSITION_PROTOCOL
        or not isinstance(envelope.get("transition"), dict)
        or set(envelope["transition"]) != transition_fields
    ):
        raise SeedOperatorError("seed_operator_rotation_record_invalid")
    transition = dict(envelope["transition"])
    old_record = envelope["old_verification_key"]
    new_record = envelope["new_verification_key"]
    old_signature = envelope["old_signature"]
    new_signature = envelope["new_signature"]
    try:
        initiated = float(transition["initiated_at"])
        effective = float(transition["effective_at"])
        overlap_expires = float(transition["overlap_expires_at"])
        previous_generation = int(transition["previous_generation"])
        authority_generation = int(transition["authority_generation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedOperatorError("seed_operator_rotation_record_invalid") from exc
    if (
        not isinstance(now, (int, float))
        or isinstance(now, bool)
        or not math.isfinite(float(now))
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in (initiated, effective, overlap_expires)
        )
        or previous_generation < 1
        or authority_generation != previous_generation + 1
        or initiated > effective
        or effective >= overlap_expires
        or not isinstance(transition["reason"], str)
        or _REASON_RE.fullmatch(transition["reason"]) is None
        or not isinstance(old_record, dict)
        or not isinstance(new_record, dict)
        or old_record.get("verification_key_digest")
        != transition["old_seed_key_digest"]
        or new_record.get("verification_key_digest")
        != transition["new_seed_key_digest"]
        or transition["old_seed_key_digest"] == transition["new_seed_key_digest"]
        or (
            expected_old_digest is not None
            and transition["old_seed_key_digest"] != expected_old_digest
        )
    ):
        raise SeedOperatorError("seed_operator_rotation_record_invalid")
    body = canonical_json_bytes(transition)
    try:
        old_verify = build_ed25519_verifier([old_record])
        new_verify = build_ed25519_verifier([new_record])
    except Exception as exc:
        raise SeedOperatorError("seed_operator_rotation_key_invalid") from exc
    if not old_verify(body, old_signature):
        raise SeedOperatorError("seed_operator_rotation_old_signature_invalid")
    if not new_verify(body, new_signature):
        raise SeedOperatorError("seed_operator_rotation_new_signature_invalid")
    return transition


def _owner_file(path: Path, code: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SeedOperatorError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SeedOperatorError(code)


def _load_authority(state_root: Path, *, incarnation: str):
    lease = None
    try:
        lease = private_directory_lease(state_root, create=False)
        if not lease.exists:
            raise SeedOperatorError("seed_operator_state_missing")
        lease.revalidate()
        database = lease.path / "state.sqlite3"
        _owner_file(database, "seed_operator_database_invalid")
        state = SqliteSeedState(database)
        binding = state.identity_binding()
        signer = load_bound_seed_signer(
            lease.path / "identity",
            expected_digest=binding["seed_key_digest"],
        )
        coordinator = SeedCoordinator(
            swarm_id=binding["swarm_id"],
            seed_node_id=binding["seed_node_id"],
            seed_url=None,
            signer=signer,
            invite_registry=SqliteInviteRegistry(database),
            incarnation=incarnation,
            state=state,
        )
        lease.revalidate()
        return lease, coordinator, binding
    except SeedOperatorError:
        if lease is not None:
            lease.close()
        raise
    except SeedAuthorityError as exc:
        if lease is not None:
            lease.close()
        code = {
            "node_identity_permissions_invalid": "seed_operator_identity_invalid",
            "node_identity_path_invalid": "seed_operator_identity_invalid",
            "node_identity_invalid": "seed_operator_identity_invalid",
            "seed_authority_key_mismatch": "seed_operator_identity_mismatch",
        }.get(exc.code, "seed_operator_state_invalid")
        raise SeedOperatorError(code) from exc
    except (OSError, SeedCoordinatorError, SeedStateError, ValueError) as exc:
        if lease is not None:
            lease.close()
        raise SeedOperatorError("seed_operator_state_invalid") from exc


def seed_inventory(
    state_root: Path,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Return a privacy-reduced inventory from the durable coordinator store."""

    lease, coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-inventory",
    )
    try:
        observed_at = float(now())
        members = []
        for member in coordinator.members():
            lifecycle = member["lifecycle_state"]
            lease_fresh = float(member["lease_expires_at"]) > observed_at
            policy_eligible = peer_runtime_is_activation_eligible(
                member["peer_class"],
                member["runtime_capability"],
            )
            members.append(
                {
                    "node_id": member["node_id"],
                    "peer_class": member["peer_class"],
                    "generation": member["generation"],
                    "incarnation": member["incarnation"],
                    "lifecycle_state": lifecycle,
                    "lease_freshness": "fresh" if lease_fresh else "expired",
                    "activation_eligible": (
                        policy_eligible
                        and lease_fresh
                        and lifecycle in {"CONFIGURED", "RUNNING"}
                    ),
                    "revocation_state": (
                        "revoked" if lifecycle == "STOPPED" else "active"
                    ),
                }
            )
        return {
            "protocol": SEED_OPERATOR_INVENTORY_PROTOCOL,
            "swarm_id": binding["swarm_id"],
            "seed_node_id": binding["seed_node_id"],
            "seed_key_digest": binding["seed_key_digest"],
            "observed_at_unix_ms": int(observed_at * 1_000),
            "members": members,
            "route_ready": False,
        }
    except (TypeError, ValueError) as exc:
        raise SeedOperatorError("seed_operator_inventory_invalid") from exc
    finally:
        lease.close()


def revoke_seed_member(
    state_root: Path,
    *,
    node_id: str,
    expected_generation: int,
    reason: str,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Fence one member generation and persist a terminal lifecycle state."""

    if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
        raise SeedOperatorError("seed_operator_reason_invalid")
    lease, coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-revoke",
    )
    try:
        member = coordinator.advance_member_generation(
            node_id=node_id,
            expected_generation=expected_generation,
            lifecycle_state="STOPPED",
        )
        return {
            "protocol": SEED_OPERATOR_REVOCATION_PROTOCOL,
            "swarm_id": binding["swarm_id"],
            "seed_key_digest": binding["seed_key_digest"],
            "node_id": member["node_id"],
            "previous_generation": expected_generation,
            "generation": member["generation"],
            "lifecycle_state": member["lifecycle_state"],
            "reason": reason,
            "revoked_at_unix_ms": int(float(now()) * 1_000),
            "route_ready": False,
        }
    except SeedCoordinatorError as exc:
        raise SeedOperatorError(exc.code) from exc
    finally:
        lease.close()


def begin_seed_key_rotation(
    state_root: Path,
    *,
    reason: str,
    overlap_seconds: float,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Create and durably bind a dual-signed pending seed-key transition."""

    if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
        raise SeedOperatorError("seed_operator_reason_invalid")
    if (
        not isinstance(overlap_seconds, (int, float))
        or isinstance(overlap_seconds, bool)
        or not math.isfinite(float(overlap_seconds))
        or float(overlap_seconds) <= 0.0
        or float(overlap_seconds) > _MAX_ROTATION_OVERLAP_SECONDS
    ):
        raise SeedOperatorError("seed_operator_rotation_overlap_invalid")
    lease, coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-rotate-begin",
    )
    next_key = lease.path / "identity" / "seed.next.key"
    transition_path = lease.path / "identity" / "seed.rotation.json"
    created_key = False
    created_transition = False
    try:
        if (
            next_key.exists()
            or next_key.is_symlink()
            or transition_path.exists()
            or transition_path.is_symlink()
        ):
            raise SeedOperatorError("seed_operator_rotation_pending")
        state = SqliteSeedState(lease.path / "state.sqlite3")
        authority = state.authority_state()
        if (
            authority["rotation"] is not None
            and authority["rotation"]["status"] == "PENDING"
        ):
            raise SeedOperatorError("seed_operator_rotation_pending")
        ensure_product_pseudonym_salt(
            lease.path / "identity",
            signer=coordinator.signer,
            swarm_id=binding["swarm_id"],
        )
        new_signer = load_or_create_node_signer(next_key)
        created_key = True
        instant = float(now())
        if not math.isfinite(instant):
            raise SeedOperatorError("seed_operator_clock_invalid")
        previous_generation = int(authority["authority_generation"])
        transition = {
            "swarm_id": binding["swarm_id"],
            "seed_node_id": binding["seed_node_id"],
            "previous_generation": previous_generation,
            "authority_generation": previous_generation + 1,
            "old_seed_key_digest": binding["seed_key_digest"],
            "new_seed_key_digest": new_signer.verification_key_digest,
            "initiated_at": instant,
            "effective_at": instant,
            "overlap_expires_at": instant + float(overlap_seconds),
            "reason": reason,
        }
        envelope = {
            "protocol": SEED_KEY_TRANSITION_PROTOCOL,
            "transition": transition,
            "old_signature": coordinator.signer.sign(transition),
            "old_verification_key": coordinator.signer.public_key_record(),
            "new_signature": new_signer.sign(transition),
            "new_verification_key": new_signer.public_key_record(),
        }
        verify_seed_key_transition(
            envelope,
            now=instant,
            expected_old_digest=binding["seed_key_digest"],
        )
        _write_private(transition_path, canonical_json_bytes(envelope))
        created_transition = True
        _sync_directory(next_key.parent)
        state.begin_authority_rotation(transition)
        return {
            "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
            "event": "rotation_pending",
            "swarm_id": binding["swarm_id"],
            "previous_generation": previous_generation,
            "authority_generation": previous_generation + 1,
            "old_seed_key_digest": binding["seed_key_digest"],
            "new_seed_key_digest": new_signer.verification_key_digest,
            "effective_at_unix_ms": int(instant * 1_000),
            "overlap_expires_at_unix_ms": int(
                (instant + float(overlap_seconds)) * 1_000
            ),
            "route_ready": False,
        }
    except SeedOperatorError:
        if created_transition:
            transition_path.unlink(missing_ok=True)
        if created_key:
            next_key.unlink(missing_ok=True)
        raise
    except (OSError, SeedStateError, TypeError, ValueError) as exc:
        if created_transition:
            transition_path.unlink(missing_ok=True)
        if created_key:
            next_key.unlink(missing_ok=True)
        raise SeedOperatorError("seed_operator_rotation_failed") from exc
    finally:
        lease.close()


def seed_key_rotation_status(state_root: Path) -> dict[str, Any]:
    """Return the public state of the latest durable authority transition."""

    lease, _coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-rotate-status",
    )
    try:
        state = SqliteSeedState(lease.path / "state.sqlite3")
        authority = state.authority_state()
        rotation = authority["rotation"]
        if rotation is None:
            return {
                "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
                "event": "rotation_absent",
                "swarm_id": binding["swarm_id"],
                "authority_generation": authority["authority_generation"],
                "seed_key_digest": binding["seed_key_digest"],
                "route_ready": False,
            }
        acknowledgements = state.seed_rotation_acknowledgements(
            authority_generation=rotation["authority_generation"]
        )
        return {
            "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
            "event": "rotation_" + rotation["status"].lower(),
            "swarm_id": binding["swarm_id"],
            "previous_generation": rotation["previous_generation"],
            "authority_generation": rotation["authority_generation"],
            "old_seed_key_digest": rotation["old_seed_key_digest"],
            "new_seed_key_digest": rotation["new_seed_key_digest"],
            "effective_at_unix_ms": int(float(rotation["effective_at"]) * 1_000),
            "overlap_expires_at_unix_ms": int(
                float(rotation["overlap_expires_at"]) * 1_000
            ),
            "acknowledged_members": [
                {
                    "node_id": item["node_id"],
                    "member_generation": item["member_generation"],
                    "acknowledged_at_unix_ms": int(
                        float(item["acknowledged_at"]) * 1_000
                    ),
                }
                for item in acknowledgements
            ],
            "route_ready": False,
        }
    except (SeedStateError, TypeError, ValueError) as exc:
        raise SeedOperatorError("seed_operator_rotation_state_invalid") from exc
    finally:
        lease.close()


def complete_seed_key_rotation(
    state_root: Path,
    *,
    authority_generation: int,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Promote a verified pending key and fence the previous authority key."""

    if (
        not isinstance(authority_generation, int)
        or isinstance(authority_generation, bool)
        or authority_generation < 2
    ):
        raise SeedOperatorError("seed_operator_rotation_generation_invalid")
    lease, _coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-rotate-complete",
    )
    try:
        state = SqliteSeedState(lease.path / "state.sqlite3")
        authority = state.authority_state()
        rotation = authority["rotation"]
        if (
            rotation is None
            or rotation["authority_generation"] != authority_generation
        ):
            raise SeedOperatorError("seed_operator_rotation_unknown")
        transition_path = lease.path / "identity" / "seed.rotation.json"
        _owner_file(transition_path, "seed_operator_rotation_record_invalid")
        try:
            envelope = json.loads(transition_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise SeedOperatorError("seed_operator_rotation_record_invalid") from exc
        instant = float(now())
        if not math.isfinite(instant):
            raise SeedOperatorError("seed_operator_clock_invalid")
        transition = verify_seed_key_transition(
            envelope,
            now=instant,
            expected_old_digest=rotation["old_seed_key_digest"],
        )
        if transition != rotation["transition"]:
            raise SeedOperatorError("seed_operator_rotation_record_mismatch")
        next_key = lease.path / "identity" / "seed.next.key"
        active_key = lease.path / "identity" / "seed.key"
        if rotation["status"] == "PENDING":
            if instant < float(rotation["effective_at"]):
                raise SeedOperatorError("seed_operator_rotation_not_effective")
            if instant > float(rotation["overlap_expires_at"]):
                raise SeedOperatorError("seed_operator_rotation_overlap_expired")
            _owner_file(next_key, "seed_operator_rotation_key_invalid")
            next_signer = load_node_signer(next_key)
            if (
                next_signer.verification_key_digest
                != rotation["new_seed_key_digest"]
                or binding["seed_key_digest"] != rotation["old_seed_key_digest"]
            ):
                raise SeedOperatorError("seed_operator_rotation_key_mismatch")
            transition_digest = "sha256:" + hashlib.sha256(
                canonical_json_bytes(rotation["transition"])
            ).hexdigest()
            acknowledgements = {
                item["node_id"]: item
                for item in state.seed_rotation_acknowledgements(
                    authority_generation=authority_generation
                )
            }
            required_members = [
                member
                for member in state.load_members()
                if member["lease_expires_at"] > instant
                and member["lifecycle_state"] in {"CONFIGURED", "RUNNING"}
                and peer_runtime_is_activation_eligible(
                    member["peer_class"], member["runtime_capability"]
                )
            ]
            missing = [
                member["node_id"]
                for member in required_members
                if (
                    member["node_id"] not in acknowledgements
                    or acknowledgements[member["node_id"]]["member_generation"]
                    != member["generation"]
                    or acknowledgements[member["node_id"]]["transition_digest"]
                    != transition_digest
                )
            ]
            if missing:
                raise SeedOperatorError(
                    "seed_operator_rotation_acknowledgements_incomplete"
                )
            state.complete_authority_rotation(
                authority_generation=authority_generation,
                new_seed_key_digest=rotation["new_seed_key_digest"],
            )
        elif (
            rotation["status"] != "COMPLETED"
            or binding["seed_key_digest"] != rotation["new_seed_key_digest"]
        ):
            raise SeedOperatorError("seed_operator_rotation_state_invalid")
        if next_key.exists() or next_key.is_symlink():
            os.replace(next_key, active_key)
            active_key.chmod(0o600)
            _sync_directory(active_key.parent)
        active_signer = load_node_signer(active_key)
        if active_signer.verification_key_digest != rotation["new_seed_key_digest"]:
            raise SeedOperatorError("seed_operator_rotation_key_mismatch")
        return {
            "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
            "event": "rotation_completed",
            "swarm_id": binding["swarm_id"],
            "authority_generation": authority_generation,
            "old_seed_key_digest": rotation["old_seed_key_digest"],
            "new_seed_key_digest": rotation["new_seed_key_digest"],
            "completed_at_unix_ms": int(instant * 1_000),
            "route_ready": False,
        }
    except SeedOperatorError:
        raise
    except (OSError, SeedAuthorityError, SeedStateError, TypeError, ValueError) as exc:
        raise SeedOperatorError("seed_operator_rotation_failed") from exc
    finally:
        lease.close()


def backup_seed_state(
    state_root: Path,
    *,
    output_root: Path,
    now: Callable[[], float] = time.time,
    backup_id_source: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Capture the key and a transactionally consistent SQLite generation."""

    lease, coordinator, binding = _load_authority(
        state_root,
        incarnation="seed-operator-backup",
    )
    output_lease = None
    created = None
    try:
        output_lease = private_directory_lease(output_root, create=True)
        output_lease.revalidate()
        source = backup_id_source or (lambda: f"backup-{secrets.token_hex(12)}")
        backup_id = source()
        if (
            not isinstance(backup_id, str)
            or not backup_id.startswith("backup-")
            or len(backup_id) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in backup_id)
        ):
            raise SeedOperatorError("seed_operator_backup_id_invalid")
        created = output_lease.path / backup_id
        created.mkdir(mode=0o700)
        created.chmod(0o700)
        identity_dir = created / "identity"
        identity_dir.mkdir(mode=0o700)
        identity_dir.chmod(0o700)

        pseudonym_salt = ensure_product_pseudonym_salt(
            lease.path / "identity",
            signer=coordinator.signer,
            swarm_id=binding["swarm_id"],
        )

        source_key = lease.path / "identity" / "seed.key"
        if (
            not source_key.exists()
            or load_node_signer(source_key).verification_key_digest
            != binding["seed_key_digest"]
        ):
            source_key = lease.path / "identity" / "seed.next.key"
        _owner_file(source_key, "seed_operator_identity_invalid")
        if (
            load_node_signer(source_key).verification_key_digest
            != binding["seed_key_digest"]
        ):
            raise SeedOperatorError("seed_operator_identity_mismatch")
        _write_private(identity_dir / "seed.key", source_key.read_bytes())
        _write_private(
            identity_dir / PRODUCT_PSEUDONYM_KEY_FILE,
            pseudonym_salt,
        )
        database_target = created / "state.sqlite3"
        source_database = sqlite3.connect(
            f"file:{lease.path / 'state.sqlite3'}?mode=ro",
            uri=True,
            timeout=30.0,
        )
        target_database = sqlite3.connect(database_target, timeout=30.0)
        try:
            source_database.backup(target_database)
            target_database.execute("PRAGMA synchronous = FULL")
            target_database.commit()
        finally:
            target_database.close()
            source_database.close()
        database_target.chmod(0o600)
        _owner_file(database_target, "seed_operator_backup_invalid")

        members = coordinator.members()
        authority = SqliteSeedState(database_target).authority_state()
        manifest = {
            "protocol": SEED_BACKUP_PROTOCOL,
            "backup_id": backup_id,
            "created_at_unix_ms": int(float(now()) * 1_000),
            "swarm_id": binding["swarm_id"],
            "seed_node_id": binding["seed_node_id"],
            "seed_key_digest": binding["seed_key_digest"],
            "authority_generation": authority["authority_generation"],
            "product_pseudonym_digest": "sha256:"
            + hashlib.sha256(pseudonym_salt).hexdigest(),
            "database_digest": _file_digest(database_target),
            "member_count": len(members),
        }
        _write_private(created / "manifest.json", canonical_json_bytes(manifest))
        _sync_directory(identity_dir)
        _sync_directory(created)
        _sync_directory(output_lease.path)
        return {
            "protocol": SEED_BACKUP_PROTOCOL,
            "backup_id": backup_id,
            "seed_key_digest": binding["seed_key_digest"],
            "authority_generation": authority["authority_generation"],
            "member_count": len(members),
            "backup_directory": str(created),
            "route_ready": False,
        }
    except SeedOperatorError:
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)
        raise SeedOperatorError("seed_operator_backup_failed") from exc
    finally:
        if output_lease is not None:
            output_lease.close()
        lease.close()


def restore_seed_state(
    backup_root: Path,
    *,
    target_root: Path,
) -> dict[str, Any]:
    """Restore one complete verified generation into an absent private root."""

    backup_lease = None
    target_lease = None
    created_target = False
    try:
        if target_root.exists() or target_root.is_symlink():
            raise SeedOperatorError("seed_operator_restore_target_exists")
        backup_lease = private_directory_lease(backup_root, create=False)
        if not backup_lease.exists:
            raise SeedOperatorError("seed_operator_backup_invalid")
        backup_lease.revalidate()
        manifest = _backup_manifest(backup_lease.path)
        key_source = backup_lease.path / "identity" / "seed.key"
        pseudonym_source = (
            backup_lease.path / "identity" / PRODUCT_PSEUDONYM_KEY_FILE
        )
        database_source = backup_lease.path / "state.sqlite3"
        _owner_file(key_source, "seed_operator_backup_invalid")
        if "product_pseudonym_digest" in manifest:
            _owner_file(pseudonym_source, "seed_operator_backup_invalid")
        _owner_file(database_source, "seed_operator_backup_invalid")
        signer = load_node_signer(key_source)
        if signer.verification_key_digest != manifest["seed_key_digest"]:
            raise SeedOperatorError("seed_operator_backup_identity_mismatch")
        if _file_digest(database_source) != manifest["database_digest"]:
            raise SeedOperatorError("seed_operator_backup_digest_mismatch")
        _verify_database_integrity(database_source)
        source_state = SqliteSeedState(database_source)
        binding = source_state.identity_binding()
        if binding != {
            "swarm_id": manifest["swarm_id"],
            "seed_node_id": manifest["seed_node_id"],
            "seed_key_digest": manifest["seed_key_digest"],
        } or len(source_state.load_members()) != manifest["member_count"]:
            raise SeedOperatorError("seed_operator_backup_identity_mismatch")
        if (
            source_state.authority_state()["authority_generation"]
            != manifest["authority_generation"]
        ):
            raise SeedOperatorError("seed_operator_backup_generation_mismatch")
        if "product_pseudonym_digest" in manifest:
            pseudonym_salt = pseudonym_source.read_bytes()
            if (
                len(pseudonym_salt) != 32
                or "sha256:" + hashlib.sha256(pseudonym_salt).hexdigest()
                != manifest["product_pseudonym_digest"]
            ):
                raise SeedOperatorError("seed_operator_backup_pseudonym_mismatch")
        else:
            pseudonym_salt = derive_product_pseudonym_salt(
                signer,
                swarm_id=manifest["swarm_id"],
            )

        target_lease = private_directory_lease(target_root, create=True)
        created_target = True
        identity_target = target_lease.path / "identity"
        identity_target.mkdir(mode=0o700)
        identity_target.chmod(0o700)
        _write_private(identity_target / "seed.key", key_source.read_bytes())
        _write_private(
            identity_target / PRODUCT_PSEUDONYM_KEY_FILE,
            pseudonym_salt,
        )
        _write_private(target_lease.path / "state.sqlite3", database_source.read_bytes())
        _write_private(
            target_lease.path / "restore-manifest.json",
            canonical_json_bytes(manifest),
        )
        _sync_directory(identity_target)
        _sync_directory(target_lease.path)
        target_lease.revalidate()
        restored_signer = load_node_signer(identity_target / "seed.key")
        restored_state = SqliteSeedState(target_lease.path / "state.sqlite3")
        if (
            restored_signer.verification_key_digest != manifest["seed_key_digest"]
            or restored_state.identity_binding() != binding
        ):
            raise SeedOperatorError("seed_operator_restore_verification_failed")
        return {
            "protocol": SEED_BACKUP_PROTOCOL,
            "event": "seed_restored",
            "backup_id": manifest["backup_id"],
            "seed_key_digest": manifest["seed_key_digest"],
            "authority_generation": manifest["authority_generation"],
            "member_count": manifest["member_count"],
            "route_ready": False,
        }
    except SeedOperatorError:
        if target_lease is not None:
            target_lease.close()
            target_lease = None
        if created_target:
            shutil.rmtree(target_root, ignore_errors=True)
        raise
    except (OSError, sqlite3.Error, SeedStateError, ValueError) as exc:
        if target_lease is not None:
            target_lease.close()
            target_lease = None
        if created_target:
            shutil.rmtree(target_root, ignore_errors=True)
        raise SeedOperatorError("seed_operator_restore_failed") from exc
    finally:
        if target_lease is not None:
            target_lease.close()
        if backup_lease is not None:
            backup_lease.close()


__all__ = [
    "SEED_OPERATOR_INVENTORY_PROTOCOL",
    "SEED_OPERATOR_REVOCATION_PROTOCOL",
    "SEED_KEY_TRANSITION_PROTOCOL",
    "SEED_OPERATOR_ROTATION_PROTOCOL",
    "SeedOperatorError",
    "backup_seed_state",
    "begin_seed_key_rotation",
    "complete_seed_key_rotation",
    "revoke_seed_member",
    "restore_seed_state",
    "seed_key_rotation_status",
    "seed_inventory",
    "verify_seed_key_transition",
]
