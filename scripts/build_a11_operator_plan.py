#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A11 operator-plan steps for the run-day window (network use only where stated).

Subcommands:

``extend``  (offline)
    ``mycelium_a11_operator_plan.extend_operator_plan``: stage the owner-authorised
    draft snapshot and the entry node's ``a11/<node_id>/node.json`` binding into a
    clone of the plan's transfer bundle (shipped to the entry node only).
``bind``  (ssh to re-measure run-scoped peer identities)
    Re-sign the extended plan's offers with the seed (the run-day bind path).

The deployment topology itself comes from ``scripts/build_a7_operator_plan.py
topology`` and ``scripts/build_qwen_live_route.py``; nothing here changes the
target route.
"""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_private(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True, indent=2)
        handle.write("\n")


def _digests(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or not name or not digest or name in result:
            raise SystemExit("--expected-sha256 expects unique FILE=HEX")
        result[name] = digest
    return result


def extend(args: argparse.Namespace) -> dict[str, Any]:
    from mycelium_a11_operator_plan import extend_operator_plan

    warm = None
    if args.warm_p50_ms is not None or args.warm_p95_ms is not None:
        if args.warm_p50_ms is None or args.warm_p95_ms is None:
            raise SystemExit("--warm-p50-ms and --warm-p95-ms go together")
        warm = {"p50": args.warm_p50_ms, "p95": args.warm_p95_ms}
    plan, summary = extend_operator_plan(
        _read(args.plan),
        out_bundle=args.out_bundle,
        draft_snapshot=args.draft_snapshot,
        model_id=args.model_id,
        resolved_commit=args.resolved_commit,
        expected_sha256=_digests(args.expected_sha256),
        width=args.width,
        quantize_bits=None if args.quantize_bits == 0 else args.quantize_bits,
        minimum_baseline_samples=args.minimum_baseline_samples,
        warm_target_tpot_ms=warm,
    )
    _write_private(args.out_plan, plan)
    if args.summary_out is not None:
        _write_private(args.summary_out, summary)
    return summary


def bind(args: argparse.Namespace) -> dict[str, Any]:
    from mycelium_a7_operator_plan import bind_extended_plan

    bound = bind_extended_plan(
        _read(args.plan), seed_root=args.seed_root, now=time.time(), refresh_identities=not args.no_identity_refresh
    )
    _write_private(args.out, bound)
    return {"stage": "bound", "output": str(args.out), "plan_id": bound["plan_id"], "run_id": bound["run_id"]}


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("extend")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--draft-snapshot", type=Path, required=True)
    p.add_argument("--model-id", default="Qwen/Qwen2.5-0.5B-Instruct")
    p.add_argument("--resolved-commit", default="7ae557604adf67be50417f59c2c2f167def9a775")
    p.add_argument("--expected-sha256", action="append", required=True, help="FILE=HEX, owner-authorised")
    p.add_argument("--width", type=int, default=4)
    p.add_argument("--quantize-bits", type=int, default=8, help="0 = unquantized draft")
    p.add_argument("--minimum-baseline-samples", type=int, default=16)
    p.add_argument("--warm-p50-ms", type=float)
    p.add_argument("--warm-p95-ms", type=float)
    p.add_argument("--out-bundle", type=Path, required=True)
    p.add_argument("--out-plan", type=Path, required=True)
    p.add_argument("--summary-out", type=Path)
    p = commands.add_parser("bind")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--seed-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--no-identity-refresh", action="store_true")
    args = root.parse_args(argv)
    result = {"extend": extend, "bind": bind}[args.command](args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
