# SPDX-License-Identifier: AGPL-3.0-only
"""A12 regression: the 2026-09-27 family-bound claim narrowing keeps the guard.

[2026-09-27 OWNER RULING] ``two_android_family_proof`` NARROWED FOR THE INITIAL
BUILD, not deleted (``status/decisions.log``; packet
``a12-claim-narrowing-single-family-20260927``).  The initial-build claim is bound
to one named family (Google Pixel 8 Pro, ``husky``, SDK 36, Android 16), so a
family-bound claim may stand on one family — while a *generic* Android claim on
that same single family is still refused with
``android_general_claim_requires_two_families``, and an unqualified (default)
call is treated as a generic claim.  This module is the regression that keeps the
generic-claim guard in force after the narrowing; ``one_phone_generic_android_claim``
remains a forbidden shortcut in ``tests/a12_acceptance/inventory.v1.json``.
"""

from __future__ import annotations

import pytest

import mycelium_mobile.a12_activation as a12

ONE_FAMILY = "family:" + "a" * 16
SECOND_FAMILY = "sha256:" + "b" * 64
TWO_FAMILIES = (ONE_FAMILY, SECOND_FAMILY)
REFUSAL = "android_general_claim_requires_two_families"


def _refusal(family_refs: object, *, claim_scope: object) -> str:
    with pytest.raises(a12.A12ContractError) as error:
        a12.require_android_family_evidence(family_refs, claim_scope=claim_scope)
    return error.value.code


def test_android_general_claim_requires_two_families() -> None:
    """A generic Android claim backed by ONE family is still refused.

    [2026-09-27 OWNER RULING] the narrowing did not remove the two-family
    requirement for a general/cross-vendor Android claim: one family is refused
    with ``android_general_claim_requires_two_families``, and the unqualified
    default call is a general claim, so it refuses the same way.
    """

    assert (
        _refusal((ONE_FAMILY,), claim_scope=a12.ANDROID_GENERAL_CLAIM_SCOPE) == REFUSAL
    )

    with pytest.raises(a12.A12ContractError) as unqualified:
        a12.require_android_family_evidence((ONE_FAMILY,))
    assert unqualified.value.code == REFUSAL

    assert a12.require_android_family_evidence(TWO_FAMILIES) == TWO_FAMILIES


def test_family_bound_claim_on_one_family_is_accepted() -> None:
    """The narrowed initial-build claim may stand on the one bound family.

    [2026-09-27 OWNER RULING] ``two_android_family_proof`` is narrowed for the
    initial build: the claim is family-bound to one named family, so the
    family-bound scope accepts a single family reference.  This is a claim
    narrowing, not a guard deletion — the generic scope above still refuses it.
    """

    assert a12.require_android_family_evidence(
        (ONE_FAMILY,), claim_scope=a12.ANDROID_FAMILY_BOUND_CLAIM_SCOPE
    ) == (ONE_FAMILY,)
    assert (
        a12.INITIAL_BUILD_ANDROID_FAMILY
        == "Google Pixel 8 Pro (husky), SDK 36, Android 16"
    )


def test_family_bound_scope_does_not_relax_reference_validation() -> None:
    """Only the family count is relaxed for a family-bound claim."""

    for family_refs, code in (
        ((), "android_family_evidence_invalid"),
        (["pixel-8-pro"], "android_family_reference_invalid"),
        ((ONE_FAMILY, ONE_FAMILY), "android_family_evidence_duplicated"),
    ):
        assert (
            _refusal(family_refs, claim_scope=a12.ANDROID_FAMILY_BOUND_CLAIM_SCOPE)
            == code
        )


def test_an_unknown_claim_scope_is_refused() -> None:
    """No caller may invent a scope that skips the family arithmetic."""

    assert (
        _refusal((ONE_FAMILY,), claim_scope="cross_vendor_swarm_scope")
        == "android_claim_scope_invalid"
    )
    assert _refusal(TWO_FAMILIES, claim_scope=None) == "android_claim_scope_invalid"


def test_deferred_two_family_obligation_names_its_trigger_and_the_ruling() -> None:
    """[2026-09-27 OWNER RULING] the deferral stays tracked with its trigger named."""

    assert a12.MINIMUM_ANDROID_FAMILIES == 2
    assert a12.ANDROID_CLAIM_SCOPES == ("general", "family_bound")
    assert a12.TWO_FAMILY_REQUIREMENT_REACTIVATION_TRIGGER == (
        "before_any_general_or_cross_vendor_android_claim_including_public_or_swarm_scope"
    )
