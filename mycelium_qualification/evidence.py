"""Immutable canonical evidence files and SHA-256 manifest validation."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

EVIDENCE_MANIFEST_PROTOCOL = "mycelium.route_qualification_evidence_manifest.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_FIELDS = frozenset(
    {
        "protocol",
        "run_id",
        "evidence_class",
        "file_count",
        "total_size_bytes",
        "files",
    }
)
_ENTRY_FIELDS = frozenset({"path", "size_bytes", "sha256"})


class EvidenceValidationError(ValueError):
    """Fail-closed immutable-evidence error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise EvidenceValidationError(code, detail)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one value using RouteQualificationV1 canonical JSON."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("noncanonical_json", str(exc)) from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceValidationError("duplicate_json_key", key)
        result[key] = value
    return result


def canonical_json_loads(content: bytes, *, path: str) -> Any:
    """Parse bytes only when they are valid, duplicate-free canonical JSON."""
    _require(isinstance(content, bytes), "invalid_evidence_bytes", path)
    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvidenceValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceValidationError("invalid_evidence_json", path) from exc
    _require(canonical_json_bytes(value) == content, "noncanonical_evidence_json", path)
    return value


def sha256_bytes(content: bytes) -> str:
    _require(isinstance(content, bytes), "invalid_evidence_bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def sha256_document(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def is_sha256_ref(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _validate_path(path: Any) -> str:
    _require(isinstance(path, str) and path != "", "unsafe_evidence_path", str(path))
    _require(path == path.strip(), "unsafe_evidence_path", path)
    _require(not path.startswith("/"), "unsafe_evidence_path", path)
    _require("\\" not in path and "//" not in path, "unsafe_evidence_path", path)
    segments = path.split("/")
    _require(
        all(segment not in {"", ".", ".."} for segment in segments),
        "unsafe_evidence_path",
        path,
    )
    _require(not any(ord(character) < 32 for character in path), "unsafe_evidence_path", path)
    return path


def build_evidence_manifest(
    *, run_id: str, evidence_class: str, files: Mapping[str, bytes]
) -> dict[str, Any]:
    """Build a self-excluding manifest over every supplied evidence file."""
    _require(isinstance(run_id, str) and bool(run_id.strip()), "invalid_evidence_run_id")
    _require(
        evidence_class in {"physical_qualification", "synthetic_test_fixture"},
        "invalid_evidence_class",
    )
    _require(isinstance(files, Mapping) and bool(files), "empty_evidence_manifest")
    entries: list[dict[str, Any]] = []
    total_size = 0
    for raw_path in sorted(files):
        path = _validate_path(raw_path)
        content = files[raw_path]
        _require(isinstance(content, bytes), "invalid_evidence_bytes", path)
        total_size += len(content)
        entries.append(
            {
                "path": path,
                "size_bytes": len(content),
                "sha256": sha256_bytes(content),
            }
        )
    return {
        "protocol": EVIDENCE_MANIFEST_PROTOCOL,
        "run_id": run_id,
        "evidence_class": evidence_class,
        "file_count": len(entries),
        "total_size_bytes": total_size,
        "files": entries,
    }


def evidence_manifest_digest(manifest: Mapping[str, Any]) -> str:
    _require(isinstance(manifest, Mapping), "invalid_evidence_manifest")
    return sha256_document(dict(manifest))


def validate_evidence_manifest(
    manifest: Mapping[str, Any], files: Mapping[str, bytes]
) -> str:
    """Validate exact file coverage, canonical ordering, sizes and SHA-256 hashes."""
    _require(isinstance(manifest, Mapping), "invalid_evidence_manifest")
    document = dict(manifest)
    missing = _MANIFEST_FIELDS - set(document)
    unknown = set(document) - _MANIFEST_FIELDS
    _require(not missing, "missing_evidence_manifest_field", ",".join(sorted(missing)))
    _require(not unknown, "unknown_evidence_manifest_field", ",".join(sorted(unknown)))
    _require(document["protocol"] == EVIDENCE_MANIFEST_PROTOCOL, "unsupported_evidence_manifest_protocol")
    _require(
        isinstance(document["run_id"], str) and bool(document["run_id"].strip()),
        "invalid_evidence_run_id",
    )
    _require(
        document["evidence_class"] in {"physical_qualification", "synthetic_test_fixture"},
        "invalid_evidence_class",
    )
    entries = document["files"]
    _require(isinstance(entries, list) and bool(entries), "empty_evidence_manifest")
    _require(isinstance(files, Mapping), "invalid_evidence_files")

    indexed: dict[str, dict[str, Any]] = {}
    observed_order: list[str] = []
    for entry in entries:
        _require(isinstance(entry, Mapping), "invalid_evidence_manifest_entry")
        entry_document = dict(entry)
        _require(
            set(entry_document) == _ENTRY_FIELDS,
            "invalid_evidence_manifest_entry",
        )
        path = _validate_path(entry_document["path"])
        _require(path not in indexed, "duplicate_evidence_path", path)
        size = entry_document["size_bytes"]
        _require(
            isinstance(size, int) and not isinstance(size, bool) and size >= 0,
            "invalid_evidence_file_size",
            path,
        )
        _require(is_sha256_ref(entry_document["sha256"]), "invalid_evidence_file_digest", path)
        indexed[path] = entry_document
        observed_order.append(path)

    _require(observed_order == sorted(observed_order), "evidence_manifest_not_canonical")
    _require(
        isinstance(document["file_count"], int)
        and not isinstance(document["file_count"], bool)
        and document["file_count"] == len(entries),
        "evidence_file_count_mismatch",
    )
    expected_total = sum(entry["size_bytes"] for entry in indexed.values())
    _require(
        isinstance(document["total_size_bytes"], int)
        and not isinstance(document["total_size_bytes"], bool)
        and document["total_size_bytes"] == expected_total,
        "evidence_total_size_mismatch",
    )
    actual_paths: set[str] = set()
    for raw_path, content in files.items():
        path = _validate_path(raw_path)
        _require(isinstance(content, bytes), "invalid_evidence_bytes", path)
        actual_paths.add(path)
    _require(actual_paths == set(indexed), "evidence_manifest_file_set_mismatch")
    for path, entry in indexed.items():
        content = files[path]
        _require(len(content) == entry["size_bytes"], "evidence_file_size_mismatch", path)
        _require(sha256_bytes(content) == entry["sha256"], "evidence_file_digest_mismatch", path)
    canonical_json_bytes(document)
    return evidence_manifest_digest(document)
