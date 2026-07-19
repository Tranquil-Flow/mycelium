"""Create-new, immutable filesystem sealing for physical route evidence."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any

from .contracts import RouteQualificationV1
from .evidence import (
    EvidenceValidationError,
    build_evidence_manifest,
    canonical_json_bytes,
    canonical_json_loads,
    evidence_manifest_digest,
    validate_evidence_manifest,
)
from .qualifier import qualify_route

EVIDENCE_MANIFEST_NAME = "evidence-manifest.json"
MAX_SEALED_FILE_BYTES = 1_048_576
MAX_SEALED_TOTAL_BYTES = 67_108_864
REQUIRED_AUTHORITY_DOCUMENTS = frozenset(
    {
        "qualification/source-provenance.json",
        "model/model-manifest.json",
        "control/control-plane-tranche.json",
        "control/gossip-signature.json",
        "runtime/provisioning-reports.json",
        "runtime/load-proofs.json",
        "runtime/load-proof-signatures.json",
        "router/execution-graph.json",
        "run/route-challenge.json",
        "run/negative-runs.json",
    }
)


class EvidenceSealingError(ValueError):
    """Fail-closed filesystem evidence error carrying a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class SealedEvidence:
    """Read-only locator and immutable manifest identity for one sealed tree."""

    root: Path
    manifest_digest: str
    file_count: int
    total_size_bytes: int


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise EvidenceSealingError(code, detail)


def _wrap_validation(exc: EvidenceValidationError) -> EvidenceSealingError:
    return EvidenceSealingError(exc.code, exc.detail)


def _write_new_file(path: Path, content: bytes) -> None:
    """Create one file, flush its bytes, then remove all write permission."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise EvidenceSealingError("evidence_path_exists", path.as_posix()) from exc
    except OSError as exc:
        raise EvidenceSealingError("evidence_write_failed", path.as_posix()) from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o400, follow_symlinks=False)
    except OSError as exc:
        raise EvidenceSealingError("evidence_write_failed", path.as_posix()) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise EvidenceSealingError("evidence_directory_sync_failed", path.as_posix()) from exc


def _make_parent_directories(root: Path, relative_path: str) -> None:
    current = root
    for component in Path(relative_path).parts[:-1]:
        current /= component
        if current.is_symlink():
            raise EvidenceSealingError("sealed_tree_symlink", current.as_posix())
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            _require(current.is_dir(), "evidence_parent_not_directory", current.as_posix())
        except OSError as exc:
            raise EvidenceSealingError(
                "evidence_directory_create_failed", current.as_posix()
            ) from exc


def _render_files(
    *, documents: Mapping[str, Any], extra_files: Mapping[str, bytes]
) -> dict[str, bytes]:
    _require(isinstance(documents, Mapping), "invalid_authority_documents")
    observed = set(documents)
    missing = REQUIRED_AUTHORITY_DOCUMENTS - observed
    unknown = observed - REQUIRED_AUTHORITY_DOCUMENTS
    _require(not missing, "missing_authority_document", ",".join(sorted(missing)))
    _require(not unknown, "unknown_authority_document", ",".join(sorted(unknown)))
    _require(isinstance(extra_files, Mapping), "invalid_extra_evidence_files")

    rendered: dict[str, bytes] = {}
    try:
        for path in sorted(REQUIRED_AUTHORITY_DOCUMENTS):
            rendered[path] = canonical_json_bytes(documents[path])
    except EvidenceValidationError as exc:
        raise _wrap_validation(exc) from exc

    for path, content in extra_files.items():
        _require(isinstance(path, str), "unsafe_evidence_path", str(path))
        _require(path not in rendered, "duplicate_evidence_path", path)
        _require(path != EVIDENCE_MANIFEST_NAME, "reserved_evidence_path", path)
        _require(type(content) is bytes, "invalid_evidence_bytes", path)
        if path.startswith("observations/"):
            try:
                canonical_json_loads(content, path=path)
            except EvidenceValidationError as exc:
                raise _wrap_validation(exc) from exc
        rendered[path] = bytes(content)

    for path, content in rendered.items():
        _require(
            len(content) <= MAX_SEALED_FILE_BYTES,
            "evidence_file_too_large",
            path,
        )
    _require(
        sum(len(content) for content in rendered.values()) <= MAX_SEALED_TOTAL_BYTES,
        "evidence_tree_too_large",
    )
    return rendered


def seal_physical_evidence(
    *,
    output_dir: str | os.PathLike[str],
    run_id: str,
    documents: Mapping[str, Any],
    extra_files: Mapping[str, bytes] | None = None,
) -> SealedEvidence:
    """Canonicalize and durably seal one create-new physical evidence tree."""
    _require(
        isinstance(run_id, str) and run_id == run_id.strip() and bool(run_id),
        "invalid_evidence_run_id",
    )
    challenge = (
        documents.get("run/route-challenge.json")
        if isinstance(documents, Mapping)
        else None
    )
    if not isinstance(challenge, Mapping):
        raise EvidenceSealingError(
            "missing_authority_document", "run/route-challenge.json"
        )
    _require(
        challenge.get("run_id") == run_id
        and challenge.get("evidence_class") == "physical_qualification",
        "nonphysical_evidence",
    )
    files = _render_files(
        documents=documents,
        extra_files={} if extra_files is None else extra_files,
    )
    try:
        manifest = build_evidence_manifest(
            run_id=run_id,
            evidence_class="physical_qualification",
            files=files,
        )
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_digest = evidence_manifest_digest(manifest)
    except EvidenceValidationError as exc:
        raise _wrap_validation(exc) from exc

    try:
        root = Path(os.path.abspath(os.fspath(output_dir)))
    except TypeError as exc:
        raise EvidenceSealingError("invalid_evidence_output") from exc
    _require(root.name not in {"", ".", ".."}, "invalid_evidence_output")
    if root.is_symlink():
        raise EvidenceSealingError("evidence_output_symlink", root.as_posix())
    _require(not root.exists(), "evidence_output_exists", root.as_posix())
    _require(
        root.parent.exists() and root.parent.is_dir(),
        "evidence_output_parent_missing",
        root.parent.as_posix(),
    )
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise EvidenceSealingError("evidence_output_exists", root.as_posix()) from exc
    except OSError as exc:
        raise EvidenceSealingError("evidence_output_create_failed", root.as_posix()) from exc

    for relative_path in sorted(files):
        _make_parent_directories(root, relative_path)
        _write_new_file(root / relative_path, files[relative_path])

    # Every evidence byte and containing directory reaches stable storage before
    # the self-excluding manifest appears. Its presence therefore marks a fully
    # written candidate, never an in-progress tree.
    directories = sorted(
        {path for path in root.rglob("*") if path.is_dir()} | {root},
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        _fsync_directory(directory)
    _write_new_file(root / EVIDENCE_MANIFEST_NAME, manifest_bytes)
    _fsync_directory(root)

    for directory in directories:
        try:
            os.chmod(directory, 0o500, follow_symlinks=False)
        except OSError as exc:
            raise EvidenceSealingError(
                "evidence_permission_lock_failed", directory.as_posix()
            ) from exc
    _fsync_directory(root)
    return SealedEvidence(
        root=root,
        manifest_digest=manifest_digest,
        file_count=int(manifest["file_count"]),
        total_size_bytes=int(manifest["total_size_bytes"]),
    )


def _read_sealed_evidence(
    sealed: SealedEvidence | str | os.PathLike[str],
) -> tuple[dict[str, bytes], dict[str, Any]]:
    if isinstance(sealed, SealedEvidence):
        descriptor: SealedEvidence | None = sealed
        root = sealed.root
    else:
        descriptor = None
        root = Path(sealed)
    _require(not root.is_symlink(), "sealed_tree_symlink", root.as_posix())
    _require(root.is_dir(), "sealed_tree_missing", root.as_posix())

    observed_files: set[str] = set()
    observed_directories: list[Path] = [root]
    for current_raw, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_raw)
        for name in directory_names:
            path = current / name
            _require(not path.is_symlink(), "sealed_tree_symlink", path.as_posix())
            observed_directories.append(path)
        for name in file_names:
            path = current / name
            _require(not path.is_symlink(), "sealed_tree_symlink", path.as_posix())
            observed_files.add(path.relative_to(root).as_posix())

    manifest_path = root / EVIDENCE_MANIFEST_NAME
    _require(EVIDENCE_MANIFEST_NAME in observed_files, "sealed_manifest_missing")
    try:
        manifest_value = canonical_json_loads(
            manifest_path.read_bytes(), path=EVIDENCE_MANIFEST_NAME
        )
    except (OSError, EvidenceValidationError) as exc:
        if isinstance(exc, EvidenceValidationError):
            raise _wrap_validation(exc) from exc
        raise EvidenceSealingError("sealed_manifest_read_failed") from exc
    _require(isinstance(manifest_value, dict), "invalid_evidence_manifest")
    entries = manifest_value.get("files")
    _require(isinstance(entries, list), "invalid_evidence_manifest")
    expected_paths: set[str] = set()
    for entry in entries:
        _require(isinstance(entry, Mapping), "invalid_evidence_manifest_entry")
        entry_path = entry.get("path")
        _require(isinstance(entry_path, str), "invalid_evidence_manifest_entry")
        expected_paths.add(entry_path)
    _require(
        observed_files == expected_paths | {EVIDENCE_MANIFEST_NAME},
        "sealed_file_set_mismatch",
    )

    files: dict[str, bytes] = {}
    try:
        for path in sorted(expected_paths):
            files[path] = (root / path).read_bytes()
        digest = validate_evidence_manifest(manifest_value, files)
    except EvidenceValidationError as exc:
        raise _wrap_validation(exc) from exc
    except OSError as exc:
        raise EvidenceSealingError("sealed_evidence_read_failed") from exc

    if descriptor is not None:
        _require(digest == descriptor.manifest_digest, "sealed_manifest_identity_mismatch")
        _require(
            manifest_value.get("file_count") == descriptor.file_count
            and manifest_value.get("total_size_bytes") == descriptor.total_size_bytes,
            "sealed_manifest_identity_mismatch",
        )

    for directory in observed_directories:
        _require(
            stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) & 0o222 == 0,
            "sealed_tree_mutable",
            directory.as_posix(),
        )
    for relative_path in observed_files:
        path = root / relative_path
        _require(
            stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o222 == 0,
            "sealed_tree_mutable",
            relative_path,
        )
    return files, manifest_value


def qualify_sealed_evidence(
    sealed: SealedEvidence | str | os.PathLike[str],
    *,
    now_unix_ms: int,
    verify_gossip_signature: Callable[[bytes, dict[str, Any]], bool],
    verify_load_proof_signature: Callable[[bytes, dict[str, Any]], bool],
) -> RouteQualificationV1:
    """Reopen, rehash, and qualify one sealed tree through the sole authority."""
    files, manifest = _read_sealed_evidence(sealed)
    return qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=now_unix_ms,
        verify_gossip_signature=verify_gossip_signature,
        verify_load_proof_signature=verify_load_proof_signature,
    )
