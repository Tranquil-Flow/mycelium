#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build and verify the exact A6 behavior-bearing source candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

PROTOCOL = "mycelium.a6_source_manifest.v1"
DEFAULT_OUTPUT = "artifacts/a6/source-manifest-v1.json"
# Completion/handover bytes change only after exact-candidate qualification and
# would create a recursive evidence binding. All implementation, generated
# contract, fixture, test, and harness paths remain included.
METADATA_EXCLUSIONS = frozenset(
    {
        DEFAULT_OUTPUT,
        "docs/handover/A6_PROGRESS_2026-08-21.md",
    }
)
IGNORED_PARTS = frozenset({"__pycache__", "node_modules", "target", "dist"})


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _validate_relative_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw != path.as_posix():
        raise ValueError(f"a6_source_manifest_path_invalid:{raw}")
    return raw


def _included(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return path not in METADATA_EXCLUSIONS and not any(
        part in IGNORED_PARTS for part in parts
    )


def changed_source_paths(root: Path, *, base_commit: str) -> tuple[str, ...]:
    """Derive the candidate path set independently from Git state."""

    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", base_commit, "--"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    paths = {_validate_relative_path(item) for item in (*tracked, *untracked) if item}
    return tuple(sorted(path for path in paths if _included(path)))


def build_source_manifest(
    *,
    root: Path,
    base_commit: str,
    paths: Iterable[str],
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in sorted({_validate_relative_path(path) for path in paths}):
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"a6_source_manifest_file_invalid:{relative}")
        content = candidate.read_bytes()
        files.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": _sha256(content),
            }
        )
    body: dict[str, Any] = {
        "protocol": PROTOCOL,
        "base_commit": base_commit,
        "files": files,
    }
    return {**body, "source_digest": _sha256(_canonical(body))}


def verify_source_manifest(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    expected_paths: Iterable[str],
) -> str:
    if manifest.get("protocol") != PROTOCOL:
        raise ValueError("a6_source_manifest_protocol_invalid")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("a6_source_manifest_files_invalid")
    listed_paths = [entry.get("path") for entry in files if isinstance(entry, Mapping)]
    expected = sorted({_validate_relative_path(path) for path in expected_paths})
    if listed_paths != expected or len(files) != len(expected):
        raise ValueError("a6_source_manifest_path_set_mismatch")
    rebuilt = build_source_manifest(
        root=root,
        base_commit=str(manifest.get("base_commit", "")),
        paths=expected,
    )
    for actual, recorded in zip(rebuilt["files"], files, strict=True):
        if actual != recorded:
            raise ValueError(f"a6_source_manifest_file_mismatch:{actual['path']}")
    if rebuilt["source_digest"] != manifest.get("source_digest"):
        raise ValueError("a6_source_manifest_digest_mismatch")
    return rebuilt["source_digest"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    output = root / _validate_relative_path(args.output)
    expected_paths = changed_source_paths(root, base_commit=args.base)
    if args.check:
        manifest = json.loads(output.read_text())
        digest = verify_source_manifest(
            root=root,
            manifest=manifest,
            expected_paths=expected_paths,
        )
        print(f"A6 source manifest verified: {len(expected_paths)} files {digest}")
        return 0
    manifest = build_source_manifest(
        root=root,
        base_commit=args.base,
        paths=expected_paths,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"A6 source manifest written: {len(expected_paths)} files {manifest['source_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
