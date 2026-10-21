# SPDX-License-Identifier: AGPL-3.0-only
"""The source is killed only on a watermark that stays settled while it is frozen.

Pure fakes: no node, serve, sidecar or fleet.  SIGSTOP freezes the source
process, not a token it already handed to its transport: that token can still
land at the entry after the freeze.  The fakes reproduce the local load flake:
the first read under the freeze shows ``acknowledged == committed == 3``, then
the in-flight token commits a fourth token that the frozen source can never
acknowledge.  Killing on the first read made the entry truthfully record
``watermark_behind_gateway`` (``full_context_replay``) in a positive case.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from mycelium_a7_physical_measurement import KillObservation, MeasurementRefused, MeasurementSettings, ProcessGroup, RunningRequest, measure
from tests.live.test_a7_measurement_result_poll import _NODE

_TOKENS = 8
_SETTINGS = MeasurementSettings(result_timeout_s=5.0, settle_timeout_s=0.3, poll_s=0.01)


class _Source:
    """Entry watermark of one measured request whose source the fault freezes."""

    def __init__(self, *, acknowledged_in_flight: bool = False) -> None:
        self.acknowledged_in_flight = acknowledged_in_flight
        self.lock = threading.Lock()
        self.committed = 0
        self.acknowledged = 0
        self.paused = False
        self.pauses = 0
        self.in_flight_landed = False
        self.killed = threading.Event()
        self.kill_watermark: tuple[int, int] | None = None
        self.decoded = 0

    def read(self) -> tuple[int, int]:
        with self.lock:
            if self.paused and self.pauses == 1 and self.committed == 3 and not self.in_flight_landed:
                # The token the source sent just before SIGSTOP lands after the
                # first read under the freeze; the frozen source never acks it.
                watermark = (self.committed, self.acknowledged)
                self.in_flight_landed = True
                self.committed = 4
                if self.acknowledged_in_flight:
                    # Its checkpoint was in flight too: the standby acks it.
                    self.acknowledged = 4
                return watermark
            return self.committed, self.acknowledged


class _Fault:
    transport = "local"

    def __init__(self, source: _Source) -> None:
        self._source = source

    def discover(self) -> ProcessGroup:
        return ProcessGroup(pgid=41, pids=(41,))

    def pause(self, _group: ProcessGroup) -> int:
        with self._source.lock:
            self._source.paused = True
            self._source.pauses += 1
        return 0

    def resume(self, _group: ProcessGroup) -> int:
        with self._source.lock:
            self._source.paused = False
            # Resumed, the source acknowledges what the entry committed.
            self._source.acknowledged = self._source.committed
        return 0

    def kill(self, group: ProcessGroup) -> KillObservation:
        started = time.monotonic()
        with self._source.lock:
            self._source.kill_watermark = (self._source.committed, self._source.acknowledged)
        self._source.killed.set()
        return KillObservation(group.pgid, started, time.monotonic(), 0)

    def group_gone(self, _group: ProcessGroup) -> bool:
        return True


class _Driver:
    ordinary_browser_request = False

    def __init__(self, source: _Source, *, first_settled: int = 3) -> None:
        self._source = source
        self._first_settled = first_settled

    def start(self, label: str) -> RunningRequest:
        running = RunningRequest(label=label)
        running.request_id = "request-" + label
        source = self._source

        def run() -> None:
            if label == "baseline":
                for index in range(_TOKENS):
                    running.on_token(index, 100 + index, publisher_generation=1)
                    time.sleep(0.003)
            else:
                first = self._first_settled
                for index in range(first):
                    running.on_token(index, 100 + index, publisher_generation=1)
                    time.sleep(0.003)
                with source.lock:
                    source.committed = source.acknowledged = first
                # Unkilled, the request finishes on its own.
                source.killed.wait(timeout=10 if first < _TOKENS - 1 else 1.0)
                with source.lock:
                    start = source.kill_watermark[0] if source.kill_watermark else _TOKENS
                for index in range(first, _TOKENS):
                    if index >= start:
                        with source.lock:
                            source.decoded += 1
                    running.on_token(index, 100 + index, publisher_generation=1)
                    time.sleep(0.003)
            running.on_terminal("completed")
            running.finish()

        threading.Thread(target=run, daemon=True).start()
        return running

    def workspaces_checked(self) -> list[str]:
        return []


class _Evidence:
    def __init__(self, source: _Source) -> None:
        self._source = source

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
        source = self._source
        committed, acknowledged = source.read()
        state: dict[str, Any] = {
            "committed": committed,
            "acknowledged": acknowledged,
            "commits_sent": committed,
            "ack_lag_events": 0,
            "result": None,
        }
        incidents: list[dict[str, Any]] = []
        if source.killed.is_set():
            at_kill = source.kill_watermark or (committed, acknowledged)
            behind = at_kill[1] < at_kill[0]
            state["result"] = {
                "protocol": "mycelium.a7_entry_cutover.v1",
                "outcome": "full_context_replay" if behind else "fenced_kv_successor",
                "reason": "watermark_behind_gateway" if behind else "eligible",
                "gateway_committed_token_count": at_kill[0],
                "acknowledged_token_count": at_kill[1],
                "cutover_ms": 1.0,
                "max_ack_wait_ms": 1.0,
                "kv_continuity_claimed": not behind,
            }
            incidents = [{"subject_id": "node-2", "observed_at_monotonic_ms": int(time.monotonic() * 1000)}]
        with source.lock:
            decoded = source.decoded
        return {
            "request_id": request_id,
            "a7": {
                "request": state,
                "agent": {"shadow_checkpoint_count": committed, "shadow_checkpoint_payload_bytes": 64 * committed},
                "standby_runtime": {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": decoded},
            },
            "scoped_incidents": incidents,
        }


def test_a_token_landing_under_the_freeze_is_acknowledged_before_the_kill() -> None:
    source = _Source()
    fields, observations = measure(driver=_Driver(source), evidence=_Evidence(source), fault=_Fault(source), settings=_SETTINGS)
    fault = observations["fault"]
    # The first freeze was not settled (a token landed under it): the source
    # was resumed, acknowledged it, and was killed on a settled watermark.
    assert fault["pause_attempts"] == 2
    assert fault["kill_condition"] == "acknowledged_equals_committed"
    assert source.kill_watermark == (4, 4)
    assert fault["watermark_at_kill"]["committed"] == fault["watermark_at_kill"]["acknowledged"] == 4
    assert fields["outcome"] == "fenced_kv_successor"
    assert fields["committed_before_failure"] == fields["acknowledged_before_failure"] == 4


def test_a_token_and_its_acknowledgement_landing_under_the_freeze_settle_without_a_resume() -> None:
    source = _Source(acknowledged_in_flight=True)
    fields, observations = measure(driver=_Driver(source), evidence=_Evidence(source), fault=_Fault(source), settings=_SETTINGS)
    fault = observations["fault"]
    assert fault["pause_attempts"] == 1
    assert fault["kill_condition"] == "acknowledged_equals_committed"
    assert source.kill_watermark == (4, 4)
    assert fault["watermark_at_kill"]["committed"] == fault["watermark_at_kill"]["acknowledged"] == 4
    assert fields["outcome"] == "fenced_kv_successor"


def test_the_final_decode_step_is_killed_and_cut_over() -> None:
    """Surviving stages retain their terminal-hop KV, so the final step is a cutover point."""

    source = _Source()
    fields, observations = measure(driver=_Driver(source, first_settled=_TOKENS - 1), evidence=_Evidence(source), fault=_Fault(source), settings=_SETTINGS)
    assert observations["fault"]["kill_condition"] == "acknowledged_equals_committed"
    assert source.kill_watermark == (_TOKENS - 1, _TOKENS - 1)
    assert fields["outcome"] == "fenced_kv_successor"
    assert fields["committed_before_failure"] == fields["acknowledged_before_failure"] == _TOKENS - 1
    assert source.decoded == 1


def test_a_request_with_every_token_committed_is_never_killed() -> None:
    """Nothing is left to decode: there is no source loss to measure."""

    source = _Source()
    with pytest.raises(MeasurementRefused) as caught:
        measure(driver=_Driver(source, first_settled=_TOKENS), evidence=_Evidence(source), fault=_Fault(source), settings=_SETTINGS)
    assert caught.value.code in {"a7_request_ended_before_fault", "a7_watermark_not_settled"}
    assert source.kill_watermark is None
