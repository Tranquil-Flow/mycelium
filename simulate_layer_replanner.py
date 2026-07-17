from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mycelium_layer_planner.replan_simulator import simulate_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic Layer Replanner dropout/join simulations."
    )
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Write report JSON; default stdout")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = simulate_bundle(args.scenario)
        if args.compact:
            text = json.dumps(report, sort_keys=True, separators=(",", ":"))
        else:
            text = json.dumps(report, sort_keys=True, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(text)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"layer-replanner-simulator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
