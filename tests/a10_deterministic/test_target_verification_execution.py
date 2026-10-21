# SPDX-License-Identifier: AGPL-3.0-only
"""A10 transactional target verification: the implemented verifier path.

This suite covers the behaviour behind the contract's ``verify_candidate_positions``
and ``sequential_parity_reference`` entry points (implementation contract sections 2
and 3, slice S3): one real backend call over a declared bounded width, a target-owned
KV transaction, commit/rollback arithmetic, and the sequential parity oracle.

Everything here is deterministic: an in-process verifier double reports exactly the
per-position decisions and the committed state it was told to report, and the entry
point is judged on that report.  The double never becomes qualification evidence --
``qualify_target_verifier`` still refuses.  No socket, no child process, no model
artifact and no physical route is used.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# The lane worktree must win the import race even when another test module already
# bound ``mycelium_router`` to the standing checkout (see tests/a10_deterministic).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_target_verification as atv
from mycelium_router.a10_verifier_execution import (
   PositionDecision,
   PositionEvaluation,
   SequentialParityResult,
   SequentialReference,
   sequential_parity_reference,
   verify_candidate_positions,
)

DIGEST = "sha256:" + "a" * 64
SEQUENTIAL_DIGEST = "sha256:" + "b" * 64
PREFIX_POSITION = 16


def decisions(spec: str) -> tuple[PositionDecision, ...]:
   """``\"AAR\"`` -> accepted, accepted, rejected."""

   return tuple(
      PositionDecision(offset=index, accepted=letter == "A")
      for index, letter in enumerate(spec)
   )


def request(**overrides):
   values = {
      "request_ref": "verification:one",
      "target_kv_ref": "target-kv:one",
      "runtime_incarnation": "incarnation:one",
      "committed_prefix_position": PREFIX_POSITION,
      "committed_prefix_digest": DIGEST,
      "candidate_position_count": 4,
      "candidate_bytes": 4096,
      "generation_policy": "deterministic_greedy",
      "transaction_mode": "commit_verified_prefix",
      "deadline_at": 1.0,
      "cancellation_generation": 0,
      "idempotency_digest": DIGEST,
   }
   values.update(overrides)
   return atv.TargetVerificationRequest(**values)


class Transaction:
   """A target-KV transaction double that reports its committed state honestly."""

   def __init__(
      self,
      *,
      prefix_position: int = PREFIX_POSITION,
      prefix_digest: str = DIGEST,
      evaluation=None,
      committed_digest: str | None = None,
      ignore_commit: bool = False,
   ) -> None:
      self._prefix_position = prefix_position
      self._prefix_digest = prefix_digest
      self._evaluation = evaluation
      self._committed_digest = committed_digest
      self._ignore_commit = ignore_commit
      self._committed_position = prefix_position
      self._committed = prefix_digest
      self.evaluate_calls = 0
      self.commit_calls: list[int] = []
      self.rollback_calls = 0
      self.cleanup_calls = 0
      self.requested_widths: list[int] = []

   def committed_prefix_position(self) -> int:
      return self._committed_position

   def committed_prefix_digest(self) -> str:
      return self._committed

   def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation:
      self.evaluate_calls += 1
      self.requested_widths.append(candidate_position_count)
      return self._evaluation

   def commit_verified_prefix(self, committed_count: int) -> None:
      self.commit_calls.append(committed_count)
      if self._ignore_commit:
         return
      self._committed_position = self._prefix_position + committed_count
      self._committed = self._committed_digest or self._prefix_digest

   def rollback_to_prefix(self) -> None:
      self.rollback_calls += 1
      self._committed_position = self._prefix_position
      self._committed = self._prefix_digest

   def cleanup(self) -> str:
      self.cleanup_calls += 1
      return "verification_released"


def evaluation(spec: str, *, invocations: int = 1, prefix_position=PREFIX_POSITION,
               prefix_digest=DIGEST) -> PositionEvaluation:
   return PositionEvaluation(
      invocation_count=invocations,
      prefix_position=prefix_position,
      prefix_digest=prefix_digest,
      decisions=decisions(spec),
   )


class Port:
   def __init__(self, transaction: Transaction, reference=None) -> None:
      self.transaction = transaction
      self.reference = reference
      self.begun = 0
      self.reference_calls: list[int] = []

   def begin_verification(self):
      self.begun += 1
      return self.transaction

   def sequential_reference(self, candidate_position_count: int):
      self.reference_calls.append(candidate_position_count)
      return self.reference


def code_of(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


# ---------------------------------------------------------------------------
# One real backend call over the declared bounded width
# ---------------------------------------------------------------------------


def test_one_verification_is_one_backend_call():
   transaction = Transaction(evaluation=evaluation("AAAA"))
   port = Port(transaction)

   outcome = verify_candidate_positions(port, request())

   assert transaction.evaluate_calls == 1, "one declared call, never a loop"
   assert port.begun == 1
   assert outcome.verified_count == 4
   assert outcome.proposed_position_count == 4
   assert transaction.cleanup_calls == 1


def test_a_two_invocation_evaluation_fails_closed():
   transaction = Transaction(evaluation=evaluation("AAAA", invocations=2))
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "verification_requires_one_backend_call"
   assert transaction.rollback_calls == 1
   assert transaction.cleanup_calls == 1
   assert transaction.commit_calls == []


def test_a_short_evaluation_fails_closed():
   transaction = Transaction(evaluation=evaluation("AAA"))
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "wrong_verified_position_count"
   assert transaction.rollback_calls == 1
   assert transaction.commit_calls == []


# ---------------------------------------------------------------------------
# Commit arithmetic (implementation contract resolution 4)
# ---------------------------------------------------------------------------


def test_commit_verified_prefix_commits_only_the_accepted_prefix():
   transaction = Transaction(evaluation=evaluation("AAAR"))
   outcome = verify_candidate_positions(Port(transaction), request())

   assert outcome.accepted_prefix_count == 3
   assert outcome.correction_count == 0
   assert outcome.commit_state == "committed"
   assert outcome.committed_position == PREFIX_POSITION + 3
   assert transaction.commit_calls == [3]


def test_rollback_to_prefix_commits_nothing_and_restores_the_prefix():
   transaction = Transaction(evaluation=evaluation("AAAR"))
   outcome = verify_candidate_positions(
      Port(transaction), request(transaction_mode="rollback_to_prefix")
   )

   assert outcome.commit_state == "rolled_back"
   assert outcome.committed_position == PREFIX_POSITION
   assert transaction.commit_calls == []
   assert transaction.rollback_calls == 1


def test_a_correction_position_is_committed_only_from_the_same_call():
   transaction = Transaction(evaluation=evaluation("AARR"))
   outcome = verify_candidate_positions(
      Port(transaction),
      request(transaction_mode="commit_verified_prefix_plus_target_correction"),
   )

   assert outcome.accepted_prefix_count == 2
   assert outcome.correction_count == 1
   assert outcome.commit_state == "committed_with_correction"
   assert outcome.committed_position == PREFIX_POSITION + 3
   assert transaction.commit_calls == [3], "accepted prefix plus one correction"


def test_a_full_prefix_leaves_no_room_for_a_correction():
   transaction = Transaction(evaluation=evaluation("AAAA"))
   outcome = verify_candidate_positions(
      Port(transaction),
      request(transaction_mode="commit_verified_prefix_plus_target_correction"),
   )
   assert outcome.correction_count == 0
   assert outcome.commit_state == "committed"
   assert outcome.committed_position == PREFIX_POSITION + 4


def test_a_non_contiguous_acceptance_fails_closed():
   transaction = Transaction(evaluation=evaluation("ARRA"))
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "non_contiguous_acceptance"
   assert transaction.rollback_calls == 1
   assert transaction.commit_calls == []


def test_a_commit_that_does_not_move_the_prefix_fails_closed():
   transaction = Transaction(evaluation=evaluation("AAAR"), ignore_commit=True)
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "commit_position_mismatch"
   assert transaction.rollback_calls == 1
   assert transaction.cleanup_calls == 1


# ---------------------------------------------------------------------------
# The transaction is bound to the exact committed prefix
# ---------------------------------------------------------------------------


def test_a_stale_prefix_fails_closed_before_any_candidate_is_evaluated():
   transaction = Transaction(
      prefix_position=PREFIX_POSITION + 4, evaluation=evaluation("AAAA")
   )
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "stale_prefix"
   assert transaction.evaluate_calls == 0
   assert transaction.commit_calls == []
   assert transaction.cleanup_calls == 1


def test_a_mismatched_prefix_digest_fails_closed():
   transaction = Transaction(
      prefix_digest=SEQUENTIAL_DIGEST, evaluation=evaluation("AAAA")
   )
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), request())
   assert code_of(raised) == "stale_prefix"


def test_a_malformed_request_and_port_are_rejected_with_their_codes():
   transaction = Transaction(evaluation=evaluation("AAAA"))
   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(Port(transaction), object())
   assert code_of(raised) == "invalid_target_verification_request_argument"

   with pytest.raises(atv.A10ContractError) as raised:
      verify_candidate_positions(object(), request())
   assert code_of(raised) == "invalid_target_verification_port"


# ---------------------------------------------------------------------------
# Sequential parity oracle
# ---------------------------------------------------------------------------


def test_an_identical_sequential_authority_reports_parity():
   transaction = Transaction(
      evaluation=evaluation("AAAR"), committed_digest=SEQUENTIAL_DIGEST
   )
   reference = SequentialReference(
      prefix_position=PREFIX_POSITION,
      prefix_digest=DIGEST,
      decisions=decisions("AAAR"),
      committed_position=PREFIX_POSITION + 3,
      committed_digest=SEQUENTIAL_DIGEST,
   )
   port = Port(transaction, reference)

   result = sequential_parity_reference(port, request())

   assert isinstance(result, SequentialParityResult)
   assert result.parity is True
   assert result.mismatched_offsets == ()
   assert result.verifier_committed_position == PREFIX_POSITION + 3
   assert result.sequential_committed_position == PREFIX_POSITION + 3
   assert result.commit_digest_matches is True
   assert port.reference_calls == [4]


def test_a_disagreeing_sequential_decision_reports_the_offset():
   transaction = Transaction(
      evaluation=evaluation("AAAR"), committed_digest=SEQUENTIAL_DIGEST
   )
   reference = SequentialReference(
      prefix_position=PREFIX_POSITION,
      prefix_digest=DIGEST,
      decisions=decisions("AAAA"),
      committed_position=PREFIX_POSITION + 3,
      committed_digest=SEQUENTIAL_DIGEST,
   )
   result = sequential_parity_reference(Port(transaction, reference), request())

   assert result.parity is False
   assert result.mismatched_offsets == (3,)


def test_a_committed_position_mismatch_reports_no_parity():
   transaction = Transaction(
      evaluation=evaluation("AAAR"), committed_digest=SEQUENTIAL_DIGEST
   )
   reference = SequentialReference(
      prefix_position=PREFIX_POSITION,
      prefix_digest=DIGEST,
      decisions=decisions("AAAR"),
      committed_position=PREFIX_POSITION + 4,
      committed_digest=SEQUENTIAL_DIGEST,
   )
   result = sequential_parity_reference(Port(transaction, reference), request())

   assert result.parity is False
   assert result.verifier_committed_position != result.sequential_committed_position


def test_a_committed_digest_mismatch_reports_no_parity():
   transaction = Transaction(
      evaluation=evaluation("AAAR"), committed_digest=SEQUENTIAL_DIGEST
   )
   reference = SequentialReference(
      prefix_position=PREFIX_POSITION,
      prefix_digest=DIGEST,
      decisions=decisions("AAAR"),
      committed_position=PREFIX_POSITION + 3,
      committed_digest=DIGEST,
   )
   result = sequential_parity_reference(Port(transaction, reference), request())

   assert result.parity is False
   assert result.commit_digest_matches is False


def test_a_stale_reference_prefix_is_rejected():
   transaction = Transaction(
      evaluation=evaluation("AAAR"), committed_digest=SEQUENTIAL_DIGEST
   )
   reference = SequentialReference(
      prefix_position=PREFIX_POSITION + 4,
      prefix_digest=DIGEST,
      decisions=decisions("AAAR"),
      committed_position=PREFIX_POSITION + 7,
      committed_digest=SEQUENTIAL_DIGEST,
   )
   with pytest.raises(atv.A10ContractError) as raised:
      sequential_parity_reference(Port(transaction, reference), request())
   assert code_of(raised) == "stale_prefix"


def test_an_unqualified_verifier_still_advertises_nothing_and_earns_nothing():
   with pytest.raises(atv.A10NotImplementedError) as raised:
      atv.qualify_target_verifier(
         atv.TargetVerificationQualificationRequest(
            owner_private_root="private:root",
            runtime_digest=DIGEST,
            width_classes=atv.QUALIFIED_WIDTH_CLASSES,
            prefix_length_buckets=2,
            operations=atv.MINIMUM_QUALIFIED_OPERATIONS,
            sequential_parity_proof_digest=DIGEST,
         )
      )
   assert raised.value.code == "not_implemented:target_verification_qualification_absent"

   capability = atv.TargetVerificationCapability(
      runtime_digest=DIGEST,
      model_digest=DIGEST,
      representation_digest=DIGEST,
      runtime_incarnation="incarnation:one",
      maximum_positions=4,
      maximum_bytes=4096,
      generation_policy="deterministic_greedy",
      transaction_modes=atv.TRANSACTION_MODES,
      qualification_binding=DIGEST,
   )
   assert capability.qualification_binding == DIGEST
