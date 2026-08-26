# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate or audit the closed A8 source-manifest candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "handover" / "a8-source-manifest.v1.json"
SOURCE_PATTERNS = (
    "*.py",
    "mycelium_*/*.py",
    "mycelium_*/**/*.py",
    "scripts/*.py",
    "scripts/**/*.py",
    "scripts/*.mjs",
    "scripts/**/*.mjs",
    "tests/*.py",
    "tests/**/*.py",
    "contracts/**/*.json",
    "native/iroh_transport/Cargo.toml",
    "native/iroh_transport/Cargo.lock",
    "native/iroh_transport/src/**/*.rs",
    "native/iroh_transport/tests/**/*.rs",
    "ui/web/index.html",
    "ui/web/package.json",
    "ui/web/package-lock.json",
    "ui/web/*.config.ts",
    "ui/web/tsconfig*.json",
    "ui/web/scripts/**/*.mjs",
    "ui/web/e2e/**/*.ts",
    "ui/web/src/**/*.css",
    "ui/web/src/**/*.ts",
    "ui/web/src/**/*.tsx",
)


def source_paths(root: Path = ROOT) -> tuple[str, ...]:
    """Return complete deterministic A8 implementation and test source closure."""
    paths: set[str] = set()
    for pattern in SOURCE_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file() or path.is_symlink():
                paths.add(path.relative_to(root).as_posix())
    return tuple(sorted(paths))


REQUIRED_PATHS = source_paths(ROOT)


def _base_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def build_manifest(root: Path = ROOT) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    required_paths = source_paths(root)
    if tuple(sorted(set(required_paths))) != required_paths:
        raise ValueError("A8 source path inventory must be sorted and unique")
    for relative in required_paths:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing or unsafe A8 source path: {relative}")
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return {
        "base_commit": _base_commit(root),
        "files": files,
        "protocol": "mycelium.a8_source_manifest.v1",
    }


def manifest_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def manifest_digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(manifest_bytes(document)).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    expected = build_manifest(ROOT)
    if args.write:
        args.manifest.write_bytes(manifest_bytes(expected))
    else:
        actual = json.loads(args.manifest.read_text("utf-8"))
        if actual != expected:
            raise SystemExit("A8 source manifest drift")
    print(
        json.dumps(
            {
                "file_count": len(expected["files"]),
                "source_digest": manifest_digest(expected),
                "status": "written" if args.write else "verified",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
