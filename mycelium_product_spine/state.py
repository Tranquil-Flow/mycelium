# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-only atomic persistence for the bounded product event replay window."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping, Sequence

from .contracts import validate_product_event


STATE_PROTOCOL = "mycelium.product_evidence_state.v1"
STATE_FILE = "product-evidence-state.v1.json"
MAX_STATE_BYTES = 32 * 1024 * 1024
_LEGACY_DEVICE_ATTRIBUTE_FIELDS = frozenset(
    {
        "peer_class",
        "membership_generation",
        "incarnation",
        "lifecycle",
        "lease_freshness",
        "runtime_backend",
        "transport",
        "activation_protocol",
        "activation_eligible",
        "placement_id",
    }
)


class ProductEvidenceStateError(RuntimeError):
    """Stable state error that never embeds filesystem or evidence contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _encoded(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProductEvidenceStateError("product_evidence_state_invalid") from exc


def _upgrade_generation_one_event(value: object) -> object:
    """Upgrade only the exact pre-authority-generation device shape."""

    try:
        detached = json.loads(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
    except (TypeError, ValueError, RecursionError):
        return value
    if not isinstance(detached, dict):
        return detached
    snapshot = detached.get("snapshot")
    entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if not isinstance(entities, list):
        return detached
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("kind") != "device":
            continue
        attributes = entity.get("attributes")
        if (
            isinstance(attributes, dict)
            and set(attributes) == _LEGACY_DEVICE_ATTRIBUTE_FIELDS
        ):
            attributes["authority_generation"] = 1
    return detached


class ProductEvidenceStateStore:
    """Persist privacy-reduced events under an already-created private root."""

    def __init__(self, root: str | Path, *, replay_limit: int) -> None:
        path = Path(root).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self._root = Path(os.path.abspath(path))
        self._path = self._root / STATE_FILE
        self._replay_limit = replay_limit
        self._root_identity = self._validate_root()

    @property
    def path(self) -> Path:
        return self._path

    def _validate_root(self) -> tuple[int, int]:
        try:
            metadata = self._root.lstat()
        except OSError as exc:
            raise ProductEvidenceStateError("product_evidence_state_root_invalid") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ProductEvidenceStateError("product_evidence_state_root_invalid")
        return metadata.st_dev, metadata.st_ino

    def _revalidate_root(self) -> None:
        if self._validate_root() != self._root_identity:
            raise ProductEvidenceStateError("product_evidence_state_root_changed")

    def load(self) -> list[dict[str, Any]]:
        self._revalidate_root()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ProductEvidenceStateError("product_evidence_state_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ProductEvidenceStateError("product_evidence_state_unavailable")
            raw = b""
            while len(raw) <= MAX_STATE_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_STATE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(descriptor)
        if len(raw) > MAX_STATE_BYTES:
            raise ProductEvidenceStateError("product_evidence_state_too_large")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProductEvidenceStateError("product_evidence_state_corrupt") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"protocol", "replay_limit", "events"}
            or document.get("protocol") != STATE_PROTOCOL
            or document.get("replay_limit") != self._replay_limit
            or not isinstance(document.get("events"), list)
            or len(document["events"]) > self._replay_limit
        ):
            raise ProductEvidenceStateError("product_evidence_state_corrupt")
        try:
            events = [
                validate_product_event(_upgrade_generation_one_event(event))
                for event in document["events"]
            ]
        except (TypeError, ValueError) as exc:
            raise ProductEvidenceStateError("product_evidence_state_corrupt") from exc
        if any(
            current["cursor"] != previous["cursor"] + 1
            for previous, current in zip(events, events[1:])
        ):
            raise ProductEvidenceStateError("product_evidence_state_corrupt")
        self._revalidate_root()
        return events

    def write(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        retained = [validate_product_event(event) for event in events][-self._replay_limit :]
        while True:
            document = {
                "protocol": STATE_PROTOCOL,
                "replay_limit": self._replay_limit,
                "events": retained,
            }
            payload = _encoded(document)
            if len(payload) <= MAX_STATE_BYTES:
                break
            if len(retained) <= 1:
                raise ProductEvidenceStateError("product_evidence_state_too_large")
            retained = retained[1:]
        self._revalidate_root()
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{STATE_FILE}.",
                suffix=".tmp",
                dir=self._root,
            )
            temporary = Path(raw_path)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._revalidate_root()
            os.replace(temporary, self._path)
            temporary = None
            os.chmod(self._path, 0o600, follow_symlinks=False)
            directory = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self._revalidate_root()
        except ProductEvidenceStateError:
            raise
        except OSError as exc:
            raise ProductEvidenceStateError("product_evidence_state_write_failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return retained


__all__ = [
    "MAX_STATE_BYTES",
    "ProductEvidenceStateError",
    "ProductEvidenceStateStore",
    "STATE_FILE",
    "STATE_PROTOCOL",
]
