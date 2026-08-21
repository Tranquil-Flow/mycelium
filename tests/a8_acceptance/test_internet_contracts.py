# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gate tests for the four closed A8 internet-native contracts."""

from __future__ import annotations

import json

import pytest

from mycelium_internet.contracts import (
    BOOTSTRAP_STATUS_PROTOCOL,
    ACTIVATION_OBSERVATION_PROTOCOL,
    RELAY_PROJECTION_PROTOCOL,
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    compatibility_fixtures,
    validate_bootstrap_status,
    validate_activation_observation,
    validate_relay_projection,
    validate_internet_native_qualification,
)

VALIDATORS = {
    BOOTSTRAP_STATUS_PROTOCOL: validate_bootstrap_status,
    ACTIVATION_OBSERVATION_PROTOCOL: validate_activation_observation,
    RELAY_PROJECTION_PROTOCOL: validate_relay_projection,
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL: validate_internet_native_qualification,
}

PLACEHOLDER_DIGEST = "sha256:" + "a" * 64


def _fixture(name: str) -> dict:
    return json.loads(json.dumps(compatibility_fixtures()[name]))


# ---------------------------------------------------------------------------
# Closed-shape core gates (shared across all four shapes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(compatibility_fixtures()))
def test_every_a8_fixture_validates(name: str) -> None:
    document = _fixture(name)
    validator = VALIDATORS[document["protocol"]]
    assert validator(document) is not None


@pytest.mark.parametrize("name", list(compatibility_fixtures()))
def test_unknown_field_rejected(name: str) -> None:
    document = _fixture(name)
    validator = VALIDATORS[document["protocol"]]
    with pytest.raises(ValueError):
        validator({**document, "surprise_field": "nope"})


@pytest.mark.parametrize("name", list(compatibility_fixtures()))
def test_wrong_protocol_rejected(name: str) -> None:
    document = _fixture(name)
    validator = VALIDATORS[document["protocol"]]
    with pytest.raises(ValueError):
        validator({**document, "protocol": "mycelium.wrong_shape.v1"})


@pytest.mark.parametrize("name", list(compatibility_fixtures()))
def test_missing_field_rejected(name: str) -> None:
    document = _fixture(name)
    validator = VALIDATORS[document["protocol"]]
    missing = next(iter(document))
    with pytest.raises(ValueError):
        validator({key: value for key, value in document.items() if key != missing})


@pytest.mark.parametrize("name", list(compatibility_fixtures()))
def test_forbidden_privacy_field_rejected(name: str) -> None:
    document = _fixture(name)
    validator = VALIDATORS[document["protocol"]]
    for forbidden in (
        "hostname",
        "host",
        "address",
        "ip_address",
        "url",
        "relay_url",
        "dns_name",
        "endpoint_id",
        "private_path",
        "credential",
        "secret",
        "password",
        "token",
        "bearer",
        "authorization",
        "cookie",
        "prompt",
        "output",
        "logit",
        "tensor",
        "activation",
        "kv_content",
    ):
        with pytest.raises(ValueError):
            validator({**document, forbidden: "raw value"})


def test_fixture_names_and_protocols_are_exactly_the_four_a8_shapes() -> None:
    fixtures = compatibility_fixtures()
    assert set(fixtures) == {
        "internet-bootstrap-status-v1.json",
        "internet-activation-observation-v1.json",
        "relay-projection-v1.json",
        "internet-native-qualification-v1.json",
    }
    assert {value["protocol"] for value in fixtures.values()} == {
        BOOTSTRAP_STATUS_PROTOCOL,
        ACTIVATION_OBSERVATION_PROTOCOL,
        RELAY_PROJECTION_PROTOCOL,
        INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    }


def test_fixtures_are_privacy_clean_and_claim_nothing_executed() -> None:
    raw = json.dumps(compatibility_fixtures(), sort_keys=True)
    assert "https://" not in raw
    assert "http://" not in raw
    assert "iroh://" not in raw
    assert "100." not in raw
    for name, document in compatibility_fixtures().items():
        assert name.endswith(".json")
        encoded = json.dumps(document)
        assert "password" not in encoded.lower()
        assert "secret" not in encoded.lower()
        if document["protocol"] == INTERNET_NATIVE_QUALIFICATION_PROTOCOL:
            assert document["executed"] is False
            assert document["result"] == "not_executed"
            assert document["evidence_digests"] == []
            assert document["projection_digest"] is None


# ---------------------------------------------------------------------------
# internet_bootstrap_status.v1 semantics
# ---------------------------------------------------------------------------

def _bootstrap(**overrides: object) -> dict:
    return {
        **{
            "protocol": BOOTSTRAP_STATUS_PROTOCOL,
            "generation": 1,
            "observed_at_unix_ms": 1_752_500_000_000,
            "freshness": "current",
            "tls_state": "publicly_trusted",
            "canonical_origin_verified": True,
            "seed_pin_state": "verified",
            "route_state": "available",
            "invitation_state": "pending",
            "counters": {"requests": 3, "joins_accepted": 1, "joins_rejected": 2},
        },
        **overrides,
    }


def test_bootstrap_status_accepts_verified_state() -> None:
    validate_bootstrap_status(_bootstrap())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("freshness", "fresh"),
        ("tls_state", "verified"),
        ("seed_pin_state", "ok"),
        ("route_state", "up"),
        ("invitation_state", "joined"),
    ],
)
def test_bootstrap_status_vocabulary_is_closed(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        validate_bootstrap_status(_bootstrap(**{field: value}))


def test_bootstrap_status_unknown_vocabulary_is_supported() -> None:
    validate_bootstrap_status(
        _bootstrap(
            freshness="unknown",
            tls_state="unknown",
            seed_pin_state="unknown",
            route_state="unknown",
            invitation_state="unknown",
        )
    )


def test_bootstrap_status_rejects_negative_counters() -> None:
    with pytest.raises(ValueError):
        validate_bootstrap_status(
            _bootstrap(counters={"requests": -1, "joins_accepted": 0, "joins_rejected": 0})
        )


def test_bootstrap_status_counters_are_a_closed_object() -> None:
    with pytest.raises(ValueError):
        validate_bootstrap_status(
            _bootstrap(
                counters={"requests": 1, "joins_accepted": 0, "joins_rejected": 0, "extra": 1}
            )
        )


def test_bootstrap_status_rejects_non_finite_and_bool_numbers() -> None:
    with pytest.raises(ValueError):
        validate_bootstrap_status(_bootstrap(generation=True))
    with pytest.raises(ValueError):
        validate_bootstrap_status(_bootstrap(observed_at_unix_ms=float("inf")))
    with pytest.raises(ValueError):
        validate_bootstrap_status(_bootstrap(canonical_origin_verified="yes"))


def test_bootstrap_status_discloses_no_hostname_or_address() -> None:
    # Closed shape: any attempt to smuggle network identity is an unknown field.
    for extra in (
        {"hostname": "seed.example.com"},
        {"address": "1.2.3.4"},
        {"endpoint_id": "51947b11deadbeef"},
    ):
        with pytest.raises(ValueError):
            validate_bootstrap_status({**_bootstrap(), **extra})


# ---------------------------------------------------------------------------
# internet_activation_observation.v1 semantics
# ---------------------------------------------------------------------------

def _observation(**overrides: object) -> dict:
    return {
        **{
            "protocol": ACTIVATION_OBSERVATION_PROTOCOL,
            "observation_id": "obs-1",
            "connection_generation": 3,
            "connection_reuse": 2,
            "path_class": "direct",
            "path_source": "bound_live_connection",
            "endpoint_pseudonym": "sha256:" + "b" * 64,
            "observed_at_unix_ms": 1_752_500_000_000,
            "freshness": "current",
            "evidence_lifetime_until_unix_ms": 1_752_500_090_000,
            "metrics": {
                "rtt_ms": 12,
                "warm_rtt_ms": 9,
                "jitter_ms": 2,
                "goodput_bytes_per_second": 1_500_000,
                "loss_ratio": 0.0,
                "sample_count": 64,
                "measured_zero": True,
            },
        },
        **overrides,
    }


def test_observation_accepts_direct_bound_current() -> None:
    validate_activation_observation(_observation())


def test_observation_accepts_relay_and_unknown_paths() -> None:
    validate_activation_observation(_observation(path_class="relay"))
    validate_activation_observation(
        _observation(
            path_class="unknown",
            path_source="unknown",
            metrics={
                "rtt_ms": None,
                "warm_rtt_ms": None,
                "jitter_ms": None,
                "goodput_bytes_per_second": None,
                "loss_ratio": None,
                "sample_count": None,
                "measured_zero": False,
            },
        )
    )


def test_observation_nullable_metrics_are_unknown_not_zero() -> None:
    validate_activation_observation(
        _observation(
            metrics={
                "rtt_ms": None,
                "warm_rtt_ms": None,
                "jitter_ms": None,
                "goodput_bytes_per_second": None,
                "loss_ratio": None,
                "sample_count": None,
                "measured_zero": False,
            }
        )
    )


def test_observation_direct_requires_bound_live_connection_source() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(_observation(path_source="unknown"))
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(path_class="relay", path_source="configured")
        )


def test_observation_stale_or_unknown_freshness_forbids_path_claim_and_metrics() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(_observation(freshness="stale"))
    validate_activation_observation(
        _observation(
            freshness="stale",
            path_class="unknown",
            path_source="unknown",
            metrics={
                "rtt_ms": None,
                "warm_rtt_ms": None,
                "jitter_ms": None,
                "goodput_bytes_per_second": None,
                "loss_ratio": None,
                "sample_count": None,
                "measured_zero": False,
            },
        )
    )


def test_observation_zero_loss_requires_current_measured_samples() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": 5, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": 0.0,
                    "sample_count": 0,
                    "measured_zero": False,
                }
            )
        )
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": 5, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": 0.0,
                    "sample_count": 64,
                    "measured_zero": False,
                }
            )
        )


def test_observation_nonzero_loss_forbids_measured_zero_flag() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": 5, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": 0.02,
                    "sample_count": 64,
                    "measured_zero": True,
                }
            )
        )


def test_observation_metrics_require_sample_count() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    "rtt_ms": 5,
                    "warm_rtt_ms": 4,
                    "jitter_ms": 1,
                    "goodput_bytes_per_second": 100,
                    "loss_ratio": None,
                    "sample_count": None,
                    "measured_zero": False,
                }
            )
        )


def test_observation_rejects_negative_or_non_finite_metrics() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": -1, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": None,
                    "sample_count": 64,
                    "measured_zero": False,
                }
            )
        )
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": 5, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": float("nan"),
                    "sample_count": 64,
                    "measured_zero": False,
                }
            )
        )


def test_observation_rejects_loss_ratio_out_of_range() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                metrics={
                    **{"rtt_ms": 5, "warm_rtt_ms": 4, "jitter_ms": 1,
                       "goodput_bytes_per_second": 100},
                    "loss_ratio": 1.5,
                    "sample_count": 64,
                    "measured_zero": False,
                }
            )
        )


def test_observation_rejects_raw_endpoint_identity() -> None:
    # EndpointID is pseudonymized into a digest; raw ids cannot fit the shape.
    with pytest.raises(ValueError):
        validate_activation_observation(_observation(endpoint_pseudonym="51947b11beef"))
    with pytest.raises(ValueError):
        validate_activation_observation(_observation(endpoint_pseudonym="sha256:" + "x" * 64))


def test_observation_endpoint_pseudonym_is_exact_digest_format() -> None:
    validate_activation_observation(
        _observation(endpoint_pseudonym="sha256:" + "c" * 64)
    )


def test_observation_unknown_path_class_rejects_metrics_from_stale_source() -> None:
    with pytest.raises(ValueError):
        validate_activation_observation(
            _observation(
                path_class="unknown",
                path_source="unknown",
                metrics={
                    "rtt_ms": 5,
                    "warm_rtt_ms": None,
                    "jitter_ms": None,
                    "goodput_bytes_per_second": None,
                    "loss_ratio": None,
                    "sample_count": 64,
                    "measured_zero": False,
                },
            )
        )


# ---------------------------------------------------------------------------
# relay_projection.v1 semantics
# ---------------------------------------------------------------------------

def _projection(**overrides: object) -> dict:
    return {
        **{
            "protocol": RELAY_PROJECTION_PROTOCOL,
            "relay_reference": "hmac-sha256:" + "d" * 64,
            "region": "unknown",
            "projection_generation": 1,
            "stable": True,
            "observed_at_unix_ms": 1_752_500_000_000,
        },
        **overrides,
    }


def test_relay_projection_accepts_reference_and_unknown_region() -> None:
    validate_relay_projection(_projection())


def test_relay_projection_accepts_reviewed_coarse_region() -> None:
    validate_relay_projection(_projection(region="europe-west"))


def test_relay_projection_rejects_raw_relay_identity() -> None:
    for raw in (
        "https://relay.example.com:443",
        "relay.example.com",
        "1.2.3.4",
        "1.2.3.4:443",
        "https://example.com/relay?token=abc",
    ):
        with pytest.raises(ValueError):
            validate_relay_projection(_projection(relay_reference=raw))


def test_relay_projection_region_is_bounded() -> None:
    with pytest.raises(ValueError):
        validate_relay_projection(_projection(region="x" * 65))
    with pytest.raises(ValueError):
        validate_relay_projection(_projection(region=""))


def test_relay_projection_generation_is_monotonic_positive() -> None:
    with pytest.raises(ValueError):
        validate_relay_projection(_projection(projection_generation=0))


# ---------------------------------------------------------------------------
# internet_native_qualification.v1 semantics
# ---------------------------------------------------------------------------

def _qualification(**overrides: object) -> dict:
    return {
        **{
            "protocol": INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
            "qualification_id": "a8-qual-1",
            "gate_kind": "physical_positive",
            "case_ids": ["unrelated_https_invite_without_tailscale"],
            "observed_at_unix_ms": 1_752_500_000_000,
            "executed": True,
            "result": "passed",
            "spec_digest": PLACEHOLDER_DIGEST,
            "source_digest": PLACEHOLDER_DIGEST,
            "evidence_digests": [PLACEHOLDER_DIGEST],
            "fresh_until_unix_ms": 1_752_509_000_000,
            "projection_digest": PLACEHOLDER_DIGEST,
            "public_projection": {
                "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                "outcomes": ["https_bootstrap_succeeds"],
                "relay_reference": None,
                "observed_at_unix_ms": 1_752_500_000_000,
            },
        },
        **overrides,
    }


def test_qualification_accepts_executed_passed_shape() -> None:
    validate_internet_native_qualification(_qualification())


def test_qualification_not_executed_claims_nothing() -> None:
    validate_internet_native_qualification(
        _qualification(
            executed=False,
            result="not_executed",
            evidence_digests=[],
            fresh_until_unix_ms=None,
            projection_digest=None,
            public_projection={
                "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                "outcomes": ["not_executed"],
                "relay_reference": None,
                "observed_at_unix_ms": 1_752_500_000_000,
            },
        )
    )


def test_qualification_not_executed_must_claim_not_executed() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(_qualification(executed=False))
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(executed=False, result="not_executed")
        )


def test_qualification_result_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(_qualification(result="partial"))


def test_qualification_projection_cases_are_subset_of_executed_cases() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(
                public_projection={
                    "gate_case_ids": ["not_in_cases"],
                    "outcomes": ["https_bootstrap_succeeds"],
                    "relay_reference": None,
                    "observed_at_unix_ms": 1_752_500_000_000,
                }
            )
        )


def test_qualification_case_ids_are_bounded_unique_names() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(_qualification(case_ids=[]))
    with pytest.raises(ValueError):
        validate_internet_native_qualification(_qualification(case_ids=["a", "a"]))
    with pytest.raises(ValueError):
        validate_internet_native_qualification(_qualification(case_ids=["Bad Name!"]))


def test_qualification_outcomes_are_sorted_unique_bounded_names() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(
                public_projection={
                    "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                    "outcomes": ["b", "a"],
                    "relay_reference": None,
                    "observed_at_unix_ms": 1_752_500_000_000,
                }
            )
        )


def test_qualification_projection_relay_reference_is_privacy_safe() -> None:
    validate_internet_native_qualification(
        _qualification(
            public_projection={
                "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                "outcomes": ["https_bootstrap_succeeds"],
                "relay_reference": "hmac-sha256:" + "e" * 64,
                "observed_at_unix_ms": 1_752_500_000_000,
            }
        )
    )
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(
                public_projection={
                    "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                    "outcomes": ["https_bootstrap_succeeds"],
                    "relay_reference": "https://relay.example.com",
                    "observed_at_unix_ms": 1_752_500_000_000,
                }
            )
        )


def test_qualification_evidence_digests_are_bounded_unique() -> None:
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(evidence_digests=[PLACEHOLDER_DIGEST, PLACEHOLDER_DIGEST])
        )
    with pytest.raises(ValueError):
        validate_internet_native_qualification(
            _qualification(evidence_digests=["sha256:" + "f" * 63])
        )
