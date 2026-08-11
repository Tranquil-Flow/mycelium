from __future__ import annotations

import copy

import pytest

from mycelium_evidence import (
    EvidenceProjectionRegistry,
    evidence_is_current_live,
    sealed_evidence_projection,
    validate_evidence_projection,
)


def _runtime(*, frames_sent: int = 1) -> dict[str, object]:
    return {
        "protocol": "mycelium.live_route_status.v1",
        "route_alive": True,
        "counters": {"frames_sent": frames_sent, "fatal": None},
    }


def _historical() -> dict[str, object]:
    return sealed_evidence_projection(
        record_id="replication-plan-a",
        capability="replicated_serving",
        authority="mycelium_m18_replication:build_replica_plan",
        generation=7,
        observed_at_unix_ms=1_000,
        payload={"protocol": "mycelium.replica_plan.v1", "plan_digest": "sha256:a"},
    )


def test_runtime_generation_changes_only_with_runtime_payload() -> None:
    state = _runtime()
    times = iter((2_000, 2_100, 2_200))
    registry = EvidenceProjectionRegistry(
        runtime_source=lambda: state,
        clock_unix_ms=lambda: next(times),
        incarnation="test",
    )

    first = registry.runtime()
    second = registry.runtime()
    state["counters"] = {"frames_sent": 2, "fatal": None}
    third = registry.runtime()

    assert first["generation"] == second["generation"] == 1
    assert first["observed_at_unix_ms"] == second["observed_at_unix_ms"] == 2_000
    assert second["captured_at_unix_ms"] == 2_100
    assert third["generation"] == 2
    assert third["observed_at_unix_ms"] == 2_200


def test_historical_time_and_label_never_refresh() -> None:
    registry = EvidenceProjectionRegistry(
        runtime_source=_runtime,
        historical_records=[_historical()],
    )

    first = registry.history()
    second = registry.history(capability="replicated_serving")

    assert first == second
    assert first["records"][0]["source_kind"] == "sealed_historical"
    assert first["records"][0]["freshness"] == "historical"
    assert first["records"][0]["observed_at_unix_ms"] == 1_000


def test_sealed_record_cannot_claim_current() -> None:
    invalid = _historical()
    invalid["freshness"] = "current"
    with pytest.raises(ValueError, match="source_freshness_mismatch"):
        validate_evidence_projection(invalid)


def test_stale_or_historical_evidence_never_counts_as_current_live() -> None:
    runtime = EvidenceProjectionRegistry(
        runtime_source=_runtime,
        clock_unix_ms=lambda: 2_000,
        incarnation="test",
    ).runtime()
    assert evidence_is_current_live(runtime, now_unix_ms=2_500) is True
    assert evidence_is_current_live(runtime, now_unix_ms=5_001) is False
    assert evidence_is_current_live(_historical(), now_unix_ms=2_500) is False


def test_projection_rejects_unknown_fields_and_protocol_mismatch() -> None:
    unknown = _historical()
    unknown["private_path"] = "/secret"
    with pytest.raises(ValueError, match="shape"):
        validate_evidence_projection(unknown)

    mismatch = copy.deepcopy(_historical())
    mismatch["payload_protocol"] = "wrong.protocol"
    with pytest.raises(ValueError, match="payload_protocol_mismatch"):
        validate_evidence_projection(mismatch)


def test_divergent_duplicate_history_fails_closed() -> None:
    first = _historical()
    second = copy.deepcopy(first)
    second["payload"]["plan_digest"] = "sha256:b"
    with pytest.raises(ValueError, match="historical_record_conflict"):
        EvidenceProjectionRegistry(
            runtime_source=_runtime,
            historical_records=[first, second],
        )
