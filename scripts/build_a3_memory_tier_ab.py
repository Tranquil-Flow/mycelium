#!/usr/bin/env python3
"""Build owner-private A3 memory-tier sensitivity evidence from planner snapshots."""

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

from mycelium_layer_planner.memory_tier_ab import (  # noqa: E402
    compare_memory_tier_snapshots,
)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute a one-input memory-tier A/B through the real planner."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--memory-tier-node-id", required=True)
    parser.add_argument("--binding-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = compare_memory_tier_snapshots(
        _read(args.baseline),
        _read(args.candidate),
        memory_tier_node_id=args.memory_tier_node_id,
        binding_digest=args.binding_digest,
    )
    _write_private(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "result": result["result"],
                "baseline_allocation": result["baseline_allocation"],
                "candidate_allocation": result["candidate_allocation"],
                "explored_allocation_count": result["explored_allocation_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
