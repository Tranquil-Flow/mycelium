# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned stage/cleanup for a native Windows managed host.

The macOS target (``mycelium_a13_macos.managed_staging.TargetStaging``) runs
the controller's canonical POSIX staging script in a subprocess.  That script
relies on ``os.geteuid``, POSIX modes and a POSIX staging root, none of which
exist on Windows.  This adapter performs the same contract in process:

* the same closed archive rules (<= 256 regular files, sorted unique relative
  POSIX names, digest-pinned bytes), the same preposition document, the same
  ``.mycelium-stage-in-progress.json`` journal and ``.mycelium-stage.json``
  marker, and byte-identical acknowledgements
  (``mycelium.controller_remote_stage_ack.v1`` /
  ``mycelium.controller_remote_cleanup_ack.v1``);
* the physical root lives beneath the host's owner-only private state root
  (``mycelium_host_platform`` private directories: a protected DACL inherited
  by everything created below it), and every path component is refused if it
  is a symlink or a junction;
* the exclusive stage lock is ``LockFileEx`` through the host-platform seam;
* the acknowledged ``staging_root`` is a *logical* POSIX path
  (:func:`logical_staging_root`), which is what the controller's
  ``PeerIdentity`` admits; the physical path never leaves the host.

The adapter is portable on purpose: the same code is tested on POSIX, and the
lock and private-directory seams on the fake win32 API.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
from typing import Any

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError

from mycelium_a13_macos.authority import DemoAuthorityError, _private_directory
from mycelium_a13_macos.managed_staging import StagingError as _TargetStagingError

STAGE_ACK = "mycelium.controller_remote_stage_ack.v1"
CLEANUP_ACK = "mycelium.controller_remote_cleanup_ack.v1"
PREPOSITION = "mycelium.controller_prepositioned_member_artifacts.v1"
MARKER = ".mycelium-stage.json"
JOURNAL = ".mycelium-stage-in-progress.json"
LOCK = "stage.lock"
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


class StagingError(_TargetStagingError):
    """Same type the macOS target raises, so shared callers handle both."""


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _pin(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise StagingError("stage_digest_invalid")
    return value


def logical_staging_root(node_id: str, run_id: str) -> str:
    """The POSIX staging root the controller sees for one managed Windows run."""

    for value in (node_id, run_id):
        if not isinstance(value, str) or _NODE_RE.fullmatch(value) is None:
            raise StagingError("stage_configuration_invalid")
    return f"/mycelium-managed/{node_id}/{run_id}/mycelium-stage"


def _validated_logical_root(value: Any) -> str:
    """The exact rule of ``physical_inference_qualification.PeerIdentity``."""

    if not isinstance(value, str):
        raise StagingError("stage_configuration_invalid")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) < 4
        or not any(part.startswith("mycelium") for part in path.parts)
    ):
        raise StagingError("stage_configuration_invalid")
    return value


def _relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str):
        raise StagingError("stage_path_invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(
            part in (".", "..") or part.startswith(".mycelium") or part == ".incoming.tar"
            or ":" in part or "\\" in part
            for part in path.parts
        )
    ):
        raise StagingError("stage_path_invalid")
    return path


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse)


def _regular_beneath(root: Path, path: Path) -> os.stat_result:
    """A single-link regular file below ``root`` with no link anywhere on the way."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StagingError("stage_file_unsafe") from None
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise StagingError("stage_file_unsafe")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise StagingError("stage_file_unsafe")
    return metadata


class WindowsTargetStaging:
    """The Windows managed host's stage/cleanup adapter (target side)."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        node_id: str,
        run_id: str,
        max_archive_bytes: int,
        max_disk_bytes: int,
        current_member: Callable[[], Any],
        authorize: Callable[[Mapping[str, Any]], Any],
        artifact_cache_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if (
            not isinstance(node_id, str)
            or _NODE_RE.fullmatch(node_id) is None
            or any(type(n) is not int or n <= 0 for n in (max_archive_bytes, max_disk_bytes))
            or max_archive_bytes > 64 * 1024 * 1024
            or not callable(current_member)
            or not callable(authorize)
        ):
            raise StagingError("stage_configuration_invalid")
        try:
            self.root = _private_directory(Path(state_root), create=True)
            managed = _private_directory(self.root / "managed", create=True)
            self.run_root = _private_directory(managed / run_id, create=True)
        except (DemoAuthorityError, OSError):
            raise StagingError("stage_configuration_invalid") from None
        self.staging_root = self.run_root / "mycelium-stage"
        self.logical_root = _validated_logical_root(logical_staging_root(node_id, run_id))
        self.node_id = node_id
        self.max_archive_bytes = max_archive_bytes
        self.max_disk_bytes = max_disk_bytes
        self.current_member = current_member
        self.authorize = authorize
        self.cache = None
        if artifact_cache_root is not None:
            cache = Path(artifact_cache_root).absolute()
            if _is_link(cache) or not cache.is_dir() or cache.resolve() != cache:
                raise StagingError("stage_cache_invalid")
            self.cache = cache

    # -- helpers --------------------------------------------------------------

    def _lock(self):
        try:
            return mycelium_host_platform.hold_exclusive_lock(self.run_root, LOCK, blocking=False)
        except HostPlatformError as exc:
            if exc.code == "host_lock_busy":
                raise StagingError("stage_busy") from None
            raise StagingError("stage_lock_invalid") from None

    def _admit(self, intent: Mapping[str, Any]) -> None:
        try:
            accepted = self.current_member() is True and self.authorize(dict(intent)) is True
        except Exception:  # noqa: BLE001 - authorization fails closed
            accepted = False
        if not accepted:
            raise StagingError("stage_not_authorized")

    def _marker(self, path: Path) -> dict[str, Any]:
        try:
            metadata = _regular_beneath(self.run_root, path)
            if not 0 < metadata.st_size <= 1024:
                raise StagingError("stage_marker_invalid")
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict) or _canonical(value) != raw:
                raise StagingError("stage_marker_invalid")
            return value
        except StagingError as exc:
            if str(exc) == "stage_file_unsafe":
                raise StagingError("stage_marker_invalid") from None
            raise
        except (OSError, ValueError, UnicodeError):
            raise StagingError("stage_marker_invalid") from None

    def _files(self, archive: bytes, preposition: Mapping[str, Any] | None):
        files: dict[str, tuple[int, str]] = {}
        sources: dict[str, Path] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                members = tar.getmembers()
                names = [member.name for member in members]
                if not members or len(members) > 256 or names != sorted(set(names)):
                    raise StagingError("stage_archive_invalid")
                for member in members:
                    _relative(member.name)
                    if not member.isfile() or member.size < 0 or member.size > self.max_disk_bytes:
                        raise StagingError("stage_archive_invalid")
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise StagingError("stage_archive_invalid")
                    data = handle.read(member.size + 1)
                    if len(data) != member.size:
                        raise StagingError("stage_archive_invalid")
                    files[member.name] = (member.size, _digest(data))
        except (tarfile.TarError, OSError) as exc:
            raise StagingError("stage_archive_invalid") from exc
        encoded = b""
        if preposition is not None:
            try:
                encoded = _canonical(preposition)
                if (
                    len(encoded) > 1024 * 1024
                    or set(preposition) != {"protocol", "files"}
                    or preposition["protocol"] != PREPOSITION
                ):
                    raise ValueError()
                records = preposition["files"]
                if not isinstance(records, list) or len(records) > 256 or self.cache is None:
                    raise ValueError()
                destinations = []
                for row in records:
                    if set(row) != {"content_digest", "destination_path", "size_bytes", "source_path"}:
                        raise ValueError()
                    relative = str(_relative(row["destination_path"]))
                    source = Path(row["source_path"])
                    if not source.is_absolute() or source.resolve() != source:
                        raise ValueError()
                    metadata = _regular_beneath(self.cache, source)
                    size = row["size_bytes"]
                    if type(size) is not int or size < 0 or size != metadata.st_size or relative in files:
                        raise ValueError()
                    files[relative] = (size, _pin(row["content_digest"]))
                    sources[relative] = source
                    destinations.append(relative)
                if destinations != sorted(set(destinations)):
                    raise ValueError()
            except (TypeError, ValueError, OSError, KeyError, StagingError):
                raise StagingError("stage_preposition_invalid") from None
        if len(archive) + sum(size for size, _ in files.values()) > self.max_disk_bytes:
            raise StagingError("stage_disk_limit_exceeded")
        return files, encoded, sources

    def _verify(self, files: Mapping[str, tuple[int, str]]) -> None:
        try:
            observed = set()
            for root, dirs, names in os.walk(self.staging_root, followlinks=False):
                if any(_is_link(Path(root) / name) for name in dirs):
                    raise StagingError("stage_file_unsafe")
                for name in names:
                    path = Path(root) / name
                    if path == self.staging_root / MARKER:
                        continue
                    observed.add(path.relative_to(self.staging_root).as_posix())
            if observed != set(files):
                raise StagingError("stage_content_mismatch")
            for name, (size, digest) in files.items():
                path = self.staging_root.joinpath(*PurePosixPath(name).parts)
                if _regular_beneath(self.staging_root, path).st_size != size:
                    raise StagingError("stage_content_mismatch")
                value = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(_CHUNK):
                        value.update(chunk)
                if "sha256:" + value.hexdigest() != digest:
                    raise StagingError("stage_content_mismatch")
        except OSError:
            raise StagingError("stage_content_mismatch") from None

    @staticmethod
    def _write_new(path: Path, chunks) -> str:
        """Create ``path`` exclusively, stream ``chunks`` and fsync; returns the digest."""

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        digest = hashlib.sha256()
        descriptor = os.open(path, flags, 0o600)
        try:
            for chunk in chunks:
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                digest.update(chunk)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return "sha256:" + digest.hexdigest()

    def _extract(self, archive: bytes, files, sources, marker: Mapping[str, Any]) -> None:
        """The canonical stage script's steps, in process, beneath the run root."""

        root = self.staging_root
        root.mkdir(exist_ok=False)
        if _is_link(root):
            raise StagingError("stage_file_unsafe")
        self._write_new(root / JOURNAL, [_canonical(dict(marker))])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                relative = _relative(member.name)
                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                for parent in destination.relative_to(root).parents:
                    if str(parent) != "." and _is_link(root / parent):
                        raise StagingError("stage_file_unsafe")
                source = tar.extractfile(member)
                if source is None:
                    raise StagingError("stage_archive_invalid")
                actual = self._write_new(destination, iter(lambda: source.read(_CHUNK), b""))
                if actual != files[member.name][1]:
                    raise StagingError("stage_content_mismatch")
        for relative, source_path in sources.items():
            destination = root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            size, digest = files[relative]
            with source_path.open("rb") as source:
                actual = self._write_new(destination, iter(lambda: source.read(_CHUNK), b""))
            if actual != digest or destination.stat().st_size != size:
                raise StagingError("stage_preposition_invalid")
        self._write_new(root / MARKER, [_canonical(dict(marker))])
        (root / JOURNAL).unlink()

    # -- the target staging contract ----------------------------------------

    def prepare(self, *, archive: bytes, archive_digest: str, preposition: Mapping[str, Any] | None = None):
        if not isinstance(archive, bytes) or not 0 < len(archive) <= self.max_archive_bytes:
            raise StagingError("stage_archive_limit_exceeded")
        if _digest(archive) != _pin(archive_digest):
            raise StagingError("stage_archive_digest_mismatch")
        files, encoded, sources = self._files(archive, preposition)
        marker = {"node_id": self.node_id, "archive_digest": archive_digest}
        ack = {
            "protocol": STAGE_ACK,
            **marker,
            "staging_root": self.logical_root,
            "archive_size_bytes": len(archive),
        }
        if preposition is not None:
            digest = _digest(encoded)
            marker["preposition_digest"] = digest
            ack.update(preposition_digest=digest, preposition_size_bytes=len(encoded))
        lock = self._lock()
        try:
            self._admit(
                {
                    "operation": "prepare",
                    **marker,
                    "archive_size_bytes": len(archive),
                    "preposition_size_bytes": len(encoded),
                    "staged_bytes": sum(size for size, _ in files.values()),
                }
            )
            if os.path.lexists(self.staging_root):
                if _is_link(self.staging_root) or self._marker(self.staging_root / MARKER) != marker:
                    raise StagingError("stage_existing_binding_mismatch")
                self._verify(files)
                return ack
            needed = len(archive) + len(encoded) + sum(size for size, _ in files.values())
            if shutil.disk_usage(self.run_root).free < needed:
                raise StagingError("stage_disk_unavailable")
            try:
                self._extract(archive, files, sources, marker)
            except BaseException:
                shutil.rmtree(self.staging_root, ignore_errors=True)
                raise
            self._verify(files)
            return ack
        except OSError:
            raise StagingError("stage_operation_rejected") from None
        finally:
            lock.release()

    def verify_prepared(self, *, archive: bytes, archive_digest: str, preposition: Mapping[str, Any] | None = None):
        if (
            not isinstance(archive, bytes)
            or not 0 < len(archive) <= self.max_archive_bytes
            or _digest(archive) != _pin(archive_digest)
        ):
            raise StagingError("stage_archive_digest_mismatch")
        files, encoded, _sources = self._files(archive, preposition)
        marker = {"node_id": self.node_id, "archive_digest": archive_digest}
        if preposition is not None:
            marker["preposition_digest"] = _digest(encoded)
        lock = self._lock()
        try:
            if _is_link(self.staging_root) or self._marker(self.staging_root / MARKER) != marker:
                raise StagingError("stage_existing_binding_mismatch")
            self._verify(files)
        finally:
            lock.release()
        return marker

    def cleanup(self, *, archive_digest: str, preposition_digest: str | None = None):
        marker = {"node_id": self.node_id, "archive_digest": _pin(archive_digest)}
        if preposition_digest is not None:
            marker["preposition_digest"] = _pin(preposition_digest)
        lock = self._lock()
        try:
            try:
                permitted = self.authorize({"operation": "cleanup", **marker}) is True
            except Exception:  # noqa: BLE001 - authorization fails closed
                permitted = False
            if not permitted:
                raise StagingError("stage_not_authorized")
            removed = False
            if os.path.lexists(self.staging_root):
                if _is_link(self.staging_root):
                    raise StagingError("stage_cleanup_binding_mismatch")
                path = self.staging_root / MARKER
                if not path.exists():
                    path = self.staging_root / JOURNAL
                if self._marker(path) != marker:
                    raise StagingError("stage_cleanup_binding_mismatch")
                shutil.rmtree(self.staging_root)
                removed = True
            return {
                "protocol": CLEANUP_ACK,
                "node_id": self.node_id,
                "staging_root": self.logical_root,
                "removed": removed,
            }
        except OSError:
            raise StagingError("stage_operation_rejected") from None
        finally:
            lock.release()


__all__ = [
    "CLEANUP_ACK",
    "STAGE_ACK",
    "StagingError",
    "WindowsTargetStaging",
    "logical_staging_root",
]
