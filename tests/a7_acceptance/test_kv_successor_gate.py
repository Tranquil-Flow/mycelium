# SPDX-License-Identifier: AGPL-3.0-only
"""A7 slice 5: the physical gate coordinator's frozen plan and offline verdict."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_a7_kv_successor_gate.py"
_SPEC = importlib.util.spec_from_file_location("run_a7_kv_successor_gate", _PATH)
gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(gate)

CANDIDATE = "a" * 40
_REASON = {
   gate.POSITIVE: "eligible",
   gate.INCOMPATIBLE: "compatibility_incompatible",
   gate.LAGGING: "route_watermark_unavailable",
   gate.ABORT: "route_watermark_unavailable",
}


def _record(plan, case, **overrides):
   positive = case == gate.POSITIVE
   body = {
      "protocol": gate.CASE_PROTOCOL,
      "case": case,
      "candidate_commit": plan["candidate_commit"],
      "plan_digest": plan["plan_digest"],
      "ordinary_browser_request": True,
      "fault": "source_process_kill",
      "outcome": gate._EXPECTED_OUTCOME[case],
      "reason": _REASON[case],
      "kv_continuity_claimed": positive,
      "committed_before_failure": 3,
      "acknowledged_before_failure": 3 if positive else 0,
      "successor_recovery_prefill_operations": 0,
      "prefix_tokens_sent_to_successor": 0,
      "reference_parity": case != gate.ABORT,
      "duplicate_tokens": 0,
      "missing_tokens": 0,
      "publishers": 1,
      "terminal_results": 1,
      "successor_decode_operations": 4 if positive else 0,
      "stale_source_publications_accepted": 0,
      "interruption_ms": 3_000.0,
      "cutover_ms": 40.0,
      "checkpoint_bytes_total": 3 * 65_536,
      "checkpoint_count": 3,
      "max_ack_wait_ms": 120.0,
      "baseline_tpot_ms": 100.0,
      "candidate_tpot_ms": 120.0,
      "cleanup": {
         node: {"active_state_count_delta": 0, "active_kv_bytes_delta": 0, "shadow_bytes_delta": 0, "reservation_delta": 0, "fence_records_delta": 0}
         for node in ("node-0", "node-2")
      },
      "workspaces_checked": list(gate.THRESHOLDS["required_workspaces"]),
   }
   body.update(overrides)
   body.pop("evidence_digest", None)
   return {**body, "evidence_digest": gate._sha(body)}


def _records(plan, **per_case):
   return {case: _record(plan, case, **per_case.get(case, {})) for case in gate.CASES}


def test_plan_freezes_thresholds_and_topology_before_measurement() -> None:
   plan = gate.build_plan(CANDIDATE)
   assert plan == gate.build_plan(CANDIDATE)
   assert plan["topology"]["source_node"] == "node-2" and plan["topology"]["standby_node"] == "node-0"
   assert plan["cases"] == list(gate.CASES)
   tampered = json.loads(json.dumps(plan))
   tampered["thresholds"]["max_cutover_ms"] = 10**9
   with pytest.raises(ValueError):
      gate.evaluate(tampered, _records(plan))
   with pytest.raises(ValueError):
      gate.build_plan("not-a-commit")


def test_complete_measured_records_pass() -> None:
   plan = gate.build_plan(CANDIDATE)
   verdict = gate.evaluate(plan, _records(plan))
   assert verdict["passed"] is True, verdict["cases"]
   assert all(failures == [] for failures in verdict["cases"].values())


@pytest.mark.parametrize(
   ("case", "override", "failure"),
   [
      (gate.POSITIVE, {"successor_recovery_prefill_operations": 1}, "recovery_prefill"),
      (gate.POSITIVE, {"prefix_tokens_sent_to_successor": 7}, "prefix_replayed"),
      (gate.POSITIVE, {"acknowledged_before_failure": 2}, "watermark_not_equal_to_committed"),
      (gate.POSITIVE, {"duplicate_tokens": 1}, "duplicate_tokens"),
      (gate.POSITIVE, {"reference_parity": False}, "reference_parity"),
      (gate.POSITIVE, {"publishers": 2}, "publishers"),
      (gate.POSITIVE, {"cutover_ms": 9_000.0}, "cutover_latency"),
      (gate.POSITIVE, {"candidate_tpot_ms": 200.0}, "added_tpot"),
      (gate.POSITIVE, {"stale_source_publications_accepted": 1}, "stale_source_publication"),
      (gate.POSITIVE, {"ordinary_browser_request": False}, "not_an_ordinary_browser_request"),
      (gate.INCOMPATIBLE, {"kv_continuity_claimed": True}, "continuity_claim"),
      (gate.INCOMPATIBLE, {"successor_decode_operations": 2}, "fallback_used_kv_successor"),
      (gate.INCOMPATIBLE, {"outcome": "fenced_kv_successor"}, "outcome"),
      (gate.POSITIVE, {"missing_tokens": 1}, "missing_tokens"),
      (gate.LAGGING, {"kv_continuity_claimed": True}, "continuity_claim"),
      (gate.LAGGING, {"outcome": "aborted"}, "outcome"),
      (gate.LAGGING, {"duplicate_tokens": 1}, "duplicate_tokens"),
      (gate.INCOMPATIBLE, {"terminal_results": 2}, "terminal_results"),
      (gate.ABORT, {"terminal_results": 2}, "terminal_results"),
      (gate.ABORT, {"successor_decode_operations": 1}, "abort_successor_work"),
      (gate.POSITIVE, {"workspaces_checked": ["inference"]}, "workspaces"),
   ],
)
def test_every_frozen_predicate_fails_closed(case, override, failure) -> None:
   plan = gate.build_plan(CANDIDATE)
   verdict = gate.evaluate(plan, _records(plan, **{case: override}))
   assert verdict["passed"] is False
   assert failure in verdict["cases"][case]


def test_unbound_tampered_missing_or_extra_records_fail() -> None:
   plan = gate.build_plan(CANDIDATE)
   other = gate.build_plan("b" * 40)
   records = _records(plan)
   records[gate.POSITIVE] = _record(other, gate.POSITIVE)
   assert gate.evaluate(plan, records)["cases"][gate.POSITIVE] == ["record_not_bound_to_plan"]
   records = _records(plan)
   records[gate.POSITIVE] = dict(records[gate.POSITIVE], cutover_ms=1.0)
   assert gate.evaluate(plan, records)["cases"][gate.POSITIVE] == ["record_digest"]
   records = _records(plan)
   del records[gate.ABORT]
   assert gate.evaluate(plan, records)["cases"][gate.ABORT] == ["record_missing"]
   records = _records(plan)
   bad_cleanup = {"node-0": {"active_state_count_delta": 1, "active_kv_bytes_delta": 0, "shadow_bytes_delta": 0, "reservation_delta": 0, "fence_records_delta": 0}}
   records[gate.POSITIVE] = _record(plan, gate.POSITIVE, cleanup=bad_cleanup)
   assert "terminal_resource_delta" in gate.evaluate(plan, records)["cases"][gate.POSITIVE]


def test_execute_refuses_without_the_fleet_grant(tmp_path: Path, monkeypatch) -> None:
   plan_path = tmp_path / "plan.json"
   assert gate.main(["--plan-out", str(plan_path), "--candidate", CANDIDATE]) == 0
   assert plan_path.stat().st_mode & 0o777 == 0o600
   monkeypatch.delenv("MYCELIUM_A7_FLEET_GRANT", raising=False)
   assert gate.main(["--execute", "--plan", str(plan_path), "--records", str(tmp_path / "r"), "--case-runner", "/bin/false"]) == 2
   assert gate.main(["--evaluate", "--plan", str(plan_path), "--records", str(tmp_path / "r")]) == 1


# Owner ruling (option C): on the two-Mac fleet A6 has no successor after an A7
# fallback, so the fallback cases judge the A7 decision and its A6 handoff only.
_A6_SUCCESSOR_UNAVAILABLE = {"reference_parity": False, "missing_tokens": 29}


@pytest.mark.parametrize(
   ("case", "reason"),
   [
      (gate.INCOMPATIBLE, "compatibility_incompatible"),
      (gate.LAGGING, "route_watermark_unavailable"),
      (gate.LAGGING, "watermark_behind_gateway"),
   ],
)
def test_fallback_passes_on_the_right_decision_when_a6_has_no_successor(case, reason) -> None:
   plan = gate.build_plan(CANDIDATE)
   records = _records(plan, **{case: {**_A6_SUCCESSOR_UNAVAILABLE, "reason": reason}})
   verdict = gate.evaluate(plan, records)
   assert verdict["cases"][case] == []
   assert verdict["passed"] is True, verdict["cases"]
   # The verdict states its scope and reports (without judging) what A6 delivered.
   assert verdict["fallback_scope"]["cases"] == [gate.INCOMPATIBLE, gate.LAGGING]
   assert "not_proven_by_this_run" in verdict["fallback_scope"]["a6_recovery"]
   assert "A6 recovery is proven by the A6 recovery gate, not by this run" in verdict["claim_boundary"]
   assert verdict["fallback_a6_replay_observed"][case] == {"judged": False, "reference_parity": False, "missing_tokens": 29}


@pytest.mark.parametrize(
   ("case", "override", "failure"),
   [
      (gate.INCOMPATIBLE, {"reason": "route_watermark_unavailable"}, "fallback_reason"),
      (gate.INCOMPATIBLE, {"reason": "qualification_not_current"}, "fallback_reason"),
      (gate.LAGGING, {"reason": "compatibility_incompatible"}, "fallback_reason"),
      (gate.LAGGING, {"reason": "fence_not_installed"}, "fallback_reason"),
      (gate.INCOMPATIBLE, {"kv_continuity_claimed": True}, "continuity_claim"),
      (gate.LAGGING, {"kv_continuity_claimed": True}, "continuity_claim"),
      (gate.INCOMPATIBLE, {"outcome": "aborted"}, "outcome"),
      (gate.LAGGING, {"successor_decode_operations": 1}, "fallback_used_kv_successor"),
   ],
)
def test_fallback_with_a6_unavailable_still_fails_a_wrong_a7_decision(case, override, failure) -> None:
   plan = gate.build_plan(CANDIDATE)
   verdict = gate.evaluate(plan, _records(plan, **{case: {**_A6_SUCCESSOR_UNAVAILABLE, **override}}))
   assert verdict["passed"] is False
   assert failure in verdict["cases"][case]


def test_positive_and_abort_criteria_are_not_scoped() -> None:
   plan = gate.build_plan(CANDIDATE)
   verdict = gate.evaluate(plan, _records(plan, **{gate.POSITIVE: _A6_SUCCESSOR_UNAVAILABLE}))
   assert {"reference_parity", "missing_tokens"} <= set(verdict["cases"][gate.POSITIVE])
   # The abort case keeps its own criteria: its reason is not judged and A6 never runs.
   verdict = gate.evaluate(plan, _records(plan, **{gate.ABORT: {"successor_decode_operations": 1, "reason": "compatibility_incompatible"}}))
   assert verdict["cases"][gate.ABORT] == ["abort_successor_work"]


def test_plan_freezes_the_fallback_scope() -> None:
   plan = gate.build_plan(CANDIDATE)
   assert plan["fallback_scope"]["expected_reasons"][gate.INCOMPATIBLE] == ["compatibility_incompatible"]
   widened = json.loads(json.dumps(plan))
   widened["fallback_scope"]["expected_reasons"][gate.INCOMPATIBLE].append("fence_not_installed")
   body = {key: value for key, value in widened.items() if key != "plan_digest"}
   widened["plan_digest"] = gate._sha(body)
   with pytest.raises(ValueError, match="a7_gate_plan_scope_changed"):
      gate.evaluate(widened, {})
   unscoped = {key: value for key, value in plan.items() if key not in {"fallback_scope", "plan_digest"}}
   unscoped["plan_digest"] = gate._sha(unscoped)
   with pytest.raises(ValueError, match="a7_gate_plan_scope_changed"):
      gate.evaluate(unscoped, {})
