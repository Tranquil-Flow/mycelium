# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first tests for the A11 deterministic verifier transaction half.

TDD lane (v2/m1-a11-verifier). These tests were written before the state machine
existed; they pin the *deterministic* half of A11's runtime cycle semantics
(A11 specification section 5) on a pure, in-process model:

* one bounded target call evaluates every bounded position causally, and every
  per-position decision is compared with sequential target execution;
* the three frozen transaction modes -- ``rollback_to_prefix``,
  ``commit_verified_prefix``, ``commit_verified_prefix_plus_target_correction``;
* commit is atomic, strictly monotonic and idempotent for an identical cycle
  digest; a conflicting duplicate, a stale prefix or a parity failure fails
  closed without advancing the target watermark;
* the qualified generation policy is exact, and an unsupported policy fails
  admission instead of being silently coerced to greedy.

CLAIM BOUNDARY: the target here is an injected deterministic decision source, not
the physically qualified A10 verifier and not a model runtime. Passing this module
proves deterministic transaction arithmetic and fail-closed behaviour only. It
proves no physical verification, no proposal transfer, no benchmark, no
qualification, and no enabled or measured-disabled decision.
"""

from __future__ import annotations

import pytest

import mycelium_speculative_decode as sd
from tests.a11_scaffold.test_a11_contract_scaffold import _base_plan, _enabled_plan

PREFIX_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
CYCLE = "cycle-1"
IDEMPOTENCY = "sha256:" + "c" * 64

# Closed plans: the disabled compatibility plan (no authorized draft) and the
# qualified-enabled plan from the frozen contract scaffold.
DISABLED_PLAN = sd.seal(_base_plan())
ENABLED_PLAN = _enabled_plan()

# Candidate token ids chosen far away from any record arithmetic so a leaked raw
# token is unmistakable.
CANDIDATES = (900_001, 900_002, 900_003, 900_004)


class ContractTarget:
    """Deterministic, causal target-decision double.

    ``decide(prefix_digest, accepted_tokens)`` is the target's own greedy token at
    the next position given the exact committed prefix and the tokens already
    accepted at that position. Position ``i`` of the bounded call therefore never
    reads ``candidates[i]`` -- that is what makes the call causal.
    """

    def __init__(self, decide) -> None:
        self.decide = decide
        self.bounded_calls = 0
        self.sequential_calls = 0
        self.bounded_widths: list[int] = []

    def _decision(self, prefix_digest: str, accepted: tuple[int, ...]) -> int:
        return int(self.decide(prefix_digest, accepted))

    def evaluate_bounded_positions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
        self.bounded_calls += 1
        self.bounded_widths.append(len(candidates))
        return tuple(self._decision(prefix_digest, tuple(candidates[:index])) for index in range(len(candidates)))

    def sequential_decisions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
        """The declared reference oracle: sequential target execution on this prefix."""

        self.sequential_calls += len(candidates)
        return tuple(
            self._decision(prefix_digest, tuple(candidates[:index])) for index in range(len(candidates))
        )


def matching_target(*, correction: int | None = None) -> ContractTarget:
    """A causal target that agrees with ``CANDIDATES`` except at ``correction``."""

    def decide(_prefix_digest: str, accepted: tuple[int, ...]) -> int:
        index = len(accepted)
        if correction is not None and index == correction:
            return 800_000 + index
        return CANDIDATES[index]

    return ContractTarget(decide)


def non_causal_target() -> ContractTarget:
    """A cheating batched shortcut: every position answers ``candidates[-1]``."""

    class NonCausal(ContractTarget):
        def evaluate_bounded_positions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
            self.bounded_calls += 1
            self.bounded_widths.append(len(candidates))
            return tuple(candidates[-1] for _ in range(len(candidates)))

    return NonCausal(lambda _prefix_digest, _accepted: 0)


def open_transaction(plan: dict | None = None, *, width: int = 4, deadline_ms: float | None = 60.0) -> dict:
    return sd.open_target_verification_transaction(
        plan=DISABLED_PLAN if plan is None else plan,
        width=width,
        deadline_ms=deadline_ms,
        cycle_ref=CYCLE,
        committed_prefix_position=0,
        committed_prefix_digest=PREFIX_DIGEST,
        idempotency_digest=IDEMPOTENCY,
    )


def evaluated(*, correction: int | None = None) -> tuple[dict, ContractTarget]:
    target = matching_target(correction=correction)
    transaction = sd.verify_transaction_positions(
        transaction=open_transaction(), candidate_tokens=CANDIDATES, target=target
    )
    return transaction, target


def commit_transaction(transaction: dict, *, ledger: "sd.TargetPrefixLedger | None" = None) -> dict:
    return sd.commit_verified_prefix(transaction=transaction, ledger=ledger)


# ---------------------------------------------------------------------------
# 1. Opening a transaction: exact prefix, frozen deadline, exact policy
# ---------------------------------------------------------------------------


def test_transaction_open_returns_a_record_at_the_exact_committed_prefix() -> None:
    transaction = open_transaction()

    assert transaction["commit_state"] == "open"
    assert transaction["proposal_width"] == 4
    assert transaction["accepted_prefix_length"] == 0
    assert transaction["accepted_prefix_length"] <= transaction["proposal_width"]
    assert transaction["verified_count"] == 0
    assert transaction["committed_position_count"] == 0
    assert transaction["target_prefix_position"] == 0
    assert transaction["target_prefix_digest"] == PREFIX_DIGEST
    assert transaction["transaction_mode"] is None
    assert transaction["parity"] == "not_checked"
    assert transaction["cycle_ref"] == CYCLE
    assert transaction["generation_policy"] == "deterministic_greedy"


def test_transaction_open_clamps_the_deadline_with_the_frozen_formula() -> None:
    assert open_transaction(deadline_ms=60.0)["deadline_ms"] == pytest.approx(60.0)
    assert open_transaction(deadline_ms=60.0)["deadline_clamp_reason"] == "within_bounds"
    assert open_transaction(deadline_ms=4.0)["deadline_ms"] == pytest.approx(5.0)
    assert open_transaction(deadline_ms=4.0)["deadline_clamp_reason"] == "floor_applied"
    assert open_transaction(deadline_ms=200.0)["deadline_ms"] == pytest.approx(100.0)
    assert open_transaction(deadline_ms=200.0)["deadline_clamp_reason"] == "ceiling_applied"

    tightened = _base_plan()
    tightened["deadline_policy"]["remaining_request_deadline_ms"] = 3.0
    clamped = open_transaction(sd.seal(tightened), deadline_ms=60.0)
    assert clamped["deadline_ms"] == pytest.approx(3.0)
    assert clamped["deadline_clamp_reason"] == "request_deadline_clamp"


def test_transaction_open_without_a_deadline_uses_the_plan_measurement() -> None:
    measured = _base_plan()
    measured["deadline_policy"]["warm_target_only_p95_tpot_ms"] = 80.0
    transaction = open_transaction(sd.seal(measured), deadline_ms=None)

    assert transaction["deadline_ms"] == pytest.approx(60.0)
    assert transaction["deadline_clamp_reason"] == "within_bounds"

    unknown = open_transaction(deadline_ms=None)
    assert unknown["deadline_ms"] is None
    assert unknown["deadline_clamp_reason"] == sd.DEADLINE_UNKNOWN_REASON


def test_transaction_open_refuses_unsupported_sampling_and_never_coerces_to_greedy() -> None:
    sampling = _base_plan()
    sampling["compatibility"]["generation_policy"] = "unseeded_sampling"
    sampling["decision"]["reason"] = "incompatible_token_semantics"

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        open_transaction(sd.seal(sampling))
    assert error.value.code == "unsupported_generation_policy"

    assert sd.QUALIFIED_GENERATION_POLICIES == frozenset({"deterministic_greedy"})
    assert set(sd.TRANSACTION_MODES) == {
        "rollback_to_prefix",
        "commit_verified_prefix",
        "commit_verified_prefix_plus_target_correction",
    }


def test_transaction_open_refuses_a_width_beyond_the_plan_bound() -> None:
    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        open_transaction(width=5)
    assert error.value.code == "proposal_width_out_of_bounds"

    with pytest.raises(sd.SpeculativeDecodeContractError):
        open_transaction(width=0)


# ---------------------------------------------------------------------------
# 2. One bounded call, causal, parity-checked against sequential execution
# ---------------------------------------------------------------------------


def test_bounded_evaluation_uses_exactly_one_target_call() -> None:
    transaction, target = evaluated()

    assert target.bounded_calls == 1
    assert target.bounded_widths == [4]
    # The parity oracle is a separate reference execution: one decision per position.
    assert target.sequential_calls == 4
    assert transaction["evaluated_position_count"] == 4
    assert transaction["verified_count"] == 4


def test_all_positions_matching_commit_the_verified_prefix() -> None:
    transaction, _ = evaluated()

    assert transaction["accepted_prefix_length"] == 4
    assert transaction["rejected_count"] == 0
    assert transaction["target_corrected_count"] == 0
    assert transaction["rollback_count"] == 0
    assert transaction["transaction_mode"] == "commit_verified_prefix"
    assert transaction["commit_state"] == "verified_prefix_ready"
    assert transaction["parity"] == "matched"


@pytest.mark.parametrize(
    "mismatch,accepted,rejected",
    [(0, 0, 4), (1, 1, 3), (3, 3, 1)],
)
def test_first_interior_and_final_mismatch_commit_prefix_plus_one_correction(
    mismatch: int, accepted: int, rejected: int
) -> None:
    transaction, _ = evaluated(correction=mismatch)

    assert transaction["accepted_prefix_length"] == accepted
    assert transaction["rejected_count"] == rejected
    assert transaction["target_corrected_count"] == 1
    assert transaction["rollback_count"] == 1
    assert transaction["transaction_mode"] == "commit_verified_prefix_plus_target_correction"
    assert transaction["commit_state"] == "target_correction_ready"
    # The correction position is drawn from the same single bounded call.
    assert transaction["accepted_prefix_length"] + transaction["target_corrected_count"] <= 4


def test_candidate_positions_must_equal_the_bounded_width() -> None:
    transaction = open_transaction()

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.verify_transaction_positions(
            transaction=transaction, candidate_tokens=CANDIDATES[:3], target=matching_target()
        )
    assert error.value.code == "proposal_width_mismatch"


def test_parity_oracle_compares_every_position_with_sequential_execution() -> None:
    target = matching_target()
    decisions = target.evaluate_bounded_positions(prefix_digest=PREFIX_DIGEST, candidates=CANDIDATES)
    parity = sd.check_prefix_parity(
        target=target, prefix_digest=PREFIX_DIGEST, candidates=CANDIDATES, decisions=decisions
    )

    assert parity["parity"] == "matched"
    assert parity["positions_compared"] == 4
    assert parity["first_divergence"] is None


def test_a_non_causal_target_fails_parity_and_rolls_back_to_the_exact_prefix() -> None:
    transaction = sd.verify_transaction_positions(
        transaction=open_transaction(), candidate_tokens=CANDIDATES, target=non_causal_target()
    )

    assert transaction["parity"] == "failed"
    assert transaction["failure_reason"] == "parity_failed"
    assert transaction["commit_state"] == "rolled_back"
    assert transaction["transaction_mode"] == "rollback_to_prefix"
    assert transaction["accepted_prefix_length"] == 0
    assert transaction["target_corrected_count"] == 0
    assert transaction["rollback_count"] == 1
    assert transaction["committed_position_count"] == 0
    assert transaction["target_prefix_position"] == 0
    assert transaction["target_prefix_digest"] == PREFIX_DIGEST


def test_a_parity_failure_can_never_be_committed() -> None:
    transaction = sd.verify_transaction_positions(
        transaction=open_transaction(), candidate_tokens=CANDIDATES, target=non_causal_target()
    )
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(transaction, ledger=ledger)
    assert error.value.code == "transaction_not_committable"
    assert ledger.position == 0
    assert ledger.snapshot()["commit_count"] == 0


# ---------------------------------------------------------------------------
# 3. Commit: atomic, strictly monotonic, idempotent for an identical digest
# ---------------------------------------------------------------------------


def test_commit_advances_the_target_watermark_by_the_committed_count() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    watermark = commit_transaction(transaction, ledger=ledger)

    assert watermark["commit_state"] == "committed"
    assert watermark["transaction_mode"] == "commit_verified_prefix"
    assert watermark["committed_position_count"] == 4
    assert watermark["committed_count"] == watermark["committed_position_count"]
    assert watermark["target_watermark_position"] == 4
    assert watermark["target_watermark_digest"] == transaction["committed_digest"]
    assert watermark["idempotent_duplicate"] is False
    assert ledger.position == 4
    assert ledger.digest == transaction["committed_digest"]
    assert ledger.snapshot()["commit_count"] == 1


def test_a_correction_commit_advances_by_prefix_plus_one() -> None:
    transaction, _ = evaluated(correction=2)
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    watermark = commit_transaction(transaction, ledger=ledger)

    assert watermark["commit_state"] == "committed_with_correction"
    assert watermark["transaction_mode"] == "commit_verified_prefix_plus_target_correction"
    assert watermark["committed_position_count"] == 3
    assert watermark["target_watermark_position"] == 3
    assert watermark["target_corrected_count"] == 1


def test_commit_is_idempotent_for_an_identical_cycle_digest() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    first = commit_transaction(transaction, ledger=ledger)
    second = commit_transaction(transaction, ledger=ledger)

    assert second["idempotent_duplicate"] is True
    assert second["target_watermark_position"] == first["target_watermark_position"] == 4
    assert second["target_watermark_digest"] == first["target_watermark_digest"]
    assert ledger.position == 4
    assert ledger.snapshot()["commit_count"] == 1


def test_an_identical_digest_under_a_second_cycle_reference_is_still_idempotent() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)
    first = commit_transaction(transaction, ledger=ledger)

    relabelled = dict(transaction)
    relabelled["cycle_ref"] = "cycle-2"
    again = commit_transaction(relabelled, ledger=ledger)

    assert again["idempotent_duplicate"] is True
    assert again["advanced"] is False
    assert again["target_watermark_position"] == first["target_watermark_position"] == 4
    assert ledger.snapshot()["commit_count"] == 1


def test_a_second_cycle_committing_the_same_position_with_other_content_fails_closed() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)
    commit_transaction(transaction, ledger=ledger)

    # A different cycle that would land on the same position from the same prefix,
    # but with different target content: the watermark never accepts it.
    other = dict(transaction)
    other.update(
        {
            "cycle_ref": "cycle-3",
            "idempotency_digest": OTHER_DIGEST,
            "accepted_prefix_length": 3,
            "rejected_count": 1,
            "target_corrected_count": 1,
            "rollback_count": 1,
            "committed_position_count": 0,
            "committed_count": 0,
            "commit_state": "target_correction_ready",
            "transaction_mode": "commit_verified_prefix_plus_target_correction",
            "accepted_prefix_digest": OTHER_DIGEST,
            "committed_digest": OTHER_DIGEST,
            "correction_token_digest": OTHER_DIGEST,
        }
    )

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(other, ledger=ledger)
    assert error.value.code == "conflicting_duplicate_cycle"
    assert ledger.snapshot()["commit_count"] == 1
    assert ledger.digest != OTHER_DIGEST


def test_a_conflicting_duplicate_cycle_fails_closed_and_does_not_advance() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)
    commit_transaction(transaction, ledger=ledger)

    conflicting = dict(transaction)
    conflicting["idempotency_digest"] = OTHER_DIGEST

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(conflicting, ledger=ledger)
    assert error.value.code == "conflicting_duplicate_cycle"
    assert ledger.position == 4
    assert ledger.snapshot()["commit_count"] == 1


def test_commit_from_a_stale_prefix_fails_closed() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=16, digest=OTHER_DIGEST)

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(transaction, ledger=ledger)
    assert error.value.code == "non_monotonic_commit"
    assert ledger.position == 16
    assert ledger.digest == OTHER_DIGEST


def test_commit_of_an_open_transaction_is_refused() -> None:
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(open_transaction(), ledger=ledger)
    assert error.value.code == "transaction_not_committable"
    assert ledger.snapshot()["commit_count"] == 0


def test_a_tampered_commit_beyond_the_evaluated_width_is_refused() -> None:
    transaction, _ = evaluated(correction=1)
    tampered = dict(transaction)
    tampered["accepted_prefix_length"] = 4

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        commit_transaction(tampered, ledger=sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST))
    assert error.value.code == "verification_transaction_invalid"


def test_rollback_returns_the_exact_original_prefix() -> None:
    transaction, _ = evaluated(correction=0)
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    rolled_back = sd.rollback_to_prefix(transaction=transaction, ledger=ledger)

    assert rolled_back["commit_state"] == "rolled_back"
    assert rolled_back["transaction_mode"] == "rollback_to_prefix"
    assert rolled_back["target_prefix_position"] == transaction["target_prefix_position"] == 0
    assert rolled_back["target_prefix_digest"] == transaction["target_prefix_digest"] == PREFIX_DIGEST
    assert rolled_back["target_watermark_position"] == 0
    assert rolled_back["committed_position_count"] == 0
    assert rolled_back["rollback_count"] == 1
    assert ledger.position == 0
    assert ledger.digest == PREFIX_DIGEST
    assert ledger.snapshot()["rollback_count"] == 1


def test_rollback_projection_restores_the_exact_prefix_it_was_given() -> None:
    rolled_back = sd.rollback_to_prefix(
        plan=DISABLED_PLAN, committed_prefix_position=9, committed_prefix_digest=PREFIX_DIGEST
    )

    assert rolled_back["commit_state"] == "rolled_back"
    assert rolled_back["transaction_mode"] == "rollback_to_prefix"
    assert rolled_back["target_prefix_position"] == 9
    assert rolled_back["target_prefix_digest"] == PREFIX_DIGEST
    assert rolled_back["target_watermark_position"] == 9
    assert rolled_back["committed_position_count"] == 0
    assert rolled_back["rollback_count"] == 1


def test_a_committed_transaction_cannot_be_rolled_back() -> None:
    transaction, _ = evaluated()
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)
    commit_transaction(transaction, ledger=ledger)

    committed_state = dict(transaction)
    committed_state.update(
        {
            "commit_state": "committed",
            "transaction_mode": "commit_verified_prefix",
            "committed_position_count": 4,
            "committed_count": 4,
            "target_watermark_position": 4,
            "target_watermark_digest": transaction["committed_digest"],
        }
    )

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.rollback_to_prefix(transaction=committed_state, ledger=ledger)
    assert error.value.code == "transaction_not_rollbackable"
    assert ledger.position == 4
    assert ledger.snapshot()["rollback_count"] == 0


def test_an_invalid_decision_source_is_refused_before_any_state_changes() -> None:
    ledger = sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.verify_transaction_positions(
            transaction=open_transaction(), candidate_tokens=CANDIDATES, target=object()
        )
    assert error.value.code == "target_decision_source_invalid"
    assert ledger.snapshot()["commit_count"] == 0


def test_a_failing_decision_source_becomes_a_closed_contract_error() -> None:
    class Broken:
        def evaluate_bounded_positions(self, *, prefix_digest, candidates):
            raise RuntimeError("private detail that must not escape")

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.verify_transaction_positions(
            transaction=open_transaction(), candidate_tokens=CANDIDATES, target=Broken()
        )
    assert error.value.code == "target_decision_source_failed"


def test_the_admitted_deadline_must_be_finite() -> None:
    for deadline in (float("nan"), float("inf"), -1.0):
        with pytest.raises(sd.SpeculativeDecodeContractError) as error:
            open_transaction(deadline_ms=deadline)
        assert error.value.code == "verification_deadline_invalid"


def test_commit_arithmetic_records_obey_the_frozen_runtime_invariants() -> None:
    for correction in (None, 0, 3):
        transaction, _ = evaluated(correction=correction)
        accepted = transaction["accepted_prefix_length"]
        rejected = transaction["rejected_count"]
        verified = transaction["verified_count"]
        corrected = transaction["target_corrected_count"]
        assert accepted + rejected == verified
        assert verified <= transaction["proposal_width"]
        assert corrected <= rejected
        assert transaction["rollback_count"] <= transaction["proposal_width"] - accepted
        # An evaluated transaction commits nothing until the commit itself runs.
        assert transaction["committed_position_count"] == 0

        watermark = commit_transaction(
            transaction, ledger=sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST)
        )
        assert watermark["committed_position_count"] == accepted + corrected
        assert watermark["committed_count"] == watermark["committed_position_count"]
        assert watermark["target_watermark_position"] == accepted + corrected


# ---------------------------------------------------------------------------
# 4. Plan-projection commit (the scaffold's frozen arithmetic entry point)
# ---------------------------------------------------------------------------


def test_plan_commit_projection_advances_by_prefix_plus_correction() -> None:
    watermark = sd.commit_verified_prefix(plan=DISABLED_PLAN, accepted_prefix_length=3, correction_token=9)

    assert watermark["committed_position_count"] == 4
    assert watermark["committed_count"] == 4
    assert watermark["transaction_mode"] == "commit_verified_prefix_plus_target_correction"
    assert watermark["target_corrected_count"] == 1


def test_plan_commit_projection_without_a_correction_commits_only_the_prefix() -> None:
    watermark = sd.commit_verified_prefix(plan=DISABLED_PLAN, accepted_prefix_length=3)

    assert watermark["committed_position_count"] == 3
    assert watermark["transaction_mode"] == "commit_verified_prefix"
    assert watermark["target_corrected_count"] == 0
    assert watermark["correction_token_digest"] is None


def test_plan_commit_projection_binds_the_plan_and_claims_no_verification() -> None:
    watermark = sd.commit_verified_prefix(plan=DISABLED_PLAN, accepted_prefix_length=2, correction_token=7)

    assert watermark["plan_digest"] == DISABLED_PLAN["plan_digest"]
    assert watermark["accepted_prefix_length"] == 2
    # The caller supplies the accepted prefix: this record arbitrates a commit and
    # never claims that a proposal was verified here.
    assert watermark["verification"] == "caller_supplied_accepted_prefix"
    assert watermark["claim"] == sd.DETERMINISTIC_VERIFIER_CLAIM
    assert watermark["generation_policy"] == "deterministic_greedy"


def test_plan_commit_projection_refuses_unsupported_sampling() -> None:
    sampling = _base_plan()
    sampling["compatibility"]["generation_policy"] = "unseeded_sampling"
    sampling["decision"]["reason"] = "incompatible_token_semantics"

    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.commit_verified_prefix(plan=sd.seal(sampling), accepted_prefix_length=1, correction_token=5)
    assert error.value.code == "unsupported_generation_policy"


def test_plan_commit_projection_never_touches_a_ledger() -> None:
    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.commit_verified_prefix(
            plan=DISABLED_PLAN,
            accepted_prefix_length=1,
            ledger=sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST),
        )
    assert error.value.code == "commit_ledger_requires_transaction"


def test_transaction_open_accepts_the_request_and_width_aliases() -> None:
    transaction = sd.open_target_verification_transaction(
        plan=DISABLED_PLAN, request_id="req-1", proposal_width=4, deadline_ms=60.0
    )

    assert transaction["cycle_ref"] == "req-1"
    assert transaction["proposal_width"] == 4
    assert transaction["commit_state"] == "open"


# ---------------------------------------------------------------------------
# 5. Admission: sampling rejection, off-by-default preference, revocation
# ---------------------------------------------------------------------------


def test_admission_of_a_disabled_plan_falls_back_target_only_without_a_sample() -> None:
    decision = sd.integrate_request_admission(
        plan=DISABLED_PLAN, request_id="req-1", preference="speculative_requested"
    )

    assert decision["mode"] == "target_only"
    assert decision["fallback_reason"] == "no_authorized_local_draft"
    assert decision["fallback_reason"] in sd.DISABLED_REASONS
    assert decision["effective_generation_policy"] is None
    assert decision["target_only_available"] is True
    assert decision["off_by_default"] is True


def test_admission_target_only_preference_is_a_first_class_choice() -> None:
    decision = sd.integrate_request_admission(
        plan=DISABLED_PLAN, request_id="req-1", preference="target_only_requested"
    )

    assert decision["mode"] == "target_only"
    assert decision["fallback_reason"] is None
    assert decision["admission"] == "target_only_requested"


def test_admission_of_a_qualified_enabled_plan_admits_speculation_off_by_default() -> None:
    decision = sd.integrate_request_admission(
        plan=ENABLED_PLAN, request_id="req-2", preference="speculative_requested"
    )

    assert decision["mode"] == "speculative_admitted"
    assert decision["fallback_reason"] is None
    assert decision["effective_generation_policy"] == "deterministic_greedy"
    assert decision["off_by_default"] is True
    assert decision["applies_to"] == "future_requests_only"


def test_unsupported_sampling_never_silently_switches_to_greedy() -> None:
    decision = sd.integrate_request_admission(
        plan=ENABLED_PLAN,
        request_id="req-3",
        preference="speculative_requested",
        generation_policy="unseeded_sampling",
    )

    assert decision["mode"] == "target_only"
    assert decision["fallback_reason"] == "incompatible_token_semantics"
    assert decision["requested_generation_policy"] == "unseeded_sampling"
    assert decision["effective_generation_policy"] is None
    assert decision["effective_generation_policy"] != "deterministic_greedy"
    assert decision["target_only_available"] is True


def test_a_revoked_overlay_blocks_new_admission_without_disabling_target_only() -> None:
    decision = sd.integrate_request_admission(
        plan=ENABLED_PLAN, request_id="req-4", preference="speculative_requested", revoked=True
    )

    assert decision["mode"] == "target_only"
    assert decision["fallback_reason"] == "parity_failed"
    assert decision["admission"] == "revoked_requalification_required"
    assert decision["target_only_available"] is True


def test_admission_rejects_an_unknown_preference() -> None:
    with pytest.raises(sd.SpeculativeDecodeContractError) as error:
        sd.integrate_request_admission(plan=DISABLED_PLAN, request_id="req-5", preference="force_overlay")
    assert error.value.code == "request_preference_invalid"


# ---------------------------------------------------------------------------
# 6. Privacy and claim boundary of the deterministic half
# ---------------------------------------------------------------------------


def test_transaction_records_carry_no_private_material() -> None:
    transaction, _ = evaluated(correction=1)
    watermark = commit_transaction(transaction, ledger=sd.TargetPrefixLedger(position=0, digest=PREFIX_DIGEST))
    records = [transaction, watermark]

    for record in records:
        for key in record:
            lowered = str(key).lower()
            assert "prompt" not in lowered
            assert "output_text" not in lowered
            assert "raw_token" not in lowered
            assert "logit" not in lowered
            assert "kv_bytes" not in lowered
        for value in record.values():
            assert value not in CANDIDATES


def test_the_deterministic_half_claims_only_deterministic_semantics() -> None:
    transaction = open_transaction()

    assert transaction["claim"] == sd.DETERMINISTIC_VERIFIER_CLAIM
    assert sd.SCAFFOLD_STAGE == "scaffold"
    assert "no physical target verification" in sd.DETERMINISTIC_VERIFIER_CLAIM
