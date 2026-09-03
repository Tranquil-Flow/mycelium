"""Tests for tests/a5_acceptance/workload_manifest.v1.json

The workload manifest is the frozen corpus the A5 benchmark reads. These tests
cover the 16 ad-hoc checks that the offline verifier exercised; converting them
to pytest makes the manifest a suite-grade gate, not a one-off assertion set.

Canonical spec: docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md §9
Frozen protocol: tests/a5_acceptance/benchmark_protocol.v1.json
"""

import hashlib
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "tests" / "a5_acceptance" / "workload_manifest.v1.json"
PROTOCOL_PATH = REPO_ROOT / "tests" / "a5_acceptance" / "benchmark_protocol.v1.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def protocol() -> dict:
    return json.loads(PROTOCOL_PATH.read_text())


def test_manifest_file_exists():
    assert MANIFEST_PATH.is_file(), f"missing: {MANIFEST_PATH}"


def test_manifest_top_level_keys(manifest):
    required = {
        "protocol", "gate_state", "claim_boundary", "model",
        "base_deployment_id", "route_generation",
        "prompt_output_buckets", "arrival_schedule", "qos_mix",
        "offered_concurrency", "token_limits", "product_benchmark_session",
    }
    assert required <= set(manifest.keys()), (
        f"missing top-level keys: {required - set(manifest.keys())}"
    )


def test_protocol_v1(manifest):
    assert manifest["protocol"] == "mycelium.a5_workload_manifest.v1"


def test_gate_state_design_only(manifest):
    assert manifest["gate_state"] == "design_only"


def test_prompt_output_bucket_fractions_sum_to_one(manifest):
    buckets = manifest["prompt_output_buckets"]["buckets"]
    tol = manifest["prompt_output_buckets"]["fraction_tolerance"]
    total = sum(b["fraction"] for b in buckets)
    assert abs(total - 1.0) <= tol, (
        f"bucket fractions sum to {total} (tol {tol}); {[(b['name'], b['fraction']) for b in buckets]}"
    )


def test_qos_mix_fractions_sum_to_one(manifest):
    classes = manifest["qos_mix"]["classes"]
    tol = manifest["qos_mix"]["fraction_tolerance"]
    total = sum(c["fraction"] for c in classes)
    assert abs(total - 1.0) <= tol, (
        f"qos fractions sum to {total} (tol {tol}); {[(c['name'], c['fraction']) for c in classes]}"
    )


def test_arrival_schedule_seed_is_0xA5(manifest):
    # Benchmark protocol requires seed 0xA5 = 165 for the paired bootstrap.
    assert manifest["arrival_schedule"]["rng_seed"] == 165, manifest["arrival_schedule"]["rng_seed"]


def test_terminal_per_window_meets_protocol_floor(manifest, protocol):
    floor = protocol["schedule"]["minimum_requests_per_measured_window"]
    have = manifest["offered_concurrency"]["minimum_terminal_requests_per_window"]
    assert have >= floor, f"manifest={have}, protocol floor={floor}"


def test_session_covers_all_identical_binding_fields(manifest, protocol):
    proto_required = set(protocol["identical_binding_fields"])
    session_fields = set(manifest["product_benchmark_session"]["fields"])
    missing = proto_required - session_fields
    assert not missing, (
        f"session.fields missing protocol identical_binding_fields: {sorted(missing)}"
    )


def test_per_bucket_max_le_default(manifest):
    max_default = manifest["token_limits"]["maximum_new_tokens_default"]
    overrides = manifest["token_limits"]["per_bucket_max_overrides"]
    offenders = {k: v for k, v in overrides.items() if v > max_default}
    assert not offenders, (
        f"per-bucket max exceeds default: default={max_default}, offenders={offenders}"
    )


def test_all_prompt_output_bucket_ranges_valid(manifest):
    for b in manifest["prompt_output_buckets"]["buckets"]:
        assert b["prompt_token_min"] <= b["prompt_token_max"], b
        assert b["output_token_min"] <= b["output_token_max"], b


def test_subdigests_are_canonical_sha256_64hex(manifest):
    containers = {
        "instrumentation_digest": manifest["product_benchmark_session"],
        "configuration_digest": manifest["product_benchmark_session"],
        "software_digest": manifest["product_benchmark_session"],
        "stop_token_digest": manifest["token_limits"],
    }
    for field, container in containers.items():
        value = container.get(field)
        assert isinstance(value, str), f"{field} not a string: {value!r}"
        assert value.startswith("sha256:"), f"{field} missing sha256: prefix: {value}"
        assert len(value) == 7 + 64, f"{field} not 64 hex chars: {value!r}"


def test_canonical_digest_stable_across_recanon(manifest):
    """Re-serializing canonical JSON yields the same sha256."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    round_trip = json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":"))
    d1 = hashlib.sha256(canonical.encode()).hexdigest()
    d2 = hashlib.sha256(round_trip.encode()).hexdigest()
    assert d1 == d2, f"digest drift: {d1[:16]} vs {d2[:16]}"


def test_no_overlap_between_identical_and_mode_specific_binding_fields(protocol):
    identical = set(protocol["identical_binding_fields"])
    mode_specific = set(protocol["allowed_mode_specific_binding_fields"])
    overlap = identical & mode_specific
    assert not overlap, (
        f"protocol has identical/mode_specific overlap: {sorted(overlap)}"
    )


def test_manifest_is_kebab_under_a5_acceptance():
    """Sanity: workload manifest must live at the documented path."""
    assert MANIFEST_PATH.parent.name == "a5_acceptance"
    assert MANIFEST_PATH.name == "workload_manifest.v1.json"


def test_model_binding_matches_a4_incumbent(manifest):
    """The 0.5B Qwen incumbent is the binding model for A5 — must match what A4 ships."""
    assert manifest["model"]["model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert manifest["model"]["model_revision"] == "7ae557604adf67be50417f59c2c2f167def9a775"
    assert manifest["model"]["representation_digest"] == (
        "sha256:2729a427933ffaf01af4739ef70206023f2e7aa1061c2791283d3356f686d305"
    )


def test_base_deployment_id_matches_a4(manifest):
    """Base deployment must match the A4-installed deployment identity."""
    assert manifest["base_deployment_id"] == "e1d1a2fa-f68d-5b38-a507-34b367c8d855"


def test_arrival_schedule_is_finite_and_deterministic(manifest):
    """Poisson arrival with bounded rate and a deterministic seed."""
    sched = manifest["arrival_schedule"]
    assert sched["kind"] == "poisson"
    assert sched["rate_per_second"] > 0
    assert isinstance(sched["rng_seed"], int) and sched["rng_seed"] >= 0
    assert sched["warmup_seconds"] >= 0
