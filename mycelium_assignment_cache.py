"""Verified content-addressed cache and assignment-local materialization."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import time
from typing import Any, Iterator, Mapping


PROTOCOL = "mycelium.assignment_artifact_cache.v1"
MATERIALIZATION_PROTOCOL = "mycelium.assignment_materialization.v1"


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1:
            raise ValueError("cache source must be a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("cache source changed during verification")
        return "sha256:" + digest.hexdigest(), size
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ArtifactObjectKey:
    model_revision: str
    manifest_digest: str
    format: str
    quantization: str
    tensor_digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        for field in ("manifest_digest", "tensor_digest"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value.startswith("sha256:")
                or len(value) != 71
                or any(character not in "0123456789abcdef" for character in value[7:])
            ):
                raise ValueError(f"{field} must be a lowercase SHA-256 reference")
        for field in ("model_revision", "format", "quantization"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"{field} must be non-empty and bounded")
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("size_bytes must be a positive exact integer")

    @property
    def object_id(self) -> str:
        payload = _canonical(asdict(self))
        return "sha256:" + hashlib.sha256(payload).hexdigest()


class AssignmentArtifactCache:
    """One process-safe cache; it has storage authority but no route authority."""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = Path(root)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("cache max_bytes must be a positive exact integer")
        self.max_bytes = max_bytes
        if self.root.exists() and self.root.is_symlink():
            raise ValueError("cache root may not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        (self.root / "objects").mkdir(mode=0o700, exist_ok=True)
        (self.root / "quarantine").mkdir(mode=0o700, exist_ok=True)
        self._lock_path = self.root / ".cache.lock"
        self._index_path = self.root / "index.v1.json"
        with self._locked():
            if not self._index_path.exists():
                self._write_index(self._empty_index())
            else:
                self._read_index()

    def _empty_index(self) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "max_bytes": self.max_bytes, "entries": {}}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_index(self) -> dict[str, Any]:
        if self._index_path.is_symlink() or self._index_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("cache index is unsafe")
        try:
            value = json.loads(self._index_path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("cache index is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol", "max_bytes", "entries"}
            or value["protocol"] != PROTOCOL
            or value["max_bytes"] != self.max_bytes
            or not isinstance(value["entries"], dict)
        ):
            raise ValueError("cache index is invalid")
        return value

    def _write_index(self, value: Mapping[str, Any]) -> None:
        temporary = self.root / f".index.{os.getpid()}.{time.time_ns()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(_canonical(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._index_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _object_path(self, object_id: str) -> Path:
        return self.root / "objects" / object_id[7:]

    def _verify_object(self, path: Path, key: ArtifactObjectKey) -> bool:
        try:
            digest, size = _digest_file(path)
        except (OSError, ValueError):
            return False
        return digest == key.tensor_digest and size == key.size_bytes

    def store(self, key: ArtifactObjectKey, source: Path) -> str:
        source = Path(source)
        source_digest, source_size = _digest_file(source)
        if source_digest != key.tensor_digest or source_size != key.size_bytes:
            raise ValueError("cache source does not match object key")
        with self._locked():
            index = self._read_index()
            object_id = key.object_id
            target = self._object_path(object_id)
            entry = index["entries"].get(object_id)
            if entry is not None and self._verify_object(target, key):
                entry["last_access_unix_ns"] = time.time_ns()
                self._write_index(index)
                return "reused"
            repaired = entry is not None or target.exists()
            if target.exists():
                quarantine = self.root / "quarantine" / f"{object_id[7:]}.{time.time_ns()}"
                os.replace(target, quarantine)
            temporary = self.root / "objects" / f".{object_id[7:]}.{os.getpid()}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    descriptor = -1
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if not self._verify_object(temporary, key):
                    raise ValueError("cache object changed during population")
                os.chmod(temporary, 0o400)
                os.replace(temporary, target)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
            index["entries"][object_id] = {
                "key": asdict(key),
                "relative_path": f"objects/{object_id[7:]}",
                "size_bytes": key.size_bytes,
                "last_access_unix_ns": time.time_ns(),
                "pins": sorted(entry.get("pins", [])) if isinstance(entry, dict) else [],
            }
            self._write_index(index)
            self._evict_locked(index)
            return "repaired" if repaired else "populated"

    @staticmethod
    def _safe_relative_path(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if (
            not isinstance(value, str)
            or not value
            or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("assignment artifact path is unsafe")
        return path

    def materialize_assignment(
        self,
        *,
        assignment_id: str,
        required_objects: Mapping[str, ArtifactObjectKey],
        destination: Path,
    ) -> dict[str, Any]:
        if not isinstance(assignment_id, str) or not assignment_id:
            raise ValueError("assignment_id must be non-empty")
        if not isinstance(required_objects, Mapping) or not required_objects:
            raise ValueError("assignment requires at least one object")
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError("assignment destination must not exist")
        relative_objects = {
            self._safe_relative_path(path): key for path, key in required_objects.items()
        }
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        if temporary.exists():
            raise ValueError("assignment temporary destination already exists")
        with self._locked():
            index = self._read_index()
            opened: list[str] = []
            temporary.mkdir(parents=True, mode=0o700)
            try:
                for relative, key in sorted(
                    relative_objects.items(), key=lambda item: item[0].as_posix()
                ):
                    entry = index["entries"].get(key.object_id)
                    source = self._object_path(key.object_id)
                    if entry is None or not self._verify_object(source, key):
                        raise ValueError("assignment cache object is missing or corrupt")
                    target = temporary.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, target)
                    os.chmod(target, 0o400)
                    pins = set(entry.get("pins", []))
                    pins.add(assignment_id)
                    entry["pins"] = sorted(pins)
                    entry["last_access_unix_ns"] = time.time_ns()
                    opened.append(key.object_id)
                manifest = {
                    "protocol": MATERIALIZATION_PROTOCOL,
                    "assignment_id": assignment_id,
                    "objects": [
                        {
                            "relative_path": relative.as_posix(),
                            "object_id": key.object_id,
                            "tensor_digest": key.tensor_digest,
                            "size_bytes": key.size_bytes,
                        }
                        for relative, key in sorted(
                            relative_objects.items(), key=lambda item: item[0].as_posix()
                        )
                    ],
                    "opened_object_ids": opened,
                    "cache_entry_count": len(index["entries"]),
                    "unassigned_object_count": len(index["entries"]) - len(opened),
                    "route_ready": False,
                }
                manifest_path = temporary / "assignment-materialization.json"
                manifest_path.write_bytes(_canonical(manifest))
                os.chmod(manifest_path, 0o400)
                os.replace(temporary, destination)
                self._write_index(index)
                return manifest
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    def unpin_assignment(self, assignment_id: str) -> None:
        with self._locked():
            index = self._read_index()
            for entry in index["entries"].values():
                entry["pins"] = [pin for pin in entry.get("pins", []) if pin != assignment_id]
            self._write_index(index)

    def _evict_locked(self, index: dict[str, Any]) -> tuple[str, ...]:
        total = sum(entry["size_bytes"] for entry in index["entries"].values())
        evicted: list[str] = []
        candidates = sorted(
            (
                (object_id, entry)
                for object_id, entry in index["entries"].items()
                if not entry.get("pins")
            ),
            key=lambda item: (item[1]["last_access_unix_ns"], item[0]),
        )
        for object_id, entry in candidates:
            if total <= self.max_bytes:
                break
            self._object_path(object_id).unlink(missing_ok=True)
            total -= entry["size_bytes"]
            index["entries"].pop(object_id)
            evicted.append(object_id)
        self._write_index(index)
        return tuple(evicted)

    def evict(self) -> tuple[str, ...]:
        with self._locked():
            return self._evict_locked(self._read_index())

    def status(self) -> dict[str, Any]:
        with self._locked():
            index = self._read_index()
            return {
                "protocol": PROTOCOL,
                "max_bytes": self.max_bytes,
                "entry_count": len(index["entries"]),
                "used_bytes": sum(
                    entry["size_bytes"] for entry in index["entries"].values()
                ),
                "pinned_object_count": sum(
                    bool(entry.get("pins")) for entry in index["entries"].values()
                ),
            }


def validate_cache_status(document: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(document, Mapping)
        or set(document)
        != {"protocol", "max_bytes", "entry_count", "used_bytes", "pinned_object_count"}
        or document.get("protocol") != PROTOCOL
    ):
        raise ValueError("assignment cache status is invalid")
    for field in ("max_bytes", "entry_count", "used_bytes", "pinned_object_count"):
        if type(document.get(field)) is not int or document[field] < 0:
            raise ValueError("assignment cache status is invalid")
    if (
        document["max_bytes"] <= 0
        or document["used_bytes"] > document["max_bytes"]
        or document["pinned_object_count"] > document["entry_count"]
    ):
        raise ValueError("assignment cache status is invalid")
    return json.loads(json.dumps(document))


def validate_materialization_report(document: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "protocol", "assignment_id", "objects", "opened_object_ids",
        "cache_entry_count", "unassigned_object_count", "route_ready",
    }
    if (
        not isinstance(document, Mapping)
        or set(document) != expected
        or document.get("protocol") != MATERIALIZATION_PROTOCOL
        or document.get("route_ready") is not False
        or not isinstance(document.get("assignment_id"), str)
        or not document["assignment_id"]
        or not isinstance(document.get("objects"), list)
        or not document["objects"]
        or not isinstance(document.get("opened_object_ids"), list)
    ):
        raise ValueError("assignment materialization report is invalid")
    object_ids = []
    for item in document["objects"]:
        if not isinstance(item, Mapping) or set(item) != {
            "relative_path", "object_id", "tensor_digest", "size_bytes"
        }:
            raise ValueError("assignment materialization report is invalid")
        AssignmentArtifactCache._safe_relative_path(item["relative_path"])
        for field in ("object_id", "tensor_digest"):
            value = item[field]
            if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
                raise ValueError("assignment materialization report is invalid")
        if type(item["size_bytes"]) is not int or item["size_bytes"] <= 0:
            raise ValueError("assignment materialization report is invalid")
        object_ids.append(item["object_id"])
    if document["opened_object_ids"] != object_ids or len(set(object_ids)) != len(object_ids):
        raise ValueError("assignment materialization report is invalid")
    for field in ("cache_entry_count", "unassigned_object_count"):
        if type(document.get(field)) is not int or document[field] < 0:
            raise ValueError("assignment materialization report is invalid")
    if document["cache_entry_count"] != len(object_ids) + document["unassigned_object_count"]:
        raise ValueError("assignment materialization report is invalid")
    return json.loads(json.dumps(document))


__all__ = [
    "ArtifactObjectKey",
    "AssignmentArtifactCache",
    "MATERIALIZATION_PROTOCOL",
    "PROTOCOL",
    "validate_cache_status",
    "validate_materialization_report",
]
