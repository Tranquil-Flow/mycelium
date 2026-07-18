"""Verification-only command line interface for immutable evidence bundles."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .verifier import canonical_output, verify_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mycelium-release-bundle",
        description=(
            "statically verify immutable release-evidence structure without granting readiness"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser(
        "verify",
        help="verify one existing bundle; never generate or promote evidence",
    )
    verify.add_argument("bundle_root")
    verify.add_argument("--expected-manifest-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_bundle(
        args.bundle_root,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    sys.stdout.buffer.write(canonical_output(result))
    return 0 if result["ok"] is True else 1
