#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bind a gate's roles to the eligible members present, or refuse by name.

Reads the seed membership (``--seed-db`` read-only, or a ``--members`` JSON
list of ``SeedCoordinator.members()`` projections) and the members' signed
capability reports (``--reports``: JSON files, each one report or a list), and
binds the pinned requirements spec of ``--gate``.  Network-free; contacts no
device.

Outputs: ``--binding-out`` (owner-private, names node ids, mode 0600) and
``--receipt-out`` (public, device classes and digests only).  Exit 0 bound,
2 refused (the refusal JSON names ``requirements_unsatisfied:<role>`` and
every member's reasons by member digest), 1 invalid input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_gate_requirements.binding import (  # noqa: E402
    GateBindingError,
    GateRequirementsUnsatisfied,
    bind_roles,
    members_from_seed_database,
)
from mycelium_gate_requirements.evidence import forbidden_values, public_receipt  # noqa: E402
from mycelium_gate_requirements.spec import GateSpecError, load_pinned_spec  # noqa: E402


def _reports(paths: list[Path]) -> list[dict]:
    found: list[dict] = []
    for path in paths:
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for item in files:
            value = json.loads(item.read_text("utf-8"))
            found.extend(value if isinstance(value, list) else [value])
    return found


def _write_private(path: Path, document: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gate", required=True, help="gate id with a pinned spec, e.g. A6")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--seed-db", type=Path, help="seed state.sqlite3 (opened read-only)")
    source.add_argument("--members", type=Path, help="JSON list of seed member projections")
    parser.add_argument("--reports", type=Path, action="append", default=[], required=True)
    parser.add_argument("--now", type=float, default=None, help="evaluation time (default: now)")
    parser.add_argument("--binding-out", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = load_pinned_spec(args.gate)
        members = (members_from_seed_database(args.seed_db) if args.seed_db is not None
                   else json.loads(args.members.read_text("utf-8")))
        binding = bind_roles(spec, members=members, reports=_reports(args.reports),
                             now=time.time() if args.now is None else args.now)
    except GateRequirementsUnsatisfied as refusal:
        print(json.dumps({"gate_id": args.gate, "bound": False, "reasons": refusal.reasons,
                          "detail": refusal.detail}, indent=2, sort_keys=True))
        return 2
    except (GateSpecError, GateBindingError, OSError, ValueError) as error:
        print(json.dumps({"gate_id": args.gate, "error": str(error)}), file=sys.stderr)
        return 1
    receipt = public_receipt(binding)
    from mycelium_gate_requirements.evidence import assert_host_free

    assert_host_free(receipt, forbidden_values=forbidden_values(members))
    if args.binding_out is not None:
        _write_private(args.binding_out, binding)
    if args.receipt_out is not None:
        _write_private(args.receipt_out, receipt)
    print(json.dumps({"gate_id": args.gate, "bound": True, "receipt": receipt}, indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
