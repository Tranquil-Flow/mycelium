#!/usr/bin/env python3
"""Build one privacy-reduced M15 policy matrix from a frozen planner snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.workload import (  # noqa: E402
    empirical_interactive_chat,
    sustained_batch,
)
from mycelium_layer_planner.workload_intelligence import (  # noqa: E402
    build_m15_plan_comparison,
)


def _read_snapshot(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("planner_snapshot_unsafe")
    path = path.resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("planner_snapshot_unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("planner_snapshot_invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("planner_snapshot_invalid")
    return document


def _atomic_write(path: Path, document: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--planner-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interactive-concurrency", type=int, nargs="+", default=(1, 2, 4))
    parser.add_argument("--batch-concurrency", type=int, nargs="+", default=(1, 4))
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()

    snapshot = _read_snapshot(args.planner_snapshot)
    comparison = build_m15_plan_comparison(
        snapshot,
        (
            empirical_interactive_chat(
                concurrency_points=tuple(args.interactive_concurrency)
            ),
            sustained_batch(
                concurrency_points=tuple(args.batch_concurrency),
                batch_size=args.batch_size,
            ),
        ),
    )
    _atomic_write(args.output, comparison)
    print(
        json.dumps(
            {
                "protocol": comparison["protocol"],
                "output": str(args.output.resolve()),
                "planner_snapshot_digest": comparison["planner_snapshot_digest"],
                "profiles": [item["profile_id"] for item in comparison["profiles"]],
                "selected_candidates": [
                    item["selected_candidate_id"]
                    for item in comparison["comparisons"]
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
