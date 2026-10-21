# SPDX-License-Identifier: AGPL-3.0-only
"""Post-kill reads wait out a route restart, and refusals keep their observations.

Pure fakes: no node, serve, sidecar or fleet.  The fakes reproduce the
physical ``ordinary_browser_source_kill_kv_successor`` a4 shape: after the
kill the route cold-restarts, so the evidence door refuses resources reads
with ``a7_evidence_route_unavailable`` for longer than one read.  The
post-kill cleanup read retries those refusals; when no read is answered in the
settle window, cleanup is reported as not measured instead of refusing.  A
refusal raised after the kill, or an unfinished baseline, carries what was
observed.
"""
from __future__ import annotations

from typing import Any

import pytest

from mycelium_a7_physical_measurement import MeasurementRefused, MeasurementSettings, RunningRequest, measure
from tests.live.test_a7_measurement_restored_record import _SAME_PROCESS_COUNTERS, _Evidence
from tests.live.test_a7_measurement_result_poll import _Driver, _Fault

_SETTINGS = MeasurementSettings(result_timeout_s=3.0, settle_timeout_s=0.5, poll_s=0.01)


class _RestartingResources(_Evidence):
    """Resources reads after the kill are refused ``refusals`` times (``None``: always)."""

    def __init__(self, fault: _Fault, *, refusals: int | None, code: str = "a7_evidence_route_unavailable", **kwargs: Any) -> None:
        super().__init__(fault, **kwargs)
        self._resource_refusals = refusals
        self._code = code
        self.resource_refusals_served = 0

    def resources(self) -> dict[str, Any]:
        if self._fault.killed.is_set() and (self._resource_refusals is None or self._resource_refusals > 0):
            if self._resource_refusals is not None:
                self._resource_refusals -= 1
            self.resource_refusals_served += 1
            raise MeasurementRefused(self._code)
        return super().resources()


def _measure(evidence: _RestartingResources, fault: _Fault) -> tuple[dict[str, Any], dict[str, Any]]:
    return measure(driver=_Driver(fault), evidence=evidence, fault=fault, settings=_SETTINGS)


@pytest.mark.parametrize("code", ["a7_evidence_route_unavailable", "cold_route_restart_in_progress", "a7_evidence_unreachable"])
def test_settle_read_retries_a_restarting_route(code: str) -> None:
    fault = _Fault()
    evidence = _RestartingResources(fault, refusals=3, code=code, refused_after_kill=0, live_result=True)
    fields, observations = _measure(evidence, fault)
    assert evidence.resource_refusals_served == 3
    assert observations["settle_reads"] == {"attempts": 4, "refusals": {code: 3}, "observed": True}
    assert fields["cleanup"]["node-0"]["active_state_count_delta"] == 0


def test_settle_window_without_an_answer_reports_cleanup_not_measured() -> None:
    fault = _Fault()
    evidence = _RestartingResources(fault, refusals=None, refused_after_kill=0, live_result=True)
    fields, observations = _measure(evidence, fault)
    node = fields["cleanup"]["node-0"]
    assert node["cleanup_measurement"] == "not_measured"
    assert node["cleanup_unmeasured_reason"] == "entry_process_replaced"
    assert not any(key.endswith("_delta") for key in node)
    settle = observations["settle_reads"]
    assert settle["observed"] is False
    assert set(settle["refusals"]) == {"a7_evidence_route_unavailable"}
    assert observations["resources_after"] == {}
    assert fields["outcome"] == "aborted"


def test_an_unreachable_door_alone_does_not_claim_a_replaced_entry() -> None:
    fault = _Fault()
    evidence = _RestartingResources(fault, refusals=None, code="a7_evidence_unreachable", refused_after_kill=0, live_result=True)
    fields, _observations = _measure(evidence, fault)
    assert fields["cleanup"]["node-0"]["cleanup_unmeasured_reason"] == "entry_resources_unobserved"


def test_a_non_retryable_settle_refusal_carries_the_post_kill_observations() -> None:
    fault = _Fault()
    evidence = _RestartingResources(fault, refusals=None, code="a7_evidence_grant_refused", refused_after_kill=0, live_result=True)
    with pytest.raises(MeasurementRefused) as caught:
        _measure(evidence, fault)
    assert caught.value.code == "a7_evidence_grant_refused"
    observations = caught.value.observations
    assert observations is not None and observations["refused_after_kill"] is True
    assert observations["fault"]["kill_exit_status"] == 0
    assert observations["result_poll"]["stop"] == "result_observed"
    assert observations["a7_result"]["outcome"] == "aborted"
    assert observations["settle_reads"]["refusals"] == {}
    assert observations["measured"]["terminals"] == ["failed"]


def test_a_post_kill_counter_refusal_carries_the_post_kill_observations() -> None:
    fault = _Fault()
    evidence = _RestartingResources(fault, refusals=0, refused_after_kill=0, pre_kill_counters=None, before_counters=_SAME_PROCESS_COUNTERS)
    with pytest.raises(MeasurementRefused) as caught:
        _measure(evidence, fault)
    assert caught.value.code == "a7_standby_counters_unobserved"
    observations = caught.value.observations
    assert observations is not None
    assert observations["fault"]["tokens_received_at_kill"] >= 2
    assert observations["result_poll"]["restored_from"] == "durable_last_cutover"
    assert observations["settle_reads"]["observed"] is True


class _FailedBaselineDriver(_Driver):
    def start(self, label: str) -> RunningRequest:
        if label != "baseline":
            return super().start(label)
        running = RunningRequest(label=label)
        running.request_id = "request-baseline"
        running.on_token(0, 100, publisher_generation=1)
        running.on_terminal("failed")
        running.error = "stream_reset"
        running.finish()
        return running


def test_an_unfinished_baseline_refusal_records_how_it_ended() -> None:
    fault = _Fault()
    with pytest.raises(MeasurementRefused) as caught:
        measure(driver=_FailedBaselineDriver(fault), evidence=_RestartingResources(fault, refusals=0, refused_after_kill=0), fault=fault, settings=_SETTINGS)
    assert caught.value.code == "a7_baseline_not_completed"
    assert caught.value.observations == {"baseline": {"terminals": ["failed"], "error": "stream_reset", "token_count": 1}}
    assert not fault.killed.is_set()
