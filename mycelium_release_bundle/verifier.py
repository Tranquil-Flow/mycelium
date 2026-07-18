"""Deterministic, verification-only immutable bundle inspection."""
from __future__ import annotations

import hashlib
import ipaddress
import itertools
import json
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

MANIFEST_FILENAME = "release-evidence-manifest.json"
MANIFEST_PROTOCOL = "mycelium.immutable_release_evidence_bundle.v1"
VERIFICATION_PROTOCOL = "mycelium.release_evidence_bundle_verification.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILE_COUNT = 4096
REQUIRED_PHYSICAL_INPUTS = (
    "assignment",
    "dependency_lock",
    "deployment",
    "deployment_epoch",
    "endpoint_id",
    "model_manifest",
    "negative_run",
    "parity",
    "path",
    "qualification",
    "source_commit",
    "stage_load_proof",
    "transport",
)
CLAIM_BOUNDARY = (
    "static verification of declared immutable bundle structure, hashes, provenance, "
    "bindings, and gate presence only; qualification semantics are not evaluated and "
    "no route or release readiness is granted"
)
_CHECK_NAMES = (
    "bindings",
    "content_policy",
    "declared_gate_presence",
    "file_integrity",
    "file_inventory",
    "manifest_canonical",
    "manifest_digest",
    "path_policy",
)
_MANIFEST_FIELDS = frozenset({"body", "body_sha256", "protocol"})
_BODY_FIELDS = frozenset(
    {
        "bindings",
        "bundle_id",
        "declared_gates",
        "evidence_class",
        "file_count",
        "files",
        "synthetic_fixture",
        "total_size_bytes",
    }
)
_FILE_FIELDS = frozenset(
    {"media_type", "path", "sha256", "size_bytes", "synthetic_fixture"}
)
_ALLOWED_TOP_LEVEL = frozenset(
    {"control", "model", "provenance", "qualification", "release", "router", "run", "runtime"}
)
_ALLOWED_MEDIA_TYPES = frozenset(
    {"application/json", "application/vnd.mycelium.dependency-lock"}
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BUNDLE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_GATE_FIELDS = frozenset({"evidence_paths", "gate"})
_BINDING_FIELDS = frozenset({"expected", "json_pointer", "kind", "path"})
_DIGEST_BINDING_KINDS = frozenset(
    {
        "dependency_lock",
        "model_manifest",
        "negative_run",
        "parity",
        "qualification",
        "stage_load_proof",
        "transport",
    }
)
_STRING_BINDING_KINDS = frozenset(
    {"assignment", "deployment", "endpoint_id", "path"}
)
_FORBIDDEN_PATH_WORDS = frozenset(
    {
        "activation",
        "activations",
        "credential",
        "credentials",
        "password",
        "passwords",
        "prompt",
        "prompts",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_FORBIDDEN_JSON_KEYS = frozenset(
    {
        "access_token",
        "activation",
        "activations",
        "api_key",
        "authorization",
        "bearer_token",
        "credential",
        "credentials",
        "hidden_state",
        "hidden_states",
        "input_ids",
        "kv",
        "kv_cache",
        "output_token_ids",
        "password",
        "private_endpoint",
        "private_key",
        "prompt",
        "prompt_text",
        "prompt_token_ids",
        "prompts",
        "refresh_token",
        "runtime_endpoint",
        "secret",
        "token_ids",
    }
)
_PRIVATE_ENDPOINT_KEYS = frozenset(
    {"endpoint_address", "endpoint_url", "private_endpoint", "runtime_endpoint"}
)
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "activation",
        "activations",
        "credential",
        "credentials",
        "endpoint",
        "hidden",
        "kv",
        "password",
        "passwords",
        "prompt",
        "prompts",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_RAW_SENSITIVE_PATTERN = re.compile(
    rb"(?i)(?:private[ _-]+key|"
    rb"(?:api[ _-]?key|credential|password|secret|access[ _-]?token)\s*[:=]\s*\S{6,})"
)
_IPV4_CANDIDATE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_SYNTHETIC_CONTENT_MARKERS = (
    b"synthetic-test-fixture:",
    b'"evidence_class":"synthetic_test_fixture"',
    b'"synthetic_fixture":true',
)
_ALLOWED_ARTIFACT_FIELDS = frozenset(
    {
        # Immutable evidence-manifest fields.
        "file_count",
        "files",
        "path",
        "protocol",
        "run_id",
        "sha256",
        "size_bytes",
        "total_size_bytes",
        # Frozen RouteQualificationV1 fields and stage bindings.
        "assignment_id",
        "assignments_digest",
        "claim_boundary",
        "contract_manifest_digest",
        "dependency_lock_digests",
        "deployment_epoch",
        "deployment_id",
        "endpoint_id",
        "endpoint_set_digest",
        "environment_digest",
        "evidence_class",
        "evidence_manifest_digest",
        "execution_graph_digest",
        "execution_trace_digest",
        "gossip_signature_digest",
        "gossip_snapshot_digest",
        "issued_at_unix_ms",
        "kv_ownership_digest",
        "load_proof_digest",
        "load_proof_signatures_digest",
        "load_proofs_digest",
        "manifest_digest",
        "model_id",
        "negative_run_digest",
        "negative_runs_digest",
        "node_id",
        "numeric_parity_digest",
        "parity_digest",
        "path_id",
        "path_manifest_digest",
        "placement_id",
        "planner_snapshot_digest",
        "process_host_id",
        "process_id",
        "process_set_digest",
        "provisioning_reports_digest",
        "qualification_digest",
        "qualification_id",
        "qualified_by",
        "reason_codes",
        "release_ready",
        "reservation_id",
        "reservations_digest",
        "resolved_commit",
        "route_plan_digest",
        "route_ready",
        "source_commit",
        "source_manifest_digest",
        "source_provenance_digest",
        "stage",
        "stage_bindings",
        "stage_id",
        "stage_probe_result_digest",
        "stage_signature",
        "synthetic_fixture",
        "tensor_scope_digest",
        "timing_evidence_digest",
        "token_parity_digest",
        "topology_version",
        "transport_digest",
    }
)
_OPAQUE_IDENTIFIER_FIELDS = frozenset(
    {
        "assignment_id",
        "deployment_id",
        "endpoint_id",
        "model_id",
        "node_id",
        "path_id",
        "placement_id",
        "process_host_id",
        "protocol",
        "qualification_id",
        "qualified_by",
        "reservation_id",
        "run_id",
        "stage_id",
        "stage_signature",
    }
)
_CURRENT_CLAIM_BOUNDARIES = frozenset(
    {
        (
            "all RouteQualificationV1 identity, signed load proof, physical transport, "
            "timing, tensor scope, execution trace, token/output/numeric parity, local "
            "KV ownership, negative-run, provenance, and immutable evidence-manifest "
            "gates passed"
        ),
        (
            "synthetic_test_fixture schema shape only; no physical qualification and "
            "route_ready remains false"
        ),
    }
)

ReadObserver = Callable[[str], None]


class _VerificationFailure(ValueError):
    def __init__(self, code: str, subject: str = "bundle") -> None:
        self.code = code
        self.subject = subject
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON without trailing whitespace."""
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _VerificationFailure("noncanonical_json_value", "manifest") from exc


def sha256_bytes(value: bytes) -> str:
    if type(value) is not bytes:
        raise TypeError("sha256 input must be bytes")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_output(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _VerificationFailure("duplicate_json_key", "manifest")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _load_manifest(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _VerificationFailure as exc:
        if exc.code == "duplicate_json_key":
            raise _VerificationFailure("duplicate_manifest_json_key", "manifest") from exc
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _VerificationFailure("invalid_manifest_json", "manifest") from exc
    if not isinstance(document, dict):
        raise _VerificationFailure("invalid_manifest_document", "manifest")
    if canonical_json_bytes(document) != content:
        raise _VerificationFailure("noncanonical_manifest_json", "manifest")
    return document


def _empty_result() -> dict[str, Any]:
    return {
        "body_sha256": None,
        "checks": {name: False for name in _CHECK_NAMES},
        "claim_boundary": CLAIM_BOUNDARY,
        "evidence_class": None,
        "findings": [],
        "manifest_sha256": None,
        "missing_physical_inputs": list(REQUIRED_PHYSICAL_INPUTS),
        "observed": {
            "declared_bindings": 0,
            "declared_files": 0,
            "declared_gates": 0,
            "scanned_bytes": 0,
            "scanned_files": 0,
        },
        "ok": False,
        "physical_evidence_accepted": False,
        "physical_input_inventory_complete": False,
        "protocol": VERIFICATION_PROTOCOL,
        "qualification_evaluated": False,
        "release_ready": False,
        "route_ready": False,
        "synthetic_fixture": None,
        "verification_only": True,
    }


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_root(bundle_root: str | Path) -> tuple[Path, int, os.stat_result]:
    if not isinstance(bundle_root, (str, Path)):
        raise _VerificationFailure("bundle_unavailable")
    try:
        raw = os.fspath(bundle_root)
        if type(raw) is not str or not raw or "\0" in raw:
            raise _VerificationFailure("bundle_unavailable")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        named = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(named.st_mode):
            raise _VerificationFailure("symlink_bundle_root")
        descriptor = os.open(path, _directory_flags())
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(named):
            os.close(descriptor)
            raise _VerificationFailure("bundle_unavailable")
        return path, descriptor, opened
    except _VerificationFailure:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _VerificationFailure("bundle_unavailable") from exc


def _read_regular_file(
    root_fd: int,
    relative_path: str,
    *,
    maximum_bytes: int,
    subject: str,
    read_observer: ReadObserver | None,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    parts = PurePosixPath(relative_path).parts
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.dup(root_fd)
        for part in parts[:-1]:
            child_fd = os.open(part, _directory_flags(), dir_fd=directory_fd)
            child_opened = os.fstat(child_fd)
            child_named = os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child_opened.st_mode)
                or _identity(child_opened) != _identity(child_named)
            ):
                os.close(child_fd)
                raise _VerificationFailure("concurrently_changed_input", subject)
            os.close(directory_fd)
            directory_fd = child_fd

        before = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise _VerificationFailure("symlink_input", subject)
        if not stat.S_ISREG(before.st_mode):
            raise _VerificationFailure("nonregular_input", subject)
        if before.st_size > maximum_bytes:
            raise _VerificationFailure("oversized_input", subject)
        if before.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0:
            raise _VerificationFailure("unreadable_input", subject)

        file_fd = os.open(parts[-1], _file_flags(), dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise _VerificationFailure("concurrently_changed_input", subject)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            raise _VerificationFailure("oversized_input", subject)
        if read_observer is not None:
            read_observer(relative_path)
        after_open = os.fstat(file_fd)
        after_named = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        if _snapshot(before) != _snapshot(after_open) or _snapshot(before) != _snapshot(
            after_named
        ):
            raise _VerificationFailure("concurrently_changed_input", subject)
        return content, _snapshot(after_named)
    except _VerificationFailure:
        raise
    except FileNotFoundError as exc:
        raise _VerificationFailure("missing_input", subject) from exc
    except PermissionError as exc:
        raise _VerificationFailure("unreadable_input", subject) from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _VerificationFailure("unreadable_input", subject) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _inventory(
    root_fd: int,
    expected_files: set[str],
    expected_directories: set[str],
) -> tuple[
    dict[str, tuple[int, int, int, int, int, int]],
    dict[str, tuple[int, int, int, int, int, int]],
]:
    files: dict[str, tuple[int, int, int, int, int, int]] = {}
    directories: dict[str, tuple[int, int, int, int, int, int]] = {}
    maximum_entries = len(expected_files) + len(expected_directories)
    observed_entries = 0

    def walk(directory_fd: int, prefix: str) -> None:
        nonlocal observed_entries
        remaining = maximum_entries - observed_entries
        try:
            with os.scandir(directory_fd) as iterator:
                entries = list(itertools.islice(iterator, remaining + 1))
        except OSError as exc:
            raise _VerificationFailure("unreadable_input") from exc
        if len(entries) > remaining:
            raise _VerificationFailure("added_input")
        entries.sort(key=lambda item: item.name)
        for entry in entries:
            observed_entries += 1
            name = entry.name
            relative = f"{prefix}/{name}" if prefix else name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _VerificationFailure("unreadable_input") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise _VerificationFailure("symlink_input")
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in expected_directories:
                    raise _VerificationFailure("added_input")
                if metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0:
                    raise _VerificationFailure("unreadable_input")
                directories[relative] = _snapshot(metadata)
                try:
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                except OSError as exc:
                    raise _VerificationFailure("concurrently_changed_input") from exc
                try:
                    opened = os.fstat(child_fd)
                    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _snapshot(opened) != _snapshot(named):
                        raise _VerificationFailure("concurrently_changed_input")
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in expected_files:
                    raise _VerificationFailure("added_input")
                files[relative] = _snapshot(metadata)
            else:
                raise _VerificationFailure("nonregular_input")

    walk(root_fd, "")
    return files, directories


def _normalized_path(path: str) -> str:
    return unicodedata.normalize("NFC", posixpath.normpath(path))


def _validate_path_collisions(raw_paths: list[Any]) -> None:
    exact: set[str] = set()
    normalized: dict[str, str] = {}
    folded: dict[str, str] = {}
    for raw in raw_paths:
        if not isinstance(raw, str):
            continue
        if raw in exact:
            raise _VerificationFailure("duplicate_bundle_path", "manifest")
        exact.add(raw)
        normalized_path = _normalized_path(raw)
        prior_normalized = normalized.get(normalized_path)
        if prior_normalized is not None and prior_normalized != raw:
            raise _VerificationFailure("duplicate_normalized_path", "manifest")
        normalized[normalized_path] = raw
        folded_path = normalized_path.casefold()
        prior_folded = folded.get(folded_path)
        if prior_folded is not None and prior_folded != raw:
            raise _VerificationFailure("case_colliding_bundle_path", "manifest")
        folded[folded_path] = raw


def _validate_relative_path(value: Any, *, subject: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _VerificationFailure("unsafe_bundle_path", subject)
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or _WINDOWS_ABSOLUTE.match(value) is not None
        or "\\" in value
        or "//" in value
        or _normalized_path(value) != value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _VerificationFailure("unsafe_bundle_path", subject)
    if pure.parts[0] not in _ALLOWED_TOP_LEVEL:
        raise _VerificationFailure("path_not_allowlisted", subject)
    words = {
        word
        for component in pure.parts
        for word in re.split(r"[^a-z0-9]+", component.casefold())
        if word
    }
    if words & _FORBIDDEN_PATH_WORDS or {"private", "key"} <= words or {
        "private",
        "endpoint",
    } <= words or {"kv", "cache"} <= words:
        raise _VerificationFailure("forbidden_bundle_path", subject)
    return value


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _validate_body(body: Any, result: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or set(body) != _BODY_FIELDS:
        raise _VerificationFailure("invalid_manifest_body", "manifest")
    evidence_class = body["evidence_class"]
    synthetic_fixture = body["synthetic_fixture"]
    if evidence_class not in {"physical_qualification", "synthetic_test_fixture"}:
        raise _VerificationFailure("invalid_evidence_class", "manifest")
    if type(synthetic_fixture) is not bool or synthetic_fixture != (
        evidence_class == "synthetic_test_fixture"
    ):
        raise _VerificationFailure("invalid_synthetic_fixture_marker", "manifest")
    bundle_id = body["bundle_id"]
    if not isinstance(bundle_id, str) or _BUNDLE_ID.fullmatch(bundle_id) is None:
        raise _VerificationFailure("invalid_bundle_id", "manifest")
    if synthetic_fixture and not bundle_id.startswith("synthetic-test-fixture:"):
        raise _VerificationFailure("synthetic_fixture_not_prominent", "manifest")
    if not synthetic_fixture and bundle_id.startswith("synthetic-test-fixture:"):
        raise _VerificationFailure(
            "physical_bundle_contains_synthetic_marker", "manifest"
        )

    entries = body["files"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILE_COUNT:
        raise _VerificationFailure("invalid_file_inventory", "manifest")
    raw_paths = [entry.get("path") if isinstance(entry, Mapping) else None for entry in entries]
    _validate_path_collisions(raw_paths)

    validated: list[dict[str, Any]] = []
    for index, entry_value in enumerate(entries, start=1):
        subject = f"artifact:{index:04d}"
        if not isinstance(entry_value, dict) or set(entry_value) != _FILE_FIELDS:
            raise _VerificationFailure("invalid_file_entry", subject)
        path = _validate_relative_path(entry_value["path"], subject=subject)
        size = entry_value["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _VerificationFailure("invalid_file_size", subject)
        if size > MAX_FILE_BYTES:
            raise _VerificationFailure("oversized_input", subject)
        if not _is_sha256(entry_value["sha256"]):
            raise _VerificationFailure("invalid_file_digest", subject)
        if entry_value["media_type"] not in _ALLOWED_MEDIA_TYPES:
            raise _VerificationFailure("invalid_file_media_type", subject)
        if not synthetic_fixture:
            path_words = {
                word
                for component in PurePosixPath(path).parts
                for word in re.split(r"[^a-z0-9]+", component.casefold())
                if word
            }
            if "synthetic" in path_words:
                raise _VerificationFailure(
                    "physical_bundle_contains_synthetic_marker", subject
                )
            if entry_value["media_type"] != "application/json":
                raise _VerificationFailure("opaque_physical_input_forbidden", subject)
        if type(entry_value["synthetic_fixture"]) is not bool or entry_value[
            "synthetic_fixture"
        ] is not synthetic_fixture:
            raise _VerificationFailure("invalid_synthetic_fixture_marker", subject)
        if entry_value["media_type"] == "application/json" and not path.endswith(".json"):
            raise _VerificationFailure("invalid_file_media_type", subject)
        validated.append(dict(entry_value))

    paths = [entry["path"] for entry in validated]
    if paths != sorted(paths):
        raise _VerificationFailure("noncanonical_file_order", "manifest")
    file_count = body["file_count"]
    total_size = body["total_size_bytes"]
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(validated)
    ):
        raise _VerificationFailure("file_count_mismatch", "manifest")
    expected_total = sum(entry["size_bytes"] for entry in validated)
    if (
        not isinstance(total_size, int)
        or isinstance(total_size, bool)
        or total_size != expected_total
    ):
        raise _VerificationFailure("total_size_mismatch", "manifest")
    if total_size > MAX_TOTAL_BYTES:
        raise _VerificationFailure("oversized_bundle", "manifest")

    result["evidence_class"] = evidence_class
    result["synthetic_fixture"] = synthetic_fixture
    result["observed"]["declared_files"] = len(validated)
    return validated


def _load_artifact_json(content: bytes, subject: str) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _VerificationFailure("duplicate_artifact_json_key", subject)
            result[key] = value
        return result

    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=_reject_constant,
        )
    except _VerificationFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _VerificationFailure("invalid_artifact_json", subject) from exc
    if canonical_json_bytes(document) != content:
        raise _VerificationFailure("noncanonical_artifact_json", subject)
    return document


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _is_digest_material(value: Any) -> bool:
    if _is_sha256(value):
        return True
    return (
        isinstance(value, list)
        and bool(value)
        and all(_is_sha256(item) for item in value)
    )


def _contains_private_endpoint(value: str) -> bool:
    lowered = value.casefold().strip()
    host: str | None = None
    if "://" in lowered:
        try:
            host = urlsplit(lowered).hostname
        except ValueError:
            return True
    if host is not None:
        if host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_unspecified
            ):
                return True
    for candidate in _IPV4_CANDIDATE.findall(lowered):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            return True
    return False


def _is_opaque_identifier(value: Any, *, maximum_length: int = 512) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "://" in value
    ):
        return False
    candidate = value.removeprefix("[").removesuffix("]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return True
    return False


def _validate_sensitive_field(key: str, value: Any, subject: str) -> None:
    normalized = _normalized_key(key)
    tokens = set(normalized.split("_"))
    if normalized in _OPAQUE_IDENTIFIER_FIELDS:
        if not _is_opaque_identifier(value) or (
            normalized == "endpoint_id" and _contains_private_endpoint(value)
        ):
            raise _VerificationFailure("forbidden_bundle_content", subject)
        return
    if normalized == "reason_codes":
        if (
            not isinstance(value, list)
            or len(value) > 256
            or not all(isinstance(item, str) for item in value)
            or len(value) != len(set(value))
            or not all(_is_opaque_identifier(item, maximum_length=128) for item in value)
        ):
            raise _VerificationFailure("forbidden_bundle_content", subject)
        return
    if normalized == "claim_boundary":
        if value not in _CURRENT_CLAIM_BOUNDARIES:
            raise _VerificationFailure("forbidden_bundle_content", subject)
        return
    sensitive = (
        normalized in _FORBIDDEN_JSON_KEYS
        or normalized in _PRIVATE_ENDPOINT_KEYS
        or bool(tokens & _SENSITIVE_KEY_TOKENS)
    )
    if sensitive:
        if normalized.endswith(("_digest", "_digests")) and _is_digest_material(value):
            return
        raise _VerificationFailure("forbidden_bundle_content", subject)
    if normalized in {"address", "host", "hostname", "uri", "url"} and isinstance(
        value, str
    ) and _contains_private_endpoint(value):
        raise _VerificationFailure("forbidden_bundle_content", subject)


def _scan_sensitive_json(value: Any, *, subject: str, key_hint: str | None = None) -> None:
    if key_hint is not None:
        _validate_sensitive_field(key_hint, value, subject)
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_sensitive_field(key, child, subject)
            _scan_sensitive_json(child, subject=subject)
    elif isinstance(value, list):
        for child in value:
            _scan_sensitive_json(child, subject=subject)
    elif isinstance(value, str):
        if _RAW_SENSITIVE_PATTERN.search(value.encode("utf-8")) or _contains_private_endpoint(
            value
        ):
            raise _VerificationFailure("forbidden_bundle_content", subject)


def _validate_artifact_fields(value: Any, *, subject: str) -> None:
    """Reject unmodelled JSON fields that could become opaque payload channels."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized not in _ALLOWED_ARTIFACT_FIELDS and (
                not normalized.endswith(("_digest", "_digests"))
                or not _is_digest_material(child)
            ):
                raise _VerificationFailure("unsupported_artifact_field", subject)
            _validate_artifact_fields(child, subject=subject)
    elif isinstance(value, list):
        for child in value:
            _validate_artifact_fields(child, subject=subject)


def _scan_raw_content(content: bytes, subject: str) -> None:
    if _RAW_SENSITIVE_PATTERN.search(content) is not None:
        raise _VerificationFailure("forbidden_bundle_content", subject)


def _synthetic_acceptance_present(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("route_ready") is True or value.get("release_ready") is True:
            return True
        return any(_synthetic_acceptance_present(child) for child in value.values())
    if isinstance(value, list):
        return any(_synthetic_acceptance_present(child) for child in value)
    return False


def _parse_json_pointer(pointer: str, subject: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise _VerificationFailure("invalid_json_pointer", subject)
    decoded: list[str] = []
    for token in pointer[1:].split("/"):
        output: list[str] = []
        index = 0
        while index < len(token):
            character = token[index]
            if character != "~":
                output.append(character)
                index += 1
                continue
            if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                raise _VerificationFailure("invalid_json_pointer", subject)
            output.append("~" if token[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(output))
    return tuple(decoded)


def _resolve_json_pointer(document: Any, tokens: tuple[str, ...], subject: str) -> Any:
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                raise _VerificationFailure("binding_target_missing", subject)
            value = value[token]
        elif isinstance(value, list):
            if not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise _VerificationFailure("binding_target_missing", subject)
            index = int(token)
            if index >= len(value):
                raise _VerificationFailure("binding_target_missing", subject)
            value = value[index]
        else:
            raise _VerificationFailure("binding_target_missing", subject)
    return value


def _validate_expected_shape(kind: str, expected: Any, subject: str) -> None:
    if kind in _DIGEST_BINDING_KINDS:
        if not _is_sha256(expected):
            raise _VerificationFailure("invalid_binding_expected", subject)
    elif kind in _STRING_BINDING_KINDS:
        if not isinstance(expected, str) or not expected or expected != expected.strip():
            raise _VerificationFailure("invalid_binding_expected", subject)
    elif kind == "deployment_epoch":
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise _VerificationFailure("invalid_binding_expected", subject)
    elif kind == "source_commit":
        if not isinstance(expected, str) or _SOURCE_COMMIT.fullmatch(expected) is None:
            raise _VerificationFailure("invalid_binding_expected", subject)
    else:
        raise _VerificationFailure("invalid_gate_name", "manifest")


def _validate_declarations_and_bindings(
    body: dict[str, Any],
    entries: list[dict[str, Any]],
    documents: dict[str, Any],
    result: dict[str, Any],
) -> set[str]:
    entry_by_path = {entry["path"]: entry for entry in entries}
    declarations_value = body["declared_gates"]
    if not isinstance(declarations_value, list):
        raise _VerificationFailure("invalid_gate_declarations", "manifest")
    declarations: list[dict[str, Any]] = []
    seen_gates: set[str] = set()
    declared_pairs: set[tuple[str, str]] = set()
    for declaration in declarations_value:
        if not isinstance(declaration, dict) or set(declaration) != _GATE_FIELDS:
            raise _VerificationFailure("invalid_gate_declaration", "manifest")
        gate = declaration["gate"]
        if gate not in REQUIRED_PHYSICAL_INPUTS:
            raise _VerificationFailure("invalid_gate_name", "manifest")
        if gate in seen_gates:
            raise _VerificationFailure("duplicate_gate_declaration", "manifest")
        seen_gates.add(gate)
        evidence_paths = declaration["evidence_paths"]
        if (
            not isinstance(evidence_paths, list)
            or not evidence_paths
            or evidence_paths != sorted(evidence_paths)
            or len(evidence_paths) != len(set(evidence_paths))
        ):
            raise _VerificationFailure("invalid_gate_evidence_paths", "manifest")
        for path in evidence_paths:
            _validate_relative_path(path, subject="manifest")
            if path not in entry_by_path:
                raise _VerificationFailure("declared_gate_path_missing", "manifest")
            declared_pairs.add((gate, path))
        declarations.append(declaration)
    gate_order = [item["gate"] for item in declarations]
    if gate_order != sorted(gate_order):
        raise _VerificationFailure("noncanonical_gate_order", "manifest")

    bindings_value = body["bindings"]
    if not isinstance(bindings_value, list):
        raise _VerificationFailure("invalid_bindings", "manifest")
    bindings: list[dict[str, Any]] = []
    binding_pairs: set[tuple[str, str]] = set()
    binding_keys: set[tuple[str, str, str]] = set()
    for index, binding_value in enumerate(bindings_value, start=1):
        subject = f"binding:{index:04d}"
        if not isinstance(binding_value, dict) or set(binding_value) != _BINDING_FIELDS:
            raise _VerificationFailure("invalid_binding", subject)
        kind = binding_value["kind"]
        if kind not in REQUIRED_PHYSICAL_INPUTS:
            raise _VerificationFailure("invalid_gate_name", "manifest")
        path = _validate_relative_path(binding_value["path"], subject=subject)
        if path not in entry_by_path:
            raise _VerificationFailure("binding_path_missing", subject)
        pointer = binding_value["json_pointer"]
        if pointer is not None and not isinstance(pointer, str):
            raise _VerificationFailure("invalid_json_pointer", subject)
        if pointer is None and (
            not body["synthetic_fixture"] or kind != "dependency_lock"
        ):
            raise _VerificationFailure("invalid_null_binding_pointer", subject)
        tokens = () if pointer is None else _parse_json_pointer(pointer, subject)
        key = (kind, path, "" if pointer is None else pointer)
        if key in binding_keys:
            raise _VerificationFailure("duplicate_binding", "manifest")
        binding_keys.add(key)
        expected = binding_value["expected"]
        canonical_json_bytes(expected)
        _validate_expected_shape(kind, expected, subject)
        _scan_sensitive_json(expected, subject=subject)
        if tokens:
            _scan_sensitive_json(expected, subject=subject, key_hint=tokens[-1])
        if body["synthetic_fixture"] and tokens and tokens[-1] in {
            "release_ready",
            "route_ready",
        } and expected is True:
            raise _VerificationFailure("synthetic_acceptance_forbidden", subject)
        binding_pairs.add((kind, path))
        bindings.append(binding_value)

    order = [
        (item["kind"], item["path"], item["json_pointer"] or "")
        for item in bindings
    ]
    if order != sorted(order):
        raise _VerificationFailure("noncanonical_binding_order", "manifest")
    if binding_pairs - declared_pairs:
        raise _VerificationFailure("binding_gate_not_declared", "manifest")
    if declared_pairs - binding_pairs:
        raise _VerificationFailure("declared_gate_binding_missing", "manifest")

    for index, binding in enumerate(bindings, start=1):
        subject = f"binding:{index:04d}"
        entry = entry_by_path[binding["path"]]
        pointer = binding["json_pointer"]
        if pointer is None:
            actual = entry["sha256"]
        else:
            if binding["path"] not in documents:
                raise _VerificationFailure("binding_requires_json", subject)
            tokens = _parse_json_pointer(pointer, subject)
            actual = _resolve_json_pointer(documents[binding["path"]], tokens, subject)
        if canonical_json_bytes(actual) != canonical_json_bytes(binding["expected"]):
            raise _VerificationFailure("binding_mismatch", subject)

    result["observed"]["declared_gates"] = len(declarations)
    result["observed"]["declared_bindings"] = len(bindings)
    result["checks"]["declared_gate_presence"] = True
    result["checks"]["bindings"] = True
    return seen_gates


def verify_bundle(
    bundle_root: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
    _read_observer: ReadObserver | None = None,
) -> dict[str, Any]:
    """Inspect one bundle without granting qualification or readiness."""
    result = _empty_result()
    root_path: Path | None = None
    root_fd: int | None = None
    try:
        if expected_manifest_sha256 is not None and not _is_sha256(expected_manifest_sha256):
            raise _VerificationFailure("invalid_expected_manifest_sha256", "manifest")
        if _read_observer is not None and not callable(_read_observer):
            raise _VerificationFailure("invalid_read_observer", "bundle")
        root_path, root_fd, root_identity = _open_root(bundle_root)
        manifest_content, manifest_snapshot = _read_regular_file(
            root_fd,
            MANIFEST_FILENAME,
            maximum_bytes=MAX_MANIFEST_BYTES,
            subject="manifest",
            read_observer=_read_observer,
        )
        document = _load_manifest(manifest_content)
        result["checks"]["manifest_canonical"] = True
        result["manifest_sha256"] = sha256_bytes(manifest_content)
        if expected_manifest_sha256 is not None and result["manifest_sha256"] != expected_manifest_sha256:
            raise _VerificationFailure("expected_manifest_sha256_mismatch", "manifest")
        if set(document) != _MANIFEST_FIELDS:
            raise _VerificationFailure("invalid_manifest_fields", "manifest")
        if document["protocol"] != MANIFEST_PROTOCOL or not isinstance(document["body"], dict):
            raise _VerificationFailure("invalid_manifest_protocol", "manifest")
        body_sha256 = document["body_sha256"]
        if not _is_sha256(body_sha256) or body_sha256 != sha256_bytes(
            canonical_json_bytes(document["body"])
        ):
            raise _VerificationFailure("manifest_body_digest_mismatch", "manifest")
        result["body_sha256"] = body_sha256
        result["checks"]["manifest_digest"] = True
        _scan_raw_content(manifest_content, "manifest")
        _scan_sensitive_json(document, subject="manifest")

        body = document["body"]
        entries = _validate_body(body, result)
        result["checks"]["path_policy"] = True
        expected_files = {entry["path"] for entry in entries} | {MANIFEST_FILENAME}
        expected_directories = _expected_directories({entry["path"] for entry in entries})
        actual_files, actual_directories = _inventory(
            root_fd, expected_files, expected_directories
        )
        if expected_files - set(actual_files):
            raise _VerificationFailure("missing_input", "bundle")
        if expected_directories - set(actual_directories):
            raise _VerificationFailure("missing_input", "bundle")
        if actual_files[MANIFEST_FILENAME] != manifest_snapshot:
            raise _VerificationFailure("concurrently_changed_input", "manifest")

        documents: dict[str, Any] = {}
        for index, entry in enumerate(entries, start=1):
            subject = f"artifact:{index:04d}"
            content, verified_snapshot = _read_regular_file(
                root_fd,
                entry["path"],
                maximum_bytes=MAX_FILE_BYTES,
                subject=subject,
                read_observer=_read_observer,
            )
            if actual_files[entry["path"]] != verified_snapshot:
                raise _VerificationFailure("concurrently_changed_input", subject)
            if len(content) != entry["size_bytes"]:
                raise _VerificationFailure("file_size_mismatch", subject)
            if sha256_bytes(content) != entry["sha256"]:
                raise _VerificationFailure("file_digest_mismatch", subject)
            _scan_raw_content(content, subject)
            if not body["synthetic_fixture"] and any(
                marker in content for marker in _SYNTHETIC_CONTENT_MARKERS
            ):
                raise _VerificationFailure(
                    "physical_bundle_contains_synthetic_marker", subject
                )
            if entry["media_type"] == "application/json":
                artifact = _load_artifact_json(content, subject)
                if body["synthetic_fixture"]:
                    if (
                        not isinstance(artifact, dict)
                        or artifact.get("evidence_class") != "synthetic_test_fixture"
                        or artifact.get("synthetic_fixture") is not True
                    ):
                        raise _VerificationFailure("synthetic_fixture_not_prominent", subject)
                    if _synthetic_acceptance_present(artifact):
                        raise _VerificationFailure("synthetic_acceptance_forbidden", subject)
                elif (
                    not isinstance(artifact, dict)
                    or artifact.get("evidence_class") != "physical_qualification"
                    or artifact.get("synthetic_fixture") is not False
                ):
                    raise _VerificationFailure(
                        "physical_bundle_contains_synthetic_marker", subject
                    )
                _scan_sensitive_json(artifact, subject=subject)
                _validate_artifact_fields(artifact, subject=subject)
                documents[entry["path"]] = artifact
            result["observed"]["scanned_files"] += 1
            result["observed"]["scanned_bytes"] += len(content)
        result["checks"]["content_policy"] = True

        declared_gates = _validate_declarations_and_bindings(
            body, entries, documents, result
        )
        if body["synthetic_fixture"]:
            result["missing_physical_inputs"] = list(REQUIRED_PHYSICAL_INPUTS)
        else:
            result["missing_physical_inputs"] = [
                gate for gate in REQUIRED_PHYSICAL_INPUTS if gate not in declared_gates
            ]

        final_files, final_directories = _inventory(
            root_fd, expected_files, expected_directories
        )
        if final_files != actual_files or final_directories != actual_directories:
            raise _VerificationFailure("concurrently_changed_input", "bundle")
        final_root = root_path.stat(follow_symlinks=False)
        if _snapshot(final_root) != _snapshot(root_identity):
            raise _VerificationFailure("concurrently_changed_input", "bundle")
        result["checks"]["file_inventory"] = True
        result["checks"]["file_integrity"] = True
        result["physical_input_inventory_complete"] = (
            body["evidence_class"] == "physical_qualification"
            and not result["missing_physical_inputs"]
        )
        result["ok"] = True
    except _VerificationFailure as exc:
        result["findings"] = [{"code": exc.code, "subject": exc.subject}]
    except (OSError, RuntimeError, TypeError, ValueError):
        result["findings"] = [{"code": "internal_verifier_error", "subject": "bundle"}]
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return result
