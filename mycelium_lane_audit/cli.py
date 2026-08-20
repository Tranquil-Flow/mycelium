from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .audit import AuditError, audit_repository, canonical_json
from .manifest import ManifestError, load_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.14 -m mycelium_lane_audit",
        description="Read-only structural audit of isolated Git feature lanes.",
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        report = audit_repository(args.repo_root, manifest)
    except (ManifestError, AuditError) as exc:
        print(f"lane audit error: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(report))
    return 0 if report["ownership_safe_to_dispatch"] else 1
