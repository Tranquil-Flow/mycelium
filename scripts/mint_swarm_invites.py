#!/usr/bin/env python3
"""Mint owner-only native-node invite bundles from one verified durable seed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mycelium_seed.invite_batch import (  # noqa: E402
    InviteBatchError,
    mint_invite_batch,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-data-dir", type=Path, required=True)
    parser.add_argument("--seed-url", required=True)
    parser.add_argument("--swarm-id", default="mycelium-swarm")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        status = mint_invite_batch(
            seed_data_dir=args.seed_data_dir,
            seed_url=args.seed_url,
            swarm_id=args.swarm_id,
            output_root=args.output_root,
            count=args.count,
            ttl_seconds=args.ttl_seconds,
        )
    except InviteBatchError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
