from __future__ import annotations

import json

import pytest

from mycelium_qualification_diff import (
    MAX_CHANGES,
    MAX_DOCUMENT_COUNT,
    MAX_FILE_BYTES,
    MAX_FILE_COUNT,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_BYTES,
    EvidenceDiffError,
    inspect_evidence_diff,
)

from .conftest import canonical, make_bundle, manifest_bytes, sha256


class BytesSubclass(bytes):
    pass


@pytest.mark.parametrize(
    ("slot", "code"),
    [
        ("baseline_manifest_subclass", "invalid_manifest_bytes"),
        ("candidate_manifest_bytearray", "invalid_manifest_bytes"),
        ("baseline_file_subclass", "invalid_evidence_bytes"),
        ("candidate_file_memoryview", "invalid_evidence_bytes"),
    ],
)
def test_only_exact_bytes_are_accepted(small_bundle, slot: str, code: str) -> None:
    manifest, files = small_bundle
    baseline_manifest: object = manifest
    candidate_manifest: object = manifest
    baseline_files = dict(files)
    candidate_files = dict(files)
    if slot == "baseline_manifest_subclass":
        baseline_manifest = BytesSubclass(manifest)
    elif slot == "candidate_manifest_bytearray":
        candidate_manifest = bytearray(manifest)
    elif slot == "baseline_file_subclass":
        path = next(iter(baseline_files))
        baseline_files[path] = BytesSubclass(baseline_files[path])
    else:
        path = next(iter(candidate_files))
        candidate_files[path] = memoryview(candidate_files[path])  # type: ignore[assignment]

    with pytest.raises(EvidenceDiffError) as captured:
        inspect_evidence_diff(  # type: ignore[arg-type]
            baseline_manifest,
            baseline_files,
            candidate_manifest,
            candidate_files,
        )

    assert captured.value.code == code
    assert str(captured.value) == code


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("spaced_manifest", "noncanonical_manifest_json"),
        ("duplicate_manifest", "duplicate_json_key"),
        ("spaced_document", "noncanonical_document_json"),
        ("duplicate_document", "duplicate_json_key"),
    ],
)
def test_canonical_json_and_duplicate_keys_fail_closed(
    small_bundle, kind: str, code: str
) -> None:
    manifest, files = small_bundle
    if kind == "spaced_manifest":
        manifest = json.dumps(json.loads(manifest), sort_keys=True).encode("utf-8")
    elif kind == "duplicate_manifest":
        manifest = b'{"evidence_class":"physical_qualification",' + manifest[1:]
    elif kind == "spaced_document":
        files = {"run/evidence.json": b'{"value": 1}'}
        manifest = manifest_bytes(files)
    else:
        files = {"run/evidence.json": b'{"value":1,"value":2}'}
        manifest = manifest_bytes(files)

    with pytest.raises(EvidenceDiffError) as captured:
        inspect_evidence_diff(manifest, files, manifest, files)

    assert captured.value.code == code


def test_unpaired_unicode_surrogates_fail_with_stable_json_codes(small_bundle) -> None:
    manifest, files = small_bundle
    manifest_value = json.loads(manifest)
    manifest_value["run_id"] = "\ud800"
    invalid_manifest = json.dumps(
        manifest_value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    with pytest.raises(EvidenceDiffError) as manifest_error:
        inspect_evidence_diff(invalid_manifest, files, manifest, files)
    assert manifest_error.value.code == "invalid_manifest_json"

    invalid_files = {"run/evidence.json": b'{"value":"\\ud800"}'}
    invalid_document_manifest = manifest_bytes(invalid_files)
    with pytest.raises(EvidenceDiffError) as document_error:
        inspect_evidence_diff(
            invalid_document_manifest,
            invalid_files,
            manifest,
            files,
        )
    assert document_error.value.code == "invalid_document_json"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.json",
        "/absolute.json",
        "a/./b.json",
        "a/../b.json",
        "a\\windows.json",
        "a//b.json",
        "a/\u0000secret.json",
    ],
)
def test_unsafe_paths_are_rejected_lexically_without_traversal(path: str) -> None:
    files = {path: canonical({"value": 1})}
    manifest = manifest_bytes(files)

    with pytest.raises(EvidenceDiffError) as captured:
        inspect_evidence_diff(manifest, files, manifest, files)

    assert captured.value.code == "unsafe_evidence_path"
    assert path not in str(captured.value)


def test_manifest_file_document_and_total_byte_bounds_are_enforced() -> None:
    valid_manifest, valid_files = make_bundle({"run/valid.json": {"value": 1}})
    with pytest.raises(EvidenceDiffError) as manifest_error:
        inspect_evidence_diff(
            b"x" * (MAX_MANIFEST_BYTES + 1),
            {},
            valid_manifest,
            valid_files,
        )
    assert manifest_error.value.code == "manifest_too_large"

    too_large_files = {"run/large.bin": b"x" * (MAX_FILE_BYTES + 1)}
    with pytest.raises(EvidenceDiffError) as file_error:
        inspect_evidence_diff(
            manifest_bytes(too_large_files),
            too_large_files,
            valid_manifest,
            valid_files,
        )
    assert file_error.value.code == "file_too_large"

    too_many_files = {
        f"run/file-{index:04d}.bin": b"x"
        for index in range(MAX_FILE_COUNT + 1)
    }
    with pytest.raises(EvidenceDiffError) as count_error:
        inspect_evidence_diff(
            manifest_bytes(too_many_files),
            too_many_files,
            valid_manifest,
            valid_files,
        )
    assert count_error.value.code == "too_many_files"

    too_many_documents = {
        f"run/document-{index:04d}.json": {"value": index}
        for index in range(MAX_DOCUMENT_COUNT + 1)
    }
    document_manifest, document_files = make_bundle(too_many_documents)
    with pytest.raises(EvidenceDiffError) as document_error:
        inspect_evidence_diff(
            document_manifest,
            document_files,
            valid_manifest,
            valid_files,
        )
    assert document_error.value.code == "too_many_documents"

    count = MAX_TOTAL_BYTES // MAX_FILE_BYTES + 1
    declared_entries = [
        {
            "path": f"run/declared-{index:04d}.bin",
            "sha256": sha256(b""),
            "size_bytes": MAX_FILE_BYTES,
        }
        for index in range(count)
    ]
    oversized_total_manifest = canonical(
        {
            "evidence_class": "physical_qualification",
            "file_count": count,
            "files": declared_entries,
            "protocol": "mycelium.route_qualification_evidence_manifest.v1",
            "run_id": "qualification-diff-test",
            "total_size_bytes": count * MAX_FILE_BYTES,
        }
    )
    with pytest.raises(EvidenceDiffError) as total_error:
        inspect_evidence_diff(
            oversized_total_manifest,
            {},
            valid_manifest,
            valid_files,
        )
    assert total_error.value.code == "bundle_too_large"


def test_json_depth_node_and_report_change_bounds_are_enforced() -> None:
    valid_manifest, valid_files = make_bundle({"run/valid.json": {"value": 1}})

    nested: object = 0
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = [nested]
    depth_manifest, depth_files = make_bundle({"run/deep.json": nested})
    with pytest.raises(EvidenceDiffError) as depth_error:
        inspect_evidence_diff(depth_manifest, depth_files, valid_manifest, valid_files)
    assert depth_error.value.code == "json_too_deep"

    node_manifest, node_files = make_bundle(
        {"run/nodes.json": [0] * MAX_JSON_NODES}
    )
    with pytest.raises(EvidenceDiffError) as node_error:
        inspect_evidence_diff(node_manifest, node_files, valid_manifest, valid_files)
    assert node_error.value.code == "too_many_json_nodes"

    baseline_manifest, baseline_files = make_bundle(
        {"run/changes.json": {f"field_{index:04d}": 0 for index in range(MAX_CHANGES + 1)}}
    )
    candidate_manifest, candidate_files = make_bundle(
        {"run/changes.json": {f"field_{index:04d}": 1 for index in range(MAX_CHANGES + 1)}}
    )
    with pytest.raises(EvidenceDiffError) as change_error:
        inspect_evidence_diff(
            baseline_manifest,
            baseline_files,
            candidate_manifest,
            candidate_files,
        )
    assert change_error.value.code == "too_many_changes"


def test_manifest_integrity_and_exact_file_set_are_required(small_bundle) -> None:
    manifest, files = small_bundle
    path = next(iter(files))

    changed = dict(files)
    changed[path] = canonical({"kind": "fixture", "value": 2})
    with pytest.raises(EvidenceDiffError) as digest_error:
        inspect_evidence_diff(manifest, changed, manifest, files)
    assert digest_error.value.code == "file_size_mismatch" or digest_error.value.code == "file_digest_mismatch"

    missing: dict[str, bytes] = {}
    with pytest.raises(EvidenceDiffError) as set_error:
        inspect_evidence_diff(manifest, missing, manifest, files)
    assert set_error.value.code == "file_set_mismatch"

    extra = dict(files, **{"run/unlisted.json": canonical({"secret": "CANARY"})})
    with pytest.raises(EvidenceDiffError) as extra_error:
        inspect_evidence_diff(manifest, extra, manifest, files)
    assert extra_error.value.code == "file_set_mismatch"
