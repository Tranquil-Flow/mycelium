# SPDX-License-Identifier: AGPL-3.0-only
"""A10 qualification decided from executed observations.

The verifier matrix is executed for real on the tiny offline MLX target (102
operations, three width classes, two prefix buckets, every transaction mode and the
four required negatives) and decided by ``qualify_target_verifier``.  Because that
run is a direct runtime call with fixture provenance, the honest decision is
``not_qualified`` with exactly the provenance and path blockers, while the
correctness matrix itself passes.  The deterministic cases then drive the deciders
with crafted records to pin every blocker, including the one path that emits a
capability: live evidence through the product dispatcher.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import copy

import pytest

from mycelium_router.a10_live_publication import StageComputePublication
from mycelium_router.a10_qualification import (
   REQUIRED_RUNTIME_BATCH_NEGATIVES,
   RESIDUAL_RESOURCE_FIELDS,
   RUNTIME_BATCH_EVIDENCE_PROTOCOL,
   canonical_digest,
   count_late_arrivals,
)
from mycelium_router.a10_runtime_batch import (
   ACCEPTANCE_INVENTORY_DIGEST,
   A10NotImplementedError,
   QualificationRequest,
   RuntimeBatchObservation,
   qualify_runtime_batch,
)
from mycelium_router.a10_target_verification import (
   TargetVerificationQualificationRequest,
   qualify_target_verifier,
)
from mycelium_router.a10_verifier_matrix import MatrixPlan, run_verifier_matrix
from mycelium_router.contracts import RuntimeResult
from tests.a10_acceptance.benchmark_amendment import protocol_digest
from tests.a10_acceptance.test_benchmark_amendment import _run
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401

RUNTIME = "sha256:" + "1" * 64
MODEL = "sha256:" + "2" * 64
REPRESENTATION = "sha256:" + "3" * 64
PROVENANCE_BLOCKERS = ["direct_runtime_call_cannot_qualify", "non_live_provenance_cannot_qualify"]


@pytest.fixture(scope="module")
def executed_matrix(mlx_case):
   stages = mlx_case.ports[0].a10_verifier_stages() + mlx_case.ports[1].a10_verifier_stages()

   def open_verifier(prefix, ref):
      return mlx_case.ports[0].begin_target_verifier(prefix, verification_ref=ref, stages=stages)

   def cancel(verifier):
      for port in mlx_case.ports:
         port.cancel(verifier.path_id)

   plan = MatrixPlan(
      widths={"one": 1, "qualified_default": 3, "qualified_maximum": 6},
      prefix_buckets={"short": (2, 3), "long": (7, 8)},
      operations=102,
      vocab_size=stages[0].vocab_size,
   )
   return run_verifier_matrix(
      plan,
      open_verifier=open_verifier,
      cancel_verification=cancel,
      runtime_digest=RUNTIME,
      model_digest=MODEL,
      representation_digest=REPRESENTATION,
      backend="mlx",
      provenance="fixture",
      execution_path="direct_runtime",
   )


def _verifier_request(operations, **overrides):
   values = {
      "owner_private_root": "owner-private/a10",
      "runtime_digest": RUNTIME,
      "width_classes": ("one", "qualified_default", "qualified_maximum"),
      "prefix_length_buckets": 2,
      "operations": 100,
      "sequential_parity_proof_digest": canonical_digest(operations),
   }
   values.update(overrides)
   return TargetVerificationQualificationRequest(**values)


def test_the_executed_mlx_matrix_passes_but_cannot_qualify_from_a_direct_call(executed_matrix):
   operations, negatives = executed_matrix
   bundle = qualify_target_verifier(_verifier_request(operations), operations, negatives)

   assert bundle["matrix_passed"] is True
   assert bundle["decision"] == "not_qualified"
   assert bundle["blockers"] == PROVENANCE_BLOCKERS
   assert bundle["capability"] is None
   assert bundle["operation_count"] == 102
   assert bundle["parity_failures"] == 0
   assert bundle["width_limits"] == {"one": 1, "qualified_default": 3, "qualified_maximum": 6}
   assert bundle["prefix_length_buckets"] == ["long", "short"]
   assert set(bundle["transaction_mode_counts"]) == {
      "rollback_to_prefix",
      "commit_verified_prefix",
      "commit_verified_prefix_plus_target_correction",
   }
   assert bundle["correction_operations"] > 0
   assert bundle["negatives"] == {
      "cancellation_during_verification": "failed_closed",
      "position_bound": "failed_closed",
      "stale_prefix": "failed_closed",
      "unsupported_generation_policy": "failed_closed",
   }


def _as_live_product_path(operations):
   records = copy.deepcopy(operations)
   for record in records:
      record["provenance"] = "live"
      record["execution_path"] = "product_dispatcher"
   return records


def test_only_live_product_path_records_emit_a_bound_capability(executed_matrix):
   operations, negatives = executed_matrix
   records = _as_live_product_path(operations)
   bundle = qualify_target_verifier(_verifier_request(records), records, negatives)

   assert bundle["decision"] == "qualified"
   assert bundle["blockers"] == []
   capability = bundle["capability"]
   assert capability["maximum_positions"] == 6
   assert capability["qualification_binding"] == bundle["qualification_binding"]
   assert capability["generation_policy"] == "deterministic_greedy"


@pytest.mark.parametrize(
   ("mutate", "blocker"),
   [
      (lambda ops, neg: ops[5].update(parity=False), "sequential_parity_failure"),
      (lambda ops, neg: ops[7].update(mismatched_offset_count=1), "sequential_parity_failure"),
      (lambda ops, neg: ops[9].update(cleanup="leaked"), "verifier_cleanup_incomplete"),
      (lambda ops, neg: ops[11].update(runtime_digest="sha256:" + "9" * 64), "mixed_runtime_digest"),
      (lambda ops, neg: del_tail(ops, 3), "insufficient_qualification_operations"),
      (lambda ops, neg: [op.update(prefix_bucket="short") for op in ops], "insufficient_prefix_length_buckets"),
      (lambda ops, neg: [op.update(transaction_mode="commit_verified_prefix") for op in ops if op["transaction_mode"] == "rollback_to_prefix"], "transaction_mode_not_exercised:rollback_to_prefix"),
      (lambda ops, neg: neg.pop(), "verifier_negative_not_passed:cancellation_during_verification"),
      (lambda ops, neg: neg[0].update(prefix_unchanged=False), "verifier_negative_not_passed:stale_prefix"),
   ],
)
def test_every_matrix_gap_blocks_qualification(executed_matrix, mutate, blocker):
   operations, negatives = executed_matrix
   records = _as_live_product_path(operations)
   negative_records = copy.deepcopy(negatives)
   mutate(records, negative_records)
   bundle = qualify_target_verifier(_verifier_request(records), records, negative_records)

   assert bundle["decision"] == "not_qualified"
   assert blocker in bundle["blockers"]
   assert bundle["capability"] is None


def del_tail(records, count):
   del records[-count:]


def test_a_request_bound_to_other_operations_is_blocked(executed_matrix):
   operations, negatives = executed_matrix
   records = _as_live_product_path(operations)
   request = _verifier_request(records, sequential_parity_proof_digest="sha256:" + "0" * 64)
   bundle = qualify_target_verifier(request, records, negatives)
   assert "sequential_parity_proof_digest_mismatch" in bundle["blockers"]


def test_qualification_without_executed_evidence_still_refuses(executed_matrix):
   operations, _ = executed_matrix
   with pytest.raises(A10NotImplementedError):
      qualify_target_verifier(_verifier_request(operations))
   with pytest.raises(A10NotImplementedError):
      qualify_runtime_batch(_batch_request())


# ---------------------------------------------------------------------------
# Runtime batch qualification
# ---------------------------------------------------------------------------


def _batch_request():
   return QualificationRequest(
      owner_private_root="owner-private/a10",
      benchmark_manifest_digest="sha256:" + "4" * 64,
      acceptance_inventory_digest=ACCEPTANCE_INVENTORY_DIGEST,
      baseline_windows=16,
      candidate_windows=16,
      valid_pairs=12,
   )


def _item(request_id, placement_id, phase, token_index, *, emits_token=False, payload=b"\x01"):
   from types import SimpleNamespace

   return SimpleNamespace(
      request_id=request_id,
      path_attempt=0,
      phase=phase,
      token_index=token_index,
      placement_id=placement_id,
      emits_token=emits_token,
      payload=payload,
   )


def _live_publications(case):
   """Two stages; request-b arrives after request-a's decode began and joins its batch."""

   recorders = [
      StageComputePublication(
         node_id=f"node-{index}", graph=case.graph, clock=lambda: 0.0, wall_clock=lambda: 50.0
      )
      for index in range(2)
   ]
   placements = [stage.placements[0].placement_id for stage in case.graph.stages]
   spans = [
      # request, stage, phase, token_index, start, finish, batch
      ("request-a", 0, "PREFILL", -1, 0.00, 0.05, ""),
      ("request-a", 1, "PREFILL", -1, 0.05, 0.10, ""),
      ("request-a", 0, "DECODE", 0, 0.10, 0.15, ""),
      ("request-a", 1, "DECODE", 0, 0.15, 0.25, ""),
      ("request-b", 0, "PREFILL", -1, 0.16, 0.24, ""),
      ("request-b", 1, "PREFILL", -1, 0.26, 0.30, ""),
      ("request-a", 0, "DECODE", 1, 0.31, 0.40, "batch-1"),
      ("request-b", 0, "DECODE", 0, 0.31, 0.40, "batch-1"),
      ("request-a", 1, "DECODE", 1, 0.41, 0.50, "batch-2"),
      ("request-b", 1, "DECODE", 0, 0.41, 0.50, "batch-2"),
   ]
   for request_id, stage, phase, token, start, finish, batch in spans:
      final = stage == 1
      activation = f"{request_id}:{phase}:{token}".encode()
      recorders[stage].record(
         _item(request_id, placements[stage], phase, token, emits_token=final, payload=activation),
         started_at=start,
         finished_at=finish,
         result=RuntimeResult(success=True, payload=None if final else activation, token_id=1 if final else None),
         runtime_batch_ref=batch,
      )
   batch = RuntimeBatchObservation(
      batch_ref="batch-1",
      phase="DECODE",
      stage=case.graph.stages[0].stage_id,
      backend_invocation_id="invocation:batch-1:1",
      backend_invocations=1,
      membership_authority="stage_runtime",
      proposed_member_count=2,
      actual_member_count=2,
      member_outcomes=(("member-a", "completed"), ("member-b", "completed")),
      collected_at=0.30,
      executed_at=0.40,
      cleanup="released",
   )
   return [recorders[0].snapshot(batch_observations=(batch,)), recorders[1].snapshot()]


def _batch_evidence(case, **overrides):
   evidence = {
      "protocol": RUNTIME_BATCH_EVIDENCE_PROTOCOL,
      "provenance": "live",
      "execution_path": "product_dispatcher",
      "benchmark_manifest_digest": "sha256:" + "4" * 64,
      "acceptance_inventory_digest": ACCEPTANCE_INVENTORY_DIGEST,
      "publications": _live_publications(case),
      "cross_host_clock_uncertainty": 0.001,
      "benchmark_run": _run([0.12] * 16),
      "latency_ms": {
         "baseline_interactive_p95_ttft": 900.0,
         "candidate_interactive_p95_ttft": 950.0,
         "baseline_interactive_p95_tpot": 180.0,
         "candidate_interactive_p95_tpot": 190.0,
      },
      "reliability": {
         "baseline": {"terminal_error_rate": 0.0, "timeout_rate": 0.0, "cancellation_rate": 0.01, "cleanup_success_rate": 1.0},
         "candidate": {"terminal_error_rate": 0.0, "timeout_rate": 0.0, "cancellation_rate": 0.01, "cleanup_success_rate": 1.0},
      },
      "output_parity": {"compared_requests": 960, "mismatched_requests": 0},
      "residual_resources": {field: 0 for field in RESIDUAL_RESOURCE_FIELDS},
      "negatives": {
         case_id: {"outcome": "failed_closed_within_scope", "residue": 0}
         for case_id in REQUIRED_RUNTIME_BATCH_NEGATIVES
      },
   }
   evidence.update(overrides)
   return evidence


def test_live_evidence_qualifies_the_runtime_batch(mlx_case):
   bundle = qualify_runtime_batch(_batch_request(), _batch_evidence(mlx_case))

   assert bundle["decision"] == "qualified", bundle["blockers"]
   assert bundle["observed_runtime_batches"] == 1
   assert bundle["late_arrivals"] == 1
   assert bundle["overlap"]["observed"] is True
   assert bundle["throughput"]["decision"] == "material"


def test_fixture_provenance_is_decided_but_blocked(mlx_case):
   bundle = qualify_runtime_batch(
      _batch_request(),
      _batch_evidence(mlx_case, provenance="fixture", execution_path="direct_runtime"),
   )
   assert bundle["evidence_passed"] is True
   assert bundle["decision"] == "not_qualified"
   assert bundle["blockers"] == PROVENANCE_BLOCKERS


def _without_batches(case):
   publications = _live_publications(case)
   publications[0]["runtime_batch_observations"] = []
   return publications


@pytest.mark.parametrize(
   ("overrides", "blocker"),
   [
      (lambda case: {"publications": _without_batches(case)}, "no_observed_runtime_batch"),
      (lambda case: {"cross_host_clock_uncertainty": 0.2}, "pipeline_overlap_not_observed"),
      (lambda case: {"benchmark_run": _run([0.05] * 16)}, "throughput_not_material"),
      (
         lambda case: {"latency_ms": {
            "baseline_interactive_p95_ttft": 900.0,
            "candidate_interactive_p95_ttft": 1000.0,
            "baseline_interactive_p95_tpot": 180.0,
            "candidate_interactive_p95_tpot": 190.0,
         }},
         "interactive_p95_ttft_regression_exceeds_bound",
      ),
      (lambda case: {"output_parity": {"compared_requests": 960, "mismatched_requests": 1}}, "output_parity_failed"),
      (
         lambda case: {"residual_resources": {**{field: 0 for field in RESIDUAL_RESOURCE_FIELDS}, "kv_states": 1}},
         "residual_resources_nonzero",
      ),
      (
         lambda case: {"negatives": {"timeout": {"outcome": "failed_closed_within_scope", "residue": 0}}},
         "runtime_batch_negative_not_passed:slow_consumer",
      ),
      (lambda case: {"benchmark_manifest_digest": "sha256:" + "5" * 64}, "benchmark_manifest_digest_mismatch"),
   ],
)
def test_every_runtime_batch_gap_blocks_qualification(mlx_case, overrides, blocker):
   bundle = qualify_runtime_batch(_batch_request(), _batch_evidence(mlx_case, **overrides(mlx_case)))
   assert bundle["decision"] == "not_qualified"
   assert blocker in bundle["blockers"]


def test_late_arrival_needs_a_shared_runtime_batch(mlx_case):
   publications = _live_publications(mlx_case)
   assert count_late_arrivals(publications) == 1
   for publication in publications:
      for interval in publication["intervals"]:
         interval["runtime_batch_ref"] = ""
   assert count_late_arrivals(publications) == 0


def test_the_benchmark_run_is_bound_to_the_frozen_v2_protocol():
   assert _run([0.12] * 16)["benchmark_protocol_digest"] == protocol_digest()
