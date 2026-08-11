#!/usr/bin/env python3
"""Run every static governance authority required before Mycelium promotion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .claim_boundary_audit import audit_repository
    from .contract_audit import audit as audit_contracts
    from .governance_audit import audit as audit_governance
else:
    from claim_boundary_audit import audit_repository
    from contract_audit import audit as audit_contracts
    from governance_audit import audit as audit_governance


PROTOCOL = "mycelium.governance_gate.v1"


def run(repo_root: str | Path) -> dict[str, object]:
    root = Path(repo_root).resolve(strict=True)
    governance = audit_governance(root)
    claims = audit_repository(root)
    contracts = audit_contracts(root / "contracts" / "contract-manifest.v1.json")
    children = {
        "claim_boundary": {
            "ok": claims["ok"],
            "protocol": claims["protocol"],
            "finding_count": len(claims["findings"]),
        },
        "contracts": {
            "ok": contracts["ok"],
            "protocol": "mycelium.contract_audit.v1",
            "finding_count": len(contracts["drift"]),
        },
        "governance": {
            "ok": governance["ok"],
            "protocol": governance["protocol"],
            "finding_count": len(governance["findings"]),
            "ledger_digest": governance["ledger_digest"],
        },
    }
    return {
        "children": children,
        "ok": all(item["ok"] is True for item in children.values()),
        "protocol": PROTOCOL,
        "release_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run(args.repo_root)
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    elif result["ok"]:
        print("governance gate OK: ledger, claim boundary, and contracts")
    else:
        for name, child in result["children"].items():
            if child["ok"] is not True:
                print(f"{name} failed ({child['finding_count']} findings)")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
