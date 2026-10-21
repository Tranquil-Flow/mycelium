#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed A7 fenced KV-successor physical gate coordinator.

Three modes, mirroring the A6 coordinator:

``--plan OUT``
    Network-free preparation.  Writes the gate plan for one exact candidate:
    the required cases, the owner topology (R7), the pass thresholds and the
    fallback cases' scope (``FALLBACK_SCOPE``: A7 decision and A6 handoff only),
    frozen and digest-bound *before* any candidate measurement (spec section
    11).  The plan digest is bound into every case record.

``--evaluate --plan PLAN --records DIR``
    Offline verdict over measured case records.  Every record must be closed,
    bound to the plan digest and candidate, and meet its case's frozen
    predicate; a missing case, an unknown field, a widened claim or an invented
    success value fails the gate.

``--execute --plan PLAN --records DIR --case-runner ARGV...``
    Runs each case through an owner-supplied case runner (one subprocess per
    case, which drives the ordinary browser request and the physical fault and
    writes exactly one record), then evaluates.  Refuses without the exact
    fleet grant phrase in ``MYCELIUM_A7_FLEET_GRANT``.

Nothing here simulates a case: a record exists only when a runner measured it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

EXACT_FLEET_GRANT = "FLEET GRANTED TO A7"
PLAN_PROTOCOL = "mycelium.a7_kv_successor_gate_plan.v1"
CASE_PROTOCOL = "mycelium.a7_physical_case.v1"
VERDICT_PROTOCOL = "mycelium.a7_kv_successor_gate_verdict.v1"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")

# LEGACY fleet topology (owner ruling R7, two Macs): kept so the old R7 run-day
# sheets reproduce byte-identical plans.  New runs pass ``--role-binding``: a
# verified binding of the pinned A7 requirements spec
# (``mycelium_gate_requirements/specs/a7.v1.json``) supplies the entry, source
# and standby members from whatever eligible devices the seed holds.
LEGACY_TOPOLOGY = {
   "entry_node": "node-0",
   "source_node": "node-2",
   "standby_node": "node-0",
   "covered_stage_index": 1,
   "standby_backend": "mlx",
   "fallback_incompatibility": ("dtype_mismatch", "numpy_runtime"),
}
TOPOLOGY = LEGACY_TOPOLOGY

POSITIVE = "ordinary_browser_source_kill_kv_successor"
INCOMPATIBLE = "incompatible_standby_falls_back_to_a6"
LAGGING = "lagging_standby_falls_back_to_a6"
ABORT = "no_a6_successor_aborts"
CASES = (POSITIVE, INCOMPATIBLE, LAGGING, ABORT)

# Owner ruling (option C, 2026-10-06): on the R7 two-Mac fleet the A6 replay
# that follows an A7 fallback has no successor.  Each host runs one node
# process with exactly one ACTIVE placement, so both ACTIVE placements are on
# the source path, which A6 excludes in full.  The two fallback cases therefore
# judge the A7 part only: the right fallback decision for the case's reason,
# no continuity claim, no KV-successor work, and the recorded handoff to A6
# (outcome ``full_context_replay``, which the entry emits only when it hands
# the request to A6).  Whether A6 then completes the request (reference parity,
# no missing tokens) is not judged here; the A6 recovery gate proves it.
FALLBACK_SCOPE: dict[str, Any] = {
   "cases": [INCOMPATIBLE, LAGGING],
   "judged": "a7_fallback_decision_and_a6_handoff",
   "expected_reasons": {
      INCOMPATIBLE: ["compatibility_incompatible"],
      # A lagging standby either never acknowledged (no route watermark) or
      # acknowledged less than the gateway committed.
      LAGGING: ["route_watermark_unavailable", "watermark_behind_gateway"],
   },
   "handoff_outcome": "full_context_replay",
   "not_judged": ["reference_parity", "missing_tokens"],
   "a6_recovery": "not_proven_by_this_run; proven by the A6 recovery gate (scripts/run_a6_recovery_gate.py)",
   "fleet_limit": (
      "R7 two-Mac fleet: one ACTIVE placement per node and one node per host, so no "
      "ACTIVE placement exists outside the source path that A6 excludes"
   ),
}

# Frozen before candidate measurement.  Changing any value changes the plan
# digest, so a record measured under one plan can never pass another.
THRESHOLDS: dict[str, Any] = {
   "min_committed_tokens_before_failure": 2,
   "max_duplicate_tokens": 0,
   "max_missing_tokens": 0,
   "exact_publishers": 1,
   "exact_terminal_results": 1,
   "max_successor_recovery_prefill_operations": 0,
   "max_prefix_tokens_sent_to_successor": 0,
   "max_interruption_ms": 15_000,
   "max_cutover_ms": 5_000,
   "max_ack_wait_ms": 2_000,
   "max_checkpoint_bytes_per_committed_token": 8 * 1024 * 1024,
   "max_added_tpot_fraction": 0.5,
   "max_terminal_resource_delta": 0,
   "required_workspaces": (
      "inference",
      "device_lab",
      "network",
      "nodes",
      "plans",
      "readiness",
      "incidents",
      "settings",
   ),
}

_CLEANUP_FIELDS = frozenset({"active_state_count_delta", "active_kv_bytes_delta", "shadow_bytes_delta", "reservation_delta", "fence_records_delta"})
_CLEANUP_NOT_MEASURED_FIELDS = frozenset({"cleanup_measurement", "cleanup_unmeasured_reason", "cleanup_proof", "cleanup_proof_evidence"})
PROCESS_EXIT_PROOF = "process_exit"
PROCESS_EXIT_SOURCE = "retired_route_session_exit_status"
LOCAL_PROCESS_TRANSPORT = "local"
_EXIT_RECORD_FIELDS = frozenset({"host_id", "process_id", "process_transport", "exit_status", "observed_by"})
_PROCESS_EXIT_EVIDENCE_FIELDS = frozenset(
   {"old_entry_process", "old_entry_exit", "replacement_entry_process", "replacement_terminal_state", "old_entry_process_live"}
)
_REPLACEMENT_STATE_FIELDS = frozenset(
   {
      "active_state_count",
      "active_kv_bytes",
      "shadow_kv_bytes",
      "shadow_checkpoint_count",
      "entry_fence_record_count",
      "route_request_registrations",
      "gateway_reservations",
      "request_live",
   }
)
_CASE_FIELDS = frozenset(
   {
      "protocol",
      "case",
      "candidate_commit",
      "plan_digest",
      "ordinary_browser_request",
      "fault",
      "outcome",
      "reason",
      "kv_continuity_claimed",
      "committed_before_failure",
      "acknowledged_before_failure",
      "successor_recovery_prefill_operations",
      "prefix_tokens_sent_to_successor",
      "reference_parity",
      "duplicate_tokens",
      "missing_tokens",
      "publishers",
      "terminal_results",
      "successor_decode_operations",
      "stale_source_publications_accepted",
      "interruption_ms",
      "cutover_ms",
      "checkpoint_bytes_total",
      "checkpoint_count",
      "max_ack_wait_ms",
      "baseline_tpot_ms",
      "candidate_tpot_ms",
      "cleanup",
      "workspaces_checked",
      "evidence_digest",
   }
)
_EXPECTED_OUTCOME = {
   POSITIVE: "fenced_kv_successor",
   INCOMPATIBLE: "full_context_replay",
   LAGGING: "full_context_replay",
   ABORT: "aborted",
}
_EXPECTED_FAULT = {
   POSITIVE: "source_process_kill",
   INCOMPATIBLE: "source_process_kill",
   LAGGING: "source_process_kill",
   ABORT: "source_process_kill",
}


def _fail(code: str) -> None:
   raise ValueError(code)


def _canonical(value: Any) -> bytes:
   return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
   return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _bound_topology(role_binding: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
   """Entry/source/standby from a verified binding of the pinned A7 spec."""

   root = str(Path(__file__).resolve().parents[1])
   if root not in sys.path:
      sys.path.insert(0, root)
   from mycelium_gate_requirements.binding import (
      role_device_classes,
      role_node_ids,
      verify_binding,
   )
   from mycelium_gate_requirements.spec import load_pinned_spec

   spec = load_pinned_spec("A7")
   binding = verify_binding(role_binding, spec=spec)
   nodes = {role: role_node_ids(binding, role) for role in ("entry", "source", "standby")}
   if any(len(value) != 1 for value in nodes.values()):
      _fail("a7_gate_role_binding_incomplete")
   topology = {
      **LEGACY_TOPOLOGY,
      "entry_node": nodes["entry"][0],
      "source_node": nodes["source"][0],
      "standby_node": nodes["standby"][0],
   }
   extra = {
      "requirements_spec_digest": spec.digest,
      "role_binding_digest": binding["binding_digest"],
      "device_classes": role_device_classes(binding),
   }
   return topology, extra


def build_plan(candidate_commit: str, *, role_binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
   if not isinstance(candidate_commit, str) or COMMIT.fullmatch(candidate_commit) is None:
      _fail("a7_gate_candidate_invalid")
   topology, extra = (LEGACY_TOPOLOGY, {}) if role_binding is None else _bound_topology(role_binding)
   body = {
      "protocol": PLAN_PROTOCOL,
      "candidate_commit": candidate_commit,
      "topology": topology,
      **extra,
      "cases": list(CASES),
      "thresholds": THRESHOLDS,
      "fallback_scope": FALLBACK_SCOPE,
      "claim_boundary": (
         "A7 physical gate plan only: thresholds are frozen before measurement; "
         "no case is simulated and no qualification or promotion claim is made by the plan."
      ),
   }
   body = json.loads(_canonical(body))
   return {**body, "plan_digest": _sha(body)}


def _verify_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
   if not isinstance(plan, Mapping) or plan.get("protocol") != PLAN_PROTOCOL:
      _fail("a7_gate_plan_invalid")
   body = {key: value for key, value in plan.items() if key != "plan_digest"}
   if plan.get("plan_digest") != _sha(body):
      _fail("a7_gate_plan_digest_mismatch")
   if body.get("thresholds") != json.loads(_canonical(THRESHOLDS)) or body.get("cases") != list(CASES):
      # A plan from another threshold set is not this gate.
      _fail("a7_gate_plan_thresholds_changed")
   if body.get("fallback_scope") != json.loads(_canonical(FALLBACK_SCOPE)):
      # A plan frozen under another fallback scope is not this gate either.
      _fail("a7_gate_plan_scope_changed")
   return dict(plan)


def _int(value: Any, minimum: int = 0) -> bool:
   return type(value) is int and value >= minimum


def _number(value: Any) -> bool:
   return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value >= 0 and value != float("inf")


def _cleanup_not_measured(node: Any) -> bool:
   return (
      isinstance(node, Mapping)
      and set(node) == _CLEANUP_NOT_MEASURED_FIELDS
      and node["cleanup_measurement"] == "not_measured"
      and isinstance(node["cleanup_unmeasured_reason"], str)
      and bool(node["cleanup_unmeasured_reason"])
      and node["cleanup_proof"] in (None, PROCESS_EXIT_PROOF)
      and (node["cleanup_proof_evidence"] is None or isinstance(node["cleanup_proof_evidence"], Mapping))
   )


def _process_identity(value: Any) -> bool:
   return (
      isinstance(value, Mapping)
      and set(value) == {"host_id", "process_id"}
      and isinstance(value["host_id"], str)
      and bool(value["host_id"])
      and type(value["process_id"]) is int
   )


def _process_exit_proof_failures(node: Mapping[str, Any]) -> list[str]:
   """Unmet conditions of the process-exit cleanup proof (empty = proof accepted).

   The A6 rule (``mycelium_live/router_port.py`` ``_dead_peer_cleanup_receipt``):
   process exit releases every resource that process held, so the
   identity-bound process, confirmed by its exit status, is its own cleanup
   proof; a process that is only unreachable proves nothing.  For a replaced
   A7 entry all four must hold: (1) the old entry process was observed before
   the kill; (2) that exact process was observed exited with an exit status
   from a local session (a remote launch only shows its client's exit);
   (3) the replacement entry (another process) reports a clean terminal
   resource state and does not hold the request; (4) nothing shows the old
   process still running.
   """

   evidence = node["cleanup_proof_evidence"]
   if (
      node["cleanup_proof"] != PROCESS_EXIT_PROOF
      or node["cleanup_unmeasured_reason"] != "entry_process_replaced"
      or not isinstance(evidence, Mapping)
   ):
      return ["terminal_resource_delta_not_measured"]
   if set(evidence) != _PROCESS_EXIT_EVIDENCE_FIELDS:
      return ["process_exit_evidence_invalid"]
   failures: list[str] = []
   old = evidence["old_entry_process"]
   if not _process_identity(old):
      failures.append("process_exit_old_process_unobserved")
      old = None
   exited = evidence["old_entry_exit"]
   if isinstance(exited, Mapping) and set(exited) != _EXIT_RECORD_FIELDS:
      return ["process_exit_evidence_invalid"]
   if isinstance(exited, Mapping) and (not isinstance(exited["process_transport"], str) or not exited["process_transport"]):
      return ["process_exit_evidence_invalid"]
   if not (
      old is not None
      and isinstance(exited, Mapping)
      and set(exited) == _EXIT_RECORD_FIELDS
      and exited["host_id"] == old["host_id"]
      and exited["process_id"] == old["process_id"]
      and type(exited["exit_status"]) is int
      and exited["observed_by"] == PROCESS_EXIT_SOURCE
   ):
      failures.append("process_exit_not_observed")
   elif exited["process_transport"] != LOCAL_PROCESS_TRANSPORT:
      # Only a local session's exit status is the node process's own exit; a
      # remote launch's status is its transport client's (no remote confirmation).
      failures.append("process_exit_remote_unconfirmed")
   replacement = evidence["replacement_entry_process"]
   if not _process_identity(replacement) or replacement == old:
      failures.append("process_exit_replacement_unobserved")
   state = evidence["replacement_terminal_state"]
   if not (
      isinstance(state, Mapping)
      and set(state) == _REPLACEMENT_STATE_FIELDS
      and state["request_live"] is False
      and all(type(state[name]) is int and state[name] == 0 for name in _REPLACEMENT_STATE_FIELDS - {"request_live"})
   ):
      failures.append("process_exit_replacement_not_clean")
   if evidence["old_entry_process_live"] is not False:
      failures.append("process_exit_old_process_live")
   return failures


def _cleanup_proof(record: Any) -> str | None:
   """Which cleanup proof a record carries: ``measured_delta``, ``process_exit`` or ``None``."""

   cleanup = record.get("cleanup") if isinstance(record, Mapping) else None
   if not isinstance(cleanup, Mapping) or not cleanup:
      return None
   proofs = {
      PROCESS_EXIT_PROOF if _cleanup_not_measured(node) and node["cleanup_proof"] == PROCESS_EXIT_PROOF
      else "not_measured" if _cleanup_not_measured(node)
      else "measured_delta"
      for node in cleanup.values()
   }
   return proofs.pop() if len(proofs) == 1 else "mixed"


def evaluate_case(record: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
   """Every failed predicate of one case record (empty = case passes)."""

   failures: list[str] = []
   if not isinstance(record, Mapping) or set(record) != _CASE_FIELDS:
      return ["record_fields"]
   case = record["case"]
   if record["protocol"] != CASE_PROTOCOL or case not in CASES:
      return ["record_protocol_or_case"]
   body = {key: value for key, value in record.items() if key != "evidence_digest"}
   if record["evidence_digest"] != _sha(body):
      return ["record_digest"]
   if record["plan_digest"] != plan["plan_digest"] or record["candidate_commit"] != plan["candidate_commit"]:
      return ["record_not_bound_to_plan"]
   t = plan["thresholds"]
   integer_fields = (
      "committed_before_failure",
      "acknowledged_before_failure",
      "successor_recovery_prefill_operations",
      "prefix_tokens_sent_to_successor",
      "duplicate_tokens",
      "missing_tokens",
      "publishers",
      "terminal_results",
      "successor_decode_operations",
      "stale_source_publications_accepted",
      "checkpoint_bytes_total",
      "checkpoint_count",
   )
   if not all(_int(record[name]) for name in integer_fields):
      return ["record_counts_invalid"]
   if not all(_number(record[name]) for name in ("interruption_ms", "cutover_ms", "max_ack_wait_ms", "baseline_tpot_ms", "candidate_tpot_ms")):
      return ["record_measurements_invalid"]
   if record["ordinary_browser_request"] is not True:
      failures.append("not_an_ordinary_browser_request")
   if record["fault"] != _EXPECTED_FAULT[case]:
      failures.append("fault_not_observed")
   if record["outcome"] != _EXPECTED_OUTCOME[case]:
      failures.append("outcome")
   if record["kv_continuity_claimed"] is not (case == POSITIVE):
      failures.append("continuity_claim")
   if record["committed_before_failure"] < t["min_committed_tokens_before_failure"]:
      failures.append("committed_before_failure")
   if record["duplicate_tokens"] > t["max_duplicate_tokens"]:
      failures.append("duplicate_tokens")
   if record["terminal_results"] != t["exact_terminal_results"]:
      failures.append("terminal_results")
   if record["publishers"] > t["exact_publishers"]:
      failures.append("publishers")
   if record["stale_source_publications_accepted"] != 0:
      failures.append("stale_source_publication")
   cleanup = record["cleanup"]
   if not isinstance(cleanup, Mapping) or not cleanup:
      failures.append("terminal_resource_delta")
   else:
      for node in cleanup.values():
         if _cleanup_not_measured(node):
            # A replaced entry process has no measured delta.  Its cleanup is
            # accepted only by the verified process-exit proof, never as a delta.
            for code in _process_exit_proof_failures(node):
               if code not in failures:
                  failures.append(code)
         elif (
            not isinstance(node, Mapping)
            or set(node) != _CLEANUP_FIELDS
            or any(not _int(abs(value)) if type(value) is int else True for value in node.values())
            or any(abs(value) > t["max_terminal_resource_delta"] for value in node.values())
         ):
            if "terminal_resource_delta" not in failures:
               failures.append("terminal_resource_delta")
   if not isinstance(record["workspaces_checked"], list) or sorted(record["workspaces_checked"]) != sorted(t["required_workspaces"]):
      failures.append("workspaces")
   if case == ABORT:
      if record["successor_decode_operations"] != 0 or record["publishers"] > 1:
         failures.append("abort_successor_work")
      return failures
   scope = plan["fallback_scope"]
   if case in scope["cases"]:
      # The A7 decision only (FALLBACK_SCOPE): A6's replay is judged by the A6 gate.
      if record["reason"] not in scope["expected_reasons"][case]:
         failures.append("fallback_reason")
   else:
      if record["reference_parity"] is not True:
         failures.append("reference_parity")
      if record["missing_tokens"] > t["max_missing_tokens"]:
         failures.append("missing_tokens")
   if record["publishers"] != t["exact_publishers"]:
      failures.append("publishers")
   if case == POSITIVE:
      if record["acknowledged_before_failure"] != record["committed_before_failure"]:
         failures.append("watermark_not_equal_to_committed")
      if record["successor_recovery_prefill_operations"] > t["max_successor_recovery_prefill_operations"]:
         failures.append("recovery_prefill")
      if record["prefix_tokens_sent_to_successor"] > t["max_prefix_tokens_sent_to_successor"]:
         failures.append("prefix_replayed")
      if record["successor_decode_operations"] < 1:
         failures.append("successor_work")
      if record["interruption_ms"] > t["max_interruption_ms"]:
         failures.append("interruption")
      if record["cutover_ms"] > t["max_cutover_ms"]:
         failures.append("cutover_latency")
      if record["max_ack_wait_ms"] > t["max_ack_wait_ms"]:
         failures.append("ack_wait")
      if record["checkpoint_count"] < record["committed_before_failure"] or (
         record["checkpoint_bytes_total"]
         > t["max_checkpoint_bytes_per_committed_token"] * max(record["committed_before_failure"], 1)
      ):
         failures.append("checkpoint_cost")
      baseline = record["baseline_tpot_ms"]
      if baseline <= 0 or record["candidate_tpot_ms"] > baseline * (1 + t["max_added_tpot_fraction"]):
         failures.append("added_tpot")
   else:
      # Fallback: no KV-successor work and no continuity, ever.
      if record["successor_decode_operations"] != 0:
         failures.append("fallback_used_kv_successor")
   return failures


def _a6_replay_observed(record: Any) -> dict[str, Any] | None:
   if not isinstance(record, Mapping):
      return None
   return {
      "judged": False,
      "reference_parity": record.get("reference_parity"),
      "missing_tokens": record.get("missing_tokens"),
   }


def evaluate(plan: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
   verified = _verify_plan(plan)
   cases: dict[str, Any] = {}
   for case in CASES:
      record = records.get(case)
      cases[case] = ["record_missing"] if record is None else evaluate_case(record, verified)
   unexpected = sorted(set(records) - set(CASES))
   passed = not unexpected and all(not failures for failures in cases.values())
   verdict = {
      "protocol": VERDICT_PROTOCOL,
      "plan_digest": verified["plan_digest"],
      "candidate_commit": verified["candidate_commit"],
      "cases": cases,
      "unexpected_records": unexpected,
      "cleanup_proofs": {case: _cleanup_proof(records.get(case)) for case in CASES},
      "fallback_scope": verified["fallback_scope"],
      # Reported, never judged: what the A6 replay delivered after each fallback.
      "fallback_a6_replay_observed": {case: _a6_replay_observed(records.get(case)) for case in verified["fallback_scope"]["cases"]},
      "passed": passed,
      "claim_boundary": (
         "verdict over measured records only; promotion requires the owner's ledger step. "
         "The fallback cases prove the A7 fallback decision and its handoff to A6 only: "
         "A6 recovery is proven by the A6 recovery gate, not by this run."
      ),
   }
   return {**verdict, "verdict_digest": _sha(verdict)}


def _read_json(path: Path) -> Any:
   with path.open("rb") as handle:
      return json.loads(handle.read().decode("utf-8"))


def _load_records(directory: Path) -> dict[str, Any]:
   records: dict[str, Any] = {}
   for case in CASES:
      path = directory / f"{case}.json"
      if path.is_file() and not path.is_symlink():
         records[case] = _read_json(path)
   return records


def _write_private(path: Path, document: Any) -> None:
   path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
   with os.fdopen(descriptor, "wb") as handle:
      handle.write(_canonical(document) + b"\n")


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
   mode = parser.add_mutually_exclusive_group(required=True)
   mode.add_argument("--plan-out", type=Path)
   mode.add_argument("--evaluate", action="store_true")
   mode.add_argument("--execute", action="store_true")
   parser.add_argument("--candidate")
   parser.add_argument("--plan", type=Path)
   parser.add_argument("--records", type=Path)
   parser.add_argument("--verdict-out", type=Path)
   parser.add_argument(
      "--role-binding",
      type=Path,
      help="verified role binding of the pinned A7 requirements spec (default: legacy R7 fleet topology)",
   )
   parser.add_argument("--case-runner", nargs=argparse.REMAINDER)
   args = parser.parse_args(argv)
   try:
      if args.plan_out is not None:
         binding = _read_json(args.role_binding) if args.role_binding is not None else None
         _write_private(args.plan_out, build_plan(args.candidate or "", role_binding=binding))
         return 0
      if args.plan is None or args.records is None:
         _fail("a7_gate_plan_and_records_required")
      plan = _verify_plan(_read_json(args.plan))
      if args.execute:
         if os.environ.get("MYCELIUM_A7_FLEET_GRANT") != EXACT_FLEET_GRANT:
            _fail("a7_gate_fleet_grant_missing")
         runner = args.case_runner or []
         if not runner or not Path(runner[0]).is_absolute():
            _fail("a7_gate_case_runner_required")
         for case in CASES:
            output = args.records / f"{case}.json"
            if output.exists():
               _fail("a7_gate_record_exists")
            completed = subprocess.run(
               [*runner, "--case", case, "--plan", str(args.plan), "--record-out", str(output)],
               check=False,
               timeout=1800,
            )
            if completed.returncode != 0 or not output.is_file():
               print(json.dumps({"case": case, "runner_exit": completed.returncode}), file=sys.stderr)
      verdict = evaluate(plan, _load_records(args.records))
      if args.verdict_out is not None:
         _write_private(args.verdict_out, verdict)
      print(json.dumps({"passed": verdict["passed"], "cases": verdict["cases"]}, sort_keys=True))
      return 0 if verdict["passed"] else 1
   except (ValueError, OSError, subprocess.SubprocessError) as exc:
      print(json.dumps({"error": str(exc)}), file=sys.stderr)
      return 2


if __name__ == "__main__":
   raise SystemExit(main())
