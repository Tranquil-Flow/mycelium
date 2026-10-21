# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first contract tests for the A7 successor-recovery scaffold.

These tests were written before `mycelium_a7_kv_successor.py` existed. At the
baseline commit (`b90edaf`) they fail with a collection error because the module
is absent; against the raising skeleton they fail with `A7ScaffoldBoundary`; the
deterministic contract core is implemented in the following commit.

They exercise only the deterministic contract surface. They prove nothing about
KV transport, runtime recovery, product integration, or physical behaviour --
see the implementation contract
(`docs/superpowers/specs/2026-09-20-mycelium-a7-successor-recovery-implementation-contract.md`),
section 5.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import mycelium_a7_kv_successor as a7


INVENTORY = Path(__file__).with_name("inventory.v1.json")

# The 12 A7 closure requirements still unearned at the scaffold baseline, read
# from docs/handover/mycelium-completion-checklist.v1.json at b90edaf
# (A7 completed == ["specification"]).
UNEARNED_AT_BASELINE = frozenset(
    {
        "implementation",
        "product_integration",
        "deterministic_positive",
        "deterministic_negative",
        "physical_positive",
        "physical_negative",
        "ui_live_all_eight",
        "browser_navigation_reconnect",
        "browser_second_session",
        "full_regressions_audits",
        "architecture_handover",
        "atomic_feature_commit",
    }
)

CLAIM_BOUNDARY_FIELDS = frozenset(
    {
        "protocol",
        "gate_state",
        "scaffold_state",
        "claim_boundary",
        "unearned_closure_requirements",
        "unearned_count",
        "runtime_executed",
        "integrated",
        "physical_evidence",
        "qualification_claim",
        "promotion_authorized",
        "a6_dependency_integrated",
    }
)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _stage(name: str) -> str:
    return _digest(f"stage:{name}")


def _incarnation(name: str) -> str:
    return _digest(f"incarnation:{name}")


def _ledger(*stages: str) -> a7.RouteAckLedger:
    return a7.RouteAckLedger(
        replaced_stage_ids=[_stage(name) for name in stages],
        request_attempt=1,
        current_incarnations={_stage(name): _incarnation(name) for name in stages},
    )


def _checkpoint(**overrides: object) -> dict:
    document: dict[str, object] = {
        "protocol": a7.CHECKPOINT_PROTOCOL,
        "checkpoint_id": _digest("checkpoint-id:0"),
        "request_id": _digest("request"),
        "session_id": _digest("session"),
        "request_attempt": 1,
        "source_placement_id": _digest("placement:source"),
        "source_membership_incarnation": _incarnation("source"),
        "layer_range": [0, 8],
        "checkpoint_sequence": 0,
        "predecessor_digest": None,
        "committed_token_count": 64,
        "committed_prefix_digest": _digest("prefix:64"),
        "kv_position": 64,
        "byte_size": 8192,
        "encrypted_payload_reference": "ref:00000000000000000000000000000000",
        "expires_at_unix_ms": 1_800_000_000_000,
        "source_authority_generation": 3,
    }
    document.update(overrides)
    document["evidence_digest"] = a7.binding_digest(document)
    return document


def _ack(
    name: str = "stage-a",
    *,
    sequence: int = 0,
    count: int = 64,
    prefix: str | None = None,
    **overrides: object,
) -> dict:
    checkpoint_digest = _digest(f"checkpoint:{name}:{sequence}")
    document: dict[str, object] = {
        "protocol": a7.CHECKPOINT_ACK_PROTOCOL,
        "ack_id": _digest(f"ack:{name}:{sequence}"),
        "request_id": _digest("request"),
        "session_id": _digest("session"),
        "request_attempt": 1,
        "stage_id": _stage(name),
        "standby_placement_id": _digest(f"placement:standby:{name}"),
        "standby_membership_incarnation": _incarnation(name),
        "checkpoint_sequence": sequence,
        "checkpoint_digest": checkpoint_digest,
        "predecessor_digest": (
            None if sequence == 0 else _digest(f"checkpoint:{name}:{sequence - 1}")
        ),
        "committed_token_count": count,
        "committed_prefix_digest": (
            prefix if prefix is not None else _digest(f"prefix:{count}")
        ),
        "stored_checkpoint_digest_verified": True,
        "stored_checkpoint_digest": checkpoint_digest,
        "resource_reservation_generation": 2,
        "acknowledgement_authority": "standby_runtime",
        "ack_authority_generation": 5,
    }
    document.update(overrides)
    document["evidence_digest"] = a7.binding_digest(document)
    return document


def _identity(**overrides: object) -> dict:
    values: dict[str, object] = {
        field: _digest(f"identity:{field}")
        for fields in a7.COMPATIBILITY_IDENTITY_GROUPS.values()
        for field in fields
    }
    values.update(overrides)
    return values


def _compatible_result() -> dict:
    return a7.compare_compatibility(_identity(), _identity())


def _advanced_ledger() -> a7.RouteAckLedger:
    ledger = _ledger("stage-a")
    assert ledger.record(_ack("stage-a")) == "advanced"
    return ledger


def _eligibility(ledger: a7.RouteAckLedger | None, **overrides: object) -> dict:
    arguments: dict[str, object] = {
        "route_watermark": None if ledger is None else ledger.route_watermark,
        "gateway_committed_token_count": 64,
        "gateway_committed_prefix_digest": _digest("prefix:64"),
        "compatibility": _compatible_result(),
        "qualifier_current": True,
        "fence_installed": True,
        "a6_available": True,
    }
    arguments.update(overrides)
    return a7.evaluate_cutover_eligibility(**arguments)


# ---------------------------------------------------------------------------
# Claim boundary
# ---------------------------------------------------------------------------


def test_scaffold_declares_unintegrated_state_and_unearned_requirements() -> None:
    assert a7.SCAFFOLD_STATE == "scaffold_unintegrated"
    assert set(a7.UNEARNED_CLOSURE_REQUIREMENTS) == set(UNEARNED_AT_BASELINE)
    assert len(a7.UNEARNED_CLOSURE_REQUIREMENTS) == 12
    assert "specification" not in a7.UNEARNED_CLOSURE_REQUIREMENTS


def test_claim_boundary_is_closed_design_only_and_never_promotes() -> None:
    boundary = a7.scaffold_claim_boundary()

    assert set(boundary) == CLAIM_BOUNDARY_FIELDS
    assert boundary["protocol"] == "mycelium.a7_scaffold_claim_boundary.v1"
    assert boundary["gate_state"] == "design_only"
    assert boundary["scaffold_state"] == "scaffold_unintegrated"
    assert set(boundary["unearned_closure_requirements"]) == set(UNEARNED_AT_BASELINE)
    assert boundary["unearned_count"] == 12
    assert boundary["runtime_executed"] is False
    assert boundary["integrated"] is False
    assert boundary["physical_evidence"] is False
    assert boundary["qualification_claim"] is False
    assert boundary["promotion_authorized"] is False
    assert boundary["a6_dependency_integrated"] is False


# ---------------------------------------------------------------------------
# Closed records
# ---------------------------------------------------------------------------


def test_checkpoint_record_is_closed_and_digest_bound() -> None:
    document = _checkpoint()
    assert a7.validate_private_kv_checkpoint(document) == document

    extra = _checkpoint()
    extra["note"] = "unexpected"
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(extra)

    missing = _checkpoint()
    del missing["byte_size"]
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(missing)

    malformed = _checkpoint()
    malformed["committed_prefix_digest"] = "sha256:not-hex"
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(malformed)

    tampered = _checkpoint()
    tampered["byte_size"] = 8193
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(tampered)


def test_checkpoint_rejects_a_kv_position_that_is_not_the_committed_position() -> None:
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(kv_position=63))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(kv_position=65))


def test_checkpoint_rejects_invalid_bounds_and_types() -> None:
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(committed_token_count=-1))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(committed_token_count=True))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(expires_at_unix_ms=0))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(byte_size=0))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(encrypted_payload_reference=""))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(layer_range=[4, 4]))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(layer_range=[0, 8, 9]))
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(_checkpoint(layer_range=[-1, 8]))


def test_checkpoint_sequence_zero_admits_no_predecessor() -> None:
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint(
            _checkpoint(predecessor_digest=_digest("checkpoint-id:earlier"))
        )


def test_ack_record_is_closed_and_requires_the_stored_digest_to_match() -> None:
    assert a7.validate_private_kv_checkpoint_ack(_ack()) == _ack()

    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint_ack(
            _ack(stored_checkpoint_digest=_digest("other"))
        )
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint_ack(
            _ack(stored_checkpoint_digest_verified=False)
        )
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint_ack(
            _ack(acknowledgement_authority="router")
        )
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint_ack(_ack(committed_token_count=-1))

    extra = _ack()
    extra["extra"] = 1
    with pytest.raises(ValueError):
        a7.validate_private_kv_checkpoint_ack(extra)


# ---------------------------------------------------------------------------
# Route-wide acknowledged watermark
# ---------------------------------------------------------------------------


def test_route_watermark_requires_every_replaced_stage() -> None:
    ledger = _ledger("stage-a", "stage-b")

    assert ledger.route_watermark is None
    assert ledger.pending_stage_ids == tuple(sorted([_stage("stage-a"), _stage("stage-b")]))
    assert ledger.record(_ack("stage-a")) == "accepted_route_pending"
    assert ledger.route_watermark is None
    assert ledger.pending_stage_ids == (_stage("stage-b"),)
    assert ledger.record(_ack("stage-b")) == "advanced"
    assert ledger.route_watermark == {
        "protocol": "mycelium.a7_route_watermark.v1",
        "request_attempt": 1,
        "committed_token_count": 64,
        "committed_prefix_digest": _digest("prefix:64"),
        "stage_ids": sorted([_stage("stage-a"), _stage("stage-b")]),
    }


def test_route_watermark_requires_identical_count_and_prefix_digest() -> None:
    ledger = _ledger("stage-a", "stage-b")
    ledger.record(_ack("stage-a"))

    outcome = ledger.record(
        _ack("stage-b", count=48, prefix=_digest("prefix:48"))
    )
    assert outcome == "accepted_route_pending"
    assert ledger.route_watermark is None
    assert ledger.lag_against(64) is None

    outcome = ledger.record(
        _ack("stage-b", sequence=1, count=64, prefix=_digest("prefix:64"))
    )
    assert outcome == "advanced"
    assert ledger.route_watermark["committed_token_count"] == 64


def test_an_exact_repeat_is_idempotent_without_authority_advance() -> None:
    ledger = _ledger("stage-a")
    assert ledger.record(_ack("stage-a")) == "advanced"
    watermark = ledger.route_watermark

    assert ledger.record(_ack("stage-a")) == "idempotent"
    assert ledger.route_watermark == watermark
    assert ledger.stage_positions == {_stage("stage-a"): 64}


def test_a_conflicting_repeat_is_rejected() -> None:
    ledger = _ledger("stage-a")
    ledger.record(_ack("stage-a"))

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", count=65, prefix=_digest("prefix:65")))
    assert excinfo.value.reason == "conflicting_repeat"

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(
            _ack(
                "stage-a",
                checkpoint_digest=_digest("forged"),
                stored_checkpoint_digest=_digest("forged"),
            )
        )
    assert excinfo.value.reason == "conflicting_repeat"

    assert ledger.route_watermark["committed_token_count"] == 64


def test_a_skipped_predecessor_is_rejected() -> None:
    ledger = _ledger("stage-a")

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", sequence=1))
    assert excinfo.value.reason == "skipped_predecessor"

    ledger.record(_ack("stage-a", sequence=0))
    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", sequence=2))
    assert excinfo.value.reason == "skipped_predecessor"
    assert ledger.stage_positions == {_stage("stage-a"): 64}


def test_a_broken_chain_is_rejected() -> None:
    ledger = _ledger("stage-a")
    ledger.record(_ack("stage-a"))

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(
            _ack("stage-a", sequence=1, predecessor_digest=_digest("wrong"))
        )
    assert excinfo.value.reason == "predecessor_mismatch"


def test_a_regressed_position_is_rejected() -> None:
    ledger = _ledger("stage-a")
    ledger.record(_ack("stage-a", sequence=0, count=64))
    ledger.record(_ack("stage-a", sequence=1, count=64))

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", sequence=0, count=64))
    assert excinfo.value.reason == "regressed_position"

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", sequence=2, count=48))
    assert excinfo.value.reason == "regressed_position"


def test_stale_attempt_and_foreign_incarnation_are_rejected() -> None:
    ledger = _ledger("stage-a")

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", request_attempt=2))
    assert excinfo.value.reason == "stale_attempt"

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(
            _ack("stage-a", standby_membership_incarnation=_incarnation("other"))
        )
    assert excinfo.value.reason == "stale_incarnation"

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-c"))
    assert excinfo.value.reason == "ack_unknown_stage"

    assert ledger.route_watermark is None
    assert ledger.stage_positions == {_stage("stage-a"): None}


def test_the_acknowledged_watermark_never_regresses() -> None:
    ledger = _ledger("stage-a", "stage-b")
    ledger.record(_ack("stage-a"))
    assert ledger.record(_ack("stage-b")) == "advanced"
    assert ledger.route_watermark["committed_token_count"] == 64

    # stage-a advances alone: lag is visible, the watermark does not move.
    assert ledger.record(_ack("stage-a", sequence=1, count=80)) == (
        "accepted_route_pending"
    )
    assert ledger.route_watermark["committed_token_count"] == 64

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-a", sequence=2, count=48))
    assert excinfo.value.reason == "regressed_position"
    assert ledger.route_watermark["committed_token_count"] == 64


def test_a_conflicting_digest_at_the_same_position_fails_closed() -> None:
    ledger = _ledger("stage-a", "stage-b")
    ledger.record(_ack("stage-a"))
    ledger.record(_ack("stage-b"))
    assert ledger.route_watermark["committed_token_count"] == 64

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(
            _ack("stage-b", sequence=1, count=64, prefix=_digest("prefix:forged"))
        )
    assert excinfo.value.reason == "conflicting_stage_agreement"
    assert ledger.route_watermark["committed_prefix_digest"] == _digest("prefix:64")


def test_unknown_lag_is_none_and_never_defaults_to_zero_or_ready() -> None:
    ledger = _ledger("stage-a")
    assert ledger.lag_against(64) is None

    ledger.record(_ack("stage-a"))
    assert ledger.lag_against(64) == 0
    assert ledger.lag_against(80) == 16


def test_ledger_rejection_reasons_stay_in_the_closed_vocabulary() -> None:
    ledger = _ledger("stage-a")
    with pytest.raises(a7.A7ContractViolation) as excinfo:
        ledger.record(_ack("stage-b"))
    assert excinfo.value.reason in a7.LEDGER_REJECTION_REASONS
    assert len(a7.LEDGER_REJECTION_REASONS) == 9


# ---------------------------------------------------------------------------
# Compatibility identity
# ---------------------------------------------------------------------------


def test_compatibility_requires_exact_identity_match() -> None:
    source = _identity()
    result = a7.compare_compatibility(source, dict(source))

    assert set(result) == {
        "protocol",
        "compatible",
        "mismatches",
        "unknown_fields",
        "compared_fields",
    }
    assert result["protocol"] == "mycelium.a7_kv_compatibility_result.v1"
    assert result["compatible"] is True
    assert result["mismatches"] == []
    assert result["unknown_fields"] == []
    assert result["compared_fields"] == len(source)


def test_a_single_identity_difference_is_incompatible() -> None:
    result = a7.compare_compatibility(
        _identity(), _identity(cache_implementation="mlx")
    )
    assert result["compatible"] is False
    assert result["mismatches"] == ["cache_implementation"]
    assert result["unknown_fields"] == []


def test_unknown_or_absent_compatibility_is_incompatible() -> None:
    source = _identity()

    absent = _identity()
    del absent["rope_configuration_and_scaling"]
    result = a7.compare_compatibility(source, absent)
    assert result["compatible"] is False
    assert "rope_configuration_and_scaling" in result["unknown_fields"]

    unknown = a7.compare_compatibility(
        source, _identity(device_layout_encoding=None)
    )
    assert unknown["compatible"] is False
    assert "device_layout_encoding" in unknown["unknown_fields"]

    with pytest.raises(a7.A7ContractViolation):
        a7.compare_compatibility(source, _identity(backend=7))
    with pytest.raises(a7.A7ContractViolation):
        a7.compare_compatibility({**source, "extra": "x"}, source)


def test_compatibility_result_echoes_field_names_not_values() -> None:
    source = _identity()
    result = a7.compare_compatibility(
        source, _identity(attention_layout="forged-layout")
    )
    serialized = json.dumps(result, sort_keys=True)

    assert "forged-layout" not in serialized
    for value in source.values():
        assert value not in serialized


def test_module_constants_match_the_frozen_acceptance_inventory() -> None:
    inventory = json.loads(INVENTORY.read_text("utf-8"))

    assert a7.COMPATIBILITY_IDENTITY_GROUPS == inventory["compatibility_fields"]
    assert set(a7.OUTCOMES) == set(inventory["outcomes"])
    assert set(a7.OUTCOMES) == {
        "fenced_kv_successor",
        "full_context_replay",
        "aborted",
    }
    assert list(a7.FENCE_INSTALLERS) == inventory["fencing_contract"]["installers"]
    assert a7.WATERMARK_ACTIONS["partial_acknowledgement"] == (
        inventory["watermark_contract"]["partial_acknowledgement_action"]
    )
    assert a7.WATERMARK_ACTIONS["ahead_of_gateway"] == (
        inventory["watermark_contract"]["ahead_of_gateway_action"]
    )
    assert a7.WATERMARK_ACTIONS["behind_gateway"] == (
        inventory["watermark_contract"]["behind_gateway_action"]
    )
    assert a7.WATERMARK_ACTIONS["exact_repeat"] == (
        inventory["watermark_contract"]["exact_repeat_action"]
    )
    assert a7.WATERMARK_ACTIONS["skipped_regressed_or_conflicting"] == (
        inventory["watermark_contract"]["skipped_regressed_or_conflicting_action"]
    )


# ---------------------------------------------------------------------------
# Fencing
# ---------------------------------------------------------------------------


def test_fence_generations_advance_monotonically() -> None:
    fence = a7.FenceLedger(
        request_attempt=1, path_generation=1, fencing_generation=1
    )
    assert fence.state == {
        "protocol": "mycelium.a7_fence_state.v1",
        "request_attempt": 1,
        "path_generation": 1,
        "fencing_generation": 1,
    }

    state = fence.advance()
    assert state["request_attempt"] == 2
    assert state["path_generation"] == 2
    assert state["fencing_generation"] == 2
    assert fence.fencing_generation == 2

    state = fence.advance()
    assert state["request_attempt"] == 3
    assert state["path_generation"] == 3
    assert state["fencing_generation"] == 3

    with pytest.raises(a7.A7ContractViolation):
        a7.FenceLedger(request_attempt=0, path_generation=1, fencing_generation=1)


def test_old_generation_events_are_rejected_without_authority_mutation() -> None:
    fence = a7.FenceLedger(
        request_attempt=1, path_generation=1, fencing_generation=1
    )
    fence.advance()

    assert fence.accepts_generation(2) is True
    assert fence.accepts_generation(1) is False
    assert fence.accepts_generation(3) is False

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        fence.grant_publication(1)
    assert excinfo.value.reason == "stale_generation"

    fence.grant_publication(2)
    with pytest.raises(a7.A7ContractViolation) as excinfo:
        fence.grant_publication(2)
    assert excinfo.value.reason == "duplicate_publisher"

    assert set(a7.FENCE_REASONS) == {
        "fence_state_invalid",
        "stale_generation",
        "duplicate_publisher",
        "unknown_participant",
    }


def test_cutover_is_withheld_until_every_installer_confirms() -> None:
    fence = a7.FenceLedger(
        request_attempt=1, path_generation=1, fencing_generation=1
    )
    assert fence.fence_installed is False

    for participant in a7.FENCE_INSTALLERS[:-1]:
        fence.install(participant)
    assert fence.fence_installed is False
    assert set(fence.installed_participants) == set(a7.FENCE_INSTALLERS[:-1])

    fence.install(a7.FENCE_INSTALLERS[-1])
    assert fence.fence_installed is True

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        fence.install("unknown-participant")
    assert excinfo.value.reason == "unknown_participant"

    fence.advance()
    assert fence.fence_installed is False


# ---------------------------------------------------------------------------
# Cutover eligibility and disjoint outcomes
# ---------------------------------------------------------------------------


def test_cutover_eligibility_requires_exact_gateway_equality() -> None:
    ledger = _advanced_ledger()

    eligible = _eligibility(ledger)
    assert eligible == {
        "protocol": "mycelium.a7_cutover_eligibility.v1",
        "outcome": "fenced_kv_successor",
        "kv_successor_eligible": True,
        "reason": "eligible",
        "a6_available": True,
    }

    ahead = _eligibility(ledger, gateway_committed_token_count=48)
    assert ahead["kv_successor_eligible"] is False
    assert ahead["reason"] == "watermark_ahead_of_gateway"
    assert ahead["outcome"] == "full_context_replay"

    behind = _eligibility(
        ledger,
        gateway_committed_token_count=80,
        gateway_committed_prefix_digest=_digest("prefix:80"),
    )
    assert behind["reason"] == "watermark_behind_gateway"

    mismatch = _eligibility(
        ledger, gateway_committed_prefix_digest=_digest("prefix:forged")
    )
    assert mismatch["reason"] == "prefix_digest_mismatch"


def test_every_rejection_maps_to_a6_replay_or_an_honest_abort() -> None:
    ledger = _advanced_ledger()

    for a6_available in (True, False):
        decision = _eligibility(None, a6_available=a6_available)
        assert decision["kv_successor_eligible"] is False
        assert decision["reason"] == "route_watermark_unavailable"
        assert decision["outcome"] == (
            "full_context_replay" if a6_available else "aborted"
        )

    incompat = a7.compare_compatibility(
        _identity(), _identity(dtype="bfloat16")
    )
    assert _eligibility(ledger, compatibility=incompat)["reason"] == (
        "compatibility_incompatible"
    )
    assert _eligibility(ledger, qualifier_current=False)["reason"] == (
        "qualification_not_current"
    )
    assert _eligibility(ledger, fence_installed=False)["reason"] == (
        "fence_not_installed"
    )

    rejected = _eligibility(ledger, fence_installed=False)
    assert rejected["outcome"] == "full_context_replay"
    assert rejected["outcome"] != "fenced_kv_successor"

    with pytest.raises(a7.A7ContractViolation):
        _eligibility(ledger, compatibility={"protocol": "wrong"})


def test_eligibility_reasons_stay_in_the_closed_vocabulary() -> None:
    ledger = _advanced_ledger()
    seen = {
        _eligibility(ledger)["reason"],
        _eligibility(ledger, fence_installed=False)["reason"],
        _eligibility(None)["reason"],
        _eligibility(ledger, gateway_committed_token_count=48)["reason"],
        _eligibility(
            ledger,
            gateway_committed_token_count=80,
            gateway_committed_prefix_digest=_digest("prefix:80"),
        )["reason"],
    }
    assert seen <= set(a7.ELIGIBILITY_REASONS)
    assert "eligible" in a7.ELIGIBILITY_REASONS
    assert len(a7.ELIGIBILITY_REASONS) == 8


# ---------------------------------------------------------------------------
# Standby lifecycle
# ---------------------------------------------------------------------------


def test_standby_lifecycle_is_closed_and_ordered() -> None:
    assert a7.STANDBY_CHAIN == (
        "proposed",
        "acquiring",
        "loaded",
        "challenged",
        "qualified",
        "shadowing",
        "cutover_pending",
        "active_successor",
        "draining",
        "released",
    )
    assert set(a7.STANDBY_STATES) == set(a7.STANDBY_CHAIN) | {"rejected", "failed"}

    state = a7.STANDBY_CHAIN[0]
    for target in a7.STANDBY_CHAIN[1:]:
        state = a7.transition_standby(state, target)
    assert state == "released"


def test_standby_readiness_requires_qualified_not_artifact_presence() -> None:
    assert a7.transition_standby("qualified", "shadowing") == "shadowing"
    assert a7.transition_standby("shadowing", "rejected") == "rejected"

    with pytest.raises(a7.A7ContractViolation) as excinfo:
        a7.transition_standby("loaded", "shadowing")
    assert excinfo.value.reason == "illegal_standby_transition"

    with pytest.raises(a7.A7ContractViolation):
        a7.transition_standby("acquiring", "qualified")
    with pytest.raises(a7.A7ContractViolation):
        a7.transition_standby("released", "qualified")
    with pytest.raises(a7.A7ContractViolation):
        a7.transition_standby("unknown-state", "loaded")


def test_checkpoint_digest_refuses_signed_or_whitespace_hex():
    for prefix in ("+", "-", " "):
        with pytest.raises(ValueError):
            a7.validate_private_kv_checkpoint(
                _checkpoint(committed_prefix_digest="sha256:" + prefix + "0" * 63)
            )


def test_publication_generation_refuses_bool_and_float_without_consuming_grant():
    for generation in (True, 1.0):
        ledger = a7.FenceLedger(request_attempt=1, path_generation=1, fencing_generation=1)
        with pytest.raises(a7.A7ContractViolation, match="stale_generation"):
            ledger.grant_publication(generation)
        ledger.grant_publication(1)


def test_cutover_refuses_self_contradictory_compatibility_summary():
    ledger = _advanced_ledger()
    for changes in ({"mismatches": ["model_id"]}, {"unknown_fields": ["model_id"]},
                    {"compared_fields": 0}):
        compatibility = {**_compatible_result(), **changes}
        with pytest.raises(a7.A7ContractViolation, match="compatibility_result_invalid"):
            _eligibility(ledger, compatibility=compatibility)


def test_terminal_standby_states_refuse_every_transition():
    for current in ("released", "rejected", "failed"):
        for target in a7.STANDBY_STATES:
            with pytest.raises(a7.A7ContractViolation, match="illegal_standby_transition"):
                a7.transition_standby(current, target)
