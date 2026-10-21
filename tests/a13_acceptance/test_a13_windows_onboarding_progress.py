# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows onboarding ladder — the privacy-reduced progress contract.

The Windows tranche must not fork the frozen ladder: it reuses the landed
platform-neutral ``mycelium.onboarding_progress.v1`` rung set, and adds nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_linux import progress as linux_progress
from mycelium_a13_windows import progress as windows_progress
from mycelium_a13_windows.progress import (
    AUTHORITY_PROVENANCE,
    BLOCKERS,
    LADDER,
    PROTOCOL,
    STATES,
    ProgressError,
    advance,
    new_progress,
    public_projection,
)

INVENTORY = Path(__file__).with_name("inventory.v1.json")
NOW = 2_000_000_000.0


def _progress(**overrides):
    arguments = {
        "member_generation": 1,
        "authority_provenance": "local_join",
        "now": NOW,
    }
    arguments.update(overrides)
    return new_progress(**arguments)


def _code(call):
    with pytest.raises(ProgressError) as caught:
        call()
    return caught.value.code


def test_windows_reuses_the_frozen_ladder_without_adding_a_rung() -> None:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    assert list(LADDER) == list(inventory["onboarding_ladder"])
    assert list(LADDER) == list(linux_progress.LADDER)
    assert PROTOCOL == linux_progress.PROTOCOL == "mycelium.onboarding_progress.v1"
    assert windows_progress.advance is linux_progress.advance
    assert windows_progress.new_progress is linux_progress.new_progress
    assert windows_progress.public_projection is linux_progress.public_projection
    assert BLOCKERS == linux_progress.BLOCKERS
    assert AUTHORITY_PROVENANCE == linux_progress.AUTHORITY_PROVENANCE
    assert STATES == linux_progress.STATES


def test_a_fresh_ladder_claims_nothing() -> None:
    document = _progress()
    assert document["current_rung"] == LADDER[0]
    assert document["eligibility_granted"] is False
    assert document["route_ready"] is False
    assert all(entry["state"] == "not_started" for entry in document["rungs"])


def test_rungs_advance_in_order_and_cannot_shortcut() -> None:
    document = _progress()
    assert (
        _code(
            lambda: advance(document, rung=LADDER[3], state="satisfied", now=NOW + 1)
        )
        == "onboarding_rung_out_of_order"
    )
    stepped = document
    for index, rung in enumerate(LADDER[:4]):
        stepped = advance(stepped, rung=rung, state="satisfied", now=NOW + index + 1)
    assert stepped["current_rung"] == LADDER[4]
    assert stepped["eligibility_granted"] is False
    assert stepped["route_ready"] is False
    assert _code(
        lambda: advance(
            stepped,
            rung=LADDER[0],
            state="blocked",
            blocker="storage_low",
            now=NOW + 9,
        )
    ) == "onboarding_rung_regression"
    assert _code(lambda: advance(stepped, rung="not_a_rung", state="satisfied", now=NOW + 9)) == (
        "onboarding_rung_unknown"
    )


def test_a_failure_is_immutable_and_blocks_the_rest_of_the_ladder() -> None:
    stepped = _progress()
    for index, rung in enumerate(LADDER[:2]):
        stepped = advance(stepped, rung=rung, state="satisfied", now=NOW + index + 1)
    failed = advance(
        stepped,
        rung=LADDER[2],
        state="failed",
        now=NOW + 5,
    )
    assert failed["rungs"][2]["state"] == "failed"
    assert _code(lambda: advance(failed, rung=LADDER[2], state="satisfied", now=NOW + 6)) == (
        "onboarding_failure_immutable"
    )
    assert _code(lambda: advance(failed, rung=LADDER[3], state="satisfied", now=NOW + 6)) == (
        "onboarding_rung_after_failure"
    )


def test_blockers_are_bounded_and_only_on_blocked_rungs() -> None:
    document = _progress()
    blocked = advance(
        document,
        rung=LADDER[0],
        state="blocked",
        blocker="package_verification_failed",
        now=NOW + 1,
    )
    assert blocked["rungs"][0]["blocker"] == "package_verification_failed"
    assert _code(
        lambda: advance(
            document, rung=LADDER[0], state="blocked", blocker="something_else", now=NOW + 1
        )
    ) == "onboarding_blocker_invalid"
    assert _code(
        lambda: advance(
            document, rung=LADDER[0], state="satisfied", blocker="storage_low", now=NOW + 1
        )
    ) == "onboarding_blocker_invalid"


def test_public_projection_carries_no_identity_path_or_endpoint() -> None:
    document = advance(
        _progress(),
        rung=LADDER[0],
        state="blocked",
        blocker="package_verification_failed",
        now=NOW + 1,
    )
    projection = public_projection(document)
    assert projection["protocol"] == PROTOCOL
    assert projection["current_rung"] == LADDER[0]
    assert projection["eligibility_granted"] is False
    assert projection["route_ready"] is False
    rendered = json.dumps(projection).lower()
    for forbidden in (
        "node_id",
        "endpoint",
        "/users/",
        "seed_key_digest",
        "judge-swarm",
        "nonce",
        "sha256:",
        "ciphertext",
        "private_key",
    ):
        assert forbidden not in rendered
    assert set(projection) == {
        "protocol",
        "member_generation",
        "current_rung",
        "rungs",
        "eligibility_granted",
        "route_ready",
    }


def test_time_and_generation_bounds_are_enforced() -> None:
    assert _code(lambda: _progress(member_generation=0)) == (
        "onboarding_member_generation_invalid"
    )
    assert _code(lambda: _progress(now="now")) == "onboarding_time_invalid"
    assert _code(lambda: _progress(authority_provenance="operator")) == (
        "onboarding_authority_provenance_invalid"
    )
    document = _progress()
    assert _code(
        lambda: advance(document, rung=LADDER[0], state="satisfied", now=NOW - 1)
    ) == "onboarding_time_invalid"
    assert _code(lambda: advance(document, rung=LADDER[0], state="done", now=NOW + 1)) == (
        "onboarding_rung_state_invalid"
    )
