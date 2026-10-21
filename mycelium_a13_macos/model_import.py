# SPDX-License-Identifier: AGPL-3.0-only
"""Install a model from a local folder (USB stick, shared drive) into the cache.

The folder is untrusted.  Every file is checked against the pinned manifest
(exact path set, exact size, exact digest) before anything is written, and the
copy is hashed again on the way in, so a file that changes during the import is
also refused.  Any problem refuses the whole import, names every offending
file in plain English and leaves the cache untouched.  Links are never followed.
The installed snapshot uses the layout of ``SelectedModelAcquisition``, so the
next download of this model finds it and fetches nothing.  Files are installed
read-only.  Nothing here opens a network connection.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .acquisition import (
    AcquisitionError,
    _fsync_directory,
    _private_root,
    _secure_directory,
    _validate_manifest,
)
from .serving import ModelContract

_BLOCK = 1024 * 1024
_MESSAGES = {
    "import_hash_mismatch": "{path}: this file does not match the pinned model "
                            "(it is damaged or a different version).",
    "import_size_mismatch": "{path}: its size is {actual} bytes but the pinned model expects "
                            "{expected} bytes.",
    "import_file_missing": "{path}: this file is missing from the folder.",
    "import_unexpected_file": "{path}: this file is not part of the pinned model, so it was "
                              "not imported.",
    "import_link_refused": "{path}: links are not accepted; copy the real file into the folder.",
}


class ModelImportError(RuntimeError):
    """A refused import: ``code`` is stable, ``str(error)`` is plain English."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _hasher(record: Mapping[str, Any], size: int) -> Any:
    if record["digest"]["algorithm"] == "sha256":
        return hashlib.sha256()
    return hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0")  # noqa: S324 - git blob id


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, metadata


def _digest_matches(path: Path, record: Mapping[str, Any]) -> bool:
    try:
        descriptor, metadata = _open_regular(path)
    except OSError:
        return False
    digest = _hasher(record, metadata.st_size)
    with os.fdopen(descriptor, "rb") as handle:
        while block := handle.read(_BLOCK):
            digest.update(block)
    return digest.hexdigest() == record["digest"]["value"]


def _problems(folder: Path, files: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for walk_root, directories, names in os.walk(folder, followlinks=False):
        base = Path(walk_root)
        for directory in sorted(directories):
            path = base / directory
            if path.is_symlink():
                found.append(("import_link_refused", path.relative_to(folder).as_posix(), {}))
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(folder).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                found.append(("import_link_refused", relative, {}))
                continue
            record = files.get(relative)
            if record is None or not stat.S_ISREG(metadata.st_mode):
                found.append(("import_unexpected_file", relative, {}))
                continue
            seen.add(relative)
            if metadata.st_size != record["size_bytes"]:
                found.append(("import_size_mismatch", relative,
                              {"actual": metadata.st_size, "expected": record["size_bytes"]}))
            elif not _digest_matches(path, record):
                found.append(("import_hash_mismatch", relative, {}))
    for relative in sorted(set(files) - seen):
        if not any(item[1] == relative for item in found):
            found.append(("import_file_missing", relative, {}))
    return found


def _refuse(model: ModelContract, problems: list[tuple[str, str, dict[str, Any]]]) -> None:
    lines = [_MESSAGES[code].format(path=path, **detail) for code, path, detail in problems]
    codes = {code for code, _, _ in problems}
    header = (f"Nothing was imported: this folder is not the pinned model {model.model_id} "
              f"at {model.resolved_commit[:12]}.")
    code = codes.pop() if len(codes) == 1 else "import_folder_not_the_pinned_model"
    raise ModelImportError(code, "\n".join([header, *lines]))


def _copy_verified(source: Path, target: Path, record: Mapping[str, Any]) -> None:
    descriptor, metadata = _open_regular(source)
    digest = _hasher(record, record["size_bytes"])
    written = 0
    with os.fdopen(descriptor, "rb") as reader:
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                      | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(out, "wb") as writer:
            while block := reader.read(_BLOCK):
                digest.update(block)
                writer.write(block)
                written += len(block)
            writer.flush()
            os.fsync(writer.fileno())
    if written != record["size_bytes"] or digest.hexdigest() != record["digest"]["value"]:
        raise ModelImportError(
            "import_hash_mismatch",
            "Nothing was imported: " + _MESSAGES["import_hash_mismatch"].format(
                path=record["path"]) + " It changed while it was being copied.")
    os.chmod(target, 0o400)


def import_model_folder(
    folder: str | Path,
    *,
    manifest: Mapping[str, Any],
    model: ModelContract,
    cache_root: str | Path,
) -> dict[str, Any]:
    """Verify ``folder`` against the pinned ``manifest`` and install it read-only."""
    if (not isinstance(manifest, Mapping)
            or manifest.get("model_id") != model.model_id
            or manifest.get("resolved_commit") != model.resolved_commit):
        raise ModelImportError(
            "model_manifest_commit_mismatch",
            f"Nothing was imported: the manifest is not for {model.model_id} at the pinned "
            f"commit {model.resolved_commit[:12]}.")
    try:
        checked = _validate_manifest(manifest, model=model)
    except AcquisitionError:
        raise ModelImportError("model_manifest_invalid",
                               "Nothing was imported: the manifest is damaged or not a "
                               "Mycelium model manifest.") from None
    source = Path(folder).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ModelImportError("import_folder_missing",
                               f"Nothing was imported: {source} is not a folder.")
    files = {record["path"]: record for record in checked["files"]}
    root = _private_root(cache_root)
    model_dir = root / ("models--" + model.model_id.replace("/", "--"))
    snapshot = model_dir / "snapshots" / model.resolved_commit
    if snapshot.exists() or snapshot.is_symlink():
        raise ModelImportError(
            "import_already_installed",
            f"Nothing was imported: this model is already in the cache at {snapshot}. "
            "Delete that folder first if you want to replace it.")
    problems = _problems(source, files)
    if problems:
        _refuse(model, problems)
    snapshots = _secure_directory(_secure_directory(model_dir) / "snapshots")
    stage = snapshots / ("." + model.resolved_commit + ".importing")
    if stage.exists() or stage.is_symlink():
        raise ModelImportError("import_cache_busy",
                               f"Nothing was imported: {stage} is left over from an earlier "
                               "import. Delete it and try again.")
    stage.mkdir(mode=0o700)
    try:
        for relative, record in files.items():
            parent = stage
            for part in PurePosixPath(relative).parts[:-1]:
                parent = _secure_directory(parent / part)
            _copy_verified(source.joinpath(*PurePosixPath(relative).parts),
                           parent / PurePosixPath(relative).name, record)
        os.replace(stage, snapshot)
        _fsync_directory(snapshots)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"snapshot": str(snapshot), "model_id": model.model_id,
            "resolved_commit": model.resolved_commit, "files": len(files),
            "bytes": checked["total_bytes"]}
