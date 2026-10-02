#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-host standby parity challenge runner for the A7 KV-successor gate.

Drives one physical parity measurement between a source placement (node-b)
and a STANDBY placement (node-a): the same encoded work item is executed on
both, and the outputs must be byte-identical.  The result is sealed as a
closed ``mycelium.a7_standby_parity_challenge.v1`` document under
``<records>/parity/<source>-<standby>-<n>.json`` (0600).

Modes
-----
``--plan PLAN.json --records DIR --case CASE_NAME``
    Validate the plan, refuse on unknown case / missing plan / pre-existing
    record / missing fleet grant.  The live cross-host measurement path is
    a documented plug-in point: for this revision the runner refuses the
    physical execution with ``physical_parity_runner_not_implemented`` and
    exits 2 (no record), while the document-sealing path is fully wired and
    tested.  The plug-in point is owner-authorised later.

Every refusal exits 2 and writes nothing.  Success seals exactly one record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_a7_standby_qualification import (  # noqa: E402
    physical_parity_challenge,
    validate_physical_parity_challenge,
)
from run_a7_kv_successor_gate import (  # noqa: E402
    CASES,
    EXACT_FLEET_GRANT,
    _canonical,
    _sha,
    _verify_plan,
)

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PHYSICAL_MEASUREMENT_NOT_IMPLEMENTED = "physical_parity_runner_not_implemented"


def _fail(code: str) -> None:
    raise ValueError(code)


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _write_private(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(document) + b"\n")


def _load_plan(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        _fail("a7_parity_plan_invalid")
    return _verify_plan(_read_json(path))


def _refuse(code: str, *, plan: Mapping[str, Any], case: str) -> None:
    """Seal a refusal document; exit 2, no parity record."""
    print(
        json.dumps(
            {
                "case": case,
                "candidate_commit": plan["candidate_commit"],
                "plan_digest": plan["plan_digest"],
                "refusal": code,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    raise SystemExit(2)


def run(
    *,
    plan_path: Path,
    records_dir: Path,
    case: str,
    fleet_grant: str | None,
) -> int:
    plan = _load_plan(plan_path)
    if case not in CASES:
        _fail("a7_parity_case_unknown")
    parity_dir = records_dir / "parity"
    existing = sorted(parity_dir.glob(f"*-{case}.json")) if parity_dir.is_dir() else []
    if existing:
        _fail("a7_parity_record_exists")

    if fleet_grant != EXACT_FLEET_GRANT:
        _refuse("a7_parity_fleet_grant_missing", plan=plan, case=case)

    # --- live cross-host measurement (owner-authorised plug-in point) ------
    # The physical execution path is intentionally not implemented in this
    # revision: driving two live physical_inference_node.py processes
    # cross-host requires the owner's fleet grant at gate time and the
    # standby port to be listening.  The document-sealing path below is
    # fully wired; only the measurement source is deferred.
    _refuse(PHYSICAL_MEASUREMENT_NOT_IMPLEMENTED, plan=plan, case=case)

    # unreachable in this revision; kept as the contract for the plug-in
    # return 0  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--case", required=True)
    args = parser.parse_args(argv)
    try:
        return run(
            plan_path=args.plan,
            records_dir=args.records,
            case=args.case,
            fleet_grant=os.environ.get("MYCELIUM_A7_FLEET_GRANT"),
        )
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
