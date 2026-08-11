#!/usr/bin/env python3
"""Validate M22 claims, derive the release gate, and write canonical evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_m22_release import build_release_evidence  # noqa: E402
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        claims = json.loads(args.claims.read_text("utf-8"))
        if not isinstance(claims, dict):
            raise ValueError("m22_release_claims_invalid")
        evidence = build_release_evidence(**claims)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(evidence))
    print(
        json.dumps(
            {
                "evidence_digest": evidence["evidence_digest"],
                "gate_state": evidence["gate_state"],
                "output": str(args.output),
                "protocol": evidence["protocol"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
