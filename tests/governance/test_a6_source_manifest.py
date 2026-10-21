# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.build_a6_source_manifest import (
    build_source_manifest,
    verify_source_manifest,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_build_and_verify_exact_a6_source_manifest(tmp_path: Path) -> None:
    (tmp_path / "runtime.py").write_text("runtime\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_runtime.py").write_text("test\n")

    manifest = build_source_manifest(
        root=tmp_path,
        base_commit="1fd3a6f8733fea52e55e0fbcbf46e86fa7bed03f",
        paths=("tests/test_runtime.py", "runtime.py"),
    )

    assert manifest["protocol"] == "mycelium.a6_source_manifest.v1"
    assert [entry["path"] for entry in manifest["files"]] == [
        "runtime.py",
        "tests/test_runtime.py",
    ]
    assert manifest["files"][0] == {
        "path": "runtime.py",
        "size_bytes": 8,
        "sha256": _sha256(b"runtime\n"),
    }
    assert manifest["source_digest"].startswith("sha256:")
    assert verify_source_manifest(
        root=tmp_path,
        manifest=manifest,
        expected_paths=("runtime.py", "tests/test_runtime.py"),
    ) == manifest["source_digest"]


def test_verify_rejects_path_set_or_byte_drift(tmp_path: Path) -> None:
    (tmp_path / "runtime.py").write_text("runtime\n")
    manifest = build_source_manifest(
        root=tmp_path,
        base_commit="1fd3a6f8733fea52e55e0fbcbf46e86fa7bed03f",
        paths=("runtime.py",),
    )

    try:
        verify_source_manifest(
            root=tmp_path,
            manifest=manifest,
            expected_paths=("runtime.py", "missing.py"),
        )
    except ValueError as error:
        assert str(error) == "a6_source_manifest_path_set_mismatch"
    else:
        raise AssertionError("path drift must fail")

    (tmp_path / "runtime.py").write_text("changed\n")
    try:
        verify_source_manifest(
            root=tmp_path,
            manifest=manifest,
            expected_paths=("runtime.py",),
        )
    except ValueError as error:
        assert str(error) == "a6_source_manifest_file_mismatch:runtime.py"
    else:
        raise AssertionError("byte drift must fail")


def test_manifest_digest_is_canonical() -> None:
    payload = {
        "protocol": "mycelium.a6_source_manifest.v1",
        "base_commit": "a" * 40,
        "files": [],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert _sha256(encoded).startswith("sha256:")
