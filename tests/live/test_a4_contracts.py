from __future__ import annotations

import copy

import pytest

from mycelium_live.a4_contracts import (
    compatibility_fixtures,
    validate_interruptible_stage_command,
    validate_product_qualification,
    validate_scoped_runtime_incident,
    validate_traffic_liveness,
)


def _fixture(name: str) -> dict:
    return copy.deepcopy(compatibility_fixtures()[name])


@pytest.mark.parametrize(
    ("name", "validator"),
    (
        ("interruptible-stage-command-v1.json", validate_interruptible_stage_command),
        ("traffic-liveness-v1.json", validate_traffic_liveness),
        ("scoped-runtime-incident-v1.json", validate_scoped_runtime_incident),
        (
            "product-concurrency-liveness-qualification-v1.json",
            validate_product_qualification,
        ),
    ),
)
def test_a4_contracts_reject_unknown_fields(name, validator) -> None:
    document = _fixture(name)
    document["unexpected"] = True
    with pytest.raises(ValueError):
        validator(document)


def test_interruptible_command_rejects_unbounded_or_stale_identity() -> None:
    document = _fixture("interruptible-stage-command-v1.json")
    document["command_id"] = "x" * 257
    with pytest.raises(ValueError, match="interruptible"):
        validate_interruptible_stage_command(document)

    document = _fixture("interruptible-stage-command-v1.json")
    document["issued_at_ms"] = document["absolute_deadline_ms"]
    with pytest.raises(ValueError, match="interruptible"):
        validate_interruptible_stage_command(document)


def test_traffic_liveness_rejects_duplicate_and_future_observations() -> None:
    document = _fixture("traffic-liveness-v1.json")
    subject = {
        "subject_id": "edge-a-b",
        "kind": "edge",
        "membership_generation": 1,
        "state": "fresh",
        "last_fresh_ms": 9_000,
        "last_observed_ms": 9_500,
        "next_keepalive_due_ms": 14_000,
        "consecutive_misses": 0,
        "last_source": "application_receipt",
    }
    document["subjects"] = [subject, dict(subject)]
    with pytest.raises(ValueError, match="traffic_liveness"):
        validate_traffic_liveness(document)

    document["subjects"] = [{**subject, "last_observed_ms": 10_001}]
    with pytest.raises(ValueError, match="traffic_liveness"):
        validate_traffic_liveness(document)


def test_scoped_incident_requires_full_canonical_execution_identity() -> None:
    document = _fixture("scoped-runtime-incident-v1.json")
    for field in (
        "deployment_epoch",
        "qualification_digest",
        "request_attempt",
        "path_id",
        "path_attempt",
        "path_digest",
        "topology_generation",
        "command_id",
        "cancellation_generation",
        "publisher_generation",
        "cleanup_owner_id",
    ):
        candidate = dict(document)
        candidate.pop(field)
        with pytest.raises(ValueError, match="scoped_runtime_incident"):
            validate_scoped_runtime_incident(candidate)


def test_product_qualification_cannot_claim_eligibility_with_unproven_gate() -> None:
    document = _fixture("product-concurrency-liveness-qualification-v1.json")
    document["cooperative_interruption_proven"] = False
    with pytest.raises(ValueError, match="product_concurrency"):
        validate_product_qualification(document)
