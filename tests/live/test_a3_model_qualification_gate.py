from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_a3_model_qualification_gate.py"
)
SPEC = importlib.util.spec_from_file_location("run_a3_model_qualification_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
REPRESENTATION = "int8-weight-only"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def _decision() -> dict:
    return {
        "protocol": "mycelium.model_representation_decision.v2",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": REPRESENTATION,
        "representation_digest": DIGEST_A,
        "conversion_authorized": True,
        "source_artifact_digest": DIGEST_C,
        "quantizer": "mycelium.rowwise_symmetric_int8.v1",
        "download_authorized": False,
    }


def _feasibility(decision: dict) -> dict:
    return {
        "protocol": "mycelium.model_feasibility.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "state": "feasible",
        "provisioning_authorized": True,
        "evidence_valid_until_unix_ms": 20_000,
        "evidence_generation": 8,
        "feasibility_digest": DIGEST_B,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": REPRESENTATION,
        "representation_digest": DIGEST_A,
        "artifact_digest": DIGEST_C,
        "stages": [
            {
                "stage_index": 0,
                "node_id": "node-0",
                "start_layer": 0,
                "end_layer_exclusive": 18,
            },
            {
                "stage_index": 1,
                "node_id": "node-2",
                "start_layer": 18,
                "end_layer_exclusive": 28,
            },
        ],
    }


def _binding(decision: dict, feasibility: dict) -> str:
    return MODULE._digest(
        {
            "model_id": MODEL_ID,
            "revision": REVISION,
            "representation": REPRESENTATION,
            "representation_digest": decision["representation_digest"],
            "source_artifact_digest": decision["source_artifact_digest"],
            "quantizer": decision["quantizer"],
            "feasibility_digest": feasibility["feasibility_digest"],
            "evidence_generation": feasibility["evidence_generation"],
        }
    )


def _preparation(decision: dict, feasibility: dict) -> dict:
    return {
        "protocol": "mycelium.model_preparation.v1",
        "generation": 4,
        "state": "succeeded",
        "phase": None,
        "model_id": MODEL_ID,
        "revision": REVISION,
        "representation_digest": DIGEST_A,
        "owner_decision_digest": MODULE._digest(decision),
        "candidate_id": "candidate-7b",
        "topology_size": len(feasibility["stages"]),
        "transfer_bytes": 300,
        "verified_bytes": 300,
        "reason_code": None,
        "started_at_unix_ms": 8_000,
        "completed_at_unix_ms": 9_000,
        "download_authorized": False,
        "activation_started": False,
    }


def _acquisition(
    feasibility: dict, *, generation: int, start: int, end: int, warm: bool
) -> dict:
    return {
        "protocol": "mycelium.swarm_artifact_acquisition.v1",
        "generation": generation,
        "acquisition_id": f"acquisition-{generation}",
        "state": "ready",
        "phase": None,
        "model_id": MODEL_ID,
        "model_revision": REVISION,
        "representation": REPRESENTATION,
        "representation_digest": DIGEST_A,
        "assignment_id": f"assignment-{generation}",
        "placement_id": f"placement-{start}",
        "stage_id": f"stage-{start}",
        "layer_start": start,
        "layer_end_exclusive": end,
        "total_bytes": 150,
        "cached_verified_bytes": 150 if warm else 0,
        "transferred_verified_bytes": 0 if warm else 150,
        "origin_bytes": 0,
        "missing_bytes": 0,
        "quarantined_bytes": 0,
        "chunk_count": 2,
        "verified_chunk_count": 2,
        "manifest_digest": DIGEST_B,
        "assignment_digest": DIGEST_C,
        "feasibility_digest": feasibility["feasibility_digest"],
        "evidence_generation": feasibility["evidence_generation"],
        "reason_code": None,
        "retryable": False,
        "terminal_at_unix_ms": 9_000 + generation,
    }


def _acquisitions(feasibility: dict) -> dict:
    history = []
    generation = 1
    for stage in feasibility["stages"]:
        for warm in (False, True):
            history.append(
                _acquisition(
                    feasibility,
                    generation=generation,
                    start=stage["start_layer"],
                    end=stage["end_layer_exclusive"],
                    warm=warm,
                )
            )
            generation += 1
    return {
        "protocol": "mycelium.swarm_artifact_acquisition_ledger.v1",
        "generation": generation,
        "current": None,
        "history": history,
    }


def _recovery(
    decision: dict, feasibility: dict, preparation: dict
) -> tuple[dict, dict]:
    source_feasibility = copy.deepcopy(feasibility)
    source_feasibility["feasibility_digest"] = DIGEST_D
    source_feasibility["evidence_generation"] = 7
    stages = MODULE._frozen_stages(feasibility["stages"])
    assert stages is not None
    immutable = {
        "protocol": "mycelium.model_preparation_authorization.v2",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_quantization": decision["source_quantization"],
        "serving_dtype": decision["serving_dtype"],
        "serving_quantization": decision["serving_quantization"],
        "representation_digest": decision["representation_digest"],
        "source_artifact_digest": decision["source_artifact_digest"],
        "quantizer": decision["quantizer"],
        "conversion_authorized": True,
        "download_authorized": False,
        "owner_decision_digest": MODULE._digest(decision),
        "stages": stages,
        "catalog_generation": 1,
    }
    source = {
        **immutable,
        "operation_digest": DIGEST_C,
        "feasibility_digest": source_feasibility["feasibility_digest"],
        "evidence_generation": source_feasibility["evidence_generation"],
        "preparation_binding_digest": DIGEST_A,
    }
    current = {
        **immutable,
        "operation_digest": DIGEST_B,
        "feasibility_digest": feasibility["feasibility_digest"],
        "evidence_generation": feasibility["evidence_generation"],
        "preparation_binding_digest": DIGEST_C,
    }
    recovery = {
        "protocol": "mycelium.private_model_preparation_recovery_authorization.v1",
        "source_authority": source,
        "source_authority_digest": MODULE._digest(source),
        "source_authorized_at_unix_ms": 7_000,
        "source_evidence_valid_until_unix_ms": 7_500,
        "source_preparation_binding_digest": source["preparation_binding_digest"],
        "source_completed_phase": "candidate_challenged",
        "recovery_authority": current,
        "recovery_authority_digest": MODULE._digest(current),
        "recovery_authorized_at_unix_ms": preparation["started_at_unix_ms"],
        "recovery_evidence_valid_until_unix_ms": feasibility[
            "evidence_valid_until_unix_ms"
        ],
        "recovered_at_unix_ms": preparation["started_at_unix_ms"] + 1,
        "resume_from_phase": "candidate_challenged",
    }
    return recovery, source_feasibility


def _memory_ab(decision: dict, feasibility: dict) -> dict:
    return {
        "protocol": "mycelium.a3_memory_tier_ab.v1",
        "binding_digest": _binding(decision, feasibility),
        "baseline_snapshot_digest": DIGEST_C,
        "candidate_snapshot_digest": DIGEST_D,
        "invariant_inputs_digest": DIGEST_A,
        "memory_tier_node_id": "node-2",
        "changed_fields": ["nodes.node-2.fast_memory_bytes"],
        "baseline_fast_memory_bytes": 8_000,
        "candidate_fast_memory_bytes": 4_000,
        "baseline_allocation": [
            {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 18},
            {"node_id": "node-2", "start_layer": 18, "end_layer_exclusive": 28},
        ],
        "candidate_allocation": [
            {"node_id": "node-0", "start_layer": 0, "end_layer_exclusive": 22},
            {"node_id": "node-2", "start_layer": 22, "end_layer_exclusive": 28},
        ],
        "explored_allocation_count": 41,
        "rejected_intervals": [
            {
                "node_id": "node-2",
                "start_layer": 18,
                "end_layer_exclusive": 28,
                "reason": "fast_memory_spill_pressure",
            }
        ],
        "result": "allocation_changed",
        "stability_proof": None,
    }


def _parity() -> dict:
    return {
        "protocol": "mycelium.model_reference_parity.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "representation_digest": DIGEST_A,
        "source_artifact_digest": DIGEST_C,
        "tokenizer_digest": DIGEST_D,
        "policy_digest": DIGEST_A,
        "reference_process_participates_in_route": False,
        "reference_runtime_digest": DIGEST_B,
        "distributed_runtime_digest": DIGEST_C,
        "logit_tolerance": 0.2,
        "relative_logit_tolerance": 0.01,
        "cases": [
            {
                "prompt_tokens_digest": DIGEST_D,
                "greedy_tokens_match": True,
                "finite_logits": True,
                "within_tolerance": True,
                "maximum_logit_error": 0.1,
                "maximum_relative_logit_error": 0.001,
            }
        ],
    }


def _runtime() -> dict:
    stage = {
        "loaded_only_assignment_tensors": True,
        "prefill_applied_operations": 1,
        "decode_applied_operations": 4,
        "active_kv_bytes": 32,
        "frames_sent": 5,
        "frames_received": 5,
    }
    zero = {"active_kv_states": 0, "active_kv_bytes": 0}
    return {
        "protocol": "mycelium.distributed_model_runtime_evidence.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "representation_digest": DIGEST_A,
        "simulated": False,
        "stages": [
            {"stage_id": "stage-0", **stage},
            {"stage_id": "stage-1", **stage},
        ],
        "cleanup": {
            "completion": dict(zero),
            "cancellation": dict(zero),
            "shutdown": dict(zero),
        },
    }


def _live(value: int) -> dict:
    return {
        "route_alive": True,
        "simulated": False,
        "route_identity_digest": DIGEST_D,
        "deployment_id": "deployment-7b",
        "counters": {
            "frames_sent": value,
            "frames_received": value,
            "applied_operation_count": value,
            "fatal": None,
        },
    }


def _selector() -> dict:
    incumbent = "deployment-0.5b"
    blockers = {
        "infeasible": "model_does_not_fit",
        "stale": "model_capacity_stale",
        "incomplete": "model_not_compatible",
        "mismatched": "model_representation_decision_mismatch",
        "unqualified": "deployment_not_qualified",
        "dead": "route_unavailable",
    }
    return {
        "protocol": "mycelium.model_selector_switchback.v1",
        "candidate_deployment_id": "deployment-7b",
        "candidate_model_id": MODEL_ID,
        "candidate_currently_qualified": True,
        "candidate_was_selectable": True,
        "candidate_request_completed": True,
        "incumbent_deployment_id": incumbent,
        "incumbent_reselected": True,
        "incumbent_request_completed_after_switchback": True,
        "negative_rejections": [
            {
                "case": case,
                "deployment_id": f"deployment-rejected-{case}",
                "request_path": "/__mycelium/deployments/select",
                "http_status": 409,
                "selection_error": "deployment_unknown",
                "model_blocker": blocker,
                "selected_deployment_id_before": incumbent,
                "selected_deployment_id_after": incumbent,
                "switching_allowed_before": True,
                "switching_allowed_after": True,
            }
            for case, blocker in blockers.items()
        ],
        "incumbent_request_completed_after_negative_gate": True,
        "selection_scope": "future_requests",
    }


def _qwen3() -> dict:
    return {
        "model_id": "Qwen/Qwen3-8B",
        "revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "catalog_state": "compatible",
        "qualified": False,
        "selectable": False,
        "provisioning_started": False,
        "representation_authorized": False,
        "reason": "representation_not_authorized",
        "evidence_valid_until_unix_ms": 20_000,
    }


def _ui() -> dict:
    return {
        "protocol": "mycelium.a3_ui_verification.v1",
        "workspaces": {name: True for name in MODULE.WORKSPACES},
        "refresh_reconstructed": True,
        "navigation_reconstructed": True,
        "back_forward_reconstructed": True,
        "reconnect_reconstructed": True,
        "second_session_isolated": True,
        "internal_milestone_labels_absent": True,
        "private_paths_absent": True,
    }


def _inputs() -> dict:
    decision = _decision()
    feasibility = _feasibility(decision)
    return {
        "decision": decision,
        "feasibility": feasibility,
        "preparation": _preparation(decision, feasibility),
        "acquisitions": _acquisitions(feasibility),
        "memory_ab": _memory_ab(decision, feasibility),
        "parity": _parity(),
        "runtime": _runtime(),
        "before": _live(10),
        "after": _live(15),
        "inference": {
            "request_id": "request-7b",
            "terminal_state": "completed",
            "output": "Paris is the capital of France.",
            "output_token_count": 7,
            "backend_fallback": False,
        },
        "selector": _selector(),
        "qwen3": _qwen3(),
        "ui": _ui(),
        "model_id": MODEL_ID,
        "revision": REVISION,
        "representation": REPRESENTATION,
        "now_unix_ms": 10_000,
    }


def test_gate_accepts_complete_bound_executed_evidence() -> None:
    result = MODULE.evaluate(**_inputs())

    assert result["passed"] is True
    assert all(result["checks"].values())
    detached = dict(result)
    digest = detached.pop("evidence_digest")
    assert digest == MODULE._digest(detached)


def test_gate_accepts_exact_additive_recovery_without_rewriting_receipts() -> None:
    values = _inputs()
    recovery, source_feasibility = _recovery(
        values["decision"], values["feasibility"], values["preparation"]
    )
    values["recovery"] = recovery
    values["acquisitions"] = _acquisitions(source_feasibility)

    result = MODULE.evaluate(**values)

    assert result["passed"] is True
    assert result["checks"]["durable_preparation_recovery_is_bound"] is True
    assert result["checks"]["cold_and_warm_assignment_acquisition_passed"] is True

    drift = copy.deepcopy(values)
    drift["recovery"]["recovery_authority"]["stages"][1]["start_layer"] = 17
    assert (
        MODULE.evaluate(**drift)["checks"]["durable_preparation_recovery_is_bound"]
        is False
    )


def test_gate_accepts_exact_later_phase_recovery() -> None:
    values = _inputs()
    recovery, source_feasibility = _recovery(
        values["decision"], values["feasibility"], values["preparation"]
    )
    recovery["resume_from_phase"] = "artifacts_acquired"
    recovery["source_completed_phase"] = "artifacts_acquired"
    values["recovery"] = recovery
    values["acquisitions"] = _acquisitions(source_feasibility)

    result = MODULE.evaluate(**values)

    assert result["passed"] is True
    assert result["checks"]["durable_preparation_recovery_is_bound"] is True


def test_gate_rejects_recovery_phase_drift() -> None:
    values = _inputs()
    recovery, source_feasibility = _recovery(
        values["decision"], values["feasibility"], values["preparation"]
    )
    recovery["resume_from_phase"] = "artifacts_acquired"
    values["recovery"] = recovery
    values["acquisitions"] = _acquisitions(source_feasibility)

    result = MODULE.evaluate(**values)

    assert result["passed"] is False
    assert result["checks"]["durable_preparation_recovery_is_bound"] is False


def test_gate_requires_capacity_fresh_at_preparation_not_after_cold_build() -> None:
    values = _inputs()
    values["now_unix_ms"] = 30_000

    assert (
        MODULE.evaluate(**values)["checks"][
            "fresh_feasibility_binds_decision_and_contiguous_route"
        ]
        is True
    )

    values["preparation"]["started_at_unix_ms"] = 21_000
    values["preparation"]["completed_at_unix_ms"] = 22_000
    result = MODULE.evaluate(**values)
    assert (
        result["checks"]["fresh_feasibility_binds_decision_and_contiguous_route"]
        is False
    )
    assert (
        result["checks"]["preparation_completed_under_exact_owner_authority"] is False
    )


def test_gate_rejects_decision_drift_and_unauthorized_conversion() -> None:
    values = _inputs()
    values["decision"]["conversion_authorized"] = False
    result = MODULE.evaluate(**values)

    assert result["passed"] is False
    assert result["checks"]["exact_representation_decision_is_authorized"] is False
    assert (
        result["checks"]["fresh_feasibility_binds_decision_and_contiguous_route"]
        is True
    )


def test_gate_rejects_non_memory_ab_or_assertion_only_stability() -> None:
    values = _inputs()
    values["memory_ab"]["changed_fields"] = ["nodes.node-2.decode_ms_per_layer"]
    assert (
        MODULE.evaluate(**values)["checks"]["memory_tier_ab_is_executed_and_traceable"]
        is False
    )


def test_gate_rejects_unbound_preparation_or_missing_cold_warm_acquisition() -> None:
    values = _inputs()
    values["preparation"]["download_authorized"] = True
    values["acquisitions"]["history"] = values["acquisitions"]["history"][:1]
    result = MODULE.evaluate(**values)

    assert (
        result["checks"]["preparation_completed_under_exact_owner_authority"] is False
    )
    assert result["checks"]["cold_and_warm_assignment_acquisition_passed"] is False

    values = _inputs()
    values["memory_ab"]["candidate_allocation"] = copy.deepcopy(
        values["memory_ab"]["baseline_allocation"]
    )
    values["memory_ab"]["result"] = "allocation_stable"
    values["memory_ab"]["stability_proof"] = {"kind": "written_assertion"}
    assert (
        MODULE.evaluate(**values)["checks"]["memory_tier_ab_is_executed_and_traceable"]
        is False
    )


def test_gate_accepts_evidence_backed_stable_memory_allocation() -> None:
    values = _inputs()
    values["memory_ab"]["candidate_allocation"] = copy.deepcopy(
        values["memory_ab"]["baseline_allocation"]
    )
    values["memory_ab"]["result"] = "allocation_stable"
    values["memory_ab"]["stability_proof"] = {
        "kind": "unique_minimum",
        "evaluated_alternative_count": 40,
        "proof_digest": DIGEST_D,
    }

    assert (
        MODULE.evaluate(**values)["checks"]["memory_tier_ab_is_executed_and_traceable"]
        is True
    )


def test_gate_accepts_product_exact_weight_memory_ab() -> None:
    values = _inputs()
    memory = values["memory_ab"]
    memory.update(
        {
            "protocol": "mycelium.a3_product_memory_tier_ab.v1",
            "baseline_input_digest": DIGEST_B,
            "candidate_input_digest": DIGEST_C,
            "baseline_operation_digest": DIGEST_A,
            "candidate_operation_digest": DIGEST_D,
            "baseline_feasibility_digest": DIGEST_B,
            "candidate_feasibility_digest": DIGEST_C,
            "planner": "capability_aware_contiguous_exact_weight_dp",
            "changed_fields": ["placement.nodes.node-2.fast_allocatable_bytes"],
            "counterfactual_baseline_allocation_pressure": {
                "node_id": "node-2",
                "reason": "fast_memory_spill_pressure",
                "required_memory_bytes": 6_000,
                "candidate_fast_memory_bytes": 4_000,
                "spill_bytes": 2_000,
            },
        }
    )

    assert (
        MODULE.evaluate(**values)["checks"]["memory_tier_ab_is_executed_and_traceable"]
        is True
    )


def test_gate_rejects_parity_runtime_kv_and_physical_counter_failures() -> None:
    values = _inputs()
    values["parity"]["cases"][0]["greedy_tokens_match"] = False
    values["runtime"]["cleanup"]["cancellation"]["active_kv_bytes"] = 1
    values["after"] = _live(10)
    result = MODULE.evaluate(**values)

    assert result["passed"] is False
    assert result["checks"]["independent_reference_parity_passed"] is False
    assert result["checks"]["distributed_runtime_and_kv_cleanup_passed"] is False
    assert result["checks"]["physical_route_advanced_without_fallback"] is False


def test_gate_rejects_unqualified_selector_and_dishonest_qwen3() -> None:
    values = _inputs()
    values["selector"]["candidate_currently_qualified"] = False
    values["qwen3"]["selectable"] = True
    values["qwen3"]["provisioning_started"] = True
    result = MODULE.evaluate(**values)

    assert (
        result["checks"]["qualified_selector_and_incumbent_switchback_passed"] is False
    )
    assert result["checks"]["qwen3_is_truthfully_current_blocked"] is False


def test_gate_requires_every_product_selector_negative_without_incumbent_mutation() -> (
    None
):
    values = _inputs()
    values["selector"]["negative_rejections"] = values["selector"][
        "negative_rejections"
    ][:-1]

    assert (
        MODULE.evaluate(**values)["checks"][
            "qualified_selector_and_incumbent_switchback_passed"
        ]
        is False
    )

    values = _inputs()
    values["selector"]["negative_rejections"][0]["selected_deployment_id_after"] = (
        "deployment-rejected-infeasible"
    )
    assert (
        MODULE.evaluate(**values)["checks"][
            "qualified_selector_and_incumbent_switchback_passed"
        ]
        is False
    )

    values = _inputs()
    values["selector"]["incumbent_request_completed_after_negative_gate"] = False
    assert (
        MODULE.evaluate(**values)["checks"][
            "qualified_selector_and_incumbent_switchback_passed"
        ]
        is False
    )


def test_gate_rejects_missing_workspace_or_browser_reconstruction() -> None:
    values = _inputs()
    del values["ui"]["workspaces"]["network"]
    values["ui"]["refresh_reconstructed"] = False

    assert (
        MODULE.evaluate(**values)["checks"][
            "all_product_workspaces_and_browser_reconstruction_passed"
        ]
        is False
    )


def test_gate_does_not_mutate_inputs() -> None:
    values = _inputs()
    original = copy.deepcopy(values)
    MODULE.evaluate(**values)
    assert values == original


def test_gate_writes_owner_private_artifact(tmp_path: Path) -> None:
    output = tmp_path / "a3.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)
    result = MODULE.evaluate(**_inputs())

    MODULE._write_private_json(output, result)

    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_gate_entrypoint_bootstraps_repo_imports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Seal A3" in completed.stdout
