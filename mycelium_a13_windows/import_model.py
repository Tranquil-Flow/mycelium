# SPDX-License-Identifier: AGPL-3.0-only
"""Import a LAN- or USB-supplied model snapshot into an acquisition root.

  python -m mycelium_a13_windows.import_model <snapshot-dir> --state-root <acquisition-root>

``<snapshot-dir>`` holds the model files (for example a copy of
``models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/7ae5576...``; Hugging Face cache
symlinks are read through). ``<acquisition-root>`` is the directory the A13 owner
app acquires into: ``<app-root>/model-source-<commit[:12]>`` for a non-default
run model (``<peer-state>/serving`` when that app runs in target role).  A
managed host (``TargetApplication``) does not acquire and needs no import.

The files land in the acquisition's own staging directory
(``models--<id>/snapshots/.<commit>.acquiring``), every directory created as an
owner-only protected-DACL private directory through ``mycelium_host_platform``,
so the acquisition accepts them without a download (a plain ``mkdir`` or
``robocopy`` tree is refused as ``artifact_storage_unsafe``). Every file is
hashed while it is copied and must match the pinned digest and size; any
mismatch, missing or unexpected file refuses, and nothing unverified is left
under its final name. The acquisition still resolves the repository metadata
and re-verifies every digest itself before it prepares the source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any

import mycelium_host_platform

from mycelium_a13_macos.serving import A9_ROUTE_MODEL, REGISTERED_MODELS, ModelContract

PROTOCOL = "mycelium.a13_windows_model_import.v1"
_CHUNK = 1024 * 1024

#: Pinned per-file sha256 and size for each importable model.  Provenance: the
#: wp8-01 owner acquisition snapshot of A9_ROUTE_MODEL, which the A13 acquisition
#: verified against the Hugging Face repository manifest (status total
#: 999,604,126 B); re-hashed 2026-10-07.
PINNED_SNAPSHOTS: dict[tuple[str, str], tuple[tuple[str, int, str], ...]] = {
    (A9_ROUTE_MODEL.model_id, A9_ROUTE_MODEL.resolved_commit): (
        (".gitattributes", 1519, "11ad7efa24975ee4b0c3c3a38ed18737f0658a5f75a0a96787b576a78a023361"),
        ("LICENSE", 11343, "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"),
        ("README.md", 4917, "b19c806a904db6dc878a0462e70b551f6b7ac78dfbb88c2eb966ca2b9109ae15"),
        ("config.json", 659, "18e18afcaccafade98daf13a54092927904649e1dd4eba8299ab717d5d94ff45"),
        ("generation_config.json", 242, "e558847a8b4402616f1273797b015104dc266fe4b520056fca88823ba8f8ebe6"),
        ("merges.txt", 1671839, "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"),
        ("model.safetensors", 988097824, "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe"),
        ("tokenizer.json", 7031645, "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"),
        ("tokenizer_config.json", 7305, "5b5d4f65d0acd3b2d56a35b56d374a36cbc1c8fa5cf3b3febbbfabf22f359583"),
        ("vocab.json", 2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
    ),
}


class ModelImportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise ModelImportError(code)


def pinned_files(model: ModelContract) -> tuple[tuple[str, int, str], ...]:
    files = PINNED_SNAPSHOTS.get((model.model_id, model.resolved_commit))
    if files is None:
        _refuse("model_import_manifest_unavailable")
    return files


def stage_directory(state_root: Path, model: ModelContract) -> Path:
    model_root = state_root / ("models--" + model.model_id.replace("/", "--"))
    return model_root / "snapshots" / ("." + model.resolved_commit + ".acquiring")


def _private(path: Path) -> None:
    try:
        with mycelium_host_platform.private_directory(path, create=True):
            pass
    except mycelium_host_platform.HostPlatformError:
        _refuse("import_target_unsafe")


def _source_files(source: Path) -> set[str]:
    found: set[str] = set()
    for root, directories, files in os.walk(source, followlinks=False):
        for directory in directories:
            if (Path(root) / directory).is_symlink():
                _refuse("import_source_unexpected_file")
        for name in files:
            found.add((Path(root) / name).relative_to(source).as_posix())
    return found


def _copy_verified(source: Path, target: Path, *, size: int, digest: str) -> None:
    partial = target.with_name(target.name + ".partial")
    if partial.exists() or partial.is_symlink():
        _refuse("import_target_not_clean")
    descriptor = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    hasher = hashlib.sha256()
    copied = 0
    try:
        with os.fdopen(descriptor, "wb") as out, open(source, "rb") as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                _refuse("import_source_invalid")
            while block := src.read(_CHUNK):
                copied += len(block)
                if copied > size:
                    _refuse("import_size_mismatch")
                hasher.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if copied != size:
            _refuse("import_size_mismatch")
        if hasher.hexdigest() != digest:
            _refuse("import_digest_mismatch")
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _verify_existing(path: Path, *, size: int, digest: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
        _refuse("import_target_mismatch")
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(_CHUNK):
            hasher.update(block)
    if hasher.hexdigest() != digest:
        _refuse("import_target_mismatch")


def import_snapshot(source: str | os.PathLike[str], state_root: str | os.PathLike[str], *,
                    model: ModelContract = A9_ROUTE_MODEL) -> dict[str, Any]:
    """Copy and verify one pinned snapshot into the acquisition's staging directory."""

    if model not in REGISTERED_MODELS:
        _refuse("model_not_registered")
    files = pinned_files(model)
    source_root = Path(source).expanduser().absolute()
    root = Path(state_root).expanduser().absolute()
    if not source_root.is_dir():
        _refuse("import_source_invalid")
    expected = {path for path, _size, _digest in files}
    present = _source_files(source_root)
    if present - expected:
        _refuse("import_source_unexpected_file")
    if expected - present:
        _refuse("import_source_incomplete")
    for path, size, _digest in files:
        if (source_root / path).stat().st_size != size:  # cheap check before any byte is copied
            _refuse("import_size_mismatch")
    _private(root)
    try:
        lock = mycelium_host_platform.hold_exclusive_lock(root, "acquisition.lock", blocking=False)
    except mycelium_host_platform.HostPlatformError as exc:
        _refuse("acquisition_in_progress" if exc.code == "host_lock_busy" else "import_target_unsafe")
    try:
        stage = stage_directory(root, model)
        if (stage.parent / model.resolved_commit).exists():
            _refuse("snapshot_already_present")
        for directory in (stage.parent.parent, stage.parent, stage):
            _private(directory)
        imported = kept = 0
        for path, size, digest in files:
            relative = PurePosixPath(path)
            parent = stage
            for part in relative.parts[:-1]:
                parent = parent / part
                _private(parent)
            target = parent / relative.name
            if target.exists() or target.is_symlink():
                _verify_existing(target, size=size, digest=digest)
                kept += 1
                continue
            _copy_verified(source_root.joinpath(*relative.parts), target, size=size, digest=digest)
            imported += 1
    finally:
        lock.release()
    return {
        "protocol": PROTOCOL,
        "model_id": model.model_id,
        "resolved_commit": model.resolved_commit,
        "stage": str(stage),
        "files_imported": imported,
        "files_already_verified": kept,
        "total_bytes": sum(size for _path, size, _digest in files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mycelium_a13_windows.import_model",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="directory holding the model snapshot files")
    parser.add_argument("--state-root", required=True, help="the acquisition root to import into")
    parser.add_argument("--model", default=A9_ROUTE_MODEL.resolved_commit,
                        help="resolved commit of the registered model (default: the A9 route model)")
    args = parser.parse_args(argv)
    model = next((m for m in REGISTERED_MODELS if m.resolved_commit == args.model), None)
    try:
        if model is None:
            _refuse("model_not_registered")
        result = import_snapshot(args.source, args.state_root, model=model)
    except ModelImportError as exc:
        print(json.dumps({"protocol": PROTOCOL, "refused": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
