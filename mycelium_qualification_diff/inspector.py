"""Bounded deterministic qualification evidence-diff inspection."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, cast

MANIFEST_PROTOCOL = "mycelium.route_qualification_evidence_manifest.v1"
REPORT_PROTOCOL = "mycelium.qualification_evidence_diff.v1"
MAX_MANIFEST_COUNT = 2
MAX_MANIFEST_BYTES = 512 * 1024
MAX_FILE_COUNT = 256
MAX_DOCUMENT_COUNT = 128
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_PATH_COMPONENTS = 32
MAX_PATH_COMPONENT_BYTES = 128
MAX_JSON_DEPTH = 48
MAX_JSON_NODES = 100_000
MAX_CHANGES = 1_024

CLAIM_BOUNDARY = (
    "read-only structural and digest diff of supplied qualification evidence bytes only; "
    "qualification semantics, physical execution, route readiness, and release readiness "
    "are not evaluated or granted"
)

_MANIFEST_FIELDS = frozenset(
    {
        "evidence_class",
        "file_count",
        "files",
        "protocol",
        "run_id",
        "total_size_bytes",
    }
)
_ENTRY_FIELDS = frozenset({"path", "sha256", "size_bytes"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CATEGORIES = (
    "deployment_epoch",
    "endpoints",
    "graph",
    "identity",
    "kv_ownership",
    "load_proofs",
    "manifest",
    "model_commit_manifest",
    "negative_runs",
    "other",
    "parity",
    "processes",
    "signatures",
    "source_provenance",
    "tensors",
    "timing",
    "topology_version",
    "transport",
)
_CHANGE_TYPES = ("added", "changed", "removed")
_MANIFEST_LOCATION = "\x00manifest"


class EvidenceDiffError(ValueError):
    """Fail-closed inspector error carrying only a stable machine code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceDiffError(code)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvidenceDiffError("noncanonical_json") from exc


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceDiffError("duplicate_json_key")
        result[key] = value
    return result


def _load_canonical_json(content: bytes, *, manifest: bool) -> Any:
    invalid_code = "invalid_manifest_json" if manifest else "invalid_document_json"
    noncanonical_code = (
        "noncanonical_manifest_json" if manifest else "noncanonical_document_json"
    )
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvidenceDiffError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EvidenceDiffError(invalid_code) from exc
    try:
        canonical = _canonical_json_bytes(value)
    except EvidenceDiffError as exc:
        raise EvidenceDiffError(invalid_code) from exc
    _require(canonical == content, noncanonical_code)
    return value


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _value_digest(value: Any) -> str:
    return _sha256(_canonical_json_bytes(value))


def _validate_path(value: Any) -> str:
    _require(type(value) is str and bool(value), "unsafe_evidence_path")
    path = value
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceDiffError("unsafe_evidence_path") from exc
    _require(len(encoded) <= MAX_PATH_BYTES, "unsafe_evidence_path")
    _require(path == path.strip(), "unsafe_evidence_path")
    _require(not path.startswith("/"), "unsafe_evidence_path")
    _require("\\" not in path and "//" not in path, "unsafe_evidence_path")
    _require(not any(ord(character) < 32 or ord(character) == 127 for character in path), "unsafe_evidence_path")
    components = path.split("/")
    _require(len(components) <= MAX_PATH_COMPONENTS, "unsafe_evidence_path")
    _require(all(component not in {"", ".", ".."} for component in components), "unsafe_evidence_path")
    _require(
        all(len(component.encode("utf-8")) <= MAX_PATH_COMPONENT_BYTES for component in components),
        "unsafe_evidence_path",
    )
    return path


def _measure_json(value: Any, *, allowance: int) -> int:
    stack: list[tuple[Any, int]] = [(value, 0)]
    count = 0
    while stack:
        current, depth = stack.pop()
        _require(depth <= MAX_JSON_DEPTH, "json_too_deep")
        count += 1
        _require(count <= allowance, "too_many_json_nodes")
        if type(current) is dict:
            stack.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
    return count


def _load_bundle(
    manifest_bytes: bytes,
    files: dict[str, bytes],
) -> dict[str, Any]:
    _require(type(manifest_bytes) is bytes, "invalid_manifest_bytes")
    _require(len(manifest_bytes) <= MAX_MANIFEST_BYTES, "manifest_too_large")
    _require(type(files) is dict, "invalid_bundle_files")

    manifest = _load_canonical_json(manifest_bytes, manifest=True)
    _require(type(manifest) is dict, "invalid_manifest_document")
    _require(set(manifest) == _MANIFEST_FIELDS, "invalid_manifest_fields")
    _require(manifest["protocol"] == MANIFEST_PROTOCOL, "unsupported_manifest_protocol")
    _require(
        type(manifest["run_id"]) is str
        and bool(manifest["run_id"].strip())
        and len(manifest["run_id"].encode("utf-8")) <= MAX_PATH_BYTES,
        "invalid_run_id",
    )
    _require(
        manifest["evidence_class"]
        in {"physical_qualification", "synthetic_test_fixture"},
        "invalid_evidence_class",
    )

    entries = manifest["files"]
    _require(type(entries) is list and bool(entries), "invalid_manifest_files")
    _require(len(entries) <= MAX_FILE_COUNT, "too_many_files")
    _require(
        type(manifest["file_count"]) is int
        and manifest["file_count"] == len(entries),
        "invalid_file_count",
    )
    _require(
        type(manifest["total_size_bytes"]) is int
        and manifest["total_size_bytes"] >= 0,
        "invalid_total_size",
    )
    _require(manifest["total_size_bytes"] <= MAX_TOTAL_BYTES, "bundle_too_large")

    indexed: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    declared_total = 0
    for raw_entry in entries:
        _require(
            type(raw_entry) is dict and set(raw_entry) == _ENTRY_FIELDS,
            "invalid_manifest_entry",
        )
        entry = cast(dict[str, Any], raw_entry)
        path = _validate_path(entry["path"])
        _require(path not in indexed, "duplicate_evidence_path")
        size = entry["size_bytes"]
        _require(type(size) is int and size >= 0, "invalid_file_size")
        _require(size <= MAX_FILE_BYTES, "file_too_large")
        _require(
            type(entry["sha256"]) is str
            and _SHA256.fullmatch(entry["sha256"]) is not None,
            "invalid_file_digest",
        )
        indexed[path] = entry
        order.append(path)
        declared_total += size
    _require(order == sorted(order), "noncanonical_manifest_order")
    _require(declared_total == manifest["total_size_bytes"], "invalid_total_size")

    _require(len(files) <= MAX_FILE_COUNT, "too_many_files")
    actual_paths: set[str] = set()
    for raw_path in files:
        actual_paths.add(_validate_path(raw_path))
    _require(actual_paths == set(indexed), "file_set_mismatch")

    actual_total = 0
    for path in sorted(actual_paths):
        content = files[path]
        _require(type(content) is bytes, "invalid_evidence_bytes")
        _require(len(content) <= MAX_FILE_BYTES, "file_too_large")
        entry = indexed[path]
        _require(len(content) == entry["size_bytes"], "file_size_mismatch")
        _require(_sha256(content) == entry["sha256"], "file_digest_mismatch")
        actual_total += len(content)
    _require(actual_total == manifest["total_size_bytes"], "invalid_total_size")
    _require(actual_total <= MAX_TOTAL_BYTES, "bundle_too_large")

    document_paths = [path for path in sorted(actual_paths) if path.endswith(".json")]
    _require(len(document_paths) <= MAX_DOCUMENT_COUNT, "too_many_documents")
    documents: dict[str, Any] = {}
    node_count = 0
    for path in document_paths:
        document = _load_canonical_json(files[path], manifest=False)
        node_count += _measure_json(document, allowance=MAX_JSON_NODES - node_count)
        documents[path] = document

    return {
        "document_count": len(documents),
        "documents": documents,
        "file_count": len(files),
        "files": files,
        "manifest": manifest,
        "manifest_digest": _sha256(manifest_bytes),
        "manifest_size_bytes": len(manifest_bytes),
        "total_size_bytes": actual_total,
    }


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _category(document_path: str, tokens: tuple[str | int, ...]) -> str:
    document = _normalized(document_path)
    string_tokens = [_normalized(token) for token in tokens if type(token) is str]
    leaf = string_tokens[-1] if string_tokens else document
    context = "_".join([document, *string_tokens])

    if "provenance" in document or "source_provenance" in context:
        return "source_provenance"
    if leaf == "deployment_epoch":
        return "deployment_epoch"
    if leaf in {"topology_version", "topology_generation", "snapshot_generation"}:
        return "topology_version"
    if leaf in {
        "contract_manifest_digest",
        "manifest_digest",
        "model_id",
        "model_manifest_digest",
        "resolved_commit",
        "source_commit",
    } or leaf.startswith("model_"):
        return "model_commit_manifest"
    if "endpoint" in leaf:
        return "endpoints"
    if "process" in leaf or leaf in {"pid", "pids"}:
        return "processes"
    if "tensor" in leaf:
        return "tensors"
    if "signature" in leaf:
        return "signatures"
    if (
        leaf.endswith(("_at_unix_ms", "_elapsed_ns", "_monotonic_ns"))
        or "timing" in leaf
        or leaf in {"duration_ms", "duration_ns", "timestamp"}
    ):
        return "timing"
    if "negative_run" in context or ("negative" in context and "run" in context):
        return "negative_runs"
    if "kv_ownership" in context or "kv_cache" in context:
        return "kv_ownership"
    if "parity" in context:
        return "parity"
    if "signature" in context:
        return "signatures"
    if "load_proof" in context:
        return "load_proofs"
    if "tensor" in context:
        return "tensors"
    if "process" in context:
        return "processes"
    if "endpoint" in context:
        return "endpoints"
    if "execution_graph" in context or "path_manifest" in context or "graph" in context:
        return "graph"
    if "transport" in context or "router_wire" in context:
        return "transport"
    if "model" in context or "commit" in context or "manifest_digest" in context:
        return "model_commit_manifest"
    if "topology" in context or "snapshot_generation" in context:
        return "topology_version"
    if "deployment_epoch" in context:
        return "deployment_epoch"
    if any(
        marker in context
        for marker in (
            "assignment_id",
            "deployment_id",
            "evidence_class",
            "node_id",
            "path_id",
            "qualification_id",
            "request_id",
            "route_id",
            "run_id",
            "stage_id",
            "swarm_id",
        )
    ):
        return "identity"
    if document_path == _MANIFEST_LOCATION:
        return "manifest"
    return "other"


def _location_digest(document_path: str, tokens: tuple[str | int, ...]) -> str:
    return _value_digest([document_path, *tokens])


def _append_change(
    changes: list[dict[str, Any]],
    *,
    change: str,
    document_path: str,
    tokens: tuple[str | int, ...],
    before_digest: str | None,
    after_digest: str | None,
) -> None:
    _require(len(changes) < MAX_CHANGES, "too_many_changes")
    category = _category(document_path, tokens)
    changes.append(
        {
            "after_digest": after_digest,
            "before_digest": before_digest,
            "category": category,
            "change": change,
            "code": f"{category.upper()}_{change.upper()}",
            "document_path_digest": _sha256(document_path.encode("utf-8")),
            "location_digest": _location_digest(document_path, tokens),
        }
    )


def _diff_values(
    before: Any,
    after: Any,
    *,
    document_path: str,
    tokens: tuple[str | int, ...],
    changes: list[dict[str, Any]],
) -> None:
    if type(before) is type(after) and before == after:
        return
    if type(before) is dict and type(after) is dict:
        for key in sorted(set(before) | set(after)):
            location = (*tokens, key)
            if key not in before:
                _append_change(
                    changes,
                    change="added",
                    document_path=document_path,
                    tokens=location,
                    before_digest=None,
                    after_digest=_value_digest(after[key]),
                )
            elif key not in after:
                _append_change(
                    changes,
                    change="removed",
                    document_path=document_path,
                    tokens=location,
                    before_digest=_value_digest(before[key]),
                    after_digest=None,
                )
            else:
                _diff_values(
                    before[key],
                    after[key],
                    document_path=document_path,
                    tokens=location,
                    changes=changes,
                )
        return
    if type(before) is list and type(after) is list:
        for index in range(max(len(before), len(after))):
            location = (*tokens, index)
            if index >= len(before):
                _append_change(
                    changes,
                    change="added",
                    document_path=document_path,
                    tokens=location,
                    before_digest=None,
                    after_digest=_value_digest(after[index]),
                )
            elif index >= len(after):
                _append_change(
                    changes,
                    change="removed",
                    document_path=document_path,
                    tokens=location,
                    before_digest=_value_digest(before[index]),
                    after_digest=None,
                )
            else:
                _diff_values(
                    before[index],
                    after[index],
                    document_path=document_path,
                    tokens=location,
                    changes=changes,
                )
        return
    _append_change(
        changes,
        change="changed",
        document_path=document_path,
        tokens=tokens,
        before_digest=_value_digest(before),
        after_digest=_value_digest(after),
    )


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for field in ("evidence_class", "run_id"):
        _diff_values(
            baseline["manifest"][field],
            candidate["manifest"][field],
            document_path=_MANIFEST_LOCATION,
            tokens=(field,),
            changes=changes,
        )

    baseline_paths = set(baseline["files"])
    candidate_paths = set(candidate["files"])
    for path in sorted(baseline_paths | candidate_paths):
        if path not in baseline_paths:
            _append_change(
                changes,
                change="added",
                document_path=path,
                tokens=(),
                before_digest=None,
                after_digest=_sha256(candidate["files"][path]),
            )
        elif path not in candidate_paths:
            _append_change(
                changes,
                change="removed",
                document_path=path,
                tokens=(),
                before_digest=_sha256(baseline["files"][path]),
                after_digest=None,
            )
        elif baseline["files"][path] != candidate["files"][path]:
            if path.endswith(".json"):
                _diff_values(
                    baseline["documents"][path],
                    candidate["documents"][path],
                    document_path=path,
                    tokens=(),
                    changes=changes,
                )
            else:
                _append_change(
                    changes,
                    change="changed",
                    document_path=path,
                    tokens=(),
                    before_digest=_sha256(baseline["files"][path]),
                    after_digest=_sha256(candidate["files"][path]),
                )
    changes.sort(
        key=lambda item: (
            item["category"],
            item["code"],
            item["document_path_digest"],
            item["location_digest"],
        )
    )
    return changes


def _side_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_count": bundle["document_count"],
        "file_count": bundle["file_count"],
        "manifest_digest": bundle["manifest_digest"],
        "manifest_size_bytes": bundle["manifest_size_bytes"],
        "total_size_bytes": bundle["total_size_bytes"],
    }


def inspect_evidence_diff(
    baseline_manifest_bytes: bytes,
    baseline_files: dict[str, bytes],
    candidate_manifest_bytes: bytes,
    candidate_files: dict[str, bytes],
) -> bytes:
    """Validate and compare two explicit in-memory evidence bundles.

    Output discloses only counts, stable codes, and SHA-256 digests. It never
    evaluates qualification or grants route/release readiness.
    """
    baseline = _load_bundle(baseline_manifest_bytes, baseline_files)
    candidate = _load_bundle(candidate_manifest_bytes, candidate_files)
    changes = _compare(baseline, candidate)

    by_category = {category: 0 for category in _CATEGORIES}
    by_change = {change: 0 for change in _CHANGE_TYPES}
    for item in changes:
        by_category[item["category"]] += 1
        by_change[item["change"]] += 1

    report = {
        "baseline": _side_summary(baseline),
        "bounds": {
            "manifest_count": MAX_MANIFEST_COUNT,
            "max_changes": MAX_CHANGES,
            "max_document_count_per_bundle": MAX_DOCUMENT_COUNT,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_file_count_per_bundle": MAX_FILE_COUNT,
            "max_json_depth": MAX_JSON_DEPTH,
            "max_json_nodes_per_bundle": MAX_JSON_NODES,
            "max_manifest_bytes": MAX_MANIFEST_BYTES,
            "max_path_bytes": MAX_PATH_BYTES,
            "max_path_component_bytes": MAX_PATH_COMPONENT_BYTES,
            "max_path_components": MAX_PATH_COMPONENTS,
            "max_total_bytes_per_bundle": MAX_TOTAL_BYTES,
        },
        "candidate": _side_summary(candidate),
        "changes": changes,
        "claim_boundary": CLAIM_BOUNDARY,
        "identical": not changes,
        "input_integrity_validated": True,
        "inspection_only": True,
        "protocol": REPORT_PROTOCOL,
        "qualification_evaluated": False,
        "release_ready": False,
        "route_ready": False,
        "summary": {
            "by_category": by_category,
            "by_change": by_change,
            "total_changes": len(changes),
        },
        "values_disclosed": False,
    }
    return _canonical_json_bytes(report)
