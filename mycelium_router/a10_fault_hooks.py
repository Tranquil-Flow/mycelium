# SPDX-License-Identifier: AGPL-3.0-only
"""A10 runtime-batch fault hooks: opt-in, single-shot, never on by default.

Nine of the twelve required runtime-batch negatives (specification section 10) need
a fault injected *inside* the runtime-batch path, which the ordinary product API
cannot do: timeout, incompatible member, stale prefix, partial member failure,
shared backend failure, wrong result count, runtime exit, shutdown and verifier
rollback.  This module is the injection layer, and only that.

Safety properties (each one is pinned by ``tests/a10_deterministic/test_a10_fault_hooks.py``):

* **Opt-in.**  ``A10FaultHooks`` cannot be constructed without the exact A10 fleet
  grant, the same grant that already gates the A10 verify and batch-policy
  controls (mirrored here; a test pins it to ``mycelium_a10_verify_control``).  Nothing here reads the environment: the caller passes the grant it
  holds.
* **Not in production paths.**  No production module imports this one.  Only the
  A10 gate runner (through ``a10_fault_negatives``) does; a source scan in the
  test-suite fails if any other production module names it.  The wrappers add
  nothing to a port that is not explicitly wrapped, and a wrapped port with no
  armed fault is a pure pass-through.
* **Single shot, closed vocabulary.**  One fault is armed at a time, from the nine
  declared kinds, and is spent when it fires.  A runtime exit and a shutdown latch
  the wrapped port dead/closing afterwards, because that is what they are.
* **No evidence claim.**  ``status()`` never claims a qualification or physical
  evidence; a fault is a control action, not a result.

The wrappers speak the two ports the A10 path already defines
(``BatchDispatchPort`` and ``TargetVerificationRuntimePort``), so the same
wrapper can sit around a CPU reference port (the gate's deterministic execution)
or around a node's real stage runtime (the physical execution, still pending).
"""

from __future__ import annotations

import hmac
import threading
from dataclasses import replace
from typing import Any

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   MemberExecution,
   RuntimeBatchRequest,
)
from mycelium_router.contracts import RuntimeBatchKey, RuntimeResult

PROTOCOL = "mycelium.a10_fault_hooks.v1"

#: The A10 fleet grant, mirrored from ``mycelium_a10_verify_control`` because the
#: router package must not import a top-level product module; a test pins equality.
EXACT_FLEET_GRANT = "FLEET GRANTED TO A10"

#: The runtime-batch negatives the product API cannot drive, in inventory order.
FAULT_KINDS = (
   "timeout",
   "incompatible_member",
   "stale_prefix",
   "partial_member_failure",
   "shared_backend_failure",
   "wrong_result_count",
   "runtime_exit",
   "shutdown",
   "verifier_rollback",
)

#: Faults applied to one backend invocation of a batch dispatch port.
DISPATCH_FAULTS = (
   "timeout",
   "stale_prefix",
   "partial_member_failure",
   "shared_backend_failure",
   "wrong_result_count",
   "runtime_exit",
   "shutdown",
)

_FOREIGN_STAGE_SIGNATURE = "stage-signature-foreign"


class A10FaultHookRefused(ValueError):
   """A coded refusal to build or arm a fault hook."""


class A10InjectedFault(RuntimeError):
   """The failure a wrapped port raises; it carries a stable, privacy-free code."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


class InjectedTimeout(TimeoutError):
   """A backend invocation that exceeded its deadline (injected)."""

   code = "backend_timeout"


def _exact_grant(value: object) -> bool:
   return isinstance(value, str) and hmac.compare_digest(
      value.encode("utf-8"), EXACT_FLEET_GRANT.encode("ascii")
   )


class A10FaultHooks:
   """The armed-fault register shared by the wrappers of one gate execution."""

   def __init__(self, *, grant: object) -> None:
      if not _exact_grant(grant):
         raise A10FaultHookRefused("explicit_fleet_grant_required")
      self._lock = threading.Lock()
      self._armed: tuple[str, str | None] | None = None
      self._fired: list[str] = []

   def arm(self, kind: str, *, victim: str | None = None) -> None:
      if kind not in FAULT_KINDS:
         raise A10FaultHookRefused("unknown_fault_kind")
      if victim is not None and (not isinstance(victim, str) or not victim):
         raise A10FaultHookRefused("invalid_fault_victim")
      with self._lock:
         if self._armed is not None:
            raise A10FaultHookRefused("fault_already_armed")
         self._armed = (kind, victim)

   def armed(self) -> tuple[str, str | None] | None:
      with self._lock:
         return self._armed

   def fired(self) -> tuple[str, ...]:
      with self._lock:
         return tuple(self._fired)

   def take(self, kinds: tuple[str, ...]) -> tuple[str, str | None] | None:
      """Consume the armed fault when it is one of ``kinds`` (else leave it)."""

      with self._lock:
         if self._armed is None or self._armed[0] not in kinds:
            return None
         taken, self._armed = self._armed, None
         self._fired.append(taken[0])
         return taken

   def status(self) -> dict[str, Any]:
      """Privacy-reduced status; a fault control is never evidence."""

      with self._lock:
         return {
            "protocol": PROTOCOL,
            "armed": None if self._armed is None else self._armed[0],
            "fired_count": len(self._fired),
            "qualification_claim": False,
            "physical_evidence": False,
         }

   def perturb_member_key(self, member_ref: str, key: RuntimeBatchKey) -> RuntimeBatchKey:
      """The compatibility key a member is submitted with.

      Unchanged unless an ``incompatible_member`` fault is armed for this member,
      in which case the member is offered under a foreign stage signature and the
      fault is spent.
      """

      with self._lock:
         if self._armed is None or self._armed[0] != "incompatible_member":
            return key
         if self._armed[1] is not None and self._armed[1] != member_ref:
            return key
         self._armed = None
         self._fired.append("incompatible_member")
      return replace(key, stage_signature=_FOREIGN_STAGE_SIGNATURE)


class FaultInjectingDispatchPort:
   """Wrap a ``BatchDispatchPort``; inject the armed dispatch fault, else pass through."""

   def __init__(self, inner: Any, hooks: A10FaultHooks) -> None:
      self._inner = inner
      self._hooks = hooks
      self._latched: str | None = None

   def __getattr__(self, name: str) -> Any:
      # Only reached for names this wrapper does not define: everything else about
      # the wrapped port (offers, intervals, cancellation) is the real port's.
      return getattr(self._inner, name)

   def dispatch_mode(self) -> str:
      return self._inner.dispatch_mode()

   def execute_batch(self, request: RuntimeBatchRequest) -> BatchExecutionReport:
      if self._latched == "runtime_exit":
         raise A10InjectedFault("runtime_exit")
      if self._latched == "shutdown":
         raise A10InjectedFault("runtime_shutdown")
      taken = self._hooks.take(DISPATCH_FAULTS)
      if taken is None:
         return self._inner.execute_batch(request)
      kind, victim = taken
      victim = victim if victim in request.member_refs else request.member_refs[-1]
      if kind == "timeout":
         raise InjectedTimeout("backend_timeout")
      if kind == "shared_backend_failure":
         raise A10InjectedFault("shared_backend_failure")
      if kind == "runtime_exit":
         self._latched = "runtime_exit"
         raise A10InjectedFault("runtime_exit")
      if kind == "shutdown":
         self._latched = "shutdown"
         raise A10InjectedFault("runtime_shutdown")

      report = self._inner.execute_batch(request)
      executed = list(report.executed)
      if kind == "partial_member_failure":
         executed = [
            replace(
               execution,
               result=RuntimeResult(
                  success=False,
                  failure_scope="PLACEMENT",
                  failure_reason="injected_member_failure",
               ),
            )
            if execution.member_ref == victim
            else execution
            for execution in executed
         ]
      elif kind == "wrong_result_count":
         # One result more than members offered: a duplicate of the last one.
         executed.append(executed[-1])
      elif kind == "stale_prefix":
         executed = [
            _stale(execution) if execution.member_ref == victim else execution
            for execution in executed
         ]
      return replace(report, executed=tuple(executed))


def _stale(execution: MemberExecution) -> MemberExecution:
   """The same member reporting a prefix older than the one already committed."""

   return replace(execution, committed_prefix=max(0, execution.committed_prefix - 2))


class _FaultingTransaction:
   def __init__(self, inner: Any, hooks: A10FaultHooks) -> None:
      self._inner = inner
      self._hooks = hooks

   def __getattr__(self, name: str) -> Any:
      return getattr(self._inner, name)

   def committed_prefix_position(self) -> int:
      return self._inner.committed_prefix_position()

   def committed_prefix_digest(self) -> str:
      return self._inner.committed_prefix_digest()

   def evaluate_positions(self, candidate_position_count: int) -> Any:
      if self._hooks.take(("verifier_rollback",)) is not None:
         raise A10InjectedFault("verifier_transaction_failure")
      return self._inner.evaluate_positions(candidate_position_count)

   def commit_verified_prefix(self, committed_count: int) -> None:
      self._inner.commit_verified_prefix(committed_count)

   def rollback_to_prefix(self) -> None:
      self._inner.rollback_to_prefix()

   def cleanup(self) -> str:
      return self._inner.cleanup()


class FaultInjectingVerificationPort:
   """Wrap a ``TargetVerificationRuntimePort``; fail the verifier's backend call."""

   def __init__(self, inner: Any, hooks: A10FaultHooks) -> None:
      self._inner = inner
      self._hooks = hooks

   def __getattr__(self, name: str) -> Any:
      return getattr(self._inner, name)

   def begin_verification(self) -> _FaultingTransaction:
      return _FaultingTransaction(self._inner.begin_verification(), self._hooks)

   def sequential_reference(self, candidate_position_count: int) -> Any:
      return self._inner.sequential_reference(candidate_position_count)


__all__ = [
   "DISPATCH_FAULTS",
   "EXACT_FLEET_GRANT",
   "FAULT_KINDS",
   "PROTOCOL",
   "A10FaultHookRefused",
   "A10FaultHooks",
   "A10InjectedFault",
   "FaultInjectingDispatchPort",
   "FaultInjectingVerificationPort",
   "InjectedTimeout",
]
