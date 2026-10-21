# SPDX-License-Identifier: AGPL-3.0-only
"""A10 transactional target verification: the implemented bounded verifier.

The A10 specification (section 7) makes the multi-position verifier a target-runtime
operation, not a planner flag: it creates a target-owned KV transaction from the exact
committed prefix, evaluates every bounded candidate position causally in **one** real
backend call, and then either rolls back, commits the verified prefix, or commits the
verified prefix plus one target-authoritative correction drawn from that same call.
Commit is monotonic and atomic for the declared range; a stale prefix, an excess width,
a wrong generation policy, a partial commit or a parity failure rolls back to the
original prefix.

This module holds the real deterministic mechanics; ``mycelium_router.a10_target_verification``
keeps the declared entry points and delegates here, so the machine-source surface named
by the implementation contract is unchanged while the executable body stays small.

Claim boundary: deterministic, in-process behaviour over a verifier port that reports
its own per-position decisions and committed state.  Candidate token IDs, logits,
tensors and KV bytes never leave the private path -- the projection carries counts,
positions and digests only.  ``qualify_target_verifier`` decides only from an executed
operation matrix (``a10_qualification``), so nothing here is qualification or physical
evidence and no capability is advertised from it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mycelium_router.a10_runtime_batch import A10ContractError, _bounded_int, _digest, _reject, _text
from mycelium_router.a10_target_verification import (
   SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
   TargetVerificationOutcome,
   TargetVerificationRequest,
   TRANSACTION_MODES,
)

PROTOCOL = "mycelium.a10_verifier_execution.v1"

DEFAULT_WIDTH_CLASS = "qualified_default"
MINIMUM_VERIFIED_POSITIONS = 1


class A10VerificationError(A10ContractError):
   """A coded, fail-closed verifier refusal."""


@dataclass(frozen=True)
class PositionDecision:
   """The target's per-position verdict for one bounded candidate position.

   Only the verdict leaves the private path.  The token the target preferred stays on
   the authenticated runtime path and is never projected into evidence.
   """

   offset: int
   accepted: bool

   def __post_init__(self) -> None:
      _bounded_int(
         self.offset,
         code="invalid_position_offset",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      if not isinstance(self.accepted, bool):
         _reject("invalid_position_decision")


@dataclass(frozen=True)
class PositionEvaluation:
   """What the verifier's single backend call returned for the bounded width."""

   invocation_count: int
   prefix_position: int
   prefix_digest: str
   decisions: tuple[PositionDecision, ...]

   def __post_init__(self) -> None:
      _bounded_int(
         self.invocation_count,
         code="invalid_backend_invocation_count",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _bounded_int(
         self.prefix_position,
         code="invalid_committed_prefix_position",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _digest(self.prefix_digest, code="invalid_committed_prefix_digest")
      if (
         not isinstance(self.decisions, tuple)
         or not self.decisions
         or any(not isinstance(item, PositionDecision) for item in self.decisions)
      ):
         _reject("invalid_position_decisions")
      offsets = [item.offset for item in self.decisions]
      if offsets != list(range(len(offsets))):
         _reject("invalid_position_offsets")


@dataclass(frozen=True)
class SequentialReference:
   """The sequential target authority over the same prefix.

   Produced by replaying the target one position at a time from the identical
   committed prefix on an isolated transaction, so it is a real sequential authority
   rather than a re-labelling of the bounded call.
   """

   prefix_position: int
   prefix_digest: str
   decisions: tuple[PositionDecision, ...]
   committed_position: int
   committed_digest: str

   def __post_init__(self) -> None:
      _bounded_int(
         self.prefix_position,
         code="invalid_committed_prefix_position",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _digest(self.prefix_digest, code="invalid_committed_prefix_digest")
      _bounded_int(
         self.committed_position,
         code="invalid_commit_position",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _digest(self.committed_digest, code="invalid_committed_prefix_digest")
      if (
         not isinstance(self.decisions, tuple)
         or any(not isinstance(item, PositionDecision) for item in self.decisions)
      ):
         _reject("invalid_position_decisions")


@runtime_checkable
class VerificationTransaction(Protocol):
   """One target-owned KV transaction, open until commit or rollback and cleanup."""

   def committed_prefix_position(self) -> int: ...

   def committed_prefix_digest(self) -> str: ...

   def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation: ...

   def commit_verified_prefix(self, committed_count: int) -> None: ...

   def rollback_to_prefix(self) -> None: ...

   def cleanup(self) -> str: ...


@runtime_checkable
class TargetVerificationRuntimePort(Protocol):
   """The exact target runtime shape a verification operation requires."""

   def begin_verification(self) -> VerificationTransaction: ...

   def sequential_reference(self, candidate_position_count: int) -> SequentialReference: ...


def _require_verification_port(port: object) -> TargetVerificationRuntimePort:
   if not isinstance(port, TargetVerificationRuntimePort):
      _reject("invalid_target_verification_port")
   return port


def _accepted_prefix_count(decisions: tuple[PositionDecision, ...]) -> int:
   accepted = 0
   for decision in decisions:
      if decision.accepted:
         accepted += 1
      else:
         break
   if any(decision.accepted for decision in decisions[accepted:]):
      # A verified position may never follow a rejected one: a hole in the prefix is a
      # partial commit, not a shorter prefix.
      _reject("non_contiguous_acceptance")
   return accepted


def _safe_rollback(transaction: VerificationTransaction) -> None:
   try:
      transaction.rollback_to_prefix()
   except Exception:  # noqa: BLE001 - a rollback failure must not mask the refusal
      pass


def _safe_cleanup(transaction: VerificationTransaction) -> str:
   try:
      cleanup = transaction.cleanup()
   except Exception:  # noqa: BLE001 - cleanup failure must not mask the refusal
      return "cleanup_failed"
   return cleanup if isinstance(cleanup, str) and cleanup else "cleanup_unknown"


@dataclass(frozen=True)
class VerificationExecution:
   """The transaction outcome plus the private decisions and committed state."""

   outcome: TargetVerificationOutcome
   decisions: tuple[PositionDecision, ...]
   committed_position: int
   committed_digest: str
   cleanup: str


def _run_verification(
   port: TargetVerificationRuntimePort,
   request: TargetVerificationRequest,
) -> VerificationExecution:
   transaction = port.begin_verification()
   if not isinstance(transaction, VerificationTransaction):
      _reject("invalid_target_verification_transaction")

   width = request.candidate_position_count
   try:
      prefix_position = transaction.committed_prefix_position()
      prefix_digest = transaction.committed_prefix_digest()
      if (
         prefix_position != request.committed_prefix_position
         or prefix_digest != request.committed_prefix_digest
      ):
         # Stale prefix: never evaluate a candidate against a prefix the caller did not
         # name, and never commit anything.
         _reject("stale_prefix")

      evaluation = transaction.evaluate_positions(width)
      if not isinstance(evaluation, PositionEvaluation):
         _reject("invalid_position_evaluation")
      if evaluation.invocation_count != 1:
         _reject("verification_requires_one_backend_call")
      if len(evaluation.decisions) != width:
         _reject("wrong_verified_position_count")

      accepted = _accepted_prefix_count(evaluation.decisions)

      correction = 0
      if (
         request.transaction_mode
         == "commit_verified_prefix_plus_target_correction"
         and accepted < width
      ):
         # The correction position is the next one, drawn from the same single call.
         correction = 1
      if accepted + correction > width:
         _reject("excess_commit_width")

      if request.transaction_mode == "rollback_to_prefix":
         transaction.rollback_to_prefix()
         committed_position = prefix_position
         commit_state = "rolled_back"
      else:
         transaction.commit_verified_prefix(accepted + correction)
         committed_position = prefix_position + accepted + correction
         commit_state = (
            "committed_with_correction" if correction else "committed"
         )
         if transaction.committed_prefix_position() != committed_position:
            _reject("commit_position_mismatch")

      committed_digest = transaction.committed_prefix_digest()
      cleanup = _safe_cleanup(transaction)
   except BaseException:
      _safe_rollback(transaction)
      _safe_cleanup(transaction)
      raise

   outcome = TargetVerificationOutcome(
      request_ref=request.request_ref,
      prefix_position=prefix_position,
      proposed_position_count=width,
      verified_count=len(evaluation.decisions),
      accepted_prefix_count=accepted,
      correction_count=correction,
      committed_position=committed_position,
      commit_state=commit_state,
      cleanup=cleanup,
   )
   return VerificationExecution(
      outcome=outcome,
      decisions=evaluation.decisions,
      committed_position=committed_position,
      committed_digest=committed_digest,
      cleanup=cleanup,
   )


def verify_candidate_positions(
   port: TargetVerificationRuntimePort,
   request: TargetVerificationRequest,
) -> TargetVerificationOutcome:
   """Evaluate every bounded candidate position in one real target backend call.

   Returns the privacy-reduced projection.  The transaction is never left open: it is
   committed, or rolled back to the exact supplied prefix, and cleaned up on every path.
   """

   if not isinstance(request, TargetVerificationRequest):
      _reject("invalid_target_verification_request_argument")
   port = _require_verification_port(port)
   return _run_verification(port, request).outcome


@dataclass(frozen=True)
class SequentialParityResult:
   """Parity of the bounded verifier against sequential target authority."""

   request_ref: str
   parity: bool
   compared_count: int
   mismatched_offsets: tuple[int, ...]
   verifier_committed_position: int
   sequential_committed_position: int
   prefix_position_matches: bool
   commit_digest_matches: bool
   outcome: TargetVerificationOutcome


def sequential_parity_reference(
   port: TargetVerificationRuntimePort,
   request: TargetVerificationRequest,
) -> SequentialParityResult:
   """Run the sequential oracle on the same prefix and compare target decisions.

   Per-position target decisions and the final committed prefix position and digest are
   compared.  Logits, tensors, activations, KV bytes and candidate token IDs stay on the
   authenticated private path and never enter this result.
   """

   if not isinstance(request, TargetVerificationRequest):
      _reject("invalid_target_verification_request_argument")
   port = _require_verification_port(port)

   execution = _run_verification(port, request)

   reference = port.sequential_reference(request.candidate_position_count)
   if not isinstance(reference, SequentialReference):
      _reject("invalid_sequential_reference")
   if (
      reference.prefix_position != request.committed_prefix_position
      or reference.prefix_digest != request.committed_prefix_digest
   ):
      _reject("stale_prefix")

   compared = min(len(execution.decisions), len(reference.decisions))
   mismatched = tuple(
      offset
      for offset in range(compared)
      if execution.decisions[offset].accepted != reference.decisions[offset].accepted
   )
   position_matches = execution.committed_position == reference.committed_position
   digest_matches = execution.committed_digest == reference.committed_digest
   parity = (
      compared == request.candidate_position_count
      and not mismatched
      and position_matches
      and digest_matches
   )

   return SequentialParityResult(
      request_ref=request.request_ref,
      parity=parity,
      compared_count=compared,
      mismatched_offsets=mismatched,
      verifier_committed_position=execution.committed_position,
      sequential_committed_position=reference.committed_position,
      prefix_position_matches=position_matches,
      commit_digest_matches=digest_matches,
      outcome=execution.outcome,
   )


__all__ = [
   "PROTOCOL",
   "A10VerificationError",
   "PositionDecision",
   "PositionEvaluation",
   "SequentialParityResult",
   "SequentialReference",
   "TargetVerificationRuntimePort",
   "VerificationExecution",
   "VerificationTransaction",
   "sequential_parity_reference",
   "verify_candidate_positions",
   "A10ContractError",
]
