# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from .recovery_harness import (
    A6_PRODUCT_INTERFACE,
    A6RecoveryHarness,
    FailureObservation,
    RecoveryRejected,
)


MANIFEST = Path(__file__).with_name("harness.v1.json")
EXPECTED_COVERAGE = {
    "committed_delivery_authority",
    "full_context_replay",
    "exact_request_attempt_fencing",
    "duplicate_output_prevention",
    "failure_scope",
    "cleanup",
}
EXPECTED_INTERFACES = {
    "a6.gateway_private_replay_checkpoint_and_delivery_cas.v1",
    "a6.observed_failure_input.v1",
    "a6.current_successor_qualification.v1",
    "a6.request_recovery_cutover_cas.v1",
    A6_PRODUCT_INTERFACE,
    "a6.terminal_and_owner_cleanup_cas.v1",
}


def _harness(request_id: str = "request-a") -> A6RecoveryHarness:
    return A6RecoveryHarness(
        session_id="session-1",
        request_id=request_id,
        encoded_prompt=(101, 42, 7),
        generation_settings={"decode": "greedy", "max_output": 8},
    )


def _fail(harness: A6RecoveryHarness) -> None:
    source = harness.authority
    harness.observe_failure(
        FailureObservation(
            authority="a4_liveness_authority",
            request_id=harness.request_id,
            attempt=source.attempt,
            path_generation=source.path_generation,
            scope="request",
        )
    )


def _recover(harness: A6RecoveryHarness):
    checkpoint = harness.freeze_checkpoint(caller="request_gateway")
    return harness.recover(
        checkpoint=checkpoint,
        encoded_prompt=checkpoint.encoded_prompt,
        committed_prefix=checkpoint.committed_prefix,
        successor_attempt=checkpoint.source_attempt + 1,
        successor_path_generation=checkpoint.source_path_generation + 1,
        successor_path_id="successor-path",
    )


def test_harness_manifest_is_closed_design_only_and_lists_post_a4_interfaces() -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))

    assert set(manifest) == {
        "protocol",
        "gate_state",
        "evidence_kind",
        "claim_boundary",
        "depends_on",
        "coverage",
        "production_interfaces_required_after_a4",
        "physical_recovery_evidence",
        "qualification_claim",
        "promotion_authorized",
    }
    assert manifest["gate_state"] == "design_only"
    assert manifest["evidence_kind"] == "deterministic_fixture"
    assert "never" in manifest["claim_boundary"]
    assert manifest["depends_on"] == ["A4"]
    assert set(manifest["coverage"]) == EXPECTED_COVERAGE
    interfaces = manifest["production_interfaces_required_after_a4"]
    assert {item["interface_id"] for item in interfaces} == EXPECTED_INTERFACES
    assert len(interfaces) == len(EXPECTED_INTERFACES)
    assert all(
        set(item) == {"interface_id", "owner", "capability"} for item in interfaces
    )
    assert manifest["physical_recovery_evidence"] is False
    assert manifest["qualification_claim"] is False
    assert manifest["promotion_authorized"] is False


def test_gateway_is_committed_delivery_and_checkpoint_authority() -> None:
    harness = _harness()
    source = harness.authority

    with pytest.raises(RecoveryRejected, match="delivery_authority_not_gateway"):
        harness.publish(position=1, token=301, writer=source, caller="router")
    assert (
        harness.publish(position=1, token=301, writer=source, caller="request_gateway")
        is True
    )
    assert (
        harness.publish(position=2, token=302, writer=source, caller="request_gateway")
        is True
    )
    with pytest.raises(RecoveryRejected, match="checkpoint_authority_not_gateway"):
        harness.freeze_checkpoint(caller="browser")

    _fail(harness)
    checkpoint = harness.freeze_checkpoint(caller="request_gateway")
    assert checkpoint.committed_prefix == (301, 302)
    assert checkpoint.committed_token_count == checkpoint.delivery_cursor == 2
    with pytest.raises(
        RecoveryRejected, match="checkpoint_not_current_gateway_authority"
    ):
        harness.recover(
            checkpoint=replace(checkpoint, delivery_cursor=1),
            encoded_prompt=checkpoint.encoded_prompt,
            committed_prefix=checkpoint.committed_prefix,
            successor_attempt=8,
            successor_path_generation=12,
            successor_path_id="successor-path",
        )


def test_full_context_replay_suppresses_prefix_and_starts_at_next_position() -> None:
    harness = _harness()
    source = harness.authority
    for position, token in enumerate((301, 302, 303), start=1):
        harness.publish(
            position=position,
            token=token,
            writer=source,
            caller="request_gateway",
        )
    _fail(harness)

    result = _recover(harness)

    assert result.protocol == A6_PRODUCT_INTERFACE
    assert result.reconstructed_context == (101, 42, 7, 301, 302, 303)
    assert result.suppressed_output_positions == (1, 2, 3)
    assert result.first_publishable_position == 4
    assert result.gate_state == "design_only"
    assert result.evidence_kind == "deterministic_fixture"
    assert result.physical_recovery_evidence is False
    assert harness.publish(
        position=4,
        token=304,
        writer=result.successor_authority,
        caller="request_gateway",
    )
    assert [event.token for event in harness.ledger] == [301, 302, 303, 304]


@pytest.mark.parametrize(
    ("attempt_delta", "path_delta", "reason"),
    [
        (0, 1, "successor_attempt_not_exact_increment"),
        (2, 1, "successor_attempt_not_exact_increment"),
        (1, 0, "successor_path_generation_not_exact_increment"),
        (1, 2, "successor_path_generation_not_exact_increment"),
    ],
)
def test_cutover_requires_exact_request_attempt_and_path_increment(
    attempt_delta: int, path_delta: int, reason: str
) -> None:
    harness = _harness()
    harness.publish(
        position=1,
        token=301,
        writer=harness.authority,
        caller="request_gateway",
    )
    _fail(harness)
    checkpoint = harness.freeze_checkpoint(caller="request_gateway")

    with pytest.raises(RecoveryRejected, match=reason):
        harness.recover(
            checkpoint=checkpoint,
            encoded_prompt=checkpoint.encoded_prompt,
            committed_prefix=checkpoint.committed_prefix,
            successor_attempt=checkpoint.source_attempt + attempt_delta,
            successor_path_generation=checkpoint.source_path_generation + path_delta,
            successor_path_id="successor-path",
        )


def test_source_is_fenced_and_duplicate_or_conflicting_output_never_appends() -> None:
    harness = _harness()
    source = harness.authority
    harness.publish(position=1, token=301, writer=source, caller="request_gateway")
    assert (
        harness.publish(position=1, token=301, writer=source, caller="request_gateway")
        is False
    )
    with pytest.raises(RecoveryRejected, match="conflicting_delivery_identity"):
        harness.publish(position=1, token=999, writer=source, caller="request_gateway")
    with pytest.raises(RecoveryRejected, match="noncontiguous_delivery_position"):
        harness.publish(position=3, token=303, writer=source, caller="request_gateway")
    _fail(harness)

    successor = _recover(harness).successor_authority
    ledger_before = harness.ledger
    with pytest.raises(RecoveryRejected, match="stale_request_attempt"):
        harness.publish(position=2, token=302, writer=source, caller="request_gateway")
    with pytest.raises(RecoveryRejected, match="stale_request_attempt"):
        harness.publish_terminal(outcome="completed", writer=source)
    with pytest.raises(RecoveryRejected, match="stale_request_attempt"):
        harness.cleanup(writer=source)
    assert harness.ledger == ledger_before
    assert harness.publish(
        position=2, token=302, writer=successor, caller="request_gateway"
    )


def test_a4_failure_authority_and_scope_cannot_mutate_unrelated_request() -> None:
    affected = _harness("request-a")
    unrelated = _harness("request-b")
    unrelated_before = (
        unrelated.authority,
        unrelated.ledger,
        unrelated.live_resources.copy(),
    )

    with pytest.raises(RecoveryRejected, match="failure_authority_not_a4"):
        affected.observe_failure(
            FailureObservation("recovery_harness", "request-a", 7, 11, "request")
        )
    with pytest.raises(RecoveryRejected, match="failure_scope_widened"):
        affected.observe_failure(
            FailureObservation(
                "a4_liveness_authority", "request-a", 7, 11, "deployment"
            )
        )
    with pytest.raises(RecoveryRejected, match="failure_identity_mismatch"):
        affected.observe_failure(
            FailureObservation("a4_liveness_authority", "request-b", 7, 11, "request")
        )
    _fail(affected)

    assert (
        unrelated.authority,
        unrelated.ledger,
        unrelated.live_resources,
    ) == unrelated_before
    assert unrelated.phase == "active"


def test_terminal_and_cleanup_are_exact_once_while_delivery_history_is_retained() -> (
    None
):
    harness = _harness()
    harness.publish(
        position=1,
        token=301,
        writer=harness.authority,
        caller="request_gateway",
    )
    _fail(harness)
    successor = _recover(harness).successor_authority
    harness.publish(position=2, token=302, writer=successor, caller="request_gateway")
    history = harness.ledger

    assert harness.publish_terminal(outcome="completed", writer=successor) is True
    assert harness.publish_terminal(outcome="completed", writer=successor) is False
    assert harness.cleanup(writer=successor) is True
    releases = harness.release_count.copy()
    assert harness.cleanup(writer=successor) is False

    assert all(value == 0 for value in harness.live_resources.values())
    assert harness.release_count == releases
    assert all(value == 1 for value in releases.values())
    assert harness.ledger == history
    assert harness.terminal_outcome == "completed"
