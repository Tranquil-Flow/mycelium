# SPDX-License-Identifier: AGPL-3.0-only
"""R6: gateway-authoritative attempts and the admitted v2 cleanup-authority case.

The in-process compositions below use the real gateway, router port, command
controller and M16 coordinator with a simulated route. They exercise the
product-side producer and the gate path; they are not physical evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from mycelium_a6_case_observations import (
    CaseObservationRecorder,
    ObservationRefused,
    observation_authority,
)
from mycelium_a6_fault_control import EXACT_FLEET_GRANT
from mycelium_a6_product_fault_seams import ExecutedCase, LiveProductFaultTarget
from scripts import run_a6_recovery_case_v2 as runner_v2
from scripts.run_a6_recovery_gate import (
    REQUIRED_CASES,
    TRUSTED_CASE_RUNNER,
    TRUSTED_CASE_RUNNER_V2,
    _verify_case_record,
    validate_plan,
)
from tests.a6_acceptance.test_case_observations import ROOT, plan  # noqa: F401
from mycelium_a6_cleanup_observation_v2 import LAYER_PRE_CUTOVER_ABORT
from tests.live.test_a6_cleanup_observation_v2 import (
    DelayedSuccessorProofRoute,
    UnprovenSuccessorCleanupRoute,
)
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _runtime,
    _submit_and_drain,
)

CASE = "cleanup_authority_failure"


def test_gateway_receipt_names_the_attempt_its_terminal_belongs_to(tmp_path):
    route = FailingThenRecoveringRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [event.kind for event in events if event.kind == "completed"] == ["completed"]
        receipt = service.terminal_attempt_receipt(request_id)
        identity = coordinator.route_identity(request_id)
        assert receipt is not None and receipt[0] == request_id
        assert receipt[1] == identity["request_attempt"] == 2
        assert receipt[2] == identity["path_manifest_digest"]
    finally:
        service.close()


def test_blocked_notice_attempt_is_the_same_gateway_receipt(tmp_path):
    route = DelayedSuccessorProofRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        notice = service.cleanup_notice_provenance(request_id)
        assert notice is not None and notice[1] == 2
        assert service.terminal_attempt_receipt(request_id) == notice
    finally:
        service.close()


def _executed_from(route, request_id: str) -> ExecutedCase:
    source = dict(route.source_command)
    binding = {**source, "source_node_id": "node-0", "source_attempt": source["request_attempt"],
               "path_manifest_digest": source["path_digest"]}
    record = ExecutedCase(case_id=CASE, request_id=request_id, binding=binding,
                          source_node_id="node-0", armed_reservation_count=2,
                          deadline_unix_ms=1_800_000_060_000)
    # The physical seam measures these after stalling the successor's peers;
    # in-process the simulated route withholds the successor's cleanup proof.
    record.post_state = {"cleanup_authority_refused": True, "cutover_committed": False,
                         "unpublishable_suffix_discarded": True}
    record.failure_marker = (0, 1)
    return record


def _run_v2(plan_path: Path, records: Path, monkeypatch) -> int:
    monkeypatch.setenv("MYCELIUM_A6_FLEET_GRANT", EXACT_FLEET_GRANT)
    monkeypatch.setenv("MYCELIUM_A6_CASE_EVIDENCE_FILE", str(records / f"{CASE}.json"))
    return runner_v2.main(["--case", CASE, "--contract-version", "v2",
                           "--operator-plan", str(plan_path)])


def test_pre_cutover_refusal_writes_an_authorized_v2_envelope_the_gate_admits(
        plan, tmp_path, monkeypatch):  # noqa: F811
    # Owner ruling 2026-10-06: the successor's unproven completion cleanup is
    # refused before cutover; the case asserts that real product shape.
    route = UnprovenSuccessorCleanupRoute()
    coordinator, router, service, qualification = _runtime(tmp_path / "rt", route)
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = LiveProductFaultTarget(router=router, route=route, service=service)
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=5)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [e.text for e in events if e.kind == "token"] == ["<101>", "<102>"]
        executed = _executed_from(route, request_id)
        released = recorder._settle_release(executed, None)
        command = router.a6_command_authority(request_id)
        assert command == {"request_id": request_id, "request_attempt": 2,
                           "registered_command_count": 1, "terminal_cas_count": 0}
        path = recorder.write_cleanup_authority_v2(executed, released, command)
    finally:
        service.close()
    envelope = json.loads(path.read_text())
    assert envelope["protocol"] == "mycelium.a6_cleanup_authority_observation.v3"
    assert envelope["bindings"]["request_attempt"] == 2
    assert envelope["bindings"]["successor_attempt"] == 2
    observed = envelope["observation"]
    assert observed["cutover_committed"] is False
    assert observed["committed_route_attempt"] == 1
    assert observed["coordinator_terminal"] == "failed"
    assert observed["duplicated_token_count"] == 0
    assert observed["terminal_state"] == "TERMINAL_BLOCKED"
    assert observed["stale_reservation_count"] == 0
    assert observed["gateway_authority"]["terminal_event_count"] == 1
    assert observed["gateway_authority"]["terminal_code"] == "request_cleanup_unproven"
    records = tmp_path / "records"
    records.mkdir(mode=0o700)
    assert _run_v2(plan, records, monkeypatch) == 0
    record = json.loads((records / f"{CASE}.json").read_text())
    assert record["protocol"] == "mycelium.a6_physical_recovery_case.v2"
    assert record["count_layer_state"] == LAYER_PRE_CUTOVER_ABORT
    execution = {"source_digest": authority.source_digest, "spec_digest": authority.spec_digest,
                 "contract_digest": authority.contract_digest}
    _verify_case_record(records / f"{CASE}.json", CASE, execution, "v2")
    with pytest.raises(ValueError, match="contract_mismatch"):
        _verify_case_record(records / f"{CASE}.json", CASE, execution, "v1")


def test_published_suffix_after_a_committed_cutover_is_refused(plan, tmp_path):  # noqa: F811
    # The old shape: the successor cut over (its suffix became the request's)
    # and only then was cleanup unproven. No v2 envelope may come from it.
    route = DelayedSuccessorProofRoute()
    coordinator, router, service, qualification = _runtime(tmp_path / "rt", route)
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = LiveProductFaultTarget(router=router, route=route, service=service)
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=5)
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        assert coordinator.route_identity(request_id)["request_attempt"] == 2
        executed = _executed_from(route, request_id)
        blocked = recorder._settle(executed)
        route.prove_successor_cleanup()
        released = recorder._settle_release(executed, blocked.view)
        with pytest.raises(ObservationRefused, match="cleanup_v2_cutover_committed"):
            recorder.write_cleanup_authority_v2(
                executed, released, router.a6_command_authority(request_id))
    finally:
        service.close()


@pytest.mark.parametrize("post_state", [
    {"cleanup_authority_refused": True, "cutover_committed": False,
     "unpublishable_suffix_discarded": False},
    {"cleanup_authority_refused": False, "cutover_committed": False,
     "unpublishable_suffix_discarded": True},
])
def test_pre_cutover_refusal_without_the_seam_proof_is_refused(
        plan, tmp_path, post_state):  # noqa: F811
    route = UnprovenSuccessorCleanupRoute()
    _coordinator, router, service, qualification = _runtime(tmp_path / "rt", route)
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = LiveProductFaultTarget(router=router, route=route, service=service)
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=5)
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        executed = _executed_from(route, request_id)
        executed.post_state = dict(post_state)
        released = recorder._settle_release(executed, None)
        with pytest.raises(ObservationRefused, match="cleanup_v2_case_invalid"):
            recorder.write_cleanup_authority_v2(
                executed, released, router.a6_command_authority(request_id))
    finally:
        service.close()


def test_v2_runner_refuses_without_grant_or_with_a_foreign_plan(plan, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MYCELIUM_A6_FLEET_GRANT", raising=False)
    assert runner_v2.main(["--case", CASE, "--contract-version", "v2",
                           "--operator-plan", str(tmp_path / "missing.json")]) == 2
    records = tmp_path / "records"
    records.mkdir(mode=0o700)
    # No source-owned v2 envelope exists for this plan's run.
    assert _run_v2(plan, records, monkeypatch) == 1
    assert not (records / f"{CASE}.json").exists()


def _gate_plan(tmp_path: Path, *, v2_case: str | None) -> dict:
    root = tmp_path / "evidence"
    operator_plan = str(tmp_path / "operator-plan.json")

    def argv(case):
        if case == v2_case:
            return [sys.executable, str(TRUSTED_CASE_RUNNER_V2), "--case", case,
                    "--contract-version", "v2", "--operator-plan", operator_plan]
        return [sys.executable, str(TRUSTED_CASE_RUNNER), "--case", case,
                "--operator-plan", operator_plan]

    return {
        "protocol": "mycelium.a6_physical_recovery_execution.v1",
        "source_digest": "sha256:" + "a" * 64, "spec_digest": "sha256:" + "b" * 64,
        "contract_digest": "sha256:" + "c" * 64, "product_origin": "http://127.0.0.1:8787",
        "operator_plan": operator_plan, "evidence_root": str(root),
        "expected_placements": ["node-0", "node-2"],
        "cases": [{"case_id": case, "argv": argv(case),
                   "evidence_file": str(root / f"{case}.json")} for case in REQUIRED_CASES],
        "cleanup_checks": [
            {"check_id": check, "argv": [sys.executable, str(TRUSTED_CASE_RUNNER),
                                         "--cleanup-check", check,
                                         "--operator-plan", operator_plan]}
            for check in ("local_product_port", "physical_processes", "reservations")],
    }


def test_gate_admits_the_v2_runner_only_for_the_cleanup_authority_case(tmp_path):
    validated = validate_plan(_gate_plan(tmp_path, v2_case=CASE))
    contracts = {case["case_id"]: case["contract_version"] for case in validated["cases"]}
    assert contracts[CASE] == "v2"
    assert {value for key, value in contracts.items() if key != CASE} == {"v1"}
    assert {case["contract_version"] for case in validate_plan(
        _gate_plan(tmp_path, v2_case=None))["cases"]} == {"v1"}
    with pytest.raises(ValueError, match="physical_case_runner_untrusted:no_successor"):
        validate_plan(_gate_plan(tmp_path, v2_case="no_successor"))


def test_v1_record_without_its_protocol_is_not_passing(tmp_path):
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"case_id": "no_successor", "executed": True,
                                  "result": "passed", "source_digest": "x",
                                  "spec_digest": "y", "contract_digest": "z"}))
    os.chmod(record, 0o600)
    with pytest.raises(ValueError, match="contract_mismatch"):
        _verify_case_record(record, "no_successor",
                            {"source_digest": "x", "spec_digest": "y", "contract_digest": "z"})
