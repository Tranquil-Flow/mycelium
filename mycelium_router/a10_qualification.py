# SPDX-License-Identifier: AGPL-3.0-only
"""A10 qualification decided from executed observations.

``qualify_target_verifier`` and ``qualify_runtime_batch`` (the machine-source entry
points in ``a10_target_verification`` / ``a10_runtime_batch``) delegate here once an
executed evidence set is supplied.  Without executed evidence they keep refusing
(``not_implemented:*_qualification_absent``): there is nothing to qualify.

Both deciders are pure functions of the evidence.  They never accept a caller's
verdict: every decision is recomputed from the per-operation records, the runtime-
owned live publications and the benchmark windows.  A decision is ``qualified``
only when the complete matrix passes AND every record carries ``live`` provenance
from the ordinary product path (``execution_path == "product_dispatcher"``).  The
specification (section 1) forbids a direct runtime call, fixture, deterministic
fake or qualification-only script from satisfying a verifier, runtime-batch,
overlap or performance claim, so such evidence is decided, reported and blocked:
``not_qualified`` with the exact blockers, never silently upgraded.

Outputs are owner-private bundles.  They carry counts, digests, widths and verdicts
only -- no token, prompt, output, logit, tensor or KV material.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NoReturn

from mycelium_router.a10_runtime_batch import (
   ACCEPTANCE_INVENTORY_DIGEST,
   MINIMUM_PHYSICAL_BATCH_MEMBERS,
   A10ContractError,
   QualificationRequest,
)
from mycelium_router.a10_target_verification import (
   MINIMUM_PREFIX_LENGTH_BUCKETS,
   MINIMUM_QUALIFIED_OPERATIONS,
   QUALIFIED_GENERATION_POLICIES,
   QUALIFIED_WIDTH_CLASSES,
   SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
   SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS,
   TRANSACTION_MODES,
   TargetVerificationCapability,
   TargetVerificationQualificationRequest,
)


VERIFIER_OPERATION_PROTOCOL = "mycelium.target_verification_operation_record.v1"
VERIFIER_QUALIFICATION_PROTOCOL = "mycelium.target_verification_qualification.v1"
RUNTIME_BATCH_EVIDENCE_PROTOCOL = "mycelium.runtime_batch_qualification_evidence.v1"
RUNTIME_BATCH_QUALIFICATION_PROTOCOL = "mycelium.runtime_batch_qualification.v1"

QUALIFYING_PROVENANCE = "live"
QUALIFYING_EXECUTION_PATH = "product_dispatcher"
EXECUTION_PATHS = ("product_dispatcher", "direct_runtime")
PROVENANCES = ("live", "replay", "fixture", "historical", "source")

#: Verifier negatives the correctness gate must execute, with the refusal each must
#: produce while leaving the committed prefix untouched and no transaction open.
REQUIRED_VERIFIER_NEGATIVES = {
   "stale_prefix": "stale_prefix",
   "unsupported_generation_policy": "unsupported_generation_policy",
   "position_bound": "position_bounds_exceeded",
   "cancellation_during_verification": "path_cancelled",
}

#: Runtime-batch physical negatives of specification section 10; each must fail
#: within its declared scope and leave zero residue.
REQUIRED_RUNTIME_BATCH_NEGATIVES = (
   "cancellation_isolation",
   "timeout",
   "queue_overflow",
   "slow_consumer",
   "incompatible_member",
   "stale_prefix",
   "partial_member_failure",
   "shared_backend_failure",
   "wrong_result_count",
   "runtime_exit",
   "shutdown",
   "verifier_rollback",
)
RESIDUAL_RESOURCE_FIELDS = (
   "queue_entries",
   "reservations",
   "command_handles",
   "packed_buffers",
   "kv_transactions",
   "kv_states",
   "receipts",
   "gateway_buffers",
)
MAXIMUM_LATENCY_REGRESSION_FRACTION = 0.10
RELIABILITY_FIELDS = (
   "terminal_error_rate",
   "timeout_rate",
   "cancellation_rate",
   "cleanup_success_rate",
)

VERIFIER_OPERATION_FIELDS = frozenset(
   {
      "protocol",
      "operation_ref",
      "provenance",
      "execution_path",
      "runtime_digest",
      "model_digest",
      "representation_digest",
      "runtime_incarnation",
      "backend",
      "generation_policy",
      "width_class",
      "width",
      "candidate_bytes",
      "prefix_bucket",
      "prefix_position",
      "transaction_mode",
      "accepted_prefix_count",
      "correction_count",
      "commit_state",
      "verifier_committed_position",
      "sequential_committed_position",
      "compared_count",
      "mismatched_offset_count",
      "prefix_position_matches",
      "commit_digest_matches",
      "parity",
      "cleanup",
   }
)
VERIFIER_NEGATIVE_FIELDS = frozenset(
   {"case_id", "refusal_code", "prefix_unchanged", "open_transactions"}
)


class A10QualificationError(A10ContractError):
   """A coded, fail-closed qualification-evidence refusal."""


def _reject(code: str) -> NoReturn:
   raise A10QualificationError(code)


def canonical_digest(value: object) -> str:
   encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
   return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _int(value: object, code: str, *, minimum: int = 0) -> int:
   if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
      _reject(code)
   return value


def _number(value: object, code: str, *, minimum: float = 0.0) -> float:
   if (
      isinstance(value, bool)
      or not isinstance(value, (int, float))
      or not math.isfinite(float(value))
      or float(value) < minimum
   ):
      _reject(code)
   return float(value)


def _digest_text(value: object, code: str) -> str:
   if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
      _reject(code)
   return value


# ---------------------------------------------------------------------------
# Target verifier
# ---------------------------------------------------------------------------


def verifier_operation_record(
   *,
   operation_ref: str,
   parity_result: Any,
   request: Any,
   width_class: str,
   prefix_bucket: str,
   runtime_digest: str,
   model_digest: str,
   representation_digest: str,
   backend: str,
   provenance: str,
   execution_path: str,
) -> dict[str, Any]:
   """Project one executed ``SequentialParityResult`` into its privacy-reduced record."""

   outcome = parity_result.outcome
   record = {
      "protocol": VERIFIER_OPERATION_PROTOCOL,
      "operation_ref": operation_ref,
      "provenance": provenance,
      "execution_path": execution_path,
      "runtime_digest": runtime_digest,
      "model_digest": model_digest,
      "representation_digest": representation_digest,
      "runtime_incarnation": request.runtime_incarnation,
      "backend": backend,
      "generation_policy": request.generation_policy,
      "width_class": width_class,
      "width": request.candidate_position_count,
      "candidate_bytes": request.candidate_bytes,
      "prefix_bucket": prefix_bucket,
      "prefix_position": request.committed_prefix_position,
      "transaction_mode": request.transaction_mode,
      "accepted_prefix_count": outcome.accepted_prefix_count,
      "correction_count": outcome.correction_count,
      "commit_state": outcome.commit_state,
      "verifier_committed_position": parity_result.verifier_committed_position,
      "sequential_committed_position": parity_result.sequential_committed_position,
      "compared_count": parity_result.compared_count,
      "mismatched_offset_count": len(parity_result.mismatched_offsets),
      "prefix_position_matches": parity_result.prefix_position_matches,
      "commit_digest_matches": parity_result.commit_digest_matches,
      "parity": parity_result.parity,
      "cleanup": outcome.cleanup,
   }
   return validate_verifier_operation(record)


def validate_verifier_operation(record: object) -> dict[str, Any]:
   if not isinstance(record, Mapping) or set(record) != VERIFIER_OPERATION_FIELDS:
      _reject("verifier_operation_fields_invalid")
   if record["protocol"] != VERIFIER_OPERATION_PROTOCOL:
      _reject("verifier_operation_protocol_invalid")
   if not isinstance(record["operation_ref"], str) or not record["operation_ref"]:
      _reject("verifier_operation_ref_invalid")
   if record["provenance"] not in PROVENANCES:
      _reject("verifier_operation_provenance_invalid")
   if record["execution_path"] not in EXECUTION_PATHS:
      _reject("verifier_operation_path_invalid")
   for field in ("runtime_digest", "model_digest", "representation_digest"):
      _digest_text(record[field], f"verifier_operation_{field}_invalid")
   for field in ("runtime_incarnation", "backend", "prefix_bucket"):
      if not isinstance(record[field], str) or not record[field]:
         _reject(f"verifier_operation_{field}_invalid")
   if record["generation_policy"] not in QUALIFIED_GENERATION_POLICIES:
      _reject("unsupported_generation_policy")
   if record["width_class"] not in QUALIFIED_WIDTH_CLASSES:
      _reject("verifier_operation_width_class_invalid")
   width = _int(record["width"], "verifier_operation_width_invalid", minimum=1)
   if width > SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS:
      _reject("verifier_operation_width_invalid")
   _int(record["candidate_bytes"], "verifier_operation_bytes_invalid", minimum=1)
   if record["candidate_bytes"] > SCAFFOLD_MAXIMUM_VERIFICATION_BYTES:
      _reject("verifier_operation_bytes_invalid")
   if record["transaction_mode"] not in TRANSACTION_MODES:
      _reject("verifier_operation_mode_invalid")
   for field in (
      "prefix_position",
      "accepted_prefix_count",
      "correction_count",
      "verifier_committed_position",
      "sequential_committed_position",
      "compared_count",
      "mismatched_offset_count",
   ):
      _int(record[field], f"verifier_operation_{field}_invalid")
   for field in ("prefix_position_matches", "commit_digest_matches", "parity"):
      if not isinstance(record[field], bool):
         _reject(f"verifier_operation_{field}_invalid")
   if record["accepted_prefix_count"] + record["correction_count"] > width:
      _reject("verifier_operation_commit_invalid")
   return dict(record)


def qualify_target_verifier_from_operations(
   request: TargetVerificationQualificationRequest,
   operations: Sequence[Mapping[str, Any]],
   negatives: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
   """Decide the verifier correctness matrix from executed operation records."""

   if not isinstance(request, TargetVerificationQualificationRequest):
      _reject("invalid_target_verification_qualification_argument")
   records = [validate_verifier_operation(record) for record in operations]
   blockers: list[str] = []
   if len(records) < MINIMUM_QUALIFIED_OPERATIONS or len(records) < request.operations:
      blockers.append("insufficient_qualification_operations")
   if len({record["operation_ref"] for record in records}) != len(records):
      _reject("duplicate_verifier_operation")
   operations_digest = canonical_digest(records)
   if operations_digest != request.sequential_parity_proof_digest:
      blockers.append("sequential_parity_proof_digest_mismatch")

   widths: dict[str, set[int]] = {}
   for record in records:
      widths.setdefault(record["width_class"], set()).add(record["width"])
   width_limits: dict[str, int | None] = {}
   for width_class in QUALIFIED_WIDTH_CLASSES:
      observed = widths.get(width_class, set())
      width_limits[width_class] = observed.pop() if len(observed) == 1 else None
      if len(widths.get(width_class, ())) > 1:
         blockers.append(f"inconsistent_width_class:{width_class}")
   if width_limits.get("one") not in (None, 1):
      blockers.append("width_class_one_is_not_one")
   default = width_limits.get("qualified_default")
   maximum = width_limits.get("qualified_maximum")
   if None in width_limits.values():
      blockers.append("width_class_not_covered")
   elif not (1 <= width_limits["one"] <= default <= maximum):
      blockers.append("width_classes_not_ordered")

   buckets: dict[str, set[str]] = {}
   for record in records:
      buckets.setdefault(record["prefix_bucket"], set()).add(record["width_class"])
   full_buckets = [
      bucket for bucket, classes in buckets.items() if classes == set(QUALIFIED_WIDTH_CLASSES)
   ]
   if len(full_buckets) < max(MINIMUM_PREFIX_LENGTH_BUCKETS, request.prefix_length_buckets):
      blockers.append("insufficient_prefix_length_buckets")
   bucket_positions = {
      bucket: {record["prefix_position"] for record in records if record["prefix_bucket"] == bucket}
      for bucket in full_buckets
   }
   ranges = sorted((min(positions), max(positions)) for positions in bucket_positions.values())
   if any(first[1] >= second[0] for first, second in zip(ranges, ranges[1:])):
      blockers.append("prefix_length_buckets_overlap")
   modes = {record["transaction_mode"] for record in records}
   for mode in TRANSACTION_MODES:
      if mode not in modes:
         blockers.append(f"transaction_mode_not_exercised:{mode}")
   if not any(record["correction_count"] == 1 for record in records):
      blockers.append("correction_path_not_exercised")
   if not any(
      record["accepted_prefix_count"] < record["width"] for record in records
   ):
      blockers.append("rejection_path_not_exercised")

   parity_failures = [
      record["operation_ref"]
      for record in records
      if not (
         record["parity"]
         and record["mismatched_offset_count"] == 0
         and record["compared_count"] == record["width"]
         and record["prefix_position_matches"]
         and record["commit_digest_matches"]
         and record["verifier_committed_position"] == record["sequential_committed_position"]
      )
   ]
   if parity_failures:
      blockers.append("sequential_parity_failure")
   if any(record["cleanup"] != "released" for record in records):
      blockers.append("verifier_cleanup_incomplete")
   for field in ("runtime_digest", "model_digest", "representation_digest", "runtime_incarnation", "backend"):
      if len({record[field] for record in records}) > 1:
         blockers.append(f"mixed_{field}")
   if records and records[0]["runtime_digest"] != request.runtime_digest:
      blockers.append("runtime_digest_mismatch")

   negative_results: dict[str, str] = {}
   for negative in negatives:
      if not isinstance(negative, Mapping) or set(negative) != VERIFIER_NEGATIVE_FIELDS:
         _reject("verifier_negative_fields_invalid")
      case_id = negative["case_id"]
      expected = REQUIRED_VERIFIER_NEGATIVES.get(case_id)
      passed = (
         expected is not None
         and negative["refusal_code"] == expected
         and negative["prefix_unchanged"] is True
         and negative["open_transactions"] == 0
      )
      negative_results[str(case_id)] = "failed_closed" if passed else "violated"
   for case_id in REQUIRED_VERIFIER_NEGATIVES:
      if negative_results.get(case_id) != "failed_closed":
         blockers.append(f"verifier_negative_not_passed:{case_id}")

   matrix_passed = not blockers
   if any(record["provenance"] != QUALIFYING_PROVENANCE for record in records):
      blockers.append("non_live_provenance_cannot_qualify")
   if any(record["execution_path"] != QUALIFYING_EXECUTION_PATH for record in records):
      blockers.append("direct_runtime_call_cannot_qualify")

   bundle: dict[str, Any] = {
      "protocol": VERIFIER_QUALIFICATION_PROTOCOL,
      "owner_private_root": request.owner_private_root,
      "decision": "qualified" if not blockers else "not_qualified",
      "matrix_passed": matrix_passed,
      "blockers": sorted(set(blockers)),
      "operation_count": len(records),
      "operations_digest": operations_digest,
      "width_limits": width_limits,
      "prefix_length_buckets": sorted(full_buckets),
      "width_class_counts": dict(sorted(Counter(r["width_class"] for r in records).items())),
      "transaction_mode_counts": dict(sorted(Counter(r["transaction_mode"] for r in records).items())),
      "correction_operations": sum(r["correction_count"] for r in records),
      "parity_failures": len(parity_failures),
      "negatives": dict(sorted(negative_results.items())),
      "runtime_digest": request.runtime_digest,
      "provenance": sorted({r["provenance"] for r in records}),
      "execution_paths": sorted({r["execution_path"] for r in records}),
   }
   bundle["qualification_binding"] = canonical_digest(bundle)
   bundle["capability"] = None
   if bundle["decision"] == "qualified":
      first = records[0]
      capability = TargetVerificationCapability(
         runtime_digest=first["runtime_digest"],
         model_digest=first["model_digest"],
         representation_digest=first["representation_digest"],
         runtime_incarnation=first["runtime_incarnation"],
         maximum_positions=int(maximum),
         maximum_bytes=max(r["candidate_bytes"] for r in records),
         generation_policy="deterministic_greedy",
         transaction_modes=TRANSACTION_MODES,
         qualification_binding=bundle["qualification_binding"],
      )
      bundle["capability"] = {
         "protocol": "mycelium.target_verification_capability.v1",
         "runtime_digest": capability.runtime_digest,
         "model_digest": capability.model_digest,
         "representation_digest": capability.representation_digest,
         "runtime_incarnation": capability.runtime_incarnation,
         "maximum_positions": capability.maximum_positions,
         "maximum_bytes": capability.maximum_bytes,
         "generation_policy": capability.generation_policy,
         "transaction_modes": list(capability.transaction_modes),
         "qualification_binding": capability.qualification_binding,
      }
   return bundle


# ---------------------------------------------------------------------------
# Runtime batch, continuous arrivals, overlap and the paired benchmark
# ---------------------------------------------------------------------------


def count_late_arrivals(publications: Iterable[Mapping[str, Any]]) -> int:
   """Requests that joined a runtime batch after another member's decode had begun.

   Implementation contract resolution 7: a late arrival's collection begins after
   the first decode iteration of an already-active member on the same placement,
   and it joins only at an iteration boundary.  Counted from runtime-owned
   intervals: a request whose first interval on a stage starts after another
   request's first DECODE interval on that stage, and which later shares a runtime
   batch reference with that request.
   """

   late: set[str] = set()
   for publication in publications:
      by_request: dict[str, list[Mapping[str, Any]]] = {}
      for interval in publication.get("intervals", ()):
         if interval.get("hop_kind") == "compute":
            by_request.setdefault(interval["request_ref"], []).append(interval)
      first_start = {
         request: min(item["started_at"] for item in items) for request, items in by_request.items()
      }
      first_decode = {
         request: min(
            (item["started_at"] for item in items if item["phase"] == "DECODE"),
            default=None,
         )
         for request, items in by_request.items()
      }
      batches: dict[str, set[str]] = {}
      for request, items in by_request.items():
         for item in items:
            if item["runtime_batch_ref"]:
               batches.setdefault(item["runtime_batch_ref"], set()).add(request)
      for members in batches.values():
         for request in members:
            for other in members - {request}:
               decode = first_decode.get(other)
               if decode is not None and first_start[request] > decode:
                  late.add(request)
   return len(late)


def _regression(baseline: float, candidate: float) -> float:
   if baseline <= 0:
      return math.inf if candidate > 0 else 0.0
   return candidate / baseline - 1.0


def qualify_runtime_batch_from_evidence(
   request: QualificationRequest,
   evidence: Mapping[str, Any],
) -> dict[str, Any]:
   """Decide the runtime-batch qualification from executed live evidence."""

   from mycelium_router.a10_live_publication import build_overlap_trace, validate_publication
   from mycelium_router.a10_overlap_trace import observe_stage_compute_overlap

   if not isinstance(request, QualificationRequest):
      _reject("invalid_qualification_request_argument")
   expected_fields = {
      "protocol",
      "provenance",
      "execution_path",
      "benchmark_manifest_digest",
      "acceptance_inventory_digest",
      "publications",
      "cross_host_clock_uncertainty",
      "benchmark_run",
      "latency_ms",
      "reliability",
      "output_parity",
      "residual_resources",
      "negatives",
   }
   if not isinstance(evidence, Mapping) or set(evidence) != expected_fields:
      _reject("runtime_batch_evidence_fields_invalid")
   if evidence["protocol"] != RUNTIME_BATCH_EVIDENCE_PROTOCOL:
      _reject("runtime_batch_evidence_protocol_invalid")
   if evidence["provenance"] not in PROVENANCES:
      _reject("runtime_batch_evidence_provenance_invalid")
   if evidence["execution_path"] not in EXECUTION_PATHS:
      _reject("runtime_batch_evidence_path_invalid")
   blockers: list[str] = []
   if evidence["benchmark_manifest_digest"] != request.benchmark_manifest_digest:
      blockers.append("benchmark_manifest_digest_mismatch")
   if (
      evidence["acceptance_inventory_digest"] != request.acceptance_inventory_digest
      or request.acceptance_inventory_digest != ACCEPTANCE_INVENTORY_DIGEST
   ):
      blockers.append("acceptance_inventory_digest_mismatch")

   publications = [validate_publication(item) for item in evidence["publications"]]
   observations = [
      observation
      for publication in publications
      for observation in publication["runtime_batch_observations"]
   ]
   observed_batches = [
      observation
      for observation in observations
      if observation.get("claims_runtime_batch") is True
      and observation.get("backend_invocations") == 1
      and observation.get("membership_authority") == "stage_runtime"
      and observation.get("actual_member_count", 0) >= MINIMUM_PHYSICAL_BATCH_MEMBERS
   ]
   if not observed_batches:
      blockers.append("no_observed_runtime_batch")
   late_arrivals = count_late_arrivals(publications)
   if late_arrivals < 1:
      blockers.append("no_observed_continuous_late_arrival")
   overlap: dict[str, Any] = {"observed": False, "reason": "trace_unavailable"}
   try:
      trace = build_overlap_trace(
         publications,
         cross_host_clock_uncertainty=_number(
            evidence["cross_host_clock_uncertainty"], "cross_host_uncertainty_invalid"
         ),
      )
      verdict = observe_stage_compute_overlap(trace)
      overlap = {
         "observed": verdict.observed,
         "reason": verdict.reason,
         "intersection_seconds": verdict.intersection_seconds,
      }
   except A10ContractError as exc:
      overlap = {"observed": False, "reason": exc.code}
   if not overlap["observed"]:
      blockers.append("pipeline_overlap_not_observed")

   try:
      from tests.a10_acceptance.benchmark_amendment import evaluate_throughput
   except ImportError:  # pragma: no cover - the frozen protocol math ships in-tree
      _reject("benchmark_protocol_math_unavailable")
   throughput = evaluate_throughput(evidence["benchmark_run"])
   if throughput.get("decision") != "material":
      blockers.append(f"throughput_{throughput.get('decision')}")
   valid_pairs = throughput.get("valid_pair_count")
   if not isinstance(valid_pairs, int) or valid_pairs < request.valid_pairs:
      blockers.append("fewer_valid_pairs_than_requested")

   latency = evidence["latency_ms"]
   for metric in ("interactive_p95_ttft", "interactive_p95_tpot"):
      baseline = _number(latency.get(f"baseline_{metric}"), "latency_invalid")
      candidate = _number(latency.get(f"candidate_{metric}"), "latency_invalid")
      if _regression(baseline, candidate) > MAXIMUM_LATENCY_REGRESSION_FRACTION:
         blockers.append(f"{metric}_regression_exceeds_bound")
   reliability = evidence["reliability"]
   for metric in RELIABILITY_FIELDS:
      baseline = _number(reliability["baseline"].get(metric), "reliability_invalid")
      candidate = _number(reliability["candidate"].get(metric), "reliability_invalid")
      worse = candidate < baseline if metric == "cleanup_success_rate" else candidate > baseline
      if worse:
         blockers.append(f"{metric}_regressed")
   parity = evidence["output_parity"]
   compared = _int(parity.get("compared_requests"), "output_parity_invalid")
   mismatched = _int(parity.get("mismatched_requests"), "output_parity_invalid")
   if compared < 1 or mismatched != 0:
      blockers.append("output_parity_failed")
   residual = evidence["residual_resources"]
   if not isinstance(residual, Mapping) or set(residual) != set(RESIDUAL_RESOURCE_FIELDS):
      _reject("residual_resources_invalid")
   if any(_int(residual[field], "residual_resources_invalid") != 0 for field in RESIDUAL_RESOURCE_FIELDS):
      blockers.append("residual_resources_nonzero")
   negatives = evidence["negatives"]
   if not isinstance(negatives, Mapping):
      _reject("negatives_invalid")
   for case_id in REQUIRED_RUNTIME_BATCH_NEGATIVES:
      result = negatives.get(case_id)
      if not (
         isinstance(result, Mapping)
         and result.get("outcome") == "failed_closed_within_scope"
         and result.get("residue") == 0
      ):
         blockers.append(f"runtime_batch_negative_not_passed:{case_id}")

   evidence_passed = not blockers
   if evidence["provenance"] != QUALIFYING_PROVENANCE:
      blockers.append("non_live_provenance_cannot_qualify")
   if evidence["execution_path"] != QUALIFYING_EXECUTION_PATH:
      blockers.append("direct_runtime_call_cannot_qualify")

   bundle = {
      "protocol": RUNTIME_BATCH_QUALIFICATION_PROTOCOL,
      "owner_private_root": request.owner_private_root,
      "decision": "qualified" if not blockers else "not_qualified",
      "evidence_passed": evidence_passed,
      "blockers": sorted(set(blockers)),
      "benchmark_manifest_digest": request.benchmark_manifest_digest,
      "acceptance_inventory_digest": request.acceptance_inventory_digest,
      "observed_runtime_batches": len(observed_batches),
      "late_arrivals": late_arrivals,
      "overlap": overlap,
      "throughput": {
         key: throughput.get(key)
         for key in (
            "decision",
            "reasons",
            "calibrated_offered_arrival_rate_per_second",
            "point_estimate_fraction",
            "paired_95_percent_bootstrap_lower_bound_fraction",
            "valid_pair_count",
         )
      },
      "evidence_digest": canonical_digest(
         {key: evidence[key] for key in sorted(evidence) if key != "publications"}
      ),
      "publication_digests": [canonical_digest(item) for item in publications],
   }
   bundle["qualification_binding"] = canonical_digest(bundle)
   return bundle


__all__ = [
   "A10QualificationError",
   "EXECUTION_PATHS",
   "QUALIFYING_EXECUTION_PATH",
   "REQUIRED_RUNTIME_BATCH_NEGATIVES",
   "REQUIRED_VERIFIER_NEGATIVES",
   "RESIDUAL_RESOURCE_FIELDS",
   "RUNTIME_BATCH_EVIDENCE_PROTOCOL",
   "RUNTIME_BATCH_QUALIFICATION_PROTOCOL",
   "VERIFIER_OPERATION_PROTOCOL",
   "VERIFIER_QUALIFICATION_PROTOCOL",
   "canonical_digest",
   "count_late_arrivals",
   "qualify_runtime_batch_from_evidence",
   "qualify_target_verifier_from_operations",
   "validate_verifier_operation",
   "verifier_operation_record",
]
