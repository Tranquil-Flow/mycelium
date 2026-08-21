#!/usr/bin/env python3
"""Run one A8 physical gate or emit an inert preflight envelope (spec §11-§12).

Exit codes: 0 success; 2 bounded failure (PhysicalGateError/PeerRequired);
1 unexpected failure. Nothing here fabricates a result: cases that need
infrastructure or a peer fail closed and never write evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_internet.physical import (  # noqa: E402 - path bootstrap above
    A8_PHYSICAL_CASES,
    PhysicalGateError,
    execute_case,
    preflight_document,
    seal_qualification,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a8_run_physical_gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--spec-digest", required=True)
    preflight.add_argument("--source-digest", required=True)
    preflight.add_argument("--now-unix-ms", type=int)

    run = subparsers.add_parser("run")
    run.add_argument("case_id", choices=sorted(A8_PHYSICAL_CASES))
    run.add_argument("--origin", required=True)
    run.add_argument("--evidence-root", type=Path)
    run.add_argument("--seal", action="store_true")
    run.add_argument("--spec-digest", default="sha256:" + "0" * 64)
    run.add_argument("--source-digest", default="sha256:" + "0" * 64)

    cases = subparsers.add_parser("cases")
    cases.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cases":
        listing = sorted(A8_PHYSICAL_CASES)
        if args.as_json:
            print(json.dumps(listing))
        else:
            print("\n".join(listing))
        return 0
    if args.command == "preflight":
        now_unix_ms = args.now_unix_ms
        if now_unix_ms is None:
            from time import time

            now_unix_ms = int(time() * 1000)
        document = preflight_document(
            now_unix_ms=now_unix_ms,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
        print(json.dumps(document, sort_keys=True))
        return 0
    try:
        document = execute_case(
            args.case_id,
            origin=args.origin,
            evidence_root=args.evidence_root,
            adapter=None,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
    except PhysicalGateError as exc:
        print(f"gate rejected: {exc.code}", file=sys.stderr)
        return 2
    print(json.dumps(document, sort_keys=True))
    if args.seal:
        if args.evidence_root is None:
            print("gate rejected: evidence_root_unsafe", file=sys.stderr)
            return 2
        try:
            record = seal_qualification(
                document,
                evidence_root=args.evidence_root,
            )
        except PhysicalGateError as exc:
            print(f"gate rejected: {exc.code}", file=sys.stderr)
            return 2
        print(f"sealed: {record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
