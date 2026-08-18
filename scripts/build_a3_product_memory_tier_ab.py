#!/usr/bin/env python3
"""Execute an A3 memory-only counterfactual through the product exact-weight DP."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.memory_tier_ab import (  # noqa: E402
    compare_product_memory_tier_operations,
)
from mycelium_live.model_capacity import recompute_model_operation  # noqa: E402


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-observations", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--memory-tier-node-id", required=True)
    parser.add_argument("--candidate-fast-memory-bytes", type=int, required=True)
    parser.add_argument("--evaluated-at-unix-ms", type=int, required=True)
    parser.add_argument("--binding-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline = _read(args.live_observations)
    candidate = copy.deepcopy(baseline)
    nodes = candidate.get("placement", {}).get("nodes", [])
    matches = [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("node_id") == args.memory_tier_node_id
    ]
    if len(matches) != 1 or args.candidate_fast_memory_bytes <= 0:
        raise ValueError("memory-tier counterfactual target is invalid")
    matches[0]["fast_allocatable_bytes"] = args.candidate_fast_memory_bytes
    common = {
        "cache_root": args.cache_root,
        "evaluated_at_unix_ms": args.evaluated_at_unix_ms,
    }
    baseline_operation = recompute_model_operation(
        live_observations=baseline,
        **common,
    )
    candidate_operation = recompute_model_operation(
        live_observations=candidate,
        **common,
    )
    result = compare_product_memory_tier_operations(
        baseline,
        candidate,
        baseline_operation,
        candidate_operation,
        model_id=args.model_id,
        revision=args.revision,
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
                "evidence_digest": result["evidence_digest"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
