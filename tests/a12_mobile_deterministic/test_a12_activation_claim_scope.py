# SPDX-License-Identifier: AGPL-3.0-only
"""The activation qualification record carries its claim scope explicitly.

[2026-09-27 OWNER RULING] ``two_android_family_proof`` is narrowed for the
initial build: the claim is family-bound to ``INITIAL_BUILD_ANDROID_FAMILY``.
The record-level validator previously called the family arithmetic without a
scope, so every record was a general claim.  The record now carries an optional
closed ``claim_scope``: absent or ``general`` keeps the two-family requirement;
``family_bound`` is Android-only and stands on exactly the initial-build family
reference.  No existing guard is relaxed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import mycelium_mobile.a12_activation as a12
import mycelium_mobile.a12_evidence as evidence

NOW_MS = 1_754_000_000_000.0
INVENTORY = (
    Path(__file__).resolve().parents[1] / "a12_acceptance" / "inventory.v1.json"
)
BOUND_REF = (
    "sha256:"
    + hashlib.sha256(
        "Google Pixel 8 Pro (husky), SDK 36, Android 16".encode("utf-8")
    ).hexdigest()
)
OTHER_REF = "family:" + "a" * 16
SECOND_REF = "family:" + "b" * 16


def _record(**overrides: object) -> dict[str, object]:
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
        "physical_family_refs": [OTHER_REF, SECOND_REF],
        "executed_positive_cases": 2,
        "executed_negative_cases": 3,
        "privacy_projection": {"platform_family": "android", "role_count": 1},
        "role_carryover_from": None,
        "issued_at_unix_ms": NOW_MS - 1_000.0,
        "expires_at_unix_ms": NOW_MS + 600_000.0,
    }
    record.update(overrides)
    return record


def _code(record: dict[str, object]) -> str:
    with pytest.raises(a12.A12ContractError) as error:
        evidence.validate_mobile_activation_qualification(record, now_unix_ms=NOW_MS)
    return error.value.code


def _ok(record: dict[str, object]) -> dict[str, object]:
    return evidence.validate_mobile_activation_qualification(record, now_unix_ms=NOW_MS)


def test_initial_build_reference_is_the_digest_of_the_named_family() -> None:
    assert evidence.INITIAL_BUILD_ANDROID_FAMILY_REF == BOUND_REF
    assert a12.INITIAL_BUILD_ANDROID_FAMILY == (
        "Google Pixel 8 Pro (husky), SDK 36, Android 16"
    )


def test_family_bound_record_on_the_initial_build_family_is_accepted() -> None:
    validated = _ok(_record(claim_scope="family_bound", physical_family_refs=[BOUND_REF]))
    assert validated["claim_scope"] == "family_bound"
    assert validated["physical_family_refs"] == [BOUND_REF]


def test_unscoped_and_general_records_still_require_two_families() -> None:
    # Absent scope is general: one family is refused, even the bound one.
    assert (
        _code(_record(physical_family_refs=[BOUND_REF]))
        == "android_general_claim_requires_two_families"
    )
    assert (
        _code(_record(claim_scope="general", physical_family_refs=[BOUND_REF]))
        == "android_general_claim_requires_two_families"
    )
    assert _ok(_record())["platform_family"] == "android"
    assert _ok(_record(claim_scope="general"))["claim_scope"] == "general"


@pytest.mark.parametrize(
    "refs",
    [
        [OTHER_REF],  # relabel: a different single family called family_bound
        ["sha256:" + "c" * 64],  # re-point: some other digest
        [BOUND_REF, SECOND_REF],  # a second family (Termux, simulator, phone) added
        [SECOND_REF, BOUND_REF],
    ],
)
def test_family_bound_refuses_any_other_family_set(refs: list[str]) -> None:
    assert (
        _code(_record(claim_scope="family_bound", physical_family_refs=refs))
        == "android_family_bound_claim_family_mismatch"
    )


def test_family_bound_keeps_reference_validation() -> None:
    assert (
        _code(_record(claim_scope="family_bound", physical_family_refs=[BOUND_REF] * 2))
        == "android_family_bound_claim_family_mismatch"
    )
    assert (
        _code(_record(claim_scope="family_bound", physical_family_refs=["pixel-8-pro"]))
        == "mobile_activation_family_reference_invalid"
    )
    assert (
        _code(_record(claim_scope="family_bound", physical_family_refs=[]))
        == "mobile_activation_family_reference_invalid"
    )


def test_family_bound_does_not_relax_the_physical_gate() -> None:
    bound = {"claim_scope": "family_bound", "physical_family_refs": [BOUND_REF]}
    assert (
        _code(_record(native_source_kind="termux_conformance", **bound))
        == "synthetic_conformance_cannot_satisfy_physical_gate"
    )
    assert _code(_record(executed_negative_cases=0, **bound)) == (
        "mobile_activation_cases_required"
    )
    assert _code(_record(device_brand="Pixel", **bound)) == (
        "mobile_activation_qualification_fields_invalid"
    )


@pytest.mark.parametrize("platform", ["ios", "ipados"])
def test_apple_records_are_refused_unless_general(platform: str) -> None:
    apple = {
        "platform_family": platform,
        "physical_family_refs": [BOUND_REF],
        "privacy_projection": {"platform_family": platform},
    }
    assert (
        _code(_record(claim_scope="family_bound", **apple))
        == "mobile_activation_claim_scope_invalid"
    )
    assert _ok(_record(claim_scope="general", **apple))["platform_family"] == platform
    assert _ok(_record(**apple))["platform_family"] == platform


@pytest.mark.parametrize(
    "scope",
    ["cross_vendor_swarm_scope", "FAMILY_BOUND", "", None, 1, ["family_bound"], {"a": 1}],
)
def test_claim_scope_is_a_closed_vocabulary(scope: object) -> None:
    assert (
        _code(_record(claim_scope=scope, physical_family_refs=[BOUND_REF]))
        == "mobile_activation_claim_scope_invalid"
    )


def test_generic_one_phone_claim_stays_forbidden_in_the_inventory() -> None:
    assert a12.MINIMUM_ANDROID_FAMILIES == 2
    assert a12.ANDROID_CLAIM_SCOPES == ("general", "family_bound")
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    (decision,) = [
        item
        for item in inventory["decisions"]
        if item["decision_id"] == "two_android_family_proof"
    ]
    assert {
        "one_phone_generic_android_claim",
        "termux_as_second_family",
        "simulator_as_second_family",
    } <= set(decision["forbidden_shortcuts"])
