#!/usr/bin/env python3
"""Evaluate a local model against the measured M13/M14 serving order."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mycelium_layer_planner.contracts import (  # noqa: E402
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from mycelium_model_catalog import (  # noqa: E402
    evaluate_model_feasibility,
    scan_huggingface_cache,
    swarm_feasibility_evidence_from_document,
)
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _nodes(
    placement: dict[str, object],
    topology: dict[str, object],
    *,
    workspace_bytes: int,
) -> tuple[NodeCapability, ...]:
    node_records = placement.get("nodes")
    if not isinstance(node_records, list):
        raise ValueError("placement projection requires nodes")
    by_id = {
        record.get("node_id"): record
        for record in node_records
        if isinstance(record, dict) and isinstance(record.get("node_id"), str)
    }
    decision = topology.get("decision")
    order = decision.get("opened_order") if isinstance(decision, dict) else None
    if not isinstance(order, list) or not all(isinstance(node_id, str) for node_id in order):
        raise ValueError("topology projection requires a string opened_order")
    nodes: list[NodeCapability] = []
    for node_id in order:
        record = by_id.get(node_id)
        if not isinstance(record, dict):
            raise ValueError(f"topology node {node_id} is absent from placement evidence")
        nodes.append(
            NodeCapability(
                node_id=node_id,
                prefill_ms_per_layer_token=float(record["prefill_ms_per_layer_token"]),
                decode_ms_per_layer_token=float(record["decode_ms_per_layer_token"]),
                fast_memory_bytes=int(record["fast_allocatable_bytes"]),
                total_memory_bytes=int(record["total_allocatable_bytes"]),
                memory_bandwidth_Bps=1_000_000_000.0,
                spill_bandwidth_Bps=1_000_000_000.0,
                workspace_bytes=workspace_bytes,
            )
        )
    return tuple(nodes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hf-cache", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--placement", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--swarm-evidence", type=Path, required=True)
    parser.add_argument("--prompt-tokens", type=int, default=256)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--workspace-bytes", type=int, default=536_870_912)
    parser.add_argument("--required-decode-mode", default="complete_context_replay")
    parser.add_argument("--evaluated-at-unix-ms", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    placement = _load(args.placement)
    topology = _load(args.topology)
    evidence = swarm_feasibility_evidence_from_document(_load(args.swarm_evidence))
    candidates = [
        entry
        for entry in scan_huggingface_cache(args.hf_cache)
        if entry.model_id == args.model_id
        and (args.revision is None or entry.revision == args.revision)
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected one local snapshot for {args.model_id}, found {len(candidates)}")
    if placement.get("snapshot_generation") != evidence.placement_snapshot_generation:
        raise ValueError("placement snapshot generation differs from evidence provenance")
    if _digest(placement) != evidence.placement_digest:
        raise ValueError("placement digest differs from evidence provenance")
    if _digest(topology) != evidence.topology_digest:
        raise ValueError("topology digest differs from evidence provenance")
    report = evaluate_model_feasibility(
        candidates[0],
        ordered_nodes=_nodes(placement, topology, workspace_bytes=args.workspace_bytes),
        workload=WorkloadScenario(
            name="m17_local_model_interactive_v1",
            prompt_tokens=args.prompt_tokens,
            output_tokens=args.output_tokens,
            concurrency=args.concurrency,
        ),
        policy=PlanningPolicy(memory_reserve_fraction=0.1, objective="balanced"),
        evidence=evidence,
        evaluated_at_unix_ms=(
            args.evaluated_at_unix_ms
            if args.evaluated_at_unix_ms is not None
            else int(time.time() * 1_000)
        ),
        required_decode_mode=args.required_decode_mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "model_id": report["model_id"],
        "state": report["state"],
        "stages": report["stages"],
        "output": str(args.output),
        "provisioning_authorized": report["provisioning_authorized"],
    }, sort_keys=True))
    return 0 if report["state"] == "feasible" else 2


if __name__ == "__main__":
    raise SystemExit(main())
