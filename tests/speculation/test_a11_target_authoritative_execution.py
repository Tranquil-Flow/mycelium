# SPDX-License-Identifier: AGPL-3.0-only
"""A11 target-authoritative speculative-decode execution (wave-2 source tranche).

RED-first: this module is imported before it exists, so the first execution of this
file fails with ``ModuleNotFoundError: No module named
'mycelium_speculative_decode_execution'``.  Every assertion then targets behaviour
the A11 scaffold boundary refuses with ``SpeculativeDecodeUnavailable``: the private
bounded proposal envelope, the runtime cycle, and the target-authoritative commit
that is wired to the A10 verified-token path
(``mycelium_router.a10_target_verification.verify_candidate_positions``).

What passing this file proves: deterministic in-process speculative-cycle
semantics over an injected target-decision source, an in-process private transport
and the A10 verifier's declared port.  What it does NOT prove: any physical
draft/target route, any authenticated network proposal transfer, any live-route
benchmark, any browser or eight-workspace product surface, any qualification, any
benefit claim, and no A11 closure requirement.
"""

from __future__ import annotations

import copy
import inspect
import json

import pytest

import mycelium_speculative_decode as sd
import mycelium_speculative_decode_execution as sx
from mycelium_router import a10_target_verification as a10
from mycelium_router import a10_verifier_execution as a10_execution

# Candidate token ids chosen far away from any record arithmetic so a leaked raw
# token is unmistakable in a projection.
CANDIDATES = (900_001, 900_002, 900_003, 900_004)
CORRECTION_TOKENS = (800_000, 800_001, 800_002, 800_003)


# ---------------------------------------------------------------------------
# Fixtures: an admissible, enabled plan and deterministic doubles
# ---------------------------------------------------------------------------


def admissible_plan() -> dict:
    """The fixture plan promoted to a qualified-enabled, admissible plan."""

    plan = copy.deepcopy(sd.compatibility_fixtures()["speculative_decode_plan"])
    plan["verifier"]["status"] = "qualified"
    for key in plan["compatibility"]:
        if key not in {"generation_policy", "kv_alias", "kv_transfer"}:
            plan["compatibility"][key] = True
    plan["deadline_policy"]["warm_target_only_p95_tpot_ms"] = 80.0
    plan["observation"] = {
        "sample_count": 12,
        "observed_wall_clock_reduction_fraction": 0.14,
        "paired_lower_bound_fraction": 0.11,
        "ttft_regression_fraction": 0.02,
    }
    plan["decision"] = {
        "state": sd.ENABLED_DECISION,
        "reason": "material_gain_and_parity_qualified",
        "material_gain_threshold": sd.BENCHMARK_IMPROVEMENT_FRACTION,
        "target_only_fallback": True,
    }
    return sd.seal(plan)


def disabled_plan() -> dict:
    return sd.compatibility_fixtures()["speculative_decode_plan"]


class CausalTarget:
    """Deterministic causal target-decision double (never reads candidates[i])."""

    def __init__(self, decide) -> None:
        self.decide = decide
        self.bounded_calls = 0
        self.bounded_widths: list[int] = []
        self.sequential_calls = 0

    def _decision(self, prefix_digest: str, accepted: tuple[int, ...]) -> int:
        return int(self.decide(prefix_digest, accepted))

    def evaluate_bounded_positions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
        self.bounded_calls += 1
        self.bounded_widths.append(len(candidates))
        return tuple(self._decision(prefix_digest, tuple(candidates[:index])) for index in range(len(candidates)))

    def sequential_decisions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
        self.sequential_calls += len(candidates)
        return tuple(self._decision(prefix_digest, tuple(candidates[:index])) for index in range(len(candidates)))


def matching_target(*, correction: int | None = None) -> CausalTarget:
    """A causal target that agrees with ``CANDIDATES`` up to ``correction``.

    A11/A10 authority rejects the whole suffix after the first mismatch, so the
    mismatch is contiguous: positions from ``correction`` on are all rejected.
    """

    def decide(_prefix_digest: str, accepted: tuple[int, ...]) -> int:
        index = len(accepted)
        if correction is not None and index >= correction:
            return CORRECTION_TOKENS[index]
        return CANDIDATES[index]

    return CausalTarget(decide)


def non_causal_target() -> CausalTarget:
    """A cheating batched shortcut, in the shape A10 can actually observe.

    The bounded call reports that every candidate position matched, while the
    sequential target authority over the same prefix disagrees.  A10's transactional
    verifier accepts the claimed prefix; the deterministic parity oracle refuses it.
    """

    class NonCausal(CausalTarget):
        def evaluate_bounded_positions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
            self.bounded_calls += 1
            self.bounded_widths.append(len(candidates))
            return tuple(candidates)

        def sequential_decisions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
            self.sequential_calls += len(candidates)
            return tuple(700_000 + index for index in range(len(candidates)))

    return NonCausal(lambda _prefix_digest, _accepted: 0)


def non_contiguous_target() -> CausalTarget:
    """A verifier result that accepts a position after rejecting one: a partial commit."""

    class NonContiguous(CausalTarget):
        def evaluate_bounded_positions(self, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
            self.bounded_calls += 1
            self.bounded_widths.append(len(candidates))
            raw = list(candidates)
            raw[1] = 700_001  # position 1 rejected, position 2 still "matches"
            return tuple(raw)

    return NonContiguous(lambda _prefix_digest, _accepted: 0)


def target_runtime(source: CausalTarget, **overrides: object) -> sx.A10TargetKvRuntime:
    options = {
        "target_kv_ref": "target-kv-1",
        "target_runtime_incarnation": "sha256:" + "3" * 64,
    }
    options.update(overrides)
    return sx.A10TargetKvRuntime(source, **options)  # type: ignore[arg-type]


def draft_runtime(*, tokens: tuple[int, ...] = CANDIDATES, **overrides: object) -> sx.DeterministicDraftRuntime:
    options = {
        "incarnation": "sha256:" + "6" * 64,
        "timing_ms": (1.0, 2.0),
    }
    options.update(overrides)
    return sx.DeterministicDraftRuntime(lambda _position, _width: tokens, **options)  # type: ignore[arg-type]


def run_cycle(plan: dict | None = None, *, target=None, draft=None, transport=None, cancellation=None, **kwargs):
    if isinstance(target, sx.A10TargetKvRuntime):
        target_port = target
    elif target is None:
        target_port = target_runtime(matching_target())
    else:
        target_port = target_runtime(target)
    arguments = {
        "plan": admissible_plan() if plan is None else plan,
        "request_id": "req-1",
        "cycle_ref": "cycle-1",
        "attempt": 1,
        "width": 4,
        "target": target_port,
        "draft": draft_runtime() if draft is None else draft,
        "transport": sx.ProposalTransport() if transport is None else transport,
        "cancellation": sx.CancellationScope() if cancellation is None else cancellation,
    }
    arguments.update(kwargs)
    return sx.run_speculative_cycle(**arguments)


def cycle_with_correction(index: int):
    return run_cycle(target=matching_target(correction=index))


# ---------------------------------------------------------------------------
# 1. The private bounded proposal envelope
# ---------------------------------------------------------------------------


def test_contract_stage_boundary_raised_before_this_module_existed() -> None:
    """The scaffold boundary this tranche replaces still refuses its own seam."""

    with pytest.raises(sd.SpeculativeDecodeUnavailable) as failure:
        sd.transfer_proposal_envelope(plan=admissible_plan(), proposal_tokens=list(CANDIDATES))
    assert failure.value.code == "private_proposal_transfer_not_implemented"


def test_proposal_envelope_is_bounded_and_byte_counted() -> None:
    envelope = sx.build_proposal_envelope(
        plan=admissible_plan(),
        cycle_ref="cycle-1",
        request_id="req-1",
        attempt=1,
        tokens=CANDIDATES,
        prefix_position=0,
        prefix_digest="sha256:" + "0" * 64,
        draft_runtime_incarnation="sha256:" + "6" * 64,
    )
    assert envelope.width == 4
    assert envelope.token_count == 4
    assert envelope.byte_count == sx.PROPOSAL_BYTES_PER_TOKEN * 4
    assert envelope.generation_policy == "deterministic_greedy"
    assert envelope.attempt == 1


def test_proposal_envelope_public_projection_carries_no_private_material() -> None:
    envelope = build_envelope()
    projection = envelope.public_projection()
    sd.validate_public_projection(projection)
    blob = json.dumps(projection)
    assert "900001" not in blob
    for token in CANDIDATES:
        assert str(token) not in blob
    assert "tokens" not in projection


def build_envelope(**overrides: object):
    options = {
        "plan": admissible_plan(),
        "cycle_ref": "cycle-1",
        "request_id": "req-1",
        "attempt": 1,
        "tokens": CANDIDATES,
        "prefix_position": 0,
        "prefix_digest": "sha256:" + "0" * 64,
        "draft_runtime_incarnation": "sha256:" + "6" * 64,
    }
    options.update(overrides)
    return sx.build_proposal_envelope(**options)  # type: ignore[arg-type]


def test_proposal_envelope_refuses_excess_width_and_bytes() -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        build_envelope(tokens=CANDIDATES + (900_005,))
    assert failure.value.code == "proposal_envelope_out_of_bounds"


def test_proposal_envelope_requires_a_real_token_sequence() -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        build_envelope(tokens=())
    assert failure.value.code == "proposal_envelope_out_of_bounds"


def test_proposal_envelope_binds_cycle_prefix_and_idempotency() -> None:
    first = build_envelope()
    second = build_envelope(cycle_ref="cycle-2")
    third = build_envelope(prefix_digest="sha256:" + "9" * 64)
    assert first.envelope_digest != second.envelope_digest
    assert first.envelope_digest != third.envelope_digest
    assert first.idempotency_digest != second.idempotency_digest


def test_proposal_transfer_is_bounded_and_reports_its_own_cost() -> None:
    transport = sx.ProposalTransport(transfer_ms=0.5)
    envelope = build_envelope()
    transfer_ms = transport.deliver(envelope)
    assert transfer_ms == 0.5
    assert transport.snapshot()["delivered"] == 1
    assert transport.snapshot()["bytes"] == envelope.byte_count
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        sx.ProposalTransport(maximum_bytes=4).deliver(envelope)
    assert failure.value.code == "proposal_transfer_buffer_exceeded"


# ---------------------------------------------------------------------------
# 2. A10 wiring: the declared target-verification port
# ---------------------------------------------------------------------------


def test_the_target_runtime_is_a_real_a10_verification_port() -> None:
    assert isinstance(target_runtime(matching_target()), a10_execution.TargetVerificationRuntimePort)
    wiring = " ".join(sx.A10_WIRING)
    assert "a10_target_verification" in wiring
    assert "a10_verifier_execution" in wiring


def test_the_cycle_calls_the_a10_bounded_verifier_exactly_once() -> None:
    source = matching_target()
    cycle = run_cycle(target=target_runtime(source))
    assert source.bounded_calls == 1
    assert source.bounded_widths == [4]
    assert isinstance(cycle.a10_request_object, a10.TargetVerificationRequest)
    assert cycle.a10_request_object.candidate_position_count == 4
    assert cycle.a10_request_object.transaction_mode == a10.TRANSACTION_MODES[2]
    assert cycle.a10_outcome["verified_count"] == 4


def test_the_a10_request_binds_no_candidate_token_ids() -> None:
    cycle = run_cycle()
    assert "tokens" not in cycle.a10_request
    blob = json.dumps(cycle.a10_request)
    for token in CANDIDATES:
        assert str(token) not in blob


def test_the_a10_contract_refuses_an_unqualified_generation_policy() -> None:
    with pytest.raises(a10.A10ContractError):
        a10.TargetVerificationRequest(
            request_ref="req-1",
            target_kv_ref="target-kv-1",
            runtime_incarnation="sha256:" + "3" * 64,
            committed_prefix_position=0,
            committed_prefix_digest="sha256:" + "0" * 64,
            candidate_position_count=4,
            candidate_bytes=16,
            generation_policy="sampling",
            transaction_mode=a10.TRANSACTION_MODES[2],
            deadline_at=60.0,
            cancellation_generation=0,
            idempotency_digest="sha256:" + "1" * 64,
        )


def test_sx_module_imports_the_a10_verifier_by_name() -> None:
    source = inspect.getsource(sx)
    assert "a10_verifier_execution" in source
    assert "verify_candidate_positions" in source


# ---------------------------------------------------------------------------
# 3. Target-authoritative commit, correction and rollback
# ---------------------------------------------------------------------------


def test_all_positions_matching_commit_the_verified_prefix_once() -> None:
    cycle = run_cycle()
    assert cycle.mode == "speculative"
    assert cycle.runtime_request["mode"] == "speculative_admitted"
    assert cycle.a10_outcome["commit_state"] == "committed"
    assert cycle.a10_outcome["accepted_prefix_count"] == 4
    assert cycle.a10_outcome["correction_count"] == 0
    assert cycle.target_kv["position"] == 4
    assert cycle.target_kv["commits"] == 1
    assert cycle.runtime_request["committed_count"] == 4
    assert cycle.runtime_request["target_committed_output_tokens"] == 4
    assert cycle.output == {"released": True, "released_tokens": 4, "already_released": False}


@pytest.mark.parametrize("index", [0, 2, 3])
def test_a_mismatch_commits_the_common_prefix_plus_one_correction(index: int) -> None:
    cycle = cycle_with_correction(index)
    assert cycle.a10_outcome["commit_state"] == "committed_with_correction"
    assert cycle.a10_outcome["accepted_prefix_count"] == index
    assert cycle.a10_outcome["correction_count"] == 1
    assert cycle.target_kv["position"] == index + 1
    assert cycle.runtime_request["accepted_count"] == index
    assert cycle.runtime_request["target_corrected_count"] == 1
    assert cycle.runtime_request["committed_count"] == index + 1
    assert cycle.output["released_tokens"] == index + 1


def test_rejected_suffix_positions_are_rolled_back_in_both_domains() -> None:
    cycle = cycle_with_correction(2)
    assert cycle.runtime_request["rejected_count"] == 2
    assert cycle.target_kv["rolled_back_positions"] == 2
    assert cycle.draft_kv["rolled_back_positions"] == 2
    assert cycle.draft_kv["committed_tokens"] == 3


def test_a_non_causal_target_fails_parity_and_commits_nothing() -> None:
    source = non_causal_target()
    cycle = run_cycle(target=target_runtime(source))
    assert cycle.parity["parity"] == "failed"
    assert cycle.target_kv["position"] == 0
    assert cycle.target_kv["commits"] == 0
    assert cycle.runtime_request["fallback_reason"] == "verifier_parity_failure"
    assert cycle.circuit_breaker["state"] == "revoked_requalification_required"
    assert cycle.output["released"] is False
    assert cycle.failure["failure_reason"] == "parity_failed"


def test_a_parity_failure_never_reports_a_committed_position() -> None:
    cycle = run_cycle(target=target_runtime(non_causal_target()))
    assert cycle.runtime_request["committed_count"] == 0
    assert cycle.runtime_request["target_committed_output_tokens"] == 0
    assert cycle.runtime_request["target_corrected_count"] == 0
    assert cycle.draft_kv["committed_tokens"] == 0


# ---------------------------------------------------------------------------
# 4. Idempotency, conflicting duplicates and stale prefixes
# ---------------------------------------------------------------------------


def test_an_identical_cycle_binding_is_stable_across_builds() -> None:
    """The envelope is deterministic: the same cycle inputs give the same binding."""

    first = build_envelope()
    second = build_envelope()
    assert first.idempotency_digest == second.idempotency_digest
    assert first.envelope_digest == second.envelope_digest


def test_an_identical_cycle_binding_never_advances_the_watermark_twice() -> None:
    """The ledger's idempotent branch, driven by the cycle's own envelope digest."""

    envelope = build_envelope()
    runtime = target_runtime(matching_target())
    ledger = sd.TargetPrefixLedger(position=0, digest=runtime.committed_prefix_digest())
    committed_digest = sx._prefix_digest(  # noqa: SLF001 - the frozen digest scheme
        parent_digest=runtime.committed_prefix_digest(), position=4, tokens=CANDIDATES
    )
    first = ledger.commit(
        cycle_ref="cycle-1", idempotency_digest=envelope.idempotency_digest, position=4, digest=committed_digest
    )
    second = ledger.commit(
        cycle_ref="cycle-1", idempotency_digest=envelope.idempotency_digest, position=4, digest=committed_digest
    )
    assert first["advanced"] is True
    assert second["advanced"] is False
    assert second["idempotent_duplicate"] is True
    assert ledger.position == 4


def test_two_cycles_advance_monotonically_and_never_duplicate_emission() -> None:
    source = matching_target()
    runtime = target_runtime(source)
    ledger = sd.TargetPrefixLedger(position=0, digest=runtime.committed_prefix_digest())
    first = run_cycle(target=runtime, ledger=ledger)
    second = run_cycle(target=runtime, ledger=ledger, cycle_ref="cycle-2")
    assert (first.target_kv["position"], second.target_kv["position"]) == (4, 8)
    assert first.output["released_tokens"] == 4
    assert second.output["released_tokens"] == 4
    assert first.output["already_released"] is False
    assert second.output["already_released"] is False
    assert ledger.position == 8


def test_a_conflicting_duplicate_cycle_fails_closed_to_the_watermark() -> None:
    runtime = target_runtime(matching_target())
    ledger = sd.TargetPrefixLedger(position=0, digest=runtime.committed_prefix_digest())
    first = run_cycle(target=runtime, ledger=ledger)
    conflicting = run_cycle(
        target=runtime,
        ledger=ledger,
        draft=draft_runtime(tokens=(900_001, 900_002, 900_003, 900_099)),
    )
    assert first.target_kv["position"] == 4
    assert conflicting.failure["failure_reason"] == "conflicting_duplicate_cycle"
    assert conflicting.runtime_request["fallback_reason"] == "invalid_proposal"
    assert conflicting.runtime_request["committed_count"] == 0
    assert conflicting.output["released"] is False
    assert conflicting.target_kv["position"] == 4
    assert ledger.position == 4


def test_a_stale_prefix_is_refused_before_the_verifier_is_called() -> None:
    source = matching_target()
    stale = sd.TargetPrefixLedger(position=2, digest="sha256:" + "7" * 64)
    cycle = run_cycle(target=target_runtime(source), ledger=stale)
    assert source.bounded_calls == 0
    assert cycle.a10_outcome is None
    assert cycle.failure["failure_reason"] == "stale_prefix"
    assert cycle.runtime_request["fallback_reason"] == "invalid_proposal"
    assert cycle.target_kv["position"] == 0


# ---------------------------------------------------------------------------
# 5. Admission, deadline fallback and the circuit breaker
# ---------------------------------------------------------------------------


def test_a_disabled_plan_admits_target_only_without_any_draft_work() -> None:
    draft = draft_runtime()
    cycle = run_cycle(disabled_plan(), draft=draft)
    assert cycle.mode == "target_only"
    assert cycle.admission["mode"] == "target_only"
    assert cycle.admission["fallback_reason"] == "no_authorized_local_draft"
    assert draft.snapshot()["proposals"] == 0
    assert cycle.a10_outcome is None
    assert cycle.runtime_request["mode"] == "target_only"
    assert cycle.runtime_request["proposal_width"] == 0
    assert cycle.runtime_request["deadline_outcome"] == "none"
    assert cycle.runtime_document["mode"] == "target_only"


def test_an_explicit_target_only_preference_never_runs_draft_work() -> None:
    draft = draft_runtime()
    cycle = run_cycle(draft=draft, preference="target_only_requested")
    assert cycle.mode == "target_only"
    assert draft.snapshot()["proposals"] == 0
    assert cycle.output["released"] is False


def test_a_draft_loss_falls_back_from_the_exact_target_watermark() -> None:
    cycle = run_cycle(draft=draft_runtime(lost=True))
    assert cycle.runtime_request["fallback_reason"] == "draft_lost"
    assert cycle.runtime_request["deadline_outcome"] == "none"
    assert cycle.target_kv["position"] == 0
    assert cycle.target_kv["commits"] == 0
    assert cycle.output["released"] is False


def test_a_missing_draft_reports_unavailable_not_a_failure() -> None:
    cycle = run_cycle(draft=draft_runtime(unavailable=True))
    assert cycle.runtime_request["fallback_reason"] == "draft_unavailable"
    assert cycle.target_kv["position"] == 0


def test_a_missed_proposal_deadline_uses_the_ordinary_target_step() -> None:
    cycle = run_cycle(draft=draft_runtime(timing_ms=(40.0, 40.0)))
    assert cycle.deadline["missed"] is True
    assert cycle.runtime_request["deadline_outcome"] == "proposal_deadline_missed"
    assert cycle.runtime_request["fallback_reason"] == "proposal_deadline_missed"
    assert cycle.a10_outcome is None
    assert cycle.target_kv["position"] == 0
    assert cycle.circuit_breaker["state"] == "closed"


def test_a_late_proposal_is_rejected_by_cycle_identity() -> None:
    cycle = run_cycle(deadline_ms=5.0, draft=draft_runtime(timing_ms=(40.0, 40.0)))
    assert cycle.deadline["admitted_deadline_ms"] == 5.0
    assert cycle.runtime_request["fallback_reason"] == "proposal_deadline_missed"
    assert cycle.envelope is None


def test_three_consecutive_deadlines_open_the_circuit_breaker_target_only() -> None:
    window: list[dict] = []
    states = [
        run_cycle(draft=draft_runtime(timing_ms=(40.0, 40.0)), window=window).circuit_breaker["state"]
        for _ in range(3)
    ]
    assert states == ["closed", "closed", "open_target_only"]
    assert window[-1]["deadline_missed"] is True
    assert len(window) == 3


def test_a_parity_violation_revokes_requalification_rather_than_cooling_down() -> None:
    window: list[dict] = []
    cycle = run_cycle(target=target_runtime(non_causal_target()), window=window)
    assert cycle.circuit_breaker["state"] == "revoked_requalification_required"
    assert cycle.circuit_breaker["cooldown_seconds"] is None
    assert cycle.circuit_breaker["requalification_required"] is True


def test_an_unknown_warm_baseline_never_manufactures_a_deadline() -> None:
    plan = admissible_plan()
    plan["deadline_policy"]["warm_target_only_p95_tpot_ms"] = None
    plan = sd.seal(plan)
    draft = draft_runtime()
    cycle = run_cycle(plan, draft=draft)
    assert cycle.mode == "target_only"
    assert cycle.deadline["abstain_reason"] == "warm_target_only_p95_tpot_unknown"
    assert cycle.deadline["admitted_deadline_ms"] is None
    assert draft.snapshot()["proposals"] == 0
    assert cycle.a10_outcome is None


def test_an_unsupported_generation_policy_is_never_coerced_to_greedy() -> None:
    plan = disabled_plan()
    plan["compatibility"]["generation_policy"] = "sampling"
    plan["decision"]["reason"] = "incompatible_token_semantics"
    plan = sd.seal(plan)
    draft = draft_runtime()
    cycle = run_cycle(plan, draft=draft)
    assert cycle.admission["mode"] == "target_only"
    assert cycle.admission["fallback_reason"] == "incompatible_token_semantics"
    assert draft.snapshot()["proposals"] == 0
    assert cycle.runtime_request["mode"] == "target_only"


# ---------------------------------------------------------------------------
# 6. Cancellation, target loss and exactly-once cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("checkpoint", sx.CANCELLATION_CHECKPOINTS)
def test_cancellation_at_every_boundary_rolls_back_and_releases_no_output(checkpoint: str) -> None:
    cancellation = sx.CancellationScope(cancel_at=checkpoint)
    cycle = run_cycle(cancellation=cancellation)
    assert cycle.cancellation["cancelled_at"] == checkpoint
    assert cycle.runtime_request["cancelled"] is True
    assert cycle.runtime_request["terminal_state"] == "cancelled"
    assert cycle.runtime_request["committed_count"] == 0
    assert cycle.runtime_request["cleanup_complete"] is True
    assert cycle.output["released"] is False
    assert cycle.target_kv["position"] == 0
    assert cycle.target_kv["commits"] == 0
    assert cycle.draft_kv["tentative_tokens"] == 0


def test_cancellation_checkpoints_are_checked_in_the_frozen_order() -> None:
    cancellation = sx.CancellationScope()
    cycle = run_cycle(cancellation=cancellation)
    assert tuple(cycle.cancellation["checked"]) == sx.CANCELLATION_CHECKPOINTS
    assert cycle.cancellation["cancelled_at"] is None


def test_target_loss_is_an_explicit_terminal_state_not_a_draft_fallback() -> None:
    runtime = target_runtime(matching_target())
    runtime.mark_target_lost()
    cycle = run_cycle(target=runtime)
    assert cycle.runtime_request["terminal_state"] == "target_lost_explicit"
    assert cycle.runtime_request["fallback_reason"] == "none"
    assert cycle.runtime_request["cancelled"] is False
    assert cycle.failure["failure_reason"] == "target_lost_explicit"
    assert cycle.target_kv["commits"] == 0
    assert cycle.output["released"] is False


def test_the_cycle_leaves_no_open_transaction_or_tentative_state() -> None:
    cycle = run_cycle()
    assert cycle.target_kv["open_transactions"] == 0
    assert cycle.target_kv["cleanup"] == "released"
    assert cycle.draft_kv["tentative_tokens"] == 0


# ---------------------------------------------------------------------------
# 7. Separate KV authority and privacy-reduced projection
# ---------------------------------------------------------------------------


def test_target_and_draft_kv_are_separate_owners() -> None:
    cycle = run_cycle()
    assert cycle.target_kv["kv_ref"] != cycle.draft_kv["kv_ref"]
    assert cycle.target_kv["aliased_with_draft"] is False
    assert cycle.draft_kv["wrote_target_state"] is False


def test_a_draft_rollback_can_never_roll_target_state_back() -> None:
    source = non_causal_target()
    runtime = target_runtime(source)
    cycle = run_cycle(target=runtime)
    assert cycle.draft_kv["rolled_back_positions"] == 4
    assert runtime.committed_prefix_position() == 0
    assert cycle.target_kv["position"] == 0


def test_the_runtime_document_is_accepted_by_the_frozen_contract_validator() -> None:
    cycle = run_cycle()
    document = cycle.runtime_document
    sd.validate_speculative_decode_runtime(document)
    assert document["protocol"] == sd.RUNTIME_PROTOCOL
    assert document["mode"] == "speculative"
    assert document["requests"] == [cycle.runtime_request]


def test_the_runtime_projection_carries_no_private_material() -> None:
    cycle = run_cycle()
    blob = json.dumps(cycle.runtime_document)
    for token in CANDIDATES:
        assert str(token) not in blob
    for marker in ("prompt", "logits", "tensor", "kv_bytes", "sampler"):
        assert marker not in blob


def test_the_runtime_projection_satisfies_the_frozen_count_invariants() -> None:
    for cycle in (run_cycle(), cycle_with_correction(0), run_cycle(target=target_runtime(non_causal_target()))):
        request = cycle.runtime_request
        assert request["accepted_count"] + request["rejected_count"] <= request["verified_count"]
        assert request["verified_count"] <= request["proposed_count"]
        assert request["committed_count"] == request["accepted_count"] + request["target_corrected_count"]
        assert request["target_committed_output_tokens"] == request["committed_count"]


def test_the_cycle_claims_only_deterministic_in_process_semantics() -> None:
    cycle = run_cycle()
    assert cycle.claim == sx.CLAIM_BOUNDARY
    assert "physical" in cycle.claim
    assert "No physical" in cycle.claim
    assert "no qualification" in cycle.claim.lower()


def test_a_verifier_partial_commit_refusal_fails_closed_and_revokes() -> None:
    """A10 refuses a non-contiguous acceptance; A11 commits nothing and revokes."""

    source = non_contiguous_target()
    cycle = run_cycle(target=target_runtime(source))
    assert cycle.failure["failure_reason"] == "a10_verification_refused"
    assert cycle.failure["a10_code"] == "non_contiguous_acceptance"
    assert cycle.a10_outcome is None
    assert cycle.runtime_request["fallback_reason"] == "verifier_parity_failure"
    assert cycle.circuit_breaker["state"] == "revoked_requalification_required"
    assert cycle.target_kv["position"] == 0
    assert cycle.output["released"] is False


def test_the_port_serves_the_a10_declared_sequential_parity_oracle() -> None:
    source = matching_target(correction=1)
    runtime = target_runtime(source)
    runtime.prepare_candidates(CANDIDATES)
    request = a10.TargetVerificationRequest(
        request_ref="req-1",
        target_kv_ref=runtime.target_kv_ref,
        runtime_incarnation=runtime.target_runtime_incarnation,
        committed_prefix_position=runtime.committed_prefix_position(),
        committed_prefix_digest=runtime.committed_prefix_digest(),
        candidate_position_count=4,
        candidate_bytes=16,
        generation_policy="deterministic_greedy",
        transaction_mode=a10.TRANSACTION_MODES[2],
        deadline_at=60.0,
        cancellation_generation=0,
        idempotency_digest="sha256:" + "1" * 64,
    )
    parity = a10_execution.sequential_parity_reference(runtime, request)
    assert parity.parity is True
    assert parity.compared_count == 4


def test_the_port_sequential_oracle_refuses_a_cheating_bounded_call() -> None:
    source = non_causal_target()
    runtime = target_runtime(source)
    runtime.prepare_candidates(CANDIDATES)
    reference = runtime.sequential_reference(4)
    assert reference.committed_position == 1
    assert [item.accepted for item in reference.decisions] == [False, False, False, False]


def test_the_execution_stage_is_not_a_closure_claim() -> None:
    assert sx.EXECUTION_STAGE == "deterministic_source"
    assert sx.PROTOCOL == "mycelium.speculative_decode_execution.v1"


# ---------------------------------------------------------------------------
# 8. Bounds and the still-blocked physical seams
# ---------------------------------------------------------------------------


def test_a_width_beyond_the_plan_bound_is_refused() -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        run_cycle(width=5)
    assert failure.value.code == "proposal_width_out_of_bounds"


def test_an_unqualified_verifier_never_gets_a_verification_call() -> None:
    plan = disabled_plan()
    plan["decision"]["reason"] = "target_verifier_unavailable"
    plan = sd.seal(plan)
    source = matching_target()
    cycle = run_cycle(plan, target=target_runtime(source))
    assert source.bounded_calls == 0
    assert cycle.a10_outcome is None
    assert cycle.admission["mode"] == "target_only"
    assert cycle.admission["fallback_reason"] == "target_verifier_unavailable"


@pytest.mark.parametrize("code", sorted(sx.EXECUTION_BOUNDARIES))
def test_the_physical_and_live_seams_still_refuse_and_never_return_a_document(code: str) -> None:
    seam = getattr(sx, sx.EXECUTION_BOUNDARIES[code])
    with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable) as failure:
        seam(plan=admissible_plan(), route_digest="sha256:" + "0" * 64)
    assert failure.value.code == code


def test_the_boundary_map_is_total_and_points_at_real_callables() -> None:
    for code, name in sx.EXECUTION_BOUNDARIES.items():
        assert code.endswith("_not_available")
        assert callable(getattr(sx, name))
