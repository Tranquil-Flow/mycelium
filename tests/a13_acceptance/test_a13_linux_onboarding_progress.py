# SPDX-License-Identifier: AGPL-3.0-only
"""A13 ``mycelium.onboarding_progress.v1`` — privacy-reduced dynamic ladder.

The rung vocabulary is cross-checked against the frozen A13 acceptance
inventory (read-only): a Linux tranche may not invent its own ladder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_linux.progress import (
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


def _new(**overrides):
    values = {
        "member_generation": 3,
        "authority_provenance": "seed_authority_response",
        "now": NOW,
    }
    values.update(overrides)
    return new_progress(**values)


def _code(call):
    with pytest.raises(ProgressError) as caught:
        call()
    return caught.value.code


def _satisfy(document, *rungs):
    current = document
    for index, rung in enumerate(rungs):
        current = advance(current, rung=rung, state="satisfied", now=NOW + 1 + index)
    return current


def test_ladder_matches_the_frozen_a13_acceptance_inventory() -> None:
    document = json.loads(INVENTORY.read_text("utf-8"))
    assert list(LADDER) == list(document["onboarding_ladder"])
    assert len(LADDER) == 18


def test_new_progress_is_closed_and_starts_at_the_first_rung() -> None:
    document = _new()
    assert document["protocol"] == PROTOCOL
    assert set(document) == {
        "protocol",
        "member_generation",
        "authority_provenance",
        "rungs",
        "current_rung",
        "eligibility_granted",
        "route_ready",
        "updated_at",
    }
    assert [entry["rung"] for entry in document["rungs"]] == list(LADDER)
    assert {entry["state"] for entry in document["rungs"]} == {"not_started"}
    assert {entry["blocker"] for entry in document["rungs"]} == {None}
    assert document["current_rung"] == LADDER[0]
    assert document["eligibility_granted"] is False
    assert document["route_ready"] is False


def test_states_and_blockers_are_closed_vocabularies() -> None:
    assert set(STATES) == {"not_started", "in_progress", "blocked", "satisfied", "failed"}
    assert "package_verification_failed" in BLOCKERS
    assert "seed_pin_mismatch" in BLOCKERS
    assert all(blocker == blocker.lower() for blocker in BLOCKERS)


def test_member_generation_and_authority_provenance_are_validated() -> None:
    assert _code(lambda: _new(member_generation=0)) == "onboarding_member_generation_invalid"
    assert _code(lambda: _new(member_generation=True)) == "onboarding_member_generation_invalid"
    assert _code(lambda: _new(authority_provenance="seed_url")) == (
        "onboarding_authority_provenance_invalid"
    )


def test_rungs_advance_in_order_and_move_the_current_rung() -> None:
    document = _new()
    for index, rung in enumerate(LADDER[:6]):
        document = advance(document, rung=rung, state="satisfied", now=NOW + 1 + index)
    assert [entry["state"] for entry in document["rungs"]][:6] == ["satisfied"] * 6
    assert document["current_rung"] == LADDER[6]
    assert document["eligibility_granted"] is False
    assert document["route_ready"] is False


def test_skipping_a_rung_is_refused() -> None:
    document = _new()
    assert _code(
        lambda: advance(document, rung=LADDER[4], state="satisfied", now=NOW + 1)
    ) == "onboarding_rung_out_of_order"
    assert _code(
        lambda: advance(document, rung=LADDER[1], state="in_progress", now=NOW + 1)
    ) == "onboarding_rung_out_of_order"


def test_unknown_rung_and_state_are_refused() -> None:
    document = _new()
    assert _code(
        lambda: advance(document, rung="selection_claim", state="satisfied", now=NOW + 1)
    ) == "onboarding_rung_unknown"
    assert _code(
        lambda: advance(document, rung=LADDER[0], state="qualified", now=NOW + 1)
    ) == "onboarding_rung_state_invalid"


def test_blockers_are_bounded_and_only_attach_to_blocked_rungs() -> None:
    document = _new()
    assert _code(
        lambda: advance(document, rung=LADDER[0], state="blocked", blocker="whoops", now=NOW + 1)
    ) == "onboarding_blocker_invalid"
    assert _code(
        lambda: advance(
            document,
            rung=LADDER[0],
            state="satisfied",
            blocker="package_verification_failed",
            now=NOW + 1,
        )
    ) == "onboarding_blocker_invalid"


def test_a_blocked_rung_records_its_bounded_blocker_and_stays_current() -> None:
    document = advance(
        _new(),
        rung=LADDER[0],
        state="blocked",
        blocker="package_verification_failed",
        now=NOW + 1,
    )
    assert document["rungs"][0]["state"] == "blocked"
    assert document["rungs"][0]["blocker"] == "package_verification_failed"
    assert document["current_rung"] == LADDER[0]


def test_a_failure_is_immutable_and_blocks_every_later_rung() -> None:
    failed = advance(_new(), rung=LADDER[0], state="failed", now=NOW + 1)
    assert _code(
        lambda: advance(failed, rung=LADDER[0], state="satisfied", now=NOW + 2)
    ) == "onboarding_failure_immutable"
    assert _code(
        lambda: advance(failed, rung=LADDER[1], state="in_progress", now=NOW + 2)
    ) == "onboarding_rung_after_failure"


def test_a_satisfied_rung_cannot_regress() -> None:
    document = _satisfy(_new(), LADDER[0])
    assert _code(
        lambda: advance(document, rung=LADDER[0], state="in_progress", now=NOW + 5)
    ) == "onboarding_rung_regression"


def test_later_rungs_never_grant_eligibility_or_route_readiness() -> None:
    document = _satisfy(_new(), *LADDER)
    assert document["eligibility_granted"] is False
    assert document["route_ready"] is False
    assert document["current_rung"] == LADDER[-1]


def test_progress_document_is_closed_over_shape_and_protocol() -> None:
    document = _new()
    assert _code(lambda: advance({**document, "route_ready": True}, rung=LADDER[0], state="in_progress", now=NOW + 1)) == (
        "onboarding_progress_invalid"
    )
    assert _code(
        lambda: advance({**document, "protocol": "mycelium.onboarding_progress.v2"}, rung=LADDER[0], state="in_progress", now=NOW + 1)
    ) == "onboarding_progress_invalid"
    assert _code(
        lambda: advance({**document, "endpoint_id": "abc"}, rung=LADDER[0], state="in_progress", now=NOW + 1)
    ) == "onboarding_progress_invalid"


def test_time_never_moves_backwards() -> None:
    document = advance(_new(), rung=LADDER[0], state="in_progress", now=NOW + 10)
    assert _code(
        lambda: advance(document, rung=LADDER[0], state="satisfied", now=NOW + 1)
    ) == "onboarding_time_invalid"


def test_public_projection_is_privacy_reduced_and_drops_local_state() -> None:
    document = _satisfy(_new(), LADDER[0], LADDER[1])
    document = advance(
        document, rung=LADDER[2], state="blocked", blocker="owner_authorization_pending", now=NOW + 9
    )
    projection = public_projection(document)
    assert set(projection) == {
        "protocol",
        "member_generation",
        "current_rung",
        "rungs",
        "eligibility_granted",
        "route_ready",
    }
    assert set(projection["rungs"][0]) == {"rung", "state", "blocker"}
    assert projection["current_rung"] == LADDER[2]
    assert projection["rungs"][2]["blocker"] == "owner_authorization_pending"
    rendered = json.dumps(projection).lower()
    for forbidden in ("seed_url", "endpoint", "identity.key", "127.0.0.1", "token", "/users/", "ssh"):
        assert forbidden not in rendered, forbidden
