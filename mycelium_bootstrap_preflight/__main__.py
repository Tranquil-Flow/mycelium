"""CLI for the deterministic Mycelium bootstrap preflight."""
from __future__ import annotations

import argparse
from pathlib import Path

from .core import canonical_json, run_preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_preflight(args.root)
    print(canonical_json(report), end="")
    return 0 if report["preflight_ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
