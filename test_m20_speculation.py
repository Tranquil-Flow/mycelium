from __future__ import annotations

import copy

import pytest

from mycelium_m20_speculation import (
    SpeculativeDecoder,
    build_speculative_plan,
    build_speculative_runtime,
    validate_speculative_plan,
    validate_speculative_runtime,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _binding() -> dict[str, object]:
    return {
        "deployment_id": "deployment-a",
        "deployment_epoch": 1,
        "graph_digest": DIGEST_A,
        "membership_generation": 2,
        "model_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "model_revision": "target-revision",
        "qualification_id": "qualification-a",
        "qualification_digest": DIGEST_B,
    }


def _model(model_id: str, revision: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "model_revision": revision,
        "tokenizer_digest": DIGEST_A,
        "vocabulary_size": 151936,
        "special_tokens_digest": DIGEST_B,
        "position_semantics_digest": DIGEST_A,
        "kv_schema_digest": DIGEST_B,
    }


def _measurements(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "sample_count": 4,
        "target_only_tpot_ms": 20.0,
        "draft_tpot_ms": 2.0,
        "verification_batch_ms": 8.0,
        "proposal_transfer_ms": 0.5,
        "observed_acceptance_fraction": 0.75,
        "predicted_gain_fraction": 0.25,
        "observed_gain_fraction": 0.20,
    }
    value.update(changes)
    return value


def _compatibility(**changes: bool) -> dict[str, bool]:
    value = {
        "tokenizer": True,
        "vocabulary": True,
        "special_tokens": True,
        "position_semantics": True,
        "separate_kv_ownership": True,
        "batched_target_verification": True,
    }
    value.update(changes)
    return value


def test_plan_promotes_only_compatible_measured_material_gain() -> None:
    plan = build_speculative_plan(
        binding=_binding(),
        target=_model("Qwen/Qwen2.5-1.5B-Instruct", "target-revision"),
        draft=_model("Qwen/Qwen2.5-0.5B-Instruct", "draft-revision"),
        workload_id="interactive-short",
        proposal_width=4,
        acceptance_distribution=(0.05, 0.05, 0.10, 0.20, 0.60),
        compatibility=_compatibility(),
        measurements=_measurements(),
    )
    assert validate_speculative_plan(plan)["decision"] == {
        "state": "qualified_enabled",
        "reason": "material_gain_and_parity_qualified",
        "material_gain_threshold": 0.1,
        "target_fallback": True,
    }


@pytest.mark.parametrize(
    ("compatibility", "measurements", "reason"),
    (
        (
            _compatibility(batched_target_verification=False),
            _measurements(),
            "batched_target_verification_unavailable",
        ),
        (
            _compatibility(tokenizer=False),
            _measurements(),
            "draft_target_incompatible",
        ),
        (
            _compatibility(),
            _measurements(observed_gain_fraction=0.05),
            "material_gain_not_observed",
        ),
    ),
)
def test_plan_fails_closed_to_target_only(
    compatibility: dict[str, bool], measurements: dict[str, object], reason: str
) -> None:
    plan = build_speculative_plan(
        binding=_binding(),
        target=_model("target", "target-revision"),
        draft=_model("draft", "draft-revision"),
        workload_id="interactive-short",
        proposal_width=2,
        acceptance_distribution=(0.2, 0.3, 0.5),
        compatibility=compatibility,
        measurements=measurements,
    )
    assert plan["decision"]["state"] == "disabled"
    assert plan["decision"]["reason"] == reason


def test_speculative_decode_matches_target_only_with_rejection() -> None:
    def target(context: tuple[int, ...]) -> int:
        return (sum(context) + 3) % 17

    def draft(context: tuple[int, ...]) -> int:
        return target(context) if len(context) % 3 else (target(context) + 1) % 17

    def verify(context: tuple[int, ...], proposal: tuple[int, ...]) -> list[int]:
        result = []
        current = context
        for _ in proposal:
            token = target(current)
            result.append(token)
            current += (token,)
        return result

    decoder = SpeculativeDecoder(
        draft_step=draft,
        target_step=target,
        target_verify=verify,
        proposal_width=4,
    )
    result = decoder.decode((1, 2), max_new_tokens=12)
    reference: list[int] = []
    context = (1, 2)
    for _ in range(12):
        token = target(context)
        reference.append(token)
        context += (token,)
    assert result.token_ids == tuple(reference)
    assert result.rejected > 0
    assert result.rollback > 0
    assert result.verified == result.proposed


def test_draft_loss_falls_back_and_cancel_cleans_up() -> None:
    def target(context: tuple[int, ...]) -> int:
        return (len(context) + 1) % 11
    calls = 0

    def available() -> bool:
        nonlocal calls
        calls += 1
        return calls < 2

    decoder = SpeculativeDecoder(
        draft_step=target,
        target_step=target,
        target_verify=lambda context, proposal: proposal,
        proposal_width=2,
    )
    result = decoder.decode((1,), max_new_tokens=5, draft_available=available)
    assert result.fallback is True
    assert len(result.token_ids) == 5

    cancel_calls = 0

    def cancel() -> bool:
        nonlocal cancel_calls
        cancel_calls += 1
        return cancel_calls > 1

    cancelled = decoder.decode((1,), max_new_tokens=5, cancel_requested=cancel)
    assert cancelled.cancelled is True
    runtime = build_speculative_runtime(
        binding=_binding(),
        mode="target_fallback",
        requests=(
            {
                "request_id": "request-cancelled",
                "proposal_width": 2,
                "proposed_count": cancelled.proposed,
                "target_verified_count": cancelled.verified,
                "accepted_count": cancelled.accepted,
                "rejected_count": cancelled.rejected,
                "rollback_count": cancelled.rollback,
                "fallback_state": "draft_lost",
                "terminal_state": "cancelled",
                "cleanup_complete": True,
            },
        ),
    )
    assert validate_speculative_runtime(runtime)["requests"][0]["cleanup_complete"] is True


def test_contracts_reject_unknown_private_and_digest_drift() -> None:
    plan = build_speculative_plan(
        binding=_binding(),
        target=_model("target", "target-revision"),
        draft=_model("draft", "draft-revision"),
        workload_id="interactive-short",
        proposal_width=1,
        acceptance_distribution=(0.5, 0.5),
        compatibility=_compatibility(),
        measurements=_measurements(),
    )
    private = copy.deepcopy(plan)
    private["prompt"] = "secret"
    with pytest.raises(ValueError, match="m20_plan_invalid"):
        validate_speculative_plan(private)
    drift = copy.deepcopy(plan)
    drift["proposal_width"] = 2
    with pytest.raises(ValueError, match="m20_plan_invalid"):
        validate_speculative_plan(drift)
