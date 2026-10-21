# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-only durable membership credential for invite-free node restart."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError
from mycelium_qualification.evidence import canonical_json_bytes


PROTOCOL = "mycelium.node.membership_state.v1"
FILENAME = "membership.state.json"
_FIELDS = frozenset(
    {
        "protocol",
        "node_id",
        "swarm_id",
        "seed_node_id",
        "seed_url",
        "seed_key_digest",
        "seed_key_records",
        "endpoint_id",
        "incarnation",
        "membership_generation",
        "restart_count",
    }
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_BYTES = 64 * 1024


class DurableMembershipError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _validate(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if set(value) != _FIELDS or value.get("protocol") != PROTOCOL:
            raise DurableMembershipError("node_membership_state_invalid")
        for field in (
            "node_id",
            "swarm_id",
            "seed_node_id",
            "endpoint_id",
            "incarnation",
        ):
            if not isinstance(value[field], str) or _SEGMENT_RE.fullmatch(value[field]) is None:
                raise DurableMembershipError("node_membership_state_invalid")
        if (
            not isinstance(value["seed_url"], str)
            or not value["seed_url"]
            or value["seed_url"] != value["seed_url"].strip()
            or not isinstance(value["seed_key_digest"], str)
            or _DIGEST_RE.fullmatch(value["seed_key_digest"]) is None
        ):
            raise DurableMembershipError("node_membership_state_invalid")
        records = value["seed_key_records"]
        if (
            not isinstance(records, list)
            or len(records) != 1
            or not isinstance(records[0], Mapping)
            or records[0].get("verification_key_digest")
            != value["seed_key_digest"]
        ):
            raise DurableMembershipError("node_membership_state_invalid")
        for field in ("membership_generation", "restart_count"):
            item = value[field]
            minimum = 1 if field == "membership_generation" else 0
            if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
                raise DurableMembershipError("node_membership_state_invalid")
        detached = json.loads(canonical_json_bytes(dict(value)))
        if not isinstance(detached, dict):
            raise DurableMembershipError("node_membership_state_invalid")
        return detached
    except DurableMembershipError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise DurableMembershipError("node_membership_state_invalid") from exc


def _win32_load_membership_state(state_root: Path) -> dict[str, Any] | None:
    """Read the state file through a leased owner-only directory handle."""

    value = _win32_load_owner_json(state_root, FILENAME)
    return None if value is None else _validate(value)


def _win32_load_owner_json(state_root: Path, filename: str) -> Mapping[str, Any] | None:
    try:
        with mycelium_host_platform.private_directory(state_root, create=False) as root:
            if not root.exists:
                raise DurableMembershipError("node_membership_state_unavailable")
            raw = root.read_file(filename, max_bytes=_MAX_BYTES)
    except HostPlatformError as exc:
        if exc.code == "host_file_missing":
            return None
        if exc.code in {"host_file_invalid", "host_permissions_invalid", "host_path_invalid"}:
            raise DurableMembershipError("node_membership_state_invalid") from exc
        raise DurableMembershipError("node_membership_state_unavailable") from exc
    if not raw:
        raise DurableMembershipError("node_membership_state_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping) or canonical_json_bytes(dict(value)) != raw:
            raise DurableMembershipError("node_membership_state_invalid")
    except DurableMembershipError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DurableMembershipError("node_membership_state_invalid") from exc
    return value


def _win32_save_membership_state(state_root: Path, raw: bytes, filename: str = FILENAME) -> None:
    """CREATE_NEW + flush + MoveFileExW(WRITE_THROUGH) + directory flush."""

    try:
        with mycelium_host_platform.private_directory(state_root, create=False) as root:
            if not root.exists:
                raise DurableMembershipError("node_membership_state_write_failed")
            root.atomic_replace(filename, raw)
    except HostPlatformError as exc:
        raise DurableMembershipError("node_membership_state_write_failed") from exc


def load_membership_state(state_root: Path) -> dict[str, Any] | None:
    """Load an exact owner-only state file; absence means first enrollment."""

    if mycelium_host_platform.is_win32():
        return _win32_load_membership_state(state_root)
    value = _load_owner_json(state_root, FILENAME)
    return None if value is None else _validate(value)


def _load_owner_json(state_root: Path, filename: str) -> Mapping[str, Any] | None:
    if mycelium_host_platform.is_win32():
        return _win32_load_owner_json(state_root, filename)
    path = state_root / filename
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DurableMembershipError("node_membership_state_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_BYTES
    ):
        raise DurableMembershipError("node_membership_state_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise DurableMembershipError("node_membership_state_invalid")
            raw = os.read(descriptor, _MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except DurableMembershipError:
        raise
    except OSError as exc:
        raise DurableMembershipError("node_membership_state_unavailable") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping) or canonical_json_bytes(dict(value)) != raw:
            raise DurableMembershipError("node_membership_state_invalid")
    except DurableMembershipError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DurableMembershipError("node_membership_state_invalid") from exc
    return value


def save_membership_state(state_root: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically replace the public restart credential with owner-only durability."""

    validated = _validate(value)
    raw = canonical_json_bytes(validated)
    _save_owner_bytes(state_root, FILENAME, raw)
    loaded = load_membership_state(state_root)
    if loaded != validated:
        raise DurableMembershipError("node_membership_state_write_failed")
    return validated


def _save_owner_bytes(state_root: Path, filename: str, raw: bytes) -> None:
    if mycelium_host_platform.is_win32():
        _win32_save_membership_state(state_root, raw, filename)
        return
    path = state_root / filename
    temporary = state_root / f".{filename}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count <= 0:
                raise OSError("short membership state write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        parent = os.open(state_root, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError as exc:
        raise DurableMembershipError("node_membership_state_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


#: A9: the member's migrated v2 peer-profile declaration, re-signed for every
#: later join or resume once the seed's membership contract is v2.
PROFILE_TEMPLATE_FILENAME = "peer_profile.v2.json"
PROFILE_TEMPLATE_PROTOCOL = "mycelium.membership.peer_profile.v2"


def load_v2_profile_template(state_root: Path) -> dict[str, Any] | None:
    """The owner-only v2 profile template, or ``None`` while the member is v1."""

    value = _load_owner_json(state_root, PROFILE_TEMPLATE_FILENAME)
    if value is None:
        return None
    if (
        set(value) != {"protocol", "profile"}
        or value["protocol"] != PROFILE_TEMPLATE_PROTOCOL
        or not isinstance(value["profile"], Mapping)
    ):
        raise DurableMembershipError("node_profile_template_invalid")
    return dict(value)


def save_v2_profile_template(state_root: Path, profile: Mapping[str, Any]) -> dict[str, Any]:
    document = {"protocol": PROFILE_TEMPLATE_PROTOCOL, "profile": dict(profile)}
    _save_owner_bytes(state_root, PROFILE_TEMPLATE_FILENAME, canonical_json_bytes(document))
    if load_v2_profile_template(state_root) != json.loads(canonical_json_bytes(document)):
        raise DurableMembershipError("node_membership_state_write_failed")
    return document


def next_incarnation(base: str, restart_count: int) -> str:
    """Return a stable bounded incarnation for the next persisted restart."""

    if (
        not isinstance(base, str)
        or _SEGMENT_RE.fullmatch(base) is None
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 1
        or not math.isfinite(float(restart_count))
    ):
        raise DurableMembershipError("node_membership_state_invalid")
    suffix = f".r{restart_count}"
    candidate = base + suffix
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    return f"{base[:110]}.{digest}"


__all__ = [
    "DurableMembershipError",
    "FILENAME",
    "PROTOCOL",
    "load_membership_state",
    "next_incarnation",
    "save_membership_state",
    "PROFILE_TEMPLATE_FILENAME",
    "load_v2_profile_template",
    "save_v2_profile_template",
]
