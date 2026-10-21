# SPDX-License-Identifier: AGPL-3.0-only
"""A10 scaffold: the bounded multi-position target-verification operation.

SCAFFOLD ONLY.  No target-KV transaction, no verifier backend call, no sequential
parity oracle and no capability advertisement exists here.  Every entry point
validates the closed request shape and then refuses with an explicit coded exception
(``A10NotImplementedError``).

The one honest refusal that is fully implemented is admission-level: an unsupported
generation policy (anything other than the qualified deterministic greedy policy) is
rejected with ``unsupported_generation_policy`` before any state can change.  It is
never silently coerced to greedy, and no transaction is started.

State: ``design_only``; this module earns no closure requirement.  The companion
implementation contract is
``docs/superpowers/specs/2026-09-21-mycelium-a10-implementation-contract.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, NoReturn, runtime_checkable

from mycelium_router.a10_runtime_batch import (
   A10ContractError,
   A10NotImplementedError,
   _bounded_int,
   _digest,
   _finite,
   _reject,
   _text,
)


CAPABILITY_PROTOCOL = "mycelium.target_verification_capability.v1"
RUNTIME_PROTOCOL = "mycelium.target_verification_runtime.v1"
CLAIM_BOUNDARY = (
   "deterministic slices only: the bounded one-call verification transaction and the "
   "sequential parity oracle are implemented and unit-tested in-process; no physical "
   "verifier qualification, no capability advertisement and no closure requirement "
   "earned; the remaining refusal is explicit and coded"
)

# Sourced from the frozen acceptance inventory (target_verifier_requirements).
QUALIFIED_GENERATION_POLICIES = frozenset({"deterministic_greedy"})
TRANSACTION_MODES = (
   "rollback_to_prefix",
   "commit_verified_prefix",
   "commit_verified_prefix_plus_target_correction",
)
QUALIFIED_WIDTH_CLASSES = ("one", "qualified_default", "qualified_maximum")
MINIMUM_QUALIFIED_OPERATIONS = 100
MINIMUM_PREFIX_LENGTH_BUCKETS = 2

# Scaffold-level sanity ceilings only; the qualified widths are owned by the exact
# qualified target runtime and are not decided here.
SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS = 16
SCAFFOLD_MAXIMUM_VERIFICATION_BYTES = 1 << 20

_COMMIT_STATES = ("rolled_back", "committed", "committed_with_correction")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@runtime_checkable
class TargetVerificationPort(Protocol):
   """The declared target-runtime shape a verification operation requires."""

   def execute(self, item: Any) -> Any: ...

   def kv_snapshot(self) -> dict[str, Any]: ...


def _require_verification_port(port: object) -> TargetVerificationPort:
   if not isinstance(port, TargetVerificationPort):
      _reject("invalid_target_verification_port")
   return port


@dataclass(frozen=True)
class TargetVerificationRequest:
   """A private verification request; candidate token IDs stay on the private path.

   Only the bounded width and byte count are bound here -- never the candidate token
   IDs themselves, which must remain on the authenticated private runtime path.
   """

   request_ref: str
   target_kv_ref: str
   runtime_incarnation: str
   committed_prefix_position: int
   committed_prefix_digest: str
   candidate_position_count: int
   candidate_bytes: int
   generation_policy: str
   transaction_mode: str
   deadline_at: float
   cancellation_generation: int
   idempotency_digest: str

   def __post_init__(self) -> None:
      _text(self.request_ref, code="invalid_verification_request_ref")
      _text(self.target_kv_ref, code="invalid_target_kv_ref")
      _text(self.runtime_incarnation, code="invalid_runtime_incarnation")
      _bounded_int(
         self.committed_prefix_position,
         code="invalid_committed_prefix_position",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _digest(self.committed_prefix_digest, code="invalid_committed_prefix_digest")
      _bounded_int(
         self.candidate_position_count,
         code="invalid_candidate_position_count",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS,
      )
      _bounded_int(
         self.candidate_bytes,
         code="invalid_candidate_bytes",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      if self.generation_policy not in QUALIFIED_GENERATION_POLICIES:
         # Never coerced to greedy: an unsupported policy fails admission.
         _reject("unsupported_generation_policy")
      if self.transaction_mode not in TRANSACTION_MODES:
         _reject("invalid_transaction_mode")
      _finite(self.deadline_at, code="invalid_verification_deadline", minimum=0.0)
      _bounded_int(
         self.cancellation_generation,
         code="invalid_cancellation_generation",
         minimum=0,
         maximum=1 << 31,
      )
      _digest(self.idempotency_digest, code="invalid_idempotency_digest")


@dataclass(frozen=True)
class TargetVerificationOutcome:
   """The declared privacy-reduced projection of a verification operation.

   Constructing this record is not evidence that an operation ran; the runtime must
   produce it from its own backend call and KV transaction.
   """

   request_ref: str
   prefix_position: int
   proposed_position_count: int
   verified_count: int
   accepted_prefix_count: int
   correction_count: int
   committed_position: int
   commit_state: str
   cleanup: str

   def __post_init__(self) -> None:
      _text(self.request_ref, code="invalid_verification_request_ref")
      _bounded_int(
         self.prefix_position,
         code="invalid_committed_prefix_position",
         minimum=0,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      _bounded_int(
         self.proposed_position_count,
         code="invalid_candidate_position_count",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS,
      )
      _bounded_int(
         self.verified_count,
         code="invalid_verified_count",
         minimum=0,
         maximum=self.proposed_position_count,
      )
      _bounded_int(
         self.accepted_prefix_count,
         code="invalid_accepted_prefix_count",
         minimum=0,
         maximum=self.verified_count,
      )
      _bounded_int(
         self.correction_count,
         code="invalid_correction_count",
         minimum=0,
         maximum=1,
      )
      if self.commit_state not in _COMMIT_STATES:
         _reject("invalid_commit_state")
      if self.commit_state == "rolled_back":
         if self.committed_position != self.prefix_position:
            _reject("invalid_commit_position")
      else:
         if self.committed_position != (
            self.prefix_position + self.accepted_prefix_count + self.correction_count
         ):
            _reject("invalid_commit_position")
      if self.accepted_prefix_count + self.correction_count > self.proposed_position_count:
         # The correction position is drawn from the same single backend call, so it
         # may never exceed the declared evaluated width.
         _reject("invalid_commit_position")
      if self.commit_state == "committed_with_correction" and self.correction_count != 1:
         _reject("invalid_commit_position")
      if self.commit_state != "committed_with_correction" and self.correction_count != 0:
         _reject("invalid_commit_position")
      _text(self.cleanup, code="invalid_verification_cleanup")


@dataclass(frozen=True)
class TargetVerificationCapability:
   """An advertisement is impossible without a qualification binding."""

   runtime_digest: str
   model_digest: str
   representation_digest: str
   runtime_incarnation: str
   maximum_positions: int
   maximum_bytes: int
   generation_policy: str
   transaction_modes: tuple[str, ...]
   qualification_binding: str

   def __post_init__(self) -> None:
      _digest(self.runtime_digest, code="invalid_runtime_digest")
      _digest(self.model_digest, code="invalid_model_digest")
      _digest(self.representation_digest, code="invalid_representation_digest")
      _text(self.runtime_incarnation, code="invalid_runtime_incarnation")
      _bounded_int(
         self.maximum_positions,
         code="invalid_maximum_positions",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS,
      )
      _bounded_int(
         self.maximum_bytes,
         code="invalid_maximum_bytes",
         minimum=1,
         maximum=SCAFFOLD_MAXIMUM_VERIFICATION_BYTES,
      )
      if self.generation_policy not in QUALIFIED_GENERATION_POLICIES:
         _reject("unsupported_generation_policy")
      if (
         not isinstance(self.transaction_modes, tuple)
         or not self.transaction_modes
         or any(mode not in TRANSACTION_MODES for mode in self.transaction_modes)
      ):
         _reject("invalid_transaction_modes")
      if not isinstance(self.qualification_binding, str) or _SHA256.fullmatch(
         self.qualification_binding
      ) is None:
         # Specification section 7: only a physically qualified runtime may advertise.
         _reject("unqualified_target_verification_capability")


@dataclass(frozen=True)
class TargetVerificationQualificationRequest:
   """The correctness-matrix inputs a qualification would have to bind."""

   owner_private_root: str
   runtime_digest: str
   width_classes: tuple[str, ...]
   prefix_length_buckets: int
   operations: int
   sequential_parity_proof_digest: str

   def __post_init__(self) -> None:
      _text(self.owner_private_root, code="invalid_qualification_root")
      _digest(self.runtime_digest, code="invalid_runtime_digest")
      if tuple(sorted(self.width_classes)) != tuple(sorted(QUALIFIED_WIDTH_CLASSES)):
         _reject("invalid_qualified_width_classes")
      _bounded_int(
         self.prefix_length_buckets,
         code="insufficient_prefix_length_buckets",
         minimum=MINIMUM_PREFIX_LENGTH_BUCKETS,
         maximum=1_024,
      )
      _bounded_int(
         self.operations,
         code="insufficient_qualification_operations",
         minimum=MINIMUM_QUALIFIED_OPERATIONS,
         maximum=1 << 31,
      )
      _digest(
         self.sequential_parity_proof_digest,
         code="missing_sequential_parity_proof",
      )


def verify_candidate_positions(
   port: object,
   request: TargetVerificationRequest,
) -> object:
   """Evaluate bounded candidate positions in one real target backend call.

   IMPLEMENTED (deterministic slice S3).  A target-owned KV transaction is created from
   the exact committed prefix, every bounded position is evaluated causally in one
   backend call, and the transaction then rolls back, commits the verified prefix, or
   commits the verified prefix plus one target-authoritative correction.  The
   transaction is never left open.  The mechanics live in
   ``mycelium_router.a10_verifier_execution``; this entry point stays the machine
   source named by the implementation contract.
   """

   from mycelium_router.a10_verifier_execution import (
      verify_candidate_positions as _verify,
   )

   return _verify(port, request)


def sequential_parity_reference(
   port: object,
   request: TargetVerificationRequest,
) -> object:
   """Produce the sequential target-authority oracle for the same prefix.

   IMPLEMENTED (deterministic slice S3).  The oracle replays the target one position at
   a time from the identical committed prefix and compares per-position target decisions
   and the final committed prefix position and digest; raw logits, tensors, KV bytes and
   candidate token IDs never leave the private path.
   """

   from mycelium_router.a10_verifier_execution import (
      sequential_parity_reference as _parity,
   )

   return _parity(port, request)


def qualify_target_verifier(
   request: TargetVerificationQualificationRequest,
   operations: Any = None,
   negatives: Any = (),
) -> dict[str, Any]:
   """Produce the owner-private verifier qualification matrix.

   Decided from executed operation records only
   (``mycelium_router.a10_qualification``): at least 100 operations over the widths
   ``one``, ``qualified_default`` and ``qualified_maximum`` in at least two prefix
   buckets, every transaction mode, exact sequential parity for every operation, and
   the required negatives.  ``qualified`` additionally needs live provenance through
   the product dispatcher, and only then is a capability (bound to the bundle's
   digest) emitted.  Without executed operations the entry point refuses with
   ``target_verification_qualification_absent``.
   """

   if not isinstance(request, TargetVerificationQualificationRequest):
      _reject("invalid_target_verification_qualification_argument")
   if operations is None:
      raise A10NotImplementedError("target_verification_qualification_absent")
   from mycelium_router.a10_qualification import (
      qualify_target_verifier_from_operations,
   )

   return qualify_target_verifier_from_operations(request, operations, negatives)


BOUNDARIES: tuple[tuple[str, str], ...] = (
   ("qualify_target_verifier", "target_verification_qualification_absent"),
)


__all__ = [
   "BOUNDARIES",
   "CAPABILITY_PROTOCOL",
   "RUNTIME_PROTOCOL",
   "TargetVerificationCapability",
   "TargetVerificationOutcome",
   "TargetVerificationPort",
   "TargetVerificationQualificationRequest",
   "TargetVerificationRequest",
   "qualify_target_verifier",
   "sequential_parity_reference",
   "verify_candidate_positions",
   "A10ContractError",
   "A10NotImplementedError",
]
