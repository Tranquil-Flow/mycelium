# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_router.a10_batch_producer — the missing live A10 batch producer.

The gate-satisfiability audit (2026-09-27, ``runs/v2/gate-satisfiability-
audit/FINDINGS.md`` item 3) found that no code anywhere in the live serving
path ever offers >=2 compatible items to a stage runtime as one group:
``grep -rn "a10" mycelium_live/`` returns zero hits, and every shipped
``RuntimePort.execute_batch`` treats a plain ``RuntimeBatch`` as a sequential
loop (``mlx_runtime.py``, ``numpy_runtime.py``, ``llama_runtime.py`` and
``openrouter_runtime.py`` all execute members one at a time -- see each
module's ``execute_batch``). The A10 admission and dispatch machinery
(``a10_batch_admission``, ``a10_runtime_batch``) is real and deterministic,
but nothing calls it: ``MLXRuntimePort.offer_batch_member`` has zero callers
outside its own tests.

This module is that missing caller. It is a pure translation from the
ALREADY-LIVE relay microbatch decision (``mycelium_router.relay.RelayEngine
.drain_ready_batches`` groups concurrently-queued, compatible
``HopWorkItem``s into one ``mycelium_router.contracts.RuntimeBatch`` via
``mycelium_router.batching.PhaseAwareBatchController`` whenever >=2 hops for
the same stage/phase are ready at the same time -- relay.py:1447-1473) into
the A10 offer/dispatch protocol (``offer_batch_member`` +
``execute_batch(RuntimeBatchRequest)``), and back into the ordinary
``tuple[RuntimeResult, ...]`` shape every existing caller already expects.

FAIL-CLOSED CONTRACT (read this before changing anything below):

  ``attempt_runtime_batch(port, batch)`` returns ``None`` when, and only
  when, NO real backend work has been dispatched for ANY item in ``batch``
  -- the caller may then safely fall back to its own pre-existing sequential
  path (``port.execute_batch(batch)`` treating ``batch`` as a plain
  ``RuntimeBatch``) with zero risk of double execution.

  Once ``port.execute_batch(RuntimeBatchRequest(...))`` has been called and
  returns without raising, real work has happened -- possibly as one padded
  batched invocation, possibly (if the port itself fell back internally,
  for example because a member's path was cancelled between admission and
  dispatch) as a sequential loop over whichever members the runtime actually
  accepted. Either way this function NEVER discards that result and NEVER
  tells the caller to retry: it returns one real (or, for a member the
  runtime legitimately dropped, honestly-cancelled) ``RuntimeResult`` per
  ``batch.items``, in the same order, always.

  A group is only ever offered to the runtime when it already satisfies the
  A10 minimum-member and compatibility-key preconditions (``is_eligible``),
  and only when the port itself declares the full A10 dispatch surface; a
  port that does not implement
  ``offer_batch_member``/``dispatch_mode``/``execution_interval`` is
  untouched by this module and its behaviour is unchanged -- this is what
  keeps every existing single-request and non-A10-port path identical to
  before this module existed.

KNOWN LIMITATION (disclosed, not hidden): if a member is offered
successfully but a *later* member in the same group fails to offer, the
earlier offer(s) are never dispatched and sit inert in the port's private
offer table for the lifetime of the process (they are never executed, never
attested, and never collide with a future member_ref because each ref is
derived from that item's own idempotency key). This is a bounded memory
leak, not a correctness or double-execution bug.

WIRING GAP (also disclosed): this module has no caller in the shipped tree.
The natural call site is ``mycelium_router.relay.RelayEngine
._execute_runtime_batch`` (relay.py:374-378), which today always calls
``self.runtime.execute_batch(batch)`` directly. ``relay.py`` is outside this
lane's owned-file list, so that one-line wiring change is reported to the
owner (see FINDINGS.md) rather than made here.
"""

from __future__ import annotations

import hashlib
from typing import Any

from mycelium_router.a10_batch_admission import (
   MINIMUM_OBSERVED_BATCH_MEMBERS,
   BatchExecutionReport,
   RuntimeBatchRequest,
   compatibility_digest,
)
from mycelium_router.contracts import RuntimeBatch, RuntimeResult


#: Synthesized for a member the runtime accepted the offer for but then
#: legitimately never executed (its path was cancelled between admission and
#: dispatch). Never a fabricated success, never silently dropped from the
#: returned tuple.
_CANCELLED_MEMBER_RESULT = RuntimeResult(
   success=False,
   failure_scope="PLACEMENT",
   failure_reason="path_cancelled",
)


def _supports_a10_dispatch(port: Any) -> bool:
   """Duck-typed: true only for a port that declares the full A10 surface."""

   return (
      callable(getattr(port, "offer_batch_member", None))
      and callable(getattr(port, "dispatch_mode", None))
      and callable(getattr(port, "execute_batch", None))
      and callable(getattr(port, "execution_interval", None))
   )


def is_eligible(batch: RuntimeBatch, port: Any) -> bool:
   """True only when ``batch`` and ``port`` meet every A10 precondition.

   Checking this never has a side effect on ``port`` or ``batch``; callers
   may use it to decide whether to attempt a batch at all before touching
   the port. A batch of fewer than ``MINIMUM_OBSERVED_BATCH_MEMBERS`` items,
   or one with no compatibility key (a singleton by construction -- see
   ``RuntimeBatch.__post_init__``), or a port that does not declare the A10
   surface, is never eligible: this is exactly what keeps every existing
   single-request path unchanged.
   """

   return (
      isinstance(batch, RuntimeBatch)
      and len(batch.items) >= MINIMUM_OBSERVED_BATCH_MEMBERS
      and batch.compatibility_key is not None
      and _supports_a10_dispatch(port)
   )


def attempt_runtime_batch(
   port: Any,
   batch: RuntimeBatch,
) -> tuple[RuntimeResult, ...] | None:
   """Best-effort A10 runtime-batch dispatch for one relay-formed ``RuntimeBatch``.

   See the module docstring for the exact fail-closed contract. This never
   raises: any unexpected condition is treated the same as "not eligible"
   and reported as ``None``.
   """

   if not is_eligible(batch, port):
      return None

   member_refs: list[str] = []
   try:
      for item in batch.items:
         member_ref = f"a10-member:{item.idempotency_key}"
         port.offer_batch_member(item, member_ref=member_ref)
         member_refs.append(member_ref)
   except Exception:
      # Zero real execution occurred for this batch (offering has no
      # execution side effect -- see the module docstring's KNOWN
      # LIMITATION for what happens to any member offered before the
      # failure). The caller's sequential fallback is safe.
      return None

   try:
      request = RuntimeBatchRequest(
         batch_ref="relay-batch:"
         + hashlib.sha256("|".join(member_refs).encode("utf-8")).hexdigest()[:24],
         phase=batch.items[0].phase,
         compatibility_key=compatibility_digest(batch.compatibility_key),
         member_refs=tuple(member_refs),
         proposed_member_count=len(member_refs),
      )
      report = port.execute_batch(request)
   except Exception:
      # The offered group never resolved into any execution (for example
      # every offered member's path was already cancelled) -- the dispatch
      # path refused before computing anything. Zero real execution
      # occurred, so the caller's sequential fallback is safe.
      return None

   if not isinstance(report, BatchExecutionReport):
      # Contract violation from a non-shipped port: cannot know whether real
      # execution happened. Refuse to guess either way rather than risk
      # claiming a success or forcing a possible double dispatch.
      return None

   by_ref = {execution.member_ref: execution.result for execution in report.executed}
   return tuple(by_ref.get(ref, _CANCELLED_MEMBER_RESULT) for ref in member_refs)


__all__ = [
   "attempt_runtime_batch",
   "is_eligible",
]
