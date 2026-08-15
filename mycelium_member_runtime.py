"""Deterministic least-authority runtime closure for member artifact acquisition."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping
import uuid


RUNTIME_MANIFEST_PROTOCOL = "mycelium.member_runtime_closure_manifest.v1"
RUNTIME_PATHS = (
    "mycelium_live/__init__.py",
    "mycelium_live/artifact_provisioner.py",
    "mycelium_live/artifact_transport.py",
    "mycelium_live/member_artifact_provisioner.py",
    "mycelium_node/__init__.py",
    "mycelium_node/identity.py",
    "mycelium_qualification/__init__.py",
    "mycelium_qualification/evidence.py",
    "mycelium_qualification/signing.py",
    "mycelium_swarm_artifacts.py",
)
_PACKAGE_MARKER = (
    b'"""Least-authority package marker for member artifact acquisition."""\n'
)
_GENERATED_FILES: Mapping[str, bytes] = {
    "mycelium_node/__init__.py": _PACKAGE_MARKER,
    "mycelium_qualification/__init__.py": _PACKAGE_MARKER,
}


class MemberRuntimeError(RuntimeError):
    """Stable member-runtime build failure."""


def _canonical(value: object) -> bytes:
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


def _private_directory(
    path: Path, *, create: bool = False, private: bool = True
) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise MemberRuntimeError("member_runtime_root_unsafe")
    if create:
        candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MemberRuntimeError("member_runtime_root_unsafe") from exc
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & (0o077 if private else 0o022)
    ):
        raise MemberRuntimeError("member_runtime_root_unsafe")
    return candidate


def _atomic_file(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


def _source_bytes(repo_root: Path, relative: str) -> bytes:
    if relative in _GENERATED_FILES:
        return _GENERATED_FILES[relative]
    source = repo_root.joinpath(*PurePosixPath(relative).parts)
    try:
        metadata = source.lstat()
        payload = source.read_bytes()
    except OSError as exc:
        raise MemberRuntimeError("member_runtime_source_missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise MemberRuntimeError("member_runtime_source_unsafe")
    return payload


def build_member_runtime(
    *, repo_root: Path, output_root: Path, manifest_path: Path
) -> dict[str, Any]:
    repo = _private_directory(Path(repo_root), private=False)
    output = _private_directory(Path(output_root), create=True)
    if not Path(manifest_path).is_absolute() or Path(manifest_path).parent != output:
        raise MemberRuntimeError("member_runtime_manifest_path_unsafe")
    records: list[dict[str, Any]] = []
    for relative in RUNTIME_PATHS:
        payload = _source_bytes(repo, relative)
        destination = output.joinpath(*PurePosixPath(relative).parts)
        _atomic_file(destination, payload, 0o400)
        records.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        "protocol": RUNTIME_MANIFEST_PROTOCOL,
        "files": records,
    }
    _atomic_file(Path(manifest_path), _canonical(manifest), 0o600)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import mycelium_live.member_artifact_provisioner as member; "
                "assert callable(member.main)"
            ),
        ],
        cwd=tempfile.gettempdir(),
        env={**os.environ, "PYTHONPATH": str(output), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout or probe.stderr:
        raise MemberRuntimeError("member_runtime_import_probe_failed")
    return manifest


__all__ = [
    "MemberRuntimeError",
    "RUNTIME_MANIFEST_PROTOCOL",
    "RUNTIME_PATHS",
    "build_member_runtime",
]
