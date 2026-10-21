# SPDX-License-Identifier: AGPL-3.0-only
"""A protocol v2 gate run is sealed as mycelium.a5_benchmark_evidence.v2.

The envelope binds the pre-registered v2 protocol digest and carries a
validated mycelium.replica_benchmark.v2 record derived field for field from the
harness decision.  The v1 envelope is never emitted for a v2 run.
"""

from __future__ import annotations

import copy

import pytest

import scripts.run_a5_benchmark_gate as benchmark_gate
from mycelium_replica_contracts import (
    REPLICA_BENCHMARK_V2_PROTOCOL,
    validate_replica_benchmark_v2,
)

from .materiality_harness import (
    CALIBRATION_FAILED_REASON,
    evaluate_benchmark_v2,
    protocol_v2_digest,
)
from .test_materiality_protocol_v2 import LADDER, _drive_gate, _run_v2

PROVENANCE = {
    "software_digest": "sha256:" + "d" * 64,
    "configuration_digest": "sha256:" + "c" * 64,
    "instrumentation_digest": "sha256:" + "e" * 64,
}


def _seal(fixture: dict, **overrides) -> dict:
    arguments = {
        "fixture": fixture,
        "decision": evaluate_benchmark_v2(fixture),
        "deployment_id": "deployment-gate-test",
        "route_generation": 7,
        "provenance": PROVENANCE,
        "started_at_unix_ms": 1_000,
        "finished_at_unix_ms": 2_000,
        "observed": {"arrival_rate_per_second": 1.0},
    }
    arguments.update(overrides)
    return benchmark_gate.seal_benchmark_evidence(**arguments)


def test_gate_run_is_sealed_as_the_v2_envelope(monkeypatch) -> None:
    report, _ = _drive_gate(monkeypatch, {4.0: 12, 2.0: 55})

    assert report["protocol"] == "mycelium.a5_benchmark_evidence.v2"
    assert report["protocol"] != "mycelium.a5_benchmark_evidence.v1"
    assert report["benchmark_protocol"] == "mycelium.a5_benchmark_acceptance.v2"
    assert report["benchmark_protocol_digest"] == protocol_v2_digest()
    assert report["qualification_claim"] is False
    assert report["promotion_authorized"] is False
    record = report["replica_benchmark"]
    assert validate_replica_benchmark_v2(record) == record
    assert record["protocol"] == REPLICA_BENCHMARK_V2_PROTOCOL
    assert record["benchmark_protocol_digest"] == protocol_v2_digest()
    decision = report["decision"]
    assert record["decision"] == decision["decision"] == "material"
    assert record["valid_pair_ids"] == decision["valid_pair_ids"] == list(range(16))
    assert record["paired_improvements"] == decision["paired_improvements"]
    assert record["excluded_windows"] == decision["excluded_windows"] == []
    assert record["calibration"]["selected_offered_arrival_rate_per_second"] == 2.0
    assert [trial["completed"] for trial in record["calibration"]["trials"]] == [12, 55]
    assert record["deployment_id"] == "deployment-gate-test"
    assert record["started_at_unix_ms"] <= record["finished_at_unix_ms"]


def test_failed_calibration_is_sealed_as_unscored_v2_evidence(monkeypatch) -> None:
    report, _ = _drive_gate(monkeypatch, {rate: 30 for rate in LADDER})

    assert report["protocol"] == "mycelium.a5_benchmark_evidence.v2"
    record = report["replica_benchmark"]
    assert record["decision"] == "inconclusive"
    assert record["reasons"] == [CALIBRATION_FAILED_REASON]
    assert record["calibration"]["selected_offered_arrival_rate_per_second"] is None
    assert record["paired_improvements"] == []
    assert record["point_estimate_fraction"] is None


def test_excluded_windows_are_carried_into_the_record() -> None:
    fixture = _run_v2([0.2] * 16, baseline_completed={1: 50, 6: 40})
    evidence = _seal(fixture)
    record = evidence["replica_benchmark"]
    assert [window["pair_id"] for window in record["excluded_windows"]] == [1, 6]
    assert [window["baseline_index"] for window in record["excluded_windows"]] == [3, 12]
    assert record["valid_pair_ids"] == [p for p in range(16) if p not in {1, 6}]
    assert len(record["paired_improvements"]) == 14
    assert record["decision"] == "material"


def test_too_few_valid_pairs_are_sealed_inconclusive() -> None:
    fixture = _run_v2([0.5] * 16, baseline_completed={p: 10 for p in range(5)})
    record = _seal(fixture)["replica_benchmark"]
    assert record["decision"] == "inconclusive"
    assert len(record["valid_pair_ids"]) == 11
    assert record["paired_improvements"] == []


def test_a_decision_the_record_does_not_support_is_refused() -> None:
    fixture = _run_v2([0.2] * 16, baseline_completed={p: 10 for p in range(5)})
    forged = copy.deepcopy(evaluate_benchmark_v2(fixture))
    assert forged["decision"] == "inconclusive"
    forged["decision"] = "material"
    with pytest.raises(benchmark_gate.GateError, match="replica_benchmark_v2_invalid"):
        _seal(fixture, decision=forged)


def test_a_run_bound_to_another_protocol_digest_is_refused() -> None:
    fixture = _run_v2([0.2] * 16)
    decision = evaluate_benchmark_v2(fixture)
    fixture["benchmark_protocol_digest"] = "sha256:" + "5" * 64
    with pytest.raises(
        benchmark_gate.GateError, match="benchmark_protocol_not_pre_registered"
    ):
        _seal(fixture, decision=decision)


def test_a_missing_deployment_identity_is_refused_not_invented() -> None:
    fixture = _run_v2([0.2] * 16)
    with pytest.raises(benchmark_gate.GateError, match="replica_benchmark_v2_invalid"):
        _seal(fixture, deployment_id=None)
