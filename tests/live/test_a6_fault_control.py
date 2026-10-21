# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first contract tests for the A6 controlled-fault mechanism.

These tests exist before ``mycelium_a6_fault_control`` does: they pin every
behaviour the A6 physical gate's 16 required cases depend on.  Nothing here
launches a node, binds a port, or touches a fleet; the seams are deterministic
doubles.
"""
from __future__ import annotations

import inspect

import pytest

from scripts.run_a6_recovery_gate import REQUIRED_CASES

import mycelium_a6_fault_control as fault_control
from mycelium_a6_fault_control import (
    EXACT_FLEET_GRANT,
    EXECUTE_SOURCE_FAILURE_ACTION,
    FaultRejected,
    INJECTION_POINTS,
    ControlledFaultAuthority,
    ControlledFaultMechanism,
)

BROWSER_HARNESS = (
    __import__("pathlib").Path(__file__).resolve().parents[2]
    / "ui/web/scripts/a6-product-browser-e2e.mjs"
)


class _Seam:
    """Deterministic seam double: records arm/apply order and measured state."""

    def __init__(self, seam_id: str, *, pre, post, marker: dict | None = None) -> None:
        self.seam_id = seam_id
        self._pre = dict(pre)
        self._post = dict(post)
        self.armed = False
        self.applied = 0
        self.calls: list[str] = []
        self.marker = marker

    def arm(self, *, case_id: str):
        self.calls.append(f"arm:{case_id}")
        self.armed = True
        return dict(self._pre)

    def apply(self, *, case_id: str):
        self.calls.append(f"apply:{case_id}")
        if not self.armed:
            raise AssertionError("seam applied before arm")
        self.applied += 1
        self.armed = False
        return dict(self._post)


def _seams_for_every_case() -> dict[str, _Seam]:
    seams: dict[str, _Seam] = {}
    for case_id, point in INJECTION_POINTS.items():
        seams.setdefault(
            point.seam_id,
            _Seam(point.seam_id, pre=point.required_pre_state, post=point.expected_pre_recovery_state),
        )
    return seams


def _mechanism(seams, *, grant=EXACT_FLEET_GRANT, caller_host="127.0.0.1", clock=1_000):
    return ControlledFaultMechanism(
        seams=seams,
        authority=ControlledFaultAuthority(grant=grant, caller_host=caller_host),
        clock_ms=lambda: clock,
    )


def test_injection_points_cover_the_frozen_gate_case_set_exactly() -> None:
    assert tuple(INJECTION_POINTS) == REQUIRED_CASES
    assert len(INJECTION_POINTS) == 16
    for case_id, point in INJECTION_POINTS.items():
        assert point.case_id == case_id
        assert point.seam_id
        assert point.required_pre_state
        assert point.expected_pre_recovery_state


def test_browser_harness_action_string_is_the_one_the_mechanism_accepts() -> None:
    source = BROWSER_HARNESS.read_text(encoding="utf-8")
    assert f"JSON.stringify({{ action: '{EXECUTE_SOURCE_FAILURE_ACTION}', case_id: control.caseId }})" in source
    assert "'x-mycelium-a6-fleet-grant': EXACT_FLEET_GRANT" in source
    assert "hostname: '127.0.0.1', port: control.port, path: control.path" in source
    assert EXACT_FLEET_GRANT in source
    assert fault_control.EXACT_FLEET_GRANT == EXACT_FLEET_GRANT


def test_every_case_injection_produces_its_pre_recovery_failure_state() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams)
    for expected_sequence, case_id in enumerate(REQUIRED_CASES, start=1):
        record = mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=case_id)
        assert record.sequence == expected_sequence
        assert record.case_id == case_id
        assert record.injection_point == case_id
        assert record.action == EXECUTE_SOURCE_FAILURE_ACTION
        assert record.pre_recovery_proven is True
        assert record.pre_state == INJECTION_POINTS[case_id].required_pre_state
        assert record.post_state == INJECTION_POINTS[case_id].expected_pre_recovery_state
        assert record.record_digest.startswith("sha256:")
    assert mechanism.ledger_digest().startswith("sha256:")
    assert len(mechanism.records()) == 16


def test_injection_is_single_use_and_refuses_a_repeat_without_side_effect() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams)
    mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="no_successor")
    seam = seams[INJECTION_POINTS["no_successor"].seam_id]
    applied_before = seam.applied
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="no_successor")
    assert "controlled_fault_already_executed:no_successor" in str(error.value)
    assert seam.applied == applied_before


def test_mechanism_fails_closed_without_the_exact_fleet_grant() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams, grant="not granted")
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="no_successor")
    assert str(error.value) == "explicit_fleet_grant_required"
    assert mechanism.records() == ()
    assert all(seam.applied == 0 for seam in seams.values())


def test_mechanism_refuses_a_non_loopback_caller() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams, caller_host="10.0.0.9")
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="no_successor")
    assert str(error.value) == "non_loopback_fault_request"
    assert all(seam.applied == 0 for seam in seams.values())


def test_mechanism_refuses_an_unknown_action_and_an_unknown_case() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams)
    with pytest.raises(FaultRejected) as error:
        mechanism.execute("execute_controlled_something_else", case_id="no_successor")
    assert str(error.value) == "controlled_fault_unknown_action"
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="not_a_case")
    assert str(error.value) == "controlled_fault_unknown_case"
    assert mechanism.records() == ()


def test_mechanism_refuses_when_a_seam_is_unavailable() -> None:
    mechanism = _mechanism({})
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="no_successor")
    assert str(error.value) == (
        f"controlled_fault_seam_unavailable:{INJECTION_POINTS['no_successor'].seam_id}"
    )
    assert mechanism.records() == ()


def test_mechanism_refuses_a_seam_that_does_not_prove_the_failure_state() -> None:
    """An injection that leaves the product pre-recovery-clean must not record."""

    point = INJECTION_POINTS["stale_qualification"]
    seam = _Seam(point.seam_id, pre=point.required_pre_state, post={"successor_candidate_reserved": True})
    mechanism = _mechanism({point.seam_id: seam})
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="stale_qualification")
    assert str(error.value) == "controlled_fault_injection_unproven:stale_qualification"
    assert mechanism.records() == ()


def test_mechanism_refuses_when_the_precondition_is_not_met() -> None:
    point = INJECTION_POINTS["stale_qualification"]
    seam = _Seam(point.seam_id, pre={"successor_candidate_reserved": False}, post=point.expected_pre_recovery_state)
    mechanism = _mechanism({point.seam_id: seam})
    with pytest.raises(FaultRejected) as error:
        mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="stale_qualification")
    assert str(error.value) == "controlled_fault_pre_state_unproven:stale_qualification"
    assert seam.applied == 0


def test_mechanism_status_is_a_bounded_digest_bound_projection() -> None:
    seams = _seams_for_every_case()
    mechanism = _mechanism(seams)
    before = mechanism.status()
    assert before["protocol"] == "mycelium.a6_controlled_fault.v1"
    assert before["executed_count"] == 0
    assert before["executed_cases"] == []
    mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id="ordinary_browser_source_failure_recovery")
    after = mechanism.status()
    assert after["executed_count"] == 1
    assert after["executed_cases"] == ["ordinary_browser_source_failure_recovery"]
    assert after["ledger_digest"] == mechanism.ledger_digest()
    assert after["ledger_digest"] != before["ledger_digest"]


def test_fault_records_carry_no_authored_passing_assertion() -> None:
    """The mechanism may not accept a caller-authored pass/fail field."""

    signature = inspect.signature(ControlledFaultMechanism.execute)
    assert set(signature.parameters) == {"self", "action", "case_id"}
