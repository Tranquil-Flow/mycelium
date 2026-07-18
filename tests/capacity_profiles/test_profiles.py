from __future__ import annotations

import copy
import importlib
import json
import math
from dataclasses import replace
from typing import Any

import pytest

from mycelium_gossip.schema import RecordKind, build_record


DIGEST = "sha256:" + "a" * 64


def _api():
    return importlib.import_module("mycelium_capacity_profiles")


def _key(**overrides):
    api = _api()
    values = {
        "model_digest": DIGEST,
        "source_evidence_digest": "sha256:" + "c" * 64,
        "quantization": "fp16",
        "backend": "mlx",
        "runtime_build": "mlx-0.29.0",
        "hardware_class": "apple-m4-pro-48gb",
        "power_mode": "ac",
        "context_bucket": "0-4096",
        "kv_mode": "stage-local",
    }
    values.update(overrides)
    return api.CapacityProfileKey(**values)


def _policy(**overrides):
    api = _api()
    values = {
        "ttft_p95_slo_ms": 1_000.0,
        "tpot_p95_slo_ms": 100.0,
        "min_samples": 3,
    }
    values.update(overrides)
    return api.CapacityProfilePolicy(**values)


def _point(
    concurrency: int,
    *,
    ttft: float | None = 500.0,
    tpot: float | None = 50.0,
    aggregate_tps: float | None = 10.0,
    peak_memory_bytes: int = 10,
    memory_budget_bytes: int = 100,
    sample_count: int = 5,
    oom: bool = False,
    thermal_throttled: bool = False,
):
    api = _api()
    return api.CapacityObservation(
        concurrency=concurrency,
        sample_count=sample_count,
        p95_ttft_ms=ttft,
        p95_tpot_ms=tpot,
        aggregate_output_tps=aggregate_tps,
        peak_memory_bytes=peak_memory_bytes,
        memory_budget_bytes=memory_budget_bytes,
        oom=oom,
        thermal_throttled=thermal_throttled,
    )


def test_compiler_derives_separate_safety_interactive_and_batch_limits() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(),
        (
            _point(1, ttft=500, tpot=50, aggregate_tps=10),
            _point(2, ttft=700, tpot=70, aggregate_tps=18),
            _point(3, ttft=1_200, tpot=90, aggregate_tps=24),
            _point(4, aggregate_tps=1, oom=True),
        ),
        _policy(),
    )

    assert profile.max_safe_concurrency == 3
    assert profile.interactive_concurrency_limit == 2
    assert profile.batch_concurrency_limit == 3
    assert [point.safe for point in profile.points] == [True, True, True, False]
    assert [point.interactive_slo_met for point in profile.points] == [True, True, False, False]

    document = profile.to_document()
    assert document["evidence_scope"] == "bounded_local_samples"
    assert document["qualification_evaluated"] is False
    assert document["route_ready"] is False
    assert document["release_ready"] is False
    assert document["safety_boundary"] == {
        "kind": "first_unsafe_observation",
        "concurrency": 4,
        "reasons": ["oom"],
    }
    assert document["interactive_boundary"] == {
        "kind": "first_slo_miss",
        "concurrency": 3,
        "reasons": ["ttft_p95_slo"],
    }


def test_failed_observation_needs_no_fabricated_latency_or_throughput() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(),
        (
            _point(1),
            _point(
                2,
                ttft=None,
                tpot=None,
                aggregate_tps=None,
                sample_count=1,
                oom=True,
            ),
        ),
        _policy(),
    )

    assert profile.max_safe_concurrency == 1
    assert profile.points[1].p95_ttft_ms is None
    assert profile.points[1].p95_tpot_ms is None
    assert profile.points[1].aggregate_output_tps is None


def test_first_unsafe_point_bounds_all_higher_recommendations() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(),
        (
            _point(1, aggregate_tps=10),
            _point(2, aggregate_tps=20, oom=True),
            _point(3, aggregate_tps=100),
        ),
        _policy(),
    )

    assert profile.max_safe_concurrency == 1
    assert profile.interactive_concurrency_limit == 1
    assert profile.batch_concurrency_limit == 1


def test_batch_goodput_tie_selects_lower_concurrency() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(),
        (
            _point(1, aggregate_tps=10),
            _point(2, aggregate_tps=20),
            _point(3, aggregate_tps=20),
        ),
        _policy(),
    )
    assert profile.batch_concurrency_limit == 2


@pytest.mark.parametrize(
    "points, error",
    [
        ((1, 3), "contiguous"),
        ((1, 1), "unique"),
        ((2, 3), "start at 1"),
    ],
)
def test_compiler_rejects_unobserved_or_duplicate_concurrency(
    points: tuple[int, ...], error: str
) -> None:
    api = _api()
    with pytest.raises(ValueError, match=error):
        api.compile_capacity_profile(
            _key(),
            tuple(_point(concurrency) for concurrency in points),
            _policy(),
        )


def test_observations_reject_nonfinite_missing_or_insufficient_measurements() -> None:
    api = _api()
    with pytest.raises(ValueError, match="finite"):
        _point(1, ttft=math.inf)
    with pytest.raises(ValueError, match="successful observations require"):
        _point(1, ttft=None, tpot=None, aggregate_tps=None)

    with pytest.raises(ValueError, match="min_samples"):
        api.compile_capacity_profile(
            _key(),
            (_point(1, sample_count=2),),
            _policy(min_samples=3),
        )


def test_compiler_fails_closed_without_safe_baseline() -> None:
    api = _api()
    with pytest.raises(ValueError, match="safe concurrency=1"):
        api.compile_capacity_profile(
            _key(),
            (_point(1, peak_memory_bytes=101, memory_budget_bytes=100),),
            _policy(),
        )


def test_profile_document_is_canonical_deterministic_and_identity_bound() -> None:
    api = _api()
    observations = (_point(1), _point(2, aggregate_tps=18))
    first = api.compile_capacity_profile(_key(), observations, _policy())
    second = api.compile_capacity_profile(_key(), tuple(reversed(observations)), _policy())

    assert first.profile_digest == second.profile_digest
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert json.loads(first.canonical_json_bytes()) == first.to_document()
    assert first.canonical_json_bytes() == api.canonical_json_bytes(first.to_document())

    changed = api.compile_capacity_profile(
        _key(runtime_build="mlx-0.29.1"), observations, _policy()
    )
    changed_evidence = api.compile_capacity_profile(
        _key(source_evidence_digest="sha256:" + "d" * 64),
        observations,
        _policy(),
    )
    assert changed.profile_digest != first.profile_digest
    assert changed_evidence.profile_digest != first.profile_digest


def test_profile_rejects_forged_direct_construction() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(), (_point(1), _point(2, aggregate_tps=18)), _policy()
    )

    with pytest.raises(ValueError, match="derived capacity limits"):
        replace(profile, max_safe_concurrency=999)
    with pytest.raises(ValueError, match="derived capacity limits"):
        replace(profile, interactive_concurrency_limit=999)
    with pytest.raises(ValueError, match="derived capacity limits"):
        replace(profile, batch_concurrency_limit=999)
    single = api.compile_capacity_profile(_key(), (_point(1),), _policy())
    for field in (
        "max_safe_concurrency",
        "interactive_concurrency_limit",
        "batch_concurrency_limit",
    ):
        with pytest.raises(ValueError, match="exact integers"):
            replace(single, **{field: True})


def _status_payload() -> dict[str, Any]:
    return {
        "protocol": "mycelium.device_status.v1",
        "node_id": "node-a",
        "lifecycle": "ready",
        "memory_domains": [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": 1_000,
            }
        ],
        "queue_depth": 0,
        "in_flight": 0,
        "concurrency_limit": 99,
        "extensions": {"operator_mode": "local"},
    }


def test_status_adapter_is_nonmutating_schema_valid_and_uses_interactive_limit() -> None:
    api = _api()
    profile = api.compile_capacity_profile(
        _key(),
        (
            _point(1, aggregate_tps=10),
            _point(2, aggregate_tps=20),
            _point(3, ttft=1_500, aggregate_tps=30),
        ),
        _policy(),
    )
    source = _status_payload()
    before = copy.deepcopy(source)

    with pytest.raises(ValueError, match="explicit authorization"):
        api.status_with_capacity_profile(source, profile)
    adapted = api.status_with_capacity_profile(
        source, profile, allow_concurrency_limit_update=True
    )

    assert source == before
    assert adapted["concurrency_limit"] == 2
    assert adapted["extensions"]["operator_mode"] == "local"
    summary = adapted["extensions"]["capacity_profile"]
    assert summary == {
        "protocol": "mycelium.capacity_profile_ref.v1",
        "profile_digest": profile.profile_digest,
        "max_safe_concurrency": 3,
        "interactive_concurrency_limit": 2,
        "batch_concurrency_limit": 3,
        "evidence_scope": "bounded_local_samples",
        "route_ready": False,
    }
    build_record(
        swarm_id="swarm-a",
        kind=RecordKind.STATUS,
        origin_node_id="node-a",
        incarnation=1,
        sequence=1,
        boot_id="boot-a",
        generated_at_unix_ms=0,
        ttl_ms=1_000,
        payload=adapted,
    )
    assert (
        api.status_with_capacity_profile(
            adapted, profile, allow_concurrency_limit_update=True
        )
        == adapted
    )


def test_status_adapter_rejects_conflicting_profile_or_wrong_protocol() -> None:
    api = _api()
    profile = api.compile_capacity_profile(_key(), (_point(1),), _policy())
    conflicting = _status_payload()
    conflicting["extensions"]["capacity_profile"] = {"profile_digest": "sha256:" + "b" * 64}

    with pytest.raises(ValueError, match="conflicting capacity profile"):
        api.status_with_capacity_profile(
            conflicting, profile, allow_concurrency_limit_update=True
        )

    wrong_protocol = _status_payload()
    wrong_protocol["protocol"] = "mycelium.device_status.v2"
    with pytest.raises(ValueError, match="device_status.v1"):
        api.status_with_capacity_profile(
            wrong_protocol, profile, allow_concurrency_limit_update=True
        )
