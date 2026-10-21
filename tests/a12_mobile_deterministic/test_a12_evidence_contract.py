# SPDX-License-Identifier: AGPL-3.0-only
"""A12 shared mobile evidence contract suite: RED-first, deterministic.

This suite pins the second half of the deterministic A12 lane: the shared mobile
evidence contract of
``docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md``
section 5, specializing the A9 separated-authority invariants that the frozen A9
acceptance inventory names:

- ``profile_signed_by_member`` and ``report_binds_generation_and_expiry``
  (decision ``signed_capability_binding``);
- ``source_kind_per_capability_family`` and ``native_gate_requires_native_evidence``
  (decision ``native_synthetic_evidence_separation``);
- ``unknown_visible_with_blocker`` and ``missing_observation_not_zero``
  (decision ``unknown_remains_ineligible``).

It also pins the four evidence eligibility rungs the lane authors deterministically —
registered-with-blocker, declared, synthetic-conformance, native-observed — where a
higher rung is never promoted to a lower one's claim and synthetic evidence is never
promoted to native.

What it cannot claim: no device executes anything here.  Every level record carries
``physical_execution`` false, and native-observed remains the only level that may
close a native gate — the physical gates of specification section 11 stay RED.

Run (serial, no ``-n``):

    env -u PYTHONPATH python3.14 -m pytest tests/a12_mobile_deterministic -q
"""

from __future__ import annotations

import pytest

import mycelium_mobile.a12_activation as a12
import mycelium_mobile.a12_evidence as evidence
from mycelium_qualification.signing import generate_ed25519_signer

NOW_MS = 1_754_000_000_000.0


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
        "metrics": {"sustainable_memory_bytes": 1.5e9, "thermal_state": "unknown"},
    }


def _role_qualification(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "protocol": a12.ROLE_QUALIFICATION_PROTOCOL,
        "authority": a12.RUNG_AUTHORITIES["capability_eligibility"],
        "role": "probe_contributor",
        "platform_family": "android",
        "runtime_build_digest": "sha256:" + "1" * 64,
        "profile_digest": "sha256:" + "d" * 64,
        "profile_generation": 3,
        "membership_generation": 3,
        "evidence_generation": 4,
        "source_kind": a12.NATIVE_SOURCE_KIND,
        "issued_at_unix_ms": NOW_MS - 1_000.0,
        "expires_at_unix_ms": NOW_MS + 600_000.0,
        "positive_evidence_digests": ["sha256:" + "e" * 64],
        "negative_evidence_digests": ["sha256:" + "f" * 64],
        "constraints": {"max_concurrency": 1, "foreground_only": True},
        "capability_family_sources": {
            family: a12.NATIVE_SOURCE_KIND for family in a12.REQUIRED_GATE_FAMILIES
        },
        "blockers": [],
    }
    record.update(overrides)
    return record


def _activation_qualification(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "protocol": a12.ACTIVATION_QUALIFICATION_PROTOCOL,
        "platform_family": "android",
        "roles": ["probe_contributor"],
        "native_application_digest": "sha256:" + "1" * 64,
        "transport_digest": "sha256:" + "2" * 64,
        "runtime_digest": "sha256:" + "3" * 64,
        "native_source_kind": a12.NATIVE_SOURCE_KIND,
        "membership_generation": 3,
        "profile_generation": 3,
        "evidence_generation": 4,
        "physical_family_refs": ["family:" + "a" * 16, "family:" + "b" * 16],
        "executed_positive_cases": 2,
        "executed_negative_cases": 3,
        "privacy_projection": {"platform_family": "android", "role_count": 1},
        "role_carryover_from": None,
        "issued_at_unix_ms": NOW_MS - 1_000.0,
        "expires_at_unix_ms": NOW_MS + 600_000.0,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# The four evidence eligibility rungs (a)-(d)
# ---------------------------------------------------------------------------


def test_evidence_levels_are_the_four_closed_rungs() -> None:
    assert evidence.MOBILE_EVIDENCE_LEVELS == (
        "registered_with_blocker",
        "declared",
        "synthetic_conformance",
        "native_observed",
    )
    assert evidence.EVIDENCE_LEVEL_PROTOCOL == "mycelium.mobile_evidence_level.v1"


def test_registered_with_blocker_keeps_an_unknown_platform_ineligible() -> None:
    record = evidence.author_registered_with_blocker_level(
        platform_family="unknown", role="probe_contributor"
    )

    assert record["level"] == "registered_with_blocker"
    assert record["decision"] == "ineligible"
    assert record["blockers"] == ["platform_family_unknown", "native_observation_absent"]
    assert record["source_kind"] == "unknown"
    assert record["native_evidence_required"] is True
    assert record["physical_execution"] is False
    assert record["metrics"] == {}


def test_registered_with_blocker_keeps_a_registered_platform_ineligible_until_observed() -> None:
    record = evidence.author_registered_with_blocker_level(
        platform_family="android", role="probe_contributor"
    )
    assert record["decision"] == "ineligible"
    assert record["blockers"] == ["native_observation_absent"]
    assert record["platform_family"] == "android"


def test_unregistered_platform_values_are_rejected_not_converted() -> None:
    with pytest.raises(a12.A12ContractError) as error:
        evidence.author_registered_with_blocker_level(
            platform_family="windows", role="probe_contributor"
        )
    assert error.value.code == "mobile_evidence_platform_invalid"


def test_declared_level_is_not_observation_and_cannot_qualify() -> None:
    record = evidence.author_declared_level(
        platform_family="ios",
        role="probe_contributor",
        declaration={"member_ref": "member:1", "declared_runtime": "mlx"},
    )

    assert record["level"] == "declared"
    assert record["decision"] == "ineligible"
    assert record["source_kind"] == "operator_declared"
    assert record["blockers"] == ["declaration_is_not_observation", "native_observation_absent"]
    assert record["physical_execution"] is False


def test_synthetic_conformance_stays_labelled_and_never_becomes_native() -> None:
    record = evidence.author_synthetic_conformance_level(
        platform_family="android",
        role="probe_contributor",
        observation=_observation("synthetic_conformance"),
    )

    assert record["level"] == "synthetic_conformance"
    assert record["decision"] == "ineligible"
    assert record["source_kind"] == "synthetic_conformance"
    assert record["blockers"] == ["synthetic_conformance_cannot_qualify_native"]
    assert record["cannot_qualify_native_rung"] is True
    assert record["physical_execution"] is False


def test_native_observed_is_the_only_level_that_may_close_a_native_rung() -> None:
    record = evidence.author_native_observed_level(
        platform_family="android",
        role="probe_contributor",
        observation=_observation(),
    )

    assert record["level"] == "native_observed"
    assert record["decision"] == "eligible"
    assert record["source_kind"] == a12.NATIVE_SOURCE_KIND
    assert record["blockers"] == []
    # The level is earned by observation, not by execution in this process.
    assert record["physical_execution"] is False
    assert record["metrics"]["thermal_state"] == "unknown"


def test_the_ladder_dispatches_on_the_evidence_it_is_given() -> None:
    levels = (
        evidence.evaluate_evidence_level(
            platform_family="android", role="probe_contributor"
        ),
        evidence.evaluate_evidence_level(
            platform_family="android",
            role="probe_contributor",
            declaration={"member_ref": "member:1"},
        ),
        evidence.evaluate_evidence_level(
            platform_family="android",
            role="probe_contributor",
            observation=_observation("termux_conformance"),
        ),
        evidence.evaluate_evidence_level(
            platform_family="android", role="probe_contributor", observation=_observation()
        ),
    )

    assert tuple(record["level"] for record in levels) == evidence.MOBILE_EVIDENCE_LEVELS
    assert tuple(record["decision"] for record in levels) == (
        "ineligible",
        "ineligible",
        "ineligible",
        "eligible",
    )
    assert all(record["physical_execution"] is False for record in levels)


def test_each_evidence_level_refuses_the_other_levels_source_kind() -> None:
    """A level is authored from its own source kind, never promoted from another."""

    with pytest.raises(a12.A12ContractError) as native_from_synthetic:
        evidence.author_native_observed_level(
            platform_family="android",
            role="probe_contributor",
            observation=_observation("synthetic_conformance"),
        )
    assert native_from_synthetic.value.code == "non_native_source_cannot_qualify"

    with pytest.raises(a12.A12ContractError) as synthetic_from_native:
        evidence.author_synthetic_conformance_level(
            platform_family="android",
            role="probe_contributor",
            observation=_observation(),
        )
    assert synthetic_from_native.value.code == "evidence_level_requires_its_own_source_kind"


def test_only_the_native_level_may_claim_native_closure() -> None:
    lower = (
        evidence.author_registered_with_blocker_level(
            platform_family="android", role="probe_contributor"
        ),
        evidence.author_declared_level(
            platform_family="android",
            role="probe_contributor",
            declaration={"member_ref": "member:1"},
        ),
        evidence.author_synthetic_conformance_level(
            platform_family="android",
            role="probe_contributor",
            observation=_observation("synthetic_conformance"),
        ),
    )
    native = evidence.author_native_observed_level(
        platform_family="android", role="probe_contributor", observation=_observation()
    )

    assert [record["cannot_qualify_native_rung"] for record in lower] == [True, True, True]
    assert native["cannot_qualify_native_rung"] is False
    # No level record carries a grant, so none can author another level's claim.
    assert all(record["grants"] == [] for record in (*lower, native))


# ---------------------------------------------------------------------------
# source_kind per capability family (A9 native_synthetic_evidence_separation)
# ---------------------------------------------------------------------------


def test_every_capability_family_carries_its_own_source_kind() -> None:
    sources = {family: a12.NATIVE_SOURCE_KIND for family in a12.REQUIRED_GATE_FAMILIES}

    assert evidence.require_source_kind_per_capability_family(sources) == sources


def test_a_missing_capability_family_source_is_never_inherited() -> None:
    sources = {family: a12.NATIVE_SOURCE_KIND for family in a12.REQUIRED_GATE_FAMILIES}
    del sources["thermal"]

    with pytest.raises(a12.A12ContractError) as error:
        evidence.require_source_kind_per_capability_family(sources)
    assert error.value.code == "capability_family_source_kind_missing"

    with pytest.raises(a12.A12ContractError) as unregistered:
        evidence.require_source_kind_per_capability_family(
            {**sources, "brand_thermal": a12.NATIVE_SOURCE_KIND}
        )
    assert unregistered.value.code == "capability_family_unregistered"


def test_an_unknown_family_source_is_visible_with_a_blocker_not_zero() -> None:
    sources = {family: a12.NATIVE_SOURCE_KIND for family in a12.REQUIRED_GATE_FAMILIES}
    sources["thermal"] = "unknown"

    assert evidence.require_source_kind_per_capability_family(sources)["thermal"] == "unknown"
    assert evidence.capability_family_blockers(sources) == {
        "thermal": "capability_family_observation_unknown"
    }
    assert evidence.capability_family_blockers(
        {family: a12.NATIVE_SOURCE_KIND for family in a12.REQUIRED_GATE_FAMILIES}
    ) == {}


# ---------------------------------------------------------------------------
# signed capability binding (A9 signed_capability_binding)
# ---------------------------------------------------------------------------


def test_capability_report_must_be_signed_by_the_member() -> None:
    member = generate_ed25519_signer(endpoint_id="member-endpoint-1")
    statement = {"protocol": a12.CAPABILITY_REPORT_PROTOCOL, "member_ref": "member:1"}

    envelope = evidence.sign_member_report(signer=member, statement=statement)
    assert evidence.verify_member_signed_report(
        envelope, now_unix_ms=NOW_MS, expected_member_key_digest=member.verification_key_digest
    ) == statement

    foreign = generate_ed25519_signer(endpoint_id="member-endpoint-1")
    forged = {
        "protocol": envelope["protocol"],
        "message": envelope["message"],
        "signature": foreign.sign(envelope["message"]),
        "verification_key": member.public_key_record(),
    }
    with pytest.raises(a12.A12ContractError) as error:
        evidence.verify_member_signed_report(
            forged, now_unix_ms=NOW_MS, expected_member_key_digest=member.verification_key_digest
        )
    assert error.value.code == "member_profile_signature_invalid"

    unsigned = {"protocol": envelope["protocol"], "message": envelope["message"]}
    with pytest.raises(a12.A12ContractError) as unsigned_error:
        evidence.verify_member_signed_report(unsigned, now_unix_ms=NOW_MS)
    assert unsigned_error.value.code == "member_report_envelope_invalid"


# ---------------------------------------------------------------------------
# the two capability-named records this lane validates
# ---------------------------------------------------------------------------


def test_role_qualification_record_binds_generation_and_expiry() -> None:
    assert evidence.validate_mobile_role_qualification(
        _role_qualification(), now_unix_ms=NOW_MS
    )["role"] == "probe_contributor"

    stale = _role_qualification(expires_at_unix_ms=NOW_MS - 1.0)
    with pytest.raises(a12.A12ContractError) as expired:
        evidence.validate_mobile_role_qualification(stale, now_unix_ms=NOW_MS)
    assert expired.value.code == "mobile_role_qualification_expired"

    mixed = _role_qualification(profile_generation=2)
    with pytest.raises(a12.A12ContractError) as generation:
        evidence.validate_mobile_role_qualification(mixed, now_unix_ms=NOW_MS)
    assert generation.value.code == "mobile_role_qualification_generation_invalid"

    bad_digest = _role_qualification(profile_digest="sha256:short")
    with pytest.raises(a12.A12ContractError) as digest:
        evidence.validate_mobile_role_qualification(bad_digest, now_unix_ms=NOW_MS)
    assert digest.value.code == "mobile_role_qualification_profile_binding_invalid"


def test_role_qualification_requires_native_evidence_for_every_family() -> None:
    synthetic = _role_qualification(source_kind="synthetic_conformance")
    with pytest.raises(a12.A12ContractError) as error:
        evidence.validate_mobile_role_qualification(synthetic, now_unix_ms=NOW_MS)
    assert error.value.code == "non_native_source_cannot_qualify"

    inherited = _role_qualification(
        capability_family_sources={
            family: a12.NATIVE_SOURCE_KIND
            for family in a12.REQUIRED_GATE_FAMILIES
            if family != "parity"
        }
    )
    with pytest.raises(a12.A12ContractError) as missing:
        evidence.validate_mobile_role_qualification(inherited, now_unix_ms=NOW_MS)
    assert missing.value.code == "capability_family_source_kind_missing"


def test_role_qualification_record_is_closed() -> None:
    unknown_field = {**_role_qualification(), "device_brand": "Pixel"}
    with pytest.raises(a12.A12ContractError) as error:
        evidence.validate_mobile_role_qualification(unknown_field, now_unix_ms=NOW_MS)
    assert error.value.code == "mobile_role_qualification_fields_invalid"

    missing_negative = _role_qualification(negative_evidence_digests=[])
    with pytest.raises(a12.A12ContractError) as no_negative:
        evidence.validate_mobile_role_qualification(missing_negative, now_unix_ms=NOW_MS)
    assert no_negative.value.code == "mobile_role_qualification_evidence_digest_invalid"

    wrong_authority = _role_qualification(authority="a2_provisioner")
    with pytest.raises(a12.A12ContractError) as authority:
        evidence.validate_mobile_role_qualification(wrong_authority, now_unix_ms=NOW_MS)
    assert authority.value.code == "mobile_role_qualification_authority_invalid"


def test_activation_qualification_requires_executed_physical_cases_and_private_refs() -> None:
    assert evidence.validate_mobile_activation_qualification(
        _activation_qualification(), now_unix_ms=NOW_MS
    )["platform_family"] == "android"

    with pytest.raises(a12.A12ContractError) as synthetic:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(native_source_kind="synthetic_conformance"),
            now_unix_ms=NOW_MS,
        )
    assert synthetic.value.code == "synthetic_conformance_cannot_satisfy_physical_gate"

    with pytest.raises(a12.A12ContractError) as unexecuted:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(executed_positive_cases=0), now_unix_ms=NOW_MS
        )
    assert unexecuted.value.code == "mobile_activation_cases_required"

    with pytest.raises(a12.A12ContractError) as one_family:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(physical_family_refs=["family:" + "a" * 16]),
            now_unix_ms=NOW_MS,
        )
    assert one_family.value.code == "android_general_claim_requires_two_families"


def test_activation_qualification_rejects_private_material_and_brand_text() -> None:
    with pytest.raises(a12.A12ContractError) as private:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(
                privacy_projection={"platform_family": "android", "endpoint_id": "abc"}
            ),
            now_unix_ms=NOW_MS,
        )
    assert private.value.code == "mobile_activation_private_material_present"

    with pytest.raises(a12.A12ContractError) as nested_private:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(
                privacy_projection={"platform_family": "android", "device": {"ssid": "home"}}
            ),
            now_unix_ms=NOW_MS,
        )
    assert nested_private.value.code == "mobile_activation_private_material_present"

    unknown_field = {**_activation_qualification(), "device_brand": "Pixel"}
    with pytest.raises(a12.A12ContractError) as brand:
        evidence.validate_mobile_activation_qualification(unknown_field, now_unix_ms=NOW_MS)
    assert brand.value.code == "mobile_activation_qualification_fields_invalid"


def test_apple_platform_isolation_is_enforced_on_the_activation_record() -> None:
    record = _activation_qualification(
        platform_family="ipados",
        roles=["probe_contributor"],
        physical_family_refs=["family:" + "a" * 16],
    )
    assert evidence.validate_mobile_activation_qualification(
        record, now_unix_ms=NOW_MS
    )["platform_family"] == "ipados"

    with pytest.raises(a12.A12ContractError) as carryover:
        evidence.validate_mobile_activation_qualification(
            _activation_qualification(platform_family="ios", role_carryover_from="ipados"),
            now_unix_ms=NOW_MS,
        )
    assert carryover.value.code == "apple_platform_carryover_forbidden"
