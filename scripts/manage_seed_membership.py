#!/usr/bin/env python3
"""Inspect or revoke members in one owner-only durable seed authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mycelium_seed.operator import (  # noqa: E402
    SeedOperatorError,
    backup_seed_state,
    begin_seed_key_rotation,
    complete_seed_key_rotation,
    revoke_seed_member,
    restore_seed_state,
    seed_key_rotation_status,
    seed_inventory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--seed-data-dir", type=Path, required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--seed-data-dir", type=Path, required=True)
    revoke.add_argument("--node-id", required=True)
    revoke.add_argument("--expected-generation", type=int, required=True)
    revoke.add_argument("--reason", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--seed-data-dir", type=Path, required=True)
    backup.add_argument("--output-root", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup-root", type=Path, required=True)
    restore.add_argument("--target-root", type=Path, required=True)
    rotate_begin = commands.add_parser("rotate-begin")
    rotate_begin.add_argument("--seed-data-dir", type=Path, required=True)
    rotate_begin.add_argument("--reason", required=True)
    rotate_begin.add_argument("--overlap-seconds", type=float, required=True)
    rotate_status = commands.add_parser("rotate-status")
    rotate_status.add_argument("--seed-data-dir", type=Path, required=True)
    rotate_complete = commands.add_parser("rotate-complete")
    rotate_complete.add_argument("--seed-data-dir", type=Path, required=True)
    rotate_complete.add_argument("--authority-generation", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
            result = seed_inventory(args.seed_data_dir)
        elif args.command == "revoke":
            result = revoke_seed_member(
                args.seed_data_dir,
                node_id=args.node_id,
                expected_generation=args.expected_generation,
                reason=args.reason,
            )
        elif args.command == "backup":
            result = backup_seed_state(
                args.seed_data_dir,
                output_root=args.output_root,
            )
        elif args.command == "restore":
            result = restore_seed_state(
                args.backup_root,
                target_root=args.target_root,
            )
        elif args.command == "rotate-begin":
            result = begin_seed_key_rotation(
                args.seed_data_dir,
                reason=args.reason,
                overlap_seconds=args.overlap_seconds,
            )
        elif args.command == "rotate-status":
            result = seed_key_rotation_status(args.seed_data_dir)
        else:
            result = complete_seed_key_rotation(
                args.seed_data_dir,
                authority_generation=args.authority_generation,
            )
    except SeedOperatorError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
