# SPDX-License-Identifier: AGPL-3.0-only
"""A KV-successor cutover's decode counters are read after the successor decodes.

Pure fakes: no node, serve, sidecar or fleet.  The fakes reproduce the
physical ``ordinary_browser_source_kill_kv_successor`` shape: the entry writes
its ``fenced_kv_successor`` cutover record (and the source incident) at the
kill, before the standby decodes anything.  The standby then decodes every
remaining token, the released route cold-restarts, and the terminal is held
until the restarted entry answers with its durable record, whose counters
belong to another process.  A live counter read must therefore land after the
successor's decode, not at cutover.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from mycelium_a7_physical_measurement import MeasurementRefused, MeasurementSettings, RunningRequest, measure
from tests.live.test_a7_measurement_result_poll import _NODE, _Fault

_TOKENS = 8
_BEFORE_KILL = 3
_RESULT = {
    "protocol": "mycelium.a7_entry_cutover.v1",
    "outcome": "fenced_kv_successor",
    "reason": "eligible",
    "gateway_committed_token_count": _BEFORE_KILL,
    "acknowledged_token_count": _BEFORE_KILL,
    "cutover_ms": 1.5,
    "max_ack_wait_ms": 2.0,
    "kv_continuity_claimed": True,
    "terminal_released": False,
}
_SETTINGS = MeasurementSettings(result_timeout_s=5.0, settle_timeout_s=0.3, poll_s=0.01)


class _Route:
    """Shared state of the serving entry: live, restarting, then restored."""

    def __init__(self, fault: _Fault) -> None:
        self.fault = fault
        self.decoded = 0
        self.phase = "live"
        self.refusals_left = 5
        self.killed_at_ms: int | None = None
        self.live_read_after_kill = threading.Event()
        self.live_read_seen = threading.Event()
        self.restored_answered = threading.Event()
        self.lock = threading.Lock()


class _Driver:
    ordinary_browser_request = True

    def __init__(self, route: _Route) -> None:
        self._route = route

    def start(self, label: str) -> RunningRequest:
        running = RunningRequest(label=label)
        running.request_id = "request-" + label
        route = self._route

        def run() -> None:
            if label == "baseline":
                for index in range(_TOKENS):
                    running.on_token(index, 100 + index, publisher_generation=1)
                    time.sleep(0.005)
                running.on_terminal("completed")
                running.finish()
                return
            for index in range(_BEFORE_KILL):
                running.on_token(index, 100 + index, publisher_generation=1)
                time.sleep(0.005)
            route.fault.killed.wait(timeout=10)
            # The cutover record is already written; the successor decodes only after it was read.
            route.live_read_after_kill.wait(timeout=10)
            for index in range(_BEFORE_KILL, _TOKENS):
                with route.lock:
                    route.decoded += 1
                    route.live_read_seen.clear()
                running.on_token(index, 100 + index, publisher_generation=1)
                if index < _TOKENS - 1:
                    route.live_read_seen.wait(timeout=0.5)
            # Releasing the last request cold-restarts the route at once (no
            # live read follows the last token); the terminal is sealed only
            # once the replacement answers.
            with route.lock:
                route.phase = "restarting"
            route.restored_answered.wait(timeout=3)
            running.on_terminal("completed")
            running.finish()

        threading.Thread(target=run, daemon=True).start()
        return running

    def workspaces_checked(self) -> list[str]:
        return []


class _Evidence:
    def __init__(self, route: _Route) -> None:
        self._route = route

    def resources(self) -> dict[str, Any]:
        return {
            "entry_node_id": "node-0",
            "exited_nodes": [],
            "gateway_reservations": 0,
            "route_request_registrations": 0,
            "nodes": {
                "node-0": {
                    **_NODE,
                    "a7_roles": ["entry", "standby"],
                    "standby_runtime": {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": 0},
                },
                "node-2": {**_NODE, "a7_roles": ["source"], "standby_runtime": None},
            },
        }

    def request(self, request_id: str) -> dict[str, Any]:
        route = self._route
        state: dict[str, Any] = {"committed": _BEFORE_KILL, "acknowledged": _BEFORE_KILL, "commits_sent": _BEFORE_KILL, "ack_lag_events": 0, "result": None}
        incidents: list[dict[str, Any]] = []
        with route.lock:
            killed = route.fault.killed.is_set()
            phase = route.phase
            decoded = route.decoded
            if killed and route.killed_at_ms is None:
                route.killed_at_ms = int(time.monotonic() * 1000)
            if phase == "restarting":
                if route.refusals_left > 0:
                    route.refusals_left -= 1
                    raise MeasurementRefused("a7_evidence_route_unavailable")
                route.phase = phase = "restored"
        counters = {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": decoded}
        if killed:
            state = {**state, "source_lost_accepted": True, "old_path_fenced": True, "result": dict(_RESULT)}
            incidents = [{"subject_id": "node-2", "observed_at_monotonic_ms": route.killed_at_ms}]
            if phase == "restored":
                state = {**state, "finished": True, "restored_from": "durable_last_cutover"}
                counters = {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": 0}
                incidents = []
                route.restored_answered.set()
            else:
                route.live_read_after_kill.set()
                route.live_read_seen.set()
        return {
            "request_id": request_id,
            "a7": {
                "request": state,
                "agent": {"shadow_checkpoint_count": 2, "shadow_checkpoint_payload_bytes": 64},
                "standby_runtime": counters,
            },
            "scoped_incidents": incidents,
        }


def test_successor_decode_is_counted_from_a_live_read_after_the_successor_decoded() -> None:
    fault = _Fault()
    route = _Route(fault)
    fields, observations = measure(driver=_Driver(route), evidence=_Evidence(route), fault=fault, settings=_SETTINGS)
    assert fields["outcome"] == "fenced_kv_successor"
    assert fields["reference_parity"] is True
    assert fields["missing_tokens"] == 0
    assert fields["successor_decode_operations"] >= 1
    reads = observations["evidence_reads"]
    assert reads["counter_read"] != "pre_kill"
    assert reads["counter_read"].startswith("poll:")
    assert observations["result_poll"]["stop"] == "result_observed"
    assert observations["measured"]["terminals"] == ["completed"]
    assert route.restored_answered.is_set()
