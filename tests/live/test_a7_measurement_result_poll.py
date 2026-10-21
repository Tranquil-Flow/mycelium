# SPDX-License-Identifier: AGPL-3.0-only
"""An unobserved A7 cutover record says why it was not observed.

Pure fakes: no node, serve, sidecar or fleet. The fakes reproduce the shape of
the physical ``no_a6_successor_aborts`` attempt: the request reaches its
terminal as soon as the source is killed, and every later evidence read is
refused because the route was replaced.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from mycelium_a7_physical_measurement import (
    KillObservation,
    MeasurementRefused,
    MeasurementSettings,
    ProcessGroup,
    RunningRequest,
    measure,
)

_NODE = {
    "active_state_count": 0,
    "active_kv_bytes": 0,
    "shadow_kv_bytes": 0,
    "shadow_checkpoint_count": 0,
    "entry_fence_record_count": 0,
}
_STANDBY_RUNTIME = {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": 0}


class _Fault:
    transport = "ssh"

    def __init__(self) -> None:
        self.killed = threading.Event()

    def discover(self) -> ProcessGroup:
        return ProcessGroup(pgid=41, pids=(41,))

    def pause(self, _group: ProcessGroup) -> int:
        return 0

    def resume(self, _group: ProcessGroup) -> int:
        return 0

    def kill(self, group: ProcessGroup) -> KillObservation:
        started = time.monotonic()
        self.killed.set()
        return KillObservation(group.pgid, started, time.monotonic(), 0)

    def group_gone(self, _group: ProcessGroup) -> bool:
        return True


class _Driver:
    ordinary_browser_request = True

    def __init__(self, fault: _Fault) -> None:
        self._fault = fault

    def start(self, label: str) -> RunningRequest:
        running = RunningRequest(label=label)
        running.request_id = "request-" + label

        def run() -> None:
            count = 6 if label == "baseline" else 3
            for index in range(count):
                running.on_token(index, 100 + index, publisher_generation=1)
                time.sleep(0.005)
            if label == "measured":
                # The explicit abort is synchronous with the source loss.
                self._fault.killed.wait(timeout=10)
                running.on_terminal("failed")
            else:
                running.on_terminal("completed")
            running.finish()

        threading.Thread(target=run, daemon=True).start()
        return running

    def workspaces_checked(self) -> list[str]:
        return []


class _Evidence:
    def __init__(self, fault: _Fault) -> None:
        self._fault = fault

    def resources(self) -> dict[str, Any]:
        return {
            "entry_node_id": "node-0",
            "exited_nodes": [],
            "gateway_reservations": 0,
            "route_request_registrations": 0,
            "nodes": {
                "node-0": {**_NODE, "a7_roles": ["entry", "standby"], "standby_runtime": dict(_STANDBY_RUNTIME)},
                "node-2": {**_NODE, "a7_roles": ["source"], "standby_runtime": None},
            },
        }

    def request(self, request_id: str) -> dict[str, Any]:
        if self._fault.killed.is_set():
            raise MeasurementRefused("a7_evidence_route_unavailable")
        return {
            "request_id": request_id,
            "a7": {
                "request": {"committed": 3, "acknowledged": -1, "ack_lag_events": 3, "commits_sent": 3, "result": None},
                "agent": {"shadow_checkpoint_count": 0, "shadow_checkpoint_payload_bytes": 0},
                "standby_runtime": dict(_STANDBY_RUNTIME),
            },
            "scoped_incidents": [],
        }


def test_unobserved_result_records_every_post_kill_refusal() -> None:
    fault = _Fault()
    settings = MeasurementSettings(result_timeout_s=5.0, settle_timeout_s=0.5, poll_s=0.01)
    with pytest.raises(MeasurementRefused) as caught:
        measure(driver=_Driver(fault), evidence=_Evidence(fault), fault=fault, settings=settings)
    assert caught.value.code == "a7_result_unobserved"
    observations = caught.value.observations
    poll = observations["result_poll"]
    assert poll["stop"] == "refused_after_terminal"
    assert poll["stream_done_at_stop"] is True
    assert poll["reads"] == 0 and poll["last_state"] is None
    assert set(poll["refusals"]) == {"a7_evidence_route_unavailable"}
    assert observations["source_incidents"] == []


class _HeldShadowEvidence(_Evidence):
    """The standby keeps the baseline's shadow chain after the baseline completed."""

    def resources(self) -> dict[str, Any]:
        document = super().resources()
        document["nodes"]["node-0"].update(shadow_checkpoint_count=2, shadow_kv_bytes=4096)
        return document


def test_unreleased_baseline_shadow_refusal_records_what_was_held() -> None:
    fault = _Fault()
    settings = MeasurementSettings(settle_timeout_s=0.3, poll_s=0.01)
    with pytest.raises(MeasurementRefused) as caught:
        measure(driver=_Driver(fault), evidence=_HeldShadowEvidence(fault), fault=fault, settings=settings)
    assert caught.value.code == "a7_baseline_shadow_not_released"
    observations = caught.value.observations
    assert observations is not None
    assert observations["held"] == {"node-0": {"shadow_checkpoint_count": 2, "shadow_kv_bytes": 4096}}
    assert observations["resources_before"]["entry_node_id"] == "node-0"
    assert observations["resources_before"]["nodes"]["node-0"]["shadow_kv_bytes"] == 4096
    assert observations["baseline"] == {"token_count": 6}
    assert not fault.killed.is_set()
