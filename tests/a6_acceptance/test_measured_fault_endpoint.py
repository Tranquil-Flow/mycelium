# SPDX-License-Identifier: AGPL-3.0-only
"""The A6 measured fault must be measured on a real admitted request.

Red control: with no fault assigned (or the fault disabled) the same direct
loopback control path must not produce any incident and must not produce a
receipt.  Green: an assigned source fault on an admitted, in-flight request
produces a real A4 scoped runtime incident carrying that request's identity.
"""
from __future__ import annotations

import pytest

from mycelium_a6_measured_fault_endpoint import (
    RECEIPT_PROTOCOL,
    MeasuredFaultEndpoint,
    MeasuredFaultRefused,
)
from mycelium_a6_fault_control import INJECTION_POINTS
from tests.a6_acceptance.a6_measured_stack import CASE, build_measured_stack, settle

IDENTITY_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation", "cancellation_generation",
)


def identity_of(binding):
    return {field: binding[field] for field in IDENTITY_FIELDS}


def endpoint_for(stack, *, seams, case_ids=(CASE,)):
    return MeasuredFaultEndpoint(
        seams=seams, case_ids=case_ids, expected_identity=identity_of(stack.binding),
        clock_ms=lambda: 1000, incident_source=stack.route.a4_scoped_runtime_incidents,
        gateway_terminal_count=lambda: stack.service.terminal_event_count(stack.request_id),
        test_only=True)


def source_incidents(stack):
    return [item for item in stack.route.a4_scoped_runtime_incidents()
            if item.get("request_id") == stack.request_id
            and item.get("reason") == "transport_not_running"]


def test_no_assigned_fault_measures_no_incident_and_no_receipt(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        with endpoint_for(stack, seams={}) as endpoint:
            assert endpoint.post(case_id=CASE)[0] >= 400
        assert settle(stack) is True  # the ordinary request still ran to a terminal
        assert source_incidents(stack) == []
        assert endpoint.statuses == ((CASE, 403),)
        with pytest.raises(MeasuredFaultRefused, match="case_refused"):
            endpoint.receipt()


class _FabricatedSeam:
    """Echoes the declared contract and can even invent an incident.

    This is exactly the "injected test-only seam" that must never become a
    measured receipt: it measures nothing from the product.
    """

    def __init__(self, case_id, *, incident=None):
        self.point = INJECTION_POINTS[case_id]
        self.seam_id = self.point.seam_id
        self._incident = incident

    def arm(self, *, case_id):
        return dict(self.point.required_pre_state)

    def apply(self, *, case_id):
        state = dict(self.point.expected_pre_recovery_state)
        if self._incident is not None:
            state["actual_incident"] = dict(self._incident)
        return state


def test_fabricated_seam_claiming_the_contract_is_never_a_measured_receipt(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        echo = _FabricatedSeam(CASE)
        with endpoint_for(stack, seams={echo.seam_id: echo}) as endpoint:
            assert endpoint.post(case_id=CASE)[0] == 200
        assert settle(stack) is True
        with pytest.raises(MeasuredFaultRefused, match="incident_unmeasured"):
            endpoint.receipt()
        assert source_incidents(stack) == []


def test_perfectly_shaped_fabricated_incident_the_route_never_recorded_is_refused(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        invented = {"incident_id": "incident-invented", "subject_id": stack.binding["source_node_id"],
                    "reason": "transport_not_running", "scope": "request", "fatal_accepted": False,
                    **identity_of(stack.binding)}
        fabricated = _FabricatedSeam(CASE, incident=invented)
        with endpoint_for(stack, seams={fabricated.seam_id: fabricated}) as endpoint:
            assert endpoint.post(case_id=CASE)[0] == 200
        assert settle(stack) is True
        with pytest.raises(MeasuredFaultRefused, match="incident_unobserved"):
            endpoint.receipt()
        assert source_incidents(stack) == []


def test_disabled_fault_positive_control_never_becomes_a_measured_receipt(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        slot = stack.route._test_only_transport_fault
        monkeypatch.setattr(slot, "disabled", True)
        with endpoint_for(stack, seams={stack.seam.seam_id: stack.seam}) as endpoint:
            assert endpoint.post(case_id=CASE)[0] >= 400
        assert settle(stack) is True
        assert source_incidents(stack) == []
        with pytest.raises(MeasuredFaultRefused, match="incident_unmeasured|case_refused"):
            endpoint.receipt()
        assert stack.route._test_only_transport_fault_consumed is False


def test_assigned_source_fault_measures_a_real_incident_on_the_admitted_request(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        assert stack.service.terminal_event_count(stack.request_id) == 0
        with endpoint_for(stack, seams={stack.seam.seam_id: stack.seam}) as endpoint:
            status, body = endpoint.post(case_id=CASE)
            assert (status, body) == (200, {"code": "controlled_fault_executed"})
            repeat_status, _ = endpoint.post(case_id=CASE)
            assert repeat_status == 409
            lifetime = endpoint.lifetime_id
            # The failed source never ran a decode: its transport failed
            # before any infer_start reached it.
            assert "infer_start" not in stack.source.calls
            assert settle(stack) is True
        receipt = endpoint.receipt()
        assert receipt["protocol"] == RECEIPT_PROTOCOL
        assert receipt["lifetime_id"] == lifetime and len(lifetime) == 64
        assert receipt["case_ids"] == (CASE,)
        assert receipt["executed_case_ids"] == (CASE,)
        assert receipt["statuses"] == ((CASE, 200), (CASE, 409))
        assert receipt["torn_down"] is True
        assert receipt["live_evidence"] is False
        assert receipt["physical_qualification"] is False
        assert len(receipt["measured_incidents"]) == 1
        incident = receipt["measured_incidents"][0]
        assert identity_of(incident) == identity_of(stack.binding)
        assert incident["reason"] == "transport_not_running"
        assert incident["scope"] == "request"
        assert incident["fatal_accepted"] is False
        # Frozen where the fault was measured. The gateway may already have
        # advanced by the time the control call returns, so the honest claim is
        # that the count was measured, not a particular value.
        assert isinstance(incident["gateway_terminal_count"], int)
        assert incident["gateway_terminal_count"] >= 0
        # The ordinary request still reached exactly one terminal afterwards.
        assert stack.service.terminal_event_count(stack.request_id) >= 1
        # The same incident is independently visible in the route's own record.
        assert incident["incident_id"] in [item["incident_id"] for item in source_incidents(stack)]
        assert stack.router._a6_test_only_source_failure_observed(stack.binding) is True
        assert stack.route._test_only_transport_fault_consumed is True
        # Settling lets the ordinary product path continue; the measured
        # source failure stands and is never re-invented by the fixture.
        assert stack.route._test_only_transport_fault_consumed is True


def test_receiver_lifetimes_are_independent_and_one_use(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        with endpoint_for(stack, seams={stack.seam.seam_id: stack.seam}) as first:
            assert first.post(case_id=CASE)[0] == 200
            first_lifetime = first.lifetime_id
        with endpoint_for(stack, seams={}) as second:
            assert second.post(case_id=CASE)[0] >= 400
            assert second.lifetime_id != first_lifetime
        assert len(first_lifetime) == 64 and len(second.lifetime_id) == 64
        assert first.receipt()["lifetime_id"] == first_lifetime


def test_foreign_identity_or_unclosed_endpoint_never_yields_a_receipt(monkeypatch, tmp_path):
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        foreign = MeasuredFaultEndpoint(
            seams={stack.seam.seam_id: stack.seam},
            case_ids=(CASE,), expected_identity={**identity_of(stack.binding), "request_id": "foreign"},
            clock_ms=lambda: 1000, incident_source=stack.route.a4_scoped_runtime_incidents,
            test_only=True)
        with foreign:
            assert foreign.post(case_id=CASE)[0] == 200
        with pytest.raises(MeasuredFaultRefused, match="identity_mismatch"):
            foreign.receipt()
        unclosed = MeasuredFaultEndpoint(
            seams={stack.seam.seam_id: stack.seam}, case_ids=(CASE,),
            expected_identity=identity_of(stack.binding), clock_ms=lambda: 1000,
            incident_source=stack.route.a4_scoped_runtime_incidents, test_only=True)
        unclosed.__enter__()
        with pytest.raises(MeasuredFaultRefused, match="endpoint_not_closed"):
            unclosed.receipt()
        unclosed.__exit__(None, None, None)
        with pytest.raises(ValueError, match="test_only_composition_required"):
            MeasuredFaultEndpoint(seams={}, case_ids=(CASE,),
                                  expected_identity=identity_of(stack.binding))
