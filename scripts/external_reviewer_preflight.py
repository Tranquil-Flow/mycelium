#!/usr/bin/env python3
"""Run the read-only Mycelium preflight for an invited external Mac."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_reviewer_preflight import reviewer_preflight  # noqa: E402


def _object(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("reviewer_preflight_input_invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invite-bundle", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--artifact-requirements", type=Path)
    parser.add_argument("--required-memory-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--required-disk-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = reviewer_preflight(
            invite_bundle=_object(args.invite_bundle),
            state_root=args.state_root,
            artifact_requirements=None
            if args.artifact_requirements is None
            else _object(args.artifact_requirements),
            required_memory_bytes=args.required_memory_bytes,
            required_disk_bytes=args.required_disk_bytes,
        )
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 2
    raw = canonical_json_bytes(result)
    if args.output is None:
        sys.stdout.buffer.write(raw + b"\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
