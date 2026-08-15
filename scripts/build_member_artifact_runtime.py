#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Build and verify the isolated member artifact-acquisition runtime closure."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_member_runtime import build_member_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_member_runtime(
        repo_root=args.repo_root,
        output_root=args.output_root,
        manifest_path=args.manifest,
    )
    print(
        json.dumps(
            {
                "protocol": "mycelium.member_runtime_build.v1",
                "file_count": len(manifest["files"]),
                "manifest": str(args.manifest),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
