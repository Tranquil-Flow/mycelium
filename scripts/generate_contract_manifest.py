#!/usr/bin/env python3
"""Generate deterministic hash pins for executable cross-component contracts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .contract_io import atomic_write_under_root, read_under_root
    from .contract_registry import CONTRACT_SPECS
else:
    from contract_io import atomic_write_under_root, read_under_root
    from contract_registry import CONTRACT_SPECS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts" / "contract-manifest.v1.json"


def pin(relative_path: str) -> dict[str, Any]:
    content = read_under_root(ROOT, ROOT / relative_path)
    return {
        "path": relative_path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def document() -> dict[str, Any]:
    contracts = []
    for spec in sorted(CONTRACT_SPECS, key=lambda item: item.fixture_name):
        relative_fixture = f"contracts/compatibility-fixtures/{spec.fixture_name}"
        fixture_data = json.loads(read_under_root(ROOT, ROOT / relative_fixture))
        if fixture_data.get("protocol") != spec.protocol:
            raise ValueError(
                f"fixture protocol mismatch for {spec.fixture_name}: "
                f"expected {spec.protocol!r}, got {fixture_data.get('protocol')!r}"
            )
        contracts.append(
            {
                "protocol": spec.protocol,
                "fixture": pin(relative_fixture),
                "owner_sources": [pin(source) for source in spec.owner_sources],
            }
        )
    return {
        "protocol": "mycelium.contract_manifest.v1",
        "claim_boundary": "hash-pinned executable contract fixtures and owning source files; not runtime qualification evidence",
        "contracts": contracts,
    }


def encoded() -> bytes:
    return (json.dumps(document(), sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = encoded()
    if args.check:
        try:
            actual = read_under_root(ROOT, MANIFEST)
        except ValueError:
            actual = None
        if actual != expected:
            print("contract manifest drift", file=sys.stderr)
            return 1
        print("contract manifest verified")
        return 0
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_under_root(ROOT, MANIFEST, expected)
    print(f"contract manifest generated: {len(CONTRACT_SPECS)} contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
