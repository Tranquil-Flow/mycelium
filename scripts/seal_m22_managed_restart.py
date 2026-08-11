#!/usr/bin/env python3
"""Validate managed-restart claims and write canonical privacy-reduced evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_service_restart_evidence import (  # noqa: E402
    build_managed_restart_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        claims = json.loads(args.claims.read_text("utf-8"))
        if not isinstance(claims, dict):
            raise ValueError("managed_service_restart_claims_invalid")
        evidence = build_managed_restart_evidence(**claims)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(evidence))
    print(
        json.dumps(
            {
                "evidence_digest": evidence["evidence_digest"],
                "output": str(args.output),
                "protocol": evidence["protocol"],
                "verified": evidence["verified"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
