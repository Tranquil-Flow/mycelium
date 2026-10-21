# SPDX-License-Identifier: AGPL-3.0-only
"""A12 deterministic rung-authority suite: RED-first contracts for the four rungs.

This suite is written before the implementation.  It pins the deterministic half of
A12 — the four separately authored eligibility/readiness rungs of
``docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md``
section 4 — and it must pass with the implementation and fail without it.

What it can and cannot claim:

- every rung record here is authored by control-plane arithmetic over
  caller-supplied, shape-validated records; **no device executes anything**,
  so ``physical_execution`` is false in every record this lane can produce;
- a rung that cannot be authored says so: the decision is ``withheld`` (or
  ``unknown``/``revoked``) and the blocker list names the exact missing input,
  never a zero, a healthy default or an inferred support;
- native physical positives remain RED: rung 4 (operation qualification) cannot be
  authored without owner-private physical execution, and ``qualify_mobile_operation``
  keeps refusing; the frozen weak-RED suite keeps those failures.

Run (serial, no ``-n``):

    env -u PYTHONPATH python3.14 -m pytest tests/a12_mobile_deterministic -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mycelium_mobile.a12_activation as a12
from mycelium_qualification.signing import generate_ed25519_signer

ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = ROOT / "tests" / "a12_acceptance" / "inventory.v1.json"

NOW_MS = 1_754_000_000_000.0
MEMBER_KEY_DIGEST_PLACEHOLDER = "sha256:" + "a" * 64


def _profile(
    *,
    signer_endpoint_id: str,
    member_key_digest: str,
    member_ref: str = "member:1",
    platform_family: str = "android",
    profile_generation: int = 3,
    membership_generation: int = 3,
    incarnation: str = "inc-2",
    issued_at_unix_ms: float = NOW_MS - 1_000.0,
    expires_at_unix_ms: float = NOW_MS + 600_000.0,
) -> dict[str, object]:
    return {
        "protocol": a12.PEER_PROFILE_PROTOCOL,
        "member_ref": member_ref,
        "member_endpoint_id": signer_endpoint_id,
        "member_key_digest": member_key_digest,
        "platform_family": platform_family,
        "runtime_build_digest": "sha256:" + "1" * 64,
        "profile_generation": profile_generation,
        "membership_generation": membership_generation,
        "incarnation": incarnation,
        "issued_at_unix_ms": issued_at_unix_ms,
        "expires_at_unix_ms": expires_at_unix_ms,
    }


def _signed_profile(**overrides: object) -> tuple[dict[str, object], str]:
    signer = generate_ed25519_signer(endpoint_id="member-endpoint-1")
    profile = _profile(
        signer_endpoint_id=signer.endpoint_id,
        member_key_digest=signer.verification_key_digest,
        **overrides,
    )
    return a12.sign_member_profile(signer=signer, profile=profile), signer.verification_key_digest


def _requirements(*, state: str = "satisfied") -> dict[str, object]:
    return {
        "durable_device_owned_key": {"state": state, "key_digest": "sha256:" + "b" * 64},
        "consent": {"state": state, "consent_ref": "consent:1", "granted": True},
        "current_lease_incarnation_and_generation": {
            "state": state,
            "incarnation": "inc-2",
            "membership_generation": 3,
            "lease_expires_at_unix_ms": NOW_MS + 300_000.0,
        },
        "revocation_checks": {"state": state, "checked_at_unix_ms": NOW_MS - 500.0},
        "authenticated_reconnect": {"state": state, "reconnect_ref": "reconnect:1"},
    }


def _observation(source_kind: str = a12.NATIVE_SOURCE_KIND) -> dict[str, object]:
    return {
        "protocol": a12.RUNTIME_OBSERVATION_PROTOCOL,
        "platform_family": "android",
        "runtime_build_digest": "sha256:" + "1" * 64,
        "profile_generation": 3,
        "evidence_generation": 4,
        "source_kind": source_kind,
        "observed_at_unix_ms": NOW_MS - 60_000.0,
        "measurement_duration_ms": 600_000.0,
        "sample_count": 60,
        "authority": "a9_role_class_qualifier",
        "metrics": {
            "sustainable_memory_bytes": 1.5e9,
            "thermal_state": "unknown",
        },
    }


def _capability_sources(kind: str = a12.NATIVE_SOURCE_KIND) -> dict[str, str]:
    return {family: kind for family in a12.REQUIRED_GATE_FAMILIES}


# ---------------------------------------------------------------------------
# The gate's rung vocabulary stays frozen against the acceptance inventory
# ---------------------------------------------------------------------------


def test_rung_vocabulary_and_declarations_match_the_frozen_inventory() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text("utf-8"))
    rungs = inventory["eligibility_rungs"]

    assert tuple(rung["rung_id"] for rung in rungs) == a12.RUNG_IDS
    assert {rung["rung_id"]: list(rung["cannot_grant"]) for rung in rungs} == {
        rung_id: list(cannot_grant)
        for rung_id, cannot_grant in a12.RUNG_CANNOT_GRANT.items()
    }
    assert a12.RUNG_PROOF_PROTOCOL == "mycelium.mobile_rung_proof.v1"
    assert a12.MOBILE_PLATFORM_FAMILIES == ("android", "ios", "ipados")


# ---------------------------------------------------------------------------
# Rung 1: signed membership (a9_membership_authority)
# ---------------------------------------------------------------------------


def test_signed_membership_is_withheld_until_every_required_input_is_supplied() -> None:
    record = a12.enroll_mobile_member(
        member_ref="member:1",
        platform_family="android",
        source_kind=a12.NATIVE_SOURCE_KIND,
        route_ready=False,
    )

    assert record["protocol"] == a12.RUNG_PROOF_PROTOCOL
    assert record["rung_id"] == "signed_membership"
    assert record["authority"] == a12.RUNG_AUTHORITIES["signed_membership"]
    assert record["decision"] == "withheld"
    assert record["route_ready"] is False
    assert record["grants"] == [a12.RUNG_GRANTS["signed_membership"]]
    assert record["cannot_grant"] == list(a12.RUNG_CANNOT_GRANT["signed_membership"])
    assert record["blockers"] == [
        "durable_device_owned_key",
        "signed_a9_v2_profile",
        "current_lease_incarnation_and_generation",
        "revocation_checks",
        "consent",
        "authenticated_reconnect",
    ]
    assert record["physical_execution"] is False


def test_signed_membership_is_granted_from_a_current_member_signed_profile() -> None:
    envelope, key_digest = _signed_profile()
    record = a12.enroll_mobile_member(
        member_ref="member:1",
        platform_family="android",
        source_kind=a12.NATIVE_SOURCE_KIND,
        route_ready=False,
        profile_envelope=envelope,
        requirements=_requirements(),
        expected_member_key_digest=key_digest,
        now_unix_ms=NOW_MS,
    )

    assert record["decision"] == "eligible"
    assert record["blockers"] == []
    assert record["grants"] == [a12.RUNG_GRANTS["signed_membership"]]
    assert record["profile_generation"] == 3
    assert record["membership_generation"] == 3
    assert record["route_ready"] is False
    assert record["cannot_grant"] == list(a12.RUNG_CANNOT_GRANT["signed_membership"])
    assert record["physical_execution"] is False


def test_signed_membership_rejects_a_profile_signed_by_another_member() -> None:
    """The profile binds the member's own durable key; a foreign key cannot sign it."""

    envelope, _ = _signed_profile()
    foreign = generate_ed25519_signer(endpoint_id="member-endpoint-1")
    forged = {**envelope, "signature": foreign.sign(envelope["message"])}

    with pytest.raises(a12.A12ContractError) as error:
        a12.verify_member_signed_profile(forged, now_unix_ms=NOW_MS)
    assert error.value.code == "member_profile_signature_invalid"


def test_signed_membership_rejects_a_profile_key_mismatch_and_an_expired_profile() -> None:
    envelope, key_digest = _signed_profile()

    with pytest.raises(a12.A12ContractError) as mismatch:
        a12.verify_member_signed_profile(
            envelope,
            now_unix_ms=NOW_MS,
            expected_member_key_digest="sha256:" + "c" * 64,
        )
    assert mismatch.value.code == "member_profile_key_mismatch"

    expired, _ = _signed_profile(
        issued_at_unix_ms=NOW_MS - 1_000_000.0,
        expires_at_unix_ms=NOW_MS - 1.0,
    )
    with pytest.raises(a12.A12ContractError) as stale:
        a12.verify_member_signed_profile(expired, now_unix_ms=NOW_MS)
    assert stale.value.code == "member_profile_expired"

    assert a12.verify_member_signed_profile(
        envelope, now_unix_ms=NOW_MS, expected_member_key_digest=key_digest
    )["member_ref"] == "member:1"


def test_signed_membership_denied_consent_withholds_and_revocation_is_terminal() -> None:
    envelope, key_digest = _signed_profile()
    denied = _requirements()
    denied["consent"] = {"state": "withheld", "consent_ref": "consent:1", "granted": False}

    record = a12.enroll_mobile_member(
        member_ref="member:1",
        platform_family="android",
        source_kind=a12.NATIVE_SOURCE_KIND,
        route_ready=False,
        profile_envelope=envelope,
        requirements=denied,
        expected_member_key_digest=key_digest,
        now_unix_ms=NOW_MS,
    )
    assert record["decision"] == "withheld"
    assert record["blockers"] == ["consent"]

    revoked = _requirements()
    revoked["revocation_checks"] = {"state": "revoked", "checked_at_unix_ms": NOW_MS}
    record = a12.enroll_mobile_member(
        member_ref="member:1",
        platform_family="android",
        source_kind=a12.NATIVE_SOURCE_KIND,
        route_ready=False,
        profile_envelope=envelope,
        requirements=revoked,
        expected_member_key_digest=key_digest,
        now_unix_ms=NOW_MS,
    )
    assert record["decision"] == "revoked"
    assert record["blockers"] == ["member_revoked"]


def test_signed_membership_still_refuses_a_non_native_source_and_a_ready_route() -> None:
    with pytest.raises(a12.A12ContractError) as source_error:
        a12.enroll_mobile_member(
            member_ref="member:1",
            platform_family="android",
            source_kind="synthetic_conformance",
            route_ready=False,
        )
    assert source_error.value.code == "non_native_source_cannot_qualify"

    with pytest.raises(a12.A12ContractError) as route_error:
        a12.enroll_mobile_member(
            member_ref="member:1",
            platform_family="android",
            source_kind=a12.NATIVE_SOURCE_KIND,
            route_ready=True,
        )
    assert route_error.value.code == "route_ready_must_start_false"


# ---------------------------------------------------------------------------
# Rung 2: capability eligibility (a9_role_class_qualifier)
# ---------------------------------------------------------------------------


def test_capability_eligibility_withholds_without_current_prior_rung_proofs() -> None:
    eligibility = a12.qualify_capability_eligibility(
        member_ref="member:1",
        platform_family="android",
        role="probe_contributor",
        observation=_observation(),
        now_unix_ms=NOW_MS,
    )

    assert eligibility["protocol"] == a12.RUNG_PROOF_PROTOCOL
    assert eligibility["rung_id"] == "capability_eligibility"
    assert eligibility["authority"] == a12.RUNG_AUTHORITIES["capability_eligibility"]
    assert eligibility["decision"] == "withheld"
    assert eligibility["role"] == "probe_contributor"
    assert eligibility["cannot_grant"] == list(a12.RUNG_CANNOT_GRANT["capability_eligibility"])
    assert "prior_rung_proof_not_supplied" in eligibility["blockers"]
    assert eligibility["physical_execution"] is False


def test_capability_eligibility_qualifies_one_exact_role_from_native_evidence() -> None:
    eligibility = a12.qualify_capability_eligibility(
        member_ref="member:1",
        platform_family="android",
        role="probe_contributor",
        observation=_observation(),
        prior_rung_decisions={"signed_membership": "eligible"},
        role_constraints={"max_concurrency": 1, "foreground_only": True},
        capability_sources=_capability_sources(),
        now_unix_ms=NOW_MS,
    )

    assert eligibility["decision"] == "eligible"
    assert eligibility["blockers"] == []
    assert eligibility["role"] == "probe_contributor"
    assert eligibility["grants"] == [a12.RUNG_GRANTS["capability_eligibility"]]
    # The rung that owns this decision can never author the later ones.
    assert eligibility["cannot_grant"] == list(
        a12.RUNG_CANNOT_GRANT["capability_eligibility"]
    )
    assert eligibility["grants"][0] not in eligibility["cannot_grant"]


def test_capability_eligibility_labels_a_declared_or_synthetic_observation() -> None:
    for source_kind in ("synthetic_conformance", "termux_conformance", "operator_declared"):
        with pytest.raises(a12.A12ContractError) as error:
            a12.qualify_capability_eligibility(
                member_ref="member:1",
                platform_family="android",
                role="probe_contributor",
                observation=_observation(source_kind),
                prior_rung_decisions={"signed_membership": "eligible"},
                role_constraints={"max_concurrency": 1},
                capability_sources=_capability_sources(),
                now_unix_ms=NOW_MS,
            )
        assert error.value.code == "non_native_source_cannot_qualify"


def test_capability_eligibility_never_treats_a_missing_metric_as_zero() -> None:
    eligibility = a12.qualify_capability_eligibility(
        member_ref="member:1",
        platform_family="android",
        role="probe_contributor",
        observation=_observation(),
        prior_rung_decisions={"signed_membership": "eligible"},
        role_constraints={"max_concurrency": 1},
        capability_sources={family: "unknown" for family in a12.REQUIRED_GATE_FAMILIES},
        now_unix_ms=NOW_MS,
    )

    assert eligibility["decision"] == "ineligible"
    assert eligibility["blockers"] == [
        "capability_family_observation_unknown" for _ in a12.REQUIRED_GATE_FAMILIES
    ]
    assert eligibility["metrics"]["thermal_state"] == "unknown"
    assert "sustainable_memory_bytes" in eligibility["metrics"]
    assert 0 not in [
        value for value in eligibility["metrics"].values() if isinstance(value, (int, float))
    ] or eligibility["metrics"]["sustainable_memory_bytes"] > 0


def test_capability_eligibility_blocks_on_a_stale_prior_rung() -> None:
    with pytest.raises(a12.A12ContractError) as error:
        a12.qualify_capability_eligibility(
            member_ref="member:1",
            platform_family="android",
            role="probe_contributor",
            observation=_observation(),
            prior_rung_decisions={"signed_membership": "stale"},
            role_constraints={"max_concurrency": 1},
            capability_sources=_capability_sources(),
            now_unix_ms=NOW_MS,
        )
    assert error.value.code == "prior_rung_not_current"


# ---------------------------------------------------------------------------
# Rung 3: artifact readiness (a2_provisioner)
# ---------------------------------------------------------------------------


def test_artifact_readiness_promotes_only_the_assigned_artifact() -> None:
    readiness = a12.promote_assignment_artifact(
        member_ref="member:1",
        assignment_ref="assignment:1",
        artifact_digest="sha256:" + "0" * 64,
    )

    assert readiness["rung_id"] == "artifact_readiness"
    assert readiness["authority"] == a12.RUNG_AUTHORITIES["artifact_readiness"]
    assert readiness["decision"] == "eligible"
    assert readiness["assignment_ref"] == "assignment:1"
    assert readiness["artifact_digest"] == "sha256:" + "0" * 64
    assert readiness["grants"] == [a12.RUNG_GRANTS["artifact_readiness"]]
    assert readiness["cannot_grant"] == list(a12.RUNG_CANNOT_GRANT["artifact_readiness"])
    assert readiness["cache_presence_used_as_proof"] is False
    assert readiness["physical_execution"] is False


def test_artifact_readiness_rejects_non_atomic_or_unbounded_promotion() -> None:
    for overrides, code in (
        ({"promotion": "best_effort"}, "artifact_promotion_must_be_atomic"),
        ({"artifact_format": "executable"}, "artifact_format_unsupported"),
        ({"integrity": "assumed"}, "artifact_integrity_evidence_invalid"),
        ({"quota_headroom": "unbounded"}, "artifact_quota_headroom_invalid"),
        ({"cancellation_policy": "none"}, "artifact_cancellation_policy_invalid"),
        ({"acquisition": "single_shot"}, "artifact_acquisition_policy_invalid"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.promote_assignment_artifact(
                member_ref="member:1",
                assignment_ref="assignment:1",
                artifact_digest="sha256:" + "0" * 64,
                **overrides,
            )
        assert error.value.code == code

    with pytest.raises(a12.A12ContractError) as digest:
        a12.promote_assignment_artifact(
            member_ref="member:1",
            assignment_ref="assignment:1",
            artifact_digest="sha256:short",
        )
    assert digest.value.code == "artifact_readiness_digest_invalid"


def test_artifact_readiness_cannot_author_a_capability_decision() -> None:
    readiness = a12.promote_assignment_artifact(
        member_ref="member:1",
        assignment_ref="assignment:1",
        artifact_digest="sha256:" + "0" * 64,
    )
    assert "capability_eligibility" in readiness["cannot_grant"]
    assert "operation_qualification" in readiness["cannot_grant"]


# ---------------------------------------------------------------------------
# Rung 4: operation qualification (mobile_role_operation_qualifier)
# ---------------------------------------------------------------------------


def test_operation_qualification_stays_withheld_without_physical_execution() -> None:
    record = a12.author_operation_qualification(
        member_ref="member:1",
        role="qualified_model_stage",
        platform_family="android",
        runtime_build_digest="sha256:" + "1" * 64,
        prior_rung_decisions={
            "signed_membership": "eligible",
            "capability_eligibility": "eligible",
            "artifact_readiness": "eligible",
        },
        now_unix_ms=NOW_MS,
    )

    assert record["rung_id"] == "operation_qualification"
    assert record["authority"] == a12.RUNG_AUTHORITIES["operation_qualification"]
    assert record["decision"] == "withheld"
    assert record["blockers"] == ["physical_positive_and_negative_evidence"]
    assert record["physical_execution"] is False
    assert record["grants"] == [a12.RUNG_GRANTS["operation_qualification"]]


def test_operation_qualification_never_accepts_synthetic_or_caller_authored_execution() -> None:
    with pytest.raises(a12.A12ContractError) as synthetic:
        a12.author_operation_qualification(
            member_ref="member:1",
            role="probe_contributor",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
            prior_rung_decisions={"signed_membership": "eligible", "capability_eligibility": "eligible"},
            physical_execution={
                "source_kind": "synthetic_conformance",
                "executed": True,
                "positive_cases": 1,
                "negative_cases": 1,
            },
            now_unix_ms=NOW_MS,
        )
    assert synthetic.value.code == "non_native_source_cannot_qualify"

    with pytest.raises(a12.A12ContractError) as caller_authored:
        a12.author_operation_qualification(
            member_ref="member:1",
            role="probe_contributor",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
            prior_rung_decisions={"signed_membership": "eligible", "capability_eligibility": "eligible"},
            physical_execution={
                "source_kind": a12.NATIVE_SOURCE_KIND,
                "executed": True,
                "positive_cases": 1,
                "negative_cases": 1,
            },
            now_unix_ms=NOW_MS,
        )
    assert caller_authored.value.code == "physical_execution_requires_owner_private_gate"


def test_operation_qualification_consumes_only_current_prior_rungs() -> None:
    with pytest.raises(a12.A12ContractError) as error:
        a12.author_operation_qualification(
            member_ref="member:1",
            role="qualified_model_stage",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
            prior_rung_decisions={
                "signed_membership": "eligible",
                "capability_eligibility": "revoked",
            },
            now_unix_ms=NOW_MS,
        )
    assert error.value.code == "prior_rung_not_current"


def test_operation_qualification_respects_the_artifact_free_probe_role() -> None:
    record = a12.author_operation_qualification(
        member_ref="member:1",
        role="probe_contributor",
        platform_family="android",
        runtime_build_digest="sha256:" + "1" * 64,
        prior_rung_decisions={
            "signed_membership": "eligible",
            "capability_eligibility": "eligible",
            "artifact_readiness": "not_applicable",
        },
        now_unix_ms=NOW_MS,
    )
    assert record["decision"] == "withheld"
    assert record["artifact_rung"] == {
        "rung_id": "artifact_readiness",
        "decision": "not_applicable",
        "reason": "artifact_free_probe_role",
    }


# ---------------------------------------------------------------------------
# Physical positives stay RED: the declared execution seams still refuse
# ---------------------------------------------------------------------------


def test_physical_and_projection_seams_still_refuse_with_their_coded_errors() -> None:
    with pytest.raises(a12.A12NotImplementedError) as operation:
        a12.qualify_mobile_operation(
            member_ref="member:1",
            role="qualified_model_stage",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
            prior_rung_decisions={
                "signed_membership": "eligible",
                "capability_eligibility": "eligible",
                "artifact_readiness": "eligible",
            },
        )
    assert operation.value.code == "not_implemented:operation_qualification_absent"

    with pytest.raises(a12.A12NotImplementedError) as gate:
        a12.run_mobile_physical_gate(platform_family="android", roles=("probe_contributor",))
    assert gate.value.code == "not_implemented:mobile_physical_gate_absent"
