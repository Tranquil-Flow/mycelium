from __future__ import annotations

import copy
import importlib.util
import json
import stat
import subprocess
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_a2_product_gate.py"
SPEC = importlib.util.spec_from_file_location("run_a2_product_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def _acquisition(*, generation: int, warm: bool) -> dict:
    total = 300
    return {
        "protocol": "mycelium.swarm_artifact_acquisition.v1",
        "generation": generation,
        "acquisition_id": f"acquisition-{generation}",
        "state": "ready",
        "phase": None,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "e" * 40,
        "representation": "int8-weight-only",
        "assignment_id": "assignment-a2",
        "placement_id": "placement-001",
        "stage_id": "stage-001",
        "layer_start": 23,
        "layer_end_exclusive": 24,
        "total_bytes": total,
        "cached_verified_bytes": total if warm else 0,
        "transferred_verified_bytes": 0 if warm else total,
        "missing_bytes": 0,
        "quarantined_bytes": 0,
        "duplicate_bytes_prevented": total if warm else 0,
        "eligible_source_count": 2,
        "active_source_count": 0,
        "sources": [
            {
                "source_ref": "source-000000000001",
                "state": "eligible" if warm else "rotated",
                "verified_bytes": 0 if warm else 120,
            },
            {
                "source_ref": "source-000000000002",
                "state": "eligible" if warm else "rotated",
                "verified_bytes": 0 if warm else 180,
            },
        ],
        "origin_bytes": 0,
        "aggregate_bytes_per_second": 0.0 if warm else 100.0,
        "eta_seconds": 0.0,
        "chunk_count": 3,
        "verified_chunk_count": 3,
        "resumed_chunk_count": 3 if warm else 0,
        "source_rotation_count": 0,
        "manifest_digest": DIGEST_A,
        "assignment_digest": DIGEST_B,
        "representation_digest": DIGEST_C,
        "feasibility_digest": DIGEST_D,
        "evidence_generation": 7,
        "promotion_digest": DIGEST_A,
        "reason_code": None,
        "retryable": False,
        "started_at_unix_ms": 1_000 if not warm else 2_000,
        "updated_at_unix_ms": 1_100 if not warm else 2_100,
        "terminal_at_unix_ms": 1_200 if not warm else 2_200,
    }


def _ledger() -> dict:
    return {
        "protocol": "mycelium.swarm_artifact_acquisition_ledger.v1",
        "generation": 2,
        "current": None,
        "history": [
            _acquisition(generation=1, warm=False),
            _acquisition(generation=2, warm=True),
        ],
    }


def _live(*, sent: int, received: int, applied: int) -> dict:
    return {
        "protocol": "mycelium.physical_live_status.v1",
        "route_alive": True,
        "simulated": False,
        "route_identity_digest": DIGEST_D,
        "deployment_id": "deployment-a2",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "stages": [
            {"stage_id": "stage-000", "node_id": "node-0"},
            {"stage_id": "stage-001", "node_id": "node-2"},
        ],
        "counters": {
            "frames_sent": sent,
            "frames_received": received,
            "applied_operation_count": applied,
            "fatal": None,
        },
        "recent_inferences": [
            {
                "output_tokens": 1,
                "peer_counter_deltas": [
                    {
                        "node_id": "node-0",
                        "frames_sent": 3,
                        "frames_received": 3,
                        "applied_operation_count": 2,
                    },
                    {
                        "node_id": "node-2",
                        "frames_sent": 3,
                        "frames_received": 3,
                        "applied_operation_count": 2,
                    },
                ],
            }
        ],
    }


def _inference() -> dict:
    return {
        "request_id": "request-a2",
        "event_types": ["accepted", "token", "completed"],
        "terminal_state": "completed",
        "output": "Paris",
        "output_token_count": 1,
    }


def _evaluate(ledger: dict) -> dict:
    return MODULE.evaluate(
        ledger_document=ledger,
        before=_live(sent=10, received=10, applied=10),
        after=_live(sent=13, received=13, applied=12),
        inference=_inference(),
        model_id="Qwen/Qwen2.5-0.5B-Instruct",
        stage_id="stage-001",
        minimum_sources=2,
        prompt=MODULE.DEFAULT_PROMPT,
    )


def test_a2_gate_seals_matching_cold_warm_and_post_fault_inference() -> None:
    result = _evaluate(_ledger())

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["cold"]["transferred_verified_bytes"] == 300
    assert result["warm"]["transferred_verified_bytes"] == 0
    assert result["route"]["frames_sent_delta"] == 3
    assert result["inference"]["output"] == "Paris"
    detached = dict(result)
    digest = detached.pop("evidence_digest")
    assert digest == MODULE._digest(detached)


def test_a2_gate_accepts_fresh_assignment_authority_but_rejects_scope_drift() -> None:
    drifted = _ledger()
    drifted["history"][1]["assignment_id"] = "assignment-a2-fresh"
    drifted["history"][1]["assignment_digest"] = DIGEST_D
    result = _evaluate(drifted)
    assert result["passed"] is True

    drifted["history"][1]["representation_digest"] = DIGEST_D
    result = _evaluate(drifted)
    assert result["passed"] is False
    assert result["checks"]["ordinary_ledger_has_matching_cold_warm_pair"] is False


def test_a2_gate_withholds_on_origin_bytes() -> None:
    origin = _ledger()
    origin["history"][0]["origin_bytes"] = 10
    origin["history"][0]["sources"][1]["verified_bytes"] = 170
    result = _evaluate(origin)
    assert result["passed"] is False
    assert result["checks"]["cold_is_zero_origin_multi_source_full_transfer"] is False


def test_a2_gate_withholds_on_simulation_counter_or_answer_failure() -> None:
    before = _live(sent=10, received=10, applied=10)
    after = _live(sent=10, received=11, applied=10)
    after["simulated"] = True
    inference = _inference()
    inference["output"] = "London"
    result = MODULE.evaluate(
        ledger_document=_ledger(),
        before=before,
        after=after,
        inference=inference,
        model_id="Qwen/Qwen2.5-0.5B-Instruct",
        stage_id="stage-001",
        minimum_sources=2,
        prompt=MODULE.DEFAULT_PROMPT,
    )

    assert result["passed"] is False
    assert result["checks"]["route_is_live_non_simulated_and_non_fatal"] is False
    assert result["checks"]["physical_frame_and_operation_counters_advanced"] is False
    assert result["checks"]["fixed_answer_is_correct"] is False


def test_a2_gate_requires_per_stage_request_counters() -> None:
    after = _live(sent=13, received=13, applied=12)
    after["recent_inferences"][-1]["peer_counter_deltas"].pop()
    result = MODULE.evaluate(
        ledger_document=_ledger(),
        before=_live(sent=10, received=10, applied=10),
        after=after,
        inference=_inference(),
        model_id="Qwen/Qwen2.5-0.5B-Instruct",
        stage_id="stage-001",
        minimum_sources=2,
        prompt=MODULE.DEFAULT_PROMPT,
    )

    assert result["passed"] is False
    assert result["checks"]["latest_inference_advanced_every_serving_stage"] is False


def test_a2_gate_report_is_owner_private(tmp_path: Path) -> None:
    output = tmp_path / "gate.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)

    MODULE._write_private_json(output, _evaluate(_ledger()))

    assert json.loads(output.read_text("utf-8"))["passed"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_a2_gate_entrypoint_bootstraps_repo_imports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Seal the A2 cold/warm acquisition" in completed.stdout


def test_a2_gate_does_not_mutate_source_ledger() -> None:
    ledger = _ledger()
    original = copy.deepcopy(ledger)
    _evaluate(ledger)
    assert ledger == original
