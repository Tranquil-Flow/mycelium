# SPDX-License-Identifier: AGPL-3.0-only
"""Execute the A10 target-verifier correctness matrix on one real target runtime.

The matrix (specification section 8, implementation contract slice S3) is at least
100 bounded operations over the widths ``one``, ``qualified_default`` and
``qualified_maximum`` in at least two prefix-length buckets.  Every operation runs
the transactional verifier and the sequential oracle on the same prefix
(``sequential_parity_reference``) and is projected into a privacy-reduced record.
Each operation opens a fresh request-local verifier so no operation's commit can
leak into another's prefix.

Candidates are the target's own greedy continuation, perturbed at a scheduled
offset so acceptance, rejection and the correction path are all exercised; the
transaction mode rotates through all three modes.  The required negatives are
executed against the same runtime and must fail closed with the prefix untouched.

The records say how they were produced (``provenance``, ``execution_path``); the
decider in ``a10_qualification`` qualifies only live product-path evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mycelium_router.a10_qualification import (
   REQUIRED_VERIFIER_NEGATIVES,
   verifier_operation_record,
)
from mycelium_router.a10_runtime_batch import A10ContractError
from mycelium_router.a10_target_verification import (
   QUALIFIED_WIDTH_CLASSES,
   TRANSACTION_MODES,
   TargetVerificationRequest,
   sequential_parity_reference,
   verify_candidate_positions,
)


DEFAULT_GENERATION_POLICY = "deterministic_greedy"


@dataclass(frozen=True)
class MatrixPlan:
   """The frozen matrix inputs: widths per class and prefix lengths per bucket."""

   widths: Mapping[str, int]
   prefix_buckets: Mapping[str, tuple[int, ...]]
   operations: int
   vocab_size: int
   seed: int = 10

   def __post_init__(self) -> None:
      if set(self.widths) != set(QUALIFIED_WIDTH_CLASSES) or self.widths["one"] != 1:
         raise A10ContractError("invalid_matrix_widths")
      if not (1 <= self.widths["qualified_default"] <= self.widths["qualified_maximum"]):
         raise A10ContractError("invalid_matrix_widths")
      if len(self.prefix_buckets) < 2 or any(
         not lengths or min(lengths) < 1 for lengths in self.prefix_buckets.values()
      ):
         raise A10ContractError("invalid_matrix_prefix_buckets")
      if self.operations < 1 or self.vocab_size < 3:
         raise A10ContractError("invalid_matrix_plan")


def _prefix(plan: MatrixPlan, bucket: str, length: int, index: int) -> tuple[int, ...]:
   digest = hashlib.sha256(f"{plan.seed}:{bucket}:{length}:{index}".encode()).digest()
   return tuple(1 + digest[position % len(digest)] % (plan.vocab_size - 1) for position in range(length))


def _request(verifier: Any, width: int, mode: str, ref: str, *, policy: str = DEFAULT_GENERATION_POLICY) -> TargetVerificationRequest:
   return TargetVerificationRequest(
      request_ref=ref,
      target_kv_ref=f"target-kv:{ref}",
      runtime_incarnation=getattr(verifier, "runtime_incarnation", "unknown"),
      committed_prefix_position=verifier.committed_prefix_position(),
      committed_prefix_digest=verifier.committed_prefix_digest(),
      candidate_position_count=width,
      candidate_bytes=4 * width,
      generation_policy=policy,
      transaction_mode=mode,
      deadline_at=1.0e12,
      cancellation_generation=0,
      idempotency_digest="sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
   )


def run_matrix_operation(
   plan: MatrixPlan,
   index: int,
   *,
   open_verifier: Callable[[tuple[int, ...], str], Any],
   runtime_digest: str,
   model_digest: str,
   representation_digest: str,
   backend: str,
   provenance: str,
   execution_path: str,
) -> dict[str, Any]:
   """Execute matrix operation ``index`` on a fresh verifier; return its record."""

   if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < plan.operations:
      raise A10ContractError("invalid_matrix_operation_index")
   cells = [
      (bucket, width_class)
      for bucket in plan.prefix_buckets
      for width_class in QUALIFIED_WIDTH_CLASSES
   ]
   bucket, width_class = cells[index % len(cells)]
   lengths = plan.prefix_buckets[bucket]
   length = lengths[(index // len(cells)) % len(lengths)]
   width = plan.widths[width_class]
   mode = TRANSACTION_MODES[index % len(TRANSACTION_MODES)]
   ref = f"a10-matrix-{index:04d}"
   verifier = open_verifier(_prefix(plan, bucket, length, index), ref)
   candidates = list(verifier.greedy_continuation(width))
   # Schedule: every third operation fully accepted; the others miss at a
   # rotating offset so rejection and correction are both exercised.
   if index % 3 != 0:
      miss = (index // 3) % width
      # Always a different token: 1 + c mod (V-1) never equals c for V > 2.
      candidates[miss] = 1 + candidates[miss] % (plan.vocab_size - 1)
   verifier.stage_candidates(tuple(candidates), transaction_mode=mode)
   request = _request(verifier, width, mode, ref)
   result = sequential_parity_reference(verifier, request)
   return verifier_operation_record(
      operation_ref=ref,
      parity_result=result,
      request=request,
      width_class=width_class,
      prefix_bucket=bucket,
      runtime_digest=runtime_digest,
      model_digest=model_digest,
      representation_digest=representation_digest,
      backend=backend,
      provenance=provenance,
      execution_path=execution_path,
   )


def run_verifier_negative(
   case_id: str,
   plan: MatrixPlan,
   *,
   open_verifier: Callable[[tuple[int, ...], str], Any],
   cancel_verification: Callable[[Any], None],
) -> dict[str, Any]:
   """Execute one required verifier negative; it must fail closed."""

   if case_id == "stale_prefix":
      return _stale_prefix_negative(plan, open_verifier)
   if case_id == "unsupported_generation_policy":
      return _generation_policy_negative(plan, open_verifier)
   if case_id == "position_bound":
      return _position_bound_negative(plan, open_verifier)
   if case_id == "cancellation_during_verification":
      return _cancellation_negative(plan, open_verifier, cancel_verification)
   raise A10ContractError("invalid_verifier_negative_case")


#: The required negatives in their execution order.
VERIFIER_NEGATIVE_ORDER = (
   "stale_prefix",
   "unsupported_generation_policy",
   "position_bound",
   "cancellation_during_verification",
)


def run_verifier_matrix(
   plan: MatrixPlan,
   *,
   open_verifier: Callable[[tuple[int, ...], str], Any],
   cancel_verification: Callable[[Any], None],
   runtime_digest: str,
   model_digest: str,
   representation_digest: str,
   backend: str,
   provenance: str,
   execution_path: str,
   on_operation: Callable[[int, Mapping[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
   """Run the matrix and the required negatives; return (operations, negatives)."""

   operations: list[dict[str, Any]] = []
   for index in range(plan.operations):
      record = run_matrix_operation(
         plan,
         index,
         open_verifier=open_verifier,
         runtime_digest=runtime_digest,
         model_digest=model_digest,
         representation_digest=representation_digest,
         backend=backend,
         provenance=provenance,
         execution_path=execution_path,
      )
      operations.append(record)
      if on_operation is not None:
         on_operation(index, record)

   negatives = [
      run_verifier_negative(
         case_id,
         plan,
         open_verifier=open_verifier,
         cancel_verification=cancel_verification,
      )
      for case_id in VERIFIER_NEGATIVE_ORDER
   ]
   assert {item["case_id"] for item in negatives} == set(REQUIRED_VERIFIER_NEGATIVES)
   return operations, negatives


def _negative(case_id: str, verifier: Any, before: tuple[int, str], code: str) -> dict[str, Any]:
   after = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   return {
      "case_id": case_id,
      "refusal_code": code,
      "prefix_unchanged": after == before,
      "open_transactions": verifier.snapshot()["open_transactions"],
   }


def _refusal_code(error: BaseException) -> str:
   code = getattr(error, "code", None)
   return code if isinstance(code, str) and code else type(error).__name__


def _stale_prefix_negative(plan: MatrixPlan, open_verifier: Callable[..., Any]) -> dict[str, Any]:
   verifier = open_verifier(_prefix(plan, "negative", 3, 0), "a10-negative-stale")
   verifier.stage_candidates(verifier.greedy_continuation(1), transaction_mode=TRANSACTION_MODES[1])
   before = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   request = _request(verifier, 1, TRANSACTION_MODES[1], "a10-negative-stale")
   stale = TargetVerificationRequest(
      **{**request.__dict__, "committed_prefix_digest": "sha256:" + "0" * 64}
   )
   try:
      verify_candidate_positions(verifier, stale)
      code = "accepted"
   except Exception as error:  # noqa: BLE001 - the refusal code is the evidence
      code = _refusal_code(error)
   return _negative("stale_prefix", verifier, before, code)


def _generation_policy_negative(plan: MatrixPlan, open_verifier: Callable[..., Any]) -> dict[str, Any]:
   verifier = open_verifier(_prefix(plan, "negative", 3, 1), "a10-negative-policy")
   before = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   try:
      _request(verifier, 1, TRANSACTION_MODES[1], "a10-negative-policy", policy="temperature_sampling")
      code = "accepted"
   except Exception as error:  # noqa: BLE001
      code = _refusal_code(error)
   return _negative("unsupported_generation_policy", verifier, before, code)


def _position_bound_negative(plan: MatrixPlan, open_verifier: Callable[..., Any]) -> dict[str, Any]:
   probe = open_verifier((1,), "a10-negative-bound-probe")
   limit = probe.n_positions
   width = plan.widths["qualified_maximum"]
   length = max(1, limit - width + 1)
   verifier = open_verifier(_prefix(plan, "negative", length, 2), "a10-negative-bound")
   before = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   verifier.stage_candidates((1,) * width, transaction_mode=TRANSACTION_MODES[1])
   try:
      verify_candidate_positions(verifier, _request(verifier, width, TRANSACTION_MODES[1], "a10-negative-bound"))
      code = "accepted"
   except Exception as error:  # noqa: BLE001
      code = _refusal_code(error)
   return _negative("position_bound", verifier, before, code)


def _cancellation_negative(
   plan: MatrixPlan,
   open_verifier: Callable[..., Any],
   cancel_verification: Callable[[Any], None],
) -> dict[str, Any]:
   verifier = open_verifier(_prefix(plan, "negative", 3, 3), "a10-negative-cancel")
   width = plan.widths["qualified_default"]
   verifier.stage_candidates(verifier.greedy_continuation(width), transaction_mode=TRANSACTION_MODES[2])
   before = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   cancel_verification(verifier)
   try:
      verify_candidate_positions(verifier, _request(verifier, width, TRANSACTION_MODES[2], "a10-negative-cancel"))
      code = "accepted"
   except Exception as error:  # noqa: BLE001
      code = _refusal_code(error)
   return _negative("cancellation_during_verification", verifier, before, code)


__all__ = [
   "DEFAULT_GENERATION_POLICY",
   "MatrixPlan",
   "VERIFIER_NEGATIVE_ORDER",
   "run_matrix_operation",
   "run_verifier_matrix",
   "run_verifier_negative",
]
