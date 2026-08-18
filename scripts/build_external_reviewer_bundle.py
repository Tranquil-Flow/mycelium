#!/usr/bin/env python3
"""Build the credential-free, no-checkout external reviewer runtime bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_reviewer_bundle import build_reviewer_bundle, verify_reviewer_bundle  # noqa: E402
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402


def _runtime_files(sidecar: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(ROOT.iterdir()):
        if path.is_file() and path.suffix == ".py":
            files[path.name] = path
        elif path.is_dir() and path.name.startswith("mycelium_"):
            for child in sorted(path.rglob("*.py")):
                if "__pycache__" not in child.parts:
                    files[str(child.relative_to(ROOT))] = child
    for relative in (
        "scripts/external_reviewer_preflight.py",
        "scripts/package_m22_service.py",
        "release/python-requirements.lock",
        "docs/release/external-mac-reviewer.md",
    ):
        files[relative] = ROOT / relative
    files["bin/mycelium-iroh-sidecar"] = sidecar
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="external-mac-reviewer-m22-1")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        result = verify_reviewer_bundle(args.output)
        print(
            json.dumps(
                {
                    "protocol": result["protocol"],
                    "version": result["version"],
                    "manifest_digest": result["manifest_digest"],
                },
                sort_keys=True,
            )
        )
        return 0
    result = build_reviewer_bundle(
        version=args.version, files=_runtime_files(args.sidecar), output=args.output
    )
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest_path.write_bytes(canonical_json_bytes(result))
    print(
        json.dumps(
            {
                "protocol": result["protocol"],
                "version": result["version"],
                "components": len(result["components"]),
                "manifest_digest": result["manifest_digest"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
