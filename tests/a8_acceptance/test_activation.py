# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gates for internet-native activation observations and the
privacy-safe relay projection (spec §6, §10.1 unknown_not_zero and
privacy_safe_relay_projection)."""

from __future__ import annotations

import hashlib
import hmac as hmac_module

import pytest

from mycelium_internet.activation import (
    ACTIVATION_PROTOCOL,
    ActivationError,
    ActivationObservations,
    RelayProjector,
    ConnectionEvidence,
    PathClass,
    build_activation_observation,
)


NOW_MS = 1_752_500_000_000


_MISSING = object()


def _evidence(
    *,
    path_class: str = "direct",
    generation: int = 1,
    reuse: int = 0,
    endpoint_id: str = "51947b11deadbeef51947b11deadbeef",
    metrics: dict | None = _MISSING,  # type: ignore[assignment]
    relay_identity: str | None = None,
    relay_region: str | None = None,
) -> ConnectionEvidence:
    if metrics is _MISSING:
        metrics = {
            "rtt_ms": 12,
            "warm_rtt_ms": 9,
            "jitter_ms": 2,
            "goodput_bytes_per_second": 1_500_000,
            "loss_ratio": 0.0,
            "sample_count": 64,
            "measured_zero": True,
        }
    return ConnectionEvidence(
        connection_generation=generation,
        connection_reuse=reuse,
        path_class=PathClass(path_class),
        endpoint_id=endpoint_id,
        metrics=metrics,
        observed_at_unix_ms=NOW_MS,
        relay_identity=relay_identity,
        relay_region=relay_region,
    )


# ---------------------------------------------------------------------------
# Path class: observation, never inference (unknown-not-zero invariant)
# ---------------------------------------------------------------------------

def test_path_class_is_observation_not_configuration() -> None:
    # Same-LAN / different-network / OS hints are NOT evidence.
    for hint in (
        {"same_lan": True},
        {"different_network": True},
        {"os": "macOS"},
        {"configured_path": "relay"},
    ):
        with pytest.raises(ValueError):
            ConnectionEvidence(
                connection_generation=0,
                connection_reuse=0,
                path_class=PathClass("direct"),
                endpoint_id="x",
                metrics=None,
                observed_at_unix_ms=NOW_MS,
                hints=hint,
            )


def test_missing_evidence_projects_unknown_not_zero(observations_factory=None) -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    document = observations.current_projection()
    assert document["path_class"] == "unknown"
    assert document["metrics"]["rtt_ms"] is None
    assert document["metrics"]["loss_ratio"] is None
    assert document["metrics"]["sample_count"] is None
    assert document["metrics"]["measured_zero"] is False
    assert document["freshness"] == "unknown"


def test_direct_observation_is_recorded_from_bound_connection() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(_evidence(path_class="direct"))
    document = observations.current_projection()
    assert document["path_class"] == "direct"
    assert document["path_source"] == "bound_live_connection"
    assert document["connection_generation"] == 1
    assert document["metrics"]["rtt_ms"] == 12
    assert document["metrics"]["loss_ratio"] == 0.0
    assert document["metrics"]["sample_count"] == 64
    assert document["metrics"]["measured_zero"] is True


def test_relay_observation_is_recorded() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(_evidence(path_class="relay"))
    assert observations.current_projection()["path_class"] == "relay"


def test_unknown_path_is_recorded_without_metrics() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(
        _evidence(path_class="unknown", metrics=None)
    )
    document = observations.current_projection()
    assert document["path_class"] == "unknown"
    assert document["metrics"]["rtt_ms"] is None
    assert document["metrics"]["sample_count"] is None


def test_transition_history_is_retained_and_never_rewritten() -> None:
    clock_state = {"now": NOW_MS / 1000.0}

    def clock() -> float:
        return clock_state["now"]

    observations = ActivationObservations(clock=clock)
    observations.record(_evidence(path_class="direct", generation=1))
    clock_state["now"] += 10.0
    observations.record(_evidence(path_class="relay", generation=2, reuse=0))
    clock_state["now"] += 10.0
    observations.record(_evidence(path_class="direct", generation=3, reuse=1))
    history = observations.history()
    assert [entry["path_class"] for entry in history] == [
        "direct",
        "relay",
        "direct",
    ]
    assert [entry["connection_generation"] for entry in history] == [1, 2, 3]
    assert history[0]["path_class"] == "direct"  # the first record is intact
    assert observations.current_projection()["path_class"] == "direct"


def test_persistent_reuse_is_counted_per_generation() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(_evidence(path_class="direct", generation=1, reuse=0))
    observations.record(_evidence(path_class="direct", generation=1, reuse=3))
    document = observations.current_projection()
    assert document["connection_generation"] == 1
    assert document["connection_reuse"] == 3


def test_stale_evidence_projects_unknown_and_nullable_metrics() -> None:
    clock_state = {"now": NOW_MS / 1000.0}

    def clock() -> float:
        return clock_state["now"]

    observations = ActivationObservations(clock=clock, freshness_seconds=90.0)
    observations.record(_evidence(path_class="direct", generation=1))
    clock_state["now"] += 91.0
    document = observations.current_projection()
    assert document["path_class"] == "unknown"
    assert document["freshness"] == "stale"
    assert document["metrics"]["rtt_ms"] is None
    assert document["metrics"]["loss_ratio"] is None
    assert document["metrics"]["sample_count"] is None


def test_nullable_measurements_are_unknown_not_zero() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(
        _evidence(
            path_class="direct",
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
    document = observations.current_projection()
    for key in ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second"):
        assert document["metrics"][key] is None


def test_explicit_measured_zero_loss_requires_current_samples() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    with pytest.raises(ActivationError) as exc_info:
        observations.record(
            _evidence(
                path_class="direct",
                metrics={
                    "rtt_ms": 5,
                    "warm_rtt_ms": 4,
                    "jitter_ms": 1,
                    "goodput_bytes_per_second": 100,
                    "loss_ratio": 0.0,
                    "sample_count": 0,
                    "measured_zero": True,
                },
            )
        )
    assert exc_info.value.code == "zero_loss_requires_samples"
    # With a current measured sample the explicit zero is valid.
    observations.record(
        _evidence(
            path_class="direct",
            metrics={
                "rtt_ms": 5,
                "warm_rtt_ms": 4,
                "jitter_ms": 1,
                "goodput_bytes_per_second": 100,
                "loss_ratio": 0.0,
                "sample_count": 64,
                "measured_zero": True,
            },
        )
    )
    assert observations.current_projection()["metrics"]["loss_ratio"] == 0.0


def test_rejected_observation_does_not_mutate_state() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(_evidence(path_class="direct"))
    before = observations.current_projection()
    with pytest.raises(ActivationError):
        observations.record(
            _evidence(
                path_class="direct",
                endpoint_id="mismatched-endpoint-id",
            )
        )
    assert observations.current_projection() == before


def test_endpoint_id_exact_match_is_enforced() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(
        _evidence(path_class="direct", endpoint_id="endpoint-a")
    )
    # A different endpoint id is a different activation target: rejected.
    with pytest.raises(ActivationError) as exc_info:
        observations.record(
            _evidence(
                path_class="direct",
                generation=2,
                endpoint_id="endpoint-b",
            )
        )
    assert exc_info.value.code == "endpoint_mismatch"


def test_endpoint_id_is_pseudonymized_in_every_projection() -> None:
    endpoint_id = "51947b11deadbeef51947b11deadbeef"
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(_evidence(path_class="direct", endpoint_id=endpoint_id))
    encoded = str(observations.current_projection())
    assert endpoint_id not in encoded
    assert observations.current_projection()["endpoint_pseudonym"].startswith(
        "sha256:"
    )


def test_build_activation_observation_emits_a_closed_contract_document() -> None:
    from mycelium_internet.contracts import validate_activation_observation

    document = build_activation_observation(
        evidence=_evidence(path_class="relay"),
        endpoint_pseudonym="sha256:" + "a" * 64,
        observation_id="obs-1",
    )
    validate_activation_observation(document)
    assert document["protocol"] == ACTIVATION_PROTOCOL


def test_relay_redaction_removes_raw_identity_before_projection() -> None:
    observations = ActivationObservations(clock=lambda: NOW_MS / 1000.0)
    observations.record(
        _evidence(
            path_class="relay",
            relay_identity="https://relay.example.com:443/path",
            relay_region="europe-west",
        )
    )
    document = observations.current_projection()
    assert "relay.example.com" not in str(document)
    assert "https://" not in str(document)
    assert "443" not in str(document)


# ---------------------------------------------------------------------------
# Privacy-safe relay projection (HMAC, versioned domain separator)
# ---------------------------------------------------------------------------

def test_relay_projection_is_stable_for_the_same_relay() -> None:
    projector = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    first = projector.reference("relay.example.com")
    second = projector.reference("relay.example.com")
    assert first == second
    assert first.startswith("hmac-sha256:")
    assert len(first) == len("hmac-sha256:") + 64


def test_relay_projection_separates_different_relays() -> None:
    projector = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    assert projector.reference("relay-a.example.com") != projector.reference(
        "relay-b.example.com"
    )


def test_relay_projection_domain_version_changes_the_reference() -> None:
    v1 = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    v2 = RelayProjector(projection_key=b"k" * 32, domain_version=2)
    assert v1.reference("relay.example.com") != v2.reference("relay.example.com")


def test_relay_projection_is_a_verifiable_hmac() -> None:
    key = b"k" * 32
    identity = "relay.example.com"
    projector = RelayProjector(projection_key=key, domain_version=1)
    reference = projector.reference(identity)
    expected = hmac_module.new(
        key,
        b"mycelium.relay.projection.v1" + b"||" + identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert reference == f"hmac-sha256:{expected}"


def test_relay_projection_region_is_reviewed_or_unknown() -> None:
    projector = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    assert projector.region("europe-west", reviewed=True) == "europe-west"
    assert projector.region("europe-west", reviewed=False) == "unknown"
    with pytest.raises(ValueError):
        projector.region("x" * 65, reviewed=True)


def test_relay_projection_rejects_non_canonical_identity_inputs() -> None:
    projector = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    for raw in ("", "a" * 300):
        with pytest.raises(ValueError):
            projector.reference(raw)
    # A well-formed identity string HMACs fine - only the HMAC is projected.
    assert projector.reference("not a relay identity").startswith("hmac-sha256:")


def test_relay_projection_key_is_required_and_never_projected() -> None:
    projector = RelayProjector(projection_key=b"k" * 32, domain_version=1)
    reference = projector.reference("relay.example.com")
    assert projector.projection_key not in reference.encode("utf-8")
    with pytest.raises(ValueError):
        RelayProjector(projection_key=b"", domain_version=1)
    with pytest.raises(ValueError):
        RelayProjector(projection_key=b"short", domain_version=1)
