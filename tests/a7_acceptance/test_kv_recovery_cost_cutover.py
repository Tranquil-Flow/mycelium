"""PV4: the cost policy at the A7-continuation vs A6-replay decision point.

The entry controller's covered-source cutover is the one place where both
recoveries are possible (the closed eligibility predicate says the standby's
acknowledged KV is exactly the committed prefix AND A6 is available).  A
cost policy is only an input there: it may choose replay over the eligible KV
continuation, never the reverse, and never when only one recovery exists.
A6/A7 semantics (exact output, fencing, honest fallback) are unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mycelium_kv_recovery_cost as cost
from tests.a7_acceptance.test_entry_cutover import _MAX_NEW, _Harness, _reference_tokens

SHAPE = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16")
FAST = cost.LinkMeasurement(bandwidth_bytes_per_s=100_000_000.0, rtt_s=0.003, source="probe")
SLOW = cost.LinkMeasurement(bandwidth_bytes_per_s=350_000.0, rtt_s=0.080, source="probe")
PREFILL = cost.PrefillMeasurement(tokens_per_s=4000.0, source="calibration")
# The numpy fixture has only 6 KV positions, so a zero-RTT pipe isolates the bytes-vs-tokens term.
FAST_NO_RTT = cost.LinkMeasurement(bandwidth_bytes_per_s=100_000_000.0, rtt_s=0.0, source="probe")


class _Recording:
    """A policy double that records the contexts it was asked about."""

    def __init__(self, decision) -> None:
        self.decision = decision
        self.contexts: list[dict] = []

    def __call__(self, context):
        self.contexts.append(dict(context))
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def _decision(choice: str, *, link=FAST, always_move_all: bool = False, positions: int = 2048):
    policy = cost.make_recovery_cost_policy(
        shape_for=lambda stage_id: SHAPE,
        link_for=lambda context: link,
        prefill_for=lambda context: PREFILL,
        margin=0.25,
        always_move_all=always_move_all,
    )
    record = policy({"stage_id": "s", "kv_positions": positions, "replay_tokens": positions + 1, "pending_positions": 0})
    assert record["choice"] == choice, record
    return record


def _cutover_result(harness: _Harness, request_id: str):
    record = harness.entry.entry.get_request(request_id)
    return record, record.kv_successor_cutover


def test_without_a_policy_the_result_is_unchanged(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run("request:pv4-none", kill_after=3)
    _record, result = _cutover_result(harness, "request:pv4-none")
    assert result["outcome"] == "fenced_kv_successor"
    assert "cost_decision" not in result and "cost_policy_error" not in result


def test_policy_receives_the_cutover_facts_and_kv_is_kept_when_it_wins(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    policy = _Recording(_decision("kv"))
    harness.controller.recovery_cost_policy = policy
    sink = harness.run("request:pv4-kv", kill_after=3)
    record, result = _cutover_result(harness, "request:pv4-kv")
    assert len(policy.contexts) == 1
    context = policy.contexts[0]
    # prompt (4 tokens) + 3 committed - 1 = 6 KV positions; the replay re-prefills prompt + committed = 7 tokens.
    assert context["kv_positions"] == 6 and context["replay_tokens"] == 7
    assert context["pending_positions"] == 0  # eligibility means the standby acknowledged every committed token
    assert context["gateway_committed_token_count"] == 3 == context["acknowledged_token_count"]
    assert isinstance(context["stage_id"], str) and context["source_placement_id"] != context["standby_placement_id"]
    # Continuation exactly as before: output equals the uninterrupted reference, no recovery prefill.
    assert result["outcome"] == "fenced_kv_successor" and result["reason"] == "eligible"
    assert result["kv_continuity_claimed"] is True and result["recovery_prefill"] is False
    assert sink.token_ids == _reference_tokens(harness.case)
    assert sink.token_indexes == list(range(_MAX_NEW))
    assert harness.standby_port.kv_snapshot()["prefill_operation_count"] == 0
    assert record.kv_successor_recovery is None
    # The decision and both predictions are in the recovery record.
    recorded = result["cost_decision"]
    assert recorded["choice"] == "kv" and recorded["transfer_s"] is not None and recorded["recompute_s"] is not None
    assert recorded["summary"].startswith("continued from KV")


def test_policy_choosing_replay_hands_the_attempt_to_a6_honestly(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    policy = _Recording(_decision("replay", link=SLOW, always_move_all=True))
    harness.controller.recovery_cost_policy = policy
    harness.run("request:pv4-replay", kill_after=3)
    record, result = _cutover_result(harness, "request:pv4-replay")
    assert len(policy.contexts) == 1
    assert result["outcome"] == "full_context_replay"
    assert result["reason"] == "cost_prefers_replay"
    assert result["kv_continuity_claimed"] is False
    assert result["gateway_committed_token_count"] == 3 == result["acknowledged_token_count"]
    recorded = result["cost_decision"]
    assert recorded["choice"] == "replay"
    assert recorded["transfer_s"] > recorded["recompute_s"]  # both predictions recorded
    assert recorded["summary"].startswith("recomputed (replay)")
    # Same as every other honest fallback: A6 owns the attempt, nothing of A7 stays behind.
    assert record.kv_successor_recovery is not None
    assert record.kv_successor_recovery["recovery_mode"] == "full_context_replay"
    assert harness.standby_port.kv_snapshot()["decode_operation_count"] == 0
    assert harness.standby_port.kv_snapshot()["active_state_count"] == 0
    assert harness.shadow.snapshot()["shadow_entry_count"] == 0
    assert harness.fences.current("request:pv4-replay") is None
    assert harness.fences.record_count() == 0
    # The old attempt was fenced durably before the choice (generation 1 -> 2), as for any rejection.
    assert result["fencing_generation"] == 2 and result["request_attempt"] == 2


def test_the_real_cost_model_drives_the_choice_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "fenced_kv_successor").mkdir()
    (tmp_path / "full_context_replay").mkdir()
    for link, expected, request_id in ((FAST_NO_RTT, "fenced_kv_successor", "request:pv4-fast"), (SLOW, "full_context_replay", "request:pv4-slow")):
        harness = _Harness(tmp_path / expected)
        # Cold-transfer pricing: as if the standby had to pull the whole KV at loss time.
        harness.controller.recovery_cost_policy = cost.make_recovery_cost_policy(
            shape_for=lambda stage_id: SHAPE,
            link_for=lambda context, link=link: link,
            prefill_for=lambda context: PREFILL,
            always_move_all=True,
        )
        harness.run(request_id, kill_after=3)
        _record, result = _cutover_result(harness, request_id)
        assert result["outcome"] == expected, result
        assert result["cost_decision"]["choice"] == ("kv" if expected == "fenced_kv_successor" else "replay")
        assert result["cost_decision"]["kv_bytes_total"] == SHAPE.kv_bytes(6)


def test_with_a6_unavailable_the_policy_is_not_consulted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, a6_available=False)
    policy = _Recording(_decision("replay", link=SLOW, always_move_all=True))
    harness.controller.recovery_cost_policy = policy
    harness.run("request:pv4-no-a6", kill_after=3)
    _record, result = _cutover_result(harness, "request:pv4-no-a6")
    assert policy.contexts == []  # only one recovery exists: unchanged behaviour
    assert result["outcome"] == "fenced_kv_successor" and "cost_decision" not in result


def test_with_the_kv_successor_ineligible_the_policy_is_not_consulted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, qualification_ttl_ms=10)
    policy = _Recording(_decision("kv"))
    harness.controller.recovery_cost_policy = policy
    harness.run("request:pv4-ineligible", kill_after=3)
    _record, result = _cutover_result(harness, "request:pv4-ineligible")
    assert policy.contexts == []
    assert result["outcome"] == "full_context_replay" and result["reason"] == "qualification_not_current"
    assert "cost_decision" not in result


@pytest.mark.parametrize(
    ("returned", "code"),
    [
        (RuntimeError("boom"), "RuntimeError"),
        ("replay", "cost_policy_invalid"),
        ({"choice": "both"}, "cost_policy_invalid"),
        ({"protocol": "other", "choice": "replay"}, "cost_policy_invalid"),
    ],
)
def test_a_broken_policy_never_changes_the_recovery(tmp_path: Path, returned, code) -> None:
    harness = _Harness(tmp_path)
    harness.controller.recovery_cost_policy = _Recording(returned)
    sink = harness.run("request:pv4-broken", kill_after=3)
    _record, result = _cutover_result(harness, "request:pv4-broken")
    assert result["outcome"] == "fenced_kv_successor" and result["reason"] == "eligible"
    assert result["cost_policy_error"] == code
    assert "cost_decision" not in result
    assert sink.token_ids == _reference_tokens(harness.case)


def test_a_policy_with_no_opinion_changes_nothing(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.controller.recovery_cost_policy = _Recording(None)
    harness.run("request:pv4-none-opinion", kill_after=3)
    _record, result = _cutover_result(harness, "request:pv4-none-opinion")
    assert result["outcome"] == "fenced_kv_successor"
    assert "cost_decision" not in result and "cost_policy_error" not in result


def test_product_evidence_carries_the_decision(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.controller.recovery_cost_policy = _Recording(_decision("kv"))
    harness.run("request:pv4-evidence", kill_after=3)
    evidence = harness.controller.product_evidence()
    assert evidence is not None and evidence["outcome"] == "fenced_kv_successor"
    assert evidence["cost_decision"]["choice"] == "kv"
    # Without a decision the evidence keeps its exact previous shape.
    (tmp_path / "plain").mkdir()
    plain = _Harness(tmp_path / "plain")
    plain.run("request:pv4-evidence-plain", kill_after=3)
    assert "cost_decision" not in plain.controller.product_evidence()
