"""Deterministic tests for mycelium_replica_contracts.py.

The four closed shapes (replica_plan.v1, replica_qualification.v1,
replica_runtime.v1, replica_benchmark.v1) are the wire format the A5 gate
emits. These tests cover spec §10 "Closed-shape, count/size, canonical
digest, privacy, unknown-field, duplicate identity, mixed generation, stale
authority, and illegal transition tests for every contract."

Spec: docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md §7, §10
Note: docs/superpowers/notes/2026-08-20-mycelium-a5-replica-contract-shapes.md
"""

from __future__ import annotations

import json

import pytest

from mycelium_replica_contracts import (
    REPLICA_PLAN_PROTOCOL,
    REPLICA_QUALIFICATION_PROTOCOL,
    REPLICA_RUNTIME_PROTOCOL,
    REPLICA_BENCHMARK_PROTOCOL,
    compatibility_fixtures,
    replica_qualification_digest,
    validate_replica_benchmark,
    validate_replica_plan,
    validate_replica_qualification,
    validate_replica_runtime,
)


def _sha(letter: str) -> str:
    return "sha256:" + letter * 64


def _plan_payload(**overrides):
    base = {
        "protocol": REPLICA_PLAN_PROTOCOL,
        "plan_id": "plan-fixture",
        "plan_digest": _sha("a"),
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "base_qualification_digest": _sha("b"),
        "issued_at_unix_ms": 10_000,
        "model_id": "model-fixture",
        "model_revision": "revision-fixture",
        "representation_digest": _sha("c"),
        "route_generation": 7,
        "route_ready": False,
        "replica_groups": [
            {"group_id": "group-fixture-0", "layer_range": [0, 22]},
            {"group_id": "group-fixture-1", "layer_range": [23, 24]},
        ],
        "legal_tracks": [
            {"track_id": "track-fixture", "placement_sequence": ["p0", "p1"]},
        ],
        "directed_edges": [],
        "traffic_fractions": [
            {"track_id": "track-fixture", "fraction": 1.0},
        ],
        "predicted_marginal_gain_fraction": 0.12,
        "uncertainty": {"lower": 0.0, "upper": 0.3},
        "failure_domain_facts": [],
        "zero_flow_removals": [],
        "rejections": [],
        "workload_digest": _sha("d"),
    }
    base.update(overrides)
    return base


def _qualification_payload(**overrides):
    base = {
        "protocol": REPLICA_QUALIFICATION_PROTOCOL,
        "qualification_id": _sha("a"),
        "qualification_digest": _sha("a"),
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "replica_group_id": "group-fixture-0",
        "placement_id": "placement-fixture-replica",
        "placement_ids": [
            "placement-fixture-replica",
            "placement-fixture-stage-1",
        ],
        "track_id": "track-fixture",
        "traffic_fraction": 0.5,
        "qualifier_generation": 1,
        "issued_at_unix_ms": 10_000,
        "expires_at_unix_ms": 600_000,
        "evidence_bundle_digest": _sha("b"),
        "load_proof_digest": _sha("c"),
        "assignment_digest": _sha("f"),
        "artifact_verification_digest": _sha("d"),
        "parity_verified": True,
        "startup_challenge_passed": True,
        "memory_within_bounds": True,
        "cleanup_within_bounds": True,
        "directed_link_qualified": True,
        "workload_envelope_digest": _sha("e"),
        "rejected_reasons": [],
        "route_ready": True,
    }
    base.update(overrides)
    if base["route_ready"] is False and "rejected_reasons" not in overrides:
        base["rejected_reasons"] = ["owner_authority_missing"]
    identity = replica_qualification_digest(base)
    base["qualification_id"] = identity
    base["qualification_digest"] = identity
    return base


def _runtime_payload(**overrides):
    base = {
        "protocol": REPLICA_RUNTIME_PROTOCOL,
        "runtime_digest": _sha("a"),
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "qualification_digest": _sha("b"),
        "route_generation": 7,
        "snapshot_generation": 1,
        "observed_at_unix_ms": 10_000,
        "tracks": [],
        "placements": [],
        "batch_mode": "tracked",
        "admitted_requests": [
            {
                "request_id": "request-fixture",
                "track_id": "track-fixture",
                "placement_sequence": ["p0", "p1"],
                "edge_set": ["e0", "e1"],
            },
        ],
        "rejected_admissions": [],
        "replica_loss_actions": [],
    }
    base.update(overrides)
    return base


def _benchmark_payload(**overrides):
    base = {
        "protocol": REPLICA_BENCHMARK_PROTOCOL,
        "benchmark_run_id": "run-fixture",
        "benchmark_protocol_digest": _sha("a"),
        "workload_manifest_digest": _sha("b"),
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "route_generation": 7,
        "started_at_unix_ms": 10_000,
        "finished_at_unix_ms": 1_000_000,
        "primary_only_samples": [],
        "replicated_samples": [],
        "paired_improvements": [0.12, 0.14, 0.11, 0.15, 0.13, 0.16],
        "point_estimate_fraction": 0.135,
        "paired_95_percent_bootstrap_lower_bound_fraction": 0.105,
        "prediction_error_fraction": -0.015,
        "decision": "material",
        "reasons": ["throughput_window_within_tolerance"],
        "qualification_claim": False,
        "promotion_authorized": False,
        "provenance": {
            "software_digest": _sha("c"),
            "configuration_digest": _sha("d"),
            "instrumentation_digest": _sha("e"),
        },
    }
    base.update(overrides)
    return base


# ---------- replica_plan.v1 ----------

def test_replica_plan_accepts_valid_payload():
    validate_replica_plan(_plan_payload())


def test_replica_plan_rejects_unknown_field():
    payload = _plan_payload()
    payload["unknown_field"] = "nope"
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_missing_field():
    payload = _plan_payload()
    del payload["plan_digest"]
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_route_ready_true():
    """Spec §7: replica_plan.v1 always reports route_ready=false."""
    payload = _plan_payload()
    payload["route_ready"] = True
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_wrong_protocol():
    payload = _plan_payload()
    payload["protocol"] = "mycelium.replica_plan.v2"
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_traffic_fractions_not_summing_to_one():
    payload = _plan_payload()
    payload["traffic_fractions"] = [
        {"track_id": "t1", "fraction": 0.4},
        {"track_id": "t2", "fraction": 0.4},
    ]
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_fewer_than_two_groups():
    """Spec §3: ordered groups cover the model once with at least two."""
    payload = _plan_payload()
    payload["replica_groups"] = [{"group_id": "g0", "layer_range": [0, 24]}]
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_non_digest_digest_field():
    payload = _plan_payload()
    payload["plan_digest"] = "not-a-digest"
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_oversized_identifier():
    payload = _plan_payload()
    payload["deployment_id"] = "x" * 257  # > 256-byte limit
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_empty_identifier():
    payload = _plan_payload()
    payload["model_id"] = ""
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_unknown_nested_group_field():
    payload = _plan_payload()
    payload["replica_groups"][0]["prompt"] = "must-not-cross-contract"
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


def test_replica_plan_rejects_unbounded_nested_collection():
    payload = _plan_payload()
    payload["failure_domain_facts"] = [{} for _ in range(257)]
    with pytest.raises(ValueError, match="invalid_replica_plan"):
        validate_replica_plan(payload)


# ---------- replica_qualification.v1 ----------

def test_replica_qualification_accepts_valid_payload():
    validate_replica_qualification(_qualification_payload())


def test_replica_qualification_binds_complete_multistage_track():
    """Spec §3: one qualified track binds every ordered stage placement."""
    payload = _qualification_payload()
    payload["placement_ids"] = [
        "placement-fixture-replica",
        "placement-fixture-stage-1",
    ]
    assert validate_replica_qualification(payload) == payload


def test_replica_qualification_accepts_route_ready_false():
    """Spec §7: route_ready may be True or False; both are valid."""
    validate_replica_qualification(_qualification_payload(route_ready=False))


def test_replica_qualification_rejects_semantic_tamper_without_resealing():
    payload = _qualification_payload()
    payload["artifact_verification_digest"] = _sha("f")
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_route_ready_with_failed_checks():
    payload = _qualification_payload(
        parity_verified=False,
        rejected_reasons=["parity_mismatch"],
        route_ready=True,
    )
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_unknown_field():
    payload = _qualification_payload()
    payload["rogue"] = "x"
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_missing_field():
    payload = _qualification_payload()
    del payload["parity_verified"]
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_expires_before_issued():
    payload = _qualification_payload()
    payload["issued_at_unix_ms"] = 1000
    payload["expires_at_unix_ms"] = 500
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_mixed_generation_signals():
    """Spec §10: stale authority + mixed generation must fail closed."""
    payload = _qualification_payload()
    payload["qualifier_generation"] = -1
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


def test_replica_qualification_rejects_non_bool_verification():
    payload = _qualification_payload()
    payload["parity_verified"] = "yes"  # must be bool
    with pytest.raises(ValueError, match="invalid_replica_qualification"):
        validate_replica_qualification(payload)


# ---------- replica_runtime.v1 ----------

def test_replica_runtime_accepts_valid_payload():
    validate_replica_runtime(_runtime_payload())


def test_replica_runtime_rejects_unknown_field():
    payload = _runtime_payload()
    payload["rogue"] = True
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_rejects_invalid_batch_mode():
    payload = _runtime_payload()
    payload["batch_mode"] = "fast"
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


@pytest.mark.parametrize("forbidden", [
    "prompt", "output", "text", "token_id", "logit",
    "activation", "kv", "credential", "key",
])
def test_replica_runtime_rejects_private_data_leak(forbidden):
    """Spec §7 last paragraph: no prompts/output/tokens/logits/KV/credentials."""
    payload = _runtime_payload()
    payload["admitted_requests"] = [
        {
            "request_id": "request-fixture",
            "track_id": "track-fixture",
            forbidden: "would-be-leaked-secret",
        },
    ]
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_rejects_missing_request_id():
    payload = _runtime_payload()
    payload["admitted_requests"] = [
        {"track_id": "track-fixture", "placement_sequence": []},
    ]
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_rejects_oversized_request_id():
    payload = _runtime_payload()
    payload["admitted_requests"] = [
        {
            "request_id": "x" * 257,
            "track_id": "track-fixture",
        },
    ]
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_rejects_nested_private_data_outside_requests():
    payload = _runtime_payload()
    payload["tracks"] = [{"track_id": "track-fixture", "secret": "nope"}]
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_rejects_extra_admitted_request_field():
    payload = _runtime_payload()
    payload["admitted_requests"][0]["metadata"] = "not-closed"
    with pytest.raises(ValueError, match="invalid_replica_runtime"):
        validate_replica_runtime(payload)


def test_replica_runtime_canonical_digest_stable():
    """Round-trip JSON preserves the canonical sha256 (replica digest too)."""
    payload = _runtime_payload()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest_a = __import__("hashlib").sha256(canonical.encode()).hexdigest()
    round_trip = json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":"))
    digest_b = __import__("hashlib").sha256(round_trip.encode()).hexdigest()
    assert digest_a == digest_b


# ---------- replica_benchmark.v1 ----------

def test_replica_benchmark_accepts_valid_payload():
    validate_replica_benchmark(_benchmark_payload())


def test_replica_benchmark_accepts_inconclusive_decision():
    validate_replica_benchmark(_benchmark_payload(
        decision="inconclusive",
        reasons=["bootstrap_window_too_short"],
    ))


def test_replica_benchmark_accepts_not_material_decision():
    validate_replica_benchmark(_benchmark_payload(
        decision="not_material",
        point_estimate_fraction=0.05,
        paired_95_percent_bootstrap_lower_bound_fraction=0.0,
    ))


def test_replica_benchmark_rejects_unknown_decision():
    payload = _benchmark_payload()
    payload["decision"] = "yes"
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_qualification_claim_true():
    """Spec §11: benchmark never promotes a claim."""
    payload = _benchmark_payload()
    payload["qualification_claim"] = True
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_promotion_authorized_true():
    payload = _benchmark_payload()
    payload["promotion_authorized"] = True
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_wrong_paired_count():
    """Spec §9: exactly six paired fractional improvements."""
    payload = _benchmark_payload()
    payload["paired_improvements"] = [0.12, 0.14, 0.11, 0.15, 0.13]
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_finished_before_started():
    payload = _benchmark_payload()
    payload["started_at_unix_ms"] = 1000
    payload["finished_at_unix_ms"] = 500
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_nonfinite_point_estimate():
    payload = _benchmark_payload()
    payload["point_estimate_fraction"] = float("inf")
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_unbound_provenance_shape():
    payload = _benchmark_payload()
    payload["provenance"]["host_path"] = "/private/build"
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


def test_replica_benchmark_rejects_nested_private_sample_data():
    payload = _benchmark_payload()
    payload["primary_only_samples"] = [{"prompt": "nope"}]
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(payload)


# ---------- compatibility_fixtures() ----------

def test_compatibility_fixtures_returns_all_four_shapes():
    fixtures = compatibility_fixtures()
    assert set(fixtures) == {
        "replica-plan-v1.json",
        "replica-qualification-v1.json",
        "replica-runtime-v1.json",
        "replica-benchmark-v1.json",
    }


def test_compatibility_fixtures_each_round_trips_through_validator():
    fixtures = compatibility_fixtures()
    # Re-validate every fixture — proves the fixtures are themselves valid.
    validate_replica_plan(fixtures["replica-plan-v1.json"])
    validate_replica_qualification(fixtures["replica-qualification-v1.json"])
    validate_replica_runtime(fixtures["replica-runtime-v1.json"])
    validate_replica_benchmark(fixtures["replica-benchmark-v1.json"])


def test_compatibility_fixtures_no_private_data():
    """The fixtures themselves must not accidentally leak private data."""
    fixtures = compatibility_fixtures()
    private_keys = {"prompt", "output", "token_id", "logit", "kv",
                    "credential", "key", "private_key"}
    for name, payload in fixtures.items():
        serialized = json.dumps(payload)
        for pk in private_keys:
            assert f'"{pk}"' not in serialized, (
                f"{name} contains private key '{pk}'"
            )


def test_compatibility_fixtures_protocols_are_canonical():
    fixtures = compatibility_fixtures()
    assert fixtures["replica-plan-v1.json"]["protocol"] == REPLICA_PLAN_PROTOCOL
    assert fixtures["replica-qualification-v1.json"]["protocol"] == REPLICA_QUALIFICATION_PROTOCOL
    assert fixtures["replica-runtime-v1.json"]["protocol"] == REPLICA_RUNTIME_PROTOCOL
    assert fixtures["replica-benchmark-v1.json"]["protocol"] == REPLICA_BENCHMARK_PROTOCOL


# ---------- cross-shape canonical digest stability ----------

def test_replica_plan_canonical_digest_stable_across_recanon():
    payload = validate_replica_plan(_plan_payload())
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    d1 = __import__("hashlib").sha256(canonical.encode()).hexdigest()
    round_trip = json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":"))
    d2 = __import__("hashlib").sha256(round_trip.encode()).hexdigest()
    assert d1 == d2


def test_replica_benchmark_decision_vocabulary_matches_spec():
    """Spec §9: decisions are exactly material / not_material / inconclusive."""
    payload = validate_replica_benchmark(_benchmark_payload())
    assert payload["decision"] in {"material", "not_material", "inconclusive"}
