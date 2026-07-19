from __future__ import annotations

import copy

import pytest

from mycelium_qualification.evidence import (
    EvidenceValidationError,
    build_evidence_manifest,
    canonical_json_bytes,
    canonical_json_loads,
    evidence_manifest_digest,
    sha256_bytes,
    validate_evidence_manifest,
)


def _files() -> dict[str, bytes]:
    return {
        "a/source.json": canonical_json_bytes({"z": 1, "a": "moon"}),
        "b/lockfile": b"locked dependency bytes\n",
    }


def test_evidence_manifest_is_sorted_immutable_and_sha256_bound() -> None:
    files = _files()
    manifest = build_evidence_manifest(
        run_id="synthetic_test_fixture/evidence",
        evidence_class="synthetic_test_fixture",
        files=files,
    )

    assert manifest["protocol"] == "mycelium.route_qualification_evidence_manifest.v1"
    assert [entry["path"] for entry in manifest["files"]] == sorted(files)
    assert manifest["file_count"] == len(files)
    assert manifest["total_size_bytes"] == sum(map(len, files.values()))
    assert evidence_manifest_digest(manifest).startswith("sha256:")
    assert validate_evidence_manifest(manifest, files) == evidence_manifest_digest(manifest)
    for entry in manifest["files"]:
        assert entry["sha256"] == sha256_bytes(files[entry["path"]])
        assert entry["size_bytes"] == len(files[entry["path"]])


def test_evidence_manifest_rejects_changed_missing_and_unlisted_bytes() -> None:
    files = _files()
    manifest = build_evidence_manifest(
        run_id="synthetic_test_fixture/evidence",
        evidence_class="synthetic_test_fixture",
        files=files,
    )

    changed = dict(files)
    changed["a/source.json"] = b"X" + changed["a/source.json"][1:]
    with pytest.raises(EvidenceValidationError, match="evidence_file_digest_mismatch"):
        validate_evidence_manifest(manifest, changed)

    missing = dict(files)
    missing.pop("a/source.json")
    with pytest.raises(EvidenceValidationError, match="evidence_manifest_file_set_mismatch"):
        validate_evidence_manifest(manifest, missing)

    extra = dict(files, **{"c/unlisted": b"unlisted"})
    with pytest.raises(EvidenceValidationError, match="evidence_manifest_file_set_mismatch"):
        validate_evidence_manifest(manifest, extra)


def test_evidence_manifest_rejects_reordered_duplicate_and_unsafe_paths() -> None:
    files = _files()
    manifest = build_evidence_manifest(
        run_id="synthetic_test_fixture/evidence",
        evidence_class="synthetic_test_fixture",
        files=files,
    )

    reordered = copy.deepcopy(manifest)
    reordered["files"].reverse()
    with pytest.raises(EvidenceValidationError, match="evidence_manifest_not_canonical"):
        validate_evidence_manifest(reordered, files)

    duplicate = copy.deepcopy(manifest)
    duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
    duplicate["file_count"] += 1
    duplicate["total_size_bytes"] += duplicate["files"][-1]["size_bytes"]
    with pytest.raises(EvidenceValidationError, match="duplicate_evidence_path"):
        validate_evidence_manifest(duplicate, files)

    for unsafe in (
        "../escape",
        "/absolute",
        "a/./b",
        "a\\windows",
        "a//b",
        "a\x7fdel",
        "a" * 513,
        "/".join(["a"] * 33),
        "a" * 129,
        "a\ud800surrogate",
    ):
        unsafe_files = {unsafe: b"x"}
        with pytest.raises(EvidenceValidationError, match="unsafe_evidence_path"):
            build_evidence_manifest(
                run_id="synthetic_test_fixture/evidence",
                evidence_class="synthetic_test_fixture",
                files=unsafe_files,
            )


def test_canonical_json_loader_rejects_noncanonical_and_duplicate_documents() -> None:
    canonical = canonical_json_bytes({"a": 1, "z": [True, None]})
    assert canonical_json_loads(canonical, path="canonical.json") == {
        "a": 1,
        "z": [True, None],
    }

    with pytest.raises(EvidenceValidationError, match="noncanonical_evidence_json"):
        canonical_json_loads(b'{"z": [true, null], "a": 1}', path="spaced.json")
    with pytest.raises(EvidenceValidationError, match="duplicate_json_key"):
        canonical_json_loads(b'{"a":1,"a":2}', path="duplicate.json")
    with pytest.raises(EvidenceValidationError, match="invalid_evidence_json"):
        canonical_json_loads(b'{"value":NaN}', path="nan.json")


def test_canonical_json_rejects_excessive_nesting_with_stable_error() -> None:
    nested: object = None
    for _ in range(2_000):
        nested = [nested]
    with pytest.raises(EvidenceValidationError, match="noncanonical_json"):
        canonical_json_bytes(nested)

    content = b"[" * 2_000 + b"null" + b"]" * 2_000
    with pytest.raises(EvidenceValidationError, match="invalid_evidence_json"):
        canonical_json_loads(content, path="deep.json")


def test_manifest_digest_changes_for_every_manifest_mutation() -> None:
    files = _files()
    manifest = build_evidence_manifest(
        run_id="synthetic_test_fixture/evidence",
        evidence_class="synthetic_test_fixture",
        files=files,
    )
    original = evidence_manifest_digest(manifest)
    mutated = copy.deepcopy(manifest)
    mutated["run_id"] += "-mutated"

    assert evidence_manifest_digest(mutated) != original
