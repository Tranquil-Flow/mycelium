from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .planner import plan_snapshot
from .serialization import dumps_route_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_layer_planner",
        description="Build deterministic mycelium.route_plan.v2 placement intent.",
    )
    parser.add_argument("--snapshot", required=True, type=Path, help="Fleet/model/workload snapshot JSON")
    parser.add_argument("--output", type=Path, help="Write plan JSON here; default stdout")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON instead of indented JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot JSON must be an object")
        plan = plan_snapshot(snapshot)
        text = dumps_route_plan(plan, pretty=not args.compact)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"layer-planner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
