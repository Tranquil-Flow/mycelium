from __future__ import annotations

from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from mycelium_candidate_promotion import (
    evaluate_candidate_promotion,
    validate_candidate_promotion_report,
)
from mycelium_live.registry import DeploymentSelectionError, LiveDeploymentRegistry
from tests.live.test_deployment_registry import _runtime


def _budget() -> dict:
    return {
        "protocol": "mycelium.performance_budget.v1",
        "budget_id": "m13-candidate-v1",
        "workload_label": "interactive_chat_v1",
        "minimum_sample_size": 2,
        "ttft_ms": {"maximum_p50": 1_000.0, "maximum_p95": 2_000.0},
        "tpot_ms": {"maximum_p50": 500.0, "maximum_p95": 1_000.0},
        "minimum_output_tokens_per_second": 1.0,
        "maximum_peak_rss_bytes_by_member": {"member-a": 1_000},
        "maximum_frames_per_request_by_stage": {"stage-a": 20},
        "execution_scope": "sequential_observed",
        "queueing_budget_state": "deferred_to_m16",
    }


def _observations() -> list[dict]:
    return [
        {
            "case_id": f"case-{index}",
            "completed": True,
            "quality_passed": True,
            "negative_check_passed": True,
            "ttft_ms": 100.0 + index,
            "tpot_ms": 50.0 + index,
            "output_tokens_per_second": 2.0,
            "peak_rss_bytes_by_member": {"member-a": 500},
            "frames_per_request_by_stage": {"stage-a": 10},
        }
        for index in range(2)
    ]


def _report() -> dict:
    return evaluate_candidate_promotion(
        candidate_deployment_id="deployment-1",
        incumbent_deployment_id="deployment-0",
        planner_snapshot_digest="sha256:" + "a" * 64,
        observations=_observations(),
        performance_budget=_budget(),
    )


def test_report_is_derived_and_rejects_failed_or_tampered_canary() -> None:
    report = _report()
    assert validate_candidate_promotion_report(report)["decision"] == "promote"

    failed = _observations()
    failed[1]["negative_check_passed"] = False
    rejected = evaluate_candidate_promotion(
        candidate_deployment_id="deployment-1",
        incumbent_deployment_id="deployment-0",
        planner_snapshot_digest="sha256:" + "a" * 64,
        observations=failed,
        performance_budget=_budget(),
    )
    assert rejected["decision"] == "reject"
    tampered = {**report, "decision": "reject"}
    with pytest.raises(ValueError, match="not derived"):
        validate_candidate_promotion_report(tampered)


def test_atomic_promotion_and_rollback_preserve_incumbent_request_binding() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    registry = LiveDeploymentRegistry(runtimes)
    registry.encode("incumbent prompt")
    request = SimpleNamespace(
        request_id="request-incumbent",
        prompt_token_ids=(1,),
        max_new_tokens=1,
    )
    sink = SimpleNamespace(emit=lambda _index, _token: None)
    registry.admit(request, sink, pinned_deployment=registry.current_deployment())

    promoted = registry.promote_candidate(_report())
    assert promoted["selected_deployment_id"] == "deployment-1"
    assert registry.decode_one(request.request_id) is True
    assert registry.request_status(request.request_id) == "COMPLETED"
    assert registry.decode_token(7) == "model-0:7"

    rolled_back = registry.rollback_candidate("deployment-1", reason="canary_regression")
    assert rolled_back["selected_deployment_id"] == "deployment-0"
    assert [item["state"] for item in registry.public_status()["incidents"][-2:]] == [
        "qualified_candidate_promoted",
        "qualified_candidate_rolled_back",
    ]


def test_promoted_candidate_projects_validated_report_summary() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    runtimes[1] = replace(
        runtimes[1],
        placement_projection={"promotion": None},
    )
    registry = LiveDeploymentRegistry(runtimes)

    registry.promote_candidate(_report())

    assert registry.public_status()["placement"]["promotion"] == {
        "candidate_deployment_id": "deployment-1",
        "incumbent_deployment_id": "deployment-0",
        "decision": "promote",
        "reasons": [],
        "sample_size": 2,
    }


def test_blocked_status_read_does_not_hold_registry_promotion_lock() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    registry = LiveDeploymentRegistry(runtimes)
    entered = threading.Event()
    release = threading.Event()
    promoted = threading.Event()
    original_status = runtimes[0].route.public_status

    def blocked_status():
        entered.set()
        assert release.wait(timeout=2)
        return original_status()

    runtimes[0].route.public_status = blocked_status
    status_thread = threading.Thread(target=registry.public_status)
    promotion_thread = threading.Thread(
        target=lambda: (registry.promote_candidate(_report()), promoted.set())
    )
    status_thread.start()
    assert entered.wait(timeout=1)
    promotion_thread.start()

    assert promoted.wait(timeout=1)
    release.set()
    status_thread.join(timeout=2)
    promotion_thread.join(timeout=2)


def test_registry_rejects_failed_candidate_evidence() -> None:
    registry = LiveDeploymentRegistry([_runtime(0), _runtime(1)])
    observations = _observations()
    observations[0]["completed"] = False
    report = evaluate_candidate_promotion(
        candidate_deployment_id="deployment-1",
        incumbent_deployment_id="deployment-0",
        planner_snapshot_digest="sha256:" + "a" * 64,
        observations=observations,
        performance_budget=_budget(),
    )
    with pytest.raises(DeploymentSelectionError, match="candidate_evidence_rejected"):
        registry.promote_candidate(report)
