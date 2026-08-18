from __future__ import annotations

import copy
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from mycelium_layer_planner.memory_tier_ab import (
    compare_memory_tier_snapshots,
    compare_product_memory_tier_operations,
)


DIGEST = "sha256:" + "a" * 64
SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "build_a3_memory_tier_ab.py"
)


def _snapshot() -> dict:
    nodes = [
        {
            "node_id": "fast",
            "prefill_ms_per_layer_token": 0.01,
            "decode_ms_per_layer_token": 0.01,
            "fast_memory_bytes": 90_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 50_000_000,
        },
        {
            "node_id": "tiered",
            "prefill_ms_per_layer_token": 0.01,
            "decode_ms_per_layer_token": 0.01,
            "fast_memory_bytes": 90_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 1_000_000_000,
            "spill_bandwidth_Bps": 1_000_000,
        },
    ]
    links = [
        {
            "src": "fast",
            "dst": "tiered",
            "rtt_ms": 1,
            "jitter_ms": 0.1,
            "bandwidth_Bps": 100_000_000,
        },
        {
            "src": "tiered",
            "dst": "fast",
            "rtt_ms": 1,
            "jitter_ms": 0.1,
            "bandwidth_Bps": 100_000_000,
        },
    ]
    return {
        "model": {
            "model_id": "Qwen/test",
            "revision": "a" * 40,
            "weight_digest": DIGEST,
            "architecture": "qwen2",
            "num_layers": 4,
            "hidden_size": 128,
            "dtype_bytes": 2,
            "kv_heads": 2,
            "head_dim": 32,
            "weight_bytes": 160_000_000,
        },
        "nodes": nodes,
        "links": links,
        "admitted_node_ids": ["fast", "tiered"],
        "workload": {
            "scenarios": [
                {
                    "name": "memory-ab",
                    "prompt_tokens": 4,
                    "output_tokens": 2,
                    "concurrency": 1,
                }
            ]
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 0,
            "ttft_slo_ms": 1_000_000,
            "tpot_slo_ms": 1_000_000,
        },
    }


def test_memory_only_ab_runs_real_planner_and_traces_allocation_change() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["nodes"][1]["fast_memory_bytes"] = 45_000_000

    result = compare_memory_tier_snapshots(
        baseline,
        candidate,
        memory_tier_node_id="tiered",
        binding_digest=DIGEST,
    )

    assert result["protocol"] == "mycelium.a3_memory_tier_ab.v1"
    assert result["changed_fields"] == ["nodes.tiered.fast_memory_bytes"]
    assert result["baseline_snapshot_digest"] != result["candidate_snapshot_digest"]
    assert result["explored_allocation_count"] == 6
    assert result["result"] == "allocation_changed"
    assert result["baseline_allocation"] != result["candidate_allocation"]
    assert any(
        item["node_id"] == "tiered"
        and item["reason"] == "fast_memory_spill_pressure"
        for item in result["rejected_intervals"]
    )


def test_memory_only_ab_rejects_any_second_changed_input() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["nodes"][1]["fast_memory_bytes"] = 45_000_000
    candidate["nodes"][1]["decode_ms_per_layer_token"] = 0.02

    with pytest.raises(ValueError, match="other than one fast-memory tier"):
        compare_memory_tier_snapshots(
            baseline,
            candidate,
            memory_tier_node_id="tiered",
            binding_digest=DIGEST,
        )


def test_memory_only_ab_seals_stability_proof_when_plan_does_not_move() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["nodes"][1]["fast_memory_bytes"] = 89_000_000

    result = compare_memory_tier_snapshots(
        baseline,
        candidate,
        memory_tier_node_id="tiered",
        binding_digest=DIGEST,
    )

    assert result["result"] == "allocation_stable"
    assert result["stability_proof"]["kind"] in {
        "unique_minimum",
        "all_alternatives_dominated",
    }
    assert result["stability_proof"]["evaluated_alternative_count"] > 0
    assert result["stability_proof"]["proof_digest"].startswith("sha256:")


def test_memory_only_ab_cli_writes_owner_private_evidence(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "evidence.json"
    baseline_value = _snapshot()
    candidate_value = copy.deepcopy(baseline_value)
    candidate_value["nodes"][1]["fast_memory_bytes"] = 45_000_000
    baseline.write_text(json.dumps(baseline_value), encoding="utf-8")
    candidate.write_text(json.dumps(candidate_value), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--memory-tier-node-id",
            "tiered",
            "--binding-digest",
            DIGEST,
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["result"] == "allocation_changed"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def _product_operation(*, candidate: bool) -> dict:
    allocation = (("fast", 0, 3, 90), ("tiered", 3, 4, 40)) if candidate else (
        ("fast", 0, 2, 60),
        ("tiered", 2, 4, 80),
    )
    return {
        "protocol": "mycelium.model_operation.v1",
        "operation_digest": "sha256:" + ("2" if candidate else "1") * 64,
        "catalog": {
            "entries": [
                {
                    "model_id": "Qwen/test",
                    "revision": "a" * 40,
                    "artifact_digest": DIGEST,
                    "num_layers": 4,
                }
            ]
        },
        "feasibility_reports": [
            {
                "model_id": "Qwen/test",
                "revision": "a" * 40,
                "artifact_digest": DIGEST,
                "source_quantization": "bfloat16",
                "serving_quantization": "int8-weight-only",
                "serving_dtype": "float32",
                "representation_digest": "sha256:" + "b" * 64,
                "workload": {"name": "live_interactive_capacity_v1"},
                "planner": "capability_aware_contiguous_exact_weight_dp",
                "state": "feasible",
                "stages": [
                    {
                        "node_id": node_id,
                        "start_layer": start,
                        "end_layer_exclusive": end,
                        "required_memory_bytes": required,
                        "spill_bytes": 0,
                        "headroom_bytes": 100,
                    }
                    for node_id, start, end, required in allocation
                ],
                "bottleneck_service_work_ms": 2.0 if candidate else 1.0,
                "rejected_candidates": [],
                "feasibility_digest": "sha256:"
                + ("4" if candidate else "3") * 64,
            }
        ],
    }


def test_product_memory_ab_seals_exact_weight_dp_allocation_change() -> None:
    baseline = {
        "protocol": "mycelium.live_swarm_resource_observations.v1",
        "placement": {
            "nodes": [
                {"node_id": "fast", "fast_allocatable_bytes": 200},
                {"node_id": "tiered", "fast_allocatable_bytes": 100},
            ]
        },
        "topology": {"decision": {"opened_order": ["fast", "tiered"]}},
        "signed_snapshots": [{"digest": DIGEST}],
    }
    candidate = copy.deepcopy(baseline)
    candidate["placement"]["nodes"][1]["fast_allocatable_bytes"] = 60

    result = compare_product_memory_tier_operations(
        baseline,
        candidate,
        _product_operation(candidate=False),
        _product_operation(candidate=True),
        model_id="Qwen/test",
        revision="a" * 40,
        memory_tier_node_id="tiered",
        binding_digest=DIGEST,
    )

    assert result["protocol"] == "mycelium.a3_product_memory_tier_ab.v1"
    assert result["result"] == "allocation_changed"
    assert result["explored_allocation_count"] == 6
    assert result["counterfactual_baseline_allocation_pressure"] == {
        "node_id": "tiered",
        "reason": "fast_memory_spill_pressure",
        "required_memory_bytes": 80,
        "candidate_fast_memory_bytes": 60,
        "spill_bytes": 20,
    }
    assert result["evidence_digest"].startswith("sha256:")
