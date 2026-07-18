from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from mycelium_release_bundle import MAX_FILE_BYTES, verify_bundle

from .conftest import rewrite_manifest


def _rewrite_entry_path(
    root: Path, manifest: dict[str, object], replacement: str
) -> None:
    body = manifest["body"]
    assert isinstance(body, dict)
    files = body["files"]
    assert isinstance(files, list)
    files[0]["path"] = replacement
    rewrite_manifest(root, manifest)


def test_honest_bundle_checks_exact_inventory_and_file_hashes(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle

    result = verify_bundle(root)

    assert result["ok"] is True
    assert result["checks"]["path_policy"] is True
    assert result["checks"]["file_inventory"] is True
    assert result["checks"]["file_integrity"] is True
    assert result["observed"]["declared_files"] == 2
    assert result["observed"]["scanned_files"] == 2
    assert result["observed"]["scanned_bytes"] > 0


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.json",
        "/absolute.json",
        "qualification/../escape.json",
        "qualification\\windows.json",
        "qualification//double.json",
        "qualification/./dot.json",
    ],
)
def test_traversal_absolute_and_noncanonical_paths_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]], unsafe: str
) -> None:
    root, manifest = synthetic_bundle
    _rewrite_entry_path(root, manifest, unsafe)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "unsafe_bundle_path", "subject": "artifact:0001"}
    ]
    assert result["route_ready"] is False
    assert result["release_ready"] is False


def test_non_allowlisted_top_level_path_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    _rewrite_entry_path(root, manifest, "unreviewed/evidence.json")

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "path_not_allowlisted", "subject": "artifact:0001"}
    ]


def test_duplicate_normalized_paths_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    body = manifest["body"]
    assert isinstance(body, dict)
    original = body["files"][0]
    body["files"] = [copy.deepcopy(original), copy.deepcopy(original)]
    body["files"][0]["path"] = "qualification/cafe\N{COMBINING ACUTE ACCENT}.json"
    body["files"][1]["path"] = "qualification/caf\N{LATIN SMALL LETTER E WITH ACUTE}.json"
    body["file_count"] = 2
    body["total_size_bytes"] = sum(entry["size_bytes"] for entry in body["files"])
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "duplicate_normalized_path", "subject": "manifest"}
    ]


def test_exact_duplicate_paths_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    body = manifest["body"]
    assert isinstance(body, dict)
    body["files"].append(copy.deepcopy(body["files"][0]))
    body["file_count"] += 1
    body["total_size_bytes"] += body["files"][-1]["size_bytes"]
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "duplicate_bundle_path", "subject": "manifest"}
    ]


def test_case_colliding_paths_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = synthetic_bundle
    body = manifest["body"]
    assert isinstance(body, dict)
    original = body["files"][0]
    body["files"] = [copy.deepcopy(original), copy.deepcopy(original)]
    body["files"][0]["path"] = "qualification/Alpha.json"
    body["files"][1]["path"] = "qualification/alpha.json"
    body["file_count"] = 2
    body["total_size_bytes"] = sum(entry["size_bytes"] for entry in body["files"])
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "case_colliding_bundle_path", "subject": "manifest"}
    ]


def test_symlink_input_is_rejected_without_following_it(
    synthetic_bundle: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    root, _manifest = synthetic_bundle
    target = root / "qualification/synthetic-summary.json"
    external = tmp_path / "outside.json"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "symlink_input", "subject": "bundle"}
    ]


def test_missing_and_added_inputs_fail_closed(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    missing = root / "provenance/synthetic-dependencies.lock"
    missing.unlink()
    missing_result = verify_bundle(root)
    assert missing_result["findings"] == [
        {"code": "missing_input", "subject": "bundle"}
    ]

    missing.write_bytes(b"synthetic-test-dependency-lock\n")
    added = root / "qualification/added.json"
    added.write_bytes(b"{}")
    added_result = verify_bundle(root)
    assert added_result["findings"] == [
        {"code": "added_input", "subject": "bundle"}
    ]


def test_modified_input_fails_sha256_verification(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    target = root / "provenance/synthetic-dependencies.lock"
    original = target.read_bytes()
    target.write_bytes(b"X" + original[1:])

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "file_digest_mismatch", "subject": "artifact:0001"}
    ]


def test_oversized_input_fails_before_hashing(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    target = root / "provenance/synthetic-dependencies.lock"
    with target.open("r+b") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "oversized_input", "subject": "artifact:0001"}
    ]


def test_unreadable_input_fails_closed(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    target = root / "provenance/synthetic-dependencies.lock"
    target.chmod(0)
    try:
        result = verify_bundle(root)
    finally:
        target.chmod(0o600)

    assert result["findings"] == [
        {"code": "unreadable_input", "subject": "artifact:0001"}
    ]


def test_concurrently_changed_input_fails_closed(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    changed = False

    def mutate_after_read(relative_path: str) -> None:
        nonlocal changed
        if relative_path == "qualification/synthetic-summary.json" and not changed:
            changed = True
            target = root / relative_path
            target.write_bytes(target.read_bytes() + b" ")
            os.utime(target, None)

    result = verify_bundle(root, _read_observer=mutate_after_read)

    assert changed is True
    assert result["findings"] == [
        {"code": "concurrently_changed_input", "subject": "artifact:0002"}
    ]


def test_already_verified_input_changed_during_later_read_fails_closed(
    synthetic_bundle: tuple[Path, dict[str, object]],
) -> None:
    root, _manifest = synthetic_bundle
    changed = False

    def mutate_earlier_file(relative_path: str) -> None:
        nonlocal changed
        if relative_path == "qualification/synthetic-summary.json" and not changed:
            changed = True
            earlier = root / "provenance/synthetic-dependencies.lock"
            earlier.write_bytes(earlier.read_bytes() + b"changed-after-verification\n")

    result = verify_bundle(root, _read_observer=mutate_earlier_file)

    assert changed is True
    assert result["findings"] == [
        {"code": "concurrently_changed_input", "subject": "bundle"}
    ]
    assert result["route_ready"] is False
    assert result["release_ready"] is False
