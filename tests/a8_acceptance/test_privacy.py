# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic privacy-scan gates (spec §11): no secret, raw EndpointID,
address, hostname, relay URL, private path, prompt, output, token, tensor,
activation, or KV content crosses any projection."""

from __future__ import annotations

import json

import pytest

from mycelium_internet.activation import (
    ActivationObservations,
    ConnectionEvidence,
    PathClass,
)
from mycelium_internet.contracts import (
    compatibility_fixtures,
    validate_relay_projection,
)
from mycelium_internet.privacy import (
    PrivacyViolation,
    ensure_privacy_clean,
    scan_projection,
)

RAW_TOKEN = "invite-secret-token-0a1b2c3d"
RAW_ENDPOINT_ID = "51947b11deadbeef51947b11deadbeef"
RAW_HOSTNAME = "seed.example.com"
RAW_RELAY_URL = "https://relay.example.com:443/path"
PRIVATE_PATH = "/Users/operator/.ssh/id_ed25519"


def _evidence(path_class: str = "relay") -> ConnectionEvidence:
    return ConnectionEvidence(
        connection_generation=1,
        connection_reuse=0,
        path_class=PathClass(path_class),
        endpoint_id=RAW_ENDPOINT_ID,
        metrics={
            "rtt_ms": 12,
            "warm_rtt_ms": 9,
            "jitter_ms": 2,
            "goodput_bytes_per_second": 1_500_000,
            "loss_ratio": 0.0,
            "sample_count": 64,
            "measured_zero": True,
        },
        observed_at_unix_ms=1_752_500_000_000,
        relay_identity=RAW_RELAY_URL,
    )


def _relay_document(**overrides: object) -> dict:
    document = {
        "protocol": "mycelium.relay_projection.v1",
        "relay_reference": "hmac-sha256:" + "a" * 64,
        "region": "unknown",
        "projection_generation": 1,
        "stable": True,
        "observed_at_unix_ms": 1_752_500_000_000,
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# Every emitted projection is clean
# ---------------------------------------------------------------------------

def test_every_contract_fixture_is_privacy_clean() -> None:
    for name, document in compatibility_fixtures().items():
        assert scan_projection(document) == [], name


def test_activation_projection_is_privacy_clean() -> None:
    observations = ActivationObservations(clock=lambda: 1_752_500_000.0)
    observations.record(_evidence())
    document = observations.current_projection()
    violations = scan_projection(
        document,
        forbidden_needles=[RAW_RELAY_URL, RAW_HOSTNAME, RAW_ENDPOINT_ID],
    )
    assert violations == []
    ensure_privacy_clean(document)


def test_relay_projection_document_is_privacy_clean() -> None:
    document = _relay_document(region="europe-west")
    validate_relay_projection(document)
    ensure_privacy_clean(document, forbidden_needles=[RAW_RELAY_URL, RAW_HOSTNAME])


def test_activation_history_is_privacy_clean_after_transitions() -> None:
    observations = ActivationObservations(clock=lambda: 1_752_500_000.0)
    observations.record(_evidence(path_class="direct"))
    observations.record(_evidence(path_class="relay"))
    observations.record(_evidence(path_class="direct"))
    for document in observations.history():
        violations = scan_projection(
            document,
            forbidden_needles=[RAW_ENDPOINT_ID, RAW_HOSTNAME, RAW_RELAY_URL],
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Forbidden material is flagged wherever it appears
# ---------------------------------------------------------------------------

def test_raw_url_and_hostname_values_are_flagged() -> None:
    violations = scan_projection(
        _relay_document(relay_reference=RAW_RELAY_URL)
    )
    assert any("relay_reference" in item for item in violations)
    # Hostnames are caller-known needles: exact-match scan flags them.
    violations = scan_projection(
        {"region": RAW_HOSTNAME},
        forbidden_needles=[RAW_HOSTNAME],
    )
    assert any("region" in item for item in violations)


def test_ip_address_and_tailscale_references_are_flagged() -> None:
    for value in (
        "1.2.3.4",
        "100.64.1.2",
        "host123.ts.net",
        "https://100.126.111.123/seed/identity",
    ):
        assert scan_projection({"field": value}) != [], value


def test_private_paths_are_flagged() -> None:
    for value in (
        PRIVATE_PATH,
        "/home/astra/data",
        "/private/var/seed",
        "/var/lib/mycelium/seed",
        "/etc/ssl/private",
        "~/.ssh/known_hosts",
    ):
        assert scan_projection({"field": value}) != []


def test_bearer_tokens_and_credentials_are_flagged() -> None:
    for value in (
        "Bearer abcdefghijklmnop",
        "sk-1234567890abcdef",
        "token=deadbeef",
        "https://seed.example.com/join?token=abc",
    ):
        assert scan_projection({"field": value}) != []


def test_forbidden_keys_are_flagged() -> None:
    for key in (
        "prompt",
        "output",
        "token",
        "tensor",
        "activation",
        "kv_content",
        "credential",
        "secret",
        "password",
        "endpoint_id",
        "hostname",
        "relay_url",
        "private_path",
    ):
        violations = scan_projection({key: "anything"})
        assert any(key in item for item in violations), key


def test_forbidden_needles_are_flagged_wherever_they_appear() -> None:
    document = {
        "protocol": "mycelium.internet_bootstrap_status.v1",
        "generation": 1,
        "observed_at_unix_ms": 1_752_500_000_000,
        "freshness": "current",
        "tls_state": "publicly_trusted",
        "canonical_origin_verified": True,
        "seed_pin_state": "verified",
        "route_state": "available",
        "invitation_state": "pending",
        "counters": {"requests": 0, "joins_accepted": 0, "joins_rejected": 0},
    }
    for needle in (RAW_TOKEN, RAW_ENDPOINT_ID, RAW_HOSTNAME):
        tampered = {
            **document,
            "observed_at_unix_ms": 1_752_500_000_000,
            "extra": needle,
        }
        assert scan_projection(tampered, forbidden_needles=[needle]) != []
    assert scan_projection(
        document,
        forbidden_needles=[RAW_TOKEN, RAW_ENDPOINT_ID, RAW_HOSTNAME],
    ) == []


def test_privacy_needle_matches_inside_nested_values() -> None:
    document = {
        "metrics": {
            "note": f"observed via {RAW_RELAY_URL}",
        }
    }
    violations = scan_projection(document)
    assert any("metrics.note" in item for item in violations)


# ---------------------------------------------------------------------------
# Fail-closed enforcement
# ---------------------------------------------------------------------------

def test_ensure_privacy_clean_raises_bounded_violation() -> None:
    with pytest.raises(PrivacyViolation) as exc_info:
        ensure_privacy_clean({"output": "raw model output"})
    assert exc_info.value.code == "projection_privacy_violation"
    assert isinstance(exc_info.value.violations, list)
    assert exc_info.value.violations


def test_ensure_privacy_clean_passes_clean_documents() -> None:
    ensure_privacy_clean(_relay_document())
    ensure_privacy_clean(
        {
            "protocol": "mycelium.internet_activation_observation.v1",
            "observation_id": "obs-1",
            "connection_generation": 1,
            "connection_reuse": 0,
            "path_class": "unknown",
            "path_source": "unknown",
            "endpoint_pseudonym": None,
            "observed_at_unix_ms": 1_752_500_000_000,
            "freshness": "unknown",
            "evidence_lifetime_until_unix_ms": 1_752_500_090_000,
            "metrics": {
                "rtt_ms": None,
                "warm_rtt_ms": None,
                "jitter_ms": None,
                "goodput_bytes_per_second": None,
                "loss_ratio": None,
                "sample_count": None,
                "measured_zero": False,
            },
        }
    )


def test_privacy_violation_serializes_without_the_secret() -> None:
    try:
        ensure_privacy_clean(
            {"secret": "hunter2-private-value"},
            forbidden_needles=["hunter2-private-value"],
        )
    except PrivacyViolation as exc:
        encoded = json.dumps(
            {"code": exc.code, "violations": exc.violations}
        )
        assert "hunter2-private-value" not in encoded
    else:
        pytest.fail("expected PrivacyViolation")
