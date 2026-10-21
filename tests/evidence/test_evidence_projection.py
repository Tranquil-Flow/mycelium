# SPDX-License-Identifier: AGPL-3.0-only
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


def test_private_nested_payload_fails_closed() -> None:
    bad = _historical()
    payload = bad["payload"]
    assert isinstance(payload, dict)
    payload["nested"] = {"prompt": "PRIVATE"}
    with pytest.raises(ValueError, match="privacy"):
        validate_evidence_projection(bad)


@pytest.mark.parametrize("field,value", [
    ("source_kind", "unknown"), ("capability", "unknown"),
    ("observed_at_unix_ms", 2_000),
])
def test_unknown_source_capability_and_future_observation_fail_closed(field: str, value: object) -> None:
    bad = _historical()
    bad[field] = value
    with pytest.raises(ValueError):
        validate_evidence_projection(bad)


def test_identical_duplicate_history_is_idempotent_and_sorted() -> None:
    later = _historical()
    later["record_id"] = "later"
    later["observed_at_unix_ms"] = 2_000
    later["captured_at_unix_ms"] = 2_000
    first = _historical()
    registry = EvidenceProjectionRegistry(runtime_source=_runtime, historical_records=[later, first, copy.deepcopy(first)])
    assert [item["record_id"] for item in registry.history()["records"]] == [first["record_id"], "later"]

@pytest.mark.parametrize("field,value", [
    ("authority", "https://canary.invalid/private"),
    ("record_id", "/Users/canary/private"),
    ("payload_protocol", "Bearer canary"),
])
def test_private_envelope_metadata_refused_at_seal_and_validation(field: str, value: str) -> None:
    bad = _historical()
    bad[field] = value
    if field == "payload_protocol":
        payload = bad["payload"]
        assert isinstance(payload, dict)
        payload["protocol"] = value
    with pytest.raises(ValueError, match="privacy"):
        validate_evidence_projection(bad)
    if field == "authority":
        with pytest.raises(ValueError, match="privacy"):
            sealed_evidence_projection(record_id="safe-id", capability="replicated_serving", authority=value,
                generation=0, observed_at_unix_ms=1000, payload={"protocol": "safe.protocol"})
    if field == "record_id":
        with pytest.raises(ValueError, match="privacy"):
            sealed_evidence_projection(record_id=value, capability="replicated_serving", authority="public-source",
                generation=0, observed_at_unix_ms=1000, payload={"protocol": "safe.protocol"})

@pytest.mark.parametrize("field", ["generation", "captured_at_unix_ms", "observed_at_unix_ms"])
def test_projection_uses_js_safe_integer_boundary(field: str) -> None:
    safe = _historical()
    safe[field] = 9007199254740991
    if field == "observed_at_unix_ms":
        safe["captured_at_unix_ms"] = safe[field]
    elif field == "captured_at_unix_ms":
        safe["observed_at_unix_ms"] = safe[field]
    assert validate_evidence_projection(safe)[field] == 9007199254740991
    safe[field] = 9007199254740992
    if field == "observed_at_unix_ms":
        safe["captured_at_unix_ms"] = safe[field]
    with pytest.raises(ValueError, match="invalid"):
        validate_evidence_projection(safe)

def test_live_expiration_uses_js_safe_integer_boundary() -> None:
    live = EvidenceProjectionRegistry(runtime_source=_runtime, clock_unix_ms=lambda: 1000).runtime()
    live["valid_until_unix_ms"] = 9007199254740991
    assert validate_evidence_projection(live)["valid_until_unix_ms"] == 9007199254740991
    live["valid_until_unix_ms"] += 1
    with pytest.raises(ValueError, match="invalid"):
        validate_evidence_projection(live)

def test_history_exact_bound_applies_after_dedup_without_loss() -> None:
    def records(n: int) -> list[dict[str, object]]:
        return [dict(_historical(), record_id=f"record-{i:03}",
                     captured_at_unix_ms=1000+i, observed_at_unix_ms=1000+i) for i in range(n)]
    for n in (0, 1, 128):
        history = EvidenceProjectionRegistry(runtime_source=_runtime, historical_records=records(n)).history()
        assert len(history["records"]) == n
        assert [r["record_id"] for r in history["records"]] == [f"record-{i:03}" for i in range(n)]
    many = records(128)
    assert len(EvidenceProjectionRegistry(runtime_source=_runtime, historical_records=many + [copy.deepcopy(many[0])]).history()["records"]) == 128
    with pytest.raises(ValueError, match="history.*invalid"):
        EvidenceProjectionRegistry(runtime_source=_runtime, historical_records=records(129))
